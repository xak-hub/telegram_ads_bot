import asyncio
import fcntl
import logging
import os
import sys
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message,
)
from dotenv import load_dotenv

load_dotenv()

import questionnaire
import users
import yandex_storage
import card_service
import avito_api
import avito_sync
import pc_feed
import pc_sheets
import regard_publisher
from claude_vision import analyze_photo
from avito_row import make_listing_id
from avito_export import (EXPORT_PATH, list_listing_ids, remove_listings,
                         get_listing_row, list_listing_rows)
from avito_sync import sync_removed_from_avito
from yandex_storage import download_feed

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
dp.include_router(questionnaire.router)

# Буфер для фото, присланных одним разом (media group) — Telegram шлёт их
# отдельными апдейтами с одним media_group_id, собираем с небольшой задержкой.
MEDIA_GROUPS: dict[str, list[Message]] = {}
MEDIA_GROUP_TASKS: dict[str, asyncio.Task] = {}
MEDIA_GROUP_DELAY = 1.5

# Сессия сбора одного объявления: фото и текстовые уточнения копятся здесь,
# пока пользователь не нажмёт «Готово» (или не пришлёт /done).
# chat_id -> {"photos": [Message,...], "notes": [str,...], "prompt_id": int|None}
COLLECTING: dict[int, dict] = {}

# Блокировка на чат — несколько пачек фото могут прилететь почти одновременно,
# без неё бот пытается редактировать одно и то же сообщение параллельно и вместо
# этого шлёт новое каждый раз (отсюда путаница со счётчиком фото).
COLLECTING_LOCKS: dict[int, asyncio.Lock] = {}

# Чаты, для которых прямо сейчас идёт анализ фото — защита от повторного
# запуска, если что-то триггернёт обработку дважды (например /done почти
# одновременно с автозапуском), пока идёт первый анализ (20-40 секунд).
PROCESSING: set[int] = set()

# Автозапуск анализа: без кнопки «Готово» — если после последнего фото/текста
# в течение AUTO_START_DELAY секунд ничего нового не пришло, начинаем сами.
# Каждое новое фото/сообщение сбрасывает таймер заново.
AUTO_START_DELAY = 5
AUTO_START_TASKS: dict[int, asyncio.Task] = {}

# Бегущая точка вместо пояснительного текста под счётчиком фото.
DOT_FRAMES = ["●○○○○", "○●○○○", "○○●○○", "○○○●○", "○○○○●"]
DOT_ANIMATION_DELAY = 0.5
PHOTO_ANIM_TASKS: dict[int, asyncio.Task] = {}

# После сбора фото — пауза на выбор режима обработки (две кнопки), пока
# пользователь не нажмёт одну из них. chat_id -> {"photos", "notes", "message"}.
PENDING_MODE: dict[int, dict] = {}

# /upgrade — мастер «вариант с бОльшим SSD/RAM»: выбор устройства кнопками →
# SSD → RAM → цена, популярные значения кнопками, «своё» — текстом.
# chat_id -> {"storage_gb","price"} — финальное состояние, ждёт фото.
UPGRADE_PENDING: dict[int, dict] = {}
# Промежуточное состояние мастера: {"rows", "idx", "params", "awaiting"}.
UPGRADE_FLOW: dict[int, dict] = {}


async def _mode_keyboard() -> InlineKeyboardMarkup:
    # Каждая кнопка на своей строке — крупнее и заметнее, чем в один ряд.
    # full: фон удаляется у всех фото + карточка с характеристиками.
    # specs: карточка строится (обложка вырезается), галерея — как есть.
    # raw: ничего не трогаем, только распознавание данных для Авито.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Удаление фона + карточка", callback_data="procmode:full")],
        [InlineKeyboardButton(text="Карточка, фон не удалять", callback_data="procmode:specs")],
        [InlineKeyboardButton(text="Без обработки (как есть)", callback_data="procmode:raw")],
    ])


def _lock_for(chat_id: int) -> asyncio.Lock:
    return COLLECTING_LOCKS.setdefault(chat_id, asyncio.Lock())


def _cancel_auto_start(chat_id: int) -> None:
    existing = AUTO_START_TASKS.pop(chat_id, None)
    if existing:
        existing.cancel()


def _cancel_photo_anim(chat_id: int) -> None:
    existing = PHOTO_ANIM_TASKS.pop(chat_id, None)
    if existing:
        existing.cancel()


async def _animate_photo_prompt(chat_id: int, message_id: int, count: int) -> None:
    i = 0
    try:
        while True:
            await asyncio.sleep(DOT_ANIMATION_DELAY)
            frame = DOT_FRAMES[i % len(DOT_FRAMES)]
            try:
                await bot.edit_message_text(
                    f"Добавлено фото: {count}.\n{frame}", chat_id=chat_id, message_id=message_id
                )
            except Exception:
                pass
            i += 1
    except asyncio.CancelledError:
        pass


def _schedule_auto_start(chat_id: int, anchor_message: Message) -> None:
    _cancel_auto_start(chat_id)
    AUTO_START_TASKS[chat_id] = asyncio.create_task(_auto_start_later(chat_id, anchor_message))


async def _auto_start_later(chat_id: int, anchor_message: Message) -> None:
    try:
        await asyncio.sleep(AUTO_START_DELAY)
    except asyncio.CancelledError:
        return
    AUTO_START_TASKS.pop(chat_id, None)
    await _finalize_collection(chat_id, anchor_message)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    COLLECTING.pop(message.chat.id, None)
    UPGRADE_PENDING.pop(message.chat.id, None)
    UPGRADE_FLOW.pop(message.chat.id, None)
    # Сбрасываем зависшие состояния предпоказа/анкеты, чтобы /start начинал чисто.
    questionnaire.PREVIEW_PENDING.pop(message.chat.id, None)
    questionnaire.SESSIONS.pop(message.chat.id, None)

    # Кабинет: автоматическая регистрация при первом /start (анонимно).
    username = message.from_user.username if message.from_user else ""
    is_new = users.register_if_new(message.chat.id, username)
    users.set_username(message.chat.id, username)

    # Реферальная ссылка: /start ref_<chat_id пригласившего>.
    referral_welcome = ""
    args = (message.text or "").split()[1:]
    if args and args[0].startswith("ref_"):
        try:
            inviter = int(args[0][4:])
        except ValueError:
            inviter = None
        if inviter and inviter != message.chat.id and is_new:
            users.link_referral(message.chat.id, inviter)
            referral_welcome = (
                "🎁 Пришёл по приглашению — учтено! После твоего первого "
                "объявления пригласивший получит +5 объявлений.\n\n"
            )
    await message.answer(
        referral_welcome
        + "Пришли фото товара — можно одно, можно несколько по очереди или пачкой. "
        "В любой момент можно дописать текстом детали, которых не видно на фото "
        "(бренд, модель, доп. характеристики) — просто напиши сообщением.\n\n"
        "Совет по качеству: обычное фото в Telegram сжимается. Если нужно "
        "оригинальное разрешение (для чёткости на Avito) — прикрепляй фото как "
        "«Файл»/«Без сжатия» (в Telegram при выборе фото есть такая опция), а не "
        "как обычное сжатое фото.\n\n"
        f"Когда всё прислал(а) — просто подожди {AUTO_START_DELAY} секунд, я начну "
        "анализировать сам. Не хочешь ждать — пришли команду /done.\n\n"
        "Команда /defaults — настроить значения по умолчанию (в чате, кнопками).\n\n"
        "Системные блоки с Регарда: /regard [N] — опубликовать топ-N сборок, "
        "/pcfeed — список в фиде, /pcsold <№> — убрать проданное. "
        "Каждый день автоматически добавляется до 10 новых сборок.\n\n"
        "Дальше я распознаю характеристики, пришлю кнопки для заполнения оставшихся "
        "параметров и цены прямо в чате, а фото сам залью на Яндекс.Диск "
        "и запишу всё в Google Sheets и файл для Avito.\n\n"
        f"Твой тариф: Free ({users.LIMITS['free']} объявлений в месяц). "
        "Тарифы и апгрейд: /tariff. Кабинет: /profile. Справка: /help."
    )


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    """Кабинет: тариф, израсходовано/лимит, бонусы, реферальная ссылка."""
    chat_id = message.chat.id
    users.register_if_new(chat_id, message.from_user.username if message.from_user else "")
    u = users.get_user(chat_id)
    users._ensure_month(u)
    u = users.get_user(chat_id)
    tariff = users._effective_tariff(u)
    if tariff == "free":
        limit = users.LIMITS["free"]
        used = min(u["listings_count"], limit)
        remain = max(0, limit + u["bonus_listings"] - used)
        tariff_line = f"Тариф: Free ({used}/{limit + u['bonus_listings']} объявлений в месяце, осталось {remain})"
    else:
        used = u["listings_count"]
        bonus = u["bonus_listings"]
        tariff_line = f"Тариф: {tariff.capitalize()} ({used} объявлений в месяце" + (f", бонусов: {bonus}" if bonus else "") + ")"
        if u["tariff_until"]:
            tariff_line += f", действует до {u['tariff_until']}"
    reg = (u["registered_at"] or "")[:10]

    me = await message.bot.me()
    ref_link = f"https://t.me/{me.username}?start=ref_{chat_id}"
    await message.answer(
        "👤 Личный кабинет\n\n"
        f"{tariff_line}\n"
        f"С нами с: {reg}\n\n"
        f"🎁 Приведи друга — получи +5 объявлений:\n{ref_link}\n\n"
        "Тарифы: /tariff · Справка: /help"
    )


@dp.message(Command("tariff"))
async def cmd_tariff(message: Message):
    """Тарифная сетка + кнопки выбора (оплата подключается отдельным этапом)."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Start — 290₽/мес (30 об.)", callback_data="tar:start")],
        [InlineKeyboardButton(text="Pro — 690₽/мес (100 об.)", callback_data="tar:pro")],
        [InlineKeyboardButton(text="Business — 1990₽/мес (500 об.)", callback_data="tar:business")],
        [InlineKeyboardButton(text="Годовые: −25% (Start 2610₽ · Pro 6210₽ · Biz 5965₽)", callback_data="tar:yearly_info")],
    ])
    await message.answer(
        "💳 Тарифы\n\n"
        "Free — 5 объявлений/мес, бесплатно.\n"
        "Start — 290₽/мес, 30 объявлений.\n"
        "Pro — 690₽/мес, 100 объявлений.\n"
        "Business — 1990₽/мес, 500 объявлений.\n\n"
        "Выбери тариф:", reply_markup=kb)


@dp.callback_query(F.data.startswith("tar:"))
async def handle_tariff_choice(callback: CallbackQuery):
    kind = callback.data.split(":", 1)[1]
    await callback.answer()
    await callback.message.answer(
        "💳 Приём оплат подключается отдельным этапом (Telegram Stars / ЮKassa).\n"
        "Твой выбор зафиксирован: " + kind + " — сообщу, как оплата заработает.")


@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 Справка по командам\n\n"
        "Ноутбуки:\n"
        "/start — начать работу / сброс сессии\n"
        "/done — обработать присланные фото сразу\n"
        "/upgrade <ID> <SSD> <цена> [RAM] — клон с бОльшим SSD/RAM (фото следом)\n"
        "/defaults — значения по умолчанию (адрес, состояние и т.д.)\n"
        "/feed — список объявлений в фиде\n"
        "/sold <№ или ID> — убрать проданное из фида (Авито уберёт из кабинета)\n"
        "/sync — сверить статусы с Авито через API (нужна настройка)\n\n"
        "Системные блоки (Регард):\n"
        "/regard [N] — опубликовать топ-N сборок (по умолчанию 10)\n"
        "/pcfeed — список сборок в фиде\n"
        "/pcsold <№ или ID> — убрать проданные сборки\n"
        "/pcrefresh — пересобрать сборки из свежих данных Регарда\n\n"
        "Как продавать ноутбука:\n"
        "1) пришли фото (можно пачкой, текстом можно дописать детали)\n"
        "2) /done или подожди автостарта → прогресс → карточка и фото\n"
        "3) анкета кнопками → цена → адрес\n"
        "4) предпоказ: ✅ Да / ✏️ Редактировать / 📥 Сохранить / ✖ Отмена\n"
        "5) продал → /feed → /sold <номер>"
    )


@dp.message(Command("defaults"))
async def cmd_defaults(message: Message):
    await questionnaire.start_defaults(message)


@dp.message(Command("done"))
async def cmd_done(message: Message):
    await _finalize_collection(message.chat.id, message)


@dp.message(Command("sync"))
async def cmd_sync(message: Message):
    if not avito_api.is_configured():
        await message.answer(
            "Avito API не настроен — добавь AVITO_CLIENT_ID и AVITO_CLIENT_SECRET "
            "в .env, тогда бот сможет сам убирать проданные объявления из фида.\n\n"
            "Пока можно вручную: /feed — список, /sold <номер> — убрать проданное."
        )
        return
    await message.answer("Проверяю статусы объявлений на Avito…")
    try:
        res = await sync_removed_from_avito()
    except Exception as e:
        logger.exception("Ручной синхрон с Avito упал")
        await message.answer(f"⚠️ Синхрон не удался: {e}")
        return
    if res["removed"]:
        await message.answer(
            f"Убрал из фида снятые/проданные ({len(res['removed'])}) — фид перезалит:\n"
            + "\n".join(res["removed"])
        )
    else:
        await message.answer(
            f"Проверил {res['checked']} — снятых/проданных нет, фид не менял."
        )


@dp.message(Command("feed"))
async def cmd_feed(message: Message):
    """Показывает объявления, которые сейчас лежат в фиде автозагрузки Avito.
    Автозагрузка считает фид «списком того, что должно висеть»: пока ID есть в
    файле, Avito поднимет объявление заново, даже если оно продано и снято."""
    ids = list_listing_ids()
    if not ids:
        await message.answer("Фид пуст — активных объявлений нет.")
        return
    lines = [f"{i}. {id_}" for i, id_ in enumerate(ids, 1)]
    await message.answer(
        "📋 Сейчас в фиде (автозагрузка Avito):\n" + "\n".join(lines)
        + "\n\nПродал товар — убери его из фида: /sold <номер или ID>"
    )


@dp.message(Command("sold"))
async def cmd_sold(message: Message):
    """Убирает проданные/снятые объявления из фида и перезаливает его — иначе
    автозагрузка Avito снова поднимет их в личный кабинет. Аргументы: номера
    из /feed и/или точные ID, например: /sold 1 3 5 или /sold T14-6557-i5-1234."""
    args = message.text.split()[1:]
    if not args:
        await message.answer(
            "Укажи номера из /feed или ID: /sold 1 3 или /sold T14-6557-i5-1234"
        )
        return
    ids_in_feed = list_listing_ids()
    by_number = {str(i): id_ for i, id_ in enumerate(ids_in_feed, 1)}
    targets = {by_number.get(a, a) for a in args}
    targets &= set(ids_in_feed)  # отсекаем опечатки/несуществующие

    if not targets:
        await message.answer("Не нашёл таких объявлений в фиде. Список: /feed")
        return

    removed = remove_listings(targets)
    if not removed:
        await message.answer("Ничего не убрал — возможно, их уже нет в фиде.")
        return
    try:
        await yandex_storage.upload_feed(EXPORT_PATH)
        await message.answer(
            f"✅ Убрал из фида {removed}: " + ", ".join(sorted(targets))
            + "\nФид перезалит — Avito уберёт их из кабинета по расписанию "
            "автозагрузки (обычно в течение часа)."
        )
    except Exception as e:
        logger.exception("Не удалось перезалить фид после /sold")
        await message.answer(
            f"⚠️ Убрал локально ({removed}), но фид не перезалился: {e}\n"
            "Перезалей вручную или повтори /sold с этими ID позже."
        )


def _upgrade_params_from_row(row: dict) -> dict:
    """Клонирует vision-параметры из строки фида (без GLM): основные поля из
    колонок, частоты CPU и циклы АКБ — из текста описания, флаги
    сенсорный/трансформер — из префиксов заголовка."""
    import re as _re
    params = {
        "brand": row.get("Производитель", ""),
        "model": row.get("Модель", ""),
        "cpu": row.get("Процессор", ""),
        "gpu": row.get("Видеокарта", ""),
        "gpu_vram_gb": row.get("Объем видеопамяти", ""),
        "ram_gb": row.get("Объем оперативной памяти", ""),
        "storage_gb": row.get("Общий объем накопителей", ""),
        "screen_size": row.get("Диагональ экрана ноутбука", ""),
        "screen_resolution": row.get("Разрешение экрана", ""),
        "os": row.get("Операционная система", ""),
        "color": row.get("Цвет", ""),
    }
    desc = row.get("Описание объявления", "")
    m = _re.search(r"\((\d+(?:\.\d+)?)-(\d+(?:\.\d+)?) ГГц\)", desc)
    if m:
        params["cpu_ghz_min"], params["cpu_ghz_max"] = m.group(1), m.group(2)
    m = _re.search(r"Циклов АКБ: (\d+)", desc)
    if m:
        params["battery_cycle_count"] = m.group(1)
    title = row.get("Название объявления", "")
    if title.lower().startswith("сенсорный"):
        params["touchscreen"] = "да"
    if "трансформер" in title.lower():
        params["transformer"] = "да"
    return params


def _upgrade_devices_keyboard() -> InlineKeyboardMarkup:
    """Кнопки выбора устройства: последние объявления фида, свежие сверху."""
    rows = list_listing_rows()
    last = rows[-25:][::-1]  # свежие 25, последние первыми
    buttons = []
    for i, row in enumerate(last):
        brand_model = f"{row.get('Производитель','')} {row.get('Модель','')}".strip() or "Ноутбук"
        price = row.get("Цена", "")
        label = f"{i+1}. {brand_model[:32]}"
        if price:
            label += f" · {price}₽"
        buttons.append([InlineKeyboardButton(
            text=label, callback_data=f"upg:sel:{i}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons), last


def _upgrade_value_keyboard(kind: str, current: str) -> InlineKeyboardMarkup:
    """Кнопки выбора SSD/RAM: популярные значения + «не менять» + «своё»."""
    popular = ["256", "512", "1024", "2048"] if kind == "ssd" else ["8", "16", "32", "64"]
    rows = []
    for i in range(0, len(popular), 2):
        rows.append([InlineKeyboardButton(
            text=(f"{v} ГБ" + (" ✓" if v == current else "")),
            callback_data=f"upg:{kind}:{v}") for v in popular[i:i+2]])
    rows.append([InlineKeyboardButton(text="↩ Не менять", callback_data=f"upg:{kind}:keep")])
    rows.append([InlineKeyboardButton(text="✍️ Своё значение", callback_data=f"upg:{kind}:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _upgrade_finish(chat_id: int, message) -> None:
    """Финал мастера: переносит параметры в UPGRADE_PENDING и просит фото."""
    flow = UPGRADE_FLOW.pop(chat_id, None)
    if not flow or not flow.get("params"):
        await message.answer("Сессия апгрейда устарела — начни заново: /upgrade")
        return
    params = flow["params"]
    UPGRADE_PENDING[chat_id] = {"params": params}
    ssd = params.get("storage_gb", "?")
    ram = params.get("ram_gb", "?")
    price = params.get("price", "?")
    await message.answer(
        f"🆙 Готово: «{params.get('brand','')} {params.get('model','')}» — "
        f"SSD {ssd} ГБ, RAM {ram} ГБ, цена {price} ₽.\n"
        "Характеристики клонированы, распознавание не нужно.\n"
        "📷 Пришли фото этого же ноутбука — опубликую вариант.\n"
        "Старое объявление не трогаю."
    )


@dp.message(Command("upgrade"))
async def cmd_upgrade(message: Message):
    """Мастер «вариант с бОльшим SSD/RAM»: /upgrade без аргументов открывает
    выбор устройства кнопками, далее SSD → RAM → цена (популярные значения
    кнопками, свои — текстом). Старый синтаксис тоже работает:
    /upgrade <ID> <SSD-ГБ> <цена> [RAM-ГБ]."""
    args = message.text.split()[1:]
    chat_id = message.chat.id

    if not args:
        kb, rows = _upgrade_devices_keyboard()
        if not rows:
            await message.answer("Фид пуст — нечего апгрейдить. Сначала опубликуй ноутбук.")
            return
        UPGRADE_FLOW[chat_id] = {"rows": rows, "idx": None, "params": None, "awaiting": None}
        await message.answer(
            "🆙 Апгрейд ноутбука — шаг 1 из 4: выбери устройство",
            reply_markup=kb)
        return

    # Легаси-синтаксис: /upgrade <ID> <SSD> <цена> [RAM]
    listing_id = args[0]
    row = get_listing_row(listing_id)
    if not row:
        await message.answer(f"Не нашёл в фиде «{listing_id}». Список: /upgrade (кнопками)")
        return
    if len(args) < 3:
        await message.answer(
            f"Формат: /upgrade <ID> <SSD-ГБ> <цена> [RAM-ГБ]\n"
            f"Например: /upgrade {listing_id} 1024 55000 32\n"
            "Или просто /upgrade — выберу кнопками.")
        return
    try:
        ssd = int(args[1]); price = int(args[2])
        ram = int(args[3]) if len(args) > 3 else None
        if not (64 <= ssd <= 8192) or price <= 0:
            raise ValueError
        if ram is not None and not (4 <= ram <= 128):
            raise ValueError
    except ValueError:
        await message.answer("Не понял числа. SSD 64–8192, RAM 4–128, цена — целое.")
        return
    params = _upgrade_params_from_row(row)
    params["storage_gb"] = str(ssd)
    params["price"] = str(price)
    if ram:
        params["ram_gb"] = str(ram)
    UPGRADE_PENDING[chat_id] = {"params": params}
    await message.answer(
        f"🆙 Апгрейд «{params['brand']} {params['model']}»: SSD {ssd} ГБ"
        + (f", RAM {ram} ГБ" if ram else "") + f", цена {price:,} ₽.".replace(",", " ")
        + "\n📷 Пришли фото ноутбука. Старое объявление не трогаю.")


@dp.callback_query(F.data.startswith("upg:"))
async def handle_upgrade_flow(callback: CallbackQuery):
    """Кнопки мастера: выбор устройства → SSD → RAM (цена — текстом)."""
    chat_id = callback.message.chat.id
    flow = UPGRADE_FLOW.get(chat_id)
    if not flow:
        await callback.answer("Сессия устарела — /upgrade заново", show_alert=True)
        return
    _, step, value = callback.data.split(":", 2)

    async def edit(text, kb=None):
        try:
            await callback.message.edit_text(text, reply_markup=kb)
        except Exception:
            pass

    if step == "sel":
        i = int(value)
        row = flow["rows"][i]
        flow["idx"] = i
        flow["params"] = _upgrade_params_from_row(row)
        brand_model = f"{row.get('Производитель','')} {row.get('Модель','')}".strip()
        await edit(
            f"🆙 Шаг 2 из 4 — {brand_model}\nВыбери новый SSD:",
            _upgrade_value_keyboard("ssd", flow["params"].get("storage_gb", "")))
        await callback.answer()

    elif step == "ssd":
        params = flow["params"]
        if value == "keep":
            await callback.answer("SSD без изменений")
        elif value == "custom":
            flow["awaiting"] = "ssd"
            await edit("Напиши новый SSD в ГБ (например: 1024)")
            await callback.answer()
            return
        else:
            params["storage_gb"] = value
            await callback.answer(f"SSD {value} ГБ")
        await edit(
            f"🆙 Шаг 3 из 4 — SSD {params.get('storage_gb','')} ГБ\nВыбери RAM:",
            _upgrade_value_keyboard("ram", params.get("ram_gb", "")))

    elif step == "ram":
        params = flow["params"]
        if value == "custom":
            flow["awaiting"] = "ram"
            await edit("Напиши RAM в ГБ (например: 32)")
            await callback.answer()
            return
        if value != "keep":
            params["ram_gb"] = value
        await callback.answer(f"RAM {params.get('ram_gb','')} ГБ")
        flow["awaiting"] = "price"
        old_price = flow["rows"][flow["idx"]].get("Цена", "") if flow.get("idx") is not None else ""
        hint = f" (сейчас {old_price}₽)" if old_price else ""
        await edit(f"🆙 Шаг 4 из 4 — цена{hint}\nНапиши новую цену в ₽ (например: 55000)")


@dp.message(F.text, F.func(lambda m: m.chat.id in UPGRADE_FLOW and UPGRADE_FLOW[m.chat.id].get("awaiting")))
async def handle_upgrade_text(message: Message):
    """Свои значения в мастере: SSD/RAM/цена — текстом."""
    chat_id = message.chat.id
    flow = UPGRADE_FLOW[chat_id]
    awaiting = flow["awaiting"]
    text = (message.text or "").strip().replace(" ", "")
    if text.startswith("/"):
        return  # команды не съедаем
    try:
        val = int(text)
    except ValueError:
        await message.answer("Нужно целое число. Попробуй ещё раз.")
        return
    params = flow["params"]

    if awaiting == "ssd":
        if not (64 <= val <= 8192):
            await message.answer("SSD — от 64 до 8192 ГБ. Попробуй ещё раз.")
            return
        params["storage_gb"] = str(val)
        flow["awaiting"] = None
        await message.answer(f"SSD {val} ГБ ✓")
        await message.answer(
            "🆙 Шаг 3 из 4 — выбери RAM:",
            reply_markup=_upgrade_value_keyboard("ram", params.get("ram_gb", "")))
    elif awaiting == "ram":
        if not (4 <= val <= 128):
            await message.answer("RAM — от 4 до 128 ГБ. Попробуй ещё раз.")
            return
        params["ram_gb"] = str(val)
        flow["awaiting"] = "price"
        await message.answer(f"RAM {val} ГБ ✓")
        old_price = flow["rows"][flow["idx"]].get("Цена", "") if flow.get("idx") is not None else ""
        hint = f" (сейчас {old_price}₽)" if old_price else ""
        await message.answer(f"🆙 Шаг 4 из 4 — цена{hint}\nНапиши новую цену в ₽")
    elif awaiting == "price":
        if val <= 0:
            await message.answer("Цена — положительное число. Попробуй ещё раз.")
            return
        params["price"] = str(val)
        flow["awaiting"] = None
        await _upgrade_finish(chat_id, message)


@dp.message(Command("regard"))
async def cmd_regard(message: Message):
    """Публикует топовые сборки Регарда в фид системных блоков: /regard [N].
    По умолчанию N = REGARD_DAILY_LIMIT (10). Уже опубликованные сборки не
    дублируются — при изменившейся цене на Регарде обновляется их цена."""
    args = message.text.split()[1:]
    try:
        limit = int(args[0]) if args else REGARD_DAILY_LIMIT
    except ValueError:
        await message.answer("Сколько сборок опубликовать? Например: /regard 5")
        return
    limit = max(1, min(limit, 30))

    status = await message.answer(f"Беру топ-{limit} сборок Регарда «в наличии»…")
    try:
        res = await regard_publisher.publish_new(limit)
    except Exception as e:
        logger.exception("Публикация сборок Регарда упала")
        await message.answer(f"⚠️ Не удалось: {e}")
        return

    lines = []
    if res["added"]:
        lines.append(f"✅ Добавил ({len(res['added'])}): " + ", ".join(res["added"]))
    if res["updated"]:
        lines.append(f"💰 Обновил цену ({len(res['updated'])}): " + ", ".join(res["updated"]))
    if not res["added"] and not res["updated"]:
        lines.append("Новых полных сборок не нашёл — всё топовое уже опубликовано.")
    lines.append(f"Отсеял неполных: {len(res['rejected'])}")
    if res["feed_url"]:
        lines.append(f"\nФид перезалит: {res['feed_url']}")
        lines.append(
            "Если автозагрузка Avito ещё не смотрит на этот URL — добавь второе "
            "подключение автозагрузки (файл по ссылке)."
        )
    await status.edit_text("\n".join(lines))


@dp.message(Command("pcfeed"))
async def cmd_pcfeed(message: Message):
    """Показывает сборки из фида системных блоков (аналог /feed для ноутбуков)."""
    ids = pc_feed.list_listing_ids()
    if not ids:
        await message.answer(
            "Фид системных блоков пуст. Наполнить: /regard (топ сборок Регарда)."
        )
        return
    lines = [f"{i}. {id_}" for i, id_ in enumerate(ids, 1)]
    await message.answer(
        "🖥 Сейчас в фиде системных блоков (автозагрузка Avito):\n" + "\n".join(lines)
        + "\n\nПродали — убери из фида: /pcsold <номер или ID>"
    )


@dp.message(Command("pcsold"))
async def cmd_pcsold(message: Message):
    """Убирает проданные сборки из фида системных блоков и перезаливает его
    (аналог /sold): /pcsold 1 3 или /pcsold pc-2027409."""
    args = message.text.split()[1:]
    if not args:
        await message.answer("Укажи номера из /pcfeed или ID: /pcsold 1 3 или /pcsold pc-2027409")
        return
    ids_in_feed = pc_feed.list_listing_ids()
    by_number = {str(i): id_ for i, id_ in enumerate(ids_in_feed, 1)}
    targets = {by_number.get(a, a) for a in args}
    targets &= set(ids_in_feed)  # отсекаем опечатки/несуществующие

    if not targets:
        await message.answer("Не нашёл таких сборок в фиде. Список: /pcfeed")
        return

    removed = pc_feed.remove_listings(targets)
    if not removed:
        await message.answer("Ничего не убрал — возможно, их уже нет в фиде.")
        return
    try:
        await yandex_storage.upload_feed(pc_feed.EXPORT_PATH, yandex_storage.PC_OBJECT_KEY)
        await message.answer(
            f"✅ Убрал из фида системных блоков {removed}: " + ", ".join(sorted(targets))
            + "\nФид перезалит — Avito уберёт их по расписанию автозагрузки."
        )
    except Exception as e:
        logger.exception("Не удалось перезалить ПК-фид после /pcsold")
        await message.answer(
            f"⚠️ Убрал локально ({removed}), но фид не перезалился: {e}\n"
            "Повтори /pcsold с этими ID позже."
        )


@dp.message(Command("pcrefresh"))
async def cmd_pcrefresh(message: Message):
    """Пересобирает все опубликованные сборки из свежих данных Регарда —
    обновляет цены и заново прогоняет парсинг полей (для улучшений парсера)."""
    status = await message.answer("Пересобираю все сборки из данных Регарда…")
    try:
        res = await regard_publisher.refresh_existing()
    except Exception as e:
        logger.exception("Обновление сборок Регарда упало")
        await status.edit_text(f"⚠️ Не удалось: {e}")
        return
    lines = [
        f"Пересобрано: {len(res['refreshed'])} из {res['total']}",
        f"Фид и Google Sheets обновлены.",
    ]
    if res["failed"]:
        lines.append("Не получилось (Регард не отдал): " + ", ".join(res["failed"]))
    if res["feed_url"]:
        lines.append(f"\nФид перезалит: {res['feed_url']}")
    await status.edit_text("\n".join(lines))


async def _download(msg: Message) -> bytes:
    if msg.document:
        file = await bot.get_file(msg.document.file_id)
    else:
        photo = msg.photo[-1]
        file = await bot.get_file(photo.file_id)
    stream = await bot.download_file(file.file_path)
    return stream.read()


def _mime_of(msg: Message) -> str:
    if msg.document and msg.document.mime_type:
        return msg.document.mime_type
    return "image/jpeg"  # обычные Telegram-фото всегда пережимаются в JPEG


async def _add_photos_to_collection(anchor_message: Message, chat_id: int, messages: list[Message]):
    # Гасим старый таймер автозапуска сразу, до любых сетевых вызовов ниже —
    # иначе он может успеть сработать, пока мы редактируем сообщение о счётчике.
    _cancel_auto_start(chat_id)
    _cancel_photo_anim(chat_id)

    async with _lock_for(chat_id):
        session = COLLECTING.setdefault(chat_id, {"photos": [], "notes": [], "prompt_id": None})
        session["photos"].extend(messages)
        for m in messages:
            if m.caption:
                session["notes"].append(m.caption)

        count = len(session["photos"])
        text = f"Добавлено фото: {count}.\n{DOT_FRAMES[0]}"

        if session["prompt_id"]:
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=session["prompt_id"])
            except Exception:
                pass
        else:
            msg = await anchor_message.answer(text)
            session["prompt_id"] = msg.message_id

    PHOTO_ANIM_TASKS[chat_id] = asyncio.create_task(
        _animate_photo_prompt(chat_id, session["prompt_id"], count)
    )
    _schedule_auto_start(chat_id, anchor_message)


@dp.message(F.photo)
async def handle_photo(message: Message):
    if message.media_group_id:
        group_id = message.media_group_id
        MEDIA_GROUPS.setdefault(group_id, []).append(message)
        if group_id in MEDIA_GROUP_TASKS:
            MEDIA_GROUP_TASKS[group_id].cancel()
        MEDIA_GROUP_TASKS[group_id] = asyncio.create_task(_process_group_later(group_id, message.chat.id))
        return

    await _add_photos_to_collection(message, message.chat.id, [message])


def _is_image_document(message: Message) -> bool:
    return bool(message.document and (message.document.mime_type or "").startswith("image/"))


@dp.message(_is_image_document)
async def handle_image_document(message: Message):
    # Фото, присланное файлом (без сжатия Telegram) — оригинальное разрешение.
    if message.media_group_id:
        group_id = message.media_group_id
        MEDIA_GROUPS.setdefault(group_id, []).append(message)
        if group_id in MEDIA_GROUP_TASKS:
            MEDIA_GROUP_TASKS[group_id].cancel()
        MEDIA_GROUP_TASKS[group_id] = asyncio.create_task(_process_group_later(group_id, message.chat.id))
        return

    await _add_photos_to_collection(message, message.chat.id, [message])


async def _process_group_later(group_id: str, chat_id: int):
    try:
        await asyncio.sleep(MEDIA_GROUP_DELAY)
    except asyncio.CancelledError:
        return
    messages = MEDIA_GROUPS.pop(group_id, [])
    MEDIA_GROUP_TASKS.pop(group_id, None)
    if messages:
        await _add_photos_to_collection(messages[0], chat_id, messages)


def _is_collecting_text(message: Message) -> bool:
    return (
        message.text is not None
        and not message.text.startswith("/")
        and message.chat.id in COLLECTING
    )


@dp.message(_is_collecting_text)
async def handle_collect_text(message: Message):
    chat_id = message.chat.id
    _cancel_auto_start(chat_id)
    session = COLLECTING[chat_id]
    session["notes"].append(message.text)
    await message.answer(
        f"Добавил в описание. Фото сейчас: {len(session['photos'])}. "
        f"Присылай ещё, или подожди {AUTO_START_DELAY} сек — начну сам (либо /done)."
    )
    _schedule_auto_start(chat_id, message)


async def _finalize_collection(chat_id: int, message: Message):
    _cancel_auto_start(chat_id)
    _cancel_photo_anim(chat_id)
    if chat_id in PROCESSING:
        return
    async with _lock_for(chat_id):
        session = COLLECTING.pop(chat_id, None)
    if not session or not session["photos"]:
        await message.answer("Пока нет ни одного фото — пришли хотя бы одно.")
        return
    if session.get("prompt_id"):
        try:
            await bot.delete_message(chat_id, session["prompt_id"])
        except Exception:
            pass
    PROCESSING.add(chat_id)
    PENDING_MODE[chat_id] = {"photos": session["photos"], "notes": session["notes"], "message": message}
    await message.answer("Как обработать фото?", reply_markup=await _mode_keyboard())


@dp.callback_query(F.data.startswith("procmode:"))
async def handle_mode_choice(callback: CallbackQuery):
    chat_id = callback.message.chat.id
    pending = PENDING_MODE.pop(chat_id, None)
    if not pending:
        await callback.answer("Сессия устарела, пришли фото заново.", show_alert=True)
        return
    mode = callback.data.split(":", 1)[1]
    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    try:
        await _process_listing(
            pending["message"], pending["photos"], "\n".join(pending["notes"]), mode=mode,
        )
    finally:
        PROCESSING.discard(chat_id)


async def _send_photo_album(chat_id: int, items: list[tuple[bytes, str]]) -> None:
    """Присылает готовые фото объявления прямо в чат альбомом — чтобы сразу
    увидеть результат, не открывая ссылки из таблицы/фида."""
    if not items:
        return
    files = [
        BufferedInputFile(data, filename=f"photo_{i}.{yandex_storage._EXT_BY_MIME.get(mime, 'jpg')}")
        for i, (data, mime) in enumerate(items, start=1)
    ]
    try:
        if len(files) == 1:
            await bot.send_photo(chat_id, files[0])
            return
        for i in range(0, len(files), 10):
            await bot.send_media_group(chat_id, media=[InputMediaPhoto(media=f) for f in files[i:i + 10]])
    except Exception:
        logger.exception("Не удалось отправить альбом с фото пользователю")


async def _process_listing(anchor_message: Message, photo_messages: list[Message], user_note: str,
                           mode: str = "full"):
    # Лимит Free-тарифа: проверка ДО тяжёлой обработки (GLM/Bria/GPU).
    allowed, reason, _remain = users.check_allowance(anchor_message.chat.id)
    if not allowed:
        await anchor_message.answer(reason)
        return
    status = await anchor_message.answer("⏳ Обработка...\n   [ ] Скачиваю фото\n   [ ] Распознавание...\n   [ ] Убираю фон\n   [ ] Собираю карточку")

    def _format_recognized(p: dict) -> list[str]:
        """Список строк распознанных характеристик для этапа прогресс-бара."""
        bits = []
        brand_model = f"{p.get('brand','').strip()} {p.get('model','').strip()}".strip()
        if brand_model:
            bits.append(f"📷 {brand_model}")
        if p.get("cpu"):
            bits.append(f"💻 {p['cpu']}")
        mem = []
        if p.get("ram_gb"):
            mem.append(f"{p['ram_gb']} ГБ ОЗУ")
        if p.get("storage_gb"):
            from avito_row import round_storage
            mem.append(f"{round_storage(p['storage_gb'])} ГБ SSD")
        if mem:
            bits.append("🧠 " + " + ".join(mem))
        if p.get("screen_size"):
            screen = f'🖥️ {p["screen_size"]}"'
            if p.get("screen_resolution"):
                screen += f", {p['screen_resolution']}"
            bits.append(screen)
        return bits

    # Этапы прогресс-бара. done — сколько этапов已完成 (0..3),
    # current — actively в работе. recognized — список строк распознанного,
    # показывается как отдельный «этап» после скачивания.
    async def _progress(done: int, current: bool = False, extra: str = "",
                        recognized: list[str] | None = None) -> None:
        lines = ["⏳ Обработка..."]
        # Этап 0: Скачиваю фото
        mark = "✓" if done > 0 else ("⟳" if (current and done == 0) else " ")
        lines.append(f"   [{mark}] Скачиваю фото")
        # Этап 1: распознанные характеристики (многострочный)
        if recognized:
            for i, line in enumerate(recognized):
                m = "✓" if (done > 1 or (i == 0 and not current)) else ("⟳" if (current and done == 1) else " ")
                if i == 0:
                    lines.append(f"   [{m}] {line}")
                else:
                    lines.append(f"       {line}")
        else:
            mark = "✓" if done > 1 else ("⟳" if (current and done == 1) else " ")
            lines.append(f"   [{mark}] Распознавание...")
        # Этап 2: Убираю фон
        mark = "✓" if done > 2 else ("⟳" if (current and done == 2) else " ")
        bg_line = "Убираю фон"
        if extra and done == 2:
            bg_line += f" ({extra})"
        lines.append(f"   [{mark}] {bg_line}")
        # Этап 3: Собираю карточку
        mark = "✓" if done > 3 else ("⟳" if (current and done == 3) else " ")
        lines.append(f"   [{mark}] Собираю карточку")
        try:
            await status.edit_text("\n".join(lines))
        except Exception:
            pass  # слишком частое обновление игнорируем

    try:
        await _progress(0, current=True)
        images = [(await _download(m), _mime_of(m)) for m in photo_messages]
        await _progress(1, current=True)
        # /upgrade: параметры уже клонированы из старого объявления — GLM
        # не вызываем, все фото считаем фото устройства.
        pending_upgrade = UPGRADE_PENDING.get(anchor_message.chat.id)
        if pending_upgrade:
            result = {
                "parameters": pending_upgrade["params"],
                "image_types": ["photo"] * len(images),
                "cover_photo_index": 1,
                "needs_clarification": False,
            }
            UPGRADE_PENDING.pop(anchor_message.chat.id, None)
        else:
            result = await analyze_photo(images, user_note)
    except Exception as e:
        logger.exception("Photo analysis failed")
        await status.edit_text(
            "Не получилось обработать фото. Попробуй ещё раз или пришли "
            f"другое фото.\nТехническая причина: {e}"
        )
        return

    if result.get("needs_clarification"):
        questions = "\n".join(f"— {q}" for q in result.get("clarifying_questions", []))
        await status.edit_text(
            "Не смог определить бренд/модель. Пришли, пожалуйста, фото ещё раз "
            f"и/или текстом уточни:\n{questions}\n\nЗатем снова /done."
        )
        return

    recognized = _format_recognized(result.get("parameters", {}))
    await _progress(2, recognized=recognized)

    # Скриншоты (характеристики, диагностика, чужие объявления) уже дали свои
    # данные распознаванию выше — в само объявление идут только реальные фото
    # устройства. Если модель не вернула классификацию или она не сходится по
    # длине — подстраховка: считаем все фото годными, лучше лишнее фото, чем
    # пустое объявление.
    image_types = result.get("image_types", [])
    if len(image_types) == len(images):
        device_photos = [img for img, t in zip(images, image_types) if t != "screenshot"]
    else:
        device_photos = images
    if not device_photos:
        device_photos = images

    # ВСЕ фото устройства идут в объявление (как и было изначально) —
    # эксперименты с удалением «обрезанных» отключены: и GLM, и локальный
    # детектор ошибались и удаляли ценные кадры. Вырезаем каждое фото один
    # раз и переиспользуем cutout для галереи и карточки (без повторной
    # вырезки). Обрезанность только логируем, для статистики.
    # specs: вырезаем ТОЛЬКО обложку (для карточки), галерея — как есть.
    cutouts: dict[tuple[bytes, str], Optional[bytes]] = {}
    if mode == "full":
        await _progress(2, current=True, extra="подготовка фото", recognized=recognized)
        for photo in device_photos:
            cut = await card_service._remove_bg(photo[0], photo[1])
            cutouts[photo] = cut
            if cut and card_service.is_cropped_by_frame(cut):
                logger.info("Статистика: фото обрезано кадром (в объявлении остаётся)")
    elif mode == "specs":
        await _progress(2, current=True, extra="вырезка обложки", recognized=recognized)
        # Определяем обложку заранее (нужна только она).
        _cp = None
        _ci = result.get("cover_photo_index")
        if isinstance(_ci, int) and 1 <= _ci <= len(images):
            _cand = images[_ci - 1]
            if _cand in device_photos:
                _cp = _cand
        if _cp is None and device_photos:
            _cp = device_photos[0]
        if _cp:
            cut = await card_service._remove_bg(_cp[0], _cp[1])
            cutouts[_cp] = cut

    # Обложка — лучший презентационный кадр по выбору GLM (валиден, только
    # если это фото устройства, не скриншот); иначе первое фото устройства.
    cover_photo = None
    cover_idx = result.get("cover_photo_index")
    if isinstance(cover_idx, int) and 1 <= cover_idx <= len(images):
        candidate = images[cover_idx - 1]
        if candidate in device_photos:
            cover_photo = candidate
    if cover_photo is None and device_photos:
        cover_photo = device_photos[0]
    cover_cutout = cutouts.get(cover_photo) if cover_photo else None

    listing_id = make_listing_id()

    photo_urls: list[str] = []
    listing_photos: list[tuple[bytes, str]] = []
    try:
        await _progress(3, current=True, extra=f"{len(device_photos)} фото", recognized=recognized)
        if mode in ("full", "specs"):
            for photo in device_photos:
                cut = cutouts.get(photo)
                composed = await card_service.compose_listing_from_cutout(cut) if cut else None
                if composed is not None:
                    listing_photos.append((composed, "image/jpeg"))
                else:
                    listing_photos.append(photo)
        else:
            listing_photos = device_photos
        photo_urls = await yandex_storage.upload_photos(listing_photos, listing_id)
    except Exception as e:
        logger.exception("Photo upload to Yandex Storage failed")
        await anchor_message.answer(f"⚠️ Не удалось загрузить фото: {e}")

    # То же, что уходит в photo_urls, но байтами — чтобы сразу показать всё
    # пользователю альбомом в чате, не заставляя его открывать ссылки.
    media_items: list[tuple[bytes, str]] = list(listing_photos)

    # Карточка-обложка (только в режиме "Удаление фона + инфа"): убираем фон с
    # выбранного фото (cover_photo), рисуем карточку и ставим её главным
    # (первым) фото объявления. Если не вышло — идём с обычными фото.
    card_bytes = None
    if mode in ("full", "specs"):
        try:
            if cover_photo:
                await _progress(4, current=True, recognized=recognized)
                card_bytes = await card_service.build_card(
                    result.get("parameters", {}), cover_photo[0], cover_photo[1],
                    cutout_bytes=cover_cutout,
                )
                if card_bytes:
                    card_url = await yandex_storage.upload_photo(
                        card_bytes, f"{listing_id}_card.png", "image/png"
                    )
                    photo_urls = [card_url] + photo_urls
                    media_items = [(card_bytes, "image/png")] + media_items
        except Exception:
            logger.exception("Card generation step failed")

    # Финальный прогресс — всё готово.
    await _progress(5, recognized=recognized)

    await _send_photo_album(anchor_message.chat.id, media_items)

    p = result.get("parameters", {})
    detected = questionnaire._detect_answers(result)
    detected_bits = []
    if detected.get(questionnaire._DRIVE_SIZE_FIELD["avito_column"]):
        detected_bits.append(f"{detected[questionnaire._DRIVE_SIZE_FIELD['avito_column']]} ГБ")
    if detected.get(questionnaire._RAM_SIZE_FIELD["avito_column"]):
        detected_bits.append(f"{detected[questionnaire._RAM_SIZE_FIELD['avito_column']]} ГБ ОЗУ")
    if detected.get(questionnaire._OS_FIELD["avito_column"]):
        detected_bits.append(detected[questionnaire._OS_FIELD["avito_column"]])
    if detected.get(questionnaire._COLOR_FIELD["avito_column"]):
        detected_bits.append(detected[questionnaire._COLOR_FIELD["avito_column"]])

    # Префиксы «сенсорный»/«трансформер» — в заголовок Авито (= текст «Вижу:»).
    prefix_bits = []
    if p.get("touchscreen", "").strip().lower() == "да":
        prefix_bits.append("Сенсорный")
    if p.get("transformer", "").strip().lower() == "да":
        prefix_bits.append("трансформер")
    prefix = " ".join(prefix_bits)
    seen = ", ".join(
        x for x in [p.get("brand"), p.get("model"), p.get("cpu"), p.get("gpu"), *detected_bits] if x
    )
    if prefix:
        seen = f"{prefix} {seen}".strip()
    await status.edit_text(f"Вижу: {seen or 'ноутбук'}.")

    # Заголовок объявления на Avito = текст из «Вижу:». Пробрасываем через vision.
    result.setdefault("parameters", {})["title_seen"] = seen

    await questionnaire.start_questionnaire(status, result, listing_id, photo_urls, card_bytes)


@dp.message(F.func(lambda m: (
    m.chat.id not in questionnaire.PRICE_PENDING
    and m.chat.id not in questionnaire.ADDRESS_PENDING
    and m.chat.id not in COLLECTING
)))
async def fallback(message: Message):
    await message.answer(
        "Пришли, пожалуйста, фото товара — я работаю только с фотографиями "
        "(текст можно дописать после первого фото)."
    )


# Как часто фоном сверять фид с реальными статусами на Avito и убирать
# проданные/снятые (по умолчанию раз в 30 минут). 0 — выключить фон, оставив
# только ручную команду /sync.
SYNC_INTERVAL = int(os.environ.get("AVITO_SYNC_INTERVAL", "1800"))

# Сколько новых сборок Регарда публиковать в день (фоновая задача ниже).
REGARD_DAILY_LIMIT = int(os.environ.get("REGARD_DAILY_LIMIT", "10"))
# Первый запуск дневной задачи — вскоре после старта бота (секунды), далее раз в сутки.
REGARD_FIRST_RUN_DELAY = int(os.environ.get("REGARD_FIRST_RUN_DELAY", "60"))
REGARD_RUN_INTERVAL = 24 * 3600


async def _sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            res = await sync_removed_from_avito()
            if res["removed"]:
                logger.info("Автосинхрон: убрано из фида %s", res["removed"])
        except Exception:
            logger.exception("Автосинхрон с Avito упал — попробую в следующий раз")
        try:
            removed = await avito_sync.sync_pc_removed_from_avito()
            if removed:
                logger.info("Автосинхрон ПК-фида: убрано %s", removed)
        except Exception:
            logger.exception("Автосинхрон ПК-фида упал — попробую в следующий раз")


async def _regard_loop():
    """Раз в сутки добавляет топовые сборки Регарда в фид системных блоков.
    REGARD_DAILY_LIMIT=0 полностью отключает цикл."""
    if REGARD_DAILY_LIMIT <= 0:
        logger.info("Дневная публикация Регарда ОТКЛЮЧЕНА (REGARD_DAILY_LIMIT=0)")
        return
    await asyncio.sleep(REGARD_FIRST_RUN_DELAY)
    while True:
        try:
            res = await regard_publisher.publish_new(REGARD_DAILY_LIMIT)
            logger.info(
                "Регард: добавлено %d, обновлено %d, отсеяно %d",
                len(res["added"]), len(res["updated"]), len(res["rejected"]),
            )
        except Exception:
            logger.exception("Дневная публикация сборок Регарда упала — попробую в следующий раз")
        await asyncio.sleep(REGARD_RUN_INTERVAL)


async def main():
    users.init_db()  # SQLite кабинет пользователей
    try:
        restored = await download_feed(EXPORT_PATH)
        logger.info(
            "avito_export.xlsx восстановлен из Yandex Storage" if restored
            else "В Yandex Storage ещё нет фида — начнём с шаблона при первой позиции"
        )
    except Exception:
        logger.exception(
            "Не удалось подтянуть avito_export.xlsx из Yandex Storage — "
            "продолжаю с тем, что есть локально"
        )

    if REGARD_DAILY_LIMIT > 0:
        try:
            restored_pc = await download_feed(pc_feed.EXPORT_PATH, yandex_storage.PC_OBJECT_KEY)
            logger.info(
                "avito_export_pc.xlsx восстановлен из Yandex Storage" if restored_pc
                else "ПК-фида в Yandex Storage ещё нет — начнём с шаблона при первой сборке"
            )
        except Exception:
            logger.exception(
                "Не удалось подтянуть avito_export_pc.xlsx из Yandex Storage — "
                "продолжаю с тем, что есть локально"
            )

    if avito_api.is_configured() and SYNC_INTERVAL > 0:
        asyncio.create_task(_sync_loop())
        logger.info("Автосинхрон статусов Avito включён: каждые %d сек", SYNC_INTERVAL)
    else:
        logger.info(
            "Автосинхрон статусов Avito выключен (нет AVITO_CLIENT_ID/SECRET "
            "или AVITO_SYNC_INTERVAL=0) — доступна ручная команда /sync"
        )

    asyncio.create_task(_regard_loop())
    if pc_sheets.is_configured():
        # Таблицу могли настроить уже после первых публикаций — догоняем её фидом.
        asyncio.create_task(pc_sheets.sync_all_from_feed())
    else:
        logger.info(
            "GOOGLE_SHEET_ID_PC не задан — сборки Регарда пишутся только в фид, "
            "без зеркала в Google Sheets"
        )

    await dp.start_polling(bot)


LOCK_PATH = os.path.join(os.path.dirname(__file__), ".bot.lock")


def _acquire_single_instance_lock():
    """Не даёт запустить второй экземпляр бота — два процесса конкурируют за
    Telegram getUpdates и путают сессии друг друга (уже наступали на эти грабли)."""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Бот уже запущен в другом процессе — выхожу, чтобы не конфликтовать за Telegram polling.")
        sys.exit(1)
    return lock_file


if __name__ == "__main__":
    _lock_handle = _acquire_single_instance_lock()
    asyncio.run(main())
