"""Scoring accuracy against hand-labelled calls. `python run.py --eval`

The judging brief weights "agreement with human auditors" at 30% and says critical
checks must essentially never false-pass. This measures exactly that, on the
synthetic calls in data/synth/, whose labels were written by hand in
tools/synth_calls.py and are never read by the scorer.

How machine outcomes are counted against a pass/fail label:

  pass  vs pass / fail vs fail   agreement
  pass  vs label fail            FALSE PASS - the one that must be zero on criticals
  fail  vs label pass            false fail - on a critical this would wrongly hold a sale
  unsure                         abstained: routed to a human. Not agreement, not an error.

Gate outcomes: label HELD or SUBMITTED against machine HELD / QA / SUBMITTED. A
labelled-HELD sale that the machine SUBMITTED is a false submit; that is the
number the whole gate exists to keep at zero.
"""

from __future__ import annotations

import json

from .config import DATA_DIR, RUNS_DIR
from .store import utcnow

TRUTH_PATH = DATA_DIR / "synth" / "truth.json"


def load_truth() -> dict:
    if not TRUTH_PATH.exists():
        raise FileNotFoundError("no ground truth yet - run: python tools/synth_calls.py")
    return json.loads(TRUTH_PATH.read_text(encoding="utf-8"))["leads"]


def _kappa(pairs: list[tuple[str, str]]) -> float | None:
    """Cohen's kappa for two raters on pass/fail. None when it is undefined."""
    n = len(pairs)
    if not n:
        return None
    observed = sum(a == b for a, b in pairs) / n
    p_machine = sum(a == "pass" for a, _ in pairs) / n
    p_label = sum(b == "pass" for _, b in pairs) / n
    expected = p_machine * p_label + (1 - p_machine) * (1 - p_label)
    if expected >= 1.0:
        return 1.0 if observed == 1.0 else None
    return round((observed - expected) / (1 - expected), 3)


def evaluate(pipeline, source: str = "fixture") -> dict:
    """Score every labelled lead and compare. source='asr' uses dialler/ASR transcripts only."""
    truth = load_truth()
    rows, per_check, gate_rows, skipped = [], {}, [], []

    for lead_id, label in truth.items():
        if source == "asr" and not pipeline.store.asr_transcript_path(lead_id).exists():
            skipped.append(lead_id)
            continue
        run = pipeline.run_lead(lead_id, source=source, record=False)
        results = {r["check_id"]: r for r in run["results"]}
        for check_id, expected in label["checks"].items():
            r = results.get(check_id)
            if r is None:
                continue
            got = r["status"]
            if got == expected:
                outcome = "agree"
            elif got == "unsure":
                outcome = "abstain"
            elif got == "pass":
                outcome = "FALSE_PASS"
            else:
                outcome = "false_fail"
            rows.append({"lead_id": lead_id, "check_id": check_id, "critical": r["critical"],
                         "label": expected, "machine": got, "confidence": r["confidence"],
                         "timestamp": r["timestamp"], "outcome": outcome})
            bucket = per_check.setdefault(check_id, {"check_id": check_id, "critical": r["critical"],
                                                     "n": 0, "agree": 0, "abstain": 0,
                                                     "false_pass": 0, "false_fail": 0, "pairs": []})
            bucket["n"] += 1
            bucket["agree"] += outcome == "agree"
            bucket["abstain"] += outcome == "abstain"
            bucket["false_pass"] += outcome == "FALSE_PASS"
            bucket["false_fail"] += outcome == "false_fail"
            if got != "unsure":
                bucket["pairs"].append((got, expected))

        machine_gate = run["gate"]["status"]
        expected_gate = label["gate"]
        if machine_gate == expected_gate:
            g = "agree"
        elif machine_gate == "QA":
            g = "escalated"
        elif machine_gate == "SUBMITTED":
            g = "FALSE_SUBMIT"
        else:
            g = "false_hold"
        gate_rows.append({"lead_id": lead_id, "title": label["title"], "label": expected_gate,
                          "machine": machine_gate, "outcome": g,
                          "transcript_source": run["transcript"]["source"]})

    for bucket in per_check.values():
        decided = bucket["n"] - bucket["abstain"]
        bucket["agreement_pct"] = round(100 * bucket["agree"] / decided, 1) if decided else None
        bucket["kappa"] = _kappa(bucket.pop("pairs"))

    all_pairs = [(r["machine"], r["label"]) for r in rows if r["machine"] != "unsure"]
    crit = [r for r in rows if r["critical"]]
    decided = [r for r in rows if r["machine"] != "unsure"]
    summary = {
        "calls": len(gate_rows),
        "check_verdicts": len(rows),
        "agreement_pct_decided": round(100 * sum(r["outcome"] == "agree" for r in decided) / len(decided), 1)
        if decided else None,
        "kappa": _kappa(all_pairs),
        "abstain_pct": round(100 * sum(r["outcome"] == "abstain" for r in rows) / len(rows), 1) if rows else None,
        "critical_false_pass": sum(r["outcome"] == "FALSE_PASS" for r in crit),
        "critical_false_fail": sum(r["outcome"] == "false_fail" for r in crit),
        "gate_agree": sum(g["outcome"] == "agree" for g in gate_rows),
        "gate_escalated_to_human": sum(g["outcome"] == "escalated" for g in gate_rows),
        "gate_false_submit": sum(g["outcome"] == "FALSE_SUBMIT" for g in gate_rows),
        "gate_false_hold": sum(g["outcome"] == "false_hold" for g in gate_rows),
    }
    report = {
        "generated_at": utcnow(),
        "transcript_source": source,
        "extractor": pipeline.extractor_status()["type_b_active"],
        "checklist_version": pipeline.store.checks_doc["checklist_version"],
        "summary": summary,
        "per_check": sorted(per_check.values(), key=lambda b: (not b["critical"], b["check_id"])),
        "gates": gate_rows,
        "disagreements": [r for r in rows if r["outcome"] != "agree"],
        "skipped_no_asr_transcript": skipped,
    }
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    (RUNS_DIR / f"eval_report_{source}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def print_report(report: dict) -> None:
    s = report["summary"]
    print()
    print(f"  Scoring accuracy - {s['calls']} labelled calls, {s['check_verdicts']} check verdicts")
    print(f"  transcripts: {report['transcript_source']}  |  Type B: {report['extractor']}"
          f"  |  checklist {report['checklist_version']}")
    print()
    print(f"  {'CHECK':<22} {'CRIT':<5} {'N':>3} {'AGREE':>6} {'UNSURE':>7} {'F-PASS':>7} {'F-FAIL':>7} {'AGREE%':>7} {'KAPPA':>6}")
    for b in report["per_check"]:
        pct = "-" if b["agreement_pct"] is None else f"{b['agreement_pct']:.0f}%"
        kappa = "-" if b["kappa"] is None else f"{b['kappa']:.2f}"
        print(f"  {b['check_id']:<22} {('yes' if b['critical'] else '-'):<5} {b['n']:>3} {b['agree']:>6} "
              f"{b['abstain']:>7} {b['false_pass']:>7} {b['false_fail']:>7} {pct:>7} {kappa:>6}")
    print()
    print(f"  {'LEAD':<9} {'LABEL':<10} {'MACHINE':<10} {'OUTCOME':<13} SCENARIO")
    for g in report["gates"]:
        print(f"  {g['lead_id']:<9} {g['label']:<10} {g['machine']:<10} {g['outcome']:<13} {g['title']}")
    print()
    print(f"  agreement on decided verdicts {s['agreement_pct_decided']}%  |  kappa {s['kappa']}  |  "
          f"routed to a human {s['abstain_pct']}%")
    print(f"  CRITICAL FALSE PASSES: {s['critical_false_pass']}   critical false fails: {s['critical_false_fail']}")
    print(f"  gate: {s['gate_agree']} agree, {s['gate_escalated_to_human']} escalated to QA, "
          f"{s['gate_false_submit']} FALSE SUBMIT, {s['gate_false_hold']} false hold")
    if report["disagreements"]:
        print()
        print("  not in agreement:")
        for r in report["disagreements"]:
            print(f"    {r['lead_id']} {r['check_id']:<22} label {r['label']:<5} machine {r['machine']:<7} "
                  f"conf {r['confidence']:.2f} at {r['timestamp']}  [{r['outcome']}]")
    if report["skipped_no_asr_transcript"]:
        print(f"\n  skipped (no ASR transcript yet): {', '.join(report['skipped_no_asr_transcript'])}")
    print(f"\n  report: runs/eval_report_{report['transcript_source']}.json\n")
