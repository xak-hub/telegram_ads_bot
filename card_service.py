"""card_service.py — обработка фото товара для объявления.

Убирает фон с фото локально через rembg (модель isnet-general-use по умолчанию,
см. REMBG_MODEL в .env) в двух местах:
1. Карточка-обложка: вырезанное фото рисуется на брендированной карточке
   модулем avitomat_card, готовая карточка ставится первым (главным) фото.
2. Остальные фото объявления: тот же вырез кладётся на градиентный фон (как
   у карточки) единого для всех фото размера (по первому фото), с тенью,
   сглаженными краями и логотипом — так все фото объявления выглядят
   единообразно, без исходного фона со стола/фона съёмки.
Если rembg не смог убрать фон для конкретного фото — оно остаётся как есть,
без обработки; объявление не должно срываться из-за одного неудачного фото.
"""

import asyncio
import io
import logging
import os
import tempfile
from typing import Optional, Tuple, Union

from PIL import Image, ImageFilter

from avitomat_card.card_generator import (
    CardConfig, Badge, Spec, generate_card, _linear_gradient_v, GRADIENT_TOP, GRADIENT_BOTTOM,
)
from avito_row import round_storage

logger = logging.getLogger(__name__)

# rembg — локальное удаление фона. Модель по умолчанию isnet-general-use
# (лучшее качество для объектов общего вида, включая ноутбуки; не путает
# фон/предметы за товаром с самим товаром). Сессия создаётся один раз и
# переиспользуется — иначе каждый вызов заново грузит ~170МБ модели в память.
_REMBG_MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use")
_rembg_session = None

_ICONS = os.path.join(os.path.dirname(__file__), "avitomat_card", "assets", "icons")

# Фиксированные пункты доверия (в одну линию под фото на карточке).
_BADGES = [
    (os.path.join(_ICONS, "insurance.png"), "Гарантия 6 месяцев"),
    (os.path.join(_ICONS, "loading.png"), "Свежие драйверы, базовый софт"),
    (os.path.join(_ICONS, "security.png"), "Проверен по 25 параметрам"),
]

# Иконки характеристик (сетка 2x2) — в том же порядке, что и _card_specs().
_SPEC_ICONS = [
    os.path.join(_ICONS, "spec_screen.png"),
    os.path.join(_ICONS, "spec_cpu.png"),
    os.path.join(_ICONS, "spec_ram.png"),
    os.path.join(_ICONS, "spec_gpu.png"),
]


def _get_rembg_session():
    """Лениво создаёт и кэширует сессию rembg. Первый вызов скачивает модель
    (~170МБ в ~/.u2net/) и загружает её в ONNX-runtime — занимает 10–30с,
    поэтому делаем это один раз за всё время работы бота."""
    global _rembg_session
    if _rembg_session is None:
        from rembg import new_session
        logger.info("Загружаю модель rembg %s (первый запуск может занять время)...", _REMBG_MODEL)
        _rembg_session = new_session(_REMBG_MODEL)
        logger.info("Модель rembg %s загружена", _REMBG_MODEL)
    return _rembg_session


def _remove_bg_sync(image_bytes: bytes) -> Optional[bytes]:
    """Синхронное удаление фона через rembg. Возвращает PNG-байты с прозрачным
    фоном или None при сбое. Запускается в пуле потоков через asyncio.to_thread,
    чтобы не блокировать event loop бота."""
    from rembg import remove
    inp = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = remove(inp, session=_get_rembg_session())
    # rembg.remove может вернуть PIL.Image или байты — приводим к PNG-байтам.
    if isinstance(out, Image.Image):
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    return out  # уже байты (PNG)


async def _remove_bg(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[bytes]:
    """Убирает фон локально через rembg (isnet-general-use). Возвращает PNG-байты
    с прозрачным фоном или None при сбое. mime_type оставлен для совместимости
    с контрактом, rembg сам определяет формат по содержимому."""
    try:
        return await asyncio.to_thread(_remove_bg_sync, image_bytes)
    except Exception:
        logger.exception("rembg не смог убрать фон")
        return None


# Логотип и градиент — те же, что на карточке-обложке, для единого стиля.
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "avitomat_card", "assets", "logo.png")
# Высота логотипа/отступы — в пикселях (размер фото теперь всегда фиксирован,
# см. LISTING_PHOTO_SIZE, так что дробные доли больше не нужны). Отступ сверху
# и справа — одно и то же число, чтобы логотип был строго в углу.
_LOGO_HEIGHT_FRAC = 92 / 1080
_LOGO_MARGIN = 32  # одинаковый отступ сверху и справа, в пикселях


def _compose_listing_photo(cutout_bytes: bytes, target_size: Tuple[int, int]) -> bytes:
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
                                               target_size: Tuple[int, int]) -> Optional[bytes]:
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
    удалось (ошибка rembg), остаются как есть с warning в лог — объявление не
    должно срываться из-за одного неудачного фото, но сбой не проходит
    незамеченным."""
    results = []
    failed = []
    total = len(images)
    for i, (image_bytes, mime_type) in enumerate(images, start=1):
        processed = await _remove_background_for_listing_photo(image_bytes, mime_type, LISTING_PHOTO_SIZE)
        if processed is not None:
            results.append((processed, "image/jpeg"))
        else:
            failed.append(i)
            results.append((image_bytes, mime_type))
    if failed:
        logger.warning("rembg не справился с фото %s из %s — они пойдут как есть", failed, total)
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
                     mime_type: str = "image/jpeg") -> Optional[bytes]:
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
