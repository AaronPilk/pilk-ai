from fastapi import APIRouter, Request

router = APIRouter(prefix="/cost")


@router.get("/summary")
async def summary(request: Request) -> dict:
    return await request.app.state.ledger.summary()


@router.get("/entries")
async def entries(request: Request, limit: int = 50) -> dict:
    return {"entries": await request.app.state.ledger.recent(limit=limit)}
