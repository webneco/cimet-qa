"""Ingest -> score -> gate, plus the override path.

The run artifact this produces is the audit record: it contains the checklist
version in force, the transcript checksum, every result with its evidence, the
gate decision with its reasons, and the full override history. Anything a team
leader is asked to trust is reconstructable from that one file.
"""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import __version__, asr, gate
from .config import RUNS_DIR
from .llm import Extractor, LeakGuardError
from .offline_extract import OfflineExtractor
from .scoring import score_card_handling, score_type_a, score_type_b, score_type_c
from .store import Store, utcnow
from .textutil import mmss, redact_long_digits

MIN_OVERRIDE_REASON = 10


class PipelineError(ValueError):
    """A caller mistake - surfaced to the UI as a 4xx, not a stack trace."""


def _msg(exc: Exception) -> str:
    """KeyError stringifies with quotes around it; unwrap so the UI reads cleanly."""
    return str(exc.args[0]) if exc.args else str(exc)


class Pipeline:
    def __init__(self, store: Store, settings) -> None:
        self.store = store
        self.settings = settings
        self.llm = Extractor(settings)
        self.offline = OfflineExtractor()

    # ------------------------------------------------------------------ status

    def extractor_status(self) -> dict:
        return {
            "type_a": "deterministic script-span matcher (no LLM)",
            "type_b_primary": self.llm.name if self.llm.available else "unavailable",
            "type_b_active": self.llm.name if self.llm.available else self.offline.name,
            "type_b_fallback": self.offline.name,
            "llm_available": self.llm.available,
            "llm_unavailable_reason": self.llm.unavailable_reason,
            "model": self.settings.model if self.llm.available else None,
        }

    # ------------------------------------------------------------------ ingest

    def ingest(self, lead_id: str, source: str = "auto") -> dict:
        if not lead_id:
            raise PipelineError("lead_id is required")
        try:
            record = self.store.ingest(lead_id, self.settings.redact_digit_run, source)
        except KeyError as exc:
            raise PipelineError(_msg(exc)) from None
        return record

    def ingest_recording(self, lead_id: str, audio: bytes, filename: str, content_type: str,
                         meta: dict | None = None, on_stage=None) -> dict:
        """Dialler recording -> ASR -> transcript stored against the lead -> scored run.

        The recording is saved before ASR runs, so a transcription failure never loses
        the call. The transcript is written in the same shape as the fixtures, with the
        engine, the audio checksum and the speaker mapping on it, then scored exactly
        like any other transcript.
        """
        stage = on_stage or (lambda _name: None)
        try:
            lead = self.store.lead(lead_id)
        except KeyError as exc:
            raise PipelineError(_msg(exc)) from None
        if not audio:
            raise PipelineError("the recording is empty")
        ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ".wav"
        audio_path = self.store.save_audio(lead_id, audio, ext)

        stage("transcribing")
        t0 = time.perf_counter()
        try:
            result = asr.transcribe(audio, filename, content_type, self.settings)
        except asr.AsrError as exc:
            raise PipelineError(str(exc)) from None
        asr_ms = int((time.perf_counter() - t0) * 1000)

        turns = result["turns"]
        meta = meta or {}
        doc = {
            "lead_id": lead_id,
            "call_id": meta.get("call_id") or lead.get("call_id"),
            "recorded_at": meta.get("recorded_at") or lead.get("recorded_at"),
            "duration_sec": turns[-1]["end_sec"],
            "audio_quality": "from_recording",
            "asr_engine": result["asr_engine"],
            "asr_ms": asr_ms,
            "channels": "mono_diarised",
            "speaker_mapping": result["speaker_mapping"],
            "audio": {
                "sha256": hashlib.sha256(audio).hexdigest(),
                "bytes": len(audio),
                "filename": filename,
                "stored_as": audio_path.name,
                "received_from": meta.get("received_from", "dialler_webhook"),
            },
            "turns": turns,
        }
        # Raw ASR text can hold a spoken card number. Mask before the transcript
        # touches disk, and carry the count so the ingest still reports it.
        masked = 0
        for t in doc["turns"]:
            t["text"], n = redact_long_digits(t["text"], self.settings.redact_digit_run)
            masked += n
        doc["redactions_at_asr"] = masked
        self.store.save_asr_transcript(lead_id, doc)

        stage("scoring")
        run = self.run_lead(lead_id, source="asr")
        run["pipeline_trace"].insert(0, {
            "stage": "asr",
            "detail": f"{result['asr_engine']}: {len(turns)} turns, agent = "
                      f"{result['speaker_mapping']['agent_speaker']} "
                      f"({result['speaker_mapping']['method']})",
            "ms": asr_ms,
        })
        self.store.save_run(run)
        return run

    # ------------------------------------------------------------------- score

    def _extract_one(self, check: dict, turns: list[dict], secrets: list[str]) -> tuple[dict | None, str | None]:
        """Run the LLM extractor, falling back to the deterministic one on failure."""
        if self.llm.available:
            try:
                return self.llm.extract(check, turns, secrets), None
            except LeakGuardError as exc:
                # A leak guard trip is a pipeline defect, never a customer's problem.
                return None, f"leak guard blocked the request ({exc})"
            except Exception as exc:
                fallback = self.offline.extract(check, turns, secrets)
                fallback["_meta"]["degraded_from"] = self.llm.name
                fallback["_meta"]["degrade_reason"] = f"{type(exc).__name__}: {exc}"
                return fallback, None
        return self.offline.extract(check, turns, secrets), None

    def score(self, ingest_id: str, record: bool = True) -> dict:
        """Score an ingest. record=False keeps the run in memory only (used by the eval)."""
        started = time.perf_counter()
        try:
            ingest = self.store.get_ingest(ingest_id)
        except KeyError as exc:
            raise PipelineError(_msg(exc)) from None

        lead_id = ingest["lead_id"]
        lead = self.store.lead(lead_id)
        plan = self.store.plan_for(lead_id)
        turns = ingest["turns"]
        secrets = self.store.secret_reference_values(lead_id)
        floor = self.settings.confidence_floor_critical
        trace: list[dict] = [{
            "stage": "ingest",
            "detail": f"{ingest['turn_count']} turns, {mmss(ingest['duration_sec'])} of audio, "
                      f"{ingest['redactions_applied']} redaction(s) applied",
            "ms": 0,
        }]

        pack = self.store.pack_for(lead_id)
        checks = pack["checks"]
        type_b_checks = [c for c in checks if c["type"] == "B"]
        t0 = time.perf_counter()
        extractions: dict[str, tuple[dict | None, str | None]] = {}
        if type_b_checks:
            with ThreadPoolExecutor(max_workers=min(4, len(type_b_checks))) as pool:
                futures = {
                    pool.submit(self._extract_one, check, turns, secrets): check["check_id"]
                    for check in type_b_checks
                }
                for future, check_id in futures.items():
                    try:
                        extractions[check_id] = future.result()
                    except Exception as exc:  # pragma: no cover - defensive
                        extractions[check_id] = (None, f"{type(exc).__name__}: {exc}")
        extract_ms = int((time.perf_counter() - t0) * 1000)
        trace.append({
            "stage": "extract",
            "detail": f"{len(type_b_checks)} Type B extraction(s) via "
                      f"{self.extractor_status()['type_b_active']}",
            "ms": extract_ms,
        })

        t0 = time.perf_counter()
        results: list[dict] = []
        for check in checks:
            if check["type"] == "A" and check["script_or_rule"].get("kind") == "card_handling":
                results.append(score_card_handling(check, turns, floor))
            elif check["type"] == "A":
                results.append(score_type_a(check, turns, floor))
            elif check["type"] == "B":
                extraction, error = extractions.get(check["check_id"], (None, "extractor not run"))
                reference, label, source = self.store.reference_for(check, lead_id)
                results.append(
                    score_type_b(check, turns, extraction, reference, label, source, floor, error)
                )
            elif check["type"] == "C":
                results.append(score_type_c(check, turns, floor))
            else:
                raise PipelineError(f"check {check['check_id']} has unknown type {check['type']!r}")
        trace.append({
            "stage": "score",
            "detail": f"{len(results)} checks scored against checklist "
                      f"{pack['checklist_version']}",
            "ms": int((time.perf_counter() - t0) * 1000),
        })

        t0 = time.perf_counter()
        decision = gate.evaluate(results, lead_id, self.settings)
        trace.append({
            "stage": "gate",
            "detail": f"{decision['display_status']}"
                      + (f" -> {decision['queue']['queue']}" if decision.get("queue") else ""),
            "ms": int((time.perf_counter() - t0) * 1000),
        })

        run = {
            "run_id": self.store.new_run_id(lead_id),
            "lead_id": lead_id,
            "ingest_id": ingest_id,
            "app_version": __version__,
            "started_at": utcnow(),
            "completed_at": utcnow(),
            "duration_ms": int((time.perf_counter() - started) * 1000),
            "checklist": {
                "pack_id": pack.get("pack_id", "default"),
                "retailer": pack["retailer"],
                "checklist_id": pack["checklist_id"],
                "checklist_version": pack["checklist_version"],
                "effective_from": pack["effective_from"],
                "check_count": len(checks),
            },
            "lead": {
                "lead_id": lead_id,
                "retailer": lead["retailer"],
                "vertical": lead.get("vertical", "energy"),
                "commercials": {k: lead[k] for k in ("tmc_cents", "new_development_fee_cents", "fee_applicable")
                                if k in lead} or None,
                "plan_id": lead["plan_id"],
                "channel": lead.get("channel"),
                "agent_id": lead.get("agent_id"),
                "agent_name": lead.get("agent_name"),
                "call_id": lead.get("call_id"),
                "recorded_at": lead.get("recorded_at"),
                "audio_quality": lead.get("audio_quality"),
                "demo_note": lead.get("demo_note"),
            },
            "crm_snapshot": {
                **lead["crm"],
                "_policy": "read-only in this app - QA never writes to the CRM",
            },
            "plan_snapshot": plan,
            "transcript": {k: v for k, v in ingest.items() if k != "turns"},
            "turns": turns,
            "audio_url": f"/api/audio/{lead_id}" if self.store.audio_for(lead_id) else None,
            "results": results,
            "gate": decision,
            "gate_history": [{
                "at": utcnow(),
                "status": decision["display_status"],
                "trigger": "initial_scoring",
                "by": "system",
            }],
            "override_log": [],
            "config": self.settings.snapshot(),
            "extractors": self.extractor_status(),
            "pipeline_trace": trace,
            "warnings": list(self.settings.warnings),
        }
        if record:
            self.store.save_run(run)
            self._enqueue(run)
        return run

    def run_lead(self, lead_id: str, source: str = "auto", record: bool = True) -> dict:
        return self.score(self.ingest(lead_id, source)["ingest_id"], record)

    def _enqueue(self, run: dict) -> None:
        """Write the routing decision to a queue file so HELD/QA are demonstrably queued."""
        queue = run["gate"].get("queue")
        if not queue:
            return
        line = {
            "at": utcnow(),
            "run_id": run["run_id"],
            "lead_id": run["lead_id"],
            "gate_status": run["gate"]["display_status"],
            **queue,
        }
        path = RUNS_DIR / "queue.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")

    # ---------------------------------------------------------------- override

    def override(self, run_id: str, check_id: str, to_status: str, reason: str, actor: str) -> dict:
        try:
            run = self.store.get_run(run_id)
        except KeyError as exc:
            raise PipelineError(_msg(exc)) from None

        to_status = (to_status or "").strip().lower()
        if to_status not in {"pass", "fail"}:
            raise PipelineError("override target must be 'pass' or 'fail'")
        reason = (reason or "").strip()
        if len(reason) < MIN_OVERRIDE_REASON:
            raise PipelineError(
                f"a written reason of at least {MIN_OVERRIDE_REASON} characters is required"
            )
        actor = (actor or "").strip() or "unattributed"

        result = next((r for r in run["results"] if r["check_id"] == check_id), None)
        if result is None:
            raise PipelineError(f"run {run_id} has no check {check_id!r}")
        if result["type"] == "C":
            raise PipelineError(
                f"{result['name']} is a Type C coaching note. It never affects the gate, "
                "so there is nothing to override."
            )
        if result["status"] == to_status and result.get("overridden"):
            raise PipelineError(f"{result['name']} is already overridden to {to_status}")

        gate_before = run["gate"]["display_status"]
        from_status = result["status"]

        result["status"] = to_status
        result["overridden"] = True
        result["outcome_label"] = to_status.upper()
        result["confidence"] = 1.0
        result["confidence_source"] = "human_override"
        result["blocks_sale"] = bool(result["critical"] and to_status == "fail")
        result["override"] = {
            "from_status": from_status,
            "to_status": to_status,
            "machine_status": result["machine_status"],
            "machine_confidence": result["machine_confidence"],
            "reason": reason,
            "actor": actor,
            "at": utcnow(),
        }

        decision = gate.evaluate(run["results"], run["lead_id"], self.settings)
        run["gate"] = decision
        entry = {
            "at": utcnow(),
            "run_id": run_id,
            "lead_id": run["lead_id"],
            "check_id": check_id,
            "check_name": result["name"],
            "check_version": result["check_version"],
            "from_status": from_status,
            "to_status": to_status,
            "machine_status": result["machine_status"],
            "machine_confidence": result["machine_confidence"],
            "reason": reason,
            "actor": actor,
            "gate_before": gate_before,
            "gate_after": decision["display_status"],
            "crm_written": False,
        }
        run["override_log"].append(entry)
        run["gate_history"].append({
            "at": entry["at"],
            "status": decision["display_status"],
            "trigger": f"override:{check_id} {from_status}->{to_status}",
            "by": actor,
        })
        self.store.append_override(entry)
        self.store.save_run(run)
        if gate_before != decision["display_status"]:
            self._enqueue(run)
        return run
