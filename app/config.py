"""Settings. Reads .env from the project root with no third-party dependency.

Real environment variables always win over .env, so a judge can override any
setting inline without editing a file:

    CIMET_SAMPLE_AUDIT_RATE=1.0 python run.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
RUNS_DIR = ROOT / "runs"
WEB_DIR = Path(__file__).resolve().parent / "web"


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Populate os.environ from .env. Existing env vars are never overwritten."""
    path = Path(path) if path else ROOT / ".env"
    loaded: dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Resolved runtime settings. `snapshot()` is safe to write into a run artifact."""

    api_key: str = ""
    model: str = "claude-opus-5"
    llm_mode: str = "auto"  # auto | on | off
    llm_timeout_sec: float = 60.0
    llm_effort: str = "medium"
    port: int = 8787
    host: str = "127.0.0.1"
    open_browser: bool = True
    auto_install: bool = True
    sample_audit_rate: float = 0.05
    sample_audit_salt: str = "cimet-qa-gate-2026"
    confidence_floor_critical: float = 0.6
    preload_lead_id: str = "3613790"
    redact_digit_run: int = 13
    asr_provider: str = "auto"  # auto | elevenlabs | deepgram
    elevenlabs_api_key: str = ""
    deepgram_api_key: str = ""
    asr_model: str = ""
    asr_timeout_sec: float = 300.0
    dialler_token: str = ""
    max_recording_mb: int = 200
    warnings: list[str] = field(default_factory=list)

    @property
    def llm_enabled(self) -> bool:
        if self.llm_mode == "off":
            return False
        if self.llm_mode == "on":
            return True
        return bool(self.api_key)

    def snapshot(self) -> dict:
        """Config as recorded in the audit artifact. Never contains the key itself."""
        return {
            "model": self.model,
            "llm_mode": self.llm_mode,
            "llm_enabled": self.llm_enabled,
            "llm_effort": self.llm_effort,
            "api_key_present": bool(self.api_key),
            "api_key_fingerprint": (self.api_key[:7] + "..." + self.api_key[-4:]) if self.api_key else None,
            "sample_audit_rate": self.sample_audit_rate,
            "sample_audit_salt": self.sample_audit_salt,
            "confidence_floor_critical": self.confidence_floor_critical,
            "redact_digit_run": self.redact_digit_run,
            "asr_provider": self.asr_provider,
            "asr_model": self.asr_model or None,
            "elevenlabs_key_present": bool(self.elevenlabs_api_key),
            "deepgram_key_present": bool(self.deepgram_api_key),
            "dialler_token_required": bool(self.dialler_token),
        }


def load_settings() -> Settings:
    load_dotenv()
    s = Settings(
        api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        model=os.environ.get("CIMET_MODEL", "").strip() or "claude-opus-5",
        llm_mode=(os.environ.get("CIMET_LLM", "").strip().lower() or "auto"),
        llm_timeout_sec=_float("CIMET_LLM_TIMEOUT", 60.0),
        llm_effort=os.environ.get("CIMET_LLM_EFFORT", "").strip() or "medium",
        port=_int("CIMET_PORT", 8787),
        host=os.environ.get("CIMET_HOST", "").strip() or "127.0.0.1",
        open_browser=_bool("CIMET_OPEN_BROWSER", True),
        auto_install=_bool("CIMET_AUTO_INSTALL", True),
        sample_audit_rate=_float("CIMET_SAMPLE_AUDIT_RATE", 0.05),
        sample_audit_salt=os.environ.get("CIMET_SAMPLE_SALT", "").strip() or "cimet-qa-gate-2026",
        confidence_floor_critical=_float("CIMET_CONFIDENCE_FLOOR", 0.6),
        preload_lead_id=os.environ.get("CIMET_PRELOAD_LEAD", "").strip() or "3613790",
        asr_provider=(os.environ.get("CIMET_ASR", "").strip().lower() or "auto"),
        elevenlabs_api_key=os.environ.get("ELEVENLABS_API_KEY", "").strip(),
        deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY", "").strip(),
        asr_model=os.environ.get("CIMET_ASR_MODEL", "").strip(),
        asr_timeout_sec=_float("CIMET_ASR_TIMEOUT", 300.0),
        dialler_token=os.environ.get("CIMET_DIALLER_TOKEN", "").strip(),
        max_recording_mb=_int("CIMET_MAX_RECORDING_MB", 200),
    )
    if s.asr_provider not in {"auto", "elevenlabs", "deepgram"}:
        s.warnings.append(f"CIMET_ASR={s.asr_provider!r} is not auto|elevenlabs|deepgram - falling back to auto")
        s.asr_provider = "auto"
    if s.llm_mode not in {"auto", "on", "off"}:
        s.warnings.append(f"CIMET_LLM={s.llm_mode!r} is not auto|on|off - falling back to auto")
        s.llm_mode = "auto"
    if s.llm_mode == "on" and not s.api_key:
        s.warnings.append("CIMET_LLM=on but ANTHROPIC_API_KEY is empty - Type B will use the deterministic extractor")
    if not 0.0 <= s.sample_audit_rate <= 1.0:
        s.warnings.append(f"CIMET_SAMPLE_AUDIT_RATE={s.sample_audit_rate} out of range - clamped")
        s.sample_audit_rate = min(1.0, max(0.0, s.sample_audit_rate))
    return s
