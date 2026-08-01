"""
card_generator.py — генерация карточки товара для объявлений (Avito, 1280x960).

Горизонтальная карточка 4:3 (не обрезается в выдаче Авито). Композиция:
- Левая колонка (~45% ширины): вырезанное фото товара по центру, с мягкой тенью.
- Правая колонка (~55% ширины): логотип сверху, заголовок (бренд+модель),
  характеристики вертикальным списком (иконка+текст), пункты доверия снизу.
- Вертикальный градиент фона (светло-серый сверху -> тёмно-серый снизу).
- Без цены (цена только в тексте объявления).

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
"""

from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ---------------------------------------------------------------------------
# Константы дизайна
# ---------------------------------------------------------------------------

CANVAS_W, CANVAS_H = 1280, 960

ASSETS_DIR = Path(__file__).parent / "assets"
FONT_BOLD_PATH = str(ASSETS_DIR / "fonts" / "Roboto-Bold.ttf")
FONT_THIN_PATH = str(ASSETS_DIR / "fonts" / "Roboto-Thin.ttf")
DEFAULT_LOGO_PATH = str(ASSETS_DIR / "logo.png")

GRADIENT_TOP = (250, 250, 252)      # светло-серый верх
GRADIENT_BOTTOM = (150, 152, 157)   # тёмно-серый низ

TITLE_COLOR = (82, 33, 33)          # тёмно-бордовый #522121
BADGE_TEXT_COLOR = (236, 236, 236)  # светло-серый — пункты доверия
SPEC_TEXT_COLOR = (44, 62, 80)      # #2C3E50 — текст характеристик

# Раскладка колонок.
LEFT_COL_W = int(CANVAS_W * 0.45)   # левая колонка = фото товара
RIGHT_COL_X = LEFT_COL_W + 24       # правая колонка = текст, с отступом от левой
RIGHT_COL_W = CANVAS_W - RIGHT_COL_X - 36

# Шрифты.
TITLE_FONT_SIZE = 60
SPEC_FONT_SIZE = 30
SPEC_LABEL_FONT_SIZE = 24
BADGE_FONT_SIZE = 22

# Логотип.
LOGO_HEIGHT = 70
LOGO_TOP = 36

# Геометрия текстовых блоков правой колонки.
TITLE_TOP = LOGO_TOP + LOGO_HEIGHT + 36
TITLE_LINE_GAP = 8     # межстрочный интервал заголовка при переносе
TITLE_BOTTOM_GAP = 28  # отступ от заголовка до характеристик

# Характеристики — вертикальный список, иконка + (значение крупно + подпис мелко).
SPEC_ICON_SIZE = 48
SPEC_ROW_GAP = 26
SPEC_VALUE_TOP_PAD = 0     # выравнивание значения относительно иконки
SPEC_LABEL_GAP = 6         # отступ подписи от значения

# Пункты доверия — внизу правой колонки.
BADGE_ICON_SIZE = 34
BADGE_BOTTOM = 28
BADGE_ROW_GAP = 14  # по вертикали между пунктами (если не влезают в строку)

# Товар в левой колонке.
PRODUCT_AREA_PADDING = 60  # отступ фото от границ левой колонки


# ---------------------------------------------------------------------------
# Публичные типы конфигурации
# ---------------------------------------------------------------------------

@dataclass
class Badge:
    """Один пункт доверия: иконка (путь к PNG с прозрачным фоном) + текст."""
    icon_path: str
    text: str


@dataclass
class Spec:
    """Одна характеристика: иконка + значение (крупно) + необязательная подпись (мелко).

    Если text содержит двоеточие или перевод строки — всё после первого разделителя
    считается подписью (мелким шрифтом под значением). Иначе подписи нет.
    """
    icon_path: str
    text: str

    def split_value_label(self) -> Tuple[str, str]:
        """Разделяет 'Экран 14"' -> ('Экран 14"', '') или
        'Экран: 14"' -> ('Экран', '14"'). Используем '|'-разделитель в тексте,
        чтобы caller мог задать пару явно."""
        if "|" in self.text:
            value, label = self.text.split("|", 1)
            return value.strip(), label.strip()
        return self.text.strip(), ""


@dataclass
class CardConfig:
    product_cutout_path: str
    title: str
    specs: List[Spec]                      # рекомендуется 4 штуки
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
    """Обрезает иконку по реальному содержимому и вписывает в квадрат size x size."""
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


def _fit_font(text: str, font_path: str, start_size: int, max_w: int,
              min_size: int = 22) -> ImageFont.FreeTypeFont:
    """Уменьшает размер шрифта, пока текст не влезает в max_w."""
    size = start_size
    font = ImageFont.truetype(font_path, size)
    while size > min_size and font.getlength(text) > max_w:
        size -= 2
        font = ImageFont.truetype(font_path, size)
    return font


def _wrap_title(draw, text, font_path, font_size, max_w, line_gap):
    """Переносит заголовок по словам; при необходимости уменьшает шрифт так,
    чтобы самое длинное слово влезало. Возвращает (font, list_of_lines)."""
    words = text.split()
    font_size = font_size
    while font_size > 22:
        font = ImageFont.truetype(font_path, font_size)
        lines, cur = [], ""
        ok = True
        for w in words:
            trial = (cur + " " + w).strip()
            if font.getlength(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) >= 3:  # не больше 3 строк
                    ok = False
                    break
        if ok:
            if cur:
                lines.append(cur)
            return font, lines
        font_size -= 3
    font = ImageFont.truetype(font_path, font_size)
    return font, [text]


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def generate_card(config: CardConfig) -> str:
    """Собирает горизонтальную карточку 1280x960 и сохраняет по config.output_path."""

    W, H = config.canvas_size

    bg = _linear_gradient_v((W, H), GRADIENT_TOP, GRADIENT_BOTTOM)
    canvas = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # ===================== ЛЕВАЯ КОЛОНКА: ТОВАР ============================
    cut = Image.open(config.product_cutout_path).convert("RGBA")
    cut = cut.crop(cut.getbbox()) if cut.getbbox() else cut

    area_x0 = PRODUCT_AREA_PADDING
    area_y0 = PRODUCT_AREA_PADDING
    area_x1 = LEFT_COL_W - 20
    area_y1 = H - PRODUCT_AREA_PADDING
    area_w = area_x1 - area_x0
    area_h = area_y1 - area_y0
    scale = min(area_w / cut.width, area_h / cut.height)
    cut_r = cut.resize((int(cut.width * scale), int(cut.height * scale)), Image.LANCZOS)
    prod_x = area_x0 + (area_w - cut_r.width) // 2
    prod_y = area_y0 + (area_h - cut_r.height) // 2

    # Мягкая тень под товаром.
    shadow_shape = Image.new("RGBA", cut_r.size, (0, 0, 0, 110))
    shadow_shape.putalpha(cut_r.split()[3])
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste(shadow_shape, (prod_x, prod_y + 26), shadow_shape)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(20))
    canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(cut_r, (prod_x, prod_y))

    # ===================== ПРАВАЯ КОЛОНКА: ЛОГОТИП =========================
    logo_raw = Image.open(config.logo_path).convert("RGBA")
    logo_raw = logo_raw.crop(logo_raw.getbbox()) if logo_raw.getbbox() else logo_raw
    logo = logo_raw.resize(
        (int(logo_raw.width * LOGO_HEIGHT / logo_raw.height), LOGO_HEIGHT), Image.LANCZOS)
    canvas.alpha_composite(logo, (RIGHT_COL_X, LOGO_TOP))

    # ===================== ПРАВАЯ КОЛОНКА: ЗАГОЛОВОК =======================
    title_font, title_lines = _wrap_title(
        draw, config.title, FONT_BOLD_PATH, TITLE_FONT_SIZE, RIGHT_COL_W, TITLE_LINE_GAP)
    title_y = TITLE_TOP
    line_h = title_font.size + TITLE_LINE_GAP
    for line in title_lines:
        draw.text((RIGHT_COL_X, title_y), line, font=title_font, fill=TITLE_COLOR)
        title_y += line_h
    specs_top = title_y + TITLE_BOTTOM_GAP

    # ===================== ПРАВАЯ КОЛОНКА: ХАРАКТЕРИСТИКИ ==================
    spec_icon_imgs = [_load_icon(s.icon_path, SPEC_ICON_SIZE) for s in config.specs]
    value_font = ImageFont.truetype(FONT_BOLD_PATH, SPEC_FONT_SIZE)
    label_font = ImageFont.truetype(FONT_THIN_PATH, SPEC_LABEL_FONT_SIZE)

    # Зарезервируем место под пункты доверия снизу, чтобы характеристики не
    # налезли на них при длинных значениях.
    badges_block_h = _estimate_badges_block_h(config.badges, label_font, H, BADGE_BOTTOM)
    specs_max_bottom = H - badges_block_h - 20

    y = specs_top
    for icon_img, spec in zip(spec_icon_imgs, config.specs):
        value, label = spec.split_value_label()
        # Уменьшаем шрифт значения, если оно не влезает в ширину колонки.
        vfont = _fit_font(value, FONT_BOLD_PATH, SPEC_FONT_SIZE,
                          RIGHT_COL_W - SPEC_ICON_SIZE - 14)
        row_h = max(SPEC_ICON_SIZE, vfont.size + (SPEC_LABEL_GAP + label_font.size if label else 0))
        if y + row_h > specs_max_bottom:
            break  # не вышло за пределы — обрезаем лишние характеристики

        icon_y = y + (row_h - SPEC_ICON_SIZE) // 2
        canvas.alpha_composite(icon_img, (RIGHT_COL_X, icon_y))

        tx = RIGHT_COL_X + SPEC_ICON_SIZE + 14
        vtb = draw.textbbox((0, 0), value, font=vfont)
        draw.text((tx, y + SPEC_VALUE_TOP_PAD - vtb[1]), value, font=vfont, fill=SPEC_TEXT_COLOR)
        if label:
            ltb = draw.textbbox((0, 0), label, font=label_font)
            draw.text((tx, y + vfont.size + SPEC_LABEL_GAP - ltb[1]), label,
                      font=label_font, fill=(90, 95, 102))
        y += row_h + SPEC_ROW_GAP

    # ===================== ПРАВАЯ КОЛОНКА: ПУНКТЫ ДОВЕРИЯ ==================
    _draw_badges(canvas, draw, config.badges, RIGHT_COL_X, RIGHT_COL_W, H)

    # ===================== СОХРАНЕНИЕ ======================================
    canvas = canvas.convert("RGB")
    out_path = Path(config.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=95)
    return str(out_path)


def _estimate_badges_block_h(badges, label_font, canvas_h, bottom):
    """Грубая оценка высоты блока пунктов доверия: пытаемся уложить в 1 строку,
    иначе в N строк по переносам слов."""
    if not badges:
        return 0
    line_h = max(BADGE_ICON_SIZE, label_font.size) + BADGE_ROW_GAP
    # Будем считать, что помещается в 3 строки максимум — этого хватит с запасом.
    return min(len(badges), 3) * line_h + bottom


def _draw_badges(canvas, draw, badges, x0, max_w, canvas_h):
    """Пункты доверия — в нижней части правой колонки. Если не помещаются в одну
    строку — переносим по одному на строку (иконка + текст)."""
    if not badges:
        return
    badge_font = ImageFont.truetype(FONT_THIN_PATH, BADGE_FONT_SIZE)
    icon_imgs = [_load_icon(b.icon_path, BADGE_ICON_SIZE) for b in badges]

    # Ширина каждой позиции.
    item_widths = []
    for b in badges:
        tw = draw.textlength(b.text, font=badge_font)
        item_widths.append(BADGE_ICON_SIZE + 10 + tw)

    total_w = sum(item_widths) + BADGE_ROW_GAP * (len(badges) - 1)
    if total_w <= max_w:
        # Одна строка, по центру.
        x = x0 + max(0, (max_w - total_w) / 2)
        y = canvas_h - BADGE_BOTTOM - BADGE_ICON_SIZE
        for icon_img, b, iw in zip(icon_imgs, badges, item_widths):
            canvas.alpha_composite(icon_img, (int(x), int(y)))
            th = draw.textbbox((0, 0), b.text, font=badge_font)
            draw.text((x + BADGE_ICON_SIZE + 10,
                       y + (BADGE_ICON_SIZE - (th[3] - th[1])) / 2 - th[1]),
                      b.text, font=badge_font, fill=BADGE_TEXT_COLOR)
            x += iw + BADGE_ROW_GAP
    else:
        # По одному на строку, прижаты к низу.
        line_h = max(BADGE_ICON_SIZE, badge_font.size) + 6
        y = canvas_h - BADGE_BOTTOM - line_h * len(badges)
        for icon_img, b in zip(icon_imgs, badges):
            canvas.alpha_composite(icon_img, (x0, int(y)))
            th = draw.textbbox((0, 0), b.text, font=badge_font)
            draw.text((x0 + BADGE_ICON_SIZE + 10,
                       y + (BADGE_ICON_SIZE - (th[3] - th[1])) / 2 - th[1]),
                      b.text, font=badge_font, fill=BADGE_TEXT_COLOR)
            y += line_h
