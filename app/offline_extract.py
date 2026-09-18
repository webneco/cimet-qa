"""Deterministic Type B extractor used when no API key is configured.

It obeys exactly the same contract as the LLM extractor, including the one that
matters most: when the audio is obstructed it abstains rather than filling the
gap. `extractor` on every result records which of the two produced the value, so
a demo run without a key is never mistaken for an LLM run.
"""

from __future__ import annotations

import re

from .textutil import MARKER_RE, normalise

EXTRACTOR_NAME = "deterministic-fallback"

_UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
         "seventy": 70, "eighty": 80, "ninety": 90}
_DIGIT_WORDS = {w: str(v) for w, v in _UNITS.items() if v < 10}


def _abstain(reason: str, quote: str = "", start_sec: float = -1, spoken_by: str = "unknown") -> dict:
    return {
        "found": False, "value": "", "quote": quote, "start_sec": start_sec,
        "spoken_by": spoken_by, "confidence": 0.0, "reason": reason,
        "_meta": {"extractor": EXTRACTOR_NAME, "model": None},
    }


def _hit(value: str, turn: dict, confidence: float, reason: str) -> dict:
    return {
        "found": True, "value": value, "quote": turn["text"],
        "start_sec": turn["start_sec"], "spoken_by": turn.get("speaker", "unknown"),
        "confidence": round(min(0.97, confidence), 2), "reason": reason,
        "_meta": {"extractor": EXTRACTOR_NAME, "model": None},
    }


def _words_to_number(text: str) -> float | None:
    """'twenty-eight point six' -> 28.6. Returns None if it is not a clean number."""
    parts = re.split(r"[\s-]+", text.strip().lower())
    parts = [p for p in parts if p]
    if not parts:
        return None
    whole, seen = 0, False
    idx = 0
    while idx < len(parts) and parts[idx] not in {"point"}:
        word = parts[idx]
        if word in _TENS:
            whole += _TENS[word]
            seen = True
        elif word in _UNITS:
            whole += _UNITS[word]
            seen = True
        elif word == "and":
            pass
        else:
            return None
        idx += 1
    if not seen:
        return None
    if idx < len(parts) and parts[idx] == "point":
        decimals = ""
        for word in parts[idx + 1:]:
            if word in _DIGIT_WORDS:
                decimals += _DIGIT_WORDS[word]
            else:
                return None
        if decimals:
            return float(f"{whole}.{decimals}")
    return float(whole)


def _digit_runs(text: str) -> list[str]:
    """Digit sequences read out one at a time, in digits or in words."""
    runs: list[str] = []
    for match in re.finditer(r"\d(?:[ -]\d){3,}|\d{4,}", text):
        runs.append(re.sub(r"\D", "", match.group(0)))
    words = re.findall(r"[a-z]+", text.lower())
    current: list[str] = []
    for word in words:
        if word in _DIGIT_WORDS:
            current.append(_DIGIT_WORDS[word])
        else:
            if len(current) >= 4:
                runs.append("".join(current))
            current = []
    if len(current) >= 4:
        runs.append("".join(current))
    return runs


# --------------------------------------------------------------------- fields

def _extract_rate(turns: list[dict]) -> dict:
    obstructed = None
    for turn in turns:
        text = normalise(turn["text"])
        match = re.search(r"(.{0,70}?)cents?\s+(?:per|a)\s+kilowatt[\s-]*hour", text, re.I)
        if not match:
            continue
        prefix = match.group(1).rstrip()
        # Only the words immediately before the unit can carry the figure.
        tail = prefix[-40:]
        if MARKER_RE.search(tail) or re.search(r"[-—]$", tail):
            # A number cut off by crosstalk is not a number. Record and keep looking.
            obstructed = obstructed or turn
            continue
        numeric = re.search(r"(\d+(?:\.\d+)?)$", tail)
        if numeric:
            return _hit(numeric.group(1), turn, 0.95 * turn.get("asr_confidence", 1.0),
                        "numeric rate stated immediately before the unit")
        words = re.findall(r"[a-z]+", tail.lower())
        for size in range(min(6, len(words)), 0, -1):
            value = _words_to_number(" ".join(words[-size:]))
            if value is not None:
                return _hit(f"{value:g}", turn, 0.88 * turn.get("asr_confidence", 1.0),
                            "rate spoken in words immediately before the unit")
        obstructed = obstructed or turn
    if obstructed is not None:
        return _abstain(
            "the rate was spoken but the figure itself is obstructed by crosstalk or an incomplete word",
            obstructed["text"], obstructed["start_sec"], obstructed.get("speaker", "unknown"),
        )
    return _abstain("no usage rate in cents per kilowatt hour was stated on this call")


def _assemble_spoken_email(local: str, domain: str) -> str:
    def join(part: str) -> str:
        pieces = [p for p in re.split(r"\s+dot\s+", part.strip().lower()) if p]
        return ".".join(pieces)
    return f"{join(local)}@{join(domain)}"


def _extract_email(turns: list[dict]) -> dict:
    obstructed = None
    ordered = [t for t in turns if t.get("speaker") == "customer"] + \
              [t for t in turns if t.get("speaker") != "customer"]
    for turn in ordered:
        text = normalise(turn["text"])
        literal = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)
        if literal:
            return _hit(literal.group(0).lower().rstrip("."), turn,
                        0.96 * turn.get("asr_confidence", 1.0), "literal email address in the transcript")
        spoken = re.search(
            r"\b([a-z]+(?:\s+dot\s+[a-z]+)*)\s+at\s+([a-z]+(?:\s+dot\s+[a-z]+)+)", text, re.I
        )
        if spoken:
            return _hit(_assemble_spoken_email(spoken.group(1), spoken.group(2)), turn,
                        0.93 * turn.get("asr_confidence", 1.0), "email assembled from the spoken form")
        if re.search(r"\bat\b", text, re.I) and MARKER_RE.search(text) and re.search(r"\bdot\b", text, re.I):
            obstructed = obstructed or turn
    if obstructed is not None:
        return _abstain(
            "an email address was being given but part of it is obscured by an audio marker",
            obstructed["text"], obstructed["start_sec"], obstructed.get("speaker", "unknown"),
        )
    return _abstain("no email address was spoken on this call")


def _extract_nmi(turns: list[dict]) -> dict:
    obstructed = None
    for turn in turns:
        text = normalise(turn["text"])
        if not re.search(r"\bn\.?m\.?i\.?\b|national meter identifier", text, re.I):
            continue
        for run in _digit_runs(text):
            if 10 <= len(run) <= 11:
                return _hit(run, turn, 0.94 * turn.get("asr_confidence", 1.0),
                            "digit sequence of NMI length read out in an NMI turn")
        obstructed = obstructed or turn
    if obstructed is None:
        for turn in turns:
            for run in _digit_runs(normalise(turn["text"])):
                if len(run) == 11:
                    return _hit(run, turn, 0.72 * turn.get("asr_confidence", 1.0),
                                "11 digit sequence found outside an explicit NMI turn")
    if obstructed is not None:
        return _abstain(
            "the NMI was being read back but the digits are broken by an audio marker",
            obstructed["text"], obstructed["start_sec"], obstructed.get("speaker", "unknown"),
        )
    return _abstain("no NMI was read back on this call")


def _extract_fuel(turns: list[dict]) -> dict:
    for turn in turns:
        text = normalise(turn["text"]).lower()
        if re.search(r"\bdual fuel\b", text) or ("electricity and gas" in text):
            return _hit("dual fuel", turn, 0.93 * turn.get("asr_confidence", 1.0), "dual fuel stated")
        if re.search(r"\belectricity only\b", text):
            return _hit("electricity", turn, 0.95 * turn.get("asr_confidence", 1.0),
                        "'electricity only' stated on the call")
        if re.search(r"\bgas only\b", text):
            return _hit("gas", turn, 0.93 * turn.get("asr_confidence", 1.0), "'gas only' stated")
    return _abstain("the fuel type was never confirmed out loud")


_FIELDS = {
    "spoken_peak_rate_cents": _extract_rate,
    "spoken_customer_email": _extract_email,
    "spoken_nmi": _extract_nmi,
    "spoken_fuel_type": _extract_fuel,
}


class OfflineExtractor:
    """Same interface as llm.Extractor, minus the network."""

    name = EXTRACTOR_NAME
    available = True
    unavailable_reason = None

    def extract(self, check: dict, turns: list[dict], secrets: list[str]) -> dict:
        field = check["script_or_rule"]["field"]
        handler = _FIELDS.get(field)
        if handler is None:
            return _abstain(f"no deterministic extractor implemented for field {field!r}")
        return handler(turns)
