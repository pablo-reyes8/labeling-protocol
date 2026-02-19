# Protocolo de etiquetado (bbox)

Proyecto simple para etiquetar imagenes con bounding boxes, guardar JSON local y subir automaticamente ese JSON a Google Drive (sin subir la imagen).

## Que hace

- Abre una interfaz para etiquetar una imagen.
- Guarda el resultado en `results/*.json`.
- Sube el JSON al Web App de Google Apps Script (Drive).
- Si pasas una carpeta (`data`) y `num_images > 1`, selecciona imagenes aleatorias.
- Con upload activo, intenta reservar imagenes en Drive para reducir duplicados entre usuarios.

## Requisitos

- Python 3.10+
- `pip`

## Instalacion (una sola vez)

```bash
pip install -r requirements.txt
```

## Estructura importante

- `annotate_jupyter.ipynb`: flujo para Jupyter.
- `annotate_ide.py`: flujo para correr en IDE.
- `annotate_cli.py`: flujo por consola.
- `src/envegecimiento/run_annotation.py`: pipeline principal.
- `src/envegecimiento/preview_boxes_from_json.py`: dibuja boxes desde JSON.
- `results/`: JSON locales.
- `data_boxes/`: previews con boxes (se crea automatico).

## 1) Uso en Jupyter

Instala dependencias primero (si no lo hiciste):

```bash
pip install -r requirements.txt
```

En una celda:

```python
import importlib
import sys
from pathlib import Path

SRC_DIR = Path("src").resolve()
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import envegecimiento.external_bbox_annotator as external_bbox_annotator
import envegecimiento.run_annotation as run_annotation

importlib.reload(external_bbox_annotator)
importlib.reload(run_annotation)
```

Ejemplo de corrida:

```python
NUM_IMAGES = 2
result = run_annotation.run("data", num_images=NUM_IMAGES)
result
```

Preview desde un JSON:

```python
from envegecimiento.preview_boxes_from_json import show_boxes_from_json

preview = show_boxes_from_json("results/tu_archivo_annotations.json")
preview
```

## 2) Uso en IDE (script)

Instala dependencias primero (si no lo hiciste):

```bash
pip install -r requirements.txt
```

Edita en `annotate_ide.py`:

- `IMAGE_PATH = r"data"` (carpeta o imagen)
- `NUM_IMAGES = 1` (o mas)

Luego ejecuta:

```bash
python annotate_ide.py
```

## 3) Uso por consola (CLI)

Instala dependencias primero (si no lo hiciste):

```bash
pip install -r requirements.txt
```

Ejemplos:

```bash
python annotate_cli.py data --count 3
python annotate_cli.py "data/mi_imagen.jpg"
python annotate_cli.py data --count 2 --no-upload
```

## Parametros principales

En `run_annotation.run(...)`:

- `image_path` (str): ruta de imagen o carpeta.
- `num_images` (int): cuantas imagenes etiquetar en secuencia.
- `output_json_path` (str | None): solo para `num_images=1`.
- `reference_dir` (str | None): carpeta de referencias (opcional).
- `upload_to_drive` (bool): si sube o no el JSON.
- `drive_webapp_url` (str | None): URL `/exec` de Apps Script.
- `drive_api_token` (str | None): token compartido con Apps Script.
- `fail_on_upload_error` (bool): si corta el flujo cuando falla upload.

## Variables de entorno opcionales

Puedes definirlas para no hardcodear en codigo:

```bash
# Linux/Mac
export ANNOTATION_DRIVE_WEBAPP_URL="https://script.google.com/macros/s/XXXX/exec"
export ANNOTATION_DRIVE_API_TOKEN="tu_token"
```

```powershell
# Windows PowerShell
setx ANNOTATION_DRIVE_WEBAPP_URL "https://script.google.com/macros/s/XXXX/exec"
setx ANNOTATION_DRIVE_API_TOKEN "tu_token"
```

## Salidas

- JSON local: `results/<nombre_imagen>_annotations.json`
- JSON remoto en Drive: `<nombre_imagen>.json`
- Preview de boxes: `data_boxes/<nombre_imagen>_boxes_preview.jpg`
