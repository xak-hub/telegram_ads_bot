"""users.py — кабинет пользователей: SQLite, тарифы, лимиты, рефералы.

Хранилище: ~/telegram_ads_bot/users.db (SQLite, WAL). Анонимно: только
chat_id + username из Telegram, никаких телефонов.

Тарифы (лимит объявлений/мес):
  free      0 ₽       5
  start   290 ₽/мес  30     (год: 2610 ₽, −25%)
  pro     690 ₽/мес 100     (год: 6210 ₽, −25%)
  business 1990 ₽/мес 500   (год: 5965 ₽, −25%)

Счётчик объявлений автоматически сбрасывается 1-го числа каждого месяца.
Реферальная программа: +5 объявлений пригласившему за каждого друга,
который опубликовал первое объявление.
"""

import datetime
import os
import sqlite3
import threading
from typing import Optional

BASE_DIR = os.path.dirname(__file__)
DB_PATH = os.path.join(BASE_DIR, "users.db")

# Лимиты объявлений в месяц по тарифам.
LIMITS = {
    "free": 5,
    "start": 30,
    "pro": 100,
    "business": 500,
}

# Годовые тарифы: цена за 12 мес со скидкой 25%.
YEARLY_PRICES = {
    "start": 2610,     # 290×12×0.75
    "pro": 6210,       # 690×12×0.75
    "business": 5965,  # 1990×12×0.75
}

BONUS_PER_REFERRAL = 5  # +5 объявлений за друга (единоразово на счётчик лимита)

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None


def _month_key() -> str:
    return datetime.date.today().strftime("%Y-%m")


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
    return _conn


def init_db() -> None:
    """Создаёт таблицы. Вызывается при старте бота."""
    with _lock:
        conn = _connect()
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id       INTEGER PRIMARY KEY,
            username      TEXT,
            registered_at TEXT,
            tariff        TEXT DEFAULT 'free',
            tariff_until  TEXT,                -- ISO дата окончания платного тарифа
            listings_count INTEGER DEFAULT 0,   -- объявления за текущий месяц
            month         TEXT,                -- YYYY-MM
            bonus_listings INTEGER DEFAULT 0,  -- реферальные/промо бонусы (сгорают в конце месяца)
            invited_by    INTEGER              -- chat_id пригласившего
        );
        CREATE TABLE IF NOT EXISTS referrals (
            invited    INTEGER PRIMARY KEY,    -- chat_id приглашённого
            inviter    INTEGER,                -- chat_id пригласившего
            created_at TEXT,
            rewarded   INTEGER DEFAULT 0       -- 1 = бонус уже начислен (после 1-го объявления)
        );
        """)
        conn.commit()


def register_if_new(chat_id: int, username: str = "") -> bool:
    """Создаёт пользователя при первом визите. Возвращает True, если новый."""
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT 1 FROM users WHERE chat_id=?", (chat_id,))
        if cur.fetchone():
            return False
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO users (chat_id, username, registered_at, tariff, listings_count, month) "
            "VALUES (?,?,?,'free',0,?)",
            (chat_id, username or "", now, _month_key()),
        )
        conn.commit()
        return True


def get_user(chat_id: int) -> Optional[sqlite3.Row]:
    with _lock:
        return _connect().execute(
            "SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()


def _ensure_month(user: sqlite3.Row) -> None:
    """Сброс счётчика при смене месяца (1-е число)."""
    mk = _month_key()
    if user["month"] != mk:
        _connect().execute(
            "UPDATE users SET listings_count=0, month=?, bonus_listings=0 WHERE chat_id=?",
            (mk, user["chat_id"]))
        _connect().commit()


def set_tariff(chat_id: int, tariff: str, months: int = 1) -> None:
    """Выдаёт/меняет тариф. Платный тариф действует months месяцев (tariff_until)."""
    until = None
    if tariff != "free":
        until = (datetime.date.today() + datetime.timedelta(days=30 * months)).isoformat()
    with _lock:
        _connect().execute(
            "UPDATE users SET tariff=?, tariff_until=? WHERE chat_id=?",
            (tariff, until, chat_id))
        _connect().commit()


def _effective_tariff(user: sqlite3.Row) -> str:
    """free, если платный тариф истёк."""
    t = user["tariff"]
    if t != "free" and user["tariff_until"]:
        try:
            if datetime.date.fromisoformat(user["tariff_until"]) < datetime.date.today():
                return "free"
        except ValueError:
            pass
    return t


def check_allowance(chat_id: int) -> tuple[bool, str, int]:
    """Проверяет лимит БЕЗ списания: (можно_ли, сообщение, осталось)."""
    user = get_user(chat_id)
    if not user:
        return True, "", LIMITS["free"]
    _ensure_month(user)
    user = get_user(chat_id)
    tariff = _effective_tariff(user)
    if tariff == "free":
        limit = LIMITS["free"]
        used = user["listings_count"]
        remain = max(0, limit - used)
        if remain <= 0:
            return False, (
                f"🏁 Лимит Free исчерпан ({used}/{limit} в этом месяце).\n\n"
                "Тарифы: /tariff — Start/Pro/Business без лимитов Free.\n"
                "Лимит обнулится 1-го числа."
            ), 0
        return True, "", remain
    # платные: без жёсткого блока, но показываем остаток
    limit = LIMITS.get(tariff, 0)
    used = user["listings_count"]
    remain = max(0, limit + user["bonus_listings"] - used)
    return True, "", remain


def consume_listing(chat_id: int) -> None:
    """Списание одного объявления (после успешной публикации)."""
    user = get_user(chat_id)
    if not user:
        return
    _ensure_month(user)
    with _lock:
        _connect().execute(
            "UPDATE users SET listings_count=listings_count+1 WHERE chat_id=?",
            (chat_id,))
        _connect().commit()


def grant_bonus(chat_id: int, amount: int = BONUS_PER_REFERRAL) -> None:
    """Начисляет бонусные объявления (рефералы/промо) — к текущему месяцу."""
    _ensure_month(get_user(chat_id))
    with _lock:
        _connect().execute(
            "UPDATE users SET bonus_listings=bonus_listings+? WHERE chat_id=?",
            (amount, chat_id))
        _connect().commit()


def link_referral(invited_chat_id: int, inviter_chat_id: int) -> None:
    """Фиксирует приглашение (однократно). Бонус начисляется пригласившему
    после первого объявления приглашённого — см. maybe_reward_referral."""
    with _lock:
        conn = _connect()
        cur = conn.execute("SELECT 1 FROM referrals WHERE invited=?", (invited_chat_id,))
        if cur.fetchone():
            return
        now = datetime.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO referrals (invited, inviter, created_at) VALUES (?,?,?)",
            (invited_chat_id, inviter_chat_id, now))
        conn.commit()


def maybe_reward_referral(invited_chat_id: int) -> Optional[int]:
    """Если приглашённый опубликовал первое объявление — начисляет пригласившему
    +5 объявлений. Возвращает chat_id пригласившего (или None)."""
    with _lock:
        conn = _connect()
        row = conn.execute(
            "SELECT inviter, rewarded FROM referrals WHERE invited=?",
            (invited_chat_id,)).fetchone()
        if not row or row["rewarded"]:
            return None
        conn.execute("UPDATE referrals SET rewarded=1 WHERE invited=?", (invited_chat_id,))
        inviter = row["inviter"]
    grant_bonus(inviter, BONUS_PER_REFERRAL)
    return inviter


def set_username(chat_id: int, username: str) -> None:
    with _lock:
        _connect().execute(
            "UPDATE users SET username=? WHERE chat_id=?", (username or "", chat_id))
        _connect().commit()
