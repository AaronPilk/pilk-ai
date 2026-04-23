from fastapi import APIRouter, Request

from core.config import get_settings

router = APIRouter(prefix="/cost")


@router.get("/summary")
async def summary(request: Request) -> dict:
    return await request.app.state.ledger.summary()


@router.get("/entries")
async def entries(request: Request, limit: int = 50) -> dict:
    return {"entries": await request.app.state.ledger.recent(limit=limit)}


@router.get("/subscription-usage")
async def subscription_usage(request: Request) -> dict:
    """Claude Max subscription usage approximation.

    Counts subscription-backed LLM turns (``tier_provider=claude_code``)
    in the rolling 5-hour window — Anthropic's documented Max plan
    rate-limit window. Anthropic doesn't expose the real cap via API,
    so we pair the live count with an operator-tunable
    ``PILK_MAX_MESSAGES_PER_5H`` estimate so the dashboard can render a
    "used / remaining" bar that's close enough to act on.
    """
    ledger = request.app.state.ledger
    settings = get_settings()
    data = await ledger.subscription_usage(window_hours=5)
    cap = max(1, int(settings.max_messages_per_5h))
    count = data["count"]
    pct = min(100.0, round((count / cap) * 100.0, 1))
    return {
        **data,
        "estimated_cap": cap,
        "pct": pct,
        # Bucket the bar color server-side so the client can stay
        # dumb: green <60%, amber 60-85%, red >85%.
        "severity": (
            "ok" if pct < 60.0 else "warn" if pct < 85.0 else "hot"
        ),
    }
