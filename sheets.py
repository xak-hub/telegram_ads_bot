import asyncio
import os
from typing import Optional

import gspread
from dotenv import load_dotenv

from avito_row import ALL_COLUMNS, build_values
from retry import retry_async

load_dotenv()

SPREADSHEET_ID = os.environ["GOOGLE_SHEET_ID"]
CREDENTIALS_FILE = os.environ.get("GOOGLE_CREDENTIALS_FILE", "service_account.json")

_gc = gspread.service_account(filename=CREDENTIALS_FILE)
_sheet = _gc.open_by_key(SPREADSHEET_ID).sheet1


def _is_transient(e: Exception) -> bool:
    if isinstance(e, gspread.exceptions.APIError):
        response = getattr(e, "response", None)
        status = response.status_code if response is not None else None
        return status is None or status >= 500 or status == 429
    return isinstance(e, (ConnectionError, TimeoutError))


def _ensure_header_sync() -> None:
    """Заголовок всегда приводится к текущей схеме Avito (65 колонок), даже если
    в листе уже что-то было записано старым форматом — перезаписываем первую строку."""
    _sheet.update("A1", [ALL_COLUMNS])


def _append_row_sync(row: list) -> None:
    _sheet.append_row(row, value_input_option="USER_ENTERED")


async def ensure_header() -> None:
    await retry_async(lambda: asyncio.to_thread(_ensure_header_sync), is_transient=_is_transient)


async def append_row(
    vision_params: dict, avito_answers: dict, price: str, address: str, listing_id: str,
    photo_urls: Optional[list[str]] = None,
) -> None:
    await ensure_header()
    values = build_values(vision_params, avito_answers, price, address, listing_id, photo_urls)
    row = [values.get(col, "") for col in ALL_COLUMNS]
    await retry_async(lambda: asyncio.to_thread(_append_row_sync, row), is_transient=_is_transient)
