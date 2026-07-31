"""
card_generator.py — генерация карточки товара для объявлений (Avito, 1280x960).

Финальный дизайн, согласованный в чате:
- Вертикальный градиент фона (светло-серый сверху -> тёмно-серый снизу)
- Заголовок по центру
- Логотип сверху справа (без эффектов)
- Характеристики (сетка 2x2) под заголовком — текст прямо на фоне, без подложек
- Фото товара (с прозрачным фоном) по центру, максимального размера
- Пункты доверия (иконка + текст) в одну линию под фото

Использование:

    from card_generator import CardConfig, Badge, Spec, generate_card

    generate_card(CardConfig(
        product_cutout_path="my_laptop_cutout.png",
        title="Lenovo ThinkPad T14 Gen 4",
        specs=[
            Spec("assets/icons/spec_screen.png", "Экран 14\""),
            Spec("assets/icons/spec_cpu.png", "Intel Core i5-1335U"),
            Spec("assets/icons/spec_ram.png", "16 ГБ ОЗУ + 256 ГБ SSD"),
            Spec("assets/icons/spec_gpu.png", "Intel Iris Xe Graphics"),
        ],
        badges=[
            Badge("assets/icons/insurance.png", "Гарантия 6 месяцев"),
            Badge("assets/icons/security.png", "Проверен по 25 параметрам"),
            Badge("assets/icons/loading.png", "Свежие драйверы, базовый софт"),
        ],
        output_path="output/card.png",
    ))

Важно: product_cutout_path должен быть PNG с ПРОЗРАЧНЫМ фоном (товар уже вырезан).
Если у вас есть только исходное фото на обычном фоне — сначала прогоните его через
background_removal.remove_background() из соседнего модуля.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------------------
# Константы дизайна (фиксированный, согласованный стиль — менять с осторожностью)
# ---------------------------------------------------------------------------

CANVAS_W, CANVAS_H = 1280, 1080

ASSETS_DIR = Path(__file__).parent / "assets"
FONT_BOLD_PATH = str(ASSETS_DIR / "fonts" / "Roboto-Bold.ttf")
FONT_THIN_PATH = str(ASSETS_DIR / "fonts" / "Roboto-Thin.ttf")
DEFAULT_LOGO_PATH = str(ASSETS_DIR / "logo.png")

GRADIENT_TOP = (250, 250, 252)      # светло-серый верх
GRADIENT_BOTTOM = (150, 152, 157)   # тёмно-серый низ

TITLE_COLOR = (82, 33, 33)          # тёмно-бордовый #522121
BADGE_TEXT_COLOR = (222, 222, 222)  # светло-серый — пункты доверия
SPEC_TEXT_COLOR = (44, 62, 80)      # #2C3E50 — текст характеристик

TITLE_FONT_SIZE = 77
TITLE_TOP_MARGIN = 40
BADGE_FONT_SIZE = 26
SPEC_FONT_SIZE = 32

LOGO_HEIGHT = 92
LOGO_MARGIN_RIGHT = 32

BADGE_ICON_SIZE = 38
BADGE_ROW_HEIGHT = 64
BADGE_GAP = 32  # расстояние между пунктами доверия в общей линии

SPEC_ROW_H = 46
SPEC_ROW_GAP = 16
SPEC_COL_INNER_GAP = 80  # зазор между двумя колонками характеристик
SPEC_COL2_EXTRA_SHIFT = 75  # доп. сдвиг правой колонки вправо (~4 символа)
SPEC_GRID_X0, SPEC_GRID_X1 = 36, 1244  # используется для центрирования пунктов доверия
BOTTOM_MARGIN = 8

PRODUCT_AREA_WIDTH_BUDGET = 1180  # с запасом от краёв канваса (1280), чтобы товар не упирался в них
PRODUCT_OFFSET_X = 20  # сдвиг фото товара вправо от центра


# ---------------------------------------------------------------------------
# Публичные типы конфигурации
# ---------------------------------------------------------------------------

@dataclass
class Badge:
    """Один пункт доверия: иконка (путь к PNG с прозрачным фоном) + текст в одну строку."""
    icon_path: str
    text: str


@dataclass
class Spec:
    """Одна характеристика в сетке 2x2: иконка (PNG с прозрачным фоном) + текст."""
    icon_path: str
    text: str


@dataclass
class CardConfig:
    product_cutout_path: str
    title: str
    specs: List[Spec]                      # ровно 4 штуки — сетка 2x2
    badges: List[Badge]
    output_path: str
    logo_path: str = DEFAULT_LOGO_PATH
    canvas_size: Tuple[int, int] = (CANVAS_W, CANVAS_H)


# ---------------------------------------------------------------------------
# Внутренние помощники
# ---------------------------------------------------------------------------

def _linear_gradient_v(size, top, bottom):
    w, h = size
    img = Image.new("RGB", size, bottom)
    px = img.load()
    for y in range(h):
        d = y / (h - 1)
        r = int(top[0] + (bottom[0] - top[0]) * d)
        g = int(top[1] + (bottom[1] - top[1]) * d)
        b = int(top[2] + (bottom[2] - top[2]) * d)
        for x in range(0, w, 2):
            px[x, y] = (r, g, b)
            if x + 1 < w:
                px[x + 1, y] = (r, g, b)
    return img


def _load_icon(path, size):
    """Обрезает иконку по реальному содержимому (без прозрачных полей) и
    вписывает в квадрат size x size — так иконки с разным исходным паддингом
    выглядят одного визуального размера."""
    im = Image.open(path).convert("RGBA")
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    scale = min(size / im.width, size / im.height)
    nw, nh = max(1, int(im.width * scale)), max(1, int(im.height * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    square = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    square.paste(im, ((size - nw) // 2, (size - nh) // 2), im)
    return square


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def generate_card(config: CardConfig) -> str:
    """Собирает карточку товара и сохраняет по config.output_path.
    Возвращает путь к сохранённому файлу."""

    if len(config.specs) != 4:
        raise ValueError("specs должен содержать ровно 4 строки (сетка 2x2)")

    W, H = config.canvas_size

    bg = _linear_gradient_v((W, H), GRADIENT_TOP, GRADIENT_BOTTOM)
    canvas = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # ---- ширина элементов блока характеристик (по фактическому контенту) ----
    spec_font = ImageFont.truetype(FONT_THIN_PATH, SPEC_FONT_SIZE)
    spec_icon_imgs = [_load_icon(s.icon_path, BADGE_ICON_SIZE) for s in config.specs]

    spec_item_widths = []
    for spec in config.specs:
        tb2 = draw.textbbox((0, 0), spec.text, font=spec_font)
        spec_item_widths.append(BADGE_ICON_SIZE + 14 + (tb2[2] - tb2[0]))
    col0_w = max(spec_item_widths[0], spec_item_widths[2])
    col1_w = max(spec_item_widths[1], spec_item_widths[3])

    # ---- заголовок — размер и позиция считаются первыми (не помещается в
    # канвас — уменьшаем), логотип и характеристики выравниваются по нему ----
    title_max_w = W - 2 * SPEC_GRID_X0
    title_font = ImageFont.truetype(FONT_BOLD_PATH, TITLE_FONT_SIZE)
    tb = draw.textbbox((0, 0), config.title, font=title_font)
    tw = tb[2] - tb[0]
    title_size = TITLE_FONT_SIZE
    while tw > title_max_w and title_size > 24:
        title_size -= 2
        title_font = ImageFont.truetype(FONT_BOLD_PATH, title_size)
        tb = draw.textbbox((0, 0), config.title, font=title_font)
        tw = tb[2] - tb[0]
    title_x0 = (W - tw) / 2
    title_h = tb[3] - tb[1]
    title_center_y = TITLE_TOP_MARGIN + tb[1] + title_h / 2

    # ---- логотип сверху справа — по горизонтали (центру) вровень с заголовком ----
    logo_raw = Image.open(config.logo_path).convert("RGBA")
    logo_raw = logo_raw.crop(logo_raw.getbbox())
    logo_small = logo_raw.resize(
        (int(logo_raw.width * LOGO_HEIGHT / logo_raw.height), LOGO_HEIGHT), Image.LANCZOS)
    logo_x = W - LOGO_MARGIN_RIGHT - logo_small.width
    logo_y = title_center_y - LOGO_HEIGHT / 2 - 12
    canvas.alpha_composite(logo_small, (int(logo_x), int(logo_y)))

    draw.text((title_x0, TITLE_TOP_MARGIN), config.title, font=title_font, fill=TITLE_COLOR)
    title_bottom = TITLE_TOP_MARGIN + title_h + 10

    # ---- блок характеристик — левый край вровень с левым краем заголовка ----
    spec_col_x = [title_x0, title_x0 + col0_w + SPEC_COL_INNER_GAP + SPEC_COL2_EXTRA_SHIFT]

    # ---- геометрия: характеристики теперь под заголовком (сверху), пункты
    # доверия — внизу; фото занимает всё, что остаётся между ними ----
    specs_h = 2 * SPEC_ROW_H + SPEC_ROW_GAP
    specs_top = title_bottom + 35
    badges_h = BADGE_ROW_HEIGHT
    badges_top = H - BOTTOM_MARGIN - badges_h

    photo_top = specs_top + specs_h + 20
    photo_bottom = badges_top - 20
    product_area_h = photo_bottom - photo_top

    # ---- характеристики (иконка+текст прямо на фоне, без подложек) ----
    for i, (spec_icon, spec) in enumerate(zip(spec_icon_imgs, config.specs)):
        col, row = i % 2, i // 2
        x0 = spec_col_x[col]
        y0 = specs_top + row * (SPEC_ROW_H + SPEC_ROW_GAP)

        icon_y = y0 + (SPEC_ROW_H - BADGE_ICON_SIZE) / 2
        canvas.alpha_composite(spec_icon, (int(x0), int(icon_y)))
        tb2 = draw.textbbox((0, 0), spec.text, font=spec_font)
        th2 = tb2[3] - tb2[1]
        draw.text((x0 + BADGE_ICON_SIZE + 14, y0 + (SPEC_ROW_H - th2) / 2 - tb2[1]),
                   spec.text, font=spec_font, fill=SPEC_TEXT_COLOR)

    # ---- фото товара (по центру, максимальный размер) ----
    cut_master = Image.open(config.product_cutout_path).convert("RGBA")
    cut_master = cut_master.crop(cut_master.getbbox())
    scale = min(PRODUCT_AREA_WIDTH_BUDGET / cut_master.width, product_area_h / cut_master.height)
    cut_resized = cut_master.resize(
        (int(cut_master.width * scale), int(cut_master.height * scale)), Image.LANCZOS)
    prod_x = (W - cut_resized.width) // 2 + PRODUCT_OFFSET_X
    prod_x = max(0, min(prod_x, W - cut_resized.width))
    prod_y = photo_top + (product_area_h - cut_resized.height) // 2

    shadow_shape = Image.new("RGBA", cut_resized.size, (0, 0, 0, 100))
    shadow_shape.putalpha(cut_resized.split()[3])
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste(shadow_shape, (prod_x, prod_y + 22), shadow_shape)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(18))
    canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(cut_resized, (prod_x, prod_y))

    # ---- пункты доверия: все в одну линию — под фото. Раскладка по фактической
    # ширине текста (не фиксированные колонки), иначе длинные пункты вылезают
    # за край, а короткие оставляют некрасивые пустоты ----
    badge_font = ImageFont.truetype(FONT_THIN_PATH, BADGE_FONT_SIZE)
    icon_imgs = [_load_icon(b.icon_path, BADGE_ICON_SIZE) for b in config.badges]

    item_widths = []
    for badge in config.badges:
        tb2 = draw.textbbox((0, 0), badge.text, font=badge_font)
        item_widths.append(BADGE_ICON_SIZE + 14 + (tb2[2] - tb2[0]))

    row_w = sum(item_widths) + BADGE_GAP * (len(config.badges) - 1)
    available_w = SPEC_GRID_X1 - SPEC_GRID_X0
    x = SPEC_GRID_X0 + max(0, (available_w - row_w) / 2)

    for icon_img, badge, item_w in zip(icon_imgs, config.badges, item_widths):
        canvas.alpha_composite(icon_img, (int(x), int(badges_top)))
        tb2 = draw.textbbox((0, 0), badge.text, font=badge_font)
        th2 = tb2[3] - tb2[1]
        draw.text((x + BADGE_ICON_SIZE + 14, badges_top + (BADGE_ICON_SIZE - th2) / 2 - tb2[1]),
                   badge.text, font=badge_font, fill=BADGE_TEXT_COLOR)
        x += item_w + BADGE_GAP

    # ---- сохранение ----
    canvas = canvas.convert("RGB")
    out_path = Path(config.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=95)
    return str(out_path)
