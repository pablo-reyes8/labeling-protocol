from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def run_annotation_demo(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.aging_bbox_annotator")
    return module.run_annotation_demo(*args, **kwargs)


def __getattr__(name: str):
    if name in {"RegionSpec", "REGION_SPECS"}:
        module = importlib.import_module("envegecimiento.aging_bbox_annotator")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "REGION_SPECS",
    "RegionSpec",
    "run_annotation_demo",
]
