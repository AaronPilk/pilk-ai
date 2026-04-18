from fastapi import APIRouter

from core import __version__
from core.config import get_settings

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict:
    settings = get_settings()
    return {
        "version": __version__,
        "home": str(settings.resolve_home()),
    }
