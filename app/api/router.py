from fastapi import APIRouter

from app.api.routes.autocall import router as autocall_router
from app.bbc.routes import router as bbc_router  # BBC Dashboard (removable module)
from app.api.routes.health import router as health_router
from app.api.routes.scanned import router as scanned_router
from app.api.routes.transforms import router as transforms_router
from app.webexcel.routes import router as webexcel_router  # Web-Excel (removable module)

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(transforms_router, tags=["transforms"])
api_router.include_router(scanned_router, tags=["scanned"])
api_router.include_router(autocall_router, tags=["autocall"])
api_router.include_router(bbc_router, tags=["bbc"])  # BBC Dashboard (removable module)
api_router.include_router(webexcel_router)  # Web-Excel (removable module)
