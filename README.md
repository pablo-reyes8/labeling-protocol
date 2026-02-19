# Etiquetado De Imagenes Para Modelo De Envejecimiento

Este repositorio es para que varias personas etiqueten fotos con bounding boxes.
Cada etiquetado se guarda en JSON y se sube a Drive (solo JSON, nunca la imagen).

## Antes De Empezar

1. Abre una terminal en esta carpeta.
2. Instala dependencias:

```bash
pip install -r requirements.txt
```

## Opcion Recomendada (Jupyter)

Archivo: `annotate_jupyter.ipynb`

1. Abre el notebook.
2. Ejecuta las celdas en orden.
3. Cambia solo estos valores cuando te lo pida:
   - Ruta de imagen o carpeta (por ejemplo `data`)
   - Cantidad de imagenes a etiquetar
4. Etiqueta y guarda cuando aparezca la ventana.

## Opcion IDE (Script Python)

Archivo: `annotate_ide.py`

1. Abre `annotate_ide.py`.
2. Cambia:
   - `IMAGE_PATH` (ejemplo: `r"data"`)
   - `NUM_IMAGES` (cuantas quieres etiquetar)
3. Ejecuta:

```bash
python annotate_ide.py
```

## Opcion Consola (CLI)

Archivo: `annotate_cli.py`

Ejemplo:

```bash
python annotate_cli.py data --count 2
```

Si quieres una sola imagen especifica:

```bash
python annotate_cli.py "data/mi_imagen.jpg"
```

## Que Se Guarda

- JSON local en `results/`
- JSON remoto en Drive con nombre de la imagen
- Preview de boxes en `data_boxes/`

Ejemplo de preview ya generado:

- `data_boxes/portrait-white-man-isolated_boxes_preview.jpg`

## Si Algo Falla

- Revisa que corriste `pip install -r requirements.txt`.
- Revisa que estas ejecutando desde la carpeta raiz del proyecto.
