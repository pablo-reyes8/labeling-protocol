# Guia Rapida De Etiquetado Facial

Bienvenido/a. Esta guia es para personas que van a **etiquetar imagenes** para apoyar un modelo de Deep Learning de envejecimiento.

Solo necesitas abrir el anotador, marcar las cajas y guardar. El sistema genera un JSON y lo sube a Drive.

## Asi Se Ve Una Anotacion 🖼️

![Ejemplo de anotacion 1](data_boxes/confident-woman-business-owener-wearing-apron-face-portrait_boxes_preview.jpg)

![Ejemplo de anotacion 2](data_boxes/smiling-businessman-face-portrait-wearing-suit_boxes_preview.jpg)

## Inicio En 2 Minutos ⚡

1. Abre terminal en esta carpeta.
2. Instala dependencias:

```bash
pip install -r requirements.txt
```

3. Abre `annotate_jupyter.ipynb` y ejecuta las celdas.

## Opcion Recomendada: Jupyter

En el notebook usa estos chunks.

### Chunk 1: Cargar modulos

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

### Chunk 2: Ejecutar etiquetado

```python
NUM_IMAGES = 2  # cambia este numero

result = run_annotation.run(
    "data",          # carpeta con imagenes o ruta de una imagen
    num_images=NUM_IMAGES,
)
result
```

### Chunk 3 (opcional): Ver preview de un JSON

```python
from envegecimiento.preview_boxes_from_json import show_boxes_from_json

preview = show_boxes_from_json("results/tu_archivo_annotations.json")
preview
```

## Opcion 2: Script En IDE

Archivo: `annotate_ide.py`

1. Cambia `IMAGE_PATH`.
2. Cambia `NUM_IMAGES`.
3. Ejecuta:

```bash
python annotate_ide.py
```

## Opcion 3: Consola (CLI)

```bash
python annotate_cli.py data --count 2
```

Para una imagen especifica:

```bash
python annotate_cli.py "data/mi_imagen.jpg"
```

## Que Se Guarda

- JSON local en `results/`
- JSON remoto en Drive (nombre = nombre de imagen)
- Preview en `data_boxes/`

## Si Algo Falla

1. Ejecuta de nuevo `pip install -r requirements.txt`.
2. Verifica que estas en la carpeta raiz del proyecto.
