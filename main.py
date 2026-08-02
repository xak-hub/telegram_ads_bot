import asyncio
import fcntl
import logging
import os
import sys

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    InputMediaPhoto, Message,
)
from dotenv import load_dotenv

load_dotenv()

import questionnaire
import yandex_storage
import card_service
import avito_api
from claude_vision import analyze_photo
from avito_row import make_listing_id
from avito_export import EXPORT_PATH
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


def _mode_keyboard() -> InlineKeyboardMarkup:
    # Каждая кнопка на своей строке — крупнее и заметнее, чем в один ряд.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Без удаления фона", callback_data="procmode:raw")],
        [InlineKeyboardButton(text="Удаление фона + добавление инфы", callback_data="procmode:full")],
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
    # Сбрасываем зависшие состояния предпоказа/анкеты, чтобы /start начинал чисто.
    questionnaire.PREVIEW_PENDING.pop(message.chat.id, None)
    questionnaire.SESSIONS.pop(message.chat.id, None)
    await message.answer(
        "Пришли фото товара — можно одно, можно несколько по очереди или пачкой. "
        "В любой момент можно дописать текстом детали, которых не видно на фото "
        "(бренд, модель, доп. характеристики) — просто напиши сообщением.\n\n"
        "Совет по качеству: обычное фото в Telegram сжимается. Если нужно "
        "оригинальное разрешение (для чёткости на Avito) — прикрепляй фото как "
        "«Файл»/«Без сжатия» (в Telegram при выборе фото есть такая опция), а не "
        "как обычное сжатое фото.\n\n"
        f"Когда всё прислал(а) — просто подожди {AUTO_START_DELAY} секунд, я начну "
        "анализировать сам. Не хочешь ждать — пришли команду /done.\n\n"
        "Команда /defaults — настроить значения по умолчанию (в чате, кнопками).\n\n"
        "Дальше я распознаю характеристики, пришлю кнопки для заполнения оставшихся "
        "параметров и цены прямо в чате, а фото сам залью на Яндекс.Диск "
        "и запишу всё в Google Sheets и файл для Avito."
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
            "в .env, тогда бот сможет сам убирать проданные объявления из фида."
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
    await message.answer("Как обработать фото?", reply_markup=_mode_keyboard())


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
    status = await anchor_message.answer("⏳ Обработка...\n   [ ] Скачиваю фото\n   [ ] Распознаю характеристики\n   [ ] Классифицирую фото\n   [ ] Убираю фон\n   [ ] Собираю карточку")

    # Этапы прогресс-бара: mark_done — сколько этапов已完成, current — текст активного.
    async def _progress(done: int, current: str | None = None, extra: str = "",
                        details: str = "") -> None:
        stages = ["Скачиваю фото", "Распознаю характеристики", "Классифицирую фото", "Убираю фон", "Собираю карточку"]
        lines = ["⏳ Обработка..."]
        for i, label in enumerate(stages):
            mark = "✓" if i < done else ("⟳" if (current and i == done) else " ")
            line = f"   [{mark}] {label}"
            if i == done and current == label and extra:
                line += f" {extra}"
            lines.append(line)
        if details:
            lines.append("")
            lines.append(details)
        try:
            await status.edit_text("\n".join(lines))
        except Exception:
            pass  # слишком частое обновление игнорируем

    def _format_recognized(p: dict) -> str:
        """Сводка распознанных характеристик для показа под прогресс-баром."""
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
        if not bits:
            return ""
        return "🔍 Распознал:\n   " + "\n   ".join(bits)

    try:
        await _progress(0, "Скачиваю фото")
        images = [(await _download(m), _mime_of(m)) for m in photo_messages]
        await _progress(1, "Распознаю характеристики")
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
    await _progress(2, "Классифицирую фото", details=recognized)

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

    # Фото для обложки — то, что Claude Vision счёл лучшим презентационным
    # кадром (открыт, стоит прямо/чуть боком, экран виден). Если индекс не
    # пришёл, невалиден или указывает на отфильтрованный скриншот — берём
    # первое доступное фото устройства, как раньше.
    cover_photo = None
    cover_idx = result.get("cover_photo_index")
    if isinstance(cover_idx, int) and 1 <= cover_idx <= len(images):
        candidate = images[cover_idx - 1]
        if candidate in device_photos:
            cover_photo = candidate
    if cover_photo is None and device_photos:
        cover_photo = device_photos[0]

    listing_id = make_listing_id()

    photo_urls: list[str] = []
    listing_photos: list[tuple[bytes, str]] = []
    try:
        await _progress(3, "Убираю фон", f"({len(device_photos)} фото)", details=recognized)
        listing_photos = await card_service.remove_backgrounds(device_photos) if mode == "full" else device_photos
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
    if mode == "full":
        try:
            if cover_photo:
                await _progress(4, "Собираю карточку", details=recognized)
                card_bytes = await card_service.build_card(
                    result.get("parameters", {}), cover_photo[0], cover_photo[1]
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
    await _progress(5, details=recognized)

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

    seen = ", ".join(
        x for x in [p.get("brand"), p.get("model"), p.get("cpu"), p.get("gpu"), *detected_bits] if x
    )
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


async def _sync_loop():
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            res = await sync_removed_from_avito()
            if res["removed"]:
                logger.info("Автосинхрон: убрано из фида %s", res["removed"])
        except Exception:
            logger.exception("Автосинхрон с Avito упал — попробую в следующий раз")


async def main():
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

    if avito_api.is_configured() and SYNC_INTERVAL > 0:
        asyncio.create_task(_sync_loop())
        logger.info("Автосинхрон статусов Avito включён: каждые %d сек", SYNC_INTERVAL)
    else:
        logger.info(
            "Автосинхрон статусов Avito выключен (нет AVITO_CLIENT_ID/SECRET "
            "или AVITO_SYNC_INTERVAL=0) — доступна ручная команда /sync"
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
