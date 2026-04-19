"""Creative-content toolkit: image + video generation.

Two thin wrappers used by the `creative_content_agent`:

    * `nano_banana_generate` — Google's `gemini-2.5-flash-image` (a.k.a.
      "Nano Banana"). One synchronous call → inline base64 image bytes →
      written to the workspace as a PNG.

    * `higgsfield_generate` — Higgsfield Cloud cinematic text→video /
      image→video. Async generation with a status URL; we poll every 5s
      up to a hard cap, then download the resulting MP4 into the
      workspace.

Both tools follow the rest of the toolkit's conventions:
    * Dashboard-paste secrets win over env vars (resolve_secret).
    * Missing key → clean ``is_error`` outcome telling the operator
      exactly which integration to set up.
    * Network failures are reported as is_error rather than raised so
      the agent loop can recover.
    * Outputs are scoped to the agent's sandbox workspace, never the
      shared workspace, when the caller is sandboxed.

Risk class is NET_WRITE for both — these calls cost real money and
emit non-idempotent jobs into a third party.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from pathlib import Path
from typing import Any

import httpx

from core.config import get_settings
from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

GEMINI_BASE = "https://generativelanguage.googleapis.com"
NANO_BANANA_MODEL = "gemini-2.5-flash-image"
DEFAULT_TIMEOUT_S = 60.0
HIGGSFIELD_POLL_INTERVAL_S = 5.0
HIGGSFIELD_MAX_WAIT_S = 600.0  # 10 min cap; videos rarely take longer
ASPECT_TO_GEMINI = {
    "1:1": "1:1",
    "16:9": "16:9",
    "9:16": "9:16",
    "4:3": "4:3",
    "3:4": "3:4",
}


def _secret(name: str, fallback: str | None) -> str | None:
    """Dashboard-paste secret wins; env-var fallback for boot/migration."""
    return resolve_secret(name, fallback)


def _workspace_root(ctx: ToolContext) -> Path:
    """Where this tool writes generated assets. Mirrors fs.py logic."""
    if ctx.sandbox_root is not None:
        root = ctx.sandbox_root.expanduser().resolve()
    else:
        root = get_settings().workspace_dir.expanduser().resolve()
    out = root / "creative"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _slugify(prompt: str, max_len: int = 40) -> str:
    """Filename-safe slug from a free-text prompt for stable output names."""
    s = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    return (s[:max_len] or "untitled").rstrip("-")


# ── Nano Banana (Gemini 2.5 Flash Image) ──────────────────────────


async def _nano_banana_generate(
    args: dict, ctx: ToolContext
) -> ToolOutcome:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return ToolOutcome(
            content="nano_banana_generate requires a 'prompt'.",
            is_error=True,
        )
    aspect = str(args.get("aspect_ratio") or "1:1")
    if aspect not in ASPECT_TO_GEMINI:
        return ToolOutcome(
            content=(
                f"aspect_ratio '{aspect}' not supported. Use one of "
                f"{sorted(ASPECT_TO_GEMINI)}."
            ),
            is_error=True,
        )

    api_key = _secret("nano_banana_api_key", get_settings().nano_banana_api_key)
    if not api_key:
        return ToolOutcome(
            content=(
                "Nano Banana is not configured. Add a Google AI / Gemini "
                "API key in Settings → API Keys (or set "
                "NANO_BANANA_API_KEY)."
            ),
            is_error=True,
        )

    url = (
        f"{GEMINI_BASE}/v1beta/models/{NANO_BANANA_MODEL}:generateContent"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseModalities": ["IMAGE"],
            "imageConfig": {"aspectRatio": ASPECT_TO_GEMINI[aspect]},
        },
    }

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(
                url,
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=f"nano_banana_generate failed: {type(e).__name__}: {e}",
            is_error=True,
        )

    if resp.status_code >= 400:
        return ToolOutcome(
            content=(
                f"Nano Banana error {resp.status_code}: "
                f"{resp.text[:500]}"
            ),
            is_error=True,
            data={"status": resp.status_code},
        )

    payload = resp.json()
    image_bytes = _extract_inline_image(payload)
    if image_bytes is None:
        return ToolOutcome(
            content=(
                "Nano Banana returned no image bytes. "
                f"Raw response: {str(payload)[:500]}"
            ),
            is_error=True,
            data={"response": payload},
        )

    out_dir = _workspace_root(ctx)
    name = f"{int(time.time())}-{_slugify(prompt)}.png"
    out_path = out_dir / name
    out_path.write_bytes(image_bytes)

    rel = out_path.relative_to(out_dir.parent)
    return ToolOutcome(
        content=(
            f"Generated image ({len(image_bytes)} bytes) saved to "
            f"{rel}. Prompt: {prompt}"
        ),
        data={
            "path": str(rel),
            "absolute_path": str(out_path),
            "bytes": len(image_bytes),
            "aspect_ratio": aspect,
            "model": NANO_BANANA_MODEL,
        },
    )


def _extract_inline_image(payload: dict[str, Any]) -> bytes | None:
    """Pull the first inline_data PNG out of a Gemini generateContent
    response. Schema:
        {"candidates":[{"content":{"parts":[
            {"inlineData":{"mimeType":"image/png","data":"<b64>"}}, ...
        ]}}]}
    """
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                try:
                    return base64.b64decode(inline["data"])
                except (ValueError, TypeError):
                    return None
    return None


nano_banana_generate_tool = Tool(
    name="nano_banana_generate",
    description=(
        "Generate an image from a text prompt using Google Nano Banana "
        "(gemini-2.5-flash-image). Saves a PNG to the agent workspace "
        "under creative/ and returns the relative path. Requires "
        "NANO_BANANA_API_KEY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Free-text image description.",
            },
            "aspect_ratio": {
                "type": "string",
                "enum": sorted(ASPECT_TO_GEMINI),
                "description": "Output aspect ratio (default 1:1).",
            },
        },
        "required": ["prompt"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_nano_banana_generate,
)


# ── Higgsfield (cinematic video) ──────────────────────────────────


async def _higgsfield_generate(
    args: dict, ctx: ToolContext
) -> ToolOutcome:
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return ToolOutcome(
            content="higgsfield_generate requires a 'prompt'.",
            is_error=True,
        )
    image_url = args.get("image_url")
    duration_s = int(args.get("duration_s") or 5)
    duration_s = max(3, min(duration_s, 10))

    api_key = _secret(
        "higgsfield_api_key", get_settings().higgsfield_api_key
    )
    if not api_key:
        return ToolOutcome(
            content=(
                "Higgsfield is not configured. Add a Higgsfield API key "
                "in Settings → API Keys (or set HIGGSFIELD_API_KEY)."
            ),
            is_error=True,
        )

    base = get_settings().higgsfield_api_base.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    request_body: dict[str, Any] = {
        "prompt": prompt,
        "duration": duration_s,
    }
    if image_url:
        request_body["image_url"] = str(image_url)

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            create = await client.post(
                f"{base}/v1/generations",
                headers=headers,
                json=request_body,
            )
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=f"higgsfield_generate failed: {type(e).__name__}: {e}",
            is_error=True,
        )

    if create.status_code >= 400:
        return ToolOutcome(
            content=(
                f"Higgsfield create error {create.status_code}: "
                f"{create.text[:500]}"
            ),
            is_error=True,
            data={"status": create.status_code},
        )

    created = create.json()
    status_url = created.get("status_url") or created.get("statusUrl")
    generation_id = (
        created.get("generation_id")
        or created.get("generationId")
        or created.get("id")
    )
    if not status_url and generation_id:
        status_url = f"{base}/v1/generations/{generation_id}"
    if not status_url:
        return ToolOutcome(
            content=(
                "Higgsfield response missing both status_url and "
                f"generation_id: {str(created)[:300]}"
            ),
            is_error=True,
            data={"response": created},
        )

    video_url = await _poll_higgsfield(status_url, headers)
    if video_url is None:
        return ToolOutcome(
            content=(
                f"Higgsfield generation timed out after "
                f"{HIGGSFIELD_MAX_WAIT_S:.0f}s (id={generation_id})."
            ),
            is_error=True,
            data={"generation_id": generation_id},
        )
    if isinstance(video_url, ToolOutcome):
        return video_url

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            video_resp = await client.get(video_url)
    except (httpx.HTTPError, TimeoutError) as e:
        return ToolOutcome(
            content=(
                f"Failed to download Higgsfield video: "
                f"{type(e).__name__}: {e}"
            ),
            is_error=True,
            data={"video_url": video_url},
        )
    if video_resp.status_code >= 400:
        return ToolOutcome(
            content=(
                f"Failed to download Higgsfield video "
                f"({video_resp.status_code}): {video_resp.text[:300]}"
            ),
            is_error=True,
            data={"video_url": video_url},
        )

    out_dir = _workspace_root(ctx)
    name = f"{int(time.time())}-{_slugify(prompt)}.mp4"
    out_path = out_dir / name
    out_path.write_bytes(video_resp.content)
    rel = out_path.relative_to(out_dir.parent)

    return ToolOutcome(
        content=(
            f"Generated {duration_s}s video ({len(video_resp.content)} "
            f"bytes) saved to {rel}. Prompt: {prompt}"
        ),
        data={
            "path": str(rel),
            "absolute_path": str(out_path),
            "bytes": len(video_resp.content),
            "duration_s": duration_s,
            "generation_id": generation_id,
        },
    )


async def _poll_higgsfield(
    status_url: str, headers: dict[str, str]
) -> str | ToolOutcome | None:
    """Poll until ``status`` is terminal. Returns the downloadable
    video URL on success, a ToolOutcome (with is_error=True) on a
    server-reported failure, or None if the wait cap is exceeded.
    """
    deadline = time.monotonic() + HIGGSFIELD_MAX_WAIT_S
    async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(status_url, headers=headers)
            except (httpx.HTTPError, TimeoutError) as e:
                return ToolOutcome(
                    content=(
                        f"Higgsfield status poll failed: "
                        f"{type(e).__name__}: {e}"
                    ),
                    is_error=True,
                )
            if resp.status_code >= 400:
                return ToolOutcome(
                    content=(
                        f"Higgsfield status error {resp.status_code}: "
                        f"{resp.text[:500]}"
                    ),
                    is_error=True,
                )
            payload = resp.json()
            state = (payload.get("status") or "").lower()
            if state in ("succeeded", "completed", "success"):
                return (
                    payload.get("video_url")
                    or payload.get("output_url")
                    or (payload.get("output") or {}).get("video_url")
                )
            if state in ("failed", "error", "cancelled", "canceled"):
                return ToolOutcome(
                    content=(
                        f"Higgsfield generation {state}: "
                        f"{payload.get('error') or payload.get('message') or ''}"
                    ),
                    is_error=True,
                    data={"response": payload},
                )
            await asyncio.sleep(HIGGSFIELD_POLL_INTERVAL_S)
    return None


CREATIVE_TOOLS: list[Tool] = []  # populated below; declared here so the
# circular import-order issue (Tool referencing CREATIVE_TOOLS in module
# init) is impossible — both items live in the same module.


higgsfield_generate_tool = Tool(
    name="higgsfield_generate",
    description=(
        "Generate a short cinematic video from a text prompt (and "
        "optional reference image_url) using Higgsfield Cloud. Polls "
        "until the job finishes (cap 10 min), downloads the MP4 into "
        "the agent workspace under creative/, and returns the relative "
        "path. Requires HIGGSFIELD_API_KEY."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Free-text scene description.",
            },
            "image_url": {
                "type": "string",
                "description": (
                    "Optional reference image. Providing this switches "
                    "to image→video mode."
                ),
            },
            "duration_s": {
                "type": "integer",
                "minimum": 3,
                "maximum": 10,
                "description": "Clip length in seconds (default 5).",
            },
        },
        "required": ["prompt"],
    },
    risk=RiskClass.NET_WRITE,
    handler=_higgsfield_generate,
)


CREATIVE_TOOLS.extend([nano_banana_generate_tool, higgsfield_generate_tool])
