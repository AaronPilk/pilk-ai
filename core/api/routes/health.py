from fastapi import APIRouter

from core import __version__

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/version")
async def version() -> dict:
    return {"version": __version__}
