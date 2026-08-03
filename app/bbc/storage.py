"""Хранилище приложенных к касаниям файлов.

Два бэкенда за одним интерфейсом:

* **s3** — Cloudflare R2 (или любой S3-совместимый). Байты в бакете, в базе
  только ключ.
* **postgres** — байты прямо в `bbc.touch_files.data`. Включается сам, пока
  ключи R2 не заданы, чтобы фичу можно было выкатить до того, как заведён
  бакет.

Файловая система контейнера не используется ни в одном из вариантов: на Railway
она стирается при каждом деплое, и приложенный к долгу скрин исчезал бы вместе
с ней — молча, через недели после того, как его приложили.

Отдаются файлы всегда своим эндпоинтом с проверкой прав, а не публичной ссылкой
и не presigned-URL: скрин переписки о долге не должен открываться по угаданному
или пересланному адресу.
"""
from __future__ import annotations

import logging
import mimetypes
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache

from app.bbc.config import bbc_settings

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES_PER_TOUCH = 5

# Что принимаем. Ключ — то, что реально приходит из WhatsApp и почты: скрин
# переписки, скан акта сверки, гарантийное письмо.
ALLOWED_TYPES: dict[str, str] = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}

# Сигнатуры содержимого. Расширению и заголовку Content-Type верить нельзя: то и
# другое пишет клиент. docx и любой другой OOXML — это zip, поэтому проверка у
# них общая, до «PK».
_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
    (b"PK\x03\x04", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
)


class StorageError(Exception):
    """Отказ хранилища с сообщением, которое можно показать человеку."""


@dataclass(frozen=True)
class StoredFile:
    backend: str
    key: str | None
    data: bytes | None
    content_type: str
    size_bytes: int


def sniff_content_type(blob: bytes, declared: str | None) -> str:
    """Определить тип по содержимому. Заявленный тип — только подсказка.

    webp отдельным случаем: у него сигнатура составная (RIFF….WEBP), в общий
    список префиксов она не укладывается.
    """
    head = blob[:16]
    if head[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "image/webp"
    for prefix, mime in _MAGIC:
        if head.startswith(prefix):
            return mime
    raise StorageError(
        "Такой файл приложить нельзя. Подойдут скрин (JPG, PNG, WebP), PDF или DOCX."
    )


def validate(blob: bytes, filename: str, declared_type: str | None) -> tuple[str, str]:
    """Проверить файл. Возвращает (content_type, безопасное имя)."""
    if not blob:
        raise StorageError("Файл пустой")
    if len(blob) > MAX_FILE_BYTES:
        limit = MAX_FILE_BYTES // (1024 * 1024)
        raise StorageError(f"Файл больше {limit} МБ. Сожмите скрин или приложите ссылку.")

    content_type = sniff_content_type(blob, declared_type)

    # Имя приходит от клиента и попадёт в заголовок ответа при скачивании.
    # Оставляем только базовое имя: «../../etc/passwd» не должно доехать никуда.
    name = (filename or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '"\r\n')[:120]
    if not name:
        suffix = ALLOWED_TYPES.get(content_type) or mimetypes.guess_extension(content_type) or ""
        name = f"файл{suffix}"
    return content_type, name


# ── S3 ───────────────────────────────────────────────────────────────────────────


class StorageUnavailable(StorageError):
    """Хранилище недоступно из-за настройки, а не из-за самого файла.

    Отдельный класс, потому что эти два случая лечатся по-разному и путать их
    дорого: «файл не подошёл» правит человек, «бакет не отвечает» правит
    администратор. Один общий текст «не удалось сохранить» однажды уже стоил
    часа поисков — продовый образ ставится из requirements-prod.txt, boto3 туда
    не дописали, и отказ выглядел как сбой сети.
    """


@lru_cache(maxsize=1)
def _s3_client():
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover — зависимость объявлена
        raise StorageUnavailable(
            "Хранилище файлов не настроено на сервере: не установлен boto3"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=bbc_settings.s3_endpoint or None,
        aws_access_key_id=bbc_settings.s3_access_key_id,
        aws_secret_access_key=bbc_settings.s3_secret_access_key,
        region_name=bbc_settings.s3_region or "auto",
        config=Config(
            signature_version="s3v4",
            # Свежий botocore по умолчанию добавляет к запросу CRC32-контрольную
            # сумму, а R2 такой запрос отвергает. `when_required` возвращает
            # поведение к тому, которое S3-совместимые хранилища понимают.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _s3_key(touch_id: int, filename: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y/%m")
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    return f"touches/{stamp}/{touch_id}/{secrets.token_hex(8)}{suffix[:8]}"


# ── Интерфейс ────────────────────────────────────────────────────────────────────


def put(touch_id: int, blob: bytes, filename: str, content_type: str) -> StoredFile:
    backend = bbc_settings.storage_backend
    if backend != "s3":
        return StoredFile(
            backend="postgres",
            key=None,
            data=blob,
            content_type=content_type,
            size_bytes=len(blob),
        )

    key = _s3_key(touch_id, filename)
    try:
        _s3_client().put_object(
            Bucket=bbc_settings.s3_bucket,
            Key=key,
            Body=blob,
            ContentType=content_type,
        )
    except Exception as exc:
        # Молча свалиться в Postgres здесь нельзя: получилось бы, что часть
        # файлов в бакете, часть в базе, и никто не заметил, что бакет отвалился.
        log.exception("BBC: S3 put failed for touch %s", touch_id)
        raise StorageError("Не удалось сохранить файл. Попробуйте ещё раз.") from exc

    return StoredFile(
        backend="s3",
        key=key,
        data=None,
        content_type=content_type,
        size_bytes=len(blob),
    )


def get(backend: str, key: str | None, data: bytes | None) -> bytes:
    """Прочитать байты. `backend` берётся из строки файла, а не из настроек:
    после переключения на R2 старые файлы остаются лежать в Postgres."""
    if backend != "s3":
        if data is None:
            raise StorageError("Файл не найден")
        return data

    if not key:
        raise StorageError("Файл не найден")
    try:
        response = _s3_client().get_object(Bucket=bbc_settings.s3_bucket, Key=key)
        return response["Body"].read()
    except Exception as exc:
        log.exception("BBC: S3 get failed for %s", key)
        raise StorageError("Файл сейчас недоступен") from exc


def delete(backend: str, key: str | None) -> None:
    """Удаление из бакета — лучшее усилие: строка в базе уже уходит, и оставить
    её из-за сетевой ошибки хуже, чем оставить осиротевший объект в R2."""
    if backend != "s3" or not key:
        return
    try:
        _s3_client().delete_object(Bucket=bbc_settings.s3_bucket, Key=key)
    except Exception:
        log.warning("BBC: S3 delete failed for %s (объект остался в бакете)", key)


__all__ = [
    "ALLOWED_TYPES",
    "MAX_FILES_PER_TOUCH",
    "MAX_FILE_BYTES",
    "StorageError",
    "StoredFile",
    "delete",
    "get",
    "put",
    "validate",
]
