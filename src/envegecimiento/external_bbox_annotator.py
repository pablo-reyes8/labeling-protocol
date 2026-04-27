from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox

from PIL import Image, ImageTk


@dataclass(frozen=True)
class RegionSpec:
    label_id: int
    key: str
    canonical_name: str
    ui_name: str
    max_boxes: int


REGION_SPECS: list[RegionSpec] = [
    RegionSpec(1, "frente", "Frente", "Frente", 1),
    RegionSpec(2, "glabela_entrecejo", "Glabela / Entrecejo", "Entrecejo", 1),
    RegionSpec(3, "patas_de_gallo", "Patas de gallo", "Patas de gallo", 2),
    RegionSpec(4, "bajo_ojo_ojeras", "Bajo ojo / Ojeras", "Ojeras", 2),
    RegionSpec(5, "surcos_nasogenianos", "Surcos Nasogenianos", "Surcos nasogenianos", 2),
    RegionSpec(6, "labio_superior", "Labio superior", "Labio superior", 1),
    RegionSpec(7, "comisuras_lineas_marioneta", "Comisuras / lineas de marioneta", "Lineas marioneta", 2),
    RegionSpec(8, "puente_nasal", "Puente Nasal", "Puente nasal", 1),
]

ANNOTATED_SLOT_STATUS = "Annotated"
MISSING_SLOT_STATUS = "Missing"


class RedoBoxSelection(Exception):
    def __init__(self, box_index: int) -> None:
        super().__init__(f"Redo requested for box {box_index}")
        self.box_index = box_index


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _resize_for_bounds(image: Image.Image, max_w: int, max_h: int) -> tuple[Image.Image, float]:
    scale = min(max_w / image.width, max_h / image.height, 1.0)
    out_w = max(1, int(round(image.width * scale)))
    out_h = max(1, int(round(image.height * scale)))

    if scale < 0.999:
        resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
        return image.resize((out_w, out_h), resample), scale

    return image.copy(), scale


def _discover_reference_paths(reference_dir: Path) -> dict[int, Path]:
    mapping: dict[int, Path] = {}
    if not reference_dir.exists():
        return mapping

    valid_ext = {".jpg", ".jpeg", ".png", ".webp"}
    for path in sorted(reference_dir.iterdir()):
        if not path.is_file() or path.suffix.lower() not in valid_ext:
            continue

        match = re.match(r"^(\d+)_", path.name)
        if not match:
            continue

        label_id = int(match.group(1))
        if 1 <= label_id <= 8:
            mapping[label_id] = path

    return mapping


def _iso_utc_from_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_slot_payload(
    *,
    box_index: int,
    bbox: dict[str, int] | None,
    score: int | None,
    ethnicity: str,
    omitted: bool,
) -> dict[str, Any]:
    return {
        "box_index": box_index,
        "bbox": bbox,
        "score": score,
        "ethnicity": ethnicity,
        "omitted": omitted,
        "slot_status": MISSING_SLOT_STATUS if omitted else ANNOTATED_SLOT_STATUS,
    }


class ExternalBBoxAnnotator:
    def __init__(
        self,
        root: tk.Tk,
        image_path: str,
        output_json_path: str | None = None,
        reference_dir: str | None = None,
        close_root_on_exit: bool = True,
    ) -> None:
        self.root = root
        self.close_root_on_exit = bool(close_root_on_exit)
        self.image_path = Path(image_path).expanduser().resolve()
        if not self.image_path.exists():
            raise FileNotFoundError(f"No existe la imagen: {self.image_path}")

        if output_json_path is not None:
            self.output_json_path = Path(output_json_path).expanduser().resolve()
        else:
            results_dir = _project_root() / "results"
            self.output_json_path = results_dir / f"{self.image_path.stem}_annotations.json"

        if reference_dir is not None:
            self.reference_dir = Path(reference_dir).expanduser().resolve()
        else:
            self.reference_dir = _project_root() / "Ejemplo"

        self.reference_paths = _discover_reference_paths(self.reference_dir)
        self.prompt_position: tuple[int, int] | None = None
        self.zoom_window_position: tuple[int, int] | None = None

        self.screen_w = int(self.root.winfo_screenwidth())
        self.screen_h = int(self.root.winfo_screenheight())

        target_original = Image.open(self.image_path).convert("RGB")
        self.target_original_w, self.target_original_h = target_original.size
        self.target_original_image = target_original
        image_stat = self.image_path.stat()

        # Reserve vertical space for header, instructions and controls so the
        # full image fits onscreen without looking cropped inside the window.
        global_reserved_h = 320
        region_reserved_h = 360

        global_max_w = int(self.screen_w * 0.78)
        global_max_h = max(320, self.screen_h - global_reserved_h)
        self.target_global_image, _ = _resize_for_bounds(target_original, global_max_w, global_max_h)
        self.target_global_photo = ImageTk.PhotoImage(self.target_global_image)

        panel_max_w = int(self.screen_w * 0.42)
        panel_max_h = max(300, self.screen_h - region_reserved_h)
        self.target_panel_image, self.target_panel_scale = _resize_for_bounds(target_original, panel_max_w, panel_max_h)
        self.target_panel_photo = ImageTk.PhotoImage(self.target_panel_image)
        self.target_panel_w, self.target_panel_h = self.target_panel_image.size

        self.reference_photo_cache: dict[int, ImageTk.PhotoImage] = {}
        self._prepare_reference_cache(panel_max_w=self.target_panel_w, panel_max_h=self.target_panel_h)

        self.current_region_index = -1
        self.current_region: RegionSpec | None = None
        self.current_boxes: list[dict[str, int]] = []
        self.current_artists: list[tuple[int, int]] = []
        self.drag_start: tuple[int, int] | None = None
        self.preview_rect_id: int | None = None
        self.zoom_preview_requested = True
        self.cancelled = False
        self.layout_mode = "none"
        self.left_canvas_image_id: int | None = None
        self.right_canvas_image_id: int | None = None
        self._done_var: tk.BooleanVar | None = None
        self.session_keep_topmost = True

        self.result: dict[str, Any] = {
            "image_id": self.image_path.name,
            "image_path": str(self.image_path),
            "image_meta": {
                "filename": self.image_path.name,
                "stem": self.image_path.stem,
                "extension": self.image_path.suffix.lower(),
                "file_size_bytes": int(image_stat.st_size),
                "width_px": self.target_original_w,
                "height_px": self.target_original_h,
                "modified_at": _iso_utc_from_timestamp(image_stat.st_mtime),
            },
            "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "global": {"label_id": 0, "score": None, "ethnicity": None},
            "regions": {},
            "all_slots": [],
        }

        self._build_ui()
        self._bind_events()

    def _prepare_reference_cache(self, panel_max_w: int, panel_max_h: int) -> None:
        for label_id, path in self.reference_paths.items():
            ref_image = Image.open(path).convert("RGB")
            resized, _scale = _resize_for_bounds(ref_image, panel_max_w, panel_max_h)
            self.reference_photo_cache[label_id] = ImageTk.PhotoImage(resized)

    def _build_instruction_card(self, parent: tk.Frame, column: int, title: str, body: str) -> None:
        card = tk.Frame(
            parent,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            padx=10,
            pady=6,
        )
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 6, 0))

        tk.Label(
            card,
            text=title,
            anchor="w",
            justify="left",
            font=("Arial", 9, "bold"),
            fg="#0f172a",
            bg="#ffffff",
        ).pack(fill="x")

        tk.Label(
            card,
            text=body,
            anchor="w",
            justify="left",
            wraplength=180,
            font=("Arial", 8),
            fg="#475569",
            bg="#ffffff",
        ).pack(fill="x", pady=(4, 0))

    def _build_ui(self) -> None:
        self.root.title("Aging Box Annotator - Local")
        self.root.configure(bg="#f3f4f6")

        wrapper = tk.Frame(self.root, bg="#f3f4f6")
        wrapper.pack(fill="both", expand=True, padx=10, pady=10)

        header = tk.Frame(wrapper, bg="#0f172a", bd=0)
        header.pack(fill="x", pady=(0, 6))

        self.step_var = tk.StringVar(value="Paso 0/8 | Global")
        step_label = tk.Label(
            header,
            textvariable=self.step_var,
            anchor="w",
            justify="left",
            font=("Arial", 16, "bold"),
            fg="white",
            bg="#0f172a",
            padx=12,
            pady=6,
        )
        step_label.pack(fill="x")

        status_caption = tk.Label(
            header,
            text="Que hacer ahora",
            anchor="w",
            justify="left",
            font=("Arial", 8, "bold"),
            fg="#93c5fd",
            bg="#0f172a",
            padx=12,
            pady=0,
        )
        status_caption.pack(fill="x", pady=(0, 2))

        self.status_var = tk.StringVar(value="Cargando interfaz...")
        status_label = tk.Label(
            header,
            textvariable=self.status_var,
            anchor="w",
            justify="left",
            font=("Arial", 10),
            fg="#dbeafe",
            bg="#0f172a",
            padx=12,
            pady=0,
        )
        status_label.pack(fill="x", pady=(0, 8))

        help_panel = tk.Frame(wrapper, bg="#f3f4f6")
        help_panel.pack(fill="x", pady=(0, 6))
        for column in range(4):
            help_panel.grid_columnconfigure(column, weight=1)

        self._build_instruction_card(
            help_panel,
            0,
            "1. Dibuja la zona",
            "Click y arrastra en la foto derecha.",
        )
        self._build_instruction_card(
            help_panel,
            1,
            "2. Guarda y sigue",
            "Enter o boton 'Guardar zona y seguir'.",
        )
        self._build_instruction_card(
            help_panel,
            2,
            "3. Si no se puede ver",
            "Usa Q si esta tapada o corrupta.",
        )
        self._build_instruction_card(
            help_panel,
            3,
            "Atajos utiles",
            "Backspace deshace. Z cambia el zoom.",
        )

        self.panes_row = tk.Frame(wrapper, bg="#f3f4f6")
        self.panes_row.pack(fill="both", expand=True)

        self.left_frame = tk.Frame(self.panes_row, bg="#f3f4f6")
        self.left_title_var = tk.StringVar(value="Referencia")
        left_title = tk.Label(
            self.left_frame,
            textvariable=self.left_title_var,
            font=("Arial", 12, "bold"),
            fg="#0f172a",
            bg="#f3f4f6",
        )
        left_title.pack(pady=(0, 6))

        self.left_canvas = tk.Canvas(
            self.left_frame,
            width=self.target_panel_w,
            height=self.target_panel_h,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#9ca3af",
        )
        self.left_canvas.pack(expand=True)

        self.right_frame = tk.Frame(self.panes_row, bg="#f3f4f6")
        self.right_title_var = tk.StringVar(value="Imagen a anotar")
        right_title = tk.Label(
            self.right_frame,
            textvariable=self.right_title_var,
            font=("Arial", 12, "bold"),
            fg="#0f172a",
            bg="#f3f4f6",
        )
        right_title.pack(pady=(0, 6))

        self.right_canvas = tk.Canvas(
            self.right_frame,
            width=self.target_panel_w,
            height=self.target_panel_h,
            bg="#ffffff",
            highlightthickness=1,
            highlightbackground="#9ca3af",
            cursor="crosshair",
        )
        self.right_canvas.pack(expand=True)

        controls = tk.Frame(wrapper, bg="#f3f4f6")
        controls.pack(fill="x", pady=(10, 0))
        tk.Button(controls, text="Guardar zona y seguir (Enter)", command=self.complete_region).pack(side="left")
        tk.Button(controls, text="Deshacer ultima caja", command=self.undo_last_box).pack(side="left", padx=8)
        tk.Button(controls, text="Omitir esta zona (Q)", command=self.omit_current_region).pack(side="left")
        tk.Button(controls, text="Cancelar anotacion", command=self.on_close).pack(side="right")

        self._try_maximize()
        self._show_global_layout()
        self._schedule_initial_foreground()

    def _schedule_initial_foreground(self) -> None:
        # Algunos launchers (Jupyter/IDE) dejan la ventana nueva en segundo plano.
        # Hacemos varios intentos cortos mientras Tk termina de mapearla.
        self.root.after(20, self._bring_main_window_to_front)
        self.root.after(180, self._bring_main_window_to_front)
        self.root.after(420, self._bring_main_window_to_front)

    def _release_topmost(self, window: Any) -> None:
        try:
            if window.winfo_exists():
                window.attributes("-topmost", False)
        except Exception:
            pass

    def _try_force_foreground_on_windows(self, window: Any) -> None:
        if os.name != "nt":
            return

        try:
            import ctypes

            hwnd = int(window.winfo_id())
            user32 = ctypes.windll.user32
            sw_restore = 9
            hwnd_topmost = -1
            hwnd_notopmost = -2
            swp_nosize = 0x0001
            swp_nomove = 0x0002
            swp_showwindow = 0x0040
            flags = swp_nosize | swp_nomove | swp_showwindow

            user32.ShowWindow(hwnd, sw_restore)
            user32.SetWindowPos(hwnd, hwnd_topmost, 0, 0, 0, 0, flags)
            user32.SetWindowPos(hwnd, hwnd_notopmost, 0, 0, 0, 0, flags)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass

    def _present_window(
        self,
        window: Any,
        focus_widget: Any | None = None,
        keep_topmost: bool = False,
    ) -> None:
        try:
            window.update_idletasks()
        except Exception:
            pass

        try:
            window.deiconify()
        except Exception:
            pass

        try:
            window.lift()
        except Exception:
            pass

        self._try_force_foreground_on_windows(window)

        must_keep_topmost = keep_topmost or self.session_keep_topmost
        try:
            window.attributes("-topmost", True)
            if not must_keep_topmost:
                window.after(250, lambda win=window: self._release_topmost(win))
        except Exception:
            pass

        target = focus_widget or window
        try:
            target.focus_force()
        except Exception:
            try:
                target.focus_set()
            except Exception:
                pass

    def _bring_main_window_to_front(self) -> None:
        focus_target = self.right_canvas if self.right_canvas.winfo_exists() else self.root
        self._present_window(self.root, focus_widget=focus_target, keep_topmost=True)

    def _restore_main_window_after_dialog(self) -> None:
        if not self.root.winfo_exists():
            return

        focus_target = self.right_canvas if self.right_canvas.winfo_exists() else self.root
        self._present_window(self.root, focus_widget=focus_target, keep_topmost=True)
        self.root.update_idletasks()
        self.root.after(80, self._bring_main_window_to_front)

    def _disable_session_topmost(self) -> None:
        self.session_keep_topmost = False
        self._release_topmost(self.root)

    def _try_maximize(self) -> None:
        try:
            self.root.state("zoomed")
            return
        except Exception:
            pass

        w = min(int(self.screen_w * 0.95), self.screen_w - 40)
        h = min(int(self.screen_h * 0.92), self.screen_h - 60)
        self.root.geometry(f"{w}x{h}+20+20")

    def _bind_events(self) -> None:
        self.root.bind("<Return>", lambda _: self.complete_region())
        self.root.bind("<BackSpace>", lambda _: self.undo_last_box())
        self.root.bind("<Delete>", lambda _: self.undo_last_box())
        self.root.bind("<q>", lambda _: self.omit_current_region())
        self.root.bind("<Q>", lambda _: self.omit_current_region())
        self.root.bind("<z>", lambda _: self.toggle_zoom_for_scoring())
        self.root.bind("<Z>", lambda _: self.toggle_zoom_for_scoring())
        self.root.bind("<Escape>", lambda _: self.on_close())
        self.right_canvas.bind("<ButtonPress-1>", self._on_mouse_down)
        self.right_canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.right_canvas.bind("<ButtonRelease-1>", self._on_mouse_up)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def _set_step(self, text: str) -> None:
        self.step_var.set(text)

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _show_global_layout(self) -> None:
        self.left_frame.pack_forget()
        self.right_frame.pack_forget()
        self.right_frame.pack(fill="both", expand=True)
        self.layout_mode = "global"

        self.right_title_var.set("Imagen a anotar | Vista global")
        self.right_canvas.delete("all")
        self.right_canvas.config(
            width=self.target_global_image.width,
            height=self.target_global_image.height,
        )
        self.right_canvas.pack_configure(fill="none", expand=True)
        self.right_canvas_image_id = self.right_canvas.create_image(
            self.target_global_image.width // 2,
            self.target_global_image.height // 2,
            image=self.target_global_photo,
            anchor="center",
        )
        # Keep a direct reference on the widget to avoid any Tk image drop.
        self.right_canvas.image = self.target_global_photo
        self.root.update_idletasks()
        self._bring_main_window_to_front()

        self._set_step("Paso 0/8 | Datos globales")
        self._set_status(
            "Completa primero los datos generales.\n"
            "Ingresa sexo y etnicidad, y luego el score global entre 0 y 100."
        )

    def _show_region_layout(self) -> None:
        self.left_frame.pack_forget()
        self.right_frame.pack_forget()
        self.left_frame.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self.right_frame.pack(side="right", fill="both", expand=True, padx=(8, 0))
        self.layout_mode = "region"

        self.right_canvas.pack_configure(fill="none", expand=True)
        self.left_canvas.pack_configure(fill="none", expand=True)

        self.right_canvas.delete("all")
        self.right_canvas.config(width=self.target_panel_w, height=self.target_panel_h)
        self.right_canvas_image_id = self.right_canvas.create_image(0, 0, image=self.target_panel_photo, anchor="nw")
        self.right_canvas.image = self.target_panel_photo
        self.right_canvas.focus_set()
        self.root.update_idletasks()
        self._bring_main_window_to_front()

    def _update_reference_panel(self, region: RegionSpec) -> None:
        self.left_canvas.delete("all")

        ref_photo = self.reference_photo_cache.get(region.label_id)
        if ref_photo is None:
            self.left_title_var.set(f"Referencia | {region.ui_name} (no encontrada)")
            self.left_canvas.create_text(
                self.target_panel_w // 2,
                self.target_panel_h // 2,
                text=f"Sin imagen de referencia\nID {region.label_id}",
                fill="white",
                font=("Arial", 12, "bold"),
                justify="center",
            )
            return

        self.left_title_var.set(f"Referencia #{region.label_id} | {region.ui_name}")
        x = (self.target_panel_w - ref_photo.width()) // 2
        y = (self.target_panel_h - ref_photo.height()) // 2
        self.left_canvas_image_id = self.left_canvas.create_image(x, y, image=ref_photo, anchor="nw")
        self.left_canvas.image = ref_photo

    def _remember_prompt_position(self, window: tk.Toplevel) -> None:
        try:
            self.prompt_position = (window.winfo_x(), window.winfo_y())
        except Exception:
            pass

    def _place_prompt_window(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        w = window.winfo_width()
        h = window.winfo_height()

        if self.prompt_position is not None:
            x = _clamp(self.prompt_position[0], 10, max(10, self.screen_w - w - 10))
            y = _clamp(self.prompt_position[1], 10, max(10, self.screen_h - h - 10))
        else:
            x = self.root.winfo_x() + max(0, (self.root.winfo_width() - w) // 2)
            y = self.root.winfo_y() + max(0, (self.root.winfo_height() - h) // 2)
            x = _clamp(x, 10, max(10, self.screen_w - w - 10))
            y = _clamp(y, 10, max(10, self.screen_h - h - 10))

        window.geometry(f"+{x}+{y}")

    def _prompt_value(self, title: str, prompt: str, initial: str = "") -> str | None:
        result: dict[str, str | None] = {"value": None}

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f9fafb")

        container = tk.Frame(dialog, padx=14, pady=12, bg="#f9fafb")
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=prompt,
            anchor="w",
            justify="left",
            font=("Arial", 10, "bold"),
            bg="#f9fafb",
            fg="#111827",
        ).pack(fill="x", pady=(0, 8))

        entry_var = tk.StringVar(value=initial)
        entry = tk.Entry(container, textvariable=entry_var, font=("Arial", 11), width=42)
        entry.pack(fill="x")

        buttons = tk.Frame(container, bg="#f9fafb")
        buttons.pack(fill="x", pady=(10, 0))

        def on_ok() -> None:
            result["value"] = entry_var.get()
            self._remember_prompt_position(dialog)
            dialog.destroy()

        def on_cancel() -> None:
            self._remember_prompt_position(dialog)
            dialog.destroy()

        tk.Button(buttons, text="OK", width=10, command=on_ok).pack(side="left")
        tk.Button(buttons, text="Cancelar", width=10, command=on_cancel).pack(side="left", padx=8)

        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.bind("<Return>", lambda _: on_ok())
        dialog.bind("<Escape>", lambda _: on_cancel())

        self._place_prompt_window(dialog)
        self._present_window(dialog, focus_widget=entry)
        dialog.wait_window()
        self._restore_main_window_after_dialog()
        return result["value"]

    def _prompt_score_value(self, title: str, prompt: str, initial: str = "") -> tuple[str, str | None]:
        result: dict[str, str | None] = {"action": "redo", "value": None}

        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#f9fafb")

        container = tk.Frame(dialog, padx=14, pady=12, bg="#f9fafb")
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=prompt,
            anchor="w",
            justify="left",
            font=("Arial", 10, "bold"),
            bg="#f9fafb",
            fg="#111827",
        ).pack(fill="x", pady=(0, 8))

        tk.Label(
            container,
            text="Tip: usa Rehacer caja o cierra esta ventana para volver a seleccionar.",
            anchor="w",
            justify="left",
            font=("Arial", 9),
            bg="#f9fafb",
            fg="#64748b",
        ).pack(fill="x", pady=(0, 8))

        entry_var = tk.StringVar(value=initial)
        entry = tk.Entry(container, textvariable=entry_var, font=("Arial", 11), width=42)
        entry.pack(fill="x")

        buttons = tk.Frame(container, bg="#f9fafb")
        buttons.pack(fill="x", pady=(10, 0))

        def on_ok() -> None:
            result["action"] = "ok"
            result["value"] = entry_var.get()
            self._remember_prompt_position(dialog)
            dialog.destroy()

        def on_redo() -> None:
            result["action"] = "redo"
            result["value"] = None
            self._remember_prompt_position(dialog)
            dialog.destroy()

        tk.Button(buttons, text="Guardar score", width=12, command=on_ok).pack(side="left")
        tk.Button(buttons, text="Rehacer caja", width=12, command=on_redo).pack(side="left", padx=8)

        dialog.protocol("WM_DELETE_WINDOW", on_redo)
        dialog.bind("<Escape>", lambda _: on_redo())
        dialog.bind("<Return>", lambda _: on_ok())

        self._place_prompt_window(dialog)
        self._present_window(dialog, focus_widget=entry)
        dialog.wait_window()
        self._restore_main_window_after_dialog()
        return str(result["action"]), result["value"]

    def _confirm_retry(self, message: str) -> bool:
        return messagebox.askyesno("Valor requerido", message, parent=self.root)

    def _confirm_omit_region(self, region: RegionSpec, current_box_count: int) -> bool:
        result = {"omit": False}

        dialog = tk.Toplevel(self.root)
        dialog.title("Confirmar omision")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.configure(bg="#fff7ed")

        container = tk.Frame(dialog, padx=16, pady=14, bg="#fff7ed")
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text="Warning",
            anchor="w",
            justify="left",
            font=("Arial", 11, "bold"),
            fg="#9a3412",
            bg="#fff7ed",
        ).pack(fill="x", pady=(0, 6))

        warning_message = f"Vas a omitir la zona '{region.ui_name}'."
        if current_box_count > 0:
            warning_message += (
                f"\n\nYa dibujaste {current_box_count} caja(s). "
                "Si confirmas, esas cajas se descartaran y la zona quedara como Missing."
            )
        else:
            warning_message += "\n\nLa zona quedara guardada como Missing."

        tk.Label(
            container,
            text=warning_message,
            anchor="w",
            justify="left",
            font=("Arial", 10),
            fg="#111827",
            bg="#fff7ed",
        ).pack(fill="x")

        tk.Label(
            container,
            text="Presiona Enter para confirmar o usa el boton para volver a etiquetar.",
            anchor="w",
            justify="left",
            font=("Arial", 9),
            fg="#7c2d12",
            bg="#fff7ed",
        ).pack(fill="x", pady=(10, 0))

        buttons = tk.Frame(container, bg="#fff7ed")
        buttons.pack(fill="x", pady=(12, 0))

        def on_confirm() -> None:
            result["omit"] = True
            self._remember_prompt_position(dialog)
            dialog.destroy()

        def on_back() -> None:
            result["omit"] = False
            self._remember_prompt_position(dialog)
            dialog.destroy()

        confirm_button = tk.Button(buttons, text="Confirmar omision", width=16, command=on_confirm)
        confirm_button.pack(side="left")
        tk.Button(buttons, text="Volver a etiquetar", width=16, command=on_back).pack(side="left", padx=8)

        dialog.protocol("WM_DELETE_WINDOW", on_back)
        dialog.bind("<Return>", lambda _: on_confirm())
        dialog.bind("<Escape>", lambda _: on_back())

        self._place_prompt_window(dialog)
        self._present_window(dialog, focus_widget=confirm_button)
        dialog.wait_window()
        self._restore_main_window_after_dialog()
        return bool(result["omit"])

    def _ask_ethnicity(self) -> str:
        while True:
            raw = self._prompt_value(
                title="Sexo y Etnicidad",
                prompt="Sexo y Etnicidad (ej: Mujer Blanca Europea):",
            )
            if raw is None:
                if self._confirm_retry("Debes ingresar etnicidad. Quieres intentar de nuevo?"):
                    continue
                raise RuntimeError("Anotacion cancelada por usuario.")

            value = raw.strip()
            if not value:
                messagebox.showerror("Dato invalido", "La etnicidad no puede estar vacia.", parent=self.root)
                continue

            if any(char.isdigit() for char in value):
                messagebox.showerror(
                    "Dato invalido",
                    "La etnicidad solo acepta texto. No uses numeros.",
                    parent=self.root,
                )
                continue

            return value

    def _ask_score(self, title: str, prompt: str) -> int:
        while True:
            raw = self._prompt_value(title=title, prompt=prompt)
            if raw is None:
                if self._confirm_retry("Debes ingresar un score entre 0 y 100. Quieres intentar de nuevo?"):
                    continue
                raise RuntimeError("Anotacion cancelada por usuario.")

            value = raw.strip()
            try:
                score = int(value)
            except ValueError:
                messagebox.showerror("Dato invalido", "El score debe ser un numero entero.", parent=self.root)
                continue

            if not (0 <= score <= 100):
                messagebox.showerror("Dato invalido", "El score debe estar entre 0 y 100.", parent=self.root)
                continue

            return score

    def _ask_score_with_redo(self, title: str, prompt: str, box_index: int) -> int:
        while True:
            action, raw = self._prompt_score_value(title=title, prompt=prompt)
            if action == "redo":
                raise RedoBoxSelection(box_index=box_index)

            if raw is None:
                raise RedoBoxSelection(box_index=box_index)

            value = raw.strip()
            try:
                score = int(value)
            except ValueError:
                messagebox.showerror("Dato invalido", "El score debe ser un numero entero.", parent=self.root)
                continue

            if not (0 <= score <= 100):
                messagebox.showerror("Dato invalido", "El score debe estar entre 0 y 100.", parent=self.root)
                continue

            return score

    def run(self) -> dict[str, Any]:
        self._done_var = tk.BooleanVar(master=self.root, value=False)
        self.root.after(60, self._bring_main_window_to_front)
        self.root.after(120, self._start_pipeline)
        if self.close_root_on_exit:
            self.root.mainloop()
        else:
            self.root.wait_variable(self._done_var)
        if self.cancelled:
            raise RuntimeError("Anotacion cancelada.")
        return self.result

    def _quit_app(self) -> None:
        self._disable_session_topmost()
        if self._done_var is not None:
            try:
                self._done_var.set(True)
            except Exception:
                pass

        if self.close_root_on_exit:
            self.root.quit()
            if self.root.winfo_exists():
                self.root.destroy()

    def _start_pipeline(self) -> None:
        try:
            self._bring_main_window_to_front()
            ethnicity = self._ask_ethnicity()
            global_score = self._ask_score("Score global", "Score global de envejecimiento (0-100):")

            self.result["global"]["ethnicity"] = ethnicity
            self.result["global"]["score"] = global_score

            self._show_region_layout()
            self._start_next_region()
        except RuntimeError:
            self.cancelled = True
            self._quit_app()

    def _start_next_region(self) -> None:
        self.current_region_index += 1
        if self.current_region_index >= len(REGION_SPECS):
            self._finish()
            return

        self.current_region = REGION_SPECS[self.current_region_index]
        self.current_boxes = []
        self.drag_start = None
        self._clear_annotation_overlays()

        self._show_region_layout()
        self._update_reference_panel(self.current_region)

        self.right_title_var.set(f"Imagen a anotar | {self.current_region.ui_name}")
        self._set_step(f"Paso {self.current_region_index + 1}/8 | {self.current_region.ui_name}")
        self._set_status(
            f"Dibuja la zona de {self.current_region.ui_name} en la imagen derecha.\n"
            "Cuando termines esta zona, usa 'Guardar zona y seguir' o presiona Enter.\n"
            f"Zoom al calificar: {'activado' if self.zoom_preview_requested else 'desactivado'}."
        )
        self.right_canvas.focus_set()

    def toggle_zoom_for_scoring(self) -> None:
        if self.current_region is None:
            return

        self.zoom_preview_requested = not self.zoom_preview_requested
        if self.zoom_preview_requested:
            self._set_status(
                f"{self.current_region.ui_name}: zoom activado. "
                "Al calificar, se abrira una ventana por caja y se cerrara al finalizar cada score."
            )
        else:
            self._set_status(f"{self.current_region.ui_name}: zoom desactivado.")

    def _clear_annotation_overlays(self) -> None:
        if self.preview_rect_id is not None:
            self.right_canvas.delete(self.preview_rect_id)
            self.preview_rect_id = None

        for rect_id, text_id in self.current_artists:
            self.right_canvas.delete(rect_id)
            self.right_canvas.delete(text_id)

        self.current_artists = []

    def _on_mouse_down(self, event: tk.Event[Any]) -> None:
        if self.current_region is None:
            return

        if len(self.current_boxes) >= self.current_region.max_boxes:
            self._set_status(
                f"{self.current_region.ui_name}: maximo de {self.current_region.max_boxes} cajas alcanzado. "
                "Ahora guarda esta zona con Enter o con el boton de continuar."
            )
            return

        x = _clamp(int(event.x), 0, self.target_panel_w - 1)
        y = _clamp(int(event.y), 0, self.target_panel_h - 1)
        self.drag_start = (x, y)

        if self.preview_rect_id is not None:
            self.right_canvas.delete(self.preview_rect_id)
            self.preview_rect_id = None

    def _on_mouse_drag(self, event: tk.Event[Any]) -> None:
        if self.drag_start is None:
            return

        x0, y0 = self.drag_start
        x1 = _clamp(int(event.x), 0, self.target_panel_w - 1)
        y1 = _clamp(int(event.y), 0, self.target_panel_h - 1)

        if self.preview_rect_id is None:
            self.preview_rect_id = self.right_canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                outline="#f59e0b",
                width=2,
                dash=(5, 3),
            )
        else:
            self.right_canvas.coords(self.preview_rect_id, x0, y0, x1, y1)

    def _on_mouse_up(self, event: tk.Event[Any]) -> None:
        if self.drag_start is None or self.current_region is None:
            return

        x0, y0 = self.drag_start
        x1 = _clamp(int(event.x), 0, self.target_panel_w - 1)
        y1 = _clamp(int(event.y), 0, self.target_panel_h - 1)
        self.drag_start = None

        if abs(x1 - x0) < 2 or abs(y1 - y0) < 2:
            if self.preview_rect_id is not None:
                self.right_canvas.delete(self.preview_rect_id)
                self.preview_rect_id = None
            return

        if self.preview_rect_id is not None:
            rect_id = self.preview_rect_id
            self.preview_rect_id = None
            self.right_canvas.coords(rect_id, x0, y0, x1, y1)
            self.right_canvas.itemconfig(rect_id, dash=(), outline="#f59e0b", width=2)
        else:
            rect_id = self.right_canvas.create_rectangle(x0, y0, x1, y1, outline="#f59e0b", width=2)

        box_idx = len(self.current_boxes) + 1
        tx = min(x0, x1) + 4
        ty = max(0, min(y0, y1) - 14)
        text_id = self.right_canvas.create_text(
            tx,
            ty,
            anchor="nw",
            text=f"{self.current_region.ui_name} #{box_idx}",
            fill="white",
            font=("Arial", 10, "bold"),
        )

        bbox = self._to_original_bbox(x0, y0, x1, y1)
        self.current_boxes.append(bbox)
        self.current_artists.append((rect_id, text_id))

        if len(self.current_boxes) >= self.current_region.max_boxes:
            self._set_status(
                f"{self.current_region.ui_name}: ya tienes {len(self.current_boxes)} caja(s). "
                "Ahora guarda esta zona con Enter o con el boton de continuar."
            )
        else:
            self._set_status(
                f"{self.current_region.ui_name}: caja {len(self.current_boxes)} guardada. "
                "Si necesitas otra caja, dibujala ahora. Si ya terminaste, guarda y sigue."
            )

    def _to_original_bbox(self, x0: int, y0: int, x1: int, y1: int) -> dict[str, int]:
        left = min(x0, x1)
        top = min(y0, y1)
        right = max(x0, x1)
        bottom = max(y0, y1)

        ox0 = _clamp(int(round(left / self.target_panel_scale)), 0, self.target_original_w - 1)
        oy0 = _clamp(int(round(top / self.target_panel_scale)), 0, self.target_original_h - 1)
        ox1 = _clamp(int(round(right / self.target_panel_scale)), 0, self.target_original_w - 1)
        oy1 = _clamp(int(round(bottom / self.target_panel_scale)), 0, self.target_original_h - 1)

        return {
            "x": ox0,
            "y": oy0,
            "w": max(1, ox1 - ox0),
            "h": max(1, oy1 - oy0),
        }

    def undo_last_box(self) -> None:
        if not self.current_boxes or not self.current_artists or self.current_region is None:
            return

        self.current_boxes.pop()
        rect_id, text_id = self.current_artists.pop()
        self.right_canvas.delete(rect_id)
        self.right_canvas.delete(text_id)
        self._set_status(f"{self.current_region.ui_name}: se elimino la ultima caja.")

    def complete_region(self) -> None:
        if self.current_region is None:
            return

        if not self.current_boxes:
            messagebox.showwarning(
                "Region sin marcar",
                (
                    f"{self.current_region.ui_name} no tiene cajas.\n\n"
                    "Si la zona esta tapada, corrupta o no se puede evaluar, usa 'Omitir esta zona (Q)'."
                ),
                parent=self.root,
            )
            self._set_status(
                f"{self.current_region.ui_name}: dibuja al menos una caja o usa 'Omitir esta zona (Q)'."
            )
            self.right_canvas.focus_set()
            return

        ethnicity = str(self.result["global"]["ethnicity"])

        try:
            slots = self._build_region_slots(
                self.current_region,
                self.current_boxes,
                ethnicity=ethnicity,
                show_zoom=self.zoom_preview_requested,
            )
        except RedoBoxSelection as redo_req:
            box_index = max(1, int(redo_req.box_index))
            self._remove_boxes_from_index(box_index - 1)
            self._set_status(
                f"{self.current_region.ui_name}: vuelve a dibujar desde caja {box_index}. "
                "Cuando termines, guarda esta zona otra vez."
            )
            self.right_canvas.focus_set()
            return
        except RuntimeError:
            self.cancelled = True
            self.root.destroy()
            return

        self._store_region_payload(self.current_region, slots)

        self.current_boxes = []
        self._clear_annotation_overlays()
        self._start_next_region()

    def omit_current_region(self) -> None:
        if self.current_region is None:
            return

        confirm = self._confirm_omit_region(self.current_region, len(self.current_boxes))
        if not confirm:
            self._set_status(
                f"{self.current_region.ui_name}: continuas etiquetando esta zona. "
                "Dibuja la caja y luego guarda para seguir."
            )
            self.right_canvas.focus_set()
            return

        ethnicity = str(self.result["global"]["ethnicity"])
        slots = self._build_missing_region_slots(self.current_region, ethnicity=ethnicity)
        self._store_region_payload(self.current_region, slots)

        self.current_boxes = []
        self._clear_annotation_overlays()
        self._start_next_region()

    def _store_region_payload(self, region: RegionSpec, slots: list[dict[str, Any]]) -> None:
        region_payload = {
            "label_id": region.label_id,
            "region_name": region.canonical_name,
            "region_alias": region.ui_name,
            "expected_boxes": region.max_boxes,
            "slots": slots,
        }
        self.result["regions"][region.key] = region_payload

        for slot in slots:
            self.result["all_slots"].append(
                {
                    "label_id": region.label_id,
                    "region_key": region.key,
                    "region_name": region.canonical_name,
                    "region_alias": region.ui_name,
                    **slot,
                }
            )

    def _build_missing_region_slots(self, region: RegionSpec, ethnicity: str) -> list[dict[str, Any]]:
        return [
            _build_slot_payload(
                box_index=box_index,
                bbox=None,
                score=None,
                ethnicity=ethnicity,
                omitted=True,
            )
            for box_index in range(1, region.max_boxes + 1)
        ]

    def _remove_boxes_from_index(self, index0: int) -> None:
        start = max(0, index0)
        while len(self.current_boxes) > start:
            self.current_boxes.pop()
            if self.current_artists:
                rect_id, text_id = self.current_artists.pop()
                self.right_canvas.delete(rect_id)
                self.right_canvas.delete(text_id)

        if self.preview_rect_id is not None:
            self.right_canvas.delete(self.preview_rect_id)
            self.preview_rect_id = None

    def _place_zoom_window(self, window: tk.Toplevel) -> None:
        window.update_idletasks()
        w = window.winfo_width()
        h = window.winfo_height()

        if self.zoom_window_position is not None:
            x = _clamp(self.zoom_window_position[0], 10, max(10, self.screen_w - w - 10))
            y = _clamp(self.zoom_window_position[1], 10, max(10, self.screen_h - h - 10))
        else:
            base_x = self.root.winfo_x() + self.root.winfo_width() - w - 24
            base_y = self.root.winfo_y() + 80
            x = _clamp(base_x, 10, max(10, self.screen_w - w - 10))
            y = _clamp(base_y, 10, max(10, self.screen_h - h - 10))
        window.geometry(f"+{x}+{y}")

    def _remember_zoom_window_position(self, window: tk.Toplevel) -> None:
        try:
            self.zoom_window_position = (window.winfo_x(), window.winfo_y())
        except Exception:
            pass

    def _open_zoom_window(self, bbox: dict[str, int], region_title: str, box_index: int) -> tk.Toplevel | None:
        x = int(bbox.get("x", 0))
        y = int(bbox.get("y", 0))
        w = max(1, int(bbox.get("w", 1)))
        h = max(1, int(bbox.get("h", 1)))

        x = _clamp(x, 0, self.target_original_w - 1)
        y = _clamp(y, 0, self.target_original_h - 1)
        x2 = _clamp(x + w, 1, self.target_original_w)
        y2 = _clamp(y + h, 1, self.target_original_h)
        if x2 <= x:
            x2 = min(self.target_original_w, x + 1)
        if y2 <= y:
            y2 = min(self.target_original_h, y + 1)

        crop = self.target_original_image.crop((x, y, x2, y2))
        max_w = int(self.screen_w * 0.34)
        max_h = int(self.screen_h * 0.40)
        preview_img, _scale = _resize_for_bounds(crop, max_w=max_w, max_h=max_h)
        preview_photo = ImageTk.PhotoImage(preview_img)

        win = tk.Toplevel(self.root)
        win.title(f"Zoom | {region_title} caja {box_index}")
        win.transient(self.root)
        win.resizable(False, False)
        win.configure(bg="#0b1220")

        try:
            win.attributes("-topmost", True)
        except Exception:
            pass

        container = tk.Frame(win, bg="#0b1220", padx=10, pady=10)
        container.pack(fill="both", expand=True)

        tk.Label(
            container,
            text=f"{region_title} | Caja {box_index}",
            font=("Arial", 11, "bold"),
            fg="white",
            bg="#0b1220",
            anchor="w",
        ).pack(fill="x", pady=(0, 6))

        canvas = tk.Canvas(
            container,
            width=preview_img.width,
            height=preview_img.height,
            bg="black",
            highlightthickness=1,
            highlightbackground="#94a3b8",
        )
        canvas.pack()
        canvas.create_image(0, 0, image=preview_photo, anchor="nw")
        canvas.image = preview_photo
        win.preview_photo = preview_photo

        tk.Label(
            container,
            text="Zoom activo para calificar esta caja",
            font=("Arial", 9),
            fg="#cbd5e1",
            bg="#0b1220",
            anchor="w",
        ).pack(fill="x", pady=(6, 0))

        self._place_zoom_window(win)
        win.bind("<Configure>", lambda _event: self._remember_zoom_window_position(win))
        return win

    def _ask_score_with_optional_zoom(
        self,
        region_title: str,
        prompt: str,
        box_index: int,
        bbox: dict[str, int] | None,
        show_zoom: bool,
    ) -> int:
        zoom_window: tk.Toplevel | None = None
        try:
            if show_zoom and bbox is not None:
                zoom_window = self._open_zoom_window(bbox, region_title=region_title, box_index=box_index)
            return self._ask_score_with_redo(region_title, prompt, box_index=box_index)
        finally:
            if zoom_window is not None and zoom_window.winfo_exists():
                self._remember_zoom_window_position(zoom_window)
                zoom_window.destroy()
            self._restore_main_window_after_dialog()

    def _build_region_slots(
        self,
        region: RegionSpec,
        boxes: list[dict[str, int]],
        ethnicity: str,
        show_zoom: bool = False,
    ) -> list[dict[str, Any]]:
        region_title = region.ui_name

        if region.max_boxes == 1:
            score = self._ask_score_with_optional_zoom(
                region_title=region_title,
                prompt=f"Score para {region_title} (caja 1, 0-100):",
                box_index=1,
                bbox=boxes[0],
                show_zoom=show_zoom,
            )
            return [
                _build_slot_payload(
                    box_index=1,
                    bbox=boxes[0],
                    score=score,
                    ethnicity=ethnicity,
                    omitted=False,
                )
            ]

        if len(boxes) == 1:
            score = self._ask_score_with_optional_zoom(
                region_title=region_title,
                prompt=f"Score para {region_title} (solo caja 1, 0-100):",
                box_index=1,
                bbox=boxes[0],
                show_zoom=show_zoom,
            )
            return [
                _build_slot_payload(
                    box_index=1,
                    bbox=boxes[0],
                    score=score,
                    ethnicity=ethnicity,
                    omitted=False,
                ),
                _build_slot_payload(
                    box_index=2,
                    bbox=None,
                    score=None,
                    ethnicity=ethnicity,
                    omitted=True,
                ),
            ]

        score_1 = self._ask_score_with_optional_zoom(
            region_title=region_title,
            prompt=f"Score para {region_title} caja 1 (0-100):",
            box_index=1,
            bbox=boxes[0],
            show_zoom=show_zoom,
        )
        score_2 = self._ask_score_with_optional_zoom(
            region_title=region_title,
            prompt=f"Score para {region_title} caja 2 (0-100):",
            box_index=2,
            bbox=boxes[1],
            show_zoom=show_zoom,
        )
        return [
            _build_slot_payload(
                box_index=1,
                bbox=boxes[0],
                score=score_1,
                ethnicity=ethnicity,
                omitted=False,
            ),
            _build_slot_payload(
                box_index=2,
                bbox=boxes[1],
                score=score_2,
                ethnicity=ethnicity,
                omitted=False,
            ),
        ]

    def _finish(self) -> None:
        self.output_json_path.parent.mkdir(parents=True, exist_ok=True)
        self.output_json_path.write_text(json.dumps(self.result, indent=2, ensure_ascii=False), encoding="utf-8")

        if self.close_root_on_exit:
            messagebox.showinfo(
                "Anotacion terminada",
                f"Global score: {self.result['global']['score']}\n"
                f"Etnicidad: {self.result['global']['ethnicity']}\n"
                f"JSON guardado en:\n{self.output_json_path}",
                parent=self.root,
            )
        else:
            self._set_status(f"Listo: {self.image_path.name}. Pasando a la siguiente imagen...")

        self._quit_app()

    def on_close(self) -> None:
        confirm = messagebox.askyesno(
            "Cancelar anotacion",
            "Quieres cerrar y cancelar la anotacion actual?",
            parent=self.root,
        )
        if confirm:
            self.cancelled = True
            self._quit_app()


def _reset_root_widgets(root: tk.Tk) -> None:
    for child in list(root.winfo_children()):
        child.destroy()
    try:
        root.update_idletasks()
    except Exception:
        pass


def run_external_annotation(
    image_path: str,
    output_json_path: str | None = None,
    reference_dir: str | None = None,
    root: tk.Tk | None = None,
    close_root_on_exit: bool = True,
) -> dict[str, Any]:
    owns_root = root is None
    if root is None:
        root = tk.Tk()
    else:
        _reset_root_widgets(root)
        if root.winfo_exists():
            root.deiconify()
            try:
                root.update_idletasks()
                root.lift()
            except Exception:
                pass

    app = ExternalBBoxAnnotator(
        root=root,
        image_path=image_path,
        output_json_path=output_json_path,
        reference_dir=reference_dir,
        close_root_on_exit=close_root_on_exit,
    )
    try:
        return app.run()
    finally:
        if owns_root and not close_root_on_exit and root.winfo_exists():
            root.destroy()


def _run_cli() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Anotador externo local con ventana desktop (Tkinter)")
    parser.add_argument("image_path", type=str, help="Ruta de la imagen")
    parser.add_argument("--output", type=str, default=None, help="Ruta de salida JSON")
    parser.add_argument(
        "--reference-dir",
        type=str,
        default=None,
        help="Carpeta con referencias 1_...8_... (por defecto: ./Ejemplo)",
    )
    args = parser.parse_args()

    run_external_annotation(
        image_path=args.image_path,
        output_json_path=args.output,
        reference_dir=args.reference_dir,
    )


if __name__ == "__main__":
    _run_cli()
