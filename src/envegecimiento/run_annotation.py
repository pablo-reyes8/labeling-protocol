from __future__ import annotations

import json
import os
import random
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# Reemplaza este valor por la URL /exec de tu deployment de Apps Script.
DEFAULT_DRIVE_WEBAPP_URL = os.getenv(
    "ANNOTATION_DRIVE_WEBAPP_URL",
    "https://script.google.com/macros/s/AKfycbw8CBryjsfHVHYgg2-qjqumY1DGO4HVgBlLTnVEjblHRnyENQDmd2vNX92szFgRzCc1xg/exec",
)
# Token simple compartido con Apps Script.
DEFAULT_DRIVE_API_TOKEN = os.getenv("ANNOTATION_DRIVE_API_TOKEN", "x94RbYlGtwNLfOiXWCsQzknrTTqY4jFv")

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _build_remote_json_name(image_id: str) -> str:
    clean = image_id.strip()
    if not clean:
        raise ValueError("No se pudo construir el nombre remoto: image_id vacio.")
    return f"{clean}.json"


def _default_output_path(image_path_obj: Path) -> Path:
    results_dir = _project_root() / "results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / f"{image_path_obj.stem}_annotations.json"


def _collect_folder_images(folder_path: Path) -> list[Path]:
    images = sorted(
        [
            p.resolve()
            for p in folder_path.iterdir()
            if p.is_file() and p.suffix.lower() in _IMAGE_EXTENSIONS
        ],
        key=lambda p: p.name.lower(),
    )
    if not images:
        raise FileNotFoundError(f"No se encontraron imagenes soportadas en: {folder_path}")
    return images


def _post_json(
    webapp_url: str,
    params: dict[str, str],
    payload: dict[str, Any],
    timeout_seconds: float = 20.0,
) -> tuple[int, str]:
    base_url = webapp_url.strip()
    if not base_url or "REEMPLAZAR_DEPLOY_ID" in base_url:
        raise RuntimeError(
            "Falta configurar la URL del Web App de Google Apps Script. "
            "Configura `DEFAULT_DRIVE_WEBAPP_URL` o la variable `ANNOTATION_DRIVE_WEBAPP_URL`."
        )

    target_url = f"{base_url}?{urlencode(params)}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        url=target_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8"},
    )

    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            response_body = response.read().decode("utf-8", errors="replace")
            status_code = int(getattr(response, "status", 200))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Error HTTP ({exc.code}) llamando Web App: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(f"No se pudo conectar al Web App de Drive: {exc.reason}") from exc

    if status_code >= 400:
        raise RuntimeError(f"Error del Web App (status {status_code}): {response_body}")

    return status_code, response_body


def _upload_json_to_drive_webapp(
    payload: dict,
    webapp_url: str,
    api_token: str,
    remote_filename: str,
    timeout_seconds: float = 20.0,
) -> dict:
    params = {"filename": remote_filename}
    if api_token:
        params["token"] = api_token

    status_code, response_body = _post_json(
        webapp_url=webapp_url,
        params=params,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )

    return {
        "status_code": status_code,
        "response_text": response_body,
        "remote_filename": remote_filename,
    }


def _claim_random_images_from_drive_webapp(
    candidate_images: list[Path],
    count: int,
    webapp_url: str,
    api_token: str,
    timeout_seconds: float = 20.0,
) -> list[Path]:
    candidate_image_ids = [p.name for p in candidate_images]
    payload: dict[str, Any] = {
        "candidate_image_ids": candidate_image_ids,
        "count": int(count),
    }

    params = {"action": "claim"}
    if api_token:
        params["token"] = api_token

    _status_code, response_body = _post_json(
        webapp_url=webapp_url,
        params=params,
        payload=payload,
        timeout_seconds=timeout_seconds,
    )

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "El Web App no devolvio JSON en action=claim. "
            "Revisa que actualizaste/deployaste `drive_webapp_receiver.gs` con soporte de claims. "
            f"Respuesta: {response_body[:200]}"
        ) from exc

    if not bool(parsed.get("ok", False)):
        raise RuntimeError(f"Claim remoto fallo: {parsed}")

    claimed_ids = parsed.get("claimed_image_ids", [])
    if not isinstance(claimed_ids, list):
        raise RuntimeError(f"Respuesta invalida en claim remoto: {parsed}")

    path_by_name = {p.name: p for p in candidate_images}
    claimed_paths = [path_by_name[name] for name in claimed_ids if name in path_by_name]

    if len(claimed_paths) < count:
        available = int(parsed.get("available_count", len(claimed_paths)))
        raise ValueError(
            f"Solo hay {available} imagen(es) disponibles para etiquetar sin repetir; pediste {count}."
        )

    return claimed_paths


def _pick_random_local_images(candidate_images: list[Path], count: int) -> list[Path]:
    if count > len(candidate_images):
        raise ValueError(
            f"Pediste {count} imagenes pero en la carpeta solo hay {len(candidate_images)}."
        )
    rng = random.SystemRandom()
    return rng.sample(candidate_images, count)


def _select_random_images(
    candidate_images: list[Path],
    count: int,
    upload_to_drive: bool,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
    fail_on_upload_error: bool,
) -> tuple[list[Path], str]:
    if upload_to_drive:
        try:
            selected = _claim_random_images_from_drive_webapp(
                candidate_images=candidate_images,
                count=count,
                webapp_url=drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL,
                api_token=DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token,
            )
            return selected, "claimed_random"
        except Exception as exc:
            if fail_on_upload_error:
                raise
            print(f"Claim remoto fallo ({exc}). Continuo con seleccion aleatoria local.")
            return _pick_random_local_images(candidate_images, count), "local_random"

    return _pick_random_local_images(candidate_images, count), "local_random"


def _annotate_one_image(
    image_path_obj: Path,
    output_json_path: str | Path | None,
    reference_dir: str | None,
    upload_to_drive: bool,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
    fail_on_upload_error: bool,
    shared_root: Any | None = None,
) -> dict[str, Any]:
    if output_json_path is None:
        output_path = _default_output_path(image_path_obj)
    else:
        output_path = Path(output_json_path).expanduser().resolve()
        output_path.parent.mkdir(parents=True, exist_ok=True)

    from .external_bbox_annotator import run_external_annotation

    run_external_annotation(
        image_path=str(image_path_obj),
        output_json_path=str(output_path),
        reference_dir=reference_dir,
        root=shared_root,
        close_root_on_exit=shared_root is None,
    )

    result = json.loads(output_path.read_text(encoding="utf-8"))
    result["_local_json_path"] = str(output_path)

    if upload_to_drive:
        image_id = str(result.get("image_id", "")).strip() or image_path_obj.name
        remote_filename = _build_remote_json_name(image_id)
        webapp_url = drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL
        api_token = DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token
        try:
            upload_info = _upload_json_to_drive_webapp(
                payload=result,
                webapp_url=webapp_url,
                api_token=api_token,
                remote_filename=remote_filename,
            )
            result["_upload"] = {"ok": True, **upload_info}
        except Exception as exc:
            result["_upload"] = {"ok": False, "error": str(exc), "remote_filename": remote_filename}
            if fail_on_upload_error:
                raise

    return result


def run(
    image_path: str,
    num_images: int = 1,
    output_json_path: str | None = None,
    prefer_external_window: bool = True,
    reference_dir: str | None = None,
    upload_to_drive: bool = True,
    drive_webapp_url: str | None = None,
    drive_api_token: str | None = None,
    fail_on_upload_error: bool = True,
):
    """Anota 1 o N imagenes y sube cada JSON.

    `image_path` puede ser:
    - Ruta de una imagen concreta.
    - Ruta de carpeta (ej: "data") para seleccion aleatoria.
    """
    _ = prefer_external_window

    input_path = Path(image_path).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"No existe la ruta: {input_path}")

    if num_images < 1:
        raise ValueError("`num_images` debe ser >= 1.")

    input_is_file = input_path.is_file()
    input_is_dir = input_path.is_dir()
    if not input_is_file and not input_is_dir:
        raise ValueError(f"La ruta debe ser imagen o carpeta: {input_path}")

    if input_is_file and num_images == 1:
        return _annotate_one_image(
            image_path_obj=input_path,
            output_json_path=output_json_path,
            reference_dir=reference_dir,
            upload_to_drive=upload_to_drive,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
            fail_on_upload_error=fail_on_upload_error,
        )

    if output_json_path is not None and num_images > 1:
        raise ValueError("`output_json_path` solo aplica cuando `num_images=1`.")

    source_folder = input_path.parent if input_is_file else input_path
    candidate_images = _collect_folder_images(source_folder)

    if num_images == 1:
        selected_images, selection_mode = _select_random_images(
            candidate_images=candidate_images,
            count=1,
            upload_to_drive=upload_to_drive,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
            fail_on_upload_error=fail_on_upload_error,
        )
        chosen = selected_images[0]
        print(f"Seleccion aleatoria ({selection_mode}): {chosen.name}")
        return _annotate_one_image(
            image_path_obj=chosen,
            output_json_path=output_json_path,
            reference_dir=reference_dir,
            upload_to_drive=upload_to_drive,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
            fail_on_upload_error=fail_on_upload_error,
        )

    selected_images, selection_mode = _select_random_images(
        candidate_images=candidate_images,
        count=num_images,
        upload_to_drive=upload_to_drive,
        drive_webapp_url=drive_webapp_url,
        drive_api_token=drive_api_token,
        fail_on_upload_error=fail_on_upload_error,
    )
    print(f"Seleccion batch ({selection_mode}): {len(selected_images)} imagen(es)")

    batch_results: list[dict[str, Any]] = []
    shared_root: Any | None = None

    upload_jobs: list[tuple[int, str, Future[dict]]] = []
    upload_executor: ThreadPoolExecutor | None = None
    resolved_webapp_url = drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL
    resolved_api_token = DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token

    if upload_to_drive:
        upload_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="json-upload")

    try:
        import tkinter as tk

        shared_root = tk.Tk()
        shared_root.withdraw()

        for index, current_image in enumerate(selected_images, start=1):
            print(f"[{index}/{len(selected_images)}] Anotando: {current_image.name}")
            current_result = _annotate_one_image(
                image_path_obj=current_image,
                output_json_path=None,
                reference_dir=reference_dir,
                upload_to_drive=False,
                drive_webapp_url=drive_webapp_url,
                drive_api_token=drive_api_token,
                fail_on_upload_error=fail_on_upload_error,
                shared_root=shared_root,
            )
            current_result["_batch_index"] = index
            current_result["_batch_total"] = len(selected_images)

            if upload_to_drive and upload_executor is not None:
                image_id = str(current_result.get("image_id", "")).strip() or current_image.name
                remote_filename = _build_remote_json_name(image_id)

                payload_for_upload = json.loads(json.dumps(current_result, ensure_ascii=False))
                future = upload_executor.submit(
                    _upload_json_to_drive_webapp,
                    payload_for_upload,
                    resolved_webapp_url,
                    resolved_api_token,
                    remote_filename,
                )
                upload_jobs.append((index - 1, remote_filename, future))
                current_result["_upload"] = {
                    "ok": None,
                    "pending": True,
                    "remote_filename": remote_filename,
                }

            batch_results.append(current_result)
    finally:
        if shared_root is not None:
            try:
                if shared_root.winfo_exists():
                    shared_root.destroy()
            except Exception:
                pass

    upload_errors: list[str] = []
    if upload_executor is not None:
        upload_executor.shutdown(wait=True)
        for item_index, remote_filename, future in upload_jobs:
            item = batch_results[item_index]
            try:
                info = future.result()
                item["_upload"] = {"ok": True, **info}
            except Exception as exc:
                item["_upload"] = {
                    "ok": False,
                    "error": str(exc),
                    "remote_filename": remote_filename,
                }
                upload_errors.append(f"{remote_filename}: {exc}")

    if upload_errors and fail_on_upload_error:
        summary = " | ".join(upload_errors[:3])
        raise RuntimeError(f"Fallaron subidas remotas en batch: {summary}")

    return {
        "mode": "batch",
        "selection": selection_mode,
        "total_requested": num_images,
        "total_completed": len(batch_results),
        "items": batch_results,
    }


def run_and_upload(
    image_path: str,
    num_images: int = 1,
    output_json_path: str | None = None,
    prefer_external_window: bool = True,
    reference_dir: str | None = None,
):
    """Wrapper para mantener pipeline identico y forzar subida remota."""
    return run(
        image_path=image_path,
        num_images=num_images,
        output_json_path=output_json_path,
        prefer_external_window=prefer_external_window,
        reference_dir=reference_dir,
        upload_to_drive=True,
    )
