from fastapi import APIRouter, Request

router = APIRouter(prefix="/browser")


@router.get("/sessions")
async def list_browser_sessions(request: Request) -> dict:
    mgr = getattr(request.app.state, "browser_sessions", None)
    if mgr is None:
        return {"sessions": [], "active": [], "enabled": False}
    return {
        "enabled": True,
        "sessions": [s.to_public() for s in mgr.all()],
        "active": [s.to_public() for s in mgr.active()],
    }
