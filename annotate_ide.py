from __future__ import annotations

import importlib
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from envegecimiento import external_bbox_annotator, run_annotation

# Cambia solo esta ruta y ejecuta este archivo en tu IDE.
# Si NUM_IMAGES > 1 y upload activo, el lote se asigna aleatoriamente con reserva remota.
IMAGE_PATH = r"data"
NUM_IMAGES = 1


def main() -> None:
    importlib.reload(external_bbox_annotator)
    importlib.reload(run_annotation)

    result = run_annotation.run(IMAGE_PATH, num_images=NUM_IMAGES)
    print(result)


if __name__ == "__main__":
    main()
