"""The gate. One function, four outcomes, no hidden paths to SUBMITTED.

    any critical fail                              -> HELD      (queued to a TL)
    any critical unsure, or critical confidence <floor -> QA     (never SUBMITTED)
    otherwise                                      -> SUBMITTED (5% sampled for audit)

Type C results are removed before any of that is evaluated, so a coaching note
cannot influence the outcome even if someone marks it failed.
"""

from __future__ import annotations

import hashlib

HELD, QA, SUBMITTED = "HELD", "QA", "SUBMITTED"


def sample_audit_bucket(lead_id: str, salt: str) -> float:
    """Stable 0-1 bucket for a lead. Deterministic so a demo replays identically."""
    digest = hashlib.sha256(f"{salt}:{lead_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def evaluate(results: list[dict], lead_id: str, settings) -> dict:
    floor = settings.confidence_floor_critical
    considered = [r for r in results if r["type"] != "C"]
    excluded = [r["check_id"] for r in results if r["type"] == "C"]
    critical = [r for r in considered if r["critical"]]

    crit_fail = [r for r in critical if r["status"] == "fail"]
    crit_unsure = [r for r in critical if r["status"] == "unsure"]
    crit_lowconf = [r for r in critical if r["status"] != "unsure" and r["confidence"] < floor]

    reasons: list[dict] = []
    if crit_fail:
        status = HELD
        for r in crit_fail:
            reasons.append({
                "rule": "critical_fail",
                "check_id": r["check_id"],
                "check_version": r["check_version"],
                "detail": f"{r['name']} failed at {r['timestamp']} (confidence {r['confidence']:.2f})",
                "timestamp": r["timestamp"],
                "start_sec": r["start_sec"],
            })
    elif crit_unsure or crit_lowconf:
        status = QA
        for r in crit_unsure:
            reasons.append({
                "rule": "critical_unsure",
                "check_id": r["check_id"],
                "check_version": r["check_version"],
                "detail": f"{r['name']} could not be resolved from the recording",
                "timestamp": r["timestamp"],
                "start_sec": r["start_sec"],
            })
        for r in crit_lowconf:
            reasons.append({
                "rule": "critical_low_confidence",
                "check_id": r["check_id"],
                "check_version": r["check_version"],
                "detail": f"{r['name']} scored {r['status']} at confidence "
                          f"{r['confidence']:.2f}, below the critical floor of {floor:.2f}",
                "timestamp": r["timestamp"],
                "start_sec": r["start_sec"],
            })
    else:
        status = SUBMITTED
        reasons.append({
            "rule": "all_critical_clear",
            "check_id": None,
            "detail": f"all {len(critical)} critical checks passed at or above the "
                      f"{floor:.2f} confidence floor",
            "timestamp": None,
            "start_sec": None,
        })

    bucket = sample_audit_bucket(lead_id, settings.sample_audit_salt)
    sample_audit = status == SUBMITTED and bucket < settings.sample_audit_rate

    weighted_total = sum(r["weight"] for r in considered) or 1
    weighted_earned = sum(
        r["weight"] * (1.0 if r["status"] == "pass" else 0.5 if r["status"] == "unsure" else 0.0)
        for r in considered
    )
    evidenced = [r for r in results if r.get("quote") and r.get("start_sec") is not None]

    queue = None
    if status == HELD:
        queue = {
            "queue": "TEAM_LEADER_REVIEW",
            "priority": "high",
            "sla_minutes": 60,
            "reason": "; ".join(r["detail"] for r in reasons),
            "blocking_check_ids": [r["check_id"] for r in crit_fail],
        }
    elif status == QA:
        queue = {
            "queue": "QA_REVIEW",
            "priority": "normal",
            "sla_minutes": 240,
            "reason": "; ".join(r["detail"] for r in reasons),
            "blocking_check_ids": [],
        }
    elif sample_audit:
        queue = {
            "queue": "SAMPLE_AUDIT",
            "priority": "low",
            "sla_minutes": 2880,
            "reason": f"random {settings.sample_audit_rate:.0%} post-submit audit sample "
                      f"(bucket {bucket:.4f})",
            "blocking_check_ids": [],
        }

    return {
        "status": status,
        "sample_audit": sample_audit,
        "display_status": f"{status} + SAMPLE AUDIT" if sample_audit else status,
        "can_submit_to_crm": status == SUBMITTED,
        "queue": queue,
        "reasons": reasons,
        "policy": {
            "order": [
                "1. any critical check with status=fail -> HELD, queued to a team leader",
                "2. any critical check with status=unsure, or confidence below the floor -> QA",
                "3. otherwise -> SUBMITTED",
                f"4. {settings.sample_audit_rate:.0%} of SUBMITTED are flagged SAMPLE_AUDIT",
            ],
            "confidence_floor_critical": floor,
            "type_c_excluded_from_gate": excluded,
            "qa_can_never_become_submitted": True,
        },
        "counters": {
            "checks_total": len(results),
            "checks_gating": len(considered),
            "critical_total": len(critical),
            "critical_pass": len([r for r in critical if r["status"] == "pass"]),
            "critical_fail": len(crit_fail),
            "critical_unsure": len(crit_unsure),
            "critical_low_confidence": len(crit_lowconf),
            "non_critical_fail": len([r for r in considered if not r["critical"] and r["status"] == "fail"]),
            "coaching_notes": len(excluded),
            "weighted_score": round(100.0 * weighted_earned / weighted_total, 1),
            "evidence_coverage_pct": round(100.0 * len(evidenced) / max(1, len(results)), 1),
        },
        "sample_audit_bucket": round(bucket, 6),
        "sample_audit_rate": settings.sample_audit_rate,
    }
