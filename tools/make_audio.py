#!/usr/bin/env python3
"""Turn the synthetic call scripts into phone-quality recordings.

    python tools/make_audio.py --dry-run                 characters needed vs. quota left, calls nothing
    python tools/make_audio.py                           all 12 calls via ElevenLabs
    python tools/make_audio.py --only 3613802 3613810    some calls
    python tools/make_audio.py --engine windows          free, offline: Windows built-in voices
    python tools/make_audio.py --list-voices             ElevenLabs voice ids on your account
    python tools/make_audio.py --sync-only               match reference transcripts to existing audio

Two engines. ElevenLabs sounds like people and needs ELEVENLABS_API_KEY; it checks
your remaining character quota first and refuses rather than stopping half way.
`windows` uses the voices built into Windows (System.Speech) - robotic, but free
and unlimited, which is enough to exercise ASR, diarisation and scoring on every
call. Standard library only - no ffmpeg. Each line is synthesised once and cached
under data/synth/audio/.cache, so re-running costs nothing.

Why the post-processing: clean studio TTS is far easier to transcribe than a real
call, so accuracy measured on it would flatter the system. Every file is
band-limited to the telephone band (300-3400 Hz), downsampled to 8 kHz mono, and
given line noise; the call marked degraded gets much more, plus real overlap
between speakers. The script's pauses become real silence, so dead air is audible.

Voices: two speakers per call, picked from ELEVENLABS_VOICES (comma-separated ids)
so agent and customer always differ. Defaults are ElevenLabs premade voices; if
your account does not have them, run --list-voices and set ELEVENLABS_VOICES.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import json
import math
import os
import random
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402

SYNTH = ROOT / "data" / "synth"
AUDIO = SYNTH / "audio"
CACHE = AUDIO / ".cache"
SRC_RATE, OUT_RATE = 16000, 8000

# Default (premade) voices, which free accounts can use over the API; library voices
# need a paid plan. Female / male alternate so a call usually has one of each.
DEFAULT_VOICES = [
    "Xb7hH8MSUJpSbSDYk0k2",  # Alice (British)
    "IKne3meq5aSn9XLyUdCD",  # Charlie (Australian)
    "pFZP5JQG7iQjIQuC4Bku",  # Lily (British)
    "JBFqnCBsd6RMkjVDRZzb",  # George (British)
]
FEMALE_NAMES = {"Priya", "Aisha", "Grace", "Sophie", "Helen", "Nina", "Chloe"}

WINDOWS_PS = r"""
Add-Type -AssemblyName System.Speech
$jobs = Get-Content -Raw -Encoding UTF8 $args[0] | ConvertFrom-Json
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fmt = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000,
  [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
foreach ($j in $jobs) {
  $s.SelectVoice($j.voice)
  $s.SetOutputToWaveFile($j.out, $fmt)
  $s.Speak($j.text)
}
$s.SetOutputToNull()
"""


def api(path: str, key: str, body: dict | None = None) -> bytes:
    req = urllib.request.Request(
        f"https://api.elevenlabs.io{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"xi-api-key": key, "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise SystemExit(f"  ElevenLabs HTTP {exc.code} on {path}: {detail}") from None


def pick_voices(script: dict, pool: list[str]) -> dict[str, str]:
    """Agent and customer get different voices; gender follows the first name where known."""
    females = [v for i, v in enumerate(pool) if i % 2 == 0] or pool
    males = [v for i, v in enumerate(pool) if i % 2 == 1] or pool
    chosen: dict[str, str] = {}
    for role in ("agent", "customer"):
        first = script["voices"][role].split()[0]
        options = females if first in FEMALE_NAMES else males
        seed = int(hashlib.sha1(script["voices"][role].encode()).hexdigest(), 16)
        voice = options[seed % len(options)]
        if voice in chosen.values():
            voice = next((v for v in pool if v not in chosen.values()), voice)
        chosen[role] = voice
    return chosen


def cache_path(text: str, voice: str, model: str) -> Path:
    digest = hashlib.sha256(f"{voice}|{model}|{text}".encode()).hexdigest()[:24]
    return CACHE / f"{digest}.pcm"


def windows_voices() -> list[str]:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer)"
         ".GetInstalledVoices() | % { $_.VoiceInfo.Gender.ToString() + '|' + $_.VoiceInfo.Name }"],
        capture_output=True, text=True, check=True).stdout.split("\n")
    pairs = [line.strip().split("|", 1) for line in out if "|" in line]
    females = [n for g, n in pairs if g == "Female"]
    males = [n for g, n in pairs if g != "Female"]
    females, males = females or males, males or females
    # Interleave female/male so pick_voices' even/odd split still means gender. The
    # shorter list repeats; pick_voices still gives agent and customer different voices.
    pool = []
    for i in range(max(len(females), len(males))):
        pool += [females[i % len(females)], males[i % len(males)]]
    return pool


def windows_prefetch(lines: list[tuple[str, str]]) -> None:
    """Synthesise every uncached (text, voice) in one PowerShell process."""
    todo = [(t, v) for t, v in lines if not cache_path(t, v, "windows-sapi").exists()]
    if not todo:
        return
    with tempfile.TemporaryDirectory() as tmp:
        jobs = [{"text": t, "voice": v, "out": str(Path(tmp) / f"{i}.wav")} for i, (t, v) in enumerate(todo)]
        (Path(tmp) / "jobs.json").write_text(json.dumps(jobs), encoding="utf-8")
        (Path(tmp) / "speak.ps1").write_text(WINDOWS_PS, encoding="utf-8")
        subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(Path(tmp) / "speak.ps1"), str(Path(tmp) / "jobs.json")], check=True)
        for job, (text, voice) in zip(jobs, todo):
            with wave.open(job["out"], "rb") as w:
                cache_path(text, voice, "windows-sapi").write_bytes(w.readframes(w.getnframes()))


def tts(text: str, voice: str, key: str, model: str) -> array.array:
    cached = cache_path(text, voice, model)
    if cached.exists():
        raw = cached.read_bytes()
    elif model == "windows-sapi":
        raise RuntimeError("windows line not prefetched")
    else:
        raw = api(f"/v1/text-to-speech/{voice}?output_format=pcm_{SRC_RATE}", key, {
            "text": text, "model_id": model,
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.75},
        })
        cached.write_bytes(raw)
    pcm = array.array("h")
    pcm.frombytes(raw[: len(raw) - len(raw) % 2])
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm


def phone_line(mix: list[float], snr_db: float, rng: random.Random) -> array.array:
    """300-3400 Hz band, 16k -> 8k, line noise at the given SNR, 16-bit out."""
    # low-pass ~3.4 kHz (two passes of a one-pole filter), then decimate by 2
    a = math.exp(-2 * math.pi * 3400 / SRC_RATE)
    for _ in range(2):
        y = 0.0
        for i, x in enumerate(mix):
            y = (1 - a) * x + a * y
            mix[i] = y
    down = mix[::2]
    # high-pass ~300 Hz
    b = math.exp(-2 * math.pi * 300 / OUT_RATE)
    prev_x = prev_y = 0.0
    for i, x in enumerate(down):
        prev_y = b * (prev_y + x - prev_x)
        prev_x = x
        down[i] = prev_y
    speech = [x for x in down if abs(x) > 200]
    rms = math.sqrt(sum(x * x for x in speech) / len(speech)) if speech else 1000.0
    noise_rms = rms / (10 ** (snr_db / 20))
    hum_amp = noise_rms * 0.5
    out = array.array("h")
    for i, x in enumerate(down):
        v = x + rng.gauss(0, noise_rms) + hum_amp * math.sin(2 * math.pi * 50 * i / OUT_RATE)
        out.append(max(-32768, min(32767, int(v))))
    return out


def render(lead_id: str, key: str, model: str, pool: list[str]) -> dict:
    script = json.loads((SYNTH / "scripts" / f"{lead_id}.json").read_text(encoding="utf-8"))
    lead = next(l for l in json.loads((SYNTH / "leads.json").read_text(encoding="utf-8"))["leads"]
                if l["lead_id"] == lead_id)
    degraded = lead.get("audio_quality") == "degraded"
    voices = pick_voices(script, pool)
    if model == "windows-sapi":
        windows_prefetch([(l["text"], voices[l["speaker"]]) for l in script["lines"]])

    clips, clock, timing = [], 0.0, []
    for i, line in enumerate(script["lines"]):
        pcm = tts(line["text"], voices[line["speaker"]], key, model)
        dur = len(pcm) / SRC_RATE
        start = clock - line["overlap"] if line["overlap"] else clock + line["pause"]
        start = max(0.0, start)
        clips.append((start, pcm, line))
        timing.append({"idx": i, "speaker": line["speaker"], "start_sec": round(start, 2),
                       "end_sec": round(start + dur, 2), "text": line["text"]})
        clock = max(clock, start + dur)

    total = int((clock + 1.0) * SRC_RATE)
    mix = [0.0] * total
    for start, pcm, line in clips:
        gain = 0.8 if (degraded and line["speaker"] == "customer") else 1.0
        offset = int(start * SRC_RATE)
        for j, v in enumerate(pcm):
            if offset + j < total:
                mix[offset + j] += v * gain

    rng = random.Random(int(lead_id))
    out = phone_line(mix, snr_db=9.0 if degraded else 24.0, rng=rng)
    if sys.byteorder == "big":
        out.byteswap()
    path = AUDIO / f"{lead_id}.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(OUT_RATE)
        wav.writeframes(out.tobytes())
    (AUDIO / f"{lead_id}.timing.json").write_text(json.dumps({
        "lead_id": lead_id, "engine": "windows" if model == "windows-sapi" else "elevenlabs",
        "voices": voices, "model": model,
        "snr_db": 9.0 if degraded else 24.0, "sample_rate": OUT_RATE,
        "note": "Ground-truth line timings in the rendered audio - use to measure ASR timestamp drift.",
        "lines": timing,
    }, indent=2), encoding="utf-8")
    sync_transcript(lead_id)
    return {"lead_id": lead_id, "seconds": round(clock, 1), "path": path}


def sync_transcript(lead_id: str) -> bool:
    """Give the reference transcript the rendered audio's real line timings.

    synth_calls.py can only estimate timings from word counts; once audio exists, the
    transcript must match it, or clicking 01:31 would play the audio at a different line.
    """
    timing_path = AUDIO / f"{lead_id}.timing.json"
    transcript_path = SYNTH / "transcripts" / f"{lead_id}.json"
    if not timing_path.exists() or not transcript_path.exists():
        return False
    lines = json.loads(timing_path.read_text(encoding="utf-8"))["lines"]
    doc = json.loads(transcript_path.read_text(encoding="utf-8"))
    if len(lines) != len(doc["turns"]):
        print(f"  {lead_id}: transcript and audio have different line counts - regenerate both")
        return False
    for turn, line in zip(doc["turns"], lines):
        turn["start_sec"], turn["end_sec"] = line["start_sec"], line["end_sec"]
    doc["duration_sec"] = round(lines[-1]["end_sec"] + 1, 1)
    doc["asr_engine"] = "synthetic-reference (line timings taken from the rendered audio, no ASR)"
    transcript_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    return True


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--only", nargs="*", help="lead ids to render (default: all)")
    parser.add_argument("--dry-run", action="store_true", help="print the character count and stop")
    parser.add_argument("--list-voices", action="store_true", help="list voices on your account")
    parser.add_argument("--engine", choices=["elevenlabs", "windows"], default="elevenlabs")
    parser.add_argument("--sync-only", action="store_true",
                        help="copy existing audio timings into the reference transcripts; no audio is made")
    args = parser.parse_args()

    if args.sync_only:
        done = [p.stem.split(".")[0] for p in sorted(AUDIO.glob("*.timing.json"))
                if (not args.only or p.stem.split(".")[0] in args.only) and sync_transcript(p.stem.split(".")[0])]
        print(f"  synced {len(done)} transcript(s) to their audio: {', '.join(done)}")
        return 0

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    model = os.environ.get("ELEVENLABS_TTS_MODEL", "").strip() or "eleven_multilingual_v2"
    pool = [v.strip() for v in os.environ.get("ELEVENLABS_VOICES", "").split(",") if v.strip()] or DEFAULT_VOICES

    if args.list_voices:
        if not key:
            print("  ELEVENLABS_API_KEY is not set (put it in .env)")
            return 2
        for v in json.loads(api("/v1/voices", key))["voices"]:
            labels = ", ".join(f"{k}={v2}" for k, v2 in (v.get("labels") or {}).items())
            print(f"  {v['voice_id']}  {v['name']:<20} {labels}")
        return 0

    scripts = sorted((SYNTH / "scripts").glob("*.json"))
    if not scripts:
        print("  no scripts - run: python tools/synth_calls.py")
        return 2
    leads = [p.stem for p in scripts if not args.only or p.stem in args.only]

    AUDIO.mkdir(parents=True, exist_ok=True)
    CACHE.mkdir(parents=True, exist_ok=True)

    if args.engine == "windows":
        model, pool = "windows-sapi", windows_voices()
        print(f"  {len(leads)} call(s) with Windows voices: {', '.join(pool)}")
        if args.dry_run:
            return 0
    else:
        billable = 0
        for lead_id in leads:
            script = json.loads((SYNTH / "scripts" / f"{lead_id}.json").read_text(encoding="utf-8"))
            voices = pick_voices(script, pool)
            billable += sum(len(l["text"]) for l in script["lines"]
                            if not cache_path(l["text"], voices[l["speaker"]], model).exists())
        remaining = None
        if key:
            sub = json.loads(api("/v1/user/subscription", key))
            remaining = sub["character_limit"] - sub["character_count"]
        print(f"  {len(leads)} call(s), {billable:,} characters still to synthesise"
              + (f", {remaining:,} left on your ElevenLabs plan" if remaining is not None else ""))
        if args.dry_run:
            return 0
        if not key:
            print("  ELEVENLABS_API_KEY is not set (put it in .env), or use --engine windows")
            return 2
        if remaining is not None and billable > remaining:
            print("  not enough quota - nothing was sent. Render fewer calls with --only, "
                  "or use --engine windows")
            return 2

    for lead_id in leads:
        result = render(lead_id, key, model, pool)
        print(f"  {lead_id}  {result['seconds']:>6.1f}s  -> {result['path'].relative_to(ROOT)}")
    print("\n  next: python run.py, then  python tools/dialler_sim.py --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
