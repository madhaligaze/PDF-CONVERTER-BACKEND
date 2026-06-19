from fastapi import APIRouter

from app.api.routes.autocall import router as autocall_router
from app.api.routes.health import router as health_router
from app.api.routes.scanned import router as scanned_router
from app.api.routes.transforms import router as transforms_router

api_router = APIRouter()
api_router.include_router(health_router, tags=["health"])
api_router.include_router(transforms_router, tags=["transforms"])
api_router.include_router(scanned_router, tags=["scanned"])
api_router.include_router(autocall_router, tags=["autocall"])
