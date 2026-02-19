from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Any

from PIL import Image, ImageDraw, ImageFont


COLORS = [
    "#ef4444",
    "#22c55e",
    "#3b82f6",
    "#eab308",
    "#f97316",
    "#06b6d4",
    "#a855f7",
    "#ec4899",
    "#84cc16",
    "#14b8a6",
    "#f43f5e",
    "#6366f1",
]


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _to_wsl_path_if_windows(raw_path: str) -> Path | None:
    if not re.match(r"^[a-zA-Z]:[\\\\/]", raw_path):
        return None

    win_path = PureWindowsPath(raw_path)
    drive = win_path.drive.rstrip(":").lower()
    if not drive:
        return None

    return Path("/mnt") / drive / Path(*win_path.parts[1:])


def _resolve_image_path_from_raw(json_path: Path, image_path_raw: str) -> Path | None:
    raw = image_path_raw.strip()
    if not raw:
        return None

    candidates: list[Path] = []

    default_path = Path(raw).expanduser()
    candidates.append(default_path)

    wsl_path = _to_wsl_path_if_windows(raw)
    if wsl_path is not None:
        candidates.append(wsl_path)

    for candidate in candidates:
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if resolved.exists():
                return resolved

    for candidate in candidates:
        rel_candidate = (json_path.parent / candidate).resolve()
        if rel_candidate.exists():
            return rel_candidate

    return None


def _resolve_image_path_from_name(json_path: Path, image_name_raw: str) -> Path | None:
    image_name = image_name_raw.strip()
    if not image_name:
        return None

    image_name_path = Path(image_name)
    repo_root = json_path.parent.parent
    candidate_dirs = [
        json_path.parent,
        repo_root,
        repo_root / "data",
        repo_root / "Foto Original",
        Path.cwd(),
    ]

    for folder in candidate_dirs:
        candidate = (folder / image_name_path).resolve()
        if candidate.exists():
            return candidate

    return None


def _resolve_image_path(json_path: Path, payload: dict[str, Any]) -> Path:
    image_path_raw = str(payload.get("image_path", ""))
    resolved_from_raw = _resolve_image_path_from_raw(json_path, image_path_raw)
    if resolved_from_raw is not None:
        return resolved_from_raw

    image_meta = payload.get("image_meta", {})
    if not isinstance(image_meta, dict):
        image_meta = {}

    image_name = str(payload.get("image_id", "")).strip() or str(image_meta.get("filename", "")).strip()
    resolved_from_name = _resolve_image_path_from_name(json_path, image_name)
    if resolved_from_name is not None:
        return resolved_from_name

    if image_path_raw.strip():
        raise FileNotFoundError(f"No se encontro la imagen del JSON: {image_path_raw}")

    if image_name:
        raise FileNotFoundError(
            "No se encontro la imagen usando image_id/image_meta.filename. "
            f"Valor buscado: {image_name}"
        )

    raise FileNotFoundError("El JSON no trae image_path ni image_id/image_meta.filename.")


def _slots_in_order(payload: dict[str, Any]) -> list[dict[str, Any]]:
    all_slots = payload.get("all_slots")
    if isinstance(all_slots, list) and all_slots:
        return all_slots

    ordered: list[tuple[int, int, dict[str, Any]]] = []
    regions = payload.get("regions", {})
    if not isinstance(regions, dict):
        return []

    for region_key, region_payload in regions.items():
        if not isinstance(region_payload, dict):
            continue

        label_id = int(region_payload.get("label_id", 999))
        region_name = str(region_payload.get("region_name", region_key))
        region_alias = str(region_payload.get("region_alias", region_name))
        slots = region_payload.get("slots", [])
        if not isinstance(slots, list):
            continue

        for slot in slots:
            if not isinstance(slot, dict):
                continue
            merged = {
                "label_id": label_id,
                "region_key": region_key,
                "region_name": region_name,
                "region_alias": region_alias,
                **slot,
            }
            box_index = int(merged.get("box_index", 999))
            ordered.append((label_id, box_index, merged))

    ordered.sort(key=lambda row: (row[0], row[1]))
    return [item[2] for item in ordered]


def extract_boxes(json_path: str, include_omitted: bool = False) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    json_path_obj = Path(json_path).expanduser().resolve()
    if not json_path_obj.exists():
        raise FileNotFoundError(f"No existe el JSON: {json_path_obj}")

    payload = json.loads(json_path_obj.read_text(encoding="utf-8"))
    image_path = _resolve_image_path(json_path_obj, payload)

    slots = _slots_in_order(payload)
    boxes: list[dict[str, Any]] = []

    for order_idx, slot in enumerate(slots, start=1):
        if not isinstance(slot, dict):
            continue

        bbox = slot.get("bbox")
        omitted = bool(slot.get("omitted", False))
        if (bbox is None or omitted) and not include_omitted:
            continue

        box_data = {
            "order": order_idx,
            "label_id": slot.get("label_id"),
            "region_key": slot.get("region_key"),
            "region_name": slot.get("region_name"),
            "region_alias": slot.get("region_alias", slot.get("region_name")),
            "box_index": slot.get("box_index"),
            "score": slot.get("score"),
            "ethnicity": slot.get("ethnicity", payload.get("global", {}).get("ethnicity")),
            "omitted": omitted,
            "bbox": bbox,
        }
        boxes.append(box_data)

    return image_path, payload, boxes


def print_boxes(boxes: list[dict[str, Any]]) -> None:
    if not boxes:
        print("No hay boxes para imprimir.")
        return

    print("\nBoxes en orden:")
    for box in boxes:
        bbox = box.get("bbox")
        if bbox is None:
            print(
                f"{box['order']:02d}. {box.get('region_alias')}#{box.get('box_index')} "
                f"(omitido) | score={box.get('score')} | etnicidad={box.get('ethnicity')}"
            )
            continue

        x = bbox.get("x")
        y = bbox.get("y")
        w = bbox.get("w")
        h = bbox.get("h")
        print(
            f"{box['order']:02d}. {box.get('region_alias')}#{box.get('box_index')} | "
            f"score={box.get('score')} | etnicidad={box.get('ethnicity')} | "
            f"bbox=(x={x}, y={y}, w={w}, h={h})"
        )


def _draw_text_box(draw: ImageDraw.ImageDraw, x: int, y: int, text: str, color: str, font: ImageFont.ImageFont) -> None:
    text_bbox = draw.textbbox((x, y), text, font=font)
    tw = text_bbox[2] - text_bbox[0]
    th = text_bbox[3] - text_bbox[1]
    by = max(0, y - th - 4)
    draw.rectangle([x, by, x + tw + 6, by + th + 4], fill=color)
    draw.text((x + 3, by + 2), text, fill="black", font=font)


def render_boxes_on_image(
    image_path: Path,
    boxes: list[dict[str, Any]],
    output_image_path: str | None = None,
) -> tuple[Path, Image.Image]:
    image = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default()

    for idx, box in enumerate(boxes):
        if box.get("omitted") or not isinstance(box.get("bbox"), dict):
            continue

        color = COLORS[idx % len(COLORS)]
        bbox = box["bbox"]
        x = int(bbox["x"])
        y = int(bbox["y"])
        w = int(bbox["w"])
        h = int(bbox["h"])
        x2 = x + max(1, w)
        y2 = y + max(1, h)

        draw.rectangle([x, y, x2, y2], outline=color, width=3)
        label = f"{box['order']:02d}:{box.get('region_alias')}#{box.get('box_index')} s={box.get('score')}"
        _draw_text_box(draw, x, y, label, color, font)

    if output_image_path is None:
        preview_dir = _project_root() / "data_boxes"
        preview_dir.mkdir(parents=True, exist_ok=True)
        output_path = preview_dir / f"{image_path.stem}_boxes_preview.jpg"
    else:
        output_path = Path(output_image_path).expanduser().resolve()

    image.save(output_path, quality=95)
    return output_path, image


def show_boxes_from_json(
    json_path: str,
    output_image_path: str | None = None,
    include_omitted: bool = False,
    show: bool = False,
) -> dict[str, Any]:
    image_path, payload, boxes = extract_boxes(json_path, include_omitted=include_omitted)

    print(f"JSON: {Path(json_path).expanduser().resolve()}")
    print(f"Imagen original: {image_path}")
    print(f"Global: {payload.get('global', {})}")
    print_boxes(boxes)

    output_path, image = render_boxes_on_image(image_path, boxes, output_image_path=output_image_path)
    print(f"\nPreview guardado en: {output_path}")

    if show:
        try:
            import matplotlib.pyplot as plt

            plt.figure(figsize=(12, 9))
            plt.imshow(image)
            plt.title("Preview de bounding boxes")
            plt.axis("off")
            plt.show()
        except Exception:
            image.show()

    return {
        "json_path": str(Path(json_path).expanduser().resolve()),
        "image_path": str(image_path),
        "preview_path": str(output_path),
        "boxes": boxes,
    }


def _run_cli() -> None:
    parser = argparse.ArgumentParser(description="Muestra y valida bounding boxes desde un JSON de anotaciones.")
    parser.add_argument("json_path", type=str, help="Ruta del archivo JSON de anotaciones")
    parser.add_argument("--output", type=str, default=None, help="Ruta de salida para la imagen preview")
    parser.add_argument("--include-omitted", action="store_true", help="Incluye slots omitidos en la salida de texto")
    parser.add_argument("--show", action="store_true", help="Abre/visualiza la imagen con boxes")
    args = parser.parse_args()

    show_boxes_from_json(
        json_path=args.json_path,
        output_image_path=args.output,
        include_omitted=args.include_omitted,
        show=args.show,
    )


if __name__ == "__main__":
    _run_cli()
