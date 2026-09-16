"""Environment-driven configuration. Every knob has a sane default except secrets."""
from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value or ""


@dataclass(frozen=True)
class Settings:
    slack_bot_token: str
    slack_app_token: str
    candidates_channel_id: str

    claude_model: str
    claude_effort: str

    paraform_browse_url: str
    paraform_storage_state: str
    paraform_cache_path: str
    paraform_cache_ttl_minutes: int
    paraform_max_detail_pages: int

    max_jobs_to_llm: int
    top_n_matches: int


def _materialise_storage_state(path: str) -> None:
    """Hosts rarely support secret files. If PARAFORM_STORAGE_STATE_B64 is set,
    write it to the configured path at startup so the scraper finds it."""
    encoded = os.environ.get("PARAFORM_STORAGE_STATE_B64", "").strip()
    if not encoded:
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(encoded))
    try:
        target.chmod(0o600)
    except OSError:
        pass


def load_settings() -> Settings:
    storage_state = _env("PARAFORM_STORAGE_STATE", "./paraform_state.json")
    _materialise_storage_state(storage_state)
    return Settings(
        slack_bot_token=_env("SLACK_BOT_TOKEN", required=True),
        slack_app_token=_env("SLACK_APP_TOKEN", required=True),
        candidates_channel_id=_env("CANDIDATES_CHANNEL_ID", ""),
        claude_model=_env("CLAUDE_MODEL", "claude-opus-5"),
        claude_effort=_env("CLAUDE_EFFORT", "high"),
        paraform_browse_url=_env("PARAFORM_BROWSE_URL", "https://www.paraform.com/browse"),
        paraform_storage_state=storage_state,
        paraform_cache_path=_env("PARAFORM_CACHE_PATH", "./paraform_jobs_cache.json"),
        paraform_cache_ttl_minutes=int(_env("PARAFORM_CACHE_TTL_MINUTES", "30")),
        paraform_max_detail_pages=int(_env("PARAFORM_MAX_DETAIL_PAGES", "0")),
        max_jobs_to_llm=int(_env("MAX_JOBS_TO_LLM", "60")),
        top_n_matches=int(_env("TOP_N_MATCHES", "2")),
    )
