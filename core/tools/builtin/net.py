"""Network read tool — HTTP GET with strict size + timeout caps.

Tagged NET_READ so it always falls outside the auto-allow set: every call
flows through the approval queue (or a matching trust rule). The body is
truncated and binary content rejected — the model gets text or an error.
"""

from __future__ import annotations

from typing import Any

import httpx

from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

MAX_BODY_BYTES = 256 * 1024
DEFAULT_TIMEOUT_S = 15


async def _net_fetch(args: dict, ctx: ToolContext) -> ToolOutcome:
    url = str(args["url"])
    if not (url.startswith("http://") or url.startswith("https://")):
        return ToolOutcome(content=f"refused: only http(s) URLs allowed: {url}", is_error=True)
    timeout = float(args.get("timeout_s") or DEFAULT_TIMEOUT_S)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as client:
            resp = await client.get(url)
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(content=f"fetch failed: {type(e).__name__}: {e}", is_error=True)

    raw = resp.content[: MAX_BODY_BYTES + 1]
    truncated = len(raw) > MAX_BODY_BYTES
    body_bytes = raw[:MAX_BODY_BYTES]
    try:
        text = body_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return ToolOutcome(
            content=f"refused: non-text body ({resp.headers.get('content-type', '?')})",
            is_error=True,
            data={"status": resp.status_code},
        )
    suffix = f"\n\n[truncated — shown {MAX_BODY_BYTES} of {len(resp.content)} bytes]" if truncated else ""
    data: dict[str, Any] = {
        "status": resp.status_code,
        "content_type": resp.headers.get("content-type"),
        "bytes": len(resp.content),
    }
    return ToolOutcome(
        content=f"GET {url} → {resp.status_code}\n\n{text}{suffix}",
        is_error=resp.status_code >= 400,
        data=data,
    )


net_fetch_tool = Tool(
    name="net_fetch",
    description=(
        "Fetch a URL via HTTP GET. Text bodies only; max 256 KiB; default 15s "
        "timeout. Outbound network — every call requires user approval unless "
        "covered by an explicit trust rule."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "Absolute http(s) URL."},
            "timeout_s": {"type": "number", "minimum": 1, "maximum": 60},
        },
        "required": ["url"],
    },
    risk=RiskClass.NET_READ,
    handler=_net_fetch,
)
