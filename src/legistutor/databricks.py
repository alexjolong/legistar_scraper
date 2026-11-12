"""What is particular to running in a Databricks workspace: who we are, and where the files are.

Credentials come from the Databricks SDK, which finds them wherever the code runs: the
notebook's own identity on a cluster, the app's service principal in a Databricks App,
or a CLI profile or ``DATABRICKS_HOST``/``DATABRICKS_TOKEN`` on a laptop. Nothing here
is imported unless the provider is ``databricks``, so a local install does not need the SDK.

A cluster sees a Unity Catalog volume as ordinary files under ``/Volumes``. A Databricks
App does not, and its own disk is emptied when it restarts; :class:`Mirror` copies the
layers the page reads to that disk when the app starts, and copies back what the app writes.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = (429, 503)
SERVED_LAYERS = ("silver", "gold")  # what the page reads; bronze stays in the volume


class DatabricksError(RuntimeError):
    pass


def _sdk_config() -> Any:
    try:
        from databricks.sdk.core import Config
    except ImportError as exc:
        raise DatabricksError(
            "The Databricks SDK is not installed. Run `uv sync --extra databricks` (or `pip install databricks-sdk`)."
        ) from exc
    try:
        return Config()
    except Exception as exc:  # noqa: BLE001 - the SDK raises its own types; the message is what matters
        raise DatabricksError(f"No Databricks credentials found: {exc}") from exc


def _volume_files() -> Any:
    from databricks.sdk import WorkspaceClient

    return WorkspaceClient(config=_sdk_config()).files


class _Auth(httpx.Auth):
    """Adds the workspace's credentials to each request; the SDK renews them as they expire."""

    def __init__(self, sdk_config: Any) -> None:
        self._config = sdk_config

    def auth_flow(self, request: httpx.Request):
        request.headers.update(self._config.authenticate())
        yield request


def serving_client(*, timeout: httpx.Timeout | float = 300.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """An HTTP client for ``<workspace>/serving-endpoints/``. With ``transport`` (tests) nothing is looked up."""
    if transport is not None:
        return httpx.Client(base_url="https://workspace.test/serving-endpoints/", timeout=timeout, transport=transport)
    sdk_config = _sdk_config()
    host = (sdk_config.host or "").rstrip("/")
    if not host:
        raise DatabricksError("No Databricks workspace is configured. Set DATABRICKS_HOST, or run `databricks auth login`.")
    return httpx.Client(base_url=f"{host}/serving-endpoints/", timeout=timeout, auth=_Auth(sdk_config))


def problem(response: httpx.Response, endpoint: str) -> str:
    """Why a serving endpoint refused, in words that say what to do about it."""
    detail = response.text[:300]
    if response.status_code == 404:
        return f"The workspace has no serving endpoint named {endpoint!r}. Check the name under Serving, then set LEGISTUTOR_CHAT_MODEL or LEGISTUTOR_EMBED_MODEL."
    if response.status_code in (401, 403):
        return f"Not allowed to query the serving endpoint {endpoint!r}. Grant CAN QUERY on it to whoever is running this (for an app, its service principal). ({detail})"
    if response.status_code == 429:
        return f"The serving endpoint {endpoint!r} is rate limited and stayed so after several tries. ({detail})"
    return f"Serving endpoint {endpoint!r} error {response.status_code}: {detail}"


def wait_before_retry(response: httpx.Response, attempt: int) -> None:
    try:
        asked = float(response.headers.get("retry-after", ""))
    except ValueError:
        asked = 0.0
    time.sleep(min(max(asked, 2.0**attempt), 60.0))


# --- files in a Unity Catalog volume ---------------------------------------------------


def is_volume_path(path: Path | str) -> bool:
    return Path(path).as_posix().startswith("/Volumes/")


class Mirror:
    """A copy on local disk of part of a volume, for code that cannot see ``/Volumes``.

    ``files`` is the SDK's Files API (``WorkspaceClient().files``).
    """

    def __init__(self, files: Any, volume_root: str, local_root: Path) -> None:
        self.files = files
        self.volume_root = PurePosixPath(volume_root)
        self.local_root = Path(local_root)

    def pull(self, subdirectories: Iterable[str] = SERVED_LAYERS) -> dict[str, int]:
        """Download what is missing or changed under each subdirectory."""
        started = time.monotonic()
        report = {"downloaded": 0, "unchanged": 0, "bytes": 0}
        for name in subdirectories:
            self._pull(self.volume_root / name, report)
        log.info(
            "volume %s -> %s: %s files downloaded (%.0f MB), %s unchanged, %.0fs",
            self.volume_root, self.local_root, report["downloaded"], report["bytes"] / 1e6, report["unchanged"], time.monotonic() - started,
        )  # fmt: skip
        return report

    def _pull(self, directory: PurePosixPath, report: dict[str, int]) -> None:
        try:
            entries = list(self.files.list_directory_contents(str(directory)))
        except Exception as exc:  # noqa: BLE001 - a layer that has not been built yet is not an error here
            log.warning("Cannot list %s (%s); skipping it", directory, exc)
            return
        for entry in entries:
            remote = PurePosixPath(entry.path)
            if entry.is_directory:
                self._pull(remote, report)
                continue
            local = self.local_root / remote.relative_to(self.volume_root)
            modified = (entry.last_modified or 0) / 1000  # the API gives milliseconds
            if local.exists() and local.stat().st_size == entry.file_size and local.stat().st_mtime >= modified:
                report["unchanged"] += 1
                continue
            local.parent.mkdir(parents=True, exist_ok=True)
            temporary = local.with_name(local.name + ".part")
            with self.files.download(str(remote)).contents as source, temporary.open("wb") as target:
                shutil.copyfileobj(source, target)
            temporary.replace(local)
            report["downloaded"] += 1
            report["bytes"] += local.stat().st_size

    def push(self, local: Path) -> None:
        """Copy one local file back to its place in the volume. A failure is logged, not raised:
        the page keeps working from its own copy."""
        remote = self.volume_root / Path(local).relative_to(self.local_root).as_posix()
        try:
            self.files.create_directory(str(remote.parent))
            with Path(local).open("rb") as source:
                self.files.upload(str(remote), source, overwrite=True)
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not save %s to the volume (%s). Does the app have WRITE VOLUME?", remote, exc)


def mirror_if_needed(data_dir: Path) -> tuple[Path, Mirror | None]:
    """The directory to read from, and the mirror behind it if one was needed.

    A volume path that is not on this machine's disk (a Databricks App) is copied to
    temporary storage first. Anything else is used where it is.
    """
    if not is_volume_path(data_dir) or Path(data_dir).exists():
        return Path(data_dir), None
    mirror = Mirror(_volume_files(), Path(data_dir).as_posix(), Path(tempfile.gettempdir()) / "legistutor-data")
    mirror.pull()
    return mirror.local_root, mirror
