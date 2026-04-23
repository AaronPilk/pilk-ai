import asyncio

from fastapi import APIRouter, Request

from core.config import get_settings
from core.ledger.claude_code_usage import scan_usage as scan_claude_code_usage

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

    Combines two data sources so the dashboard ring reflects the
    operator's actual subscription pressure, not just PILK's slice:

    1. ``pilk_count`` — subscription-backed LLM turns PILK dispatched
       (``tier_provider=claude_code`` rows in the cost ledger).
    2. ``claude_code_count`` — assistant turns the operator ran
       directly via the Claude Code CLI, scanned from the session
       JSONL files under ``~/.claude/projects``. Captures work done
       outside PILK (typing ``claude`` in a terminal) which would
       otherwise invisibly consume the Max budget.
    """
    ledger = request.app.state.ledger
    settings = get_settings()
    pilk_data = await ledger.subscription_usage(window_hours=5)
    # The scan reads multiple .jsonl files from disk; offload to a
    # worker thread so the FastAPI event loop stays responsive on
    # big session directories.
    cli = await asyncio.to_thread(scan_claude_code_usage, window_hours=5)
    pilk_count = pilk_data["count"]
    cli_count = cli.count
    count = pilk_count + cli_count
    cap = max(1, int(settings.max_messages_per_5h))
    pct = min(100.0, round((count / cap) * 100.0, 1))
    return {
        "count": count,
        "pilk_count": pilk_count,
        "claude_code_count": cli_count,
        "window_hours": 5,
        "window_start": pilk_data["window_start"],
        "oldest_at": _older_of(pilk_data["oldest_at"], cli.oldest_at),
        "estimated_cap": cap,
        "pct": pct,
        "severity": (
            "ok" if pct < 60.0 else "warn" if pct < 85.0 else "hot"
        ),
        "claude_code": cli.to_public(),
    }


def _older_of(a: str | None, b: str | None) -> str | None:
    """Earlier of two ISO timestamps; tolerates either being None
    (no data on that side of the join)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if a < b else b
