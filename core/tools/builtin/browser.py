"""Browser tools backed by Browserbase.

A single `BrowserSessionManager` keeps track of every open Browserbase
session so the dashboard can show a live-view iframe of what the agent is
doing. Three tools are exposed to agents:

- browser_session_open: create a new remote Chrome session
- browser_navigate:     point the current session at a URL and return text
- browser_session_close: tear the session down

Browser tools are tagged BROWSE — navigation/typing/scraping inside an
isolated Browserbase session. The gate auto-allows BROWSE so once the
user has authorized a task, PILK may freely drive the browser without
prompting per step. Actions that leave the sandbox (email, posts,
money) keep their original NET_WRITE / COMMS / FINANCIAL tags and still
hit the approval queue.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.browser")


@dataclass
class BrowserSession:
    id: str
    live_view_url: str
    connect_url: str
    agent_name: str | None = None
    sandbox_id: str | None = None
    plan_id: str | None = None
    status: str = "open"  # open | closed | errored
    current_url: str | None = None
    page_title: str | None = None
    created_at: float = 0.0
    last_action: str | None = None
    last_action_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "live_view_url": self.live_view_url,
            "agent_name": self.agent_name,
            "sandbox_id": self.sandbox_id,
            "plan_id": self.plan_id,
            "status": self.status,
            "current_url": self.current_url,
            "page_title": self.page_title,
            "created_at": self.created_at,
            "last_action": self.last_action,
            "last_action_at": self.last_action_at,
        }


class BrowserSessionManager:
    """In-memory registry of active Browserbase sessions.

    Keeps lightweight state so the dashboard can subscribe and render the
    live view. The actual browser lives on Browserbase; we only hold the
    connection URLs + last-known page info.
    """

    def __init__(
        self,
        api_key: str,
        project_id: str,
        broadcast=None,
    ) -> None:
        self._api_key = api_key
        self._project_id = project_id
        self._broadcast = broadcast
        self._sessions: dict[str, BrowserSession] = {}
        self._pages: dict[str, Any] = {}  # session_id -> playwright Page
        self._browsers: dict[str, Any] = {}
        self._pws: dict[str, Any] = {}
        self._lock = asyncio.Lock()

    def active(self) -> list[BrowserSession]:
        return [s for s in self._sessions.values() if s.status == "open"]

    def all(self) -> list[BrowserSession]:
        return list(self._sessions.values())

    async def open(
        self,
        agent_name: str | None,
        sandbox_id: str | None,
        plan_id: str | None = None,
    ) -> BrowserSession:
        # Imported lazily so pilkd still boots without the deps installed.
        import time

        from browserbase import Browserbase  # type: ignore
        from playwright.async_api import async_playwright  # type: ignore

        bb = Browserbase(api_key=self._api_key)
        raw = await asyncio.to_thread(
            bb.sessions.create, project_id=self._project_id
        )
        connect_url = getattr(raw, "connect_url", None) or raw.connectUrl  # type: ignore[attr-defined]
        session_id = raw.id
        # Debug live-view URL — Browserbase provides a `.debug_url`/`debug` field.
        live_view_url = ""
        try:
            debug = await asyncio.to_thread(
                bb.sessions.debug, session_id
            )
            live_view_url = (
                getattr(debug, "debugger_fullscreen_url", None)
                or getattr(debug, "debuggerFullscreenUrl", None)
                or getattr(debug, "debugger_url", None)
                or getattr(debug, "debuggerUrl", None)
                or ""
            )
        except Exception:  # pragma: no cover — SDK shape differs across versions
            live_view_url = f"https://www.browserbase.com/devtools/inspector.html?wss={session_id}"

        sess = BrowserSession(
            id=session_id,
            live_view_url=live_view_url,
            connect_url=connect_url,
            agent_name=agent_name,
            sandbox_id=sandbox_id,
            plan_id=plan_id,
            created_at=time.time(),
        )
        async with self._lock:
            self._sessions[session_id] = sess

        # Attach Playwright for future navigate/read calls.
        pw = await async_playwright().start()
        browser = await pw.chromium.connect_over_cdp(connect_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()
        self._pws[session_id] = pw
        self._browsers[session_id] = browser
        self._pages[session_id] = page

        await self._emit("browser.session_opened", sess.to_public())
        log.info("browser_session_opened", id=session_id, agent=agent_name)
        return sess

    async def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")
        await self._note_action(session_id, "navigating", {"url": url})
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        title = await page.title()
        body_text = await page.inner_text("body")
        body_text = body_text[:4000]
        sess = self._sessions[session_id]
        sess.current_url = url
        sess.page_title = title
        await self._note_action(session_id, "reading", {"url": url, "title": title})
        await self._emit("browser.session_updated", sess.to_public())
        return {"url": url, "title": title, "text": body_text}

    async def fill_contact_form(
        self,
        session_id: str,
        url: str,
        fields: dict[str, str],
        submit: bool = True,
    ) -> dict[str, Any]:
        """Navigate to a URL, find a contact form, fill it, optionally submit.

        Best-effort shim over a real "find the contact form" heuristic:
        we match input/textarea elements by name/id/placeholder/aria-label
        against each supplied field key (e.g. "name", "email", "message"),
        fill the first match for each, then click the most form-looking
        submit button. This covers ~80% of WordPress / HubSpot / Wix /
        Gravity-Forms / Typeform-embed contact pages — good enough for v1;
        callers should verify by follow-up navigate if outcome matters.
        """
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")

        await self._note_action(session_id, "navigating", {"url": url})
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        sess = self._sessions[session_id]
        sess.current_url = url
        sess.page_title = await page.title()

        filled: list[str] = []
        missed: list[str] = []
        for key, value in fields.items():
            # Try progressively broader selectors. All case-insensitive.
            kl = key.lower()
            candidates = [
                f'input[name*="{kl}" i]',
                f'textarea[name*="{kl}" i]',
                f'input[id*="{kl}" i]',
                f'textarea[id*="{kl}" i]',
                f'input[placeholder*="{kl}" i]',
                f'textarea[placeholder*="{kl}" i]',
                f'input[aria-label*="{kl}" i]',
                f'textarea[aria-label*="{kl}" i]',
            ]
            filled_this_key = False
            for sel in candidates:
                try:
                    el = await page.query_selector(sel)
                except Exception:
                    el = None
                if el is None:
                    continue
                try:
                    await el.fill(value)
                except Exception:
                    continue
                filled.append(key)
                filled_this_key = True
                await self._note_action(
                    session_id,
                    "typing",
                    {"field": key, "selector": sel},
                )
                break
            if not filled_this_key:
                missed.append(key)

        submitted = False
        if submit and filled:
            for sel in (
                'button[type="submit"]',
                'input[type="submit"]',
                'button:has-text("Send")',
                'button:has-text("Submit")',
                'button:has-text("Contact")',
            ):
                try:
                    btn = await page.query_selector(sel)
                except Exception:
                    btn = None
                if btn is None:
                    continue
                try:
                    await btn.click()
                    submitted = True
                    await self._note_action(
                        session_id,
                        "submitting",
                        {"selector": sel},
                    )
                    # Give the page a moment to react / navigate.
                    with contextlib.suppress(Exception):
                        await page.wait_for_load_state(
                            "domcontentloaded", timeout=8_000
                        )
                    break
                except Exception:
                    continue

        await self._emit("browser.session_updated", sess.to_public())
        return {
            "url": url,
            "title": sess.page_title,
            "filled": filled,
            "missed": missed,
            "submitted": submitted,
        }

    async def _note_action(
        self, session_id: str, action: str, detail: dict[str, Any]
    ) -> None:
        """Record and broadcast a short human-readable action label.

        Feeds the Live browser strip in the dashboard so the user sees
        'navigating → example.com', 'reading', 'typing …' as they happen.
        """
        import time

        sess = self._sessions.get(session_id)
        if sess is None:
            return
        sess.last_action = action
        sess.last_action_at = time.time()
        await self._emit(
            "browser.action",
            {
                "session_id": session_id,
                "plan_id": sess.plan_id,
                "agent_name": sess.agent_name,
                "action": action,
                "detail": detail,
                "at": sess.last_action_at,
            },
        )

    async def close(self, session_id: str) -> None:
        page = self._pages.pop(session_id, None)
        browser = self._browsers.pop(session_id, None)
        pw = self._pws.pop(session_id, None)
        try:
            if page is not None:
                await page.close()
        except Exception:
            pass
        try:
            if browser is not None:
                await browser.close()
        except Exception:
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:
            pass
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess.status = "closed"
            await self._emit("browser.session_closed", sess.to_public())
            log.info("browser_session_closed", id=session_id)

    async def close_all(self) -> None:
        for sid in list(self._sessions.keys()):
            await self.close(sid)

    async def close_for_plan(self, plan_id: str) -> list[str]:
        """Close every open session tied to `plan_id`. Returns closed ids.

        Called when the user cancels a plan — any browser sessions that
        plan owns go with it, so the sandbox doesn't keep running after
        the operator has hit stop.
        """
        ids = [
            sid
            for sid, s in self._sessions.items()
            if s.plan_id == plan_id and s.status == "open"
        ]
        for sid in ids:
            await self.close(sid)
        return ids

    async def _emit(self, event: str, payload: dict) -> None:
        if self._broadcast is None:
            return
        try:
            await self._broadcast(event, payload)
        except Exception:
            log.warning("broadcast_failed", event=event)


def make_browser_tools(
    manager: BrowserSessionManager,
) -> list[Tool]:
    async def _open(args: dict, ctx: ToolContext) -> ToolOutcome:
        try:
            sess = await manager.open(
                agent_name=ctx.agent_name,
                sandbox_id=ctx.sandbox_id,
                plan_id=ctx.plan_id,
            )
        except ImportError as e:
            return ToolOutcome(
                content=(
                    "browser tool unavailable: dependency missing. Run "
                    "`pip install browserbase playwright` and restart pilkd. "
                    f"({e})"
                ),
                is_error=True,
            )
        except Exception as e:
            return ToolOutcome(
                content=f"browser session open failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"Opened Browserbase session {sess.id}. Live view: "
                f"{sess.live_view_url}. Call browser_navigate with this "
                f"session_id to visit a URL."
            ),
            data={
                "session_id": sess.id,
                "live_view_url": sess.live_view_url,
            },
        )

    async def _navigate(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        url = str(args["url"])
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolOutcome(
                content=f"refused: only http(s) URLs allowed: {url}",
                is_error=True,
            )
        try:
            result = await manager.navigate(session_id, url)
        except KeyError:
            return ToolOutcome(
                content=f"no such session: {session_id}",
                is_error=True,
            )
        except Exception as e:
            return ToolOutcome(
                content=f"navigate failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"→ {result['url']}\n"
                f"title: {result['title']}\n\n"
                f"{result['text']}"
            ),
            data=result,
        )

    async def _close(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        try:
            await manager.close(session_id)
        except Exception as e:
            return ToolOutcome(
                content=f"close failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(content=f"closed browser session {session_id}")

    async def _fill(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        url = str(args["url"])
        if not (url.startswith("http://") or url.startswith("https://")):
            return ToolOutcome(
                content=f"refused: only http(s) URLs allowed: {url}",
                is_error=True,
            )
        fields = args.get("fields") or {}
        if not isinstance(fields, dict) or not fields:
            return ToolOutcome(
                content=(
                    "browser_form_fill requires a non-empty 'fields' "
                    "object (e.g. {\"name\": \"...\", \"email\": \"...\", "
                    "\"message\": \"...\"})."
                ),
                is_error=True,
            )
        fields_str = {str(k): str(v) for k, v in fields.items()}
        submit = bool(args.get("submit", True))
        try:
            result = await manager.fill_contact_form(
                session_id, url, fields_str, submit=submit,
            )
        except KeyError:
            return ToolOutcome(
                content=f"no such session: {session_id}",
                is_error=True,
            )
        except Exception as e:
            return ToolOutcome(
                content=f"form-fill failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        filled = ", ".join(result["filled"]) or "none"
        missed = ", ".join(result["missed"]) or "none"
        return ToolOutcome(
            content=(
                f"→ {result['url']}\n"
                f"title: {result['title']}\n"
                f"filled: {filled}\n"
                f"missed: {missed}\n"
                f"submitted: {result['submitted']}"
            ),
            data=result,
        )

    open_tool = Tool(
        name="browser_session_open",
        description=(
            "Open a new remote Chrome session on Browserbase. Returns a "
            "session_id and a live_view_url the user can watch in real time. "
            "Always open a session before navigating. Close it with "
            "browser_session_close when done."
        ),
        input_schema={"type": "object", "properties": {}},
        risk=RiskClass.BROWSE,
        handler=_open,
    )
    navigate_tool = Tool(
        name="browser_navigate",
        description=(
            "Navigate an open browser session to an http(s) URL and return "
            "the page title plus up to 4 KiB of visible body text. Requires "
            "a session_id from browser_session_open."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "url": {"type": "string"},
            },
            "required": ["session_id", "url"],
        },
        risk=RiskClass.BROWSE,
        handler=_navigate,
    )
    close_tool = Tool(
        name="browser_session_close",
        description="Close an open browser session by id.",
        input_schema={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
        risk=RiskClass.BROWSE,
        handler=_close,
    )
    fill_tool = Tool(
        name="browser_form_fill",
        description=(
            "Navigate a Browserbase session to a contact-form URL, match "
            "inputs/textareas against the supplied fields "
            "(e.g. name/email/phone/message), fill them, and optionally "
            "click submit. Best-effort heuristic — verify outcome with a "
            "follow-up browser_navigate on the same session if it matters. "
            "Requires a session_id from browser_session_open."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "url": {"type": "string"},
                "fields": {
                    "type": "object",
                    "description": (
                        "Field label → value map. Keys are matched "
                        "case-insensitively against input/textarea "
                        "name/id/placeholder/aria-label."
                    ),
                    "additionalProperties": {"type": "string"},
                },
                "submit": {
                    "type": "boolean",
                    "description": (
                        "Click submit after filling (default true). Set "
                        "false to leave the form populated for review."
                    ),
                },
            },
            "required": ["session_id", "url", "fields"],
        },
        risk=RiskClass.COMMS,
        handler=_fill,
    )
    return [open_tool, navigate_tool, close_tool, fill_tool]
