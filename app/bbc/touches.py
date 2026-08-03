"""Журнал касаний: кто, кому и когда написал по долгу, и к чему пришли.

Касание привязано к клиенту целиком, а не к договору. Так работают люди: звонят
главбуху, а не «по договору №247». Ключ — то же нормализованное имя, по которому
реестр дебиторки собирает договоры в одного должника, поэтому строка журнала и
строка реестра всегда встречаются.

Видимость. Читать касания по клиенту может тот, кто видит самого клиента —
значит, вопрос «чьи это данные» уже решён областью видимости строк, и второй,
независимой проверки здесь нет. Но видит человек при этом **все** касания по
своему клиенту, включая чужие: Дана обязана знать, что директор туда уже писал,
иначе журнал не выполняет ровно ту работу, ради которой заведён.

Автор может править и удалять своё, админ — любое. Удаление мягкое: история
касаний по долгу — доказательная база, и «удалить» здесь значит «убрать с
экрана», а не «стереть из мира».
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime
from typing import Any, Iterable, Sequence

from sqlalchemy import func, select

from app.bbc import storage
from app.bbc.auth import AuthedUser
from app.bbc.db import bbc_session
from app.bbc.models import BbcClientTouch, BbcTouchFile
from app.bbc.scope import Scope

log = logging.getLogger(__name__)

MAX_SUMMARY = 4000
MAX_CONTACT_NAME = 120

# Должности со стороны клиента.
#
# Список, а не свободный текст, ровно ради одного вопроса, который и просили:
# «Жанара 3 раза писала главбуху, 1 раз проджект-менеджеру». В свободном тексте
# окажутся «главбух», «ГЛ. БУХ» и «Айгуль», и посчитать будет нечего.
#
# Имя человека при этом остаётся свободным полем рядом: справочник контактов
# каждого клиента пришлось бы вести отдельно, а должность — величина конечная.
CONTACT_ROLES: tuple[dict[str, str], ...] = (
    {"key": "chief_accountant", "name": "Главный бухгалтер"},
    {"key": "accountant", "name": "Бухгалтер"},
    {"key": "finance_head", "name": "Финансовый директор"},
    {"key": "director", "name": "Директор"},
    {"key": "project_manager", "name": "Проджект-менеджер"},
    {"key": "assistant", "name": "Ассистент"},
    {"key": "founder", "name": "Учредитель"},
    {"key": "founder_spouse", "name": "Супруг(а) учредителя"},
    {"key": "lawyer", "name": "Юрист"},
    {"key": "procurement", "name": "Снабжение"},
    {"key": "other", "name": "Другое"},
)
ROLE_KEYS = frozenset(role["key"] for role in CONTACT_ROLES)
ROLE_NAMES = {role["key"]: role["name"] for role in CONTACT_ROLES}

CHANNELS: tuple[dict[str, str], ...] = (
    {"key": "whatsapp", "name": "WhatsApp"},
    {"key": "call", "name": "Звонок"},
    {"key": "email", "name": "Почта"},
    {"key": "meeting", "name": "Встреча"},
    {"key": "other", "name": "Другое"},
)
CHANNEL_KEYS = frozenset(channel["key"] for channel in CHANNELS)
CHANNEL_NAMES = {channel["key"]: channel["name"] for channel in CHANNELS}


class TouchError(Exception):
    """Отказ с сообщением, которое можно показать человеку."""


def client_key(name: str | None) -> str:
    """Нормализованное имя клиента — ключ группировки.

    Повторяет `clientKey()` из фронтенда (`blocks/receivables/debt.ts`): регистр
    и лишние пробелы сняты, больше ничего. Разойтись этим двум нельзя — иначе
    касание, записанное из реестра, не найдётся в журнале по тому же клиенту.
    """
    return re.sub(r"\s+", " ", (name or "")).strip().casefold()


# ── Видимость ────────────────────────────────────────────────────────────────────


def visible_client_keys(scope: Scope, rows: Sequence[Any]) -> set[str] | None:
    """Ключи клиентов, которых этому вызывающему видно. None = видно всех.

    `rows` — уже отфильтрованные областью видимости строки таблицы. Это и есть
    определение «своего клиента»: если его строка доехала до пользователя, то и
    касания по нему доехать должны.
    """
    if scope.is_admin:
        return None
    keys: set[str] = set()
    for row in rows:
        name = row.get("client") if isinstance(row, dict) else getattr(row, "client", None)
        key = client_key(name)
        if key:
            keys.add(key)
    return keys


# ── Разбор формы ─────────────────────────────────────────────────────────────────


def parse_touch_input(
    *,
    client: str | None,
    contacted_at: str | date | None,
    contact_role: str | None,
    contact_name: str | None,
    channel: str | None,
    summary: str | None,
) -> dict[str, Any]:
    name = " ".join((client or "").split())
    if not name:
        raise TouchError("Укажите клиента")

    if contact_role not in ROLE_KEYS:
        raise TouchError("Выберите, кому писали")

    kind = channel if channel in CHANNEL_KEYS else "whatsapp"

    when = contacted_at
    if isinstance(when, str):
        try:
            when = date.fromisoformat(when.strip()[:10])
        except ValueError as exc:
            raise TouchError("Неверная дата") from exc
    if when is None:
        when = datetime.now(UTC).date()
    # Задним числом — сколько угодно, вперёд — нельзя: «связался в следующем
    # вторнике» это опечатка, а не план.
    if when > datetime.now(UTC).date():
        raise TouchError("Дата касания не может быть в будущем")

    text = (summary or "").strip()[:MAX_SUMMARY]
    if not text:
        raise TouchError("Напишите, к чему пришли — без этого касание бесполезно")

    return {
        "client_key": client_key(name),
        "client_name": name,
        "contacted_at": when,
        "contact_role": contact_role,
        "contact_name": " ".join((contact_name or "").split())[:MAX_CONTACT_NAME],
        "channel": kind,
        "summary": text,
    }


# ── Сериализация ─────────────────────────────────────────────────────────────────


def serialize(touch: BbcClientTouch, files: Iterable[BbcTouchFile] = ()) -> dict[str, Any]:
    return {
        "id": touch.id,
        "client_key": touch.client_key,
        "client": touch.client_name,
        "author_user_id": touch.author_user_id,
        "author": touch.author_name,
        "contacted_at": touch.contacted_at.isoformat() if touch.contacted_at else None,
        "contact_role": touch.contact_role,
        "contact_role_name": ROLE_NAMES.get(touch.contact_role, touch.contact_role),
        "contact_name": touch.contact_name or "",
        "channel": touch.channel,
        "channel_name": CHANNEL_NAMES.get(touch.channel, touch.channel),
        "summary": touch.summary or "",
        "created_at": touch.created_at.isoformat() if touch.created_at else None,
        "files": [
            {
                "id": item.id,
                "filename": item.filename,
                "content_type": item.content_type,
                "size_bytes": item.size_bytes,
            }
            for item in files
        ],
    }


def _with_files(session, touches: Sequence[BbcClientTouch]) -> list[dict[str, Any]]:
    """Одним запросом на всю выборку — иначе на сотне касаний будет сто запросов."""
    if not touches:
        return []
    ids = [touch.id for touch in touches]
    by_touch: dict[int, list[BbcTouchFile]] = {}
    for item in session.scalars(select(BbcTouchFile).where(BbcTouchFile.touch_id.in_(ids))):
        by_touch.setdefault(item.touch_id, []).append(item)
    return [serialize(touch, by_touch.get(touch.id, ())) for touch in touches]


# ── Чтение ───────────────────────────────────────────────────────────────────────


def list_touches(
    allowed_keys: set[str] | None,
    *,
    client: str | None = None,
    author_id: int | None = None,
    contact_role: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    if allowed_keys is not None and not allowed_keys:
        return []

    with bbc_session() as session:
        query = select(BbcClientTouch).where(BbcClientTouch.deleted_at.is_(None))

        if allowed_keys is not None:
            query = query.where(BbcClientTouch.client_key.in_(allowed_keys))
        if client:
            query = query.where(BbcClientTouch.client_key == client_key(client))
        if author_id is not None:
            query = query.where(BbcClientTouch.author_user_id == author_id)
        if contact_role in ROLE_KEYS:
            query = query.where(BbcClientTouch.contact_role == contact_role)
        if date_from:
            query = query.where(BbcClientTouch.contacted_at >= date.fromisoformat(date_from[:10]))
        if date_to:
            query = query.where(BbcClientTouch.contacted_at <= date.fromisoformat(date_to[:10]))

        query = query.order_by(
            BbcClientTouch.contacted_at.desc(), BbcClientTouch.id.desc()
        ).limit(max(1, min(limit, 2000)))

        return _with_files(session, session.scalars(query).all())


def count_by_client(allowed_keys: set[str] | None) -> dict[str, int]:
    """Карта «ключ клиента → сколько касаний» для значков в реестре дебиторки.

    Отдельным лёгким запросом, а не полем в /dataset: реестр открывают гораздо
    чаще, чем журнал, и таскать в нём тексты всех касаний незачем.
    """
    if allowed_keys is not None and not allowed_keys:
        return {}

    with bbc_session() as session:
        query = (
            select(BbcClientTouch.client_key, func.count(BbcClientTouch.id))
            .where(BbcClientTouch.deleted_at.is_(None))
            .group_by(BbcClientTouch.client_key)
        )
        if allowed_keys is not None:
            query = query.where(BbcClientTouch.client_key.in_(allowed_keys))
        return {key: count for key, count in session.execute(query)}


# ── Запись ───────────────────────────────────────────────────────────────────────


def create_touch(actor: AuthedUser, form: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    with bbc_session() as session:
        touch = BbcClientTouch(
            author_user_id=actor.id,
            # Имя автора хранится копией: «Жанара написала главбуху» обязано
            # пережить её увольнение и удаление учётки.
            author_name=actor.display_name,
            created_at=now,
            updated_at=now,
            **form,
        )
        session.add(touch)
        session.flush()
        return serialize(touch)


def update_touch(actor: AuthedUser, touch_id: int, form: dict[str, Any]) -> dict[str, Any]:
    with bbc_session() as session:
        touch = _load_editable(session, actor, touch_id)
        for field, value in form.items():
            setattr(touch, field, value)
        touch.updated_at = datetime.now(UTC)
        session.flush()
        return _with_files(session, [touch])[0]


def delete_touch(actor: AuthedUser, touch_id: int) -> None:
    with bbc_session() as session:
        touch = _load_editable(session, actor, touch_id)
        touch.deleted_at = datetime.now(UTC)


# ── Файлы ────────────────────────────────────────────────────────────────────────


def attach_file(
    actor: AuthedUser, touch_id: int, blob: bytes, filename: str, declared_type: str | None
) -> dict[str, Any]:
    content_type, safe_name = storage.validate(blob, filename, declared_type)

    with bbc_session() as session:
        touch = _load_editable(session, actor, touch_id)
        count = session.scalar(
            select(func.count(BbcTouchFile.id)).where(BbcTouchFile.touch_id == touch.id)
        )
        if (count or 0) >= storage.MAX_FILES_PER_TOUCH:
            raise TouchError(f"К одному касанию можно приложить до {storage.MAX_FILES_PER_TOUCH} файлов")

        stored = storage.put(touch.id, blob, safe_name, content_type)
        record = BbcTouchFile(
            touch_id=touch.id,
            filename=safe_name,
            content_type=stored.content_type,
            size_bytes=stored.size_bytes,
            storage_backend=stored.backend,
            storage_key=stored.key,
            data=stored.data,
            uploaded_by=actor.id,
        )
        session.add(record)
        session.flush()
        return {
            "id": record.id,
            "filename": record.filename,
            "content_type": record.content_type,
            "size_bytes": record.size_bytes,
        }


def read_file(file_id: int, allowed_keys: set[str] | None) -> tuple[bytes, str, str]:
    """Байты файла с проверкой прав. Возвращает (данные, тип, имя).

    Права проверяются по клиенту, к которому относится касание, — тем же
    множеством ключей, что и чтение самого журнала. Файл вне области видимости
    отвечает «не найдено», а не «нельзя»: существование чужого документа по
    чужому долгу подтверждать незачем.
    """
    with bbc_session() as session:
        record = session.get(BbcTouchFile, file_id)
        if record is None:
            raise TouchError("Файл не найден")
        touch = session.get(BbcClientTouch, record.touch_id)
        if touch is None or touch.deleted_at is not None:
            raise TouchError("Файл не найден")
        if allowed_keys is not None and touch.client_key not in allowed_keys:
            raise TouchError("Файл не найден")

        backend, key, data = record.storage_backend, record.storage_key, record.data
        content_type, filename = record.content_type, record.filename

    # Чтение из S3 — снаружи транзакции: сетевой вызов не должен держать
    # соединение с базой открытым.
    return storage.get(backend, key, data), content_type, filename


def delete_file(actor: AuthedUser, file_id: int) -> None:
    with bbc_session() as session:
        record = session.get(BbcTouchFile, file_id)
        if record is None:
            raise TouchError("Файл не найден")
        # Право на файл = право на касание, к которому он приложен.
        _load_editable(session, actor, record.touch_id)
        backend, key = record.storage_backend, record.storage_key
        session.delete(record)

    storage.delete(backend, key)


def _load_editable(session, actor: AuthedUser, touch_id: int) -> BbcClientTouch:
    """Касание, которое этот человек вправе менять: своё, либо любое для админа.

    Чужое касание отвечает тем же «не найдено», что и несуществующее: подтверждать
    существование записи, к которой нет доступа, незачем.
    """
    touch = session.get(BbcClientTouch, touch_id)
    if touch is None or touch.deleted_at is not None:
        raise TouchError("Касание не найдено")
    if not actor.is_admin and touch.author_user_id != actor.id:
        raise TouchError("Касание не найдено")
    return touch


__all__ = [
    "CHANNELS",
    "CONTACT_ROLES",
    "TouchError",
    "client_key",
    "count_by_client",
    "create_touch",
    "delete_touch",
    "list_touches",
    "parse_touch_input",
    "serialize",
    "update_touch",
    "visible_client_keys",
]
