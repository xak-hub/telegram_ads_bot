"""
card_generator.py — генерация карточки товара для объявлений (Avito, 1280x960).

Горизонтальная карточка 4:3 (не обрезается в выдаче Авито). Композиция:
- Заголовок сверху (по центру), Bold.
- Логотип справа вверху, одинаковый отступ сверху и справа.
- Характеристики в 2 столбца под заголовком (Thin).
- Фото товара (с прозрачным фоном) по центру, максимального размера, с тенью.
- Пункты доверия (преимущества) в самый низ, одной строкой (Thin).
- Вертикальный градиент фона (светло-серый сверху -> тёмно-серый снизу).
- Без цены (цена только в тексте объявления).
- Шрифт Roboto (аналог Helvetica Neue): Bold для заголовка, Thin для остального.

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

# Единый шрифт для всей карточки (аналог Helvetica Neue): Bold для заголовка,
# Thin для всего остального. Размеры — относительные.
TITLE_FONT_SIZE = 64
SPEC_FONT_SIZE = 30
BADGE_FONT_SIZE = 22

# Логотип — справа вверху, одинаковый отступ сверху и справа.
LOGO_HEIGHT = 70
LOGO_MARGIN = 32  # одинаковый отступ сверху и справа, в пикселях

# Заголовок — максимально высоко, по центру по горизонтали.
TITLE_TOP = 10
TITLE_LINE_GAP = 6
TITLE_BOTTOM_GAP = 16

# Характеристики — в 2 столбца под заголовком.
SPEC_ICON_SIZE = 42
SPEC_ROW_H = 48
SPEC_ROW_GAP = 8
SPEC_COL_GAP = 80   # зазор между двумя колонками (не используется при фиксированных X)
SPEC_BOTTOM_GAP = 16
# X-координаты левого края каждой колонки. None = колонка позиционируется
# автоматически (симметрично центра для левой). Правая колонка (индекс 1)
# начинается ровно с центра канваса (CANVAS_W/2 = 640).
SPEC_COL_X = (220, 640)
SPEC_TOP_Y = 90    # верхняя граница блока характеристик (явно заданная)

# Пункты доверия — в самый низ, одной строкой, единым центрированным блоком
# с одинаковыми отступами между бейджами.
BADGE_ICON_SIZE = 34
BADGE_BOTTOM = 28
BADGE_GAP = 48  # одинаковый зазор между бейджами в едином блоке

# Товар — по центру, максимального размера (между характеристиками и бейджами).
PRODUCT_SIDE_PADDING = 60  # отступ фото от боковых краёв канваса


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
    """Одна характеристика: иконка (PNG с прозрачным фоном) + текст."""
    icon_path: str
    text: str

    def split_value_label(self) -> Tuple[str, str]:
        """Если text содержит '|', всё после него — подпись (мелким шрифтом).
        Иначе подписи нет."""
        if "|" in self.text:
            value, label = self.text.split("|", 1)
            return value.strip(), label.strip()
        return self.text.strip(), ""


@dataclass
class CardConfig:
    product_cutout_path: str
    title: str
    specs: List[Spec]                      # рекомендуется 4 штуки — 2 колонки по 2
    badges: List[Badge]
    output_path: str
    logo_path: str = DEFAULT_LOGO_PATH
    canvas_size: Tuple[int, int] = (CANVAS_W, CANVAS_H)
    tags: List[str] = None                 # чипы под заголовком: «сенсорный», «трансформер»

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


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


def _wrap_title(draw, text, font_path, font_size, max_w, line_gap, max_lines=2):
    """Переносит заголовок по словам; при необходимости уменьшает шрифт, чтобы
    самое длинное слово влезало. Возвращает (font, list_of_lines)."""
    words = text.split()
    font_size_local = font_size
    while font_size_local > 24:
        font = ImageFont.truetype(font_path, font_size_local)
        lines, cur = [], ""
        ok = True
        for w in words:
            trial = (cur + " " + w).strip()
            if font.getlength(trial) <= max_w or not cur:
                cur = trial
            else:
                lines.append(cur)
                cur = w
                if len(lines) >= max_lines:
                    ok = False
                    break
        if ok:
            if cur:
                lines.append(cur)
            return font, lines
        font_size_local -= 3
    font = ImageFont.truetype(font_path, max(24, font_size_local))
    return font, [text]


def _cleanup_cutout_alpha(im):
    """Постобработка альфа-канала вырезанного товара: threshold убирает
    ореолы/остатки фона, лёгкий blur даёт антиалиасинг края."""
    alpha = im.split()[3]
    alpha = alpha.point(lambda p: 255 if p > 128 else 0)
    alpha = alpha.filter(ImageFilter.GaussianBlur(1.0))
    im.putalpha(alpha)
    return im


# ---------------------------------------------------------------------------
# Основная функция
# ---------------------------------------------------------------------------

def generate_card(config: CardConfig) -> str:
    """Собирает горизонтальную карточку 1280x960 и сохраняет по config.output_path."""

    W, H = config.canvas_size

    bg = _linear_gradient_v((W, H), GRADIENT_TOP, GRADIENT_BOTTOM)
    canvas = bg.convert("RGBA")
    draw = ImageDraw.Draw(canvas)

    # ===================== ЛОГОТИП: справа вверху ==========================
    logo_raw = Image.open(config.logo_path).convert("RGBA")
    logo_raw = logo_raw.crop(logo_raw.getbbox()) if logo_raw.getbbox() else logo_raw
    logo = logo_raw.resize(
        (int(logo_raw.width * LOGO_HEIGHT / logo_raw.height), LOGO_HEIGHT), Image.LANCZOS)
    # одинаковый отступ сверху и справа (LOGO_MARGIN)
    canvas.alpha_composite(logo, (W - LOGO_MARGIN - logo.width, LOGO_MARGIN))

    # ===================== ЗАГОЛОВОК: сверху, по центру ====================
    title_max_w = W - 2 * LOGO_MARGIN
    title_font, title_lines = _wrap_title(
        draw, config.title, FONT_BOLD_PATH, TITLE_FONT_SIZE, title_max_w, TITLE_LINE_GAP)
    line_h = title_font.size + TITLE_LINE_GAP
    title_block_h = line_h * len(title_lines)
    ty = TITLE_TOP
    for line in title_lines:
        lw = title_font.getlength(line)
        draw.text(((W - lw) / 2, ty), line, font=title_font, fill=TITLE_COLOR)
        ty += line_h

    # ===================== ЧИПЫ: «сенсорный», «трансформер» ================
    # Скруглённые плашки под заголовком, по центру; сдвигают характеристики.
    chip_h = 0
    if config.tags:
        tag_font = ImageFont.truetype(FONT_THIN_PATH, 26)
        pad_x, pad_y, gap = 14, 8, 12
        chip_h = 26 + 2 * pad_y
        widths = [tag_font.getlength(t) + 2 * pad_x for t in config.tags]
        total = sum(widths) + gap * (len(config.tags) - 1)
        x = (W - total) / 2
        y = ty + 6
        for t, wch in zip(config.tags, widths):
            draw.rounded_rectangle(
                [x, y, x + wch, y + chip_h], radius=chip_h // 2,
                fill=(82, 33, 33, 235))
            tb = draw.textbbox((0, 0), t, font=tag_font)
            draw.text((x + pad_x, y + (chip_h - (tb[3] - tb[1])) / 2 - tb[1]),
                      t, font=tag_font, fill=(255, 255, 255))
            x += wch + gap

    specs_top = max(SPEC_TOP_Y, ty + chip_h + 10)

    # ===================== ХАРАКТЕРИСТИКИ: 2 колонки =======================
    # Разбиваем список характеристик на 2 колонки: первая половина — в левую,
    # вторая — в правую. Считаем геометрию по фактическому контенту.
    spec_font = ImageFont.truetype(FONT_THIN_PATH, SPEC_FONT_SIZE)
    n = len(config.specs)
    half = (n + 1) // 2  # в левой колонке на 1 больше при нечётном количестве
    col_specs = [config.specs[:half], config.specs[half:]]
    spec_icon_imgs = [_load_icon(s.icon_path, SPEC_ICON_SIZE) for s in config.specs]

    # Ширина каждой колонки по самой широкой позиции (иконка + текст) — нужна
    # только если X колонок не задан явно (SPEC_COL_X = None).
    def _item_w(spec):
        return SPEC_ICON_SIZE + 12 + draw.textlength(spec.text, font=spec_font)

    col_widths = []
    for col in col_specs:
        col_widths.append(max([_item_w(s) for s in col], default=0))
    cols_total_w = sum(col_widths) + SPEC_COL_GAP
    cols_x0_auto = (W - cols_total_w) / 2

    # Рисуем характеристики, запоминая нижнюю границу блока.
    # Симметрия относительно центра канваса: правая колонка начинается левым
    # краем ровно с центра (X=CANVAS_W/2), левая — заканчивается правым краем
    # ровно у центра (симметрично).
    specs_bottom = specs_top
    icon_idx = 0
    center_x = W / 2
    for col_i, col in enumerate(col_specs):
        explicit = None
        if SPEC_COL_X is not None and col_i < len(SPEC_COL_X):
            explicit = SPEC_COL_X[col_i]
        if explicit is not None:
            cx = explicit
        elif col_i == 0:
            # левая колонка: правый край у центра (симметрично правой)
            cx = center_x - col_widths[0]
        else:
            # прочие — автоцентрирование (fallback)
            cx = cols_x0_auto + (sum(col_widths[:col_i]) if col_i > 0 else 0)
        y = specs_top
        for spec in col:
            icon_img = spec_icon_imgs[icon_idx]
            icon_idx += 1
            icon_y = y + (SPEC_ROW_H - SPEC_ICON_SIZE) / 2
            canvas.alpha_composite(icon_img, (int(cx), int(icon_y)))
            tb = draw.textbbox((0, 0), spec.text, font=spec_font)
            th = tb[3] - tb[1]
            draw.text((cx + SPEC_ICON_SIZE + 12,
                       y + (SPEC_ROW_H - th) / 2 - tb[1]),
                      spec.text, font=spec_font, fill=SPEC_TEXT_COLOR)
            y += SPEC_ROW_H + SPEC_ROW_GAP
        if y > specs_bottom:
            specs_bottom = y - SPEC_ROW_GAP  # без последнего межстрочного

    # ===================== ТОВАР: по центру, макс. размера =================
    cut = Image.open(config.product_cutout_path).convert("RGBA")
    # Постобработка краёв: убираем ореолы/артефакты вырезки (threshold + AA).
    cut = _cleanup_cutout_alpha(cut)
    cut = cut.crop(cut.getbbox()) if cut.getbbox() else cut

    # Область под товар: между блоком характеристик и нижней строкой бейджей.
    badges_h = max(BADGE_ICON_SIZE, _font_px_height(spec_font)) + BADGE_BOTTOM + 16
    photo_top = specs_bottom + SPEC_BOTTOM_GAP
    photo_bottom = H - badges_h
    photo_left = PRODUCT_SIDE_PADDING
    photo_right = W - PRODUCT_SIDE_PADDING
    area_w = photo_right - photo_left
    area_h = photo_bottom - photo_top
    scale = min(area_w / cut.width, area_h / cut.height)
    cut_r = cut.resize((int(cut.width * scale), int(cut.height * scale)), Image.LANCZOS)
    prod_x = photo_left + (area_w - cut_r.width) // 2
    prod_y = photo_top + (area_h - cut_r.height) // 2

    # Мягкая тень под товаром.
    shadow_shape = Image.new("RGBA", cut_r.size, (0, 0, 0, 110))
    shadow_shape.putalpha(cut_r.split()[3])
    shadow_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    shadow_layer.paste(shadow_shape, (prod_x, prod_y + 26), shadow_shape)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(20))
    canvas.alpha_composite(shadow_layer)
    canvas.alpha_composite(cut_r, (prod_x, prod_y))

    # ===================== ПУНКТЫ ДОВЕРИЯ: в самый низ, 1 строка ===========
    _draw_badges_row(canvas, draw, config.badges, W, H)

    # ===================== СОХРАНЕНИЕ ======================================
    canvas = canvas.convert("RGB")
    out_path = Path(config.output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=95)
    return str(out_path)


def _font_px_height(font):
    """Примерная высота шрифта в пикселях."""
    try:
        return font.size
    except Exception:
        return 24


def _draw_badges_row(canvas, draw, badges, canvas_w, canvas_h):
    """Пункты доверия — единым центрированным блоком в самом низу, с одинаковыми
    отступами между бейджами."""
    if not badges:
        return
    badge_font = ImageFont.truetype(FONT_THIN_PATH, BADGE_FONT_SIZE)
    icon_imgs = [_load_icon(b.icon_path, BADGE_ICON_SIZE) for b in badges]

    def _draw_one(icon_img, b, left_x, y):
        canvas.alpha_composite(icon_img, (int(left_x), int(y)))
        th = draw.textbbox((0, 0), b.text, font=badge_font)
        draw.text((left_x + BADGE_ICON_SIZE + 10,
                   y + (BADGE_ICON_SIZE - (th[3] - th[1])) / 2 - th[1]),
                  b.text, font=badge_font, fill=BADGE_TEXT_COLOR)

    def _item_w(b):
        return BADGE_ICON_SIZE + 10 + draw.textlength(b.text, font=badge_font)

    item_widths = [_item_w(b) for b in badges]
    n = len(badges)
    total_w = sum(item_widths) + BADGE_GAP * (n - 1)
    # единый блок по центру канваса
    x = (canvas_w - total_w) / 2
    y = canvas_h - BADGE_BOTTOM - BADGE_ICON_SIZE
    for icon_img, b, iw in zip(icon_imgs, badges, item_widths):
        _draw_one(icon_img, b, x, y)
        x += iw + BADGE_GAP
