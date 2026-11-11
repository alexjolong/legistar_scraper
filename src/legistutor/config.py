"""Settings, read from environment variables and from a ``.env`` file beside the data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHAT_MODEL = "gemma4:12b"
DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"


def _ollama_host() -> str:
    # Ollama's own variable; it may be a bare "host:port".
    host = os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    if "://" not in host:
        host = f"http://{host}"
    return host.replace("//0.0.0.0", "//127.0.0.1").rstrip("/")


def _think(value: str | None) -> bool | None:
    """Off unless asked for: on a 12 GB card, reasoning first multiplied the wait
    for a small gain in accuracy. Empty means the model's own default."""
    if value is None:
        return False
    if not value.strip():
        return None
    return value.strip().lower() in ("1", "true", "yes", "on")


def secret(name: str, *alternatives: str, env_file: Path = Path(".env")) -> str | None:
    """A key such as ``PLURAL_POLICY_API_KEY``: from the environment, else from ``.env``
    in the working directory (which git ignores). Only the names asked for are read."""
    names = (name, *alternatives)
    for candidate in names:
        if os.environ.get(candidate, "").strip():
            return os.environ[candidate].strip()
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.partition("=")
        if separator and key.strip() in names and not line.lstrip().startswith("#"):
            return value.strip().strip("'\"") or None
    return None


def bills_api_key() -> str | None:
    """The Open States API key; Plural Policy is the service's current owner."""
    return secret("PLURAL_POLICY_API_KEY", "OPENSTATES_API_KEY")


@dataclass(frozen=True)
class Settings:
    chat_model: str
    embed_model: str
    ollama_host: str
    ollama_num_ctx: int  # context window to request from Ollama, in tokens
    ollama_think: bool | None  # reasoning models: think before answering? None = the model's default


def settings() -> Settings:
    env = os.environ.get
    return Settings(
        chat_model=env("LEGISTUTOR_CHAT_MODEL", DEFAULT_CHAT_MODEL),
        embed_model=env("LEGISTUTOR_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        ollama_host=_ollama_host(),
        ollama_num_ctx=int(env("LEGISTUTOR_OLLAMA_NUM_CTX", "16384")),
        ollama_think=_think(env("LEGISTUTOR_OLLAMA_THINK")),
    )
