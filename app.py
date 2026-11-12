"""Entry point for a Databricks App: the same web page as ``legistutor serve``.

The app's source is this repository as cloned, so the package is run from ``src``
rather than installed. ``serve`` takes its port from ``DATABRICKS_APP_PORT`` and its
data from ``LEGISTUTOR_DATA_DIR`` (see ``app.yaml``).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from legistutor.cli import main  # noqa: E402 - after the path is set

if __name__ == "__main__":
    sys.exit(main(["serve"]))
