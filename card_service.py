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
import base64
import io
import json
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

# Удаление фона — локально. Движок выбирается через BG_ENGINE в .env:
#   "rembg"                  — isnet-general-use (быстро ~3с/фото, среднее качество)
#   "transparent-background" — InSPyReNet (медленно ~36с/фото, качество выше)
#   "bria"                   — Bria RMBG 2.0 через fal.ai (~3.5с/фото, SOTA-качество, $0.018/фото)
#   "birefnet"               — BiRefNet через fal.ai (~1.6с/фото, высокое качество, дешевле)
# Модель rembg настраивается отдельно через REMBG_MODEL (по умолчанию isnet-general-use).
_BG_ENGINE = os.environ.get("BG_ENGINE", "rembg").lower()
_REMBG_MODEL = os.environ.get("REMBG_MODEL", "isnet-general-use")
_rembg_session = None
_tb_remover = None  # ленивый инстанс transparent-background (InSPyReNet)

# fal.ai — облачные модели удаления фона.
FAL_KEY = os.environ.get("FAL_KEY", "")
FAL_URLS = {
    "bria":     "https://fal.run/fal-ai/bria/background/remove",
    "birefnet": "https://fal.run/fal-ai/birefnet",
}

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


def _remove_bg_sync_rembg(image_bytes: bytes) -> Optional[bytes]:
    """Удаление фона через rembg (isnet-general-use). Быстро (~3с/фото),
    среднее качество."""
    from rembg import remove
    inp = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = remove(inp, session=_get_rembg_session())
    if isinstance(out, Image.Image):
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()
    return out  # уже байты (PNG)


def _get_tb_remover():
    """Лениво создаёт инстанс transparent-background (InSPyReNet). Первый вызов
    скачивает модель (~170МБ) и загружает PyTorch — занимает ~10с."""
    global _tb_remover
    if _tb_remover is None:
        from transparent_background import Remover
        logger.info("Загружаю модель InSPyReNet (transparent-background)...")
        _tb_remover = Remover()
        logger.info("Модель InSPyReNet загружена")
    return _tb_remover


def _remove_bg_sync_inspyrenet(image_bytes: bytes) -> Optional[bytes]:
    """Удаление фона через InSPyReNet (transparent-background). Медленно (~36с/фото
    на CPU), но качество выше, чем у isnet."""
    remover = _get_tb_remover()
    inp = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    out = remover.process(inp)
    buf = io.BytesIO()
    # process() возвращает RGBA-изображение с прозрачным фоном
    out.save(buf, format="PNG")
    return buf.getvalue()


def _remove_bg_sync_fal(image_bytes: bytes) -> Optional[bytes]:
    """Удаление фона через fal.ai API (Bria RMBG 2.0 или BiRefNet — по BG_ENGINE).
    Возвращает PNG-байты с прозрачным фоном или None."""
    if not FAL_KEY:
        logger.warning("FAL_KEY не задан — fal.ai недоступен")
        return None
    if _BG_ENGINE not in FAL_URLS:
        logger.warning("Неизвестный fal-движок: %s", _BG_ENGINE)
        return None
    import urllib.request
    b64 = base64.standard_b64encode(image_bytes).decode()
    payload = json.dumps({"image_url": f"data:image/png;base64,{b64}"}).encode()
    req = urllib.request.Request(
        FAL_URLS[_BG_ENGINE], data=payload,
        headers={"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except Exception:
        logger.exception("fal.ai (%s) запрос не удался", _BG_ENGINE)
        return None

    url = (data.get("image") or {}).get("url", "")
    if not url:
        logger.warning("fal.ai %s: в ответе нет image.url: %s", _BG_ENGINE, str(data)[:200])
        return None

    if url.startswith("data:"):
        try:
            return base64.standard_b64decode(url.split(",", 1)[1])
        except Exception:
            logger.exception("fal.ai %s: не удалось декодировать data-URI", _BG_ENGINE)
            return None

    # fal вернул ссылку на результат — скачиваем
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return r.read()
    except Exception:
        logger.exception("fal.ai %s: не удалось скачать результат", _BG_ENGINE)
        return None


def _remove_bg_sync(image_bytes: bytes) -> Optional[bytes]:
    """Синхронное удаление фона выбранным движком (BG_ENGINE). Возвращает PNG-байты
    с прозрачным фоном или None при сбое."""
    if _BG_ENGINE in ("bria", "birefnet"):
        return _remove_bg_sync_fal(image_bytes)
    if _BG_ENGINE == "transparent-background":
        return _remove_bg_sync_inspyrenet(image_bytes)
    return _remove_bg_sync_rembg(image_bytes)


def _strip_attached_table(im: "Image.Image") -> "Image.Image":
    """Срезает «нижнюю плиту» — кусок стола/поверхности, приклеенный моделью
    вырезки к нижней части товара. Стол — это горизонтальная плита снизу
    силуэта, по цвету заметно отличающаяся от корпуса. Идём от самой нижней
    строки силуэта вверх: пока большинство непрозрачных пикселей ряда
    «чужого» цвета (L1 до цвета корпуса >= 90) — ряд срезаем целиком,
    останавливаемся на первом ряду корпуса. Цвет корпуса — медиана пикселей
    центральной полосы силуэта (сам товар). Предохранители: не срезаем
    больше 45% объекта; игнорируем «чужие» ряды, если их суммарно < 3% (шум
    края) — чтобы не трогать чистые вырезки."""
    try:
        import numpy as np
    except ImportError:
        return im
    try:
        arr = np.array(im)
        alpha = arr[..., 3]
        opaque = alpha > 100
        ys, _ = np.where(opaque)
        if ys.size == 0:
            return im
        y0, y1 = int(ys.min()), int(ys.max())
        h_obj = y1 - y0 + 1
        if h_obj < 20:
            return im

        rgb = arr[..., :3].astype(float)
        # Цвет корпуса: медиана пикселей центральной полосы силуэта.
        band = opaque[y0 + int(0.25 * h_obj): y1 - int(0.25 * h_obj) + 1]
        band_rgb = rgb[y0 + int(0.25 * h_obj): y1 - int(0.25 * h_obj) + 1][band]
        if band_rgb.size == 0:
            return im
        core = np.median(band_rgb, axis=0)

        def row_is_foreign(y: int) -> bool:
            row = opaque[y]
            n = int(row.sum())
            if n < 5:
                return False
            px = rgb[y][row]
            similar = (np.abs(px - core).sum(axis=1) < 90)
            return similar.mean() < 0.35  # <35% пикселей ряда — цвет корпуса

        # Идём снизу вверх, собираем «чужие» ряды (срез), до первого ряда корпуса.
        cut_rows = []
        for y in range(y1, y0, -1):
            if row_is_foreign(y):
                cut_rows.append(y)
            else:
                break
        if not cut_rows:
            return im
        n_cut = sum(int(opaque[y].sum()) for y in cut_rows)
        if n_cut > 0.45 * opaque.sum() or n_cut < 0.03 * opaque.sum():
            return im  # слишком много (это не стол) или шум края — не трогаем

        new_alpha = alpha.copy()
        for y in cut_rows:
            new_alpha[y][opaque[y]] = 0
        logger.info("Срез нижней плиты: убрано %d px в %d рядах (до y=%d)",
                    n_cut, len(cut_rows), cut_rows[0])
        im.putalpha(Image.fromarray(new_alpha.astype(np.uint8), "L"))
        return im
    except Exception:
        logger.exception("Срез нижней плиты не удался — отдаю как есть")
        return im


def _strip_stray_blobs(cutout_bytes: bytes) -> bytes:
    """Убирает «отвалившиеся» куски, которые модель вырезки прихватила вместе
    с товаром (куски стола, тени-обрывки, мусор): в альфа-канале оставляем
    только крупные связные области — сам товар и его крупные части (например,
    экран и базу, разделённые щелью шарнира), мелкие блобы стираем."""
    try:
        import numpy as np
        from scipy import ndimage
    except ImportError:
        return cutout_bytes  # scipy недоступен — пропускаем очистку
    try:
        im = Image.open(io.BytesIO(cutout_bytes)).convert("RGBA")
        im = _strip_attached_table(im)
        alpha = np.array(im.split()[3])
        mask = alpha > 100
        if not mask.any():
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
        labeled, n = ndimage.label(mask, structure=np.ones((3, 3), dtype=int))
        if n <= 1:
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
        sizes = ndimage.sum(mask, labeled, range(1, n + 1))
        largest = sizes.max()
        keep_ids = [i + 1 for i, s in enumerate(sizes) if s >= 0.10 * largest]
        keep_mask = np.isin(labeled, keep_ids)
        removed = int(sizes.sum() - sizes[keep_ids].sum())
        new_alpha = np.where(keep_mask, alpha, 0).astype(np.uint8)
        im.putalpha(Image.fromarray(new_alpha, "L"))
        buf = io.BytesIO()
        im.save(buf, format="PNG")
        if removed > 0:
            logger.info("Очистка вырезки: убрано мусорных пикселей %d (%d блобов из %d)",
                        removed, n - len(keep_ids), n)
        return buf.getvalue()
    except Exception:
        logger.exception("Очистка блобов не удалась — отдаю как есть")
        return cutout_bytes


async def _remove_bg(image_bytes: bytes, mime_type: str = "image/jpeg") -> Optional[bytes]:
    """Убирает фон локально. Движок = BG_ENGINE из .env (rembg / bria /
    birefnet / transparent-background). Возвращает PNG-байты с прозрачным
    фоном или None при сбое. Дополнительно вычищает «отвалившиеся» куски
    (стол/тени) из результата любой модели. mime_type оставлен для
    совместимости с контрактом."""
    try:
        out = await asyncio.to_thread(_remove_bg_sync, image_bytes)
        if out:
            out = await asyncio.to_thread(_strip_stray_blobs, out)
        return out
    except Exception:
        logger.exception("Удаление фона не удалось (движок=%s)", _BG_ENGINE)
        return None


# Логотип и градиент — те же, что на карточке-обложке, для единого стиля.
_LOGO_PATH = os.path.join(os.path.dirname(__file__), "avitomat_card", "assets", "logo.png")
# Высота логотипа/отступы — в пикселях (размер фото теперь всегда фиксирован,
# см. LISTING_PHOTO_SIZE, так что дробные доли больше не нужны). Отступ сверху
# и справа — одно и то же число, чтобы логотип был строго в углу.
_LOGO_HEIGHT_FRAC = 92 / 1080
_LOGO_MARGIN = 32  # одинаковый отступ сверху и справа, в пикселях


def _cleanup_alpha(im: Image.Image) -> Image.Image:
    """Постобработка альфа-канала вырезанного товара:
    1. Пороговое отсечение (threshold) — убирает полуопрозрачный «ореол» и
       остатки фона по краям (артефакты isnet/BiRefNet), делая край чётким.
    2. Лёгкое сглаживание (edge blur) — антиалиасинг границы, чтобы после
       threshold край не был рваным («лесенкой»).
    Возвращает изображение в режиме RGBA."""
    alpha = im.split()[3]
    # Threshold: всё ниже 128 → 0 (фон), выше → 255 (товар).
    alpha = alpha.point(lambda p: 255 if p > 128 else 0)
    # Сглаживание жёсткой границы (1px blur даёт антиалиасинг без ореолов).
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.0))
    im.putalpha(alpha)
    return im


def _compose_listing_photo(cutout_bytes: bytes, target_size: Tuple[int, int]) -> bytes:
    """Кладёт вырезанное (с прозрачным фоном) фото на градиентный фон (как у
    карточки), приводит к единому для всего объявления размеру (target_size —
    размер первого фото), добавляет мягкую тень и логотип сверху справа.
    Товар заполняет кадр максимально (минимальные отступы только под тень).
    Возвращает JPEG-байты."""
    im = Image.open(io.BytesIO(cutout_bytes)).convert("RGBA")

    # Постобработка краёв: убираем ореолы/артефакты вырезки (threshold + AA).
    im = _cleanup_alpha(im)

    # Обрезаем по фактическим границам товара (без этого прозрачные поля
    # вокруг мелко снятого товара остаются в кадре, и после вписывания в
    # холст товар выглядит мелким, хотя мог бы занимать почти весь кадр).
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)

    tw, th = target_size
    scale_factor = th / 1080

    # Максимальное заполнение: бюджет 0.995 (2-3px запаса на антиалиасинг
    # края), товар занимает весь кадр практически от края до края.
    budget = 0.995
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


def is_cropped_by_frame(cutout_bytes: bytes) -> bool:
    """True, если вырезанный объект упирается в край КАДРА (обрезан).
    Детерминированная проверка по альфа-каналу: если на внешней кромке
    изображения (полосы в 3px сверху/снизу/слева/справа) достаточно
    непрозрачных пикселей — объект касается границы кадра, значит обрезан.
    Порог: >0.5% пикселей стороны (шум края в пару пикселей не считается)."""
    try:
        import numpy as np
        alpha = np.array(Image.open(io.BytesIO(cutout_bytes)).convert("RGBA").split()[3])
        h, w = alpha.shape
        if h < 10 or w < 10:
            return False
        b = 3
        opaque = alpha > 100
        sides = {
            "top": opaque[:b, :],
            "bottom": opaque[-b:, :],
            "left": opaque[:, :b],
            "right": opaque[:, -b:],
        }
        for name, band in sides.items():
            denom = band.size
            if denom and band.sum() > 0.005 * denom and band.sum() >= 20:
                logger.info("Фото обрезано кадром: сторона %s, %d px на кромке",
                            name, int(band.sum()))
                return True
        return False
    except Exception:
        logger.exception("Проверка обрезанности не удалась — считаю фото целым")
        return False


async def compose_listing_from_cutout(cutout_bytes: bytes) -> Optional[bytes]:
    """Компонует фото объявления из ГОТОВОГО cutout (без повторной вырезки).
    None — если компоновка упала (вызывающий подставит исходник)."""
    try:
        return await asyncio.to_thread(_compose_listing_photo, cutout_bytes, LISTING_PHOTO_SIZE)
    except Exception:
        logger.exception("Не удалось собрать фото из cutout")
        return None


async def build_card(vision_params: dict, product_image_bytes: bytes,
                     mime_type: str = "image/jpeg",
                     cutout_bytes: Optional[bytes] = None) -> Optional[bytes]:
    """Полный цикл: убрать фон + нарисовать карточку. cutout_bytes — уже
    готовая вырезка (чтобы не вырезать одно фото дважды). Возвращает PNG-байты
    или None (нет ключа / не получилось). Рисование — в отдельном потоке,
    чтобы не блокировать event loop бота."""
    cutout = cutout_bytes if cutout_bytes else await _remove_bg(product_image_bytes, mime_type)
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
