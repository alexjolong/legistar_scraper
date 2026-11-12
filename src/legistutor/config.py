"""Settings, read from environment variables and from a ``.env`` file beside the data."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CHAT_MODEL = "gemma4:12b"
DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"
# On Databricks a "model" is the name of a model serving endpoint.
DATABRICKS_CHAT_ENDPOINT = "databricks-claude-sonnet-5-5"
DATABRICKS_EMBED_ENDPOINT = "databricks-qwen3-embedding-0-6b"
PROVIDERS = ("ollama", "databricks")


def _provider() -> str:
    """Where the models are. Inside a Databricks cluster or App it is the workspace's
    model serving unless ``LEGISTUTOR_PROVIDER`` says otherwise; anywhere else, Ollama."""
    named = os.environ.get("LEGISTUTOR_PROVIDER", "").strip().lower()
    if named:
        if named not in PROVIDERS:
            raise ValueError(f"LEGISTUTOR_PROVIDER is {named!r}; it must be one of {', '.join(PROVIDERS)}")
        return named
    return "databricks" if os.environ.get("DATABRICKS_RUNTIME_VERSION") or os.environ.get("DATABRICKS_APP_NAME") else "ollama"


def data_dir() -> Path:
    """Root of the data lake: ``LEGISTUTOR_DATA_DIR`` (a Unity Catalog volume path on Databricks), else ``./data``."""
    return Path(os.environ.get("LEGISTUTOR_DATA_DIR", "").strip() or "data")


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
    provider: str  # "ollama" or "databricks"
    chat_model: str  # an Ollama model, or a Databricks serving endpoint
    embed_model: str
    context_tokens: int  # Databricks: how much prompt to send the chat endpoint
    max_tokens: int  # Databricks: longest answer to ask for
    temperature: float | None  # Databricks: None leaves the endpoint's default
    ollama_host: str
    ollama_num_ctx: int  # context window to request from Ollama, in tokens
    ollama_think: bool | None  # reasoning models: think before answering? None = the model's default


def settings() -> Settings:
    env = os.environ.get
    provider = _provider()
    on_databricks = provider == "databricks"
    return Settings(
        provider=provider,
        chat_model=env("LEGISTUTOR_CHAT_MODEL") or (DATABRICKS_CHAT_ENDPOINT if on_databricks else DEFAULT_CHAT_MODEL),
        embed_model=env("LEGISTUTOR_EMBED_MODEL") or (DATABRICKS_EMBED_ENDPOINT if on_databricks else DEFAULT_EMBED_MODEL),
        context_tokens=int(env("LEGISTUTOR_CONTEXT_TOKENS") or 60000),
        max_tokens=int(env("LEGISTUTOR_MAX_TOKENS") or 4000),
        temperature=float(env("LEGISTUTOR_TEMPERATURE")) if env("LEGISTUTOR_TEMPERATURE") else None,
        ollama_host=_ollama_host(),
        ollama_num_ctx=int(env("LEGISTUTOR_OLLAMA_NUM_CTX", "16384")),
        ollama_think=_think(env("LEGISTUTOR_OLLAMA_THINK")),
    )
