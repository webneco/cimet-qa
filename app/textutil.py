"""Text normalisation, script-span matching primitives, and PII redaction.

Everything here is deterministic. Type A scoring uses only this module, which is
why Type A has no hallucination surface at all.
"""

from __future__ import annotations

import re
import unicodedata

# ASR quality markers. These mean "the audio was unusable here", which is
# categorically different from "the agent did not say it".
MARKER_RE = re.compile(r"\[(inaudible|crosstalk|silence|unintelligible|indistinct)\]", re.IGNORECASE)

# A run of 13 or more digits, tolerating single spaces or hyphens between them.
# 10-11 digit NMIs and 10 digit phone numbers deliberately fall below this.
LONG_DIGIT_RUN_RE = re.compile(r"\d(?:[ -]?\d){12,}")

_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[a-z]+")

_SMART = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", " ": " ",
}


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    for bad, good in _SMART.items():
        text = text.replace(bad, good)
    return text


def strip_markers(text: str) -> tuple[str, list[str]]:
    """Return (text without ASR markers, list of markers found)."""
    found = [m.group(1).lower() for m in MARKER_RE.finditer(text or "")]
    return MARKER_RE.sub(" ", text or ""), found


def tokens(text: str) -> list[str]:
    """Lowercase word/number tokens. '28.6' stays a single token."""
    return _TOKEN_RE.findall(normalise(text).lower())


def content_tokens(text: str) -> list[str]:
    """Tokens with ASR markers removed, so '[inaudible]' can never satisfy a phrase."""
    cleaned, _ = strip_markers(normalise(text))
    return tokens(cleaned)


def lcs_length(a: list[str], b: list[str]) -> int:
    """Longest common subsequence length. Order-aware, tolerant of filler words."""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    for token_a in a:
        cur = [0]
        for j, token_b in enumerate(b, start=1):
            if token_a == token_b:
                cur.append(prev[j - 1] + 1)
            else:
                cur.append(max(prev[j], cur[j - 1]))
        prev = cur
    return prev[-1]


def phrase_ratio(phrase: list[str], window: list[str]) -> float:
    """Fraction of the required phrase present, in order, inside the window."""
    if not phrase:
        return 1.0
    return lcs_length(phrase, window) / len(phrase)


def redact_long_digits(text: str, min_run: int = 13) -> tuple[str, int]:
    """Mask any run of `min_run`+ digits. Returns (masked text, count masked).

    Card numbers are masked outright - we do not keep the last four. Nothing
    downstream (LLM prompt, UI, stored evidence) ever sees the original digits.
    """
    if min_run == 13:
        pattern = LONG_DIGIT_RUN_RE
    else:
        pattern = re.compile(r"\d(?:[ -]?\d){%d,}" % (min_run - 1))
    count = 0

    def _mask(match: re.Match) -> str:
        nonlocal count
        count += 1
        digits = re.sub(r"\D", "", match.group(0))
        return f"[REDACTED {len(digits)}-DIGIT SEQUENCE]"

    return pattern.sub(_mask, normalise(text)), count


def mmss(seconds: float | int | None) -> str:
    """Seconds to mm:ss, the format the brief and the QA team both use."""
    if seconds is None:
        return "--:--"
    seconds = int(round(float(seconds)))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 60:02d}:{seconds % 60:02d}"


def levenshtein(a: str, b: str) -> int:
    """Edit distance, used only to explain *why* two strings differ."""
    a, b = a or "", b or ""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i]
        for j, cb in enumerate(b, start=1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]
