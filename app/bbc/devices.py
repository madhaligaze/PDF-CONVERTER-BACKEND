"""Строка `User-Agent` → как назвать это устройство человеку.

Модуль намеренно чистый: ни базы, ни сети, ни настроек. Поэтому таблицу
поведения можно проверить тестами за миллисекунды и проверить целиком, а не по
одному случаю на удачу.

Почему разбор свой, а не библиотека
───────────────────────────────────
Готовые разборщики `User-Agent` знают тысячи устройств и обновляются вслед за
рынком. Здесь этого не нужно: в списке сессий человек отвечает на один вопрос —
«это я заходил или не я». Для этого хватает браузера и системы, а всё лишнее
только добавляет зависимость, которая устаревает молча.

Главное правило: **не выдумывать**. Строка `User-Agent` — это то, что о себе
сообщил браузер, и подделать её может кто угодно. Если в ней не опознаётся
ничего, так и говорим: «неизвестное устройство». Придуманное название хуже
честного незнания — по нему человек решит, что заход был его, и не станет
завершать чужую сессию.
"""
from __future__ import annotations

import re

#: Браузеры. Порядок значим: проверки идут сверху вниз, и более узкие стоят
#: раньше. Edge и Opera представляются в том числе как Chrome, Chrome — как
#: Safari; поймай мы Chrome первым, Edge не нашёлся бы никогда.
_BROWSERS: tuple[tuple[str, str], ...] = (
    ("YaBrowser", "Яндекс.Браузер"),
    ("Edg", "Edge"),
    ("OPR", "Opera"),
    ("Opera", "Opera"),
    ("SamsungBrowser", "Samsung Internet"),
    ("Firefox", "Firefox"),
    ("CriOS", "Chrome"),
    ("FxiOS", "Firefox"),
    ("Chrome", "Chrome"),
    ("Safari", "Safari"),
)

#: Системы. Тоже сверху вниз: «Android» встречается в строках, где есть и
#: «Linux», а iPad и iPhone надо различать до общего «Mac OS».
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("Windows NT 10.0", "Windows"),
    ("Windows", "Windows"),
    ("Android", "Android"),
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)

_VERSION = re.compile(r"(?:Version|Chrome|Firefox|Edg|OPR|CriOS|FxiOS|YaBrowser)/(\d+)")

UNKNOWN = "Неизвестное устройство"


def browser_of(user_agent: str) -> str:
    for token, title in _BROWSERS:
        if token in user_agent:
            return title
    return ""


def system_of(user_agent: str) -> str:
    for token, title in _SYSTEMS:
        if token in user_agent:
            return title
    return ""


def describe_device(user_agent: str | None) -> str:
    """«Chrome на Windows». Пусто или непонятно — так и написано.

    Версия браузера в подпись не идёт намеренно. Она меняется сама каждые
    несколько недель, и один и тот же ноутбук выглядел бы в списке как десяток
    разных устройств — ровно то, что мешает заметить среди них чужое.
    """
    text = (user_agent or "").strip()
    if not text:
        return UNKNOWN
    browser = browser_of(text)
    system = system_of(text)
    if browser and system:
        return f"{browser} на {system}"
    return browser or system or UNKNOWN


def is_mobile(user_agent: str | None) -> bool:
    """Телефон или планшет — чтобы показать рядом подходящий значок."""
    text = user_agent or ""
    return any(token in text for token in ("Android", "iPhone", "iPad", "Mobile"))


__all__ = ["UNKNOWN", "browser_of", "describe_device", "is_mobile", "system_of"]
