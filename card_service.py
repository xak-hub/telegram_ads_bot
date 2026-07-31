"""card_service.py — обработка фото товара для объявления.

Убирает фон с фото через fal.ai (BiRefNet) в двух местах:
1. Карточка-обложка: вырезанное фото рисуется на брендированной карточке
   модулем avitomat_card, готовая карточка ставится первым (главным) фото.
2. Остальные фото объявления: тот же вырез кладётся на градиентный фон (как
   у карточки) единого для всех фото размера (по первому фото), с тенью,
   сглаженными краями и логотипом — так все фото объявления выглядят
   единообразно, без исходного фона со стола/фона съёмки.
Если ключа fal.ai нет или фон убрать не удалось — соответствующее фото (или
карточка) остаётся как есть, без обработки.
"""

import asyncio
import base64
import io
import logging
import os
import tempfile

import aiohttp
from PIL import Image, ImageFilter

from avitomat_card.card_generator import (
    CardConfig, Badge, Spec, generate_card, _linear_gradient_v, GRADIENT_TOP, GRADIENT_BOTTOM,
)
from avito_row import round_storage

logger = logging.getLogger(__name__)

# fal.ai — удаление фона моделью BiRefNet (значительно лучше базового rembg на
# сложных сценах: не путает фон/предметы за товаром с самим товаром).
FAL_KEY = os.environ.get("FAL_KEY", "")
FAL_BG_REMOVAL_URL = "https://fal.run/fal-ai/birefnet"

_ICONS = os.path.join(os.path.dirname(__file__), "avitomat_card", "assets", "icons")

# Фиксированные пункты доверия (в одну линию под фото на карточке).
_BADGES = [
    (os.path.join(_ICONS, "insurance.png"), "Гарантия 6 месяцев"),
    (os.path.join(_ICONS, "security.png"), "Проверен по 25 параметрам"),
    (os.path.join(_ICONS, "loading.png"), "Свежие драйверы, базовый софт"),
]

# Иконки характеристик (сетка 2x2) — в том же порядке, что и _card_specs().
_SPEC_ICONS = [
    os.path.join(_ICONS, "spec_screen.png"),
    os.path.join(_ICONS, "spec_cpu.png"),
    os.path.join(_ICONS, "spec_ram.png"),
    os.path.join(_ICONS, "spec_gpu.png"),
]


async def _remove_bg(image_bytes: bytes, mime_type: str = "image/jpeg") -> bytes | None:
    """Убирает фон через fal.ai (BiRefNet). Фото уходит data-URI, ответ с
    sync_mode=True приходит тоже data-URI — декодируем его в PNG-байты."""
    if not FAL_KEY:
        logger.info("FAL_KEY не задан — фон не убирается")
        return None
    b64 = base64.standard_b64encode(image_bytes).decode()
    payload = {"image_url": f"data:{mime_type};base64,{b64}", "sync_mode": True}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                FAL_BG_REMOVAL_URL,
                json=payload,
                headers={"Authorization": f"Key {FAL_KEY}"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status != 200:
                    logger.warning("fal.ai BiRefNet %s: %s", resp.status, (await resp.text())[:200])
                    return None
                data = await resp.json()
    except Exception:
        logger.exception("Запрос к fal.ai не удался")
        return None

    url = (data.get("image") or {}).get("url", "")
    if not url:
        logger.warning("fal.ai BiRefNet: в ответе нет image.url: %s", str(data)[:200])
        return None

    if url.startswith("data:"):
        try:
            return base64.standard_b64decode(url.split(",", 1)[1])
        except Exception:
            logger.exception("Не удалось декодировать data-URI от fal.ai")
            return None

    # На случай, если fal вернул ссылку, а не data-URI — скачиваем.
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=40)) as r:
                if r.status != 200:
                    logger.warning("fal.ai скачивание результата %s", r.status)
                    return None
                return await r.read()
    except Exception:
        logger.exception("Не удалось скачать результат fal.ai")
        return None


# Логотип и градиент — те же, что на карточке-обложке, для единого стиля.
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "avitomat_card", "assets", "logo.png")
# Высота логотипа/отступы — в пикселях (размер фото теперь всегда фиксирован,
# см. LISTING_PHOTO_SIZE, так что дробные доли больше не нужны). Отступ сверху
# и справа — одно и то же число, чтобы логотип был строго в углу.
_LOGO_HEIGHT_FRAC = 92 / 1080
_LOGO_MARGIN = 32  # одинаковый отступ сверху и справа, в пикселях


def _compose_listing_photo(cutout_bytes: bytes, target_size: tuple[int, int]) -> bytes:
    """Кладёт вырезанное (с прозрачным фоном) фото на градиентный фон (как у
    карточки), приводит к единому для всего объявления размеру (target_size —
    размер первого фото), добавляет мягкую тень и лёгкое сглаживание краёв
    после вырезания фона, плюс логотип сверху справа. Возвращает JPEG-байты."""
    im = Image.open(io.BytesIO(cutout_bytes)).convert("RGBA")

    # Обрезаем по фактическим границам товара (без этого прозрачные поля
    # вокруг мелко снятого товара остаются в кадре, и после вписывания в
    # холст товар выглядит мелким, хотя мог бы занимать почти весь кадр).
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)

    # Сглаживание краёв — лёгкий блюр альфа-канала убирает жёсткие/рваные
    # границы, которые иногда оставляет вырезание фона.
    tw, th = target_size
    scale_factor = th / 1080
    edge_blur = max(1.0, 1.5 * scale_factor)
    im.putalpha(im.split()[3].filter(ImageFilter.GaussianBlur(edge_blur)))

    # Вписываем в целевой размер с сохранением пропорций, по максимуму (с
    # небольшим запасом по краям, чтобы тень не обрезалась).
    budget = 0.94
    fit_scale = min(tw * budget / im.width, th * budget / im.height)
    new_w, new_h = max(1, int(im.width * fit_scale)), max(1, int(im.height * fit_scale))
    im = im.resize((new_w, new_h), Image.LANCZOS)
    off_x, off_y = (tw - new_w) // 2, (th - new_h) // 2

    canvas = _linear_gradient_v((tw, th), GRADIENT_TOP, GRADIENT_BOTTOM).convert("RGBA")

    # Мягкая тень под товаром — как на карточке.
    shadow_offset = int(14 * scale_factor)
    shadow_blur = max(4, int(12 * scale_factor))
    shadow_shape = Image.new("RGBA", im.size, (0, 0, 0, 90))
    shadow_shape.putalpha(im.split()[3])
    shadow_layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    shadow_layer.paste(shadow_shape, (off_x, off_y + shadow_offset), shadow_shape)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(shadow_blur))
    canvas.alpha_composite(shadow_layer)

    canvas.alpha_composite(im, (off_x, off_y))

    # Логотип сверху справа.
    logo_h = max(24, int(_LOGO_HEIGHT_FRAC * th))
    logo_raw = Image.open(_LOGO_PATH).convert("RGBA")
    logo_raw = logo_raw.crop(logo_raw.getbbox())
    logo = logo_raw.resize((int(logo_raw.width * logo_h / logo_raw.height), logo_h), Image.LANCZOS)
    logo_x = tw - _LOGO_MARGIN - logo.width
    logo_y = _LOGO_MARGIN
    canvas.alpha_composite(logo, (logo_x, logo_y))

    buf = io.BytesIO()
    canvas.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def _remove_background_for_listing_photo(image_bytes: bytes, mime_type: str,
                                               target_size: tuple[int, int]) -> bytes | None:
    cutout = await _remove_bg(image_bytes, mime_type)
    if cutout is None:
        return None
    try:
        return await asyncio.to_thread(_compose_listing_photo, cutout, target_size)
    except Exception:
        logger.exception("Не удалось собрать итоговое фото объявления")
        return None


# Фиксированный размер для всех фото объявления (не зависит от того, какой
# формы вышло первое фото — иначе, например, портретный кадр растягивал бы
# холст всех остальных фото в высоту).
LISTING_PHOTO_SIZE = (1280, 960)


async def remove_backgrounds(images: list[tuple[bytes, str]]) -> list[tuple[bytes, str]]:
    """Прогоняет каждое фото объявления через удаление фона, приводя все фото
    к единому фиксированному размеру. Фото, для которых убрать фон не
    удалось (нет ключа, ошибка API), остаются как есть — объявление не
    должно срываться из-за одного неудачного фото."""
    results = []
    for image_bytes, mime_type in images:
        processed = await _remove_background_for_listing_photo(image_bytes, mime_type, LISTING_PHOTO_SIZE)
        results.append((processed, "image/jpeg") if processed is not None else (image_bytes, mime_type))
    return results


def _card_specs(p: dict) -> list[Spec]:
    """Ровно 4 характеристики (иконка + текст) для сетки 2x2. Пустые поля
    заменяются осмысленным фолбэком, чтобы генератор не падал."""
    screen = str(p.get("screen_size", "")).strip()
    resolution = str(p.get("screen_resolution", "")).strip()
    cpu = p.get("cpu", "").strip()
    ram = str(p.get("ram_gb", "")).strip()
    storage = str(p.get("storage_gb", "")).strip()
    gpu = p.get("gpu", "").strip()

    if screen:
        line_screen = f'Экран {screen}"'
        if resolution:
            line_screen += f", {resolution}"
    else:
        line_screen = "Ноутбук"
    line_cpu = cpu or "Процессор"

    mem = []
    if ram:
        mem.append(f"{ram}ГБ ОЗУ")
    if storage:
        mem.append(f"{round_storage(storage)}ГБ SSD")
    line_mem = " + ".join(mem) or "Память"

    line_gpu = gpu or "Видеокарта"
    texts = [line_screen, line_cpu, line_mem, line_gpu]
    return [Spec(icon, text) for icon, text in zip(_SPEC_ICONS, texts)]


def _render_card_sync(cutout_bytes: bytes, title: str, specs: list[Spec]) -> bytes:
    with tempfile.TemporaryDirectory() as d:
        cut_path = os.path.join(d, "cutout.png")
        out_path = os.path.join(d, "card.png")
        with open(cut_path, "wb") as f:
            f.write(cutout_bytes)
        generate_card(CardConfig(
            product_cutout_path=cut_path,
            title=title,
            specs=specs,
            badges=[Badge(icon, text) for icon, text in _BADGES],
            output_path=out_path,
        ))
        with open(out_path, "rb") as f:
            return f.read()


async def build_card(vision_params: dict, product_image_bytes: bytes,
                     mime_type: str = "image/jpeg") -> bytes | None:
    """Полный цикл: убрать фон + нарисовать карточку. Возвращает PNG-байты или
    None (нет ключа / не получилось). Рисование — в отдельном потоке, чтобы не
    блокировать event loop бота."""
    cutout = await _remove_bg(product_image_bytes, mime_type)
    if cutout is None:
        return None

    brand = vision_params.get("brand", "").strip()
    model = vision_params.get("model", "").strip()
    title = f"{brand} {model}".strip() or "Ноутбук"
    specs = _card_specs(vision_params)

    try:
        return await asyncio.to_thread(_render_card_sync, cutout, title, specs)
    except Exception:
        logger.exception("Генерация карточки не удалась")
        return None
