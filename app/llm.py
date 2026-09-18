"""Type B extraction via Claude, with the guardrails that make it auditable.

The contract this module enforces:

  The LLM reports what was SAID. It never sees, and therefore can never supply,
  the CRM or plan value it is being compared against.

That is guaranteed two ways:

  1. Structurally - `build_prompt()` accepts only the check definition and the
     ingested transcript. CRM and plan data are not in scope.
  2. At runtime - `assert_no_reference_leak()` refuses to send a prompt in which
     a CRM/plan value appears that is not itself spoken in the transcript. If the
     assertion trips, the request is never made.
"""

from __future__ import annotations

import hashlib
import json

from .textutil import mmss

MODEL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

SYSTEM_PROMPT = """You are the extraction step of a compliance QA pipeline for Australian energy retail sales calls.

Your only job is to report what was SAID on the call. You are given the call transcript and nothing else.

Hard rules:
1. Report only what appears in the transcript. Never infer, complete, correct, normalise away, or guess a value.
2. You do not have access to the CRM record or the plan catalogue, and you must never output a value that is not spoken in the transcript. You are NOT being asked whether the value is correct - a separate deterministic step does that comparison. Supplying a plausible value is a pipeline failure, not a help.
3. If the value is obscured by [inaudible], [crosstalk], [silence], talk-over, or is simply never stated, set found=false and give a short reason. Abstaining is always the correct answer when you are not certain. A confident wrong answer here holds up or wrongly releases a real customer's contract.
4. A partial value is an abstention, not a value. "thirty-" followed by [crosstalk] is not a rate. "m dot rossi at [inaudible] dot com" is not an email address. Do not fill the gap.
5. `quote` must be copied character for character from a SINGLE transcript turn, including any markers it contains. Do not paraphrase, tidy, translate or join turns.
6. `start_sec` must be the start_sec of the exact turn you quoted, copied from the header of that line.
7. Spoken forms must be assembled literally: "j dot smith at gmail dot com" is "j.smith@gmail.com"; digits read out one at a time are joined in the order spoken. Assemble only what is actually spoken.
8. Text like [REDACTED 16-DIGIT SEQUENCE] is masked payment data. Never attempt to reconstruct it and never quote around it to imply its contents.
9. Set confidence to your probability that the value you report is exactly what was said. Use values below 0.6 freely when the audio is poor - the pipeline treats low confidence as "needs a human", which is the right outcome."""

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {
            "type": "boolean",
            "description": "True only if the value is stated clearly enough to report verbatim.",
        },
        "value": {
            "type": "string",
            "description": "The extracted value, assembled from what was spoken. Empty string if found is false.",
        },
        "quote": {
            "type": "string",
            "description": "Verbatim text of the single transcript turn the value came from. Empty string if found is false.",
        },
        "start_sec": {
            "type": "number",
            "description": "start_sec of the quoted turn, copied from that line's header. Use -1 if found is false.",
        },
        "spoken_by": {
            "type": "string",
            "enum": ["agent", "customer", "unknown"],
            "description": "Who said it.",
        },
        "confidence": {
            "type": "number",
            "description": "0 to 1. Probability the reported value is exactly what was said.",
        },
        "reason": {
            "type": "string",
            "description": "One sentence. If found is false, why - name the obstruction (inaudible, crosstalk, never stated).",
        },
    },
    "required": ["found", "value", "quote", "start_sec", "spoken_by", "confidence", "reason"],
    "additionalProperties": False,
}


class LeakGuardError(RuntimeError):
    """Raised when a CRM/plan value would have been exposed to the model."""


def render_transcript(turns: list[dict]) -> str:
    lines = []
    for turn in turns:
        lines.append(
            f"[start_sec={int(turn['start_sec'])} | {mmss(turn['start_sec'])}] "
            f"{turn['speaker'].upper()} ({turn.get('speaker_name', '')}): {turn['text']}"
        )
    return "\n".join(lines)


def build_prompt(check: dict, turns: list[dict]) -> str:
    """Build the user message. Sees only the check definition and the transcript."""
    rule = check["script_or_rule"]
    return (
        f"CALL TRANSCRIPT ({len(turns)} turns)\n"
        "----------------------------------------------------------------\n"
        f"{render_transcript(turns)}\n"
        "----------------------------------------------------------------\n\n"
        f"EXTRACTION TARGET ({check['check_id']} / {rule['field']}):\n"
        f"{rule['extraction_target']}\n\n"
        "Report what was said. If it is obscured or never stated, set found=false. "
        "Do not guess and do not supply a value that is not spoken in the transcript above."
    )


def assert_no_reference_leak(prompt: str, secrets: list[str], transcript_text: str) -> None:
    """Refuse to send a prompt containing a CRM/plan value that was never spoken.

    A reference value that also appears in the transcript is fine - that is the
    agent correctly stating it, which is exactly what we are trying to detect.
    A reference value present in the prompt but absent from the transcript could
    only have come from the CRM, so the request is aborted.
    """
    haystack = prompt.lower()
    spoken = transcript_text.lower()
    for secret in secrets:
        needle = secret.lower()
        if needle in haystack and needle not in spoken:
            raise LeakGuardError(
                f"reference value {secret!r} reached the prompt without being spoken on the call"
            )


def _first_text(response) -> str:
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text
    raise ValueError("model returned no text block")


def _loads_lenient(text: str) -> dict:
    """Parse the JSON object. Structured output guarantees it; this is a seatbelt."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start:end + 1])


class Extractor:
    """Claude-backed extraction. `available` is False when the SDK or key is missing."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.client = None
        self.unavailable_reason: str | None = None
        self.name = "unavailable"
        if not settings.llm_enabled:
            self.unavailable_reason = "LLM disabled (no ANTHROPIC_API_KEY, or CIMET_LLM=off)"
            return
        try:
            import anthropic  # noqa: PLC0415 - optional dependency, imported on demand
        except ImportError:
            self.unavailable_reason = "the anthropic package is not installed (pip install anthropic)"
            return
        try:
            self.client = anthropic.Anthropic(
                api_key=settings.api_key or None,
                timeout=settings.llm_timeout_sec,
                max_retries=2,
            )
            self._anthropic = anthropic
            self.name = f"llm:{settings.model}"
        except Exception as exc:  # pragma: no cover - construction rarely fails
            self.unavailable_reason = f"could not construct the Anthropic client: {exc}"

    @property
    def available(self) -> bool:
        return self.client is not None

    def extract(self, check: dict, turns: list[dict], secrets: list[str]) -> dict:
        """Return a raw extraction dict. Raises on transport failure; caller falls back."""
        prompt = build_prompt(check, turns)
        transcript_text = render_transcript(turns)
        assert_no_reference_leak(prompt, secrets, transcript_text)

        prompt_sha = hashlib.sha256((SYSTEM_PROMPT + "\n" + prompt).encode("utf-8")).hexdigest()
        base = {
            "model": self.settings.model,
            "max_tokens": 8000,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": prompt}],
        }
        output_config = {
            "format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA},
            "effort": self.settings.llm_effort,
        }

        # Preferred call first, then progressively plainer ones, so an older
        # installed SDK degrades to a working request rather than to no request.
        strategies = [
            ("beta+refusal_fallback", lambda: self.client.beta.messages.create(
                betas=[MODEL_FALLBACK_BETA], fallbacks="default",
                output_config=output_config, **base)),
            ("messages+output_config", lambda: self.client.messages.create(
                output_config=output_config, **base)),
            ("messages+extra_body", lambda: self.client.messages.create(
                extra_body={"output_config": output_config}, **base)),
        ]

        response, api_path, errors = None, None, []
        connection_error = getattr(self._anthropic, "APIConnectionError", ())
        for name, call in strategies:
            try:
                response, api_path = call(), name
                break
            except connection_error:
                raise
            except Exception as exc:
                errors.append(f"{name}: {type(exc).__name__}: {exc}")
        if response is None:
            raise RuntimeError("; ".join(errors))

        if getattr(response, "stop_reason", None) == "refusal":
            raise RuntimeError("model declined the extraction request")

        data = _loads_lenient(_first_text(response))
        usage = getattr(response, "usage", None)
        data["_meta"] = {
            "extractor": self.name,
            "model": getattr(response, "model", self.settings.model),
            "effort": self.settings.llm_effort,
            "prompt_sha256": prompt_sha,
            "api_path": api_path,
            "refusal_fallback_enabled": api_path == "beta+refusal_fallback",
            "degraded_api_path": errors or None,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }
        return data
