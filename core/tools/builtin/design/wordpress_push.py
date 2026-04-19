"""Push an Elementor JSON payload to a WordPress site via the REST API.

The tool reads a pre-built Elementor template JSON from disk, extracts
its ``content`` array (which is what Elementor stores in post meta
``_elementor_data``), and POSTs to ``/wp-json/wp/v2/pages`` — creating
a new page or updating an existing one by ID.

**Auth:** WordPress application passwords over HTTP Basic. The secret
value is stored in :class:`IntegrationSecretsStore` in the format
``username:app_password`` (colon-separated) so the tool can decode both
halves from one row. The app password comes from the WP admin's
"Users → Profile → Application Passwords" screen. Spaces in an app
password are cosmetic; we strip them before encoding.

**Risk class:** :data:`RiskClass.NET_WRITE`. Every call queues through
the PILK approval gate by default. A TrustStore rule matching
``wordpress_push`` + a specific ``site_url`` can bypass approval for a
trusted, repeatable pipeline.

**What this does NOT do:**

* Upload media. Image URLs in the Elementor JSON must already be
  publicly reachable (PR I adds a Canva-asset upload step; for now the
  operator hosts images separately).
* Publish. New pages land as draft status so a human can review in
  the WP editor before going live.
* Handle WP "preview" or autosave flows.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx

from core.policy.risk import RiskClass
from core.secrets import resolve_secret
from core.tools.registry import Tool, ToolContext, ToolOutcome

WP_API_BASE = "/wp-json/wp/v2"
DEFAULT_TIMEOUT_S = 20.0


async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
    # ── Arg validation ──────────────────────────────────────────
    site_url = str(args.get("site_url") or "").strip().rstrip("/")
    target = args.get("target")
    title = str(args.get("title") or "").strip()
    elementor_json_path = str(args.get("elementor_json_path") or "").strip()
    secret_key = str(args.get("secret_key") or "").strip()

    if not site_url:
        return ToolOutcome(
            content="wordpress_push requires 'site_url'.", is_error=True
        )
    if target not in ("new",) and not (
        isinstance(target, int) and target > 0
    ):
        return ToolOutcome(
            content=(
                "wordpress_push requires 'target' to be 'new' (create a page) "
                "or a positive integer page ID (update)."
            ),
            is_error=True,
        )
    if not title:
        return ToolOutcome(
            content="wordpress_push requires non-empty 'title'.",
            is_error=True,
        )
    if not elementor_json_path:
        return ToolOutcome(
            content="wordpress_push requires 'elementor_json_path'.",
            is_error=True,
        )
    if not secret_key:
        return ToolOutcome(
            content=(
                "wordpress_push requires 'secret_key' — the name of the "
                "IntegrationSecretsStore row holding 'username:app_password'."
            ),
            is_error=True,
        )

    # ── Resolve credential ─────────────────────────────────────
    # Env fallback is intentionally None: WordPress credentials per-site
    # should never live in deploy-wide env. The operator pastes the
    # `username:app_password` into Settings → API Keys per site.
    raw_secret = resolve_secret(secret_key, env_fallback=None)
    if not raw_secret:
        return ToolOutcome(
            content=(
                f"'{secret_key}' is not configured. Paste "
                f"'username:app_password' into Settings → API Keys under "
                f"the exact name '{secret_key}'. WordPress app passwords "
                "are generated per-user at Users → Profile → Application "
                "Passwords on the target site."
            ),
            is_error=True,
        )
    parsed = _parse_credential(raw_secret)
    if parsed is None:
        return ToolOutcome(
            content=(
                f"'{secret_key}' must be formatted 'username:app_password' "
                "(colon-separated, single colon). Spaces in the app "
                "password are fine — we strip them."
            ),
            is_error=True,
        )
    username, app_password = parsed

    # ── Load + normalize Elementor JSON ────────────────────────
    try:
        content_list = _load_elementor_content(Path(elementor_json_path))
    except _LoadError as e:
        return ToolOutcome(content=str(e), is_error=True)

    # ── Build WP REST request ──────────────────────────────────
    headers = {
        "Authorization": _basic_auth(username, app_password),
        "Content-Type": "application/json",
    }
    # meta._elementor_data must be a JSON STRING in post meta, not a
    # nested object — WP serializes post meta as a string. The
    # Elementor plugin re-parses it when editing.
    body = {
        "title": title,
        "status": "draft",
        "meta": {
            "_elementor_data": json.dumps(content_list),
            "_elementor_edit_mode": "builder",
        },
    }

    path = (
        f"{WP_API_BASE}/pages"
        if target == "new"
        else f"{WP_API_BASE}/pages/{int(target)}"
    )
    url = f"{site_url}{path}"

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_S) as client:
            resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        return ToolOutcome(
            content=f"wordpress_push transport error: {e}", is_error=True
        )

    return _interpret_response(resp, site_url=site_url, target=target)


def _basic_auth(username: str, password: str) -> str:
    token = base64.b64encode(
        f"{username}:{password}".encode()
    ).decode("ascii")
    return f"Basic {token}"


def _parse_credential(raw: str) -> tuple[str, str] | None:
    """Accept ``username:app_password``. WP app passwords commonly have
    spaces — strip those without affecting the username. Require exactly
    one colon so we don't silently split a password that contains one.
    """
    s = raw.strip()
    if s.count(":") != 1:
        return None
    user, password = s.split(":", 1)
    user = user.strip()
    password = password.replace(" ", "").strip()
    if not user or not password:
        return None
    return user, password


class _LoadError(Exception):
    """Internal — never raised to callers; caught + wrapped in
    ToolOutcome."""


def _load_elementor_content(path: Path) -> list[Any]:
    """Read the Elementor JSON file and return the content array.

    Accepts three shapes:

    * ``{"content": [...], "version": ...}`` — full Elementor template
      export. We extract ``content``.
    * ``[{...}, {...}]`` — bare content array (caller already
      extracted it).
    * ``{...}`` without ``content`` — wrap in a list (single-container
      page) as an escape hatch for unusual callers.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise _LoadError(f"elementor_json_path not found: {path}") from e
    except OSError as e:
        raise _LoadError(f"could not read {path}: {e}") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise _LoadError(f"{path} is not valid JSON: {e}") from e
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        if "content" in data and isinstance(data["content"], list):
            return data["content"]
        return [data]
    raise _LoadError(
        f"unexpected JSON shape in {path} — expected list or object, "
        f"got {type(data).__name__}"
    )


def _interpret_response(
    resp: httpx.Response, *, site_url: str, target: Any
) -> ToolOutcome:
    # 401 / 403 get friendly messages — these are the common operator
    # mistakes (wrong password, user lacks edit_pages capability).
    if resp.status_code == 401:
        return ToolOutcome(
            content=(
                f"WordPress rejected auth (401) at {site_url}. Verify the "
                "`username:app_password` in Settings and that the app "
                "password hasn't been revoked."
            ),
            is_error=True,
        )
    if resp.status_code == 403:
        return ToolOutcome(
            content=(
                f"WordPress refused the request (403) at {site_url}. "
                "The WP user probably doesn't have `edit_pages` capability."
            ),
            is_error=True,
        )
    if resp.status_code >= 500:
        return ToolOutcome(
            content=(
                f"WordPress upstream error ({resp.status_code}) at "
                f"{site_url}. Body: {resp.text[:500]}"
            ),
            is_error=True,
        )
    if resp.status_code >= 400:
        return ToolOutcome(
            content=(
                f"WordPress rejected the request ({resp.status_code}). "
                f"Body: {resp.text[:500]}"
            ),
            is_error=True,
        )

    try:
        payload = resp.json()
    except ValueError:
        return ToolOutcome(
            content=f"WordPress returned non-JSON at {site_url}.",
            is_error=True,
        )

    page_id = payload.get("id")
    link = payload.get("link") or f"{site_url}/?p={page_id}"
    action = "created" if target == "new" else "updated"
    return ToolOutcome(
        content=f"{action} page {page_id} at {link}",
        data={
            "page_id": page_id,
            "url": link,
            "status": payload.get("status"),
            "action": action,
        },
    )


wordpress_push_tool = Tool(
    name="wordpress_push",
    description=(
        "Push Elementor JSON to a WordPress site via the REST API. "
        "Creates a new draft page (target='new') or updates an existing "
        "one (target=<page_id>). Credentials come from IntegrationSecrets "
        "in the format 'username:app_password'. Images in the JSON must "
        "be publicly reachable — this tool does not upload media. "
        "RiskClass.NET_WRITE; every call queues for operator approval."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "site_url": {
                "type": "string",
                "description": (
                    "Base URL of the WordPress site (e.g. "
                    "'https://acme.com'). Trailing slash optional."
                ),
            },
            "target": {
                "oneOf": [
                    {"type": "string", "enum": ["new"]},
                    {"type": "integer", "minimum": 1},
                ],
                "description": "'new' to create, or an existing page ID to update.",
            },
            "title": {"type": "string", "minLength": 1},
            "elementor_json_path": {
                "type": "string",
                "description": (
                    "Absolute path to an Elementor JSON file. Accepts "
                    "full template export (with `content` + `version`) "
                    "or a bare content array."
                ),
            },
            "secret_key": {
                "type": "string",
                "description": (
                    "Name of the IntegrationSecretsStore row holding "
                    "'username:app_password'. Typically matches the "
                    "client's `wordpress_secret_key` field."
                ),
            },
        },
        "required": [
            "site_url",
            "target",
            "title",
            "elementor_json_path",
            "secret_key",
        ],
    },
    risk=RiskClass.NET_WRITE,
    handler=_handler,
)


__all__ = ["wordpress_push_tool"]
