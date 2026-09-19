"""Local HTTP server. Standard library only, so the app runs with one command.

No auth by design: this binds to 127.0.0.1 and reads local fixtures. The only
secret involved is the Anthropic key, which is read from .env and never leaves
the process.
"""

from __future__ import annotations

import base64
import hmac
import json
import mimetypes
import os
import re
import socket
import threading
import traceback
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, asr
from .config import RUNS_DIR, WEB_DIR
from .evaluate import evaluate
from .pipeline import Pipeline, PipelineError, _msg
from .store import AUDIO_TYPES, Store, utcnow

_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9_.\-]+)(/export)?$")
_ID = r"([A-Za-z0-9_.\-]+)"
_AUDIO_RE = re.compile(rf"^/api/audio/{_ID}$")
_LATEST_RE = re.compile(rf"^/api/leads/{_ID}/latest$")
_JOB_RE = re.compile(rf"^/api/jobs/{_ID}$")
_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class DiallerJobs:
    """Recordings pushed by the dialler, processed off the request thread.

    The webhook answers 202 straight away - a dialler must never wait on ASR - and
    the job moves received -> transcribing -> scoring -> done | error. Every
    transition is appended to runs/dialler_log.jsonl.
    """

    def __init__(self, pipeline: Pipeline) -> None:
        self.pipeline = pipeline
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dialler")

    def submit(self, lead_id: str, fetch, filename: str, content_type: str, meta: dict) -> dict:
        self.pipeline.store.lead(lead_id)  # unknown lead -> KeyError -> 404, before accepting
        job = {"job_id": f"JOB-{lead_id}-{uuid.uuid4().hex[:6]}", "lead_id": lead_id,
               "status": "received", "received_at": utcnow(), "filename": filename,
               "transport": meta.get("transport"), "run_id": None, "gate": None, "error": None}
        with self.lock:
            self.jobs[job["job_id"]] = job
        self._log(job)
        self.pool.submit(self._work, job, fetch, filename, content_type, meta)
        return dict(job)

    def _set(self, job: dict, **changes) -> None:
        with self.lock:
            job.update(changes)
        self._log(job)

    def _work(self, job, fetch, filename, content_type, meta) -> None:
        try:
            audio = fetch()
            run = self.pipeline.ingest_recording(
                job["lead_id"], audio, filename, content_type, meta,
                on_stage=lambda stage: self._set(job, status=stage),
            )
            self._set(job, status="done", run_id=run["run_id"], gate=run["gate"]["display_status"],
                      finished_at=utcnow())
        except Exception as exc:
            error = _msg(exc) if isinstance(exc, PipelineError) else f"{type(exc).__name__}: {exc}"
            self._set(job, status="error", error=error, finished_at=utcnow())

    def _log(self, job: dict) -> None:
        with self.lock, (RUNS_DIR / "dialler_log.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": utcnow(), **job}, ensure_ascii=False) + "\n")

    def get(self, job_id: str) -> dict:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(f"unknown job_id {job_id!r}")
            return dict(self.jobs[job_id])

    def recent(self, limit: int = 30) -> list[dict]:
        with self.lock:
            return [dict(j) for j in list(self.jobs.values())[-limit:]][::-1]


def _download(url: str, limit_bytes: int, timeout: float) -> bytes:
    if urlparse(url).scheme not in {"http", "https"}:
        raise PipelineError("recording_url must be http(s)")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read(limit_bytes + 1)
    if len(data) > limit_bytes:
        raise PipelineError(f"recording is larger than {limit_bytes // (1024 * 1024)} MB")
    return data


class _Preloader:
    """Scores the demo lead at startup so the first screen is instant."""

    def __init__(self, pipeline: Pipeline, lead_id: str) -> None:
        self.pipeline = pipeline
        self.lead_id = lead_id
        self.run_id: str | None = None
        self.error: str | None = None
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._work, name="preload", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _work(self) -> None:
        try:
            # A saved result is shown as-is; scoring (and any LLM cost) only happens
            # when there is none yet, or when someone presses Re-run.
            saved = self.pipeline.store.latest_run_for(self.lead_id)
            self.run_id = saved["run_id"] if saved else self.pipeline.run_lead(self.lead_id)["run_id"]
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            self.done.set()

    def wait(self, timeout: float = 120.0) -> None:
        self.done.wait(timeout)


class _Prescorer:
    """Scores, once, every lead that has never been scored, so the lead picker can show
    a verdict for each without anyone opening it. Runs are saved, so later startups
    reuse them and nothing is scored twice. Off with CIMET_PRESCORE=0."""

    def __init__(self, pipeline: Pipeline, after: threading.Event) -> None:
        self.pipeline = pipeline
        self.after = after
        self.pending: list[str] = []
        self.thread = threading.Thread(target=self._work, name="prescore", daemon=True)

    def start(self) -> None:
        store = self.pipeline.store
        self.pending = [lead["lead_id"] for lead in store.leads_doc["leads"]
                        if store.latest_run_for(lead["lead_id"]) is None]
        if self.pending:
            print(f"  Scoring {len(self.pending)} unscored lead(s) in the background for the lead picker")
            self.thread.start()

    def _work(self) -> None:
        self.after.wait(300)  # let the first screen load first
        for lead_id in list(self.pending):
            try:
                self.pipeline.run_lead(lead_id)
            except Exception as exc:  # one bad lead must not stop the rest
                print(f"  prescore {lead_id} failed: {type(exc).__name__}: {exc}")
            finally:
                self.pending.remove(lead_id)


def make_handler(store: Store, pipeline: Pipeline, settings, preloader: _Preloader, jobs: DiallerJobs):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"CIMET-QA-Gate/{__version__}"
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ plumbing

        def log_message(self, fmt, *args):  # noqa: A003 - stdlib signature
            if "/api/" in str(args[0] if args else ""):
                print(f"  {self.address_string()} {fmt % args}")

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload, status: int = 200, extra: dict | None = None):
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", extra)

        def _error(self, status: int, message: str):
            self._json({"error": message, "status": status}, status)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise PipelineError(f"request body is not valid JSON: {exc}") from None
            if not isinstance(payload, dict):
                raise PipelineError("request body must be a JSON object")
            return payload

        def _static(self, name: str):
            path = (WEB_DIR / name).resolve()
            if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
                return self._error(404, f"no such asset {name!r}")
            ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in {"application/javascript"}:
                ctype += "; charset=utf-8"
            self._send(200, path.read_bytes(), ctype)

        # ----------------------------------------------------------------- GET

        def do_GET(self):  # noqa: N802 - stdlib signature
            route = urlparse(self.path).path
            try:
                if route in {"/", "/index.html"}:
                    return self._static("index.html")
                if route.startswith("/static/"):
                    return self._static(route[len("/static/"):])
                if route == "/api/health":
                    return self._json({
                        "ok": True,
                        "version": __version__,
                        "extractors": pipeline.extractor_status(),
                    })
                if route == "/api/bootstrap":
                    return self._bootstrap()
                if route == "/api/override-log":
                    return self._json({"entries": store.read_override_log()})
                if route == "/api/leads/status":
                    return self._json({"leads": {
                        lead["lead_id"]: self._last_gate(lead["lead_id"]) for lead in store.leads_doc["leads"]
                    }})
                if route == "/api/jobs":
                    return self._json({"jobs": jobs.recent()})
                if route == "/api/eval":
                    source = (parse_qs(urlparse(self.path).query).get("source") or ["fixture"])[0]
                    source = source if source in {"fixture", "asr"} else "fixture"
                    path = RUNS_DIR / f"eval_report_{source}.json"
                    if not path.exists():
                        return self._error(404, "no eval report yet - run one")
                    return self._send(200, path.read_bytes(), "application/json; charset=utf-8")
                match = _JOB_RE.match(route)
                if match:
                    try:
                        return self._json(jobs.get(match.group(1)))
                    except KeyError as exc:
                        return self._error(404, _msg(exc))
                match = _AUDIO_RE.match(route)
                if match:
                    return self._audio(match.group(1))
                match = _LATEST_RE.match(route)
                if match:
                    try:
                        return self._json({"run": store.latest_run_for(match.group(1))})
                    except KeyError as exc:
                        return self._error(404, _msg(exc))
                match = _RUN_RE.match(route)
                if match:
                    run_id, export = match.group(1), match.group(2)
                    try:
                        run = store.get_run(run_id)
                    except KeyError as exc:
                        return self._error(404, _msg(exc))
                    if export:
                        body = json.dumps(run, indent=2, ensure_ascii=False).encode("utf-8")
                        return self._send(
                            200, body, "application/json; charset=utf-8",
                            {"Content-Disposition": f'attachment; filename="{run_id}.json"'},
                        )
                    return self._json(run)
                return self._error(404, f"no route for GET {route}")
            except Exception as exc:  # pragma: no cover
                traceback.print_exc()
                return self._error(500, f"{type(exc).__name__}: {exc}")

        def _audio(self, lead_id: str):
            """Stream the recording with Range support, so the player can seek to 14:02."""
            try:
                path = store.audio_for(lead_id)
            except KeyError as exc:
                return self._error(404, _msg(exc))
            if path is None:
                return self._error(404, f"no recording for lead {lead_id}")
            data = path.read_bytes()
            ctype = AUDIO_TYPES.get(path.suffix.lower(), "application/octet-stream")
            match = _RANGE_RE.match(self.headers.get("Range") or "")
            if not match or not (match.group(1) or match.group(2)):
                return self._send(200, data, ctype, {"Accept-Ranges": "bytes"})
            first, last = match.group(1), match.group(2)
            if first:
                start, end = int(first), int(last) if last else len(data) - 1
            else:
                start, end = max(0, len(data) - int(last)), len(data) - 1
            end = min(end, len(data) - 1)
            if start > end:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{len(data)}"})
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            self.wfile.write(data[start:end + 1])

        def _dialler(self):
            """POST /api/dialler/recording - the dialler pushes a recording keyed on Lead ID.

            Accepted forms:
              raw audio body   Content-Type audio/*, ?lead_id=...&filename=...&call_id=...
              JSON             {"lead_id", "recording_url"}             fetched server-side
              JSON             {"lead_id", "audio_base64", "filename"}
            Answers 202 with a job id at once; poll GET /api/jobs/<job_id>.
            """
            if settings.dialler_token and not hmac.compare_digest(
                    self.headers.get("X-Dialler-Token", ""), settings.dialler_token):
                return self._error(401, "missing or wrong X-Dialler-Token")
            provider, reason = asr.provider_for(settings)
            if provider is None:
                return self._error(503, f"speech-to-text unavailable: {reason}")
            limit = settings.max_recording_mb * 1024 * 1024
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            meta = {"received_from": "dialler_webhook"}

            if ctype.startswith("audio/") or ctype == "application/octet-stream":
                length = int(self.headers.get("Content-Length") or 0)
                if length > limit:
                    return self._error(413, f"recording is larger than {settings.max_recording_mb} MB")
                audio = self.rfile.read(length)
                lead_id = query.get("lead_id", "")
                filename = query.get("filename") or f"{lead_id}.wav"
                meta.update(call_id=query.get("call_id"), transport="raw_body")

                def fetch():
                    return audio
            else:
                body = self._body()
                lead_id = str(body.get("lead_id", ""))
                meta.update(call_id=body.get("call_id"), recorded_at=body.get("recorded_at"))
                if body.get("recording_url"):
                    url = str(body["recording_url"])
                    filename = body.get("filename") or urlparse(url).path.rsplit("/", 1)[-1] or f"{lead_id}.wav"
                    meta.update(transport="recording_url")

                    def fetch():
                        return _download(url, limit, settings.asr_timeout_sec)
                elif body.get("audio_base64"):
                    audio = base64.b64decode(body["audio_base64"])
                    filename = body.get("filename") or f"{lead_id}.wav"
                    meta.update(transport="base64")

                    def fetch():
                        return audio
                else:
                    raise PipelineError("send the audio as the body, or JSON with recording_url or audio_base64")
            if not lead_id:
                raise PipelineError("lead_id is required")
            if not ctype.startswith("audio/"):
                ctype = mimetypes.guess_type(filename)[0] or "audio/wav"
            try:
                job = jobs.submit(lead_id, fetch, filename, ctype, meta)
            except KeyError as exc:
                return self._error(404, _msg(exc))
            return self._json({**job, "poll": f"/api/jobs/{job['job_id']}"}, 202)

        def _last_gate(self, lead_id: str):
            """Gate of the newest scored run for a lead, so the picker can show it."""
            try:
                run = store.latest_run_for(lead_id)
            except Exception:
                return None
            return run["gate"]["status"] if run else None

        def _bootstrap(self):
            preloader.wait(timeout=settings.llm_timeout_sec * 2 + 20)
            preloaded = None
            if preloader.run_id:
                try:
                    preloaded = store.get_run(preloader.run_id)
                except KeyError:
                    preloaded = None
            return self._json({
                "version": __version__,
                "retailer": store.checks_doc["retailer"],
                "checklist": {
                    "checklist_id": store.checks_doc["checklist_id"],
                    "checklist_version": store.checks_doc["checklist_version"],
                    "effective_from": store.checks_doc["effective_from"],
                    "type_legend": store.checks_doc["type_legend"],
                    "checks": [
                        {
                            "check_id": c["check_id"], "name": c["name"], "section": c["section"],
                            "type": c["type"], "critical": c["critical"], "weight": c["weight"],
                            "check_version": c["check_version"], "effective_from": c["effective_from"],
                        }
                        for c in store.checks
                    ],
                },
                "leads": [
                    {
                        "lead_id": lead["lead_id"], "retailer": lead["retailer"],
                        "plan_id": lead["plan_id"], "agent_name": lead.get("agent_name"),
                        "audio_quality": lead.get("audio_quality"),
                        "account_holder": lead["crm"]["account_holder_name"],
                        "demo_note": lead.get("demo_note"),
                        "synthetic": bool(lead.get("synthetic")),
                        "vertical": lead.get("vertical", "energy"),
                        "has_audio": store.audio_for(lead["lead_id"]) is not None,
                        "has_asr_transcript": store.asr_transcript_path(lead["lead_id"]).exists(),
                        "last_gate": self._last_gate(lead["lead_id"]),
                    }
                    for lead in store.leads_doc["leads"]
                ],
                "config": settings.snapshot(),
                "extractors": pipeline.extractor_status(),
                "asr": dict(zip(("provider", "unavailable_reason"), asr.provider_for(settings))),
                "warnings": settings.warnings,
                "preload_lead_id": settings.preload_lead_id,
                "preload_error": preloader.error,
                "run": preloaded,
            })

        # ---------------------------------------------------------------- POST

        def do_POST(self):  # noqa: N802 - stdlib signature
            route = urlparse(self.path).path
            try:
                # The brief names these without the /api prefix; accept both.
                route = route if route.startswith("/api/") else "/api" + route
                if route == "/api/dialler/recording":
                    return self._dialler()
                body = self._body()
                if route == "/api/eval":
                    source = body.get("source", "fixture")
                    if source not in {"fixture", "asr"}:
                        raise PipelineError("source must be 'fixture' or 'asr'")
                    return self._json(evaluate(pipeline, source))
                if route == "/api/ingest":
                    return self._json(pipeline.ingest(body.get("lead_id", "")))
                if route == "/api/score":
                    return self._json(pipeline.score(body.get("ingest_id", "")))
                if route == "/api/run":
                    return self._json(pipeline.run_lead(body.get("lead_id", "")))
                if route == "/api/override":
                    return self._json(pipeline.override(
                        body.get("run_id", ""), body.get("check_id", ""),
                        body.get("to_status", ""), body.get("reason", ""),
                        body.get("actor", ""),
                    ))
                return self._error(404, f"no route for POST {route}")
            except PipelineError as exc:
                return self._error(422, str(exc))
            except FileNotFoundError as exc:
                return self._error(404, str(exc))
            except Exception as exc:  # pragma: no cover
                traceback.print_exc()
                return self._error(500, f"{type(exc).__name__}: {exc}")

    return Handler


class _Server(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second process bind a port already in use, so
    an old copy of the app keeps answering and the new one is silently ignored.
    Refuse if anything is listening, and hold the port exclusively once bound."""

    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        try:
            socket.create_connection(self.server_address[:2], timeout=0.5).close()
        except OSError:
            pass
        else:
            raise OSError(98, "something is already listening on this port")
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(settings) -> None:
    store = Store()
    pipeline = Pipeline(store, settings)
    preloader = _Preloader(pipeline, settings.preload_lead_id)
    preloader.start()
    if settings.prescore:
        _Prescorer(pipeline, preloader.done).start()

    jobs = DiallerJobs(pipeline)
    handler = make_handler(store, pipeline, settings, preloader, jobs)
    try:
        httpd = _Server((settings.host, settings.port), handler)
    except OSError as exc:
        print(f"\n  cannot listen on {settings.host}:{settings.port} - {exc.strerror or exc}.")
        print("  Another copy of the app is probably running. Stop it, or use --port.\n")
        raise SystemExit(1) from None
    httpd.daemon_threads = True

    url = f"http://{settings.host}:{settings.port}/"
    status = pipeline.extractor_status()
    print()
    print("  CIMET QA Gate")
    print(f"  {url}")
    print(f"  Retailer 1 checklist {store.checks_doc['checklist_version']} "
          f"| {len(store.checks)} checks | preloading lead {settings.preload_lead_id}")
    print(f"  Type A: {status['type_a']}")
    print(f"  Type B: {status['type_b_active']}"
          + (f"  ({status['llm_unavailable_reason']})" if not status["llm_available"] else ""))
    asr_provider, asr_reason = asr.provider_for(settings)
    print(f"  ASR:    {asr_provider or 'off'}" + (f"  ({asr_reason})" if asr_reason else ""))
    print(f"  Dialler webhook: POST {url}api/dialler/recording")
    for warning in settings.warnings:
        print(f"  ! {warning}")
    print("  Ctrl-C to stop")
    print()

    if settings.open_browser:
        threading.Timer(0.6, _open_browser, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
    finally:
        httpd.server_close()


def _open_browser(url: str) -> None:  # pragma: no cover - convenience only
    try:
        import webbrowser

        webbrowser.open(url)
    except Exception:
        pass
