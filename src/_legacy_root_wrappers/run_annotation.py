from __future__ import annotations

import sys
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from envegecimiento.run_annotation import (  # noqa: E402
    DEFAULT_DRIVE_API_TOKEN,
    DEFAULT_DRIVE_WEBAPP_URL,
    run,
    run_and_upload,
)

__all__ = [
    "DEFAULT_DRIVE_API_TOKEN",
    "DEFAULT_DRIVE_WEBAPP_URL",
    "run",
    "run_and_upload",
]
