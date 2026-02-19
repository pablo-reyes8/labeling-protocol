from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

_SRC_DIR = Path(__file__).resolve().parent / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))


def extract_boxes(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.preview_boxes_from_json")
    return module.extract_boxes(*args, **kwargs)


def print_boxes(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.preview_boxes_from_json")
    return module.print_boxes(*args, **kwargs)


def render_boxes_on_image(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.preview_boxes_from_json")
    return module.render_boxes_on_image(*args, **kwargs)


def show_boxes_from_json(*args: Any, **kwargs: Any):
    module = importlib.import_module("envegecimiento.preview_boxes_from_json")
    return module.show_boxes_from_json(*args, **kwargs)


__all__ = [
    "extract_boxes",
    "print_boxes",
    "render_boxes_on_image",
    "show_boxes_from_json",
]
