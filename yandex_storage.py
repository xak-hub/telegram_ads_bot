import asyncio
import logging
import os

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError
from dotenv import load_dotenv

from retry import retry_async

load_dotenv()

logger = logging.getLogger(__name__)

BUCKET = os.environ["YC_S3_BUCKET"]
OBJECT_KEY = "avito_export.xlsx"
ENDPOINT_URL = "https://storage.yandexcloud.net"
XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PHOTOS_PREFIX = "photos"
_EXT_BY_MIME = {"image/jpeg": "jpg", "image/png": "png"}

_client = boto3.client(
    "s3",
    endpoint_url=ENDPOINT_URL,
    region_name="ru-central1",
    aws_access_key_id=os.environ["YC_S3_ACCESS_KEY_ID"],
    aws_secret_access_key=os.environ["YC_S3_SECRET_ACCESS_KEY"],
)


def _is_transient(e: Exception) -> bool:
    if isinstance(e, EndpointConnectionError):
        return True
    if isinstance(e, ClientError):
        status = e.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return status is None or status >= 500 or status == 429
    return False


def _upload_sync(file_path: str) -> None:
    _client.upload_file(
        file_path, BUCKET, OBJECT_KEY,
        ExtraArgs={"ContentType": XLSX_CONTENT_TYPE, "ACL": "public-read"},
    )


async def upload_feed(file_path: str) -> str:
    """Заливает актуальный avito_export.xlsx в публичный бакет и возвращает
    постоянную ссылку для автозагрузки Avito."""
    await retry_async(lambda: asyncio.to_thread(_upload_sync, file_path), is_transient=_is_transient)
    return f"{ENDPOINT_URL}/{BUCKET}/{OBJECT_KEY}"


def _upload_bytes_sync(data: bytes, key: str, content_type: str) -> None:
    _client.put_object(
        Bucket=BUCKET, Key=key, Body=data, ContentType=content_type, ACL="public-read",
    )


async def upload_photo(image_bytes: bytes, filename: str, mime_type: str) -> str:
    """Заливает одно фото в публичный бакет и возвращает прямую ссылку на файл —
    именно такую понимает Avito в поле «Ссылки на фото» (в отличие от Яндекс.Диска,
    чья публичная ссылка ведёт на HTML-страницу предпросмотра, а не на сам файл)."""
    key = f"{PHOTOS_PREFIX}/{filename}"
    await retry_async(
        lambda: asyncio.to_thread(_upload_bytes_sync, image_bytes, key, mime_type),
        is_transient=_is_transient,
    )
    return f"{ENDPOINT_URL}/{BUCKET}/{key}"


async def upload_photos(images: list[tuple[bytes, str]], listing_id: str) -> list[str]:
    urls = []
    for i, (img, mime_type) in enumerate(images, start=1):
        ext = _EXT_BY_MIME.get(mime_type, "jpg")
        try:
            url = await upload_photo(img, f"{listing_id}_{i}.{ext}", mime_type)
        except Exception:
            logger.exception("Не удалось загрузить фото %s в Yandex Storage", i)
            continue
        urls.append(url)
    return urls


def _download_sync(file_path: str) -> None:
    _client.download_file(BUCKET, OBJECT_KEY, file_path)


async def download_feed(file_path: str) -> bool:
    """Подтягивает последнюю версию фида из бакета в file_path — так при старте
    на новой машине/после передеплоя не теряется история уже опубликованных
    объявлений. Возвращает False, если в бакете ещё ничего нет (например,
    самый первый запуск) — тогда вызывающий код сам создаст файл из шаблона."""
    try:
        await retry_async(
            lambda: asyncio.to_thread(_download_sync, file_path), is_transient=_is_transient
        )
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return False
        raise
