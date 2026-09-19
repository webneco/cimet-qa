"""Per-check scoring. One function per check type, one result shape for all three.

Every result carries the evidence that produced it: the quote, the timestamp, the
check version that was in force, and - for Type B - the extractor, the reference
value, and the comparison. A result you cannot trace back to a moment in the call
is a result this pipeline will not emit.
"""

from __future__ import annotations

import re

from .compare import compare
from .store import utcnow
from .textutil import MARKER_RE, content_tokens, mmss, phrase_ratio, strip_markers, tokens

STATUS_PASS, STATUS_FAIL, STATUS_UNSURE = "pass", "fail", "unsure"

# Below this ASR confidence we treat the words as unheard rather than unsaid.
AUDIO_TRUST_FLOOR = 0.75

# G8: when the agent's words and the order screen disagree, the call alone cannot say
# which the provider will bill - the screen is only known through the agent's own
# description of it. The verdict is FAIL at this share of normal confidence, and
# the critical floor then decides whether it holds the sale or goes to QA.
CONFLICT_FACTOR = 0.7

CONFIDENCE_FORMULA = {
    "A": "pass: min(0.98, 0.55 + 0.45*coverage) * mean_asr | fail: min(0.95, 0.55 + 0.45*(1-coverage)) * mean_asr | unsure: capped below the critical floor by construction",
    "B": "pass/fail: min(0.99, extraction_confidence * asr_of_quoted_turn) | G8 screen-vs-spoken conflict: fail at 0.7x that | unsure: capped below the critical floor by construction",
    "C": "deterministic measurement over turn timings",
}


def _base(check: dict) -> dict:
    return {
        "check_id": check["check_id"],
        "check_version": check["check_version"],
        "effective_from": check["effective_from"],
        "name": check["name"],
        "section": check["section"],
        "type": check["type"],
        "critical": bool(check["critical"]),
        "weight": check.get("weight", 0),
        "regulatory_basis": check.get("regulatory_basis"),
        "rule_summary": check["script_or_rule"].get("description", ""),
        "guardrails": [],
        "overridden": False,
        "override": None,
        "scored_at": utcnow(),
    }


def _finalise(result: dict, check: dict, floor: float) -> dict:
    """Apply the cross-cutting guardrails, then freeze the machine verdict."""
    # G5: a critical FAIL we are not confident about must not hold a customer's
    # sale. Downgrade it to unsure so the gate routes it to QA, not to a TL hold.
    if (
        result["status"] == STATUS_FAIL
        and result["critical"]
        and result["confidence"] < floor
        and result["type"] != "C"
    ):
        result["guardrails"].append(
            f"G5 low-confidence critical fail: confidence {result['confidence']:.2f} is below the "
            f"critical floor {floor:.2f}, so the fail was downgraded to unsure rather than holding the sale"
        )
        result["status"] = STATUS_UNSURE
        result["confidence"] = min(result["confidence"], round(floor - 0.05, 2))

    # Type C can never block, by construction and not by configuration.
    if result["type"] == "C":
        result["blocks_sale"] = False
    else:
        result["blocks_sale"] = bool(result["critical"] and result["status"] == STATUS_FAIL)

    result["outcome_label"] = (
        "NOTE" if result["type"] == "C" else result["status"].upper()
    )
    result["timestamp"] = mmss(result.get("start_sec"))
    result["machine_status"] = result["status"]
    result["machine_confidence"] = result["confidence"]
    result["machine_blocks_sale"] = result["blocks_sale"]
    return result


# --------------------------------------------------------------------- Type A

def score_type_a(check: dict, turns: list[dict], floor: float) -> dict:
    rule = check["script_or_rule"]
    # A required phrase is a string, or a list of acceptable wordings of the same thing.
    required = [p if isinstance(p, list) else [p] for p in rule["required_phrases"]]
    phrase_tokens = [[content_tokens(alt) for alt in alts] for alts in required]
    window_turns = int(rule.get("window_turns", 3))
    pass_threshold = float(rule.get("pass_threshold", 0.85))
    unsure_threshold = float(rule.get("unsure_threshold", 0.55))

    def glued(window: list[dict]) -> str:
        """The window with all spaces removed, so ASR run-togethers ('assuranceand')
        still match. Audio markers and turn boundaries become '|', so words either
        side of an [inaudible] can never be glued into a match."""
        parts = []
        for turn in window:
            parts += ["".join(tokens(seg)) for seg in MARKER_RE.split(turn["text"])[::2]]
        return "|".join(parts)

    def ratio_of(alts: list[list[str]], window_tokens: list[str], joined: str) -> tuple[float, int, bool]:
        best = (0.0, 0, False)
        for i, alt in enumerate(alts):
            r = phrase_ratio(alt, window_tokens)
            glue = False
            if r < 1.0 and alt and "".join(alt) in joined:
                r, glue = 1.0, True
            if r > best[0] + 1e-9:
                best = (r, i, glue)
        return best

    def measure(window: list[dict]) -> tuple[float, list[float]]:
        window_tokens: list[str] = []
        for turn in window:
            window_tokens.extend(content_tokens(turn["text"]))
        joined = glued(window)
        ratios = [ratio_of(alts, window_tokens, joined)[0] for alts in phrase_tokens]
        return (sum(ratios) / len(ratios) if ratios else 0.0), ratios

    best = None
    total = len(turns)
    for start in range(total):
        for span in range(1, window_turns + 1):
            if start + span > total:
                break
            coverage, ratios = measure(turns[start:start + span])
            if best is None or coverage > best["coverage"] + 1e-9:
                best = {"coverage": coverage, "ratios": ratios,
                        "window": turns[start:start + span]}

    if best is not None:
        # Tighten to the smallest span that still carries the wording, so the
        # timestamp a reviewer clicks is the moment it was said - not the start
        # of a three-turn window that happens to contain it.
        window, target = best["window"], best["coverage"]
        trimmed = True
        while trimmed and len(window) > 1:
            trimmed = False
            if measure(window[1:])[0] >= target - 1e-9:
                window, trimmed = window[1:], True
            elif measure(window[:-1])[0] >= target - 1e-9:
                window, trimmed = window[:-1], True
        best["coverage"], best["ratios"] = measure(window)
        best["window"] = window

    result = _base(check)
    if best is None:
        result.update(status=STATUS_UNSURE, confidence=0.3, quote="", start_sec=None,
                      turn_idx=None, speaker=None,
                      evidence={"method": "deterministic_script_span", "error": "transcript has no turns"})
        return _finalise(result, check, floor)

    window = best["window"]
    coverage = best["coverage"]
    markers: list[str] = []
    for turn in window:
        markers.extend(strip_markers(turn["text"])[1])
    asr_values = [t.get("asr_confidence", 1.0) for t in window]
    mean_asr = sum(asr_values) / len(asr_values)
    min_asr = min(asr_values)

    if coverage >= pass_threshold:
        status = STATUS_PASS
        confidence = min(0.98, 0.55 + 0.45 * coverage) * mean_asr
    elif coverage >= unsure_threshold:
        status = STATUS_UNSURE
        confidence = min(0.55, 0.3 + 0.25 * coverage)
    else:
        status = STATUS_FAIL
        confidence = min(0.95, 0.55 + 0.45 * (1 - coverage)) * mean_asr

    guardrails: list[str] = []
    notes: list[str] = []
    # "At the open": the wording has to arrive within the first N agent turns. Counted
    # in turns, not seconds, so it holds for transcripts whose timings are estimated.
    agent_ordinal = None
    if rule.get("max_agent_turn"):
        agent_ordinal = 1 + sum(1 for t in turns if t.get("speaker") == "agent"
                                and (t["start_sec"], t["idx"]) < (window[0]["start_sec"], window[0]["idx"]))
        if status == STATUS_PASS and agent_ordinal > int(rule["max_agent_turn"]):
            status = STATUS_FAIL
            confidence = 0.9 * mean_asr
            notes.append(f"the wording is present but only in agent turn {agent_ordinal}; "
                         f"it must come within the first {rule['max_agent_turn']}")

    # G1: obstructed audio is not evidence of a missing script. Never fail on it.
    if status == STATUS_FAIL and (markers or min_asr < AUDIO_TRUST_FLOOR):
        reasons = []
        if markers:
            reasons.append(f"{len(markers)} audio marker(s) ({', '.join(sorted(set(markers)))}) in the span")
        if min_asr < AUDIO_TRUST_FLOOR:
            reasons.append(f"ASR confidence as low as {min_asr:.2f}")
        guardrails.append(
            "G1 audio-quality abstain: coverage "
            f"{coverage:.0%} would normally fail, but {' and '.join(reasons)} mean the wording "
            "may have been spoken and not captured. Downgraded to unsure for human review."
        )
        status = STATUS_UNSURE
        confidence = min(0.5, 0.25 + 0.25 * coverage)

    window_tokens = [tok for t in window for tok in content_tokens(t["text"])]
    joined = glued(window)
    phrase_rows = []
    for alts_raw, alts in zip(required, phrase_tokens):
        ratio, which, glue = ratio_of(alts, window_tokens, joined)
        row = {"phrase": alts_raw[which], "ratio": round(ratio, 3), "found": ratio >= 0.9}
        if len(alts_raw) > 1:
            row["alternatives"] = alts_raw
        if glue:
            row["matched_run_together"] = True
        phrase_rows.append(row)

    result.update(
        status=status,
        confidence=round(confidence, 2),
        quote=" ".join(t["text"] for t in window).strip(),
        start_sec=window[0]["start_sec"],
        turn_idx=window[0]["idx"],
        speaker=window[0].get("speaker"),
        guardrails=guardrails,
        **({"note": "; ".join(notes)} if notes else {}),
        evidence={
            "method": "deterministic_script_span",
            "llm_used": False,
            "quote_role": "verbatim_match" if status == STATUS_PASS else "closest_matching_span",
            "coverage": round(coverage, 4),
            "pass_threshold": pass_threshold,
            "unsure_threshold": unsure_threshold,
            "phrases": phrase_rows,
            **({"agent_turn_ordinal": agent_ordinal, "max_agent_turn": rule["max_agent_turn"]}
               if rule.get("max_agent_turn") else {}),
            "window_turn_idx": [window[0]["idx"], window[-1]["idx"]],
            "audio_markers_in_span": sorted(set(markers)),
            "min_asr_confidence": round(min_asr, 2),
            "mean_asr_confidence": round(mean_asr, 2),
            "confidence_formula": CONFIDENCE_FORMULA["A"],
        },
    )
    return _finalise(result, check, floor)


def score_card_handling(check: dict, turns: list[dict], floor: float) -> dict:
    """Card data must never be on the recording. Deterministic, no LLM.

    PAN on the call (anything ingest had to mask) -> fail. Agent mentions muting or
    pausing the recording -> pass. Payment discussed with no mute mentioned -> unsure,
    because the call alone cannot show where the card number went.
    """
    rule = check["script_or_rule"]
    payment_re = re.compile(rule["payment_cues"], re.IGNORECASE)
    mute_re = re.compile(rule["mute_cues"], re.IGNORECASE)
    pan = [t for t in turns if t.get("redacted")]
    payment = [t for t in turns if payment_re.search(t["text"])]
    mute = [t for t in turns if t.get("speaker") == "agent" and mute_re.search(t["text"])]
    result = _base(check)

    if pan:
        anchor, status, confidence = pan[0], STATUS_FAIL, 0.95
        note = (f"a card-length number was spoken on the recording at {mmss(anchor['start_sec'])} "
                "and had to be masked at ingest")
    elif mute:
        anchor, status = mute[0], STATUS_PASS
        confidence = 0.92 * anchor.get("asr_confidence", 1.0)
        note = (f"recording muted for payment at {mmss(anchor['start_sec'])}; "
                "no card number anywhere in the transcript")
    elif payment:
        anchor, status, confidence = payment[0], STATUS_UNSURE, 0.4
        note = ("payment was discussed but the agent never said the recording was muted or paused - "
                "the call cannot show where the card number went")
    else:
        anchor, status, confidence = (turns[0] if turns else None), STATUS_PASS, 0.9
        note = "no payment discussed on this call"

    result.update(
        status=status, confidence=round(confidence, 2), note=note,
        quote=anchor["text"] if anchor else "",
        start_sec=anchor["start_sec"] if anchor else None,
        turn_idx=anchor["idx"] if anchor else None,
        speaker=anchor.get("speaker") if anchor else None,
        evidence={
            "method": "card_handling",
            "llm_used": False,
            "pan_turn_idx": [t["idx"] for t in pan],
            "payment_turn_idx": [t["idx"] for t in payment],
            "mute_turn_idx": [t["idx"] for t in mute],
            "rule": rule["description"],
        },
    )
    return _finalise(result, check, floor)


# --------------------------------------------------------------------- Type B

def _ground_quote(quote: str, turns: list[dict]) -> tuple[dict | None, float]:
    """Locate the transcript turn a quote came from. Guards against invention."""
    if not quote or not quote.strip():
        return None, 0.0
    needle = content_tokens(quote)
    if not needle:
        return None, 0.0
    best_turn, best_ratio = None, 0.0
    for turn in turns:
        ratio = phrase_ratio(needle, content_tokens(turn["text"]))
        if ratio > best_ratio:
            best_turn, best_ratio = turn, ratio
    return (best_turn, best_ratio) if best_ratio >= 0.8 else (None, best_ratio)


def score_type_b(
    check: dict,
    turns: list[dict],
    extraction: dict,
    reference_value,
    reference_label: str,
    reference_source: str,
    floor: float,
    extraction_error: str | None = None,
) -> dict:
    rule = check["script_or_rule"]
    result = _base(check)
    meta = (extraction or {}).get("_meta", {})
    guardrails: list[str] = []

    evidence = {
        "method": "llm_extraction_then_deterministic_comparison",
        "llm_used": bool(meta.get("model")),
        "field": rule["field"],
        "extractor": meta.get("extractor", "unavailable"),
        # Set when the LLM was configured but failed and the deterministic extractor
        # stood in - the reason is kept so a fallback is never invisible.
        "degraded_from": meta.get("degraded_from"),
        "degrade_reason": (meta.get("degrade_reason") or "")[:400] or None,
        "model": meta.get("model"),
        "effort": meta.get("effort"),
        "prompt_sha256": meta.get("prompt_sha256"),
        "refusal_fallback_enabled": meta.get("refusal_fallback_enabled"),
        "tokens": {"input": meta.get("input_tokens"), "output": meta.get("output_tokens")},
        "llm_saw_reference_value": False,
        "leak_guard": "prompt is built from the check definition and transcript only; asserted before send",
        "reference": {
            "source": reference_source,
            "path": (rule.get("reference") or {}).get("path"),
            "label": reference_label,
            "value": reference_value,
            "note": "resolved after extraction - the model never saw this",
        },
        "confidence_formula": CONFIDENCE_FORMULA["B"],
    }

    if extraction_error or not extraction:
        guardrails.append(
            f"G4 extraction unavailable: {extraction_error or 'no extraction produced'}. "
            "Reported as unsure - an unavailable extractor never produces a fail."
        )
        result.update(status=STATUS_UNSURE, confidence=0.3, quote="", start_sec=None,
                      turn_idx=None, speaker=None, guardrails=guardrails,
                      evidence={**evidence, "extraction": None, "comparison": None})
        return _finalise(result, check, floor)

    found = bool(extraction.get("found"))
    spoken_value = (extraction.get("value") or "").strip()
    raw_quote = (extraction.get("quote") or "").strip()
    extraction_conf = float(extraction.get("confidence") or 0.0)
    reason = (extraction.get("reason") or "").strip()
    evidence["extraction"] = {
        "found": found,
        "value": spoken_value,
        "other_mentions": extraction.get("other_mentions") or [],
        "confidence": round(extraction_conf, 2),
        "reason": reason,
        "model_reported_start_sec": extraction.get("start_sec"),
        "spoken_by": extraction.get("spoken_by"),
    }

    grounded_turn, ground_ratio = _ground_quote(raw_quote, turns)
    evidence["grounding"] = {
        "quote_grounded_in_transcript": grounded_turn is not None,
        "match_ratio": round(ground_ratio, 3),
        "matched_turn_idx": grounded_turn["idx"] if grounded_turn else None,
    }

    # G2: the model abstained. Silence, crosstalk and "never said" all land here,
    # and none of them are allowed to become a fail.
    if not found or not spoken_value:
        guardrails.append(
            "G2 abstention honoured: the extractor reported no reliable spoken value"
            + (f" ({reason})" if reason else "")
            + ". Recorded as unsure for human review - never as a fail."
        )
        result.update(
            status=STATUS_UNSURE,
            confidence=round(min(0.5, 0.3 + 0.2 * extraction_conf), 2),
            quote=raw_quote or (grounded_turn or {}).get("text", ""),
            start_sec=(grounded_turn or {}).get("start_sec", extraction.get("start_sec") if extraction.get("start_sec", -1) >= 0 else None),
            turn_idx=(grounded_turn or {}).get("idx"),
            speaker=(grounded_turn or {}).get("speaker"),
            guardrails=guardrails,
            evidence={**evidence, "comparison": None},
        )
        return _finalise(result, check, floor)

    # G3: a quote that is not in the transcript is a hallucination. Refuse to act on it.
    if grounded_turn is None:
        guardrails.append(
            f"G3 ungrounded quote: the extractor returned a quote that does not appear in the "
            f"transcript (best match {ground_ratio:.0%}). The value was discarded and the check "
            "reported as unsure."
        )
        result.update(status=STATUS_UNSURE, confidence=0.3, quote=raw_quote, start_sec=None,
                      turn_idx=None, speaker=None, guardrails=guardrails,
                      evidence={**evidence, "comparison": None})
        return _finalise(result, check, floor)

    model_start = extraction.get("start_sec")
    if model_start is not None and model_start >= 0 and abs(float(model_start) - grounded_turn["start_sec"]) > 1.0:
        guardrails.append(
            f"G6 timestamp corrected: extractor reported start_sec {float(model_start):g}, "
            f"the grounded turn starts at {grounded_turn['start_sec']:g}. The transcript wins."
        )

    comparison = compare(rule["comparator"], spoken_value, reference_value, float(rule.get("tolerance") or 0))
    evidence["comparison"] = comparison
    turn_asr = float(grounded_turn.get("asr_confidence", 1.0))

    if comparison["match"] is None:
        guardrails.append(
            f"G4 not comparable: {comparison['detail'].removeprefix('not comparable: ')}. Reported as unsure rather than guessing a verdict."
        )
        status, confidence = STATUS_UNSURE, min(0.5, 0.3 + 0.2 * extraction_conf)
    elif comparison["match"]:
        status, confidence = STATUS_PASS, min(0.99, extraction_conf * turn_asr)
    elif comparison.get("mishear_plausible"):
        # G7: the two values differ in a way a listener could not hear. Failing it
        # would hold a sale on ASR spelling; route it to a human with the clip instead.
        guardrails.append(
            f"G7 possible mishear: {comparison['mishear_reason']}. Reported as unsure so a person "
            "listens to the clip, rather than holding the sale on how ASR spelled it."
        )
        status, confidence = STATUS_UNSURE, min(0.5, 0.3 + 0.2 * extraction_conf)
    elif comparison.get("conflict"):
        base = min(0.99, extraction_conf * turn_asr)
        guardrails.append(
            f"G8 screen vs spoken conflict: {comparison['conflict_reason']}. The call alone cannot "
            f"show which the provider will bill, so this is a FAIL at {CONFLICT_FACTOR:.0%} of normal "
            f"confidence ({base:.2f} -> {base * CONFLICT_FACTOR:.2f}); the critical floor decides "
            "whether it holds the sale or routes it to QA."
        )
        status, confidence = STATUS_FAIL, base * CONFLICT_FACTOR
    else:
        status, confidence = STATUS_FAIL, min(0.99, extraction_conf * turn_asr)

    result.update(
        status=status,
        confidence=round(confidence, 2),
        quote=grounded_turn["text"],
        start_sec=grounded_turn["start_sec"],
        turn_idx=grounded_turn["idx"],
        speaker=grounded_turn.get("speaker"),
        guardrails=guardrails,
        evidence=evidence,
    )
    return _finalise(result, check, floor)


# --------------------------------------------------------------------- Type C

def score_type_c(check: dict, turns: list[dict], floor: float) -> dict:
    rule = check["script_or_rule"]
    metric = rule["metric"]
    threshold = float(rule.get("threshold", 0))
    result = _base(check)
    ordered = sorted(turns, key=lambda t: (t["start_sec"], t["idx"]))

    occurrences: list[dict] = []
    observed: float = 0
    anchor = ordered[0] if ordered else None

    if metric == "max_silence_gap_sec":
        for previous, nxt in zip(ordered, ordered[1:]):
            gap = nxt["start_sec"] - previous["end_sec"]
            if gap >= threshold:
                occurrences.append({
                    "start_sec": previous["end_sec"],
                    "timestamp": mmss(previous["end_sec"]),
                    "duration_sec": round(gap, 1),
                    "after_turn_idx": previous["idx"],
                })
            observed = max(observed, round(gap, 1))
        if occurrences:
            longest = max(occurrences, key=lambda o: o["duration_sec"])
            anchor = next((t for t in ordered if t["idx"] == longest["after_turn_idx"]), anchor)
            note = (f"{longest['duration_sec']:g}s of dead air at {longest['timestamp']}"
                    + (f" ({len(occurrences)} stretches over {threshold:g}s in total)" if len(occurrences) > 1 else ""))
            start_sec = longest["start_sec"]
        else:
            note = f"longest silence {observed:g}s, within the {threshold:g}s coaching threshold"
            start_sec = anchor["start_sec"] if anchor else None
    elif metric == "overlap_count":
        for previous, nxt in zip(ordered, ordered[1:]):
            if nxt["start_sec"] < previous["end_sec"]:
                occurrences.append({
                    "start_sec": nxt["start_sec"],
                    "timestamp": mmss(nxt["start_sec"]),
                    "overlap_sec": round(previous["end_sec"] - nxt["start_sec"], 1),
                    "turn_idx": nxt["idx"],
                })
        observed = len(occurrences)
        if occurrences:
            anchor = next((t for t in ordered if t["idx"] == occurrences[0]["turn_idx"]), anchor)
            start_sec = occurrences[0]["start_sec"]
        else:
            start_sec = anchor["start_sec"] if anchor else None
        note = (f"{observed:g} talk-over event(s)"
                + (f", first at {occurrences[0]['timestamp']}" if occurrences else "")
                + (f" - above the {threshold:g} coaching threshold" if observed > threshold
                   else f" - within the {threshold:g} coaching threshold"))
    elif metric in {"truncated_turns", "confusion_markers"}:
        if metric == "truncated_turns":
            # A turn that stops mid-sentence: one party was cut off by the other.
            hits = [t for t in ordered if t["text"].strip()
                    and not re.search(r"""[.?!"')\]]$""", t["text"].strip())]
            label = "turn(s) cut off mid-sentence"
        else:
            markers = [m.lower() for m in rule.get("markers", [])]
            hits = [t for t in ordered if t.get("speaker") == "customer"
                    and any(m in t["text"].lower() for m in markers)]
            label = "customer turn(s) asking the agent to repeat or explain"
        occurrences = [{"start_sec": t["start_sec"], "timestamp": mmss(t["start_sec"]),
                        "turn_idx": t["idx"], "text": t["text"][:80]} for t in hits]
        observed = len(hits)
        if hits:
            anchor, start_sec = hits[0], hits[0]["start_sec"]
        else:
            start_sec = anchor["start_sec"] if anchor else None
        note = (f"{observed} {label}"
                + (f", first at {occurrences[0]['timestamp']}" if occurrences else "")
                + (f" - above the {threshold:g} coaching threshold" if observed > threshold
                   else f" - within the {threshold:g} coaching threshold"))
    else:
        note, start_sec = f"unsupported metric {metric!r}", None

    exceeded = (observed > threshold if metric in {"overlap_count", "truncated_turns", "confusion_markers"}
                else bool(occurrences))
    result.update(
        status=STATUS_FAIL if exceeded else STATUS_PASS,
        confidence=0.99,
        quote=anchor["text"] if anchor else "",
        start_sec=start_sec,
        turn_idx=anchor["idx"] if anchor else None,
        speaker=anchor.get("speaker") if anchor else None,
        note=note,
        evidence={
            "method": "signal_note",
            "llm_used": False,
            "metric": metric,
            "observed": observed,
            "threshold": threshold,
            "exceeded": exceeded,
            "occurrences": occurrences[:10],
            "occurrence_count": len(occurrences),
            "never_blocks": True,
            "policy": "Type C is coaching signal only. It is excluded from the gate entirely and "
                      "blocks_sale is hard-coded false - silence and talk-over are never treated as "
                      "evidence of non-compliance.",
            "confidence_formula": CONFIDENCE_FORMULA["C"],
        },
    )
    return _finalise(result, check, floor)
