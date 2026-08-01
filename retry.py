import asyncio
import logging
from typing import Awaitable, Callable, TypeVar, Optional

logger = logging.getLogger(__name__)

T = TypeVar("T")


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    *,
    retries: int = 3,
    delay: float = 1.5,
    is_transient: Callable[[Exception], bool] = lambda e: True,
) -> T:
    """Повторяет асинхронный вызов при временных ошибках (сеть, 5xx, рейт-лимиты).

    Не временные ошибки (по is_transient) пробрасываются сразу, без повторов —
    нет смысла повторять запрос с неверными правами доступа или битыми данными.
    """
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            return await fn()
        except Exception as e:
            if not is_transient(e):
                raise
            last_error = e
            if attempt < retries:
                logger.warning("Временная ошибка (попытка %s/%s): %s", attempt, retries, e)
                await asyncio.sleep(delay * attempt)
    raise last_error
