from __future__ import annotations

import base64
import json
import os
import random
import re
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
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
_DRIVE_SESSION_VERSION = 1
_DRIVE_DOWNLOAD_MODE = os.getenv("ANNOTATION_DRIVE_DOWNLOAD_MODE", "auto").strip().lower() or "auto"
_DRIVE_SERVICE_ACCOUNT_FILE = os.getenv("ANNOTATION_DRIVE_SERVICE_ACCOUNT_FILE", "").strip()
_DRIVE_DIRECT_DOWNLOAD_TIMEOUT_SECONDS = 30.0
_DRIVE_TOKEN_LOCK = threading.Lock()
_DRIVE_TOKEN_CACHE: dict[str, Any] = {"token": None, "expires_at": 0.0, "source": None}


def _env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError:
        return default


_DRIVE_PREFETCH_WINDOW = max(1, min(10, _env_int("ANNOTATION_DRIVE_PREFETCH_WINDOW", 6)))


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


@dataclass
class SelectedImageHandle:
    display_name: str
    path_supplier: Callable[[], Path]

    def get_path(self) -> Path:
        return self.path_supplier()


@dataclass
class RemoteDriveSessionState:
    session_file: Path
    source_folder_id: str
    requested_count: int
    claimed_images: list[dict[str, Any]]
    next_index: int
    loaded_from_disk: bool = False

    @property
    def total_count(self) -> int:
        return self.requested_count

    @property
    def claimed_count(self) -> int:
        return len(self.claimed_images)


def _drive_cache_dir(folder_id: str) -> Path:
    cache_dir = _project_root() / ".drive_cache" / folder_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def _drive_sessions_dir() -> Path:
    session_dir = _project_root() / ".drive_sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _drive_session_file(source_folder_id: str, requested_count: int) -> Path:
    return _drive_sessions_dir() / f"{source_folder_id}_{requested_count}.json"


def _write_json_atomic(target_path: Path, payload: dict[str, Any]) -> None:
    temp_path = target_path.with_suffix(f"{target_path.suffix}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_path.replace(target_path)


def _service_account_credentials_path() -> Path | None:
    if not _DRIVE_SERVICE_ACCOUNT_FILE:
        return None
    credentials_path = Path(_DRIVE_SERVICE_ACCOUNT_FILE).expanduser().resolve()
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"No existe ANNOTATION_DRIVE_SERVICE_ACCOUNT_FILE: {credentials_path}"
        )
    return credentials_path


def _get_service_account_access_token() -> str | None:
    credentials_path = _service_account_credentials_path()
    if credentials_path is None:
        return None

    now = time.time()
    with _DRIVE_TOKEN_LOCK:
        cached_token = str(_DRIVE_TOKEN_CACHE.get("token") or "").strip()
        cached_expiry = float(_DRIVE_TOKEN_CACHE.get("expires_at") or 0.0)
        cached_source = str(_DRIVE_TOKEN_CACHE.get("source") or "")
        if cached_token and cached_expiry - now > 60 and cached_source == str(credentials_path):
            return cached_token

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError(
            "Para descargar imagenes directo desde Drive con service account, instala `google-auth` "
            "y configura `ANNOTATION_DRIVE_SERVICE_ACCOUNT_FILE`."
        ) from exc

    credentials = service_account.Credentials.from_service_account_file(
        str(credentials_path),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    credentials.refresh(GoogleAuthRequest())
    access_token = str(credentials.token or "").strip()
    if not access_token:
        raise RuntimeError("No pude obtener access token del service account de Google Drive.")

    expiry = getattr(credentials, "expiry", None)
    expires_at = float(expiry.timestamp()) if expiry is not None else now + 3000.0

    with _DRIVE_TOKEN_LOCK:
        _DRIVE_TOKEN_CACHE["token"] = access_token
        _DRIVE_TOKEN_CACHE["expires_at"] = expires_at
        _DRIVE_TOKEN_CACHE["source"] = str(credentials_path)
    return access_token


def _serialize_remote_drive_session_state(session_state: RemoteDriveSessionState) -> dict[str, Any]:
    return {
        "version": _DRIVE_SESSION_VERSION,
        "source_folder_id": session_state.source_folder_id,
        "requested_count": session_state.requested_count,
        "next_index": session_state.next_index,
        "claimed_images": session_state.claimed_images,
    }


def _save_remote_drive_session_state(session_state: RemoteDriveSessionState) -> None:
    _write_json_atomic(
        session_state.session_file,
        _serialize_remote_drive_session_state(session_state),
    )


def _load_remote_drive_session_state(
    session_file: Path,
    source_folder_id: str,
    requested_count: int,
) -> RemoteDriveSessionState | None:
    if not session_file.exists():
        return None

    try:
        payload = json.loads(session_file.read_text(encoding="utf-8"))
    except Exception:
        return None

    claimed_images = payload.get("claimed_images", [])
    next_index = int(payload.get("next_index", 0) or 0)

    if (
        int(payload.get("version", 0) or 0) != _DRIVE_SESSION_VERSION
        or str(payload.get("source_folder_id", "")).strip() != source_folder_id
        or int(payload.get("requested_count", 0) or 0) != requested_count
        or not isinstance(claimed_images, list)
    ):
        return None

    next_index = max(0, min(next_index, len(claimed_images)))
    if next_index >= len(claimed_images):
        try:
            session_file.unlink()
        except FileNotFoundError:
            pass
        return None

    return RemoteDriveSessionState(
        session_file=session_file,
        source_folder_id=source_folder_id,
        requested_count=requested_count,
        claimed_images=claimed_images,
        next_index=next_index,
        loaded_from_disk=True,
    )


def _claim_remote_drive_session_state(
    count: int,
    source_folder_id: str,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
) -> RemoteDriveSessionState:
    resolved_webapp_url = drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL
    resolved_api_token = DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token
    session_file = _drive_session_file(source_folder_id, count)

    loaded_session = _load_remote_drive_session_state(
        session_file=session_file,
        source_folder_id=source_folder_id,
        requested_count=count,
    )
    if loaded_session is not None:
        return loaded_session

    claimed_images = _claim_remote_images_from_drive_webapp(
        count=1,
        source_folder_id=source_folder_id,
        webapp_url=resolved_webapp_url,
        api_token=resolved_api_token,
    )

    session_state = RemoteDriveSessionState(
        session_file=session_file,
        source_folder_id=source_folder_id,
        requested_count=count,
        claimed_images=claimed_images,
        next_index=0,
        loaded_from_disk=False,
    )
    _save_remote_drive_session_state(session_state)
    return session_state


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


def _write_response_to_cache(response: Any, cache_path: Path, expected_size: int) -> Path:
    temp_path = cache_path.with_suffix(f"{cache_path.suffix}.tmp")
    bytes_written = 0

    try:
        with temp_path.open("wb") as output_handle:
            while True:
                chunk = response.read(1024 * 128)
                if not chunk:
                    break
                output_handle.write(chunk)
                bytes_written += len(chunk)

        if bytes_written <= 0:
            raise RuntimeError("Drive devolvio una descarga vacia.")
        if expected_size > 0 and bytes_written != expected_size:
            raise RuntimeError(
                f"Drive devolvio {bytes_written} bytes y esperaba {expected_size}."
            )

        temp_path.replace(cache_path)
        return cache_path.resolve()
    except Exception:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _download_remote_image_from_drive_direct(
    image_info: dict[str, Any],
    source_folder_id: str,
    timeout_seconds: float = _DRIVE_DIRECT_DOWNLOAD_TIMEOUT_SECONDS,
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
    resource_key = str(image_info.get("resource_key", "") or "").strip()

    if cache_path.exists() and (expected_size <= 0 or cache_path.stat().st_size == expected_size):
        return cache_path.resolve()

    attempt_errors: list[str] = []
    download_mode = _DRIVE_DOWNLOAD_MODE

    if download_mode in {"auto", "service_account"}:
        try:
            access_token = _get_service_account_access_token()
            if access_token:
                headers = {"Authorization": f"Bearer {access_token}"}
                if resource_key:
                    headers["X-Goog-Drive-Resource-Keys"] = f"{file_id}/{resource_key}"
                request = Request(
                    url=(
                        "https://www.googleapis.com/drive/v3/files/"
                        f"{quote(file_id)}?alt=media&supportsAllDrives=true"
                    ),
                    headers=headers,
                    method="GET",
                )
                with urlopen(request, timeout=timeout_seconds) as response:
                    return _write_response_to_cache(response, cache_path, expected_size)
        except Exception as exc:
            attempt_errors.append(f"service_account={exc}")
            if download_mode == "service_account":
                raise RuntimeError(
                    "Descarga directa desde Drive fallo usando service account: "
                    + " | ".join(attempt_errors)
                ) from exc

    if download_mode in {"auto", "public"}:
        params = {"export": "download", "id": file_id}
        if resource_key:
            params["resourcekey"] = resource_key
        request = Request(
            url=f"https://drive.google.com/uc?{urlencode(params)}",
            headers={"User-Agent": "Mozilla/5.0"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                content_type = str(response.headers.get("Content-Type", "") or "").lower()
                content_disposition = str(response.headers.get("Content-Disposition", "") or "").lower()
                if "text/html" in content_type and "attachment" not in content_disposition:
                    preview = response.read(256).decode("utf-8", errors="replace")
                    raise RuntimeError(f"Drive devolvio HTML en vez de binario: {preview[:120]!r}")
                return _write_response_to_cache(response, cache_path, expected_size)
        except Exception as exc:
            attempt_errors.append(f"public={exc}")
            if download_mode == "public":
                raise RuntimeError(
                    "Descarga publica directa desde Drive fallo: " + " | ".join(attempt_errors)
                ) from exc

    if attempt_errors:
        raise RuntimeError(" | ".join(attempt_errors))
    raise RuntimeError("No hay estrategia de descarga directa disponible para Drive.")


def _download_remote_image(
    image_info: dict[str, Any],
    source_folder_id: str,
    webapp_url: str,
    api_token: str,
) -> Path:
    try:
        return _download_remote_image_from_drive_direct(
            image_info=image_info,
            source_folder_id=source_folder_id,
        )
    except Exception:
        return _download_remote_image_from_drive_webapp(
            image_info=image_info,
            source_folder_id=source_folder_id,
            webapp_url=webapp_url,
            api_token=api_token,
        )


class RemoteDrivePrefetchSession:
    def __init__(
        self,
        session_state: RemoteDriveSessionState,
        webapp_url: str,
        api_token: str,
        prefetch_window: int = _DRIVE_PREFETCH_WINDOW,
    ) -> None:
        self.session_state = session_state
        self.webapp_url = webapp_url
        self.api_token = api_token
        self.prefetch_window = max(1, prefetch_window)
        self.state_lock = threading.Lock()
        self.state_changed = threading.Condition(self.state_lock)
        self.stop_event = threading.Event()
        self.downloaded_paths: dict[int, Path] = {}
        self.prefetch_error: Exception | None = None
        self.start_index = session_state.next_index
        self._ensure_slot_ready_sync(self.start_index)
        self.prefetch_thread = threading.Thread(
            target=self._prefetch_loop,
            name="drive-image-prefetch",
            daemon=True,
        )
        self.prefetch_thread.start()

    @property
    def total_count(self) -> int:
        return self.session_state.requested_count

    @property
    def next_index(self) -> int:
        with self.state_lock:
            return self.session_state.next_index

    @property
    def remaining_count(self) -> int:
        return max(0, self.total_count - self.next_index)

    def current_image_info(self) -> dict[str, Any]:
        current_index = self.next_index
        with self.state_lock:
            if current_index >= self.total_count:
                raise IndexError("La sesion remota ya no tiene imagenes pendientes.")
            if current_index >= len(self.session_state.claimed_images):
                raise RuntimeError("La siguiente imagen remota todavia no fue reclamada.")
            return self.session_state.claimed_images[current_index]

    def current_display_name(self) -> str:
        item = self.current_image_info()
        return _safe_image_name(
            str(item.get("name", "")).strip() or str(item.get("image_id", "")).strip(),
            fallback="imagen",
        )

    def _claim_next_image(self) -> dict[str, Any]:
        claimed_images = _claim_remote_images_from_drive_webapp(
            count=1,
            source_folder_id=self.session_state.source_folder_id,
            webapp_url=self.webapp_url,
            api_token=self.api_token,
        )
        next_image = claimed_images[0]
        with self.state_changed:
            self.session_state.claimed_images.append(next_image)
            _save_remote_drive_session_state(self.session_state)
            self.state_changed.notify_all()
        return next_image

    def _ensure_claimed_image_info(self, index: int) -> dict[str, Any]:
        if index < 0 or index >= self.total_count:
            raise IndexError("Indice remoto fuera de rango.")

        while True:
            with self.state_lock:
                if index < len(self.session_state.claimed_images):
                    return self.session_state.claimed_images[index]
            self._claim_next_image()

    def _download_index(self, index: int) -> Path:
        with self.state_lock:
            cached_path = self.downloaded_paths.get(index)
            if cached_path is not None:
                return cached_path

        image_info = self._ensure_claimed_image_info(index)
        downloaded_path = _download_remote_image(
            image_info=image_info,
            source_folder_id=self.session_state.source_folder_id,
            webapp_url=self.webapp_url,
            api_token=self.api_token,
        )
        with self.state_changed:
            self.downloaded_paths[index] = downloaded_path
            self.state_changed.notify_all()
        return downloaded_path

    def _ensure_slot_ready_sync(self, index: int) -> Path:
        return self._download_index(index)

    def _next_prefetch_index(self) -> int | None:
        with self.state_lock:
            current_index = self.session_state.next_index
            target_end = min(self.total_count, current_index + self.prefetch_window)
            for index in range(current_index + 1, target_end):
                if index not in self.downloaded_paths:
                    return index
        return None

    def _prefetch_loop(self) -> None:
        while not self.stop_event.is_set():
            next_candidate = self._next_prefetch_index()
            if next_candidate is None:
                with self.state_changed:
                    self.state_changed.wait(timeout=0.25)
                continue
            try:
                self._download_index(next_candidate)
            except Exception as exc:
                with self.state_changed:
                    self.prefetch_error = exc
                    self.state_changed.notify_all()
                return

    def get_current_path(self) -> Path:
        current_index = self.next_index
        with self.state_changed:
            while True:
                ready_path = self.downloaded_paths.get(current_index)
                if ready_path is not None:
                    return ready_path
                if self.prefetch_error is not None:
                    raise RuntimeError(f"Prefetch remoto fallo: {self.prefetch_error}") from self.prefetch_error
                self.state_changed.wait(timeout=0.25)

    def mark_current_completed(self) -> None:
        with self.state_changed:
            completed_index = self.session_state.next_index
            self.downloaded_paths.pop(completed_index, None)
            self.session_state.next_index = min(self.total_count, completed_index + 1)
            is_finished = self.session_state.next_index >= self.total_count

            if not is_finished:
                _save_remote_drive_session_state(self.session_state)
            self.state_changed.notify_all()

        if is_finished:
            try:
                self.session_state.session_file.unlink()
            except FileNotFoundError:
                pass

    def close(self) -> None:
        self.stop_event.set()
        with self.state_changed:
            self.state_changed.notify_all()
        self.prefetch_thread.join(timeout=1.0)


def _start_remote_drive_prefetch_session(
    count: int,
    source_folder_id: str,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
) -> tuple[RemoteDrivePrefetchSession, str]:
    resolved_webapp_url = drive_webapp_url or DEFAULT_DRIVE_WEBAPP_URL
    resolved_api_token = DEFAULT_DRIVE_API_TOKEN if drive_api_token is None else drive_api_token
    session_state = _claim_remote_drive_session_state(
        count=count,
        source_folder_id=source_folder_id,
        drive_webapp_url=drive_webapp_url,
        drive_api_token=drive_api_token,
    )
    selection_mode = "drive_remote_resume" if session_state.loaded_from_disk else "drive_remote_session"
    return (
        RemoteDrivePrefetchSession(
            session_state=session_state,
            webapp_url=resolved_webapp_url,
            api_token=resolved_api_token,
        ),
        selection_mode,
    )


def _build_local_image_handles(selected_images: list[Path]) -> list[SelectedImageHandle]:
    return [
        SelectedImageHandle(
            display_name=image_path.name,
            path_supplier=lambda image_path=image_path: image_path,
        )
        for image_path in selected_images
    ]


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


def _annotate_remote_drive_session(
    remote_session: RemoteDrivePrefetchSession,
    selection_mode: str,
    output_json_path: str | None,
    reference_dir: str | None,
    upload_to_drive: bool,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
    fail_on_upload_error: bool,
) -> dict[str, Any]:
    pending_count = remote_session.remaining_count
    if pending_count < 1:
        return {
            "mode": "batch",
            "selection": selection_mode,
            "total_requested": 0,
            "total_completed": 0,
            "items": [],
        }

    resumed_from = remote_session.start_index
    if pending_count == 1 and remote_session.total_count == 1:
        try:
            chosen_path = remote_session.get_current_path()
            print(f"Seleccion ({selection_mode}): {remote_session.current_display_name()}")
            result = _annotate_one_image(
                image_path_obj=chosen_path,
                output_json_path=output_json_path,
                reference_dir=reference_dir,
                upload_to_drive=upload_to_drive,
                drive_webapp_url=drive_webapp_url,
                drive_api_token=drive_api_token,
                fail_on_upload_error=fail_on_upload_error,
            )
            remote_session.mark_current_completed()
            result["_session_total"] = remote_session.total_count
            result["_session_resumed_from_index"] = resumed_from + 1
            return result
        finally:
            remote_session.close()

    print(
        f"Sesion batch ({selection_mode}): {pending_count} pendiente(s) "
        f"de {remote_session.total_count} total."
    )

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

        while remote_session.remaining_count > 0:
            batch_index = len(batch_results) + 1
            current_path = remote_session.get_current_path()
            current_display_name = remote_session.current_display_name()
            print(f"[{batch_index}/{pending_count}] Anotando: {current_display_name}")

            current_result = _annotate_one_image(
                image_path_obj=current_path,
                output_json_path=None,
                reference_dir=reference_dir,
                upload_to_drive=False,
                drive_webapp_url=drive_webapp_url,
                drive_api_token=drive_api_token,
                fail_on_upload_error=fail_on_upload_error,
                shared_root=shared_root,
            )
            current_result["_batch_index"] = batch_index
            current_result["_batch_total"] = pending_count
            current_result["_session_total"] = remote_session.total_count
            current_result["_session_resumed_from_index"] = resumed_from + 1

            if upload_to_drive and upload_executor is not None:
                image_id = str(current_result.get("image_id", "")).strip() or current_path.name
                remote_filename = _build_remote_json_name(image_id)
                payload_for_upload = json.loads(json.dumps(current_result, ensure_ascii=False))
                future = upload_executor.submit(
                    _upload_json_to_drive_webapp,
                    payload_for_upload,
                    resolved_webapp_url,
                    resolved_api_token,
                    remote_filename,
                )
                upload_jobs.append((len(batch_results), remote_filename, future))
                current_result["_upload"] = {
                    "ok": None,
                    "pending": True,
                    "remote_filename": remote_filename,
                }

            batch_results.append(current_result)
            remote_session.mark_current_completed()
    finally:
        if shared_root is not None:
            try:
                if shared_root.winfo_exists():
                    shared_root.destroy()
            except Exception:
                pass
        remote_session.close()

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
        "total_requested": pending_count,
        "total_completed": len(batch_results),
        "session_total": remote_session.total_count,
        "session_resumed_from_index": resumed_from + 1,
        "items": batch_results,
    }


def _annotate_selected_images(
    selected_images: list[SelectedImageHandle],
    selection_mode: str,
    output_json_path: str | None,
    reference_dir: str | None,
    upload_to_drive: bool,
    drive_webapp_url: str | None,
    drive_api_token: str | None,
    fail_on_upload_error: bool,
    selection_executor: ThreadPoolExecutor | None = None,
) -> dict[str, Any]:
    if len(selected_images) == 1:
        try:
            chosen = selected_images[0]
            chosen_path = chosen.get_path()
            print(f"Seleccion ({selection_mode}): {chosen.display_name}")
            return _annotate_one_image(
                image_path_obj=chosen_path,
                output_json_path=output_json_path,
                reference_dir=reference_dir,
                upload_to_drive=upload_to_drive,
                drive_webapp_url=drive_webapp_url,
                drive_api_token=drive_api_token,
                fail_on_upload_error=fail_on_upload_error,
            )
        finally:
            if selection_executor is not None:
                selection_executor.shutdown(wait=False, cancel_futures=False)

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
            current_image_path = current_image.get_path()
            print(f"[{index}/{len(selected_images)}] Anotando: {current_image.display_name}")
            current_result = _annotate_one_image(
                image_path_obj=current_image_path,
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
                image_id = str(current_result.get("image_id", "")).strip() or current_image_path.name
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
        if selection_executor is not None:
            selection_executor.shutdown(wait=False, cancel_futures=False)

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

        remote_session, selection_mode = _start_remote_drive_prefetch_session(
            count=num_images,
            source_folder_id=remote_drive_folder_id,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
        )
        return _annotate_remote_drive_session(
            remote_session=remote_session,
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
            upload_to_drive=False,
            drive_webapp_url=drive_webapp_url,
            drive_api_token=drive_api_token,
            fail_on_upload_error=fail_on_upload_error,
        )
        return _annotate_selected_images(
            selected_images=_build_local_image_handles(selected_images),
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
        upload_to_drive=False,
        drive_webapp_url=drive_webapp_url,
        drive_api_token=drive_api_token,
        fail_on_upload_error=fail_on_upload_error,
    )
    return _annotate_selected_images(
        selected_images=_build_local_image_handles(selected_images),
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
