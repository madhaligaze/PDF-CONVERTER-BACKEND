from fastapi import APIRouter

from app.api.routes.autocall import router as autocall_router
from app.api.routes.books import router as books_router  # «Книги» (удаляемый модуль)
from app.api.routes.finance import router as finance_router  # «Финансы» (удаляемый модуль)
from app.api.routes.finance_contracts import router as finance_contracts_router  # реестр договоров «Финансов»
from app.api.routes.finance_people import router as finance_people_router  # доступ «Финансов»: люди, права, журнал
from app.bbc.routes import router as bbc_router  # BBC Dashboard (removable module)
from app.api.routes.health import router as health_router
from app.api.routes.scanned import router as scanned_router
from app.api.routes.transforms import router as transforms_router
from app.webexcel.routes import router as webexcel_router  # «Таблицы» (удаляемый модуль)

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(transforms_router, tags=["transforms"])
api_router.include_router(scanned_router, tags=["scanned"])
api_router.include_router(autocall_router, tags=["autocall"])
api_router.include_router(bbc_router, tags=["bbc"])  # BBC Dashboard (removable module)
# «Таблицы» открыты без входа: там только свои таблицы, чужих книг и
# сервисного аккаунта в разделе больше нет (см. app/webexcel/__init__.py).
api_router.include_router(webexcel_router)
# «Книги»: маршруты лежат снаружи пакета — там, где сходятся модули.
# Сам пакет про учётки ничего не знает, и это проверяется тестом.
api_router.include_router(books_router)
# «Финансы»: та же причина, что у «Книг» — маршруты снаружи пакета, потому что
# пакет про учётки не знает, а раздел закрыт правом.
api_router.include_router(finance_router)
api_router.include_router(finance_contracts_router)
api_router.include_router(finance_people_router)
