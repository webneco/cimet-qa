"""Deterministic Type B extractors for the NBN-QA pack.

Same contract as offline_extract.py: report what the AGENT said, quote one real turn,
abstain rather than guess. Two things specific to internet sales calls:

  * Numbers come as words, often run together by ASR ("forty two dollars and
    ninetyMhmm"). The parsers here undo that and handle hundreds.
  * An agent can assert a figure AND read a different one off the customer's order
    screen. `value` is the agent's own assertion; every other mention - including
    screen readings - goes to `other_mentions`, so the evidence shows both.
"""

from __future__ import annotations

import re

from .offline_extract import _abstain, _hit
from .textutil import normalise

_UNITS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
         "eighty": 80, "ninety": 90}
_NUMBER_WORDS = set(_UNITS) | set(_TENS) | {"hundred", "and", "point"}

SCREEN_CUE = re.compile(r"you will see|you can see|still showing|still coming up|showing that|it says|on the screen",
                        re.IGNORECASE)


def degl(text: str) -> str:
    """Undo ASR run-togethers: 'ninetyMhmm' -> 'ninety Mhmm', 'Yeah.K.' -> 'Yeah. K.'"""
    text = normalise(text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    return re.sub(r"(?<=[.?!,])(?=[A-Za-z])", " ", text)


def words_to_number(words: list[str]) -> float | None:
    """['three','hundred','and','seventeen'] -> 317, ['eight','point','five'] -> 8.5."""
    # A strict grammar, so 'twenty five twenty five' (a repeat) is not read as 50:
    #   [unit hundred [and]] [tens [digit] | teen | digit]
    if not words or words[0] in {"and", "point", "hundred"}:
        return None
    value, prev, has_hundred, i = 0, None, False, 0
    while i < len(words) and words[i] != "point":
        w = words[i]
        after_hundred = prev in {"hundred", "and"}
        if w in _TENS and prev in {None} | ({"hundred", "and"} if after_hundred else set()):
            value, prev = value + _TENS[w], "tens"
        elif w in _UNITS and _UNITS[w] >= 10 and (prev is None or after_hundred):
            value, prev = value + _UNITS[w], "teen"
        elif w in _UNITS and _UNITS[w] < 10 and (prev in {None, "tens"} or after_hundred):
            value, prev = value + _UNITS[w], "digit"
        elif w == "hundred" and prev == "digit" and not has_hundred and value < 10:
            value, prev, has_hundred = value * 100, "hundred", True
        elif w == "and" and prev == "hundred":
            prev = "and"
        else:
            return None
        i += 1
    if prev in {None, "and"}:
        return None
    value = float(value)
    if i < len(words) and words[i] == "point":
        digits = "".join(str(_UNITS[w]) for w in words[i + 1:] if w in _UNITS and _UNITS[w] < 10)
        if not digits:
            return None
        value = float(f"{int(value)}.{digits}")
    return value


def _number_before(text: str, end: int, max_words: int = 7) -> tuple[float | None, int]:
    """The number spoken immediately before position `end` (words or digits)."""
    head = text[:end].rstrip()
    digit = re.search(r"\$?(\d+(?:\.\d+)?)\s*$", head)
    if digit:
        return float(digit.group(1)), digit.start()
    words = list(re.finditer(r"[a-z]+", head.lower()))[-max_words:]
    for k in range(len(words)):
        chunk = [w.group(0) for w in words[k:]]
        value = words_to_number(chunk)
        if value is not None:
            return value, words[k].start()
    return None, end


def amounts(text: str) -> list[tuple[int, int, int]]:
    """Every dollar amount in a turn as (cents, start, end). 'forty two dollars and ninety' -> 4290."""
    text = degl(text)
    out = []
    for m in re.finditer(r"\bdollars?\b", text, re.IGNORECASE):
        dollars, start = _number_before(text, m.start())
        if dollars is None:
            continue
        end, cents = m.end(), 0
        tail = re.match(r"\s+and\s+((?:[a-z]+[\s-]?){1,2})", text[m.end():], re.IGNORECASE)
        if tail:
            parts = re.findall(r"[a-z]+", tail.group(1).lower())
            for n in (2, 1):
                value = words_to_number(parts[:n]) if len(parts) >= n else None
                if value is not None and value < 100:
                    cents, end = int(value), m.end() + len(tail.group(0))
                    break
        out.append((round(dollars * 100) + cents, start, end))
    for m in re.finditer(r"\$\s?(\d+(?:\.\d{2})?)", text):
        out.append((round(float(m.group(1)) * 100), m.start(), m.end()))
    return sorted(out, key=lambda a: a[1])


def _number_after(text: str, start: int) -> tuple[float | None, int]:
    """The first number spoken at or after `start` (skipping filler like 'of', 'will be')."""
    words = list(re.finditer(r"[a-z]+|\d+(?:\.\d+)?", text[start:start + 90].lower()))
    for k, w in enumerate(words):
        if w.group(0)[0].isdigit():
            return float(w.group(0)), start + w.end()
        if w.group(0) in _NUMBER_WORDS - {"and", "point", "hundred"}:
            run = []
            for x in words[k:]:
                if x.group(0) in _NUMBER_WORDS:
                    run.append(x)
                else:
                    break
            for n in range(len(run), 0, -1):
                value = words_to_number([x.group(0) for x in run[:n]])
                if value is not None:
                    return value, start + run[n - 1].end()
            return None, start
    return None, start


def _mention(value: str, turn: dict, kind: str = "statement") -> dict:
    return {"value": value, "kind": kind, "start_sec": turn["start_sec"], "turn_idx": turn["idx"],
            "quote": turn["text"][:160]}


def _agent(turns):
    return [t for t in turns if t.get("speaker") == "agent"]


def _result(mentions: list[tuple[str, dict, str]], confidence: float, reason: str, empty: str) -> dict:
    """First agent assertion is the value; everything else is kept as other_mentions."""
    if not mentions:
        return _abstain(empty)
    primary = next((m for m in mentions if m[2] == "statement"), mentions[0])
    value, turn, _ = primary
    hit = _hit(value, turn, confidence * turn.get("asr_confidence", 1.0), reason)
    hit["other_mentions"] = [_mention(*m) for m in mentions if m is not primary]
    return hit


# ------------------------------------------------------------------ fields

def extract_promo(turns):
    mentions = []
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        for cents, _s, e in amounts(text):
            m = re.match(r".{0,80}?first (\w+) months?", text[e:], re.DOTALL)
            if m:
                months = words_to_number([m.group(1)]) if not m.group(1).isdigit() else float(m.group(1))
                if months:
                    mentions.append((f"{cents / 100:.2f} for {int(months)} months", turn, "statement"))
    return _result(mentions, 0.92, "promotional price and period stated by the agent",
                   "the agent never stated a promotional price with its duration")


def extract_ongoing(turns):
    mentions = []
    before = re.compile(r"(regular price|original plan cost|and then|goes up to|after that|after the promo)\W+(\w+\W+){0,4}$")
    after = re.compile(r"^\W*(\w+\W+){0,3}(regular price)")
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        for cents, s, e in amounts(text):
            if re.match(r".{0,80}?first \w+ months?", text[e:], re.DOTALL):
                continue  # that amount is the promo, not the ongoing price
            if before.search(text[max(0, s - 60):s]) or after.search(text[e:e + 40]):
                mentions.append((f"{cents / 100:.2f}", turn, "statement"))
    return _result(mentions, 0.9, "price the agent said applies after the promotion",
                   "the agent never stated a price for after the promotion")


def extract_speed(turns):
    mentions = []
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        speeds = []
        for m in re.finditer(r"\b(mbps|mbbs|megabits)\b", text):
            value, _ = _number_before(text, m.start(), max_words=4)
            if value is not None:
                speeds.append(value)
        if "download" in text and "upload" in text and len(speeds) >= 2:
            evening = bool(re.search(r"seven pm to eleven pm|7 ?pm (?:to|-) ?11 ?pm", text))
            mentions.append((f"{speeds[0]:g}/{speeds[1]:g}", turn,
                             "statement" if evening else "statement (no evening window)"))
        elif speeds:
            mentions.append((f"{speeds[0]:g} (download only)", turn, "passing mention"))
    return _result(mentions, 0.9, "typical download/upload speed stated by the agent",
                   "the agent never stated a download and upload speed together")


def extract_modem(turns):
    mentions = []
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        for m in re.finditer(r"netcom\s+([a-z](?:\s?[a-z])?)\s+(forty|fifty|thirty|\d+)", text):
            model = m.group(1).replace(" ", "").upper()
            number = {"forty": "40", "fifty": "50", "thirty": "30"}.get(m.group(2), m.group(2))
            around = text[m.start():m.start() + 400]
            free = re.search(r"hundred percent free|no extra cost|free|zero dollar", around)
            cost = "$0" if free else "cost not stated"
            mentions.append((f"Netcom {model}{number}, {cost}", turn, "statement"))
    return _result(mentions, 0.88, "modem model and cost stated by the agent",
                   "the agent never named the modem model")


def extract_tmc(turns):
    mentions = []
    for turn in _agent(turns):
        text = degl(turn["text"])
        for m in re.finditer(r"minimum cost|still coming up with that", text, re.IGNORECASE):
            value, end = _number_after(text, m.end())
            if value is None:
                continue
            cents = round(value * 100)
            tail = re.match(r"\s*dollars?\s+and\s+([a-z]+)", text[end:end + 30], re.IGNORECASE)
            if tail and tail.group(1).lower() in _TENS:
                cents += _TENS[tail.group(1).lower()]
            sentence_start = max(text.rfind(".", 0, m.start()), text.rfind("?", 0, m.start())) + 1
            screen = bool(SCREEN_CUE.search(text[sentence_start:m.end() + 5]))
            mentions.append((f"{cents / 100:.2f}", turn, "screen reading" if screen else "statement"))
    return _result(mentions, 0.9, "total minimum cost the agent asserted (screen readings kept separately)",
                   "the total minimum cost was never stated")


def extract_dev_fee(turns):
    mentions = []
    not_charged = re.compile(r"will not pay|not be charged|won't be charged|not applicable|no need to pay")
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        fee = re.search(r"development fee", text)
        if fee:
            found = [a for a in amounts(text) if a[1] >= fee.start() - 5]
            if found:
                claim = "not charged" if not_charged.search(text) else "charged"
                mentions.append((f"{found[0][0] / 100:.2f}; {claim}", turn, "statement"))
                continue
        for g in re.finditer(r"not be charged", text):
            # Only an amount said right after the guarantee belongs to it; a screen
            # total elsewhere in the same turn does not.
            found = [a for a in amounts(text) if 0 <= a[1] - g.end() <= 25]
            mentions.append(((f"{found[0][0] / 100:.2f}; not charged" if found else "not charged"),
                             turn, "later guarantee"))
    return _result(mentions, 0.9, "what the agent said about the new development fee",
                   "the new development fee was never mentioned")


def extract_contract(turns):
    mentions = []
    for turn in _agent(turns):
        text = degl(turn["text"]).lower()
        for m in re.finditer(r"month[\s-]to[\s-]month|(?<!month )\b(\w+) to (\w+) contract|"
                             r"(\d+|twelve|twenty four|twenty-four)[\s-]months? contract|no lock[\s-]?in", text):
            phrase = m.group(0).strip()
            phrase = "month to month" if re.match(r"month[\s-]to[\s-]month", phrase) else phrase
            mentions.append((phrase, turn, "statement"))
    if not mentions:
        return _abstain("the agent never stated a contract term")
    phrases = list(dict.fromkeys(v for v, _, _ in mentions))
    first = mentions[0][1]
    hit = _hit(" | ".join(phrases), first, 0.85 * first.get("asr_confidence", 1.0),
               "every contract term the agent stated, in order")
    hit["other_mentions"] = [_mention(*m) for m in mentions[1:]]
    return hit


FIELDS = {
    "spoken_promo_price": extract_promo,
    "spoken_ongoing_price": extract_ongoing,
    "spoken_speed_peak": extract_speed,
    "spoken_modem": extract_modem,
    "spoken_tmc": extract_tmc,
    "spoken_development_fee": extract_dev_fee,
    "spoken_contract_term": extract_contract,
}
