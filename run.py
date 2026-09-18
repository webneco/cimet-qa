#!/usr/bin/env python3
"""CIMET QA Gate - one command to run everything.

    python run.py                 start the app and open the browser
    python run.py --score 3613792 score one lead in the terminal and exit
    python run.py --selftest      assert the gate behaves as specified and exit

The web app itself is standard library only. The `anthropic` package is needed
only for LLM-backed Type B extraction; without it the app still runs end to end
on the deterministic extractor and says so on screen.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.config import load_settings  # noqa: E402

MIN_PYTHON = (3, 10)


def _ensure_anthropic(settings) -> None:
    """Install the SDK on first run when a key is present. Skip with CIMET_AUTO_INSTALL=0."""
    if not settings.api_key or not settings.auto_install:
        return
    try:
        import anthropic  # noqa: F401
        return
    except ImportError:
        pass
    print("  ANTHROPIC_API_KEY found but the anthropic package is missing.")
    print("  Installing it once (set CIMET_AUTO_INSTALL=0 to skip)...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "anthropic"],
            check=True,
        )
        print("  installed.\n")
    except Exception as exc:
        print(f"  could not install automatically ({exc}).")
        print("  Run: pip install anthropic   - or continue on the deterministic extractor.\n")


def _print_run(run: dict) -> None:
    gate = run["gate"]
    print()
    print(f"  {run['run_id']}")
    print(f"  lead {run['lead_id']}  |  {run['lead']['retailer']}  |  plan {run['lead']['plan_id']}"
          f"  |  audio {run['lead']['audio_quality']}")
    print(f"  extractor: {run['extractors']['type_b_active']}")
    print()
    print(f"  GATE: {gate['display_status']}"
          + (f"   -> {gate['queue']['queue']}" if gate.get("queue") else ""))
    for reason in gate["reasons"]:
        stamp = f"[{reason['timestamp']}] " if reason.get("timestamp") else ""
        print(f"        {stamp}{reason['detail']}")
    print()
    header = f"  {'CHECK':<22} {'TYPE':<5} {'CRIT':<5} {'STATUS':<8} {'CONF':<6} {'AT':<7} EVIDENCE"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for result in run["results"]:
        quote = (result.get("note") or result.get("quote") or "").replace("\n", " ")
        if len(quote) > 58:
            quote = quote[:55] + "..."
        print(f"  {result['check_id']:<22} {result['type']:<5} "
              f"{('yes' if result['critical'] else '-'):<5} {result['outcome_label']:<8} "
              f"{result['confidence']:<6.2f} {result['timestamp']:<7} {quote}")
        for guardrail in result.get("guardrails", []):
            print(f"  {'':22} {'':5} {'':5} guardrail: {guardrail[:96]}")
    print()
    counters = gate["counters"]
    print(f"  weighted score {counters['weighted_score']}%  |  evidence coverage "
          f"{counters['evidence_coverage_pct']}%  |  critical "
          f"{counters['critical_pass']} pass / {counters['critical_fail']} fail / "
          f"{counters['critical_unsure']} unsure")
    print(f"  audit artifact: runs/{run['run_id']}.json")
    print()


def main() -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ required, found {sys.version.split()[0]}")
        return 1

    parser = argparse.ArgumentParser(prog="run.py", description="CIMET QA Gate")
    parser.add_argument("--score", metavar="LEAD_ID", help="score one lead in the terminal and exit")
    parser.add_argument("--selftest", action="store_true", help="assert gate behaviour and exit")
    parser.add_argument("--port", type=int, help="override CIMET_PORT")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser")
    parser.add_argument("--no-llm", action="store_true", help="force the deterministic extractor")
    args = parser.parse_args()

    settings = load_settings()
    if args.port:
        settings.port = args.port
    if args.no_browser:
        settings.open_browser = False
    if args.no_llm:
        settings.llm_mode = "off"

    if args.selftest:
        from selftest import run_selftest

        return run_selftest(settings)

    _ensure_anthropic(settings)

    if args.score:
        from app.pipeline import Pipeline, PipelineError
        from app.store import Store

        pipeline = Pipeline(Store(), settings)
        try:
            _print_run(pipeline.run_lead(args.score))
        except (PipelineError, KeyError) as exc:
            print(f"  error: {exc}")
            return 2
        return 0

    from app.server import serve

    serve(settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
