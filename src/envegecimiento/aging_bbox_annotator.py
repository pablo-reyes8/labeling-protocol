from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import matplotlib
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class RegionSpec:
    label_id: int
    key: str
    name: str
    max_boxes: int


REGION_SPECS: list[RegionSpec] = [
    RegionSpec(1, "frente", "Frente", 1),
    RegionSpec(2, "glabela_entrecejo", "Glabela / Entrecejo", 1),
    RegionSpec(3, "patas_de_gallo", "Patas de gallo", 2),
    RegionSpec(4, "bajo_ojo_ojeras", "Bajo ojo / Ojeras", 2),
    RegionSpec(5, "surcos_nasogenianos", "Surcos Nasogenianos", 2),
    RegionSpec(6, "labio_superior", "Labio superior", 1),
    RegionSpec(7, "comisuras_lineas_marioneta", "Comisuras / lineas de marioneta", 2),
    RegionSpec(8, "puente_nasal", "Puente Nasal", 1),
]


def _iso_utc_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _configure_interactive_backend(prefer_external_window: bool = True) -> str:
    """Configure an interactive backend. Local mode prefers external desktop windows."""
    in_colab = "google.colab" in sys.modules

    if in_colab:
        try:
            from google.colab import output as colab_output

            colab_output.enable_custom_widget_manager()
        except Exception:
            pass

    backend = str(matplotlib.get_backend()).lower()
    if prefer_external_window and any(token in backend for token in ("tkagg", "qt")):
        return str(matplotlib.get_backend())

    if any(token in backend for token in ("ipympl", "nbagg", "widget", "tkagg", "qt")):
        return str(matplotlib.get_backend())

    candidates: list[str] = []
    if prefer_external_window and not in_colab:
        candidates.extend(["TkAgg", "QtAgg", "Qt5Agg"])
    candidates.extend(["module://ipympl.backend_nbagg", "nbAgg"])

    for candidate in candidates:
        try:
            matplotlib.use(candidate, force=True)
            return candidate
        except Exception:
            continue

    current = str(matplotlib.get_backend())
    raise RuntimeError(
        "No pude activar un backend interactivo para dibujar cajas. "
        "En local instala tkinter o pyqt5 para ventana externa, o usa ipympl para notebook: "
        "`pip install pyqt5 ipympl` "
        "(en Colab: `%pip install ipympl` y reinicia el runtime). "
        f"Backend actual: {current}"
    )


def _ask_int(prompt: str, min_value: int = 0, max_value: int = 100) -> int:
    while True:
        raw = input(prompt).strip()
        try:
            value = int(raw)
        except ValueError:
            print(f"Valor invalido: '{raw}'. Debe ser un entero entre {min_value} y {max_value}.")
            continue

        if min_value <= value <= max_value:
            return value

        print(f"Fuera de rango. Debe estar entre {min_value} y {max_value}.")


def _sanitize_bbox(x0: float, y0: float, x1: float, y1: float, width: int, height: int) -> dict[str, int]:
    x0_i = int(round(min(x0, x1)))
    y0_i = int(round(min(y0, y1)))
    x1_i = int(round(max(x0, x1)))
    y1_i = int(round(max(y0, y1)))

    x0_i = max(0, min(x0_i, width - 1))
    y0_i = max(0, min(y0_i, height - 1))
    x1_i = max(0, min(x1_i, width - 1))
    y1_i = max(0, min(y1_i, height - 1))

    w = max(1, x1_i - x0_i)
    h = max(1, y1_i - y0_i)

    return {"x": x0_i, "y": y0_i, "w": w, "h": h}


class NotebookBoxCollector:
    def __init__(self, image_rgb: np.ndarray, figure_size: tuple[float, float] = (14, 10)) -> None:
        import matplotlib.pyplot as plt
        from matplotlib.widgets import RectangleSelector

        self.plt = plt
        self.RectangleSelector = RectangleSelector
        self.image_rgb = image_rgb
        self.image_h, self.image_w = image_rgb.shape[:2]

        self.fig, self.ax = self.plt.subplots(figsize=figure_size)
        self.ax.imshow(self.image_rgb)
        self.ax.set_axis_off()

        self.current_region_name = ""
        self.current_max_boxes = 1
        self.current_boxes: list[dict[str, int]] = []
        self.current_artists: list[tuple[Any, Any]] = []
        self.region_done = False

        self.selector = self.RectangleSelector(
            self.ax,
            self._on_select,
            useblit=True,
            button=[1],
            minspanx=2,
            minspany=2,
            spancoords="pixels",
            interactive=False,
        )
        self.cid_key = self.fig.canvas.mpl_connect("key_press_event", self._on_key_press)
        self._show_figure_now()

    def _show_figure_now(self) -> None:
        backend = str(matplotlib.get_backend()).lower()

        try:
            manager = getattr(self.fig.canvas, "manager", None)
            if manager is not None:
                manager.set_window_title("Aging BBox Annotator")
                window = getattr(manager, "window", None)
                if window is not None:
                    # Maximize window when supported by the GUI toolkit.
                    if hasattr(window, "showMaximized"):
                        window.showMaximized()
                    elif hasattr(window, "state"):
                        window.state("zoomed")
        except Exception:
            pass

        if any(token in backend for token in ("ipympl", "widget", "nbagg")):
            try:
                from IPython.display import display

                if any(token in backend for token in ("ipympl", "widget")):
                    display(self.fig.canvas)
                else:
                    display(self.fig)
            except Exception:
                pass

        try:
            self.plt.show(block=False)
        except TypeError:
            self.plt.show()

        self.fig.canvas.draw_idle()
        self.plt.pause(0.1)

    def show_message(self, message: str) -> None:
        self.ax.set_title(message, fontsize=11)
        self.fig.canvas.draw_idle()
        self.plt.pause(0.05)

    def _set_title(self) -> None:
        self.ax.set_title(
            f"Anotando: {self.current_region_name} | Max cajas: {self.current_max_boxes}\n"
            "Dibuja con mouse. Enter: terminar region. Backspace/Delete: deshacer ultima caja.",
            fontsize=11,
        )
        self.fig.canvas.draw_idle()

    def _on_select(self, eclick: Any, erelease: Any) -> None:
        if eclick.xdata is None or eclick.ydata is None or erelease.xdata is None or erelease.ydata is None:
            return

        if len(self.current_boxes) >= self.current_max_boxes:
            print(f"Ya alcanzaste el maximo de {self.current_max_boxes} cajas para esta region.")
            return

        bbox = _sanitize_bbox(
            eclick.xdata,
            eclick.ydata,
            erelease.xdata,
            erelease.ydata,
            self.image_w,
            self.image_h,
        )
        self.current_boxes.append(bbox)

        box_idx = len(self.current_boxes)
        rect = self.plt.Rectangle(
            (bbox["x"], bbox["y"]),
            bbox["w"],
            bbox["h"],
            fill=False,
            linewidth=2,
            edgecolor="#f4a261",
        )
        self.ax.add_patch(rect)

        label_text = self.ax.text(
            bbox["x"],
            max(0, bbox["y"] - 4),
            f"{self.current_region_name} #{box_idx}",
            color="white",
            fontsize=9,
            bbox={"facecolor": "black", "alpha": 0.6, "pad": 2},
        )
        self.current_artists.append((rect, label_text))

        self.fig.canvas.draw_idle()
        print(f"Caja {box_idx} registrada: {bbox}")

    def _remove_last_box(self) -> None:
        if not self.current_boxes:
            print("No hay cajas para deshacer.")
            return

        self.current_boxes.pop()
        rect, text = self.current_artists.pop()
        rect.remove()
        text.remove()
        self.fig.canvas.draw_idle()
        print("Ultima caja eliminada.")

    def _on_key_press(self, event: Any) -> None:
        key = (event.key or "").lower()
        if key in {"enter", "return"}:
            self.region_done = True
            print("Region cerrada con Enter.")
        elif key in {"backspace", "delete"}:
            self._remove_last_box()

    def _freeze_current_artists(self) -> None:
        for rect, text in self.current_artists:
            rect.set_edgecolor("#2a9d8f")
            rect.set_linewidth(2)
        self.current_artists = []
        self.fig.canvas.draw_idle()

    def collect_region(self, region_name: str, max_boxes: int) -> list[dict[str, int]]:
        self.current_region_name = region_name
        self.current_max_boxes = max_boxes
        self.current_boxes = []
        self.current_artists = []
        self.region_done = False
        self._set_title()

        print("\n" + "=" * 80)
        print(f"Region actual: {region_name}")
        print(f"Dibuja hasta {max_boxes} caja(s) y presiona Enter cuando termines.")

        while not self.region_done:
            if not self.plt.fignum_exists(self.fig.number):
                raise RuntimeError("La figura se cerro antes de terminar la anotacion.")
            self.plt.pause(0.1)

        boxes = self.current_boxes[:max_boxes]
        self._freeze_current_artists()
        return boxes

    def close(self) -> None:
        self.fig.canvas.mpl_disconnect(self.cid_key)
        self.selector.set_active(False)
        self.plt.close(self.fig)


def _build_region_slots(region: RegionSpec, boxes: list[dict[str, int]]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []

    if region.max_boxes == 1:
        if boxes:
            score = _ask_int(f"Score para {region.name} (caja 1, 0-100): ")
            slots.append(
                {
                    "box_index": 1,
                    "bbox": boxes[0],
                    "score": score,
                    "omitted": False,
                }
            )
        else:
            slots.append(
                {
                    "box_index": 1,
                    "bbox": None,
                    "score": None,
                    "omitted": True,
                }
            )
        return slots

    if not boxes:
        return [
            {"box_index": 1, "bbox": None, "score": None, "omitted": True},
            {"box_index": 2, "bbox": None, "score": None, "omitted": True},
        ]

    if len(boxes) == 1:
        score = _ask_int(f"Score para {region.name} (solo caja 1, 0-100): ")
        return [
            {
                "box_index": 1,
                "bbox": boxes[0],
                "score": score,
                "omitted": False,
            },
            {
                "box_index": 2,
                "bbox": None,
                "score": None,
                "omitted": True,
            },
        ]

    score_1 = _ask_int(f"Score para {region.name} caja 1 (0-100): ")
    score_2 = _ask_int(f"Score para {region.name} caja 2 (0-100): ")
    return [
        {
            "box_index": 1,
            "bbox": boxes[0],
            "score": score_1,
            "omitted": False,
        },
        {
            "box_index": 2,
            "bbox": boxes[1],
            "score": score_2,
            "omitted": False,
        },
    ]


def run_annotation_demo(
    image_path: str,
    output_json_path: str | None = None,
    ask_global_first: bool = True,
    prefer_external_window: bool = True,
) -> dict[str, Any]:
    """
    Interactive annotation flow for Jupyter/Colab.

    Pipeline:
    1) Ask global aging score (0-100).
    2) Ask each region in fixed order and draw boxes with mouse.
    3) Press Enter to close each region.
    4) Ask scores per box according to how many boxes were drawn.
    5) Save JSON and return a Python dict with everything.
    """
    _configure_interactive_backend(prefer_external_window=prefer_external_window)

    image_path_obj = Path(image_path).expanduser().resolve()
    if not image_path_obj.exists():
        raise FileNotFoundError(f"No existe la imagen: {image_path_obj}")

    image_rgb = np.array(Image.open(image_path_obj).convert("RGB"))
    image_h, image_w = image_rgb.shape[:2]
    image_stat = image_path_obj.stat()

    global_score = None
    collector = NotebookBoxCollector(image_rgb=image_rgb)

    result: dict[str, Any] = {
        "image_id": image_path_obj.name,
        "image_meta": {
            "filename": image_path_obj.name,
            "stem": image_path_obj.stem,
            "extension": image_path_obj.suffix.lower(),
            "file_size_bytes": int(image_stat.st_size),
            "width_px": image_w,
            "height_px": image_h,
            "modified_at": _iso_utc_from_timestamp(image_stat.st_mtime),
        },
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "global": {"label_id": 0, "score": global_score},
        "regions": {},
        "all_slots": [],
    }

    try:
        collector.show_message(
            "Imagen cargada. Primero escribe el score global (0-100) en la salida de la celda."
        )

        if ask_global_first:
            global_score = _ask_int("Score global de envejecimiento (0-100): ")
            result["global"]["score"] = global_score

        if global_score is None:
            global_score = _ask_int("Score global de envejecimiento (0-100): ")
            result["global"]["score"] = global_score

        for region in REGION_SPECS:
            boxes = collector.collect_region(region.name, region.max_boxes)
            slots = _build_region_slots(region, boxes)

            region_payload = {
                "label_id": region.label_id,
                "region_name": region.name,
                "expected_boxes": region.max_boxes,
                "slots": slots,
            }
            result["regions"][region.key] = region_payload

            for slot in slots:
                result["all_slots"].append(
                    {
                        "label_id": region.label_id,
                        "region_key": region.key,
                        "region_name": region.name,
                        **slot,
                    }
                )
    finally:
        collector.close()

    if output_json_path is None:
        output_path = image_path_obj.with_name(f"{image_path_obj.stem}_annotations.json")
    else:
        output_path = Path(output_json_path).expanduser().resolve()

    output_path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n" + "=" * 80)
    print("Anotacion terminada")
    print(f"Global score: {result['global']['score']}")
    print(f"JSON guardado en: {output_path}")

    return result


def _run_cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Demo interactiva para anotar boxes y scores de envejecimiento")
    parser.add_argument("image_path", type=str, help="Ruta de la imagen")
    parser.add_argument("--output", type=str, default=None, help="Ruta de salida JSON")
    parser.add_argument(
        "--external-window",
        action="store_true",
        help="Intenta abrir la imagen en una ventana externa grande (si el entorno lo soporta).",
    )
    args = parser.parse_args()

    run_annotation_demo(
        args.image_path,
        output_json_path=args.output,
        prefer_external_window=args.external_window,
    )


if __name__ == "__main__":
    _run_cli()
