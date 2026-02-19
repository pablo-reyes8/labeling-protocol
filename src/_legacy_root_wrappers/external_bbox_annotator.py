from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def run_external_annotation(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.external_bbox_annotator")
    return module.run_external_annotation(*args, **kwargs)


def __getattr__(name: str):
    if name in {"ExternalBBoxAnnotator", "RegionSpec", "REGION_SPECS"}:
        module = importlib.import_module("envegecimiento.external_bbox_annotator")
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ExternalBBoxAnnotator",
    "REGION_SPECS",
    "RegionSpec",
    "run_external_annotation",
]
