"""Local HTTP server. Standard library only, so the app runs with one command.

No auth by design: this binds to 127.0.0.1 and reads local fixtures. The only
secret involved is the Anthropic key, which is read from .env and never leaves
the process.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from . import __version__
from .config import WEB_DIR
from .pipeline import Pipeline, PipelineError, _msg
from .store import Store

_RUN_RE = re.compile(r"^/api/runs/([A-Za-z0-9_.\-]+)(/export)?$")


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
            self.run_id = self.pipeline.run_lead(self.lead_id)["run_id"]
        except Exception as exc:  # pragma: no cover - surfaced to the UI
            self.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            self.done.set()

    def wait(self, timeout: float = 120.0) -> None:
        self.done.wait(timeout)


def make_handler(store: Store, pipeline: Pipeline, settings, preloader: _Preloader):
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
                    }
                    for lead in store.leads_doc["leads"]
                ],
                "config": settings.snapshot(),
                "extractors": pipeline.extractor_status(),
                "warnings": settings.warnings,
                "preload_lead_id": settings.preload_lead_id,
                "preload_error": preloader.error,
                "run": preloaded,
            })

        # ---------------------------------------------------------------- POST

        def do_POST(self):  # noqa: N802 - stdlib signature
            route = urlparse(self.path).path
            try:
                body = self._body()
                # The brief names these without the /api prefix; accept both.
                route = route if route.startswith("/api/") else "/api" + route
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


def serve(settings) -> None:
    store = Store()
    pipeline = Pipeline(store, settings)
    preloader = _Preloader(pipeline, settings.preload_lead_id)
    preloader.start()

    handler = make_handler(store, pipeline, settings, preloader)
    httpd = ThreadingHTTPServer((settings.host, settings.port), handler)
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
