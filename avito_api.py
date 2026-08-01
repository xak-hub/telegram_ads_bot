"""avito_api.py — тонкий клиент Avito API для синхронизации статусов объявлений.

Нужен ровно для одной задачи: узнать, какие из объявлений в нашем фиде уже
сняты/проданы на самом Avito, чтобы убрать их из avito_export.xlsx и автозагрузка
не поднимала их заново.

Связка двухшаговая, потому что фид оперирует НАШИМ идентификатором объявления
(«Уникальный идентификатор объявления»), а живой статус лежит под внутренним id
Avito:
  1. autoload API  ad_id  -> avito_id
  2. core API      avito_id -> живой статус ("active"/"removed"/"old"/...)

Если AVITO_CLIENT_ID/SECRET не заданы — клиент считается ненастроенным и все
операции превращаются в no-op, бот продолжает работать как раньше.
"""

import logging
import os
import time

from typing import Optional

import aiohttp
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

BASE_URL = "https://api.avito.ru"

CLIENT_ID = os.environ.get("AVITO_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AVITO_CLIENT_SECRET", "")
# user_id можно не задавать — подтянем сами через /core/v1/accounts/self.
_USER_ID_ENV = os.environ.get("AVITO_USER_ID", "")

# Живые статусы, при которых объявление считается снятым/проданным/архивным и
# должно быть убрано из фида. Активные, а также заблокированные/отклонённые
# модерацией (это не намерение продавца, а временная проблема) — оставляем.
REMOVED_STATUSES = {"removed", "old", "closed", "archived"}

_TIMEOUT = aiohttp.ClientTimeout(total=30)

# Кэш токена: {"value": str, "exp": unix_ts}. Токен живёт ~сутки, но перевыпуск
# дешёвый — обновляем заранее, за минуту до истечения.
_token_cache = {"value": "", "exp": 0.0}


def is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


async def _get_token(session: aiohttp.ClientSession) -> str:
    now = time.time()
    if _token_cache["value"] and now < _token_cache["exp"] - 60:
        return _token_cache["value"]
    async with session.post(
        f"{BASE_URL}/token",
        data={
            "grant_type": "client_credentials",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
        },
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    _token_cache["value"] = data["access_token"]
    _token_cache["exp"] = now + float(data.get("expires_in", 3600))
    return _token_cache["value"]


async def _get_user_id(session: aiohttp.ClientSession, token: str) -> str:
    if _USER_ID_ENV:
        return _USER_ID_ENV
    async with session.get(
        f"{BASE_URL}/core/v1/accounts/self",
        headers={"Authorization": f"Bearer {token}"},
    ) as resp:
        resp.raise_for_status()
        data = await resp.json()
    return str(data["id"])


async def _avito_id_for(session: aiohttp.ClientSession, token: str, user_id: str,
                        ad_id: str) -> Optional[int]:
    """Наш ad_id из фида -> внутренний id объявления на Avito. None, если
    автозагрузка ещё не обработала это объявление (тогда трогать его нельзя)."""
    url = f"{BASE_URL}/autoload/v1/accounts/{user_id}/items/{ad_id}/"
    async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        data = await resp.json()
    avito_id = data.get("avito_id")
    return int(avito_id) if avito_id else None


async def _live_status(session: aiohttp.ClientSession, token: str, user_id: str,
                       avito_id: int) -> Optional[str]:
    """Живой статус объявления на сайте по внутреннему id Avito."""
    url = f"{BASE_URL}/core/v1/accounts/{user_id}/items/{avito_id}/"
    async with session.get(url, headers={"Authorization": f"Bearer {token}"}) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        data = await resp.json()
    return data.get("status")


async def find_removed_ad_ids(ad_ids: list[str]) -> list[str]:
    """Из переданных ad_id возвращает те, что на Avito уже сняты/проданы/в архиве.

    Консервативно: если статус определить не удалось (объявление ещё не
    обработано автозагрузкой, сетевая ошибка и т.п.) — объявление НЕ трогаем,
    чтобы случайно не удалить активное из фида."""
    if not is_configured() or not ad_ids:
        return []
    removed: list[str] = []
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
        token = await _get_token(session)
        user_id = await _get_user_id(session, token)
        for ad_id in ad_ids:
            try:
                avito_id = await _avito_id_for(session, token, user_id, ad_id)
                if avito_id is None:
                    continue
                status = await _live_status(session, token, user_id, avito_id)
                logger.info("Avito статус ad_id=%s avito_id=%s: %s", ad_id, avito_id, status)
                if status in REMOVED_STATUSES:
                    removed.append(ad_id)
            except Exception:
                logger.exception("Не удалось проверить статус объявления %s", ad_id)
    return removed
