"""avito_sync.py — синхронизация фида с реальными статусами на Avito.

Убирает из avito_export.xlsx объявления, которые на Avito уже сняты/проданы, и
перезаливает фид. Нужно, потому что автозагрузка Avito считает фид «списком того,
что должно висеть»: пока ID объявления есть в файле, автозагрузка поднимает его
заново — даже если продавец снял его вручную. Отсюда «воскрешение» проданных.
"""

import logging

from avito_api import find_removed_ad_ids, is_configured
from avito_export import EXPORT_PATH, list_listing_ids, remove_listings
from yandex_storage import upload_feed

logger = logging.getLogger(__name__)


async def sync_removed_from_avito(dry_run: bool = False) -> dict:
    """Проверяет статусы объявлений из фида на Avito и убирает снятые/проданные.

    dry_run=True — только смотрит и сообщает, что убрал бы, но фид не меняет.
    Возвращает {'configured', 'checked', 'removed', 'dry_run'}."""
    if not is_configured():
        return {"configured": False, "checked": 0, "removed": [], "dry_run": dry_run}

    ad_ids = list_listing_ids()
    removed = await find_removed_ad_ids(ad_ids)

    if removed and not dry_run:
        remove_listings(removed)
        await upload_feed(EXPORT_PATH)
        logger.info("Синхрон: убрано из фида %d объявлений: %s", len(removed), removed)

    return {
        "configured": True,
        "checked": len(ad_ids),
        "removed": removed,
        "dry_run": dry_run,
    }
