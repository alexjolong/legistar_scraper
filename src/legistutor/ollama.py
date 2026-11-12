"""Make sure the local Ollama server is up before work that needs it begins.

After a restart of the machine Ollama is often not running, and a command such as
``refresh`` would otherwise find that out only at its last step. :func:`ensure_running`
is called first: it starts the server if it can, waits for it, and says which models
are missing.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import urlparse

import httpx

log = logging.getLogger(__name__)

START_WAIT_SECONDS = 30.0


class OllamaUnavailable(RuntimeError):
    pass


def _models(http: httpx.Client) -> list[str] | None:
    """Names of the models Ollama has, or None when it does not answer."""
    try:
        response = http.get("/api/tags")
    except httpx.TransportError:
        return None
    if response.status_code != 200:
        return None
    return [model.get("name", "") for model in response.json().get("models", [])]


def _is_local(host: str) -> bool:
    return urlparse(host).hostname in ("127.0.0.1", "localhost", "::1")


def _executable() -> str | None:
    found = shutil.which("ollama")
    if found:
        return found
    # The Windows installer's location, for a shell that started before PATH was updated.
    candidate = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe"
    return str(candidate) if candidate.is_file() else None


def _start(host: str) -> bool:
    """Start ``ollama serve`` on its own, so it outlives this command."""
    executable = _executable()
    if not executable:
        return False
    options: dict = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    try:
        subprocess.Popen([executable, "serve"], env={**os.environ, "OLLAMA_HOST": urlparse(host).netloc}, **options)  # noqa: S603
    except OSError as exc:
        log.warning("Could not start Ollama (%s)", exc)
        return False
    return True


def _has(installed: Sequence[str], model: str) -> bool:
    wanted = model if ":" in model else f"{model}:latest"
    return wanted in installed


def ensure_running(
    host: str,
    models: Sequence[str] = (),
    *,
    transport: httpx.BaseTransport | None = None,
    start: Callable[[str], bool] = _start,
    wait_seconds: float = START_WAIT_SECONDS,
) -> None:
    """Return once Ollama answers at ``host`` and has ``models``; raise :class:`OllamaUnavailable` otherwise."""
    with httpx.Client(base_url=host, timeout=5.0, transport=transport) as http:
        installed = _models(http)
        if installed is None:
            if not _is_local(host):
                raise OllamaUnavailable(f"Cannot reach Ollama at {host}. It is on another machine, so start it there.")
            if not start(host):
                raise OllamaUnavailable(
                    f"Ollama is not running at {host} and the `ollama` program was not found to start it. "
                    "Install it from https://ollama.com, or start it yourself."
                )
            log.info("Ollama was not running; starting it")
            deadline = time.monotonic() + wait_seconds
            while installed is None and time.monotonic() < deadline:
                time.sleep(0.5)
                installed = _models(http)
            if installed is None:
                raise OllamaUnavailable(
                    f"Started Ollama, but it did not answer at {host} within {wait_seconds:.0f} seconds. Try `ollama serve` in another terminal to see why."
                )
    missing = [model for model in models if model and not _has(installed, model)]
    if missing:
        raise OllamaUnavailable(
            f"Ollama does not have {', '.join(missing)}. Run: " + " && ".join(f"ollama pull {model}" for model in missing)
        )
