"""Loading of reference data, ingest records, and append-only run/override storage.

CRM and plan values live here and are never handed to the LLM. `reference_for()`
is the only way a check reaches them, and it is called *after* extraction.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR, ROOT, RUNS_DIR
from .textutil import redact_long_digits

_LOCK = threading.Lock()


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class Store:
    def __init__(self) -> None:
        self.leads_doc = _read_json(DATA_DIR / "leads.json")
        self.plans_doc = _read_json(DATA_DIR / "plans.json")
        self.checks_doc = _read_json(DATA_DIR / "checks.json")
        self.leads = {lead["lead_id"]: lead for lead in self.leads_doc["leads"]}
        self.plans = {plan["plan_id"]: plan for plan in self.plans_doc["plans"]}
        self.checks = self.checks_doc["checks"]
        self.ingests: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.run_ids_by_lead: dict[str, list[str]] = {}
        RUNS_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------------- reference

    def lead(self, lead_id: str) -> dict:
        try:
            return self.leads[lead_id]
        except KeyError:
            raise KeyError(f"unknown lead_id {lead_id!r}") from None

    def plan_for(self, lead_id: str) -> dict:
        lead = self.lead(lead_id)
        try:
            return self.plans[lead["plan_id"]]
        except KeyError:
            raise KeyError(f"lead {lead_id} references unknown plan {lead['plan_id']!r}") from None

    def reference_for(self, check: dict, lead_id: str):
        """Resolve the CRM / plan value a Type B check compares against.

        Returns (value, label, source). Called only after the LLM has finished.
        """
        ref = check["script_or_rule"].get("reference") or {}
        source, path = ref.get("source"), ref.get("path")
        if source == "crm":
            value = self.lead(lead_id)["crm"].get(path)
        elif source == "plan":
            value = self.plan_for(lead_id).get(path)
        else:
            raise ValueError(f"check {check['check_id']}: unsupported reference source {source!r}")
        return value, ref.get("label", path), source

    def secret_reference_values(self, lead_id: str) -> list[str]:
        """Every CRM/plan value the LLM must never be shown. Used by the leak guard."""
        out: list[str] = []
        crm = self.lead(lead_id).get("crm", {})
        plan = self.plan_for(lead_id)
        for value in list(crm.values()) + [
            plan.get("peak_rate_cents"),
            plan.get("daily_supply_cents"),
            plan.get("solar_fit_cents"),
        ]:
            if value is None or isinstance(value, bool):
                continue
            text = str(value).strip()
            if len(text) >= 3:
                out.append(text)
        return out

    # ------------------------------------------------------------------- ingest

    def ingest(self, lead_id: str, redact_digit_run: int = 13) -> dict:
        """Attach the local transcript for a lead. No file picker in the happy path."""
        lead = self.lead(lead_id)
        path = ROOT / lead["transcript_path"]
        if not path.exists():
            raise FileNotFoundError(f"transcript missing for lead {lead_id}: {path}")
        raw_bytes = path.read_bytes()
        doc = json.loads(raw_bytes.decode("utf-8"))

        if doc.get("lead_id") != lead_id:
            raise ValueError(
                f"transcript {path.name} declares lead_id {doc.get('lead_id')!r}, expected {lead_id!r}"
            )

        turns, redactions = [], 0
        for i, turn in enumerate(doc.get("turns", [])):
            text, masked = redact_long_digits(turn.get("text", ""), redact_digit_run)
            redactions += masked
            start = float(turn.get("start_sec", 0))
            turns.append(
                {
                    "idx": int(turn.get("idx", i)),
                    "speaker": turn.get("speaker", "unknown"),
                    "speaker_name": turn.get("speaker_name", turn.get("speaker", "unknown")),
                    "text": text,
                    "start_sec": start,
                    "end_sec": float(turn.get("end_sec", start)),
                    "asr_confidence": float(turn.get("asr_confidence", 1.0)),
                    "redacted": masked > 0,
                }
            )
        turns.sort(key=lambda t: (t["start_sec"], t["idx"]))

        record = {
            "ingest_id": f"ING-{lead_id}-{uuid.uuid4().hex[:8]}",
            "lead_id": lead_id,
            "source": "local_fixture",
            "transcript_path": lead["transcript_path"],
            "transcript_sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "call_id": doc.get("call_id"),
            "recorded_at": doc.get("recorded_at"),
            "audio_quality": doc.get("audio_quality", "unknown"),
            "audio_quality_note": doc.get("audio_quality_note"),
            "asr_engine": doc.get("asr_engine"),
            "duration_sec": float(doc.get("duration_sec") or (turns[-1]["end_sec"] if turns else 0)),
            "turn_count": len(turns),
            "redactions_applied": redactions,
            "redaction_rule": f">= {redact_digit_run} consecutive digits masked at ingest",
            "ingested_at": utcnow(),
            "turns": turns,
        }
        with _LOCK:
            self.ingests[record["ingest_id"]] = record
        return record

    def get_ingest(self, ingest_id: str) -> dict:
        try:
            return self.ingests[ingest_id]
        except KeyError:
            raise KeyError(f"unknown ingest_id {ingest_id!r}") from None

    # ---------------------------------------------------------------------- runs

    def new_run_id(self, lead_id: str) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        return f"RUN-{lead_id}-{stamp}-{uuid.uuid4().hex[:4]}"

    def save_run(self, run: dict) -> dict:
        with _LOCK:
            self.runs[run["run_id"]] = run
            self.run_ids_by_lead.setdefault(run["lead_id"], [])
            if run["run_id"] not in self.run_ids_by_lead[run["lead_id"]]:
                self.run_ids_by_lead[run["lead_id"]].append(run["run_id"])
            path = RUNS_DIR / f"{run['run_id']}.json"
            path.write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")
        return run

    def get_run(self, run_id: str) -> dict:
        with _LOCK:
            run = self.runs.get(run_id)
        if run is not None:
            return run
        path = RUNS_DIR / f"{run_id}.json"
        if path.exists():
            run = _read_json(path)
            with _LOCK:
                self.runs[run_id] = run
            return run
        raise KeyError(f"unknown run_id {run_id!r}")

    def latest_run_for(self, lead_id: str) -> dict | None:
        ids = self.run_ids_by_lead.get(lead_id) or []
        return self.get_run(ids[-1]) if ids else None

    # ----------------------------------------------------------- override log

    def append_override(self, entry: dict) -> None:
        """Append-only. Overrides are added to history; nothing is ever rewritten."""
        with _LOCK:
            path = RUNS_DIR / "override_log.jsonl"
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def read_override_log(self, limit: int = 200) -> list[dict]:
        path = RUNS_DIR / "override_log.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return rows[-limit:]
