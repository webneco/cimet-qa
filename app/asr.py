"""Speech-to-text for dialler recordings. Standard library only.

Two providers, both asked for speaker separation and word/utterance timestamps:

  elevenlabs   Scribe  - POST /v1/speech-to-text, diarize + word timestamps
  deepgram     Nova    - POST /v1/listen, diarize + utterances

Whichever runs, the output is the same diarised-turn shape as the fixtures, so the
scorer cannot tell - and does not need to know - where a transcript came from. The
engine, the model and how speakers were mapped to agent/customer are recorded on
the transcript so every score can be traced back to them.

Two things this module will not do: guess words the provider did not return, and
pretend to have transcribed when there is no key. No key is an error, not a fixture.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
import uuid

# A pause longer than this inside one speaker's words starts a new turn, so dead air
# stays visible to the Type C check instead of disappearing inside a long turn.
TURN_SPLIT_GAP_SEC = 1.5

# Phrases an agent says and a customer almost never does. Used to decide which
# diarised speaker is the agent. Recorded, never silently assumed.
AGENT_CUES = [
    "call is being recorded", "consent", "retailer one", "account holder", "default market offer",
    "reference price", "kilowatt hour", "cooling-off", "cooling off", "nmi", "welcome pack",
    "supply charge", "authorised to make changes", "life support", "concession",
]


class AsrError(RuntimeError):
    """ASR could not produce a transcript. The recording is kept; nothing is scored."""


def provider_for(settings) -> tuple[str | None, str | None]:
    """(provider, reason-if-none) according to CIMET_ASR and the keys present."""
    wanted = settings.asr_provider
    have = {"elevenlabs": bool(settings.elevenlabs_api_key), "deepgram": bool(settings.deepgram_api_key)}
    if wanted in have:
        return (wanted, None) if have[wanted] else (None, f"CIMET_ASR={wanted} but its API key is not set")
    for name in ("elevenlabs", "deepgram"):
        if have[name]:
            return name, None
    return None, "no ELEVENLABS_API_KEY or DEEPGRAM_API_KEY configured"


def transcribe(audio: bytes, filename: str, content_type: str, settings) -> dict:
    provider, reason = provider_for(settings)
    if provider is None:
        raise AsrError(f"speech-to-text unavailable: {reason}")
    if provider == "elevenlabs":
        turns, meta = _elevenlabs(audio, filename, content_type, settings)
    else:
        turns, meta = _deepgram(audio, content_type, settings)
    if not turns:
        raise AsrError(f"{provider} returned no speech for {filename}")
    turns, mapping = assign_roles(turns)
    return {"turns": turns, "speaker_mapping": mapping, **meta}


# --------------------------------------------------------------------- providers

def _post(url: str, body: bytes, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        raise AsrError(f"ASR request failed: HTTP {exc.code} {detail}") from None
    except urllib.error.URLError as exc:
        raise AsrError(f"ASR request failed: {exc.reason}") from None


def _multipart(fields: dict, file_field: str, filename: str, content_type: str, data: bytes):
    boundary = f"----cimet{uuid.uuid4().hex}"
    parts = []
    for key, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n".encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _elevenlabs(audio: bytes, filename: str, content_type: str, settings):
    model = settings.asr_model or "scribe_v1"
    body, ctype = _multipart(
        {"model_id": model, "diarize": "true", "num_speakers": "2",
         "timestamps_granularity": "word", "tag_audio_events": "false"},
        "file", filename, content_type, audio,
    )
    data = _post("https://api.elevenlabs.io/v1/speech-to-text", body,
                 {"xi-api-key": settings.elevenlabs_api_key, "Content-Type": ctype},
                 settings.asr_timeout_sec)

    turns: list[dict] = []
    current = None
    for w in data.get("words", []):
        kind = w.get("type", "word")
        if kind == "audio_event":
            continue
        if kind == "spacing":
            if current is not None:
                current["_text"].append(w.get("text", " "))
            continue
        speaker = str(w.get("speaker_id") or "speaker_0")
        start, end = float(w.get("start") or 0), float(w.get("end") or 0)
        new_turn = (current is None or speaker != current["speaker"]
                    or start - current["end_sec"] > TURN_SPLIT_GAP_SEC)
        if new_turn:
            current = {"speaker": speaker, "start_sec": start, "end_sec": end, "_text": [], "_lp": []}
            turns.append(current)
        current["_text"].append(w.get("text", ""))
        current["end_sec"] = end
        if w.get("logprob") is not None:
            current["_lp"].append(float(w["logprob"]))

    for t in turns:
        t["text"] = re.sub(r"\s+", " ", "".join(t.pop("_text"))).strip()
        lps = t.pop("_lp")
        # Mean per-word probability. Scribe gives log-probs; turn them back into 0..1.
        t["asr_confidence"] = round(sum(math.exp(lp) for lp in lps) / len(lps), 3) if lps else 0.9
    meta = {"asr_engine": f"elevenlabs:{model}", "language": data.get("language_code")}
    return [t for t in turns if t["text"]], meta


def _deepgram(audio: bytes, content_type: str, settings):
    model = settings.asr_model or "nova-3"
    url = (f"https://api.deepgram.com/v1/listen?model={model}&diarize=true&punctuate=true"
           f"&smart_format=true&utterances=true&utt_split={TURN_SPLIT_GAP_SEC}")
    data = _post(url, audio, {"Authorization": f"Token {settings.deepgram_api_key}",
                              "Content-Type": content_type}, settings.asr_timeout_sec)
    turns = [
        {"speaker": f"speaker_{u.get('speaker', 0)}", "start_sec": float(u["start"]),
         "end_sec": float(u["end"]), "text": u.get("transcript", "").strip(),
         "asr_confidence": round(float(u.get("confidence", 0.9)), 3)}
        for u in (data.get("results") or {}).get("utterances", [])
    ]
    return [t for t in turns if t["text"]], {"asr_engine": f"deepgram:{model}", "language": None}


# -------------------------------------------------------------------- roles

def assign_roles(turns: list[dict]) -> tuple[list[dict], dict]:
    """Decide which diarised speaker is the agent. Everything else is the customer."""
    scores: dict[str, int] = {}
    for t in turns:
        text = t["text"].lower()
        scores[t["speaker"]] = scores.get(t["speaker"], 0) + sum(cue in text for cue in AGENT_CUES)
    first = turns[0]["speaker"]
    best = max(scores.values())
    leaders = [s for s, v in scores.items() if v == best]
    agent = first if (best == 0 or first in leaders) else leaders[0]
    method = "agent_cue_phrases" if best > 0 else "first_speaker_fallback"
    runner_up = max([v for s, v in scores.items() if s != agent], default=0)

    out = []
    for i, t in enumerate(sorted(turns, key=lambda x: x["start_sec"])):
        role = "agent" if t["speaker"] == agent else "customer"
        out.append({"idx": i, "speaker": role, "speaker_name": f"{role.title()} ({t['speaker']})",
                    "text": t["text"], "start_sec": round(t["start_sec"], 2),
                    "end_sec": round(t["end_sec"], 2), "asr_confidence": t["asr_confidence"],
                    "diarised_speaker": t["speaker"]})
    mapping = {"method": method, "agent_speaker": agent, "cue_scores": scores,
               "margin": best - runner_up,
               "note": "Agent identified by compliance phrases only an agent says; low margin means check it."}
    return out, mapping
