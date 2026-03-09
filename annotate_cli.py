from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from envegecimiento.run_annotation import run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Anotador por consola (acepta imagen local, carpeta local, 'drive', ID o URL de carpeta de Drive)"
    )
    parser.add_argument("image", type=str, help="Ruta local, 'drive', ID o URL de carpeta de Drive")
    parser.add_argument("--output", type=str, default=None, help="Ruta de salida del JSON local")
    parser.add_argument("--reference-dir", type=str, default=None, help="Carpeta de referencias")
    parser.add_argument("--no-upload", action="store_true", help="No subir JSON a Drive")
    parser.add_argument("--count", type=int, default=1, help="Cuantas imagenes consecutivas etiquetar")
    args = parser.parse_args()

    result = run(
        image_path=args.image,
        num_images=args.count,
        output_json_path=args.output,
        reference_dir=args.reference_dir,
        upload_to_drive=not args.no_upload,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
