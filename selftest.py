"""Behavioural self-test. `python run.py --selftest`

These are the claims the app makes about itself, asserted rather than described.
Every one of them maps to something a reviewer would otherwise have to take on
trust: the worked example holds, the messy call is never failed on silence, a
coaching note cannot block a sale, and an override cannot happen without a reason.

The self-test forces the deterministic extractor so it is reproducible offline
and costs nothing to run.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from app import gate, llm
from app.config import load_settings
from app.pipeline import Pipeline, PipelineError
from app.scoring import score_type_b
from app.store import Store
from app.textutil import redact_long_digits

_RESULTS: list[tuple[bool, str, str]] = []


class _NoConnectionError(Exception):
    """Stands in for anthropic.APIConnectionError in the stubbed transport."""


def _stub_extractor(settings, payload: dict):
    """An Extractor wired to a fake transport: beta call refused, plain call answers."""
    response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        stop_reason="end_turn",
        model=settings.model,
        usage=SimpleNamespace(input_tokens=1180, output_tokens=94),
    )

    def refuse_beta(**_kwargs):
        raise TypeError("unexpected keyword argument 'fallbacks'")

    client = SimpleNamespace(
        beta=SimpleNamespace(messages=SimpleNamespace(create=refuse_beta)),
        messages=SimpleNamespace(create=lambda **_kwargs: response),
    )
    extractor = llm.Extractor.__new__(llm.Extractor)
    extractor.settings = settings
    extractor.client = client
    extractor.unavailable_reason = None
    extractor.name = f"llm:{settings.model}"
    extractor._anthropic = SimpleNamespace(APIConnectionError=_NoConnectionError)
    return extractor


def check(label: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((bool(ok), label, detail))
    return bool(ok)


def by_id(run: dict, check_id: str) -> dict:
    return next(r for r in run["results"] if r["check_id"] == check_id)


def run_selftest(settings=None) -> int:
    settings = settings or load_settings()
    settings = copy.copy(settings)
    settings.llm_mode = "off"  # deterministic and free to run
    store = Store()
    pipeline = Pipeline(store, settings)

    # ---------------------------------------------------- 1. the worked example
    held = pipeline.run_lead("3613790")
    held_machine_results = copy.deepcopy(held["results"])  # before any override
    check("3613790 gate is HELD", held["gate"]["status"] == gate.HELD, held["gate"]["display_status"])
    check("3613790 is queued to a team leader",
          (held["gate"].get("queue") or {}).get("queue") == "TEAM_LEADER_REVIEW",
          str((held["gate"].get("queue") or {}).get("queue")))
    check("3613790 cannot be submitted", held["gate"]["can_submit_to_crm"] is False)

    rate = by_id(held, "rates_and_charges")
    check("rate check fails", rate["status"] == "fail", f"{rate['status']} @ {rate['timestamp']}")
    check("rate failure is at 14:02", rate["timestamp"] == "14:02", rate["timestamp"])
    check("rate evidence is 28.6 spoken vs 31.9 on the plan",
          rate["evidence"]["comparison"]["observed_normalised"] == "28.6"
          and rate["evidence"]["comparison"]["expected_normalised"] == "31.9",
          rate["evidence"]["comparison"]["detail"])
    check("rate failure blocks the sale", rate["blocks_sale"] is True)

    email = by_id(held, "email_captured")
    check("email check fails", email["status"] == "fail", f"{email['status']} @ {email['timestamp']}")
    check("email failure is at 22:10", email["timestamp"] == "22:10", email["timestamp"])
    check("email evidence is gmail spoken vs gmial in CRM",
          email["evidence"]["comparison"]["observed_normalised"] == "j.smith@gmail.com"
          and email["evidence"]["comparison"]["expected_normalised"] == "j.smith@gmial.com",
          email["evidence"]["comparison"]["detail"])

    dead_air = by_id(held, "dead_air")
    check("dead air is noted at 18:30", dead_air["timestamp"] == "18:30", dead_air["timestamp"])
    check("dead air never blocks", dead_air["blocks_sale"] is False)
    check("dead air is excluded from the gate",
          "dead_air" in held["gate"]["policy"]["type_c_excluded_from_gate"])

    for check_id in ("recording_disclaimer", "account_holder", "dmo_verbatim"):
        row = by_id(held, check_id)
        check(f"3613790 {check_id} passes", row["status"] == "pass",
              f"{row['status']} coverage={row['evidence'].get('coverage')}")

    # ------------------------------------------------------- 2. the clean lead
    clean = pipeline.run_lead("3613791")
    check("3613791 gate is SUBMITTED", clean["gate"]["status"] == gate.SUBMITTED,
          clean["gate"]["display_status"])
    check("3613791 has no critical failures", clean["gate"]["counters"]["critical_fail"] == 0)
    check("3613791 has no unsure criticals", clean["gate"]["counters"]["critical_unsure"] == 0)
    check("3613791 rate 29.7 matches its own plan",
          by_id(clean, "rates_and_charges")["status"] == "pass",
          by_id(clean, "rates_and_charges")["evidence"]["comparison"]["detail"])

    # ------------------------------------------- 3. the messy call (guardrails)
    messy = pipeline.run_lead("3613792")
    criticals = [r for r in messy["results"] if r["critical"]]
    check("3613792 invents no critical failure from bad audio",
          not any(r["status"] == "fail" for r in criticals),
          ", ".join(f"{r['check_id']}={r['status']}" for r in criticals))
    check("3613792 gate is QA", messy["gate"]["status"] == gate.QA, messy["gate"]["display_status"])
    check("3613792 is never SUBMITTED", messy["gate"]["can_submit_to_crm"] is False)
    check("3613792 is never HELD", messy["gate"]["status"] != gate.HELD)

    dmo = by_id(messy, "dmo_verbatim")
    check("garbled DMO is unsure, not failed", dmo["status"] == "unsure",
          f"{dmo['status']} coverage={dmo['evidence']['coverage']}")
    check("the DMO downgrade is recorded as a guardrail",
          any(g.startswith("G1") for g in dmo["guardrails"]),
          "; ".join(dmo["guardrails"])[:140])
    check("coverage alone would have failed the DMO check",
          dmo["evidence"]["coverage"] < dmo["evidence"]["unsure_threshold"],
          f"coverage={dmo['evidence']['coverage']}")

    messy_rate = by_id(messy, "rates_and_charges")
    check("crosstalk on the rate is unsure, not failed", messy_rate["status"] == "unsure",
          messy_rate["evidence"]["extraction"]["reason"])
    check("the extractor abstained rather than guessing 'thirty'",
          messy_rate["evidence"]["extraction"]["found"] is False,
          str(messy_rate["evidence"]["extraction"]["value"]))
    messy_email = by_id(messy, "email_captured")
    check("obscured email domain is unsure, not failed", messy_email["status"] == "unsure",
          messy_email["evidence"]["extraction"]["reason"])
    check("3613792 still passes the checks that were audible",
          by_id(messy, "recording_disclaimer")["status"] == "pass"
          and by_id(messy, "account_holder")["status"] == "pass"
          and by_id(messy, "fuel_type")["status"] == "pass")

    # ------------------------------------------------------------ 4. redaction
    card_turn = next(t for t in held["turns"] if t["redacted"])
    check("13+ digit runs are redacted at ingest", "4511" not in card_turn["text"], card_turn["text"][:70])
    check("no result quote leaks card digits",
          not any("4511" in (r.get("quote") or "") for r in held["results"]))
    check("the whole run artifact is free of the card number",
          "4511 2233" not in str(held))
    masked, count = redact_long_digits("nmi 6 1 0 2 0 3 4 5 6 7 8 card 4511 2233 4455 6677")
    check("an 11 digit NMI survives redaction", "6 1 0 2 0 3 4 5 6 7 8" in masked, masked)
    check("a 16 digit card does not", count == 1 and "4455" not in masked, masked)

    # ----------------------------------------------------------- 5. leak guard
    rate_check = next(c for c in store.checks if c["check_id"] == "rates_and_charges")
    ingest = store.ingest("3613790")
    prompt = llm.build_prompt(rate_check, ingest["turns"])
    secrets = store.secret_reference_values("3613790")
    check("the prompt never contains the CRM email", "gmial" not in prompt.lower())
    check("the prompt never contains the plan rate", "31.9" not in prompt)
    try:
        llm.assert_no_reference_leak(prompt, secrets, llm.render_transcript(ingest["turns"]))
        check("leak guard passes a clean prompt", True)
    except llm.LeakGuardError as exc:
        check("leak guard passes a clean prompt", False, str(exc))
    try:
        llm.assert_no_reference_leak(prompt + "\nCRM says 31.9", secrets,
                                     llm.render_transcript(ingest["turns"]))
        check("leak guard blocks an injected CRM value", False, "no exception raised")
    except llm.LeakGuardError:
        check("leak guard blocks an injected CRM value", True)

    # ------------------------------------------- 6. Type C can never block
    forced = copy.deepcopy(held)
    for row in forced["results"]:
        if row["type"] == "C":
            row["status"] = "fail"
            row["blocks_sale"] = True  # even if something downstream tried
    decision = gate.evaluate(
        [r for r in forced["results"] if r["type"] == "C"]
        + [r for r in forced["results"] if r["type"] != "C" and r["status"] == "pass"],
        "3613791", settings,
    )
    check("failing every coaching note still submits", decision["status"] == gate.SUBMITTED,
          decision["display_status"])

    # ------------------------------------------------------------ 7. overrides
    try:
        pipeline.override(held["run_id"], "rates_and_charges", "pass", "ok", "TL Sam")
        check("an override without a real reason is rejected", False, "it was accepted")
    except PipelineError as exc:
        check("an override without a real reason is rejected", True, str(exc)[:70])
    try:
        pipeline.override(held["run_id"], "dead_air", "fail", "coaching note should block", "TL Sam")
        check("a Type C check cannot be overridden into blocking", False, "it was accepted")
    except PipelineError as exc:
        check("a Type C check cannot be overridden into blocking", True, str(exc)[:70])

    after = pipeline.override(
        held["run_id"], "rates_and_charges", "pass",
        "Listened back at 14:02 - agent misread the rate then corrected it off-recording; "
        "plan rate confirmed with the customer in writing.",
        "TL Sam Okafor",
    )
    entry = after["override_log"][-1]
    check("an override with a reason is accepted", len(after["override_log"]) == 1)
    check("the override records who, why, and both statuses",
          bool(entry["actor"] and entry["reason"] and entry["from_status"] == "fail"
               and entry["to_status"] == "pass"))
    check("the machine verdict is preserved alongside the override",
          by_id(after, "rates_and_charges")["machine_status"] == "fail")
    check("one override does not release a second critical failure",
          after["gate"]["status"] == gate.HELD, after["gate"]["display_status"])
    check("the override never writes to the CRM", entry["crm_written"] is False)
    check("the CRM email is untouched after the override",
          after["crm_snapshot"]["email"] == "j.smith@gmial.com")

    released = pipeline.override(
        held["run_id"], "email_captured", "pass",
        "Customer called back and confirmed j.smith@gmail.com; CRM correction raised "
        "with data ops under ticket DQ-4471.",
        "TL Sam Okafor",
    )
    last = released["override_log"][-1]
    check("clearing the last critical failure re-runs the gate",
          last["gate_before"] == gate.HELD and last["gate_after"].startswith(gate.SUBMITTED),
          f"{last['gate_before']} -> {last['gate_after']}")
    check("the gate history records every transition",
          len(released["gate_history"]) == 3,
          " | ".join(h["trigger"] for h in released["gate_history"]))
    check("the override log is append-only on disk",
          len([e for e in store.read_override_log() if e["run_id"] == held["run_id"]]) == 2)

    # -------------------------------------------------------- 8. sample audit
    always = copy.copy(settings)
    always.sample_audit_rate = 1.0
    never = copy.copy(settings)
    never.sample_audit_rate = 0.0
    passing = [r for r in clean["results"]]
    check("at a 100% rate a SUBMITTED run is sampled",
          gate.evaluate(passing, "3613791", always)["sample_audit"] is True)
    check("at a 0% rate it is not",
          gate.evaluate(passing, "3613791", never)["sample_audit"] is False)
    check("sampling is deterministic for a given lead",
          gate.sample_audit_bucket("3613791", settings.sample_audit_salt)
          == gate.sample_audit_bucket("3613791", settings.sample_audit_salt))
    check("a HELD run is never sampled instead of held",
          gate.evaluate(held_machine_results, "3613790", always)["status"] == gate.HELD)

    # --------------------------------------------------------- 9. traceability
    for run in (held, clean, messy):
        missing = [r["check_id"] for r in run["results"]
                   if r["start_sec"] is None or not (r.get("quote") or r.get("note"))]
        check(f"{run['lead_id']} anchors every check to a moment in the call",
              not missing, f"unanchored: {missing}")
        check(f"{run['lead_id']} records the checklist version on every result",
              all(r["check_version"] for r in run["results"]))

    # ------------------------------------- 10. the LLM path, without a network
    # Exercises Extractor.extract end to end against a stubbed transport, so the
    # branch that only runs with an API key is still covered by this test.
    turns_790 = ingest["turns"]
    good = {"found": True, "value": "28.6", "start_sec": 842, "spoken_by": "agent",
            "confidence": 0.96, "reason": "stated once, clearly",
            "quote": next(t["text"] for t in turns_790 if t["start_sec"] == 842)}
    extractor = _stub_extractor(settings, good)
    data = extractor.extract(rate_check, turns_790, secrets)
    check("the LLM response parses into the extraction contract",
          data["value"] == "28.6" and data["found"] is True)
    check("a rejected beta call falls through to a plain request",
          data["_meta"]["api_path"] == "messages+output_config", data["_meta"]["api_path"])
    check("the run records the model and prompt hash for audit",
          bool(data["_meta"]["model"]) and len(data["_meta"]["prompt_sha256"]) == 64)

    llm_scored = score_type_b(rate_check, turns_790, data, 31.9, "Plan peak rate",
                              "plan", floor := settings.confidence_floor_critical)
    check("an LLM extraction scores the same verdict as the offline one",
          llm_scored["status"] == "fail" and llm_scored["timestamp"] == "14:02",
          f"{llm_scored['status']} @ {llm_scored['timestamp']}")

    invented = dict(good, value="31.9",
                    quote="Our supervisor authorised a discretionary rate of 31.9 cents "
                          "per kilowatt hour for this household.")
    hallucinated = score_type_b(rate_check, turns_790,
                                _stub_extractor(settings, invented).extract(rate_check, turns_790, secrets),
                                31.9, "Plan peak rate", "plan", floor)
    check("a quote that is not in the transcript cannot produce a verdict",
          hallucinated["status"] == "unsure"
          and any(g.startswith("G3") for g in hallucinated["guardrails"]),
          f"{hallucinated['status']}: {'; '.join(hallucinated['guardrails'])[:90]}")
    check("the discarded value never reaches the comparison",
          hallucinated["evidence"]["comparison"] is None)

    # ------------------------------------------------------------------ report
    width = max(len(label) for _, label, _ in _RESULTS) + 2
    print()
    print("  CIMET QA Gate - self test")
    print("  " + "-" * (width + 12))
    failures = 0
    for ok, label, detail in _RESULTS:
        mark = "pass" if ok else "FAIL"
        if not ok:
            failures += 1
        suffix = f"  {detail}" if (detail and not ok) else ""
        print(f"  [{mark}] {label:<{width}}{suffix}")
    print("  " + "-" * (width + 12))
    print(f"  {len(_RESULTS) - failures}/{len(_RESULTS)} assertions passed")
    print()
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run_selftest())
