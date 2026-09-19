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


# Real providers Australian customers use. Lets the comparator tell which side of an
# email mismatch is the odd one out: a CRM domain that is a near-miss of one of these
# is a keying error; a spoken domain that is not one of these is probably a mishear.
KNOWN_EMAIL_DOMAINS = {
    "gmail.com", "googlemail.com", "outlook.com", "outlook.com.au", "hotmail.com", "hotmail.com.au",
    "live.com", "live.com.au", "msn.com", "yahoo.com", "yahoo.com.au", "ymail.com", "icloud.com",
    "me.com", "mac.com", "bigpond.com", "bigpond.net.au", "bigpond.com.au", "optusnet.com.au",
    "tpg.com.au", "iinet.net.au", "internode.on.net", "aapt.net.au", "dodo.com.au",
    "westnet.com.au", "protonmail.com", "proton.me",
}


def sound_key(text: str) -> str:
    """A coarse 'sounds like' key for one email part. Two strings with the same key
    cannot be told apart by ear: rahman/raman, smith/smyth, jon/john, ph/f."""
    s = re.sub(r"[^a-z]", "", str(text or "").lower())
    for a, b in (("ph", "f"), ("ck", "k"), ("q", "k"), ("x", "ks"), ("z", "s"), ("y", "i"), ("c", "k")):
        s = s.replace(a, b)
    s = re.sub(r"(?<=[aeiou])h", "", s)          # silent h after a vowel: rahman -> raman
    s = re.sub(r"(?<=[^aeiousktw])h", "", s)     # jon/john; keeps sh, th, wh
    return re.sub(r"(.)\1+", r"\1", s)            # doubled letters sound single


def _email_mishear(obs: str, exp: str) -> tuple[bool, str]:
    """Is this mismatch something audio alone cannot settle? (plausible, why)"""
    if "@" not in obs or "@" not in exp:
        return False, ""
    obs_local, obs_domain = obs.split("@", 1)
    exp_local, exp_domain = exp.split("@", 1)
    if obs_domain != exp_domain:
        if exp_domain not in KNOWN_EMAIL_DOMAINS and obs_domain in KNOWN_EMAIL_DOMAINS \
                and levenshtein(obs_domain, exp_domain) <= 2:
            return False, (f"the CRM domain {exp_domain} is not a real provider and is "
                           f"{levenshtein(obs_domain, exp_domain)} keystrokes from {obs_domain}: a keying error in the CRM")
        if exp_domain in KNOWN_EMAIL_DOMAINS and obs_domain not in KNOWN_EMAIL_DOMAINS:
            return True, (f"the CRM holds a real provider ({exp_domain}) but the transcript has "
                          f"{obs_domain}, which is not one - most likely misheard on the call")
        if sound_key(obs_domain) == sound_key(exp_domain):
            return True, f"{obs_domain} and {exp_domain} sound the same"
        return False, ""
    if obs_local != exp_local and sound_key(obs_local) == sound_key(exp_local):
        return True, f"'{obs_local}' and '{exp_local}' sound the same, so the recording cannot settle the spelling"
    return False, ""


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


def _money_cents(value) -> int:
    """'42.90', '$42.90 for 6 months', '317' -> cents. The first amount wins."""
    match = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not match:
        raise ComparisonError(f"no amount found in {value!r}")
    return round(float(match.group(0)) * 100)


def _dollars(cents) -> str:
    return f"${cents / 100:,.2f}"


_WORD_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
             "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twenty four": 24,
             "twenty-four": 24, "thirty six": 36, "thirty-six": 36}


def _contract_months(text: str) -> list[tuple[str, int | None]]:
    """Every contract term in a statement: ('month to month', 0), ('12 months', 12),
    or (phrase, None) for a term-like phrase that is not a recognisable term."""
    out: list[tuple[str, int | None]] = []
    for part in re.split(r"\s*\|\s*", str(text or "").lower()):
        part = part.strip()
        if not part:
            continue
        if re.search(r"month[\s-]+to[\s-]+month|no lock[\s-]*in|no contract|no fixed term", part):
            out.append((part, 0))
            continue
        m = re.search(r"(\d+|" + "|".join(sorted(_WORD_NUM, key=len, reverse=True)) + r")[\s-]*months?", part)
        if m:
            n = m.group(1)
            out.append((part, int(n) if n.isdigit() else _WORD_NUM[n]))
            continue
        out.append((part, None))
    return out


def _model_key(text: str) -> str:
    """'Netcom CF40', 'netcom c f forty', 'Netcom CF40 Wi-Fi 6' -> 'netcomcf40'."""
    t = str(text or "").lower()
    # Descriptors are not part of the model: 'Netcom CF40 Wi-Fi 6 modem' is a CF40.
    t = re.sub(r"\bwi[\s-]?fi(?:[\s-]*(?:6e|6|six|7|seven)\b)?|\b(?:modem|router|pre-?configured)\b", " ", t)
    for word, num in (("forty", "40"), ("fifty", "50"), ("thirty", "30"), ("twenty", "20"), ("sixty", "60")):
        t = t.replace(word, num)
    return re.sub(r"[^a-z0-9]", "", t)


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
            mishear, why = _email_mishear(obs, exp) if obs != exp else (False, "")
            result.update(
                observed_normalised=obs, expected_normalised=exp,
                match=obs == exp, delta=distance,
                mishear_plausible=mishear, mishear_reason=why,
                detail=(
                    "spoken address matches the CRM exactly"
                    if obs == exp
                    else f"spoken {obs} vs CRM {exp} - {distance} character "
                         f"{'difference' if distance == 1 else 'differences'}"
                         + (f"; {why}" if why else "")
                ),
            )
        elif comparator == "money_cents":
            obs, exp = _money_cents(observed), int(expected)
            result.update(observed_normalised=_dollars(obs), expected_normalised=_dollars(exp),
                          match=abs(obs - exp) <= tolerance, delta=obs - exp,
                          detail=(f"spoken {_dollars(obs)} matches the plan" if abs(obs - exp) <= tolerance
                                  else f"spoken {_dollars(obs)} vs plan {_dollars(exp)}"))
        elif comparator == "promo_price_months":
            price_cents, months = expected["plan.promo_price_cents"], expected["plan.promo_months"]
            obs = _money_cents(observed)
            m = re.search(r"(\d+)\s*months?", str(observed))
            obs_months = int(m.group(1)) if m else None
            price_ok = abs(obs - price_cents) <= tolerance
            months_ok = obs_months == months
            result.update(
                observed_normalised=f"{_dollars(obs)} for {obs_months if obs_months is not None else '?'} months",
                expected_normalised=f"{_dollars(price_cents)} for {months} months",
                match=price_ok and months_ok if obs_months is not None else (None if price_ok else False),
                delta=obs - price_cents,
            )
            if obs_months is None and price_ok:
                raise ComparisonError("the promo price was stated but not how long it lasts")
            result["detail"] = ("spoken promo matches the plan" if price_ok and months_ok else
                                "; ".join(x for x in (
                                    None if price_ok else f"price {_dollars(obs)} vs plan {_dollars(price_cents)}",
                                    None if months_ok else f"{obs_months} months vs plan {months}") if x))
        elif comparator == "speed_pair":
            nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", str(observed))]
            if len(nums) < 2:
                raise ComparisonError(f"need a download and an upload speed, got {observed!r}")
            down, up = expected["plan.speed_download_mbps"], expected["plan.speed_upload_mbps"]
            ok = abs(nums[0] - down) < 1e-6 and abs(nums[1] - up) < 1e-6
            result.update(observed_normalised=f"{nums[0]:g}/{nums[1]:g} Mbps",
                          expected_normalised=f"{down:g}/{up:g} Mbps", match=ok,
                          detail=("spoken typical speed matches the plan" if ok
                                  else f"spoken {nums[0]:g}/{nums[1]:g} vs plan {down:g}/{up:g} Mbps"))
        elif comparator == "modem_model_cost":
            model, cost = expected["plan.modem_model"], expected["plan.modem_cost_cents"]
            text = str(observed)
            obs_model = _model_key(text.split(",")[0])
            obs_cost = 0 if re.search(r"free|no (extra )?cost|\$\s*0(?!\.?\d*[1-9])", text, re.I) else _money_cents(text.split(",", 1)[-1])
            model_ok = obs_model == _model_key(model)
            cost_ok = obs_cost == cost
            result.update(observed_normalised=f"{text.split(',')[0].strip()}, {_dollars(obs_cost)}",
                          expected_normalised=f"{model}, {_dollars(cost)}", match=model_ok and cost_ok,
                          detail=("spoken modem and cost match the plan" if model_ok and cost_ok else "; ".join(
                              x for x in (None if model_ok else f"model {text.split(',')[0].strip()} vs plan {model}",
                                          None if cost_ok else f"cost {_dollars(obs_cost)} vs plan {_dollars(cost)}") if x)))
        elif comparator == "tmc_vs_conditional_fee":
            tmc, fee = expected["tmc_cents"], expected["new_development_fee_cents"]
            applicable = expected["fee_applicable"]
            obs = _money_cents(observed)
            result.update(observed_normalised=_dollars(obs), expected_normalised=_dollars(tmc), delta=obs - tmc)
            if abs(obs - tmc) <= tolerance:
                result.update(match=True, detail=f"spoken TMC {_dollars(obs)} matches the order")
            else:
                result.update(match=False, detail=f"agent told the customer the TMC is {_dollars(obs)}; "
                                                  f"the order's TMC is {_dollars(tmc)}")
                without_fee = tmc - fee
                if applicable is False and fee and abs(obs - without_fee) <= tolerance:
                    result.update(conflict=True, conflict_reason=(
                        f"the agent's TMC {_dollars(obs)} is the order TMC {_dollars(tmc)} less the "
                        f"{_dollars(fee)} new development fee, which the lead marks not applicable - but the "
                        f"order screen still totals {_dollars(tmc)} and the agent told the customer to disregard it"))
        elif comparator == "fee_vs_screen":
            fee, applicable, tmc = (expected["new_development_fee_cents"], expected["fee_applicable"],
                                    expected["tmc_cents"])
            text = str(observed).lower()
            obs = _money_cents(text)
            said_not_charged = bool(re.search(r"not charged|won't be charged|not be charged|will not pay|"
                                              r"not applicable|waived|no charge", text))
            result.update(observed_normalised=f"{_dollars(obs)}; {'not charged' if said_not_charged else 'charged'}",
                          expected_normalised=f"{_dollars(fee)}; {'applies' if applicable else 'not applicable'}; "
                                              f"order TMC {_dollars(tmc)}", delta=obs - fee)
            if obs != fee:
                result.update(match=False, detail=f"agent quoted the fee as {_dollars(obs)}; it is {_dollars(fee)}")
            elif said_not_charged and applicable:
                result.update(match=False, detail="agent said the fee will not be charged, but the lead says it applies")
            elif not said_not_charged and applicable is False:
                result.update(match=False, detail="agent said the fee applies, but the lead says it does not")
            elif said_not_charged and applicable is False and tmc is not None and tmc >= fee:
                result.update(match=False, conflict=True, conflict_reason=(
                    f"the agent guaranteed the {_dollars(fee)} new development fee will not be charged (the lead "
                    f"agrees it is not applicable), but the order screen the customer submitted still shows a "
                    f"{_dollars(tmc)} total that includes it"),
                    detail="spoken guarantee disagrees with the order screen")
            else:
                result.update(match=True, detail="fee amount and applicability match, and the order agrees")
        elif comparator == "contract_term":
            terms = _contract_months(observed)
            if not terms:
                raise ComparisonError("no contract term was stated")
            known = {n for _, n in terms if n is not None}
            unclear = [t for t, n in terms if n is None]
            result.update(observed_normalised=" | ".join(t for t, _ in terms), expected_normalised=expected)
            if len(known) > 1:
                result.update(match=False, conflict=False,
                              detail=f"agent stated conflicting contract terms: {' vs '.join(t for t, _ in terms)}")
            elif expected is None:
                raise ComparisonError(
                    "the lead carries no contract term to compare against"
                    + (f"; the agent also said '{unclear[0]}', which is not a recognisable term" if unclear else ""))
            elif unclear:
                raise ComparisonError(f"'{unclear[0]}' is not a recognisable contract term, so the "
                                      "stated terms cannot be confirmed consistent")
            else:
                n = known.pop()
                result.update(match=n == int(expected),
                              detail=(f"stated term matches the plan ({n} months)" if n == int(expected)
                                      else f"stated {n} months vs plan {expected} months"))
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
