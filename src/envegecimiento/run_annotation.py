from __future__ import annotations

import base64
import json
import os
import random
import re
import socket
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
DEFAULT_SOURCE_DRIVE_FOLDER_ID = os.getenv(
    "ANNOTATION_SOURCE_DRIVE_FOLDER_ID",
    "1LN7VNGPRz6kZN5a_VvbOCkbMXG6y7F3u",
)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}
_DRIVE_FOLDER_URL_RE = re.compile(r"/folders/([a-zA-Z0-9_-]+)")
_DRIVE_FOLDER_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{10,}$")


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


def _drive_cache_dir(folder_id: str) -> Path:
    cache_dir = _project_root() / ".drive_cache" / folder_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _safe_image_name(raw_name: str, fallback: str) -> str:
    candidate = Path(str(raw_name or "")).name.strip()
    if not candidate:
        candidate = fallback
    return candidate.replace("/", "_").replace("\\", "_")


def _extract_drive_folder_id(raw_value: str) -> str | None:
    value = str(raw_value or "").strip()
    if not value:
        return None

    lowered = value.lower()
    if lowered in {"drive", "gdrive", "google-drive", "remote"}:
        return DEFAULT_SOURCE_DRIVE_FOLDER_ID.strip() or None

    match = _DRIVE_FOLDER_URL_RE.search(value)
    if match:
        return match.group(1)

    if value.startswith("drive://"):
        folder_id = value[len("drive://") :].strip().strip("/")
        return folder_id or None

    if _DRIVE_FOLDER_ID_RE.fullmatch(value):
        return value

    return None


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
    except TimeoutError as exc:
        raise RuntimeError(
            f"El Web App de Drive no respondio dentro de {timeout_seconds:.0f}s. "
            "Si acabas de cambiar Apps Script, revisa que este redeployado y que la accion no este tardando demasiado."
        ) from exc
    except socket.timeout as exc:
        raise RuntimeError(
            f"El Web App de Drive excedio el timeout de {timeout_seconds:.0f}s."
        ) from exc

    if status_code >= 400:
        raise RuntimeError(f"Error del Web App (status {status_code}): {response_body}")

    return status_code, response_body


def _post_drive_json_action(
    action: str,
    payload: dict[str, Any],
    webapp_url: str,
    api_token: str,
    timeout_seconds: float = 20.0,
) -> dict[str, Any]:
    params = {"action": action}
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
        if response_body.strip().startswith("ERROR:"):
            raise RuntimeError(
                f"Apps Script devolvio un error en action={action}: {response_body.strip()}"
            ) from exc
        raise RuntimeError(
            f"El Web App no devolvio JSON en action={action}. "
            "Revisa que actualizaste/deployaste `drive_webapp_receiver.gs`. "
            f"Respuesta: {response_body[:200]}"
        ) from exc

    if not bool(parsed.get("ok", False)):
        raise RuntimeError(f"Action remota '{action}' fallo: {parsed}")

    return parsed


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
    parsed = _post_drive_json_action(
        action="claim",
        payload={
        "candidate_image_ids": candidate_image_ids,
        "count": int(count),
        },
        webapp_url=webapp_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
    )

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


def _claim_remote_images_from_drive_webapp(
    count: int,
    source_folder_id: str,
    webapp_url: str,
    api_token: str,
    timeout_seconds: float = 90.0,
) -> list[dict[str, Any]]:
    parsed = _post_drive_json_action(
        action="claim_remote",
        payload={
            "count": int(count),
            "source_folder_id": source_folder_id,
        },
        webapp_url=webapp_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
    )

    claimed_images = parsed.get("claimed_images", [])
    if not isinstance(claimed_images, list):
        raise RuntimeError(f"Respuesta invalida en claim_remote: {parsed}")

    if len(claimed_images) < count:
        available = int(parsed.get("available_count", len(claimed_images)))
        raise ValueError(
            f"Solo hay {available} imagen(es) disponibles en Drive para etiquetar; pediste {count}."
        )

    return claimed_images


def _download_remote_image_from_drive_webapp(
    image_info: dict[str, Any],
    source_folder_id: str,
    webapp_url: str,
    api_token: str,
    timeout_seconds: float = 60.0,
) -> Path:
    file_id = str(image_info.get("file_id", "")).strip()
    if not file_id:
        raise RuntimeError(f"Claim remoto invalido: falta file_id. Payload: {image_info}")

    image_name = _safe_image_name(
        str(image_info.get("name", "")).strip() or str(image_info.get("image_id", "")).strip(),
        fallback=f"{file_id}.bin",
    )
    cache_path = _drive_cache_dir(source_folder_id) / image_name

    expected_size = int(image_info.get("size_bytes", 0) or 0)
    if cache_path.exists() and (expected_size <= 0 or cache_path.stat().st_size == expected_size):
        return cache_path.resolve()

    parsed = _post_drive_json_action(
        action="download_image",
        payload={
            "file_id": file_id,
            "source_folder_id": source_folder_id,
        },
        webapp_url=webapp_url,
        api_token=api_token,
        timeout_seconds=timeout_seconds,
    )

    base64_data = str(parsed.get("base64_data", "")).strip()
    if not base64_data:
        raise RuntimeError(f"download_image no devolvio base64_data para {image_name}.")

    try:
        raw_bytes = base64.b64decode(base64_data)
    except Exception as exc:
        raise RuntimeError(f"No pude decodificar la imagen descargada desde Drive: {image_name}") from exc

    cache_path.write_bytes(raw_bytes)
    return cache_path.resolve()


def _select_remote_drive_images(
    count: int,
    source_folder_id: str,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
) -> tuple[list[Path], str]:
    resolved_webapp_url = drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL
    resolved_api_token = DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token

    claimed_images = _claim_remote_images_from_drive_webapp(
        count=count,
        source_folder_id=source_folder_id,
        webapp_url=resolved_webapp_url,
        api_token=resolved_api_token,
    )

    downloaded_paths = [
        _download_remote_image_from_drive_webapp(
            image_info=item,
            source_folder_id=source_folder_id,
            webapp_url=resolved_webapp_url,
            api_token=resolved_api_token,
        )
        for item in claimed_images
    ]

    return downloaded_paths, "drive_remote"


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


def _annotate_selected_images(
    selected_images: list[Path],
    selection_mode: str,
    output_json_path: str | None,
    reference_dir: str | None,
    upload_to_drive: bool,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
    fail_on_upload_error: bool,
) -> dict[str, Any]:
    if len(selected_images) == 1:
        chosen = selected_images[0]
        print(f"Seleccion ({selection_mode}): {chosen.name}")
        return _annotate_one_image(
            image_path_obj=chosen,
            output_json_path=output_json_path,
            reference_dir=reference_dir,
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
        "total_requested": len(selected_images),
        "total_completed": len(batch_results),
        "items": batch_results,
    }


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
    - "drive", un ID de carpeta o una URL de carpeta de Google Drive.
    """
    _ = prefer_external_window

    if num_images < 1:
        raise ValueError("`num_images` debe ser >= 1.")

    remote_drive_folder_id = _extract_drive_folder_id(image_path)
    if remote_drive_folder_id is not None:
        if output_json_path is not None and num_images > 1:
            raise ValueError("`output_json_path` solo aplica cuando `num_images=1`.")

        selected_images, selection_mode = _select_remote_drive_images(
            count=num_images,
            source_folder_id=remote_drive_folder_id,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
        )
        return _annotate_selected_images(
            selected_images=selected_images,
            selection_mode=selection_mode,
            output_json_path=output_json_path,
            reference_dir=reference_dir,
            upload_to_drive=upload_to_drive,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
            fail_on_upload_error=fail_on_upload_error,
        )

    input_path = Path(image_path).expanduser().resolve()
    if not input_path.exists():
        raise FileNotFoundError(f"No existe la ruta: {input_path}")

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
        return _annotate_selected_images(
            selected_images=selected_images,
            selection_mode=selection_mode,
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
    return _annotate_selected_images(
        selected_images=selected_images,
        selection_mode=selection_mode,
        output_json_path=output_json_path,
        reference_dir=reference_dir,
        upload_to_drive=upload_to_drive,
        drive_webapp_url=drive_webapp_url,
        drive_api_token=drive_api_token,
        fail_on_upload_error=fail_on_upload_error,
    )


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
