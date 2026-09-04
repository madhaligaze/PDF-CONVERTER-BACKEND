"""Все метаданные, за которыми следит alembic — в одном месте.

Вынесено из `env.py` не ради красоты. `env.py` при импорте запускает миграции:
внизу файла стоит `if context.is_offline_mode(): ... else: run_migrations_online()`,
и это выполняется на уровне модуля. Значит импортировать из него список
метаданных нельзя — тест, которому этот список нужен, запустил бы миграции
самим фактом импорта.

Список один и тот же для alembic и для теста намеренно: если модуль добавят
сюда, его начнёт проверять и `alembic revision --autogenerate`, и
`tests/test_migrations.py`. Две копии списка разъехались бы при первом же новом
модуле, и проверка «модели не разошлись с миграциями» тихо перестала бы
покрывать то, что добавили последним.
"""
from __future__ import annotations

from app.core.database import Base
import app.models.persistence  # noqa: F401 — регистрирует модели конвертера

from app.bbc.db import BbcBase  # BBC Dashboard (удаляемый модуль)
import app.bbc.models  # noqa: F401 — BBC Dashboard (удаляемый модуль)

from app.webexcel.db import WebExcelBase  # Раздел «Таблицы» (удаляемый модуль)
import app.webexcel.models  # noqa: F401 — раздел «Таблицы» (удаляемый модуль)

from app.books.db import BooksBase  # Раздел «Книги» (удаляемый модуль)
import app.books.models  # noqa: F401 — раздел «Книги» (удаляемый модуль)

#: Конвертер в `public`, BBC в `bbc`, «Таблицы» в `webexcel`, «Книги» в `books`.
#: Схема `webexcel` добавлена ревизией 0008 — до неё она жила вне учёта, и
#: расхождение с моделями обнаружить было нечем.
target_metadata = [
    Base.metadata,
    BbcBase.metadata,
    WebExcelBase.metadata,
    BooksBase.metadata,
]

__all__ = ["target_metadata"]
