"""Deterministic comparators for Type B checks.

The LLM extracts what was spoken. This module - and only this module - decides
whether it matches the CRM or plan. Keeping the two apart is what makes a Type B
result reproducible: rerun the comparison and you get the same verdict.
"""

from __future__ import annotations

import re

from .textutil import levenshtein

_FUEL_SYNONYMS = {
    "electricity": "electricity", "electric": "electricity", "power": "electricity",
    "elec": "electricity", "electricity only": "electricity",
    "gas": "gas", "natural gas": "gas", "gas only": "gas",
    "dual": "dual fuel", "dual fuel": "dual fuel", "both": "dual fuel",
    "electricity and gas": "dual fuel",
}


class ComparisonError(ValueError):
    """The spoken value could not be put into comparable form."""


def _to_cents(value) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).lower()
    text = re.sub(r"(cents?|c/kwh|per kilowatt hour|kwh|/kwh|incl\.? gst|including gst|\$)", " ", text)
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        raise ComparisonError(f"no numeric rate found in {value!r}")
    number = float(match.group(0))
    # A rate given in dollars ("0.319 per kWh") is normalised to cents.
    return number * 100 if number < 3 else number


def _norm_email(value) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower().rstrip(".")


def _norm_digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _norm_enum(value) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip().lower().rstrip(".")
    return _FUEL_SYNONYMS.get(text, text)


def compare(comparator: str, observed, expected, tolerance: float = 0.0) -> dict:
    """Return a verdict dict. `match` is None when the spoken value is unusable."""
    result = {
        "comparator": comparator,
        "observed_raw": observed,
        "expected_raw": expected,
        "tolerance": tolerance,
        "match": None,
        "detail": "",
        "delta": None,
    }
    try:
        if comparator == "numeric_cents":
            obs, exp = _to_cents(observed), _to_cents(expected)
            delta = round(obs - exp, 4)
            result.update(
                observed_normalised=f"{obs:g}",
                expected_normalised=f"{exp:g}",
                match=abs(delta) <= tolerance,
                delta=delta,
                detail=(
                    f"spoken {obs:g}c/kWh vs plan {exp:g}c/kWh (tolerance {tolerance:g}c)"
                    if abs(delta) <= tolerance
                    else f"spoken {obs:g}c/kWh is {abs(delta):g}c "
                         f"{'below' if delta < 0 else 'above'} the plan rate of {exp:g}c/kWh"
                ),
            )
        elif comparator == "email_exact":
            obs, exp = _norm_email(observed), _norm_email(expected)
            if not obs:
                raise ComparisonError("spoken email is empty")
            distance = levenshtein(obs, exp)
            result.update(
                observed_normalised=obs, expected_normalised=exp,
                match=obs == exp, delta=distance,
                detail=(
                    "spoken address matches the CRM exactly"
                    if obs == exp
                    else f"spoken {obs} vs CRM {exp} - {distance} character "
                         f"{'difference' if distance == 1 else 'differences'}"
                         + (", consistent with a keying error in the CRM" if distance <= 2 else "")
                ),
            )
        elif comparator == "digits_exact":
            obs, exp = _norm_digits(observed), _norm_digits(expected)
            if not obs:
                raise ComparisonError("no digits in the spoken value")
            result.update(
                observed_normalised=obs, expected_normalised=exp, match=obs == exp,
                detail=("spoken digits match the CRM" if obs == exp
                        else f"spoken {obs} vs CRM {exp}"),
            )
        elif comparator == "enum_ci":
            obs, exp = _norm_enum(observed), _norm_enum(expected)
            if not obs:
                raise ComparisonError("spoken value is empty")
            result.update(
                observed_normalised=obs, expected_normalised=exp, match=obs == exp,
                detail=(f"confirmed as {obs}" if obs == exp else f"spoken {obs} vs CRM {exp}"),
            )
        else:
            raise ComparisonError(f"unknown comparator {comparator!r}")
    except ComparisonError as exc:
        result.update(match=None, detail=f"not comparable: {exc}", error=str(exc))
    return result
