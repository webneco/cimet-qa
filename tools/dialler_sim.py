#!/usr/bin/env python3
"""Pretend to be the dialler: push a recording to the running app, keyed on Lead ID.

    python tools/dialler_sim.py 3613802                 push data/synth/audio/3613802.wav
    python tools/dialler_sim.py 3613790 --file me.m4a   push any recording for any lead
    python tools/dialler_sim.py --all                   every synthetic call that has audio
    python tools/dialler_sim.py --all --eval            ...then report agreement on ASR transcripts

This goes through the same HTTP webhook a real dialler would call. Nothing is
uploaded by hand and nothing is read from disk by the server on its behalf.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_dotenv  # noqa: E402


def call(url: str, data: bytes | None = None, headers: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"  cannot reach {url}: {exc.reason} - is `python run.py` running?") from None


def push(base: str, lead_id: str, path: Path, token: str) -> dict:
    ctype = mimetypes.guess_type(path.name)[0] or "audio/wav"
    headers = {"Content-Type": ctype}
    if token:
        headers["X-Dialler-Token"] = token
    status, job = call(f"{base}/api/dialler/recording?lead_id={lead_id}&filename={path.name}",
                       path.read_bytes(), headers)
    if status != 202:
        raise SystemExit(f"  {lead_id}: webhook refused ({status}): {job.get('error')}")
    print(f"  {lead_id}: accepted {job['job_id']} ({path.stat().st_size // 1024} KB)", flush=True)
    last = None
    while True:
        _, job = call(f"{base}{job['poll'] if 'poll' in job else '/api/jobs/' + job['job_id']}")
        if job["status"] != last:
            print(f"  {lead_id}:   {job['status']}", flush=True)
            last = job["status"]
        if job["status"] in {"done", "error"}:
            break
        time.sleep(1.5)
    if job["status"] == "done":
        print(f"  {lead_id}: GATE {job['gate']}  ({job['run_id']})")
    else:
        print(f"  {lead_id}: ERROR {job['error']}")
    return job


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Push a call recording to the QA gate like the dialler would.")
    parser.add_argument("lead_id", nargs="?")
    parser.add_argument("--file", help="recording to send (default data/synth/audio/<lead>.wav)")
    parser.add_argument("--all", action="store_true", help="push every synthetic recording")
    parser.add_argument("--eval", action="store_true", help="afterwards, score agreement on ASR transcripts")
    parser.add_argument("--url", default=f"http://127.0.0.1:{os.environ.get('CIMET_PORT', '8787')}")
    args = parser.parse_args()
    token = os.environ.get("CIMET_DIALLER_TOKEN", "").strip()

    if args.all:
        targets = [(p.stem, p) for p in sorted((ROOT / "data" / "synth" / "audio").glob("*.wav"))]
        if not targets:
            print("  no synthetic audio yet - run: python tools/make_audio.py")
            return 2
    elif args.lead_id:
        path = Path(args.file) if args.file else ROOT / "data" / "synth" / "audio" / f"{args.lead_id}.wav"
        if not path.exists():
            print(f"  no recording at {path}")
            return 2
        targets = [(args.lead_id, path)]
    else:
        parser.print_help()
        return 2

    results = [push(args.url, lead_id, path, token) for lead_id, path in targets]
    failed = [r for r in results if r["status"] != "done"]

    if args.eval:
        status, report = call(f"{args.url}/api/eval", json.dumps({"source": "asr"}).encode(),
                              {"Content-Type": "application/json"})
        if status == 200:
            from app.evaluate import print_report
            print_report(report)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
