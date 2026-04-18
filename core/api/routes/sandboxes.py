from fastapi import APIRouter, Request

router = APIRouter(prefix="/sandboxes")


@router.get("")
async def list_sandboxes(request: Request) -> dict:
    mgr = request.app.state.sandboxes
    if mgr is None:
        return {"sandboxes": []}
    return {"sandboxes": await mgr.list_all()}
