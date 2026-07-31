"""
background_removal.py — удаление фона с исходного фото товара.

Требует: pip install rembg onnxruntime

Использование:

    from background_removal import remove_background

    remove_background("raw_photo.jpg", "product_cutout.png")
"""

from pathlib import Path
from PIL import Image


def remove_background(input_path: str, output_path: str) -> str:
    """Убирает фон с фотографии, сохраняет результат как PNG с прозрачностью.
    Возвращает путь к сохранённому файлу."""
    try:
        from rembg import remove
    except ImportError as e:
        raise ImportError(
            "Для удаления фона нужен пакет rembg: pip install rembg onnxruntime"
        ) from e

    inp = Image.open(input_path).convert("RGB")
    out = remove(inp)

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path)
    return str(out_path)
