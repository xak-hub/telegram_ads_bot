import logging
import re
from typing import Optional, Union, List

from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton

from avito_options import FIELDS
from avito_defaults import load_defaults, save_defaults
from sheets import append_row, SPREADSHEET_ID
from avito_export import append_listing, EXPORT_PATH
from avito_row import make_listing_id, round_storage
from yandex_storage import upload_feed

logger = logging.getLogger(__name__)

router = Router()

# chat_id -> {"mode": "listing"|"defaults", "vision": {...}, "answers": {},
#             "multi_current": {step: set()}, "hub_message_id": int}
SESSIONS: dict[int, dict] = {}

PRICE_PENDING: set[int] = set()
ADDRESS_PENDING: set[int] = set()

# Собранные данные объявления, ожидающие подтверждения предпоказа.
# chat_id -> {"vision","answers","price","address","listing_id","photo_urls","card_bytes"}
PREVIEW_PENDING: dict[int, dict] = {}

REQUIRED_COLUMNS = {f["avito_column"] for f in FIELDS if f["required"]}

MAX_LABEL = 42


def _short(text: str) -> str:
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def _option_label(field: dict, option: str) -> str:
    """Подпись на кнопке — может быть короче значения, которое реально пишется
    в таблицу (см. avito_options.FIELDS[...]['labels'])."""
    return field.get("labels", {}).get(option, option)


def _all_required_answered(chat_id: int) -> bool:
    answers = SESSIONS[chat_id]["answers"]
    return all(col in answers for col in REQUIRED_COLUMNS)


def build_hub_keyboard(chat_id: int) -> InlineKeyboardMarkup:
    session = SESSIONS[chat_id]
    rows = []
    for step, field in enumerate(FIELDS):
        value = session["answers"].get(field["avito_column"])
        mark = "✅" if value else ("·" if not field["required"] else "○")
        label = f"{mark} {field['title']}: {value or '—'}"
        rows.append([InlineKeyboardButton(text=_short(label), callback_data=f"avf:{step}")])

    if session["mode"] == "defaults":
        rows.append([InlineKeyboardButton(text="💾 Сохранить как умолчания", callback_data="avdone")])
    else:
        if _all_required_answered(chat_id):
            rows.append([InlineKeyboardButton(text="✅ Готово — дальше цена", callback_data="avdone")])
        else:
            rows.append([InlineKeyboardButton(text="Готово (заполни ○ поля)", callback_data="avnotready")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_field_keyboard(step: int, chat_id: int) -> InlineKeyboardMarkup:
    field = FIELDS[step]
    selected = SESSIONS[chat_id]["multi_current"].get(step, set()) if field["multi"] else set()
    rows = []
    options = field["options"]
    for i in range(0, len(options), 4):
        row = []
        for option in options[i:i + 4]:
            prefix = "✅ " if option in selected else ""
            idx = options.index(option)
            label = _option_label(field, option)
            row.append(InlineKeyboardButton(text=_short(prefix + label), callback_data=f"av:{step}:{idx}"))
        rows.append(row)

    bottom = []
    if field["multi"]:
        bottom.append(InlineKeyboardButton(text="Готово ➜", callback_data=f"av:{step}:done"))
    if not field["required"]:
        bottom.append(InlineKeyboardButton(text="Пропустить", callback_data=f"av:{step}:skip"))
    bottom.append(InlineKeyboardButton(text="◀ Назад", callback_data="avback"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_flat_keyboard(chat_id: int) -> Optional[InlineKeyboardMarkup]:
    """Для объявления: поля, заполненные константами по умолчанию, скрыты совсем.
    Остальные показываются сразу с вариантами-кнопками (без захода в подменю) и
    остаются на экране после выбора — просто отмечаются галочкой, чтобы можно
    было передумать, а список не прыгал."""
    session = SESSIONS[chat_id]
    default_columns = session.get("default_columns", set())
    rows = []
    for step, field in enumerate(FIELDS):
        col = field["avito_column"]
        if col in default_columns:
            continue
        value = session["answers"].get(col)
        rows.append([InlineKeyboardButton(text=_short(field["title"]), callback_data="noop")])
        options = field["options"]
        for i in range(0, len(options), 4):
            row = []
            for option in options[i:i + 4]:
                idx = options.index(option)
                prefix = "✅ " if value == option else ""
                label = _option_label(field, option)
                row.append(InlineKeyboardButton(text=_short(prefix + label), callback_data=f"avl:{step}:{idx}"))
            rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


_COLOR_FIELD = next(f for f in FIELDS if f["key"] == "color")

# Свободный текст, который возвращает Claude Vision, сводим к одному из
# разрешённых вариантов цвета — так поле можно доверить распознаванию по фото
# и не спрашивать пользователя, если модель уверенно определила цвет.
_COLOR_SYNONYMS = {
    "серебристый": "Серебристый", "серебристая": "Серебристый", "серебро": "Серебристый",
    "silver": "Серебристый",
    "серый": "Серый", "тёмно-серый": "Серый", "темно-серый": "Серый",
    "space gray": "Серый", "space grey": "Серый", "космический серый": "Серый",
    "gray": "Серый", "grey": "Серый",
    "чёрный": "Чёрный", "черный": "Чёрный", "black": "Чёрный",
    "белый": "Белый", "white": "Белый",
}


def _normalize_color(raw: str) -> Optional[str]:
    if not raw:
        return None
    return _COLOR_SYNONYMS.get(raw.strip().lower())


_DRIVE_SIZE_FIELD = next(f for f in FIELDS if f["key"] == "drive_size")
_RAM_SIZE_FIELD = next(f for f in FIELDS if f["key"] == "ram_size")
_OS_FIELD = next(f for f in FIELDS if f["key"] == "os")


def _normalize_number(raw: str, allowed_options: List[str]) -> Optional[str]:
    """Достаёт число из текста вроде "16 ГБ"/"16GB"/"16" и проверяет, что оно
    входит в список разрешённых значений — иначе не подставляем, пусть выберут сами."""
    if not raw:
        return None
    match = re.search(r"\d+", raw)
    if not match:
        return None
    value = match.group(0)
    return value if value in allowed_options else None


def _normalize_price(raw: str) -> Optional[int]:
    """Достаёт цену из текста вроде "299 990 ₽"/"299990" — только если Claude
    реально увидел цену на скриншоте стороннего объявления."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return int(digits) if digits else None


# Порядок важен: более длинные/точные варианты проверяются первыми, иначе
# "windows 8.1" может ошибочно распознаться как "windows 8".
_OS_SYNONYMS = {
    "windows 11": "Windows 11",
    "windows 10": "Windows 10",
    "windows 8.1": "Windows 8.1",
    "windows 8": "Windows 8",
    "windows 7": "Windows 7",
    "mac os": "macOS",
    "macos": "macOS",
    "chrome os": "Chrome OS",
    "chromeos": "Chrome OS",
    "linux": "Linux",
    "ubuntu": "Linux",
    "android": "Android",
    "dos": "Без ОС (DOS)",
    "без ос": "Без ОС (DOS)",
    "нет ос": "Без ОС (DOS)",
}


def _normalize_os(raw: str) -> Optional[str]:
    if not raw:
        return None
    key = raw.strip().lower()
    for prefix in sorted(_OS_SYNONYMS, key=len, reverse=True):
        if key.startswith(prefix):
            return _OS_SYNONYMS[prefix]
    return None


def _detect_answers(vision_result: dict) -> dict:
    """Параметры, которые Claude Vision уверенно распознал по фото и которые
    совпадают с разрешёнными вариантами, — подставляем сами и прячем из меню,
    по той же логике, что и цвет."""
    p = vision_result.get("parameters", {})
    detected = {}

    # Цвет всегда проставляется — либо распознанный по фото, либо чёрный по
    # умолчанию, если Claude не увидел цвет уверенно. Поэтому поле всегда скрыто из меню.
    detected[_COLOR_FIELD["avito_column"]] = _normalize_color(p.get("color", "")) or "Чёрный"

    storage = _normalize_number(p.get("storage_gb", ""), _DRIVE_SIZE_FIELD["options"])
    if storage:
        detected[_DRIVE_SIZE_FIELD["avito_column"]] = storage

    ram = _normalize_number(p.get("ram_gb", ""), _RAM_SIZE_FIELD["options"])
    if ram:
        detected[_RAM_SIZE_FIELD["avito_column"]] = ram

    os_name = _normalize_os(p.get("os", ""))
    if os_name:
        detected[_OS_FIELD["avito_column"]] = os_name

    return detected


async def start_questionnaire(message, vision_result: dict, listing_id: str, photo_urls: List[str],
                              card_bytes: Optional[bytes] = None):
    chat_id = message.chat.id
    defaults = load_defaults()
    answers = dict(defaults)
    default_columns = set(defaults.keys())

    for column, value in _detect_answers(vision_result).items():
        answers[column] = value
        default_columns.add(column)

    SESSIONS[chat_id] = {
        "mode": "listing",
        "vision": vision_result,
        "answers": answers,
        "default_columns": default_columns,
        "multi_current": {},
        "listing_id": listing_id,
        "photo_urls": photo_urls,
        "card_bytes": card_bytes,
        "hub_message_id": None,
        "price_asked": False,
    }
    await _advance_listing(message, chat_id)


async def _advance_listing(message, chat_id: int):
    session = SESSIONS[chat_id]
    keyboard = build_flat_keyboard(chat_id)
    if keyboard:
        await message.edit_reply_markup(reply_markup=keyboard)
        session["hub_message_id"] = message.message_id
    if _all_required_answered(chat_id) and not session["price_asked"]:
        session["price_asked"] = True
        await ask_price(message, chat_id)


async def start_defaults(message):
    chat_id = message.chat.id
    SESSIONS[chat_id] = {
        "mode": "defaults",
        "vision": {},
        "answers": dict(load_defaults()),
        "multi_current": {},
    }
    msg = await message.answer(
        "Настрой значения по умолчанию — они будут подставляться автоматически в каждое новое "
        "объявление (можно менять для конкретного товара всё равно):",
        reply_markup=build_hub_keyboard(chat_id),
    )
    SESSIONS[chat_id]["hub_message_id"] = msg.message_id


async def ask_price(message, chat_id: int):
    session = SESSIONS[chat_id]
    p = session["vision"].get("parameters", {})
    detected_price = _normalize_price(p.get("price", ""))
    if detected_price:
        await _price_confirmed(message, chat_id, str(detected_price))
        return

    PRICE_PENDING.add(chat_id)
    await message.answer("Какая цена продажи, ₽? Напиши число.")


def is_awaiting_price(message: Message) -> bool:
    return message.chat.id in PRICE_PENDING


def is_awaiting_address(message: Message) -> bool:
    return message.chat.id in ADDRESS_PENDING


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("avl:"))
async def handle_listing_answer(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = SESSIONS.get(chat_id)
    if not session or session.get("mode") != "listing":
        await callback.answer("Сессия устарела, пришли фото заново.", show_alert=True)
        return

    _, step_str, choice = callback.data.split(":", 2)
    step = int(step_str)
    field = FIELDS[step]

    session["answers"][field["avito_column"]] = field["options"][int(choice)]

    await callback.answer()
    try:
        keyboard = build_flat_keyboard(chat_id)
        if keyboard:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        else:
            await callback.message.edit_text("Все параметры выбраны ✅", reply_markup=None)
    except Exception:
        pass

    if _all_required_answered(chat_id) and not session["price_asked"]:
        session["price_asked"] = True
        await ask_price(callback.message, chat_id)


@router.callback_query(F.data == "avback")
async def handle_back(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in SESSIONS:
        await callback.answer("Сессия устарела.", show_alert=True)
        return
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_hub_keyboard(chat_id))


@router.callback_query(F.data == "avnotready")
async def handle_not_ready(callback: CallbackQuery):
    await callback.answer("Сначала заполни все поля с кружком ○ — они обязательные.", show_alert=True)


@router.callback_query(F.data.startswith("avf:"))
async def open_field(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    if chat_id not in SESSIONS:
        await callback.answer("Сессия устарела, пришли фото заново.", show_alert=True)
        return
    step = int(callback.data.split(":")[1])
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_field_keyboard(step, chat_id))


@router.callback_query(F.data == "avdone")
async def handle_done(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        await callback.answer("Сессия устарела.", show_alert=True)
        return

    if session["mode"] == "defaults":
        save_defaults(session["answers"])
        await callback.answer()
        await callback.message.edit_text("Сохранено 💾 Эти значения теперь будут подставляться по умолчанию.")
        SESSIONS.pop(chat_id, None)
        return

    if not _all_required_answered(chat_id):
        await callback.answer("Сначала заполни все обязательные поля (○).", show_alert=True)
        return

    await callback.answer()
    await callback.message.edit_reply_markup()
    await ask_price(callback.message, chat_id)


# --- Предпоказ перед публикацией: ✅ Да / ✏️ Редактировать / ✖ Отмена ---
async def _send_gallery_photos(message: Message, chat_id: int, pending: dict) -> None:
    """Переотправляет карточку и фото объявления отдельными сообщениями, чтобы
    юзер мог сохранить их в галерею (долгое нажатие → «Сохранить в галерею»)."""
    from aiogram.types import BufferedInputFile, URLInputFile, InputMediaPhoto

    media = []
    card_bytes = pending.get("card_bytes")
    if card_bytes:
        media.append(InputMediaPhoto(
            media=BufferedInputFile(card_bytes, filename="card.png"),
            caption="📥 Фото для сохранения в галерею"))
    for url in pending.get("photo_urls") or []:
        try:
            media.append(InputMediaPhoto(media=URLInputFile(url)))
        except Exception:
            logger.exception("Не удалось подготовить фото из %s", url[:80])

    if not media:
        await message.answer("Нет фото для сохранения.")
        return

    try:
        # Альбомами по 10 (лимит Telegram).
        for i in range(0, len(media), 10):
            await message.answer_media_group(media=media[i:i + 10])
    except Exception:
        # Если альбом не ушёл (например, битый URL) — шлём карточку по одной.
        logger.exception("Не удалось отправить альбом для галереи")
        if card_bytes:
            await message.answer_photo(BufferedInputFile(card_bytes, filename="card.png"),
                                        caption="📥 Сохраните долгим нажатием")


@router.callback_query(F.data.startswith("pub:"))
async def handle_publish_decision(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    action = callback.data.split(":", 1)[1]

    # «Сохранить в галерею» — переотправляем фото отдельными сообщениями (юзер
    # сохраняет их долгим нажатием). Предпоказ НЕ закрываем: после сохранения
    # юзер всё ещё может разместить/отредактировать/отменить.
    if action == "save":
        pending = PREVIEW_PENDING.get(chat_id)
        if not pending:
            await callback.answer("Предпоказ устарел, начни заново с фото.", show_alert=True)
            return
        await callback.answer("Отправляю фото — сохрани их долгим нажатием")
        await _send_gallery_photos(callback.message, chat_id, pending)
        return

    pending = PREVIEW_PENDING.pop(chat_id, None)

    if action == "no":
        SESSIONS.pop(chat_id, None)
        await callback.answer("Объявление отменено")
        try:
            await callback.message.edit_text("✖ Объявление отменено, данные не опубликованы.")
        except Exception:
            await callback.message.answer("✖ Объявление отменено, данные не опубликованы.")
        return

    if not pending:
        await callback.answer("Предпоказ устарел, начни заново с фото.", show_alert=True)
        return

    if action == "edit":
        # Возвращаем пользователя к правке полей анкеты: показываем новую
        # хаб-клавиатуру отдельным сообщением. PREVIEW_PENDING уже вычищен выше,
        # сессия анкеты снова активна в SESSIONS.
        session = SESSIONS.get(chat_id)
        if not session:
            await callback.answer("Сессия устарела, пришли фото заново.", show_alert=True)
            return
        await callback.answer()
        hub = await callback.message.answer("✏️ Что поправим?", reply_markup=build_hub_keyboard(chat_id))
        session["hub_message_id"] = hub.message_id
        return

    # action == "yes" — публикуем.
    SESSIONS.pop(chat_id, None)
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await callback.message.answer("📦 Публикую объявление...")
    await finalize_listing(
        callback.message,
        pending["vision"], pending["answers"], pending["price"], pending["address"],
        pending["listing_id"], pending.get("photo_urls") or [],
    )


@router.callback_query(F.data.startswith("av:"))
async def handle_answer(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        await callback.answer("Сессия устарела, пришли фото заново.", show_alert=True)
        return

    _, step_str, choice = callback.data.split(":", 2)
    step = int(step_str)
    field = FIELDS[step]

    if choice == "skip":
        session["answers"][field["avito_column"]] = ""
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=build_hub_keyboard(chat_id))
        return

    if choice == "done":
        chosen = session["multi_current"].get(step, set())
        session["answers"][field["avito_column"]] = " | ".join(sorted(chosen))
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=build_hub_keyboard(chat_id))
        return

    option = field["options"][int(choice)]

    if field["multi"]:
        current = session["multi_current"].setdefault(step, set())
        if option in current:
            current.remove(option)
        else:
            current.add(option)
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=build_field_keyboard(step, chat_id))
        return

    session["answers"][field["avito_column"]] = option
    await callback.answer()
    await callback.message.edit_reply_markup(reply_markup=build_hub_keyboard(chat_id))


@router.message(is_awaiting_price)
async def handle_price(message: Message):
    chat_id = message.chat.id
    if chat_id not in SESSIONS:
        PRICE_PENDING.discard(chat_id)
        await message.answer("Сессия устарела, пришли фото заново.")
        return

    price_text = (message.text or "").strip().replace(" ", "")
    if not price_text.isdigit():
        await message.answer("Не понял цену — пришли просто число, например 65000.")
        return

    PRICE_PENDING.discard(chat_id)
    await _price_confirmed(message, chat_id, price_text)


async def _price_confirmed(message: Message, chat_id: int, price_text: str) -> None:
    session = SESSIONS[chat_id]
    session["price"] = price_text

    default_address = load_defaults().get("__address__", "").strip()
    if default_address:
        await finish_questionnaire(message, chat_id, price_text, default_address)
        return

    ADDRESS_PENDING.add(chat_id)
    await message.answer("И последнее — адрес (город, район), который показывать в объявлении:")


@router.message(is_awaiting_address)
async def handle_address(message: Message):
    chat_id = message.chat.id
    session = SESSIONS.get(chat_id)
    if not session:
        ADDRESS_PENDING.discard(chat_id)
        await message.answer("Сессия устарела, пришли фото заново.")
        return

    address = (message.text or "").strip()
    if not address:
        await message.answer("Пришли, пожалуйста, адрес текстом.")
        return

    ADDRESS_PENDING.discard(chat_id)
    await finish_questionnaire(message, chat_id, session["price"], address)


def is_supplement_note(message: Message) -> bool:
    """Текст, присланный после фото (пока идёт анкета кнопками), но не ответ
    на конкретный вопрос про цену/адрес — просто дополнение к объявлению."""
    chat_id = message.chat.id
    return (
        message.text is not None
        and not message.text.startswith("/")
        and chat_id in SESSIONS
        and SESSIONS[chat_id].get("mode") == "listing"
        and chat_id not in PRICE_PENDING
        and chat_id not in ADDRESS_PENDING
    )


@router.message(is_supplement_note)
async def handle_supplement_note(message: Message):
    chat_id = message.chat.id
    session = SESSIONS[chat_id]
    p = session["vision"].setdefault("parameters", {})
    existing = p.get("extra_note", "")
    p["extra_note"] = f"{existing}\n{message.text}".strip() if existing else message.text
    await message.answer("Добавил в описание.")


def _build_ad_text(vision: dict, answers: dict, price: str) -> str:
    # Краткий техсписок для превью в Telegram. Полное маркетинговое описание
    # (build_description) уходит только в таблицу/фид Avito.
    p = vision.get("parameters", {})
    brand = p.get("brand", "").strip()
    model = p.get("model", "").strip()
    cpu = p.get("cpu", "").strip()
    gpu = p.get("gpu", "").strip()
    ram = answers.get("Объем оперативной памяти") or p.get("ram_gb", "")
    storage = p.get("storage_gb", "").strip()
    screen = p.get("screen_size", "").strip()
    resolution = p.get("screen_resolution", "").strip()
    os_name = answers.get("Операционная система") or p.get("os", "")
    battery_cycles = p.get("battery_cycle_count", "").strip()

    title = " ".join(x for x in [brand, model] if x) or "Ноутбук"

    spec_lines = []
    if cpu:
        spec_lines.append(f"Процессор: {cpu}")
    if gpu:
        spec_lines.append(f"Видеокарта: {gpu}")
    if ram:
        spec_lines.append(f"Оперативная память: {ram} ГБ")
    if storage:
        spec_lines.append(f"Накопитель: {answers.get('Конфигурация накопителей', '')} {round_storage(storage)} ГБ".strip())
    if screen:
        screen_line = f"Экран: {screen}\""
        if resolution:
            screen_line += f", {resolution}"
        spec_lines.append(screen_line)
    if os_name:
        spec_lines.append(f"ОС: {os_name}")
    if battery_cycles:
        spec_lines.append(f"Циклов АКБ: {battery_cycles}")

    body = "\n".join(spec_lines)
    extra_note = p.get("extra_note", "").strip()
    if extra_note:
        body += f"\n\n{extra_note}"
    return f"{title}\n\n{body}\n\nЦена: {price} ₽"


async def finalize_listing(message, vision: dict, answers: dict, price: str, address: str,
                            listing_id: str, photo_urls: Optional[List[str]] = None):
    photo_urls = photo_urls or []
    params = vision.get("parameters", {})

    ad_text = _build_ad_text(vision, answers, price)
    await message.answer(ad_text)

    try:
        await append_row(params, answers, price, address, listing_id, photo_urls)
        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        text = f"Инфа в таблице:\n{sheet_url}"
        if photo_urls:
            text += f"\nфото:\n{photo_urls[0]}"
        await message.answer(text)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось записать в таблицу: {e}")

    try:
        append_listing(params, answers, price, address, listing_id, photo_urls)
        if not photo_urls:
            await message.answer(
                f"Добавлено в avito_export.xlsx как {listing_id}.\n"
                "⚠️ Ссылки на фото не загрузились автоматически — открой файл и вставь их "
                "вручную в колонку «Ссылки на фото» (публичные ссылки, не файлы из Telegram)."
            )
    except Exception as e:
        await message.answer(f"⚠️ Не удалось записать в avito_export.xlsx: {e}")
        return

    try:
        await upload_feed(EXPORT_PATH)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось обновить фид для автозагрузки Avito: {e}")
        return

    await message.answer(
        "✅ Объявление успешно размещено — данные записаны, фид для Avito обновлён. "
        "На самом Avito появится по расписанию автозагрузки (обычно в течение часа)."
    )


async def finish_questionnaire(message, chat_id: int, price: str, address: str):
    """Финал анкеты: показываем предпоказ (карточка + сводка + кнопки) и НЕ
    публикуем сразу — пользователь подтверждает через pub:yes / правит pub:edit
    / отменяет pub:no. Сессию из SESSIONS не вычищаем до подтверждения, чтобы
    pub:edit мог вернуть клавиатуру правки."""
    session = SESSIONS.get(chat_id)
    if not session:
        await message.answer("Сессия устарела. Пришли фото заново.")
        return
    hub_message_id = session.get("hub_message_id")
    if hub_message_id:
        try:
            await message.bot.edit_message_reply_markup(chat_id=chat_id, message_id=hub_message_id)
        except Exception:
            pass

    p = session["vision"].get("parameters", {})
    listing_id = make_listing_id(model=p.get("model", ""), cpu=p.get("cpu", ""), price=price)

    PREVIEW_PENDING[chat_id] = {
        "vision": session["vision"],
        "answers": session["answers"],
        "price": price,
        "address": address,
        "listing_id": listing_id,
        "photo_urls": session.get("photo_urls") or [],
        "card_bytes": session.get("card_bytes"),
    }

    summary = _preview_summary(session["vision"], session["answers"], price, address)
    # Каждая кнопка на своей строке — во всю ширину (максимальный размер в Telegram).
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, размещаем", callback_data="pub:yes")],
        [InlineKeyboardButton(text="✏️ Редактировать", callback_data="pub:edit")],
        [InlineKeyboardButton(text="📥 Сохранить в галерею", callback_data="pub:save")],
        [InlineKeyboardButton(text="✖ Отмена", callback_data="pub:no")],
    ])

    card_bytes = session.get("card_bytes")
    caption = f"📋 Проверьте объявление перед публикацией:\n\n{summary}"
    try:
        if card_bytes:
            from aiogram.types import BufferedInputFile
            await message.answer_photo(
                BufferedInputFile(card_bytes, filename="card.png"),
                caption=caption, reply_markup=keyboard)
        else:
            await message.answer(caption, reply_markup=keyboard)
    except Exception:
        # Если фото не отправилось — покажем хотя бы текст с кнопками.
        await message.answer(caption, reply_markup=keyboard)


def _preview_summary(vision: dict, answers: dict, price: str, address: str) -> str:
    p = vision.get("parameters", {})
    title = f"{p.get('brand','').strip()} {p.get('model','').strip()}".strip() or "Ноутбук"
    bits = []
    if p.get("screen_size"):
        bits.append(f'Экран {p["screen_size"]}"')
    if p.get("cpu"):
        bits.append(p["cpu"])
    if p.get("ram_gb"):
        bits.append(f"{p['ram_gb']} ГБ ОЗУ")
    if p.get("storage_gb"):
        bits.append(f"{round_storage(p['storage_gb'])} ГБ SSD")
    lines = [f"📷 {title}"]
    if bits:
        lines.append("⚙ " + " · ".join(bits))
    lines.append(f"💵 Цена: {price} ₽")
    if address:
        lines.append(f"📍 {address}")
    return "\n".join(lines)
