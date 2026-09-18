from fastapi import APIRouter, Depends, HTTPException

from app.api.routes.autocall import router as autocall_router
from app.api.routes.books import router as books_router  # «Книги» (удаляемый модуль)
from app.api.routes.finance import router as finance_router  # «Финансы» (удаляемый модуль)
from app.bbc.routes import router as bbc_router  # BBC Dashboard (removable module)
from app.api.routes.health import router as health_router
from app.api.routes.scanned import router as scanned_router
from app.api.routes.transforms import router as transforms_router
from app.webexcel.routes import router as webexcel_router  # Web-Excel (removable module)
from app.bbc.deps import require_active_user  # вход в «Таблицы» — см. ниже


def require_tables_admin(user=Depends(require_active_user)):
    """«Таблицы» открываются только админу дашборда BBC.

    Раньше маршруты раздела не спрашивали никого: на проде голый запрос отдавал
    список всех книг сервисного аккаунта и любую вкладку целиком. Админ, а не
    любая учётка — потому что здесь лежат «Журнал» и реестр продаж с ФОТ, а их
    дашборд намеренно не выдаёт сотрудникам (`bbc.scope.ADMIN_BLOCKS`).

    Проверка стоит здесь, на подключении роутера, а не внутри пакета: ни
    `app.webexcel`, ни `app.bbc` друг о друге не знают, и снести любой из них —
    значит поправить только этот файл.
    """
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Раздел «Таблицы» открыт только администраторам")
    return user


api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(transforms_router, tags=["transforms"])
api_router.include_router(scanned_router, tags=["scanned"])
api_router.include_router(autocall_router, tags=["autocall"])
api_router.include_router(bbc_router, tags=["bbc"])  # BBC Dashboard (removable module)
# Web-Excel (removable module). Закрыт целиком — каждый маршрут, включая будущие.
api_router.include_router(webexcel_router, dependencies=[Depends(require_tables_admin)])
# «Книги»: маршруты лежат снаружи пакета — там, где сходятся модули.
# Сам пакет про учётки ничего не знает, и это проверяется тестом.
api_router.include_router(books_router)
# «Финансы»: та же причина, что у «Книг» — маршруты снаружи пакета, потому что
# пакет про учётки не знает, а раздел закрыт правом.
api_router.include_router(finance_router)
