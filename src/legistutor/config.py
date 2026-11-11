"""Settings for the embedding and answering layers, read from the environment.

A ``.env`` file in the working directory is loaded first (it never overrides a
variable that is already set), so an API key can live there instead of in the
shell profile. ``.env`` is git-ignored.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_OLLAMA_MODEL = "gemma3:12b"
DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def _ollama_host() -> str:
    # Ollama's own variable; it may be a bare "host:port".
    host = os.environ.get("OLLAMA_HOST", "").strip() or "http://127.0.0.1:11434"
    if "://" not in host:
        host = f"http://{host}"
    return host.replace("//0.0.0.0", "//127.0.0.1").rstrip("/")


@dataclass(frozen=True)
class Settings:
    llm_provider: str  # "anthropic" or "ollama"
    anthropic_model: str
    ollama_model: str
    embed_model: str
    ollama_host: str
    ollama_num_ctx: int  # context window to request from Ollama, in tokens
    effort: str  # Claude effort level: low, medium, high, xhigh, max


def settings() -> Settings:
    load_dotenv()
    env = os.environ.get
    return Settings(
        llm_provider=env("LEGISTAR_LLM_PROVIDER", "anthropic").lower(),
        anthropic_model=env("LEGISTAR_ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        ollama_model=env("LEGISTAR_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL),
        embed_model=env("LEGISTAR_EMBED_MODEL", DEFAULT_EMBED_MODEL),
        ollama_host=_ollama_host(),
        ollama_num_ctx=int(env("LEGISTAR_OLLAMA_NUM_CTX", "16384")),
        effort=env("LEGISTAR_LLM_EFFORT", "medium"),
    )
