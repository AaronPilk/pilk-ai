"""Browser tools backed by Browserbase.

A single `BrowserSessionManager` keeps track of every open Browserbase
session so the dashboard can show a live-view iframe of what the agent is
doing. Core tools exposed to agents:

- browser_session_open: create a new remote Chrome session
- browser_navigate:     point the current session at a URL and return text
- browser_click:        click a specific selector
- browser_type:         type text into a specific selector
- browser_wait_for:     wait for selector/url conditions
- browser_eval_js:      execute page-scoped JavaScript
- browser_session_close: tear the session down

Browser tools are tagged BROWSE — navigation/typing/scraping inside an
isolated Browserbase session. The gate auto-allows BROWSE so once the
user has authorized a task, PILK may freely drive the browser without
prompting per step. Actions that leave the sandbox (email, posts,
money) keep their original NET_WRITE / COMMS / FINANCIAL tags and still
hit the approval queue.

Two form-submit tools exist on purpose:

- browser_form_fill         (COMMS): outreach/contact-style forms where
  a human recipient may receive the message.
- browser_form_fill_account (NET_WRITE): service-account signup /
  dashboard onboarding flows using PILK's own identity.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

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
        # Cost-safe signup guardrail: max one automated signup attempt
        # per site per plan/session before human handoff.
        self._signup_attempts: dict[tuple[str, str], int] = {}
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

    async def click(
        self,
        session_id: str,
        selector: str,
        *,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")
        await self._note_action(session_id, "clicking", {"selector": selector})
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        await page.click(selector, timeout=timeout_ms)
        with contextlib.suppress(Exception):
            await page.wait_for_load_state("domcontentloaded", timeout=8_000)
        sess = self._sessions[session_id]
        sess.current_url = page.url
        sess.page_title = await page.title()
        await self._emit("browser.session_updated", sess.to_public())
        return {
            "url": sess.current_url,
            "title": sess.page_title,
            "selector": selector,
        }

    async def type_text(
        self,
        session_id: str,
        selector: str,
        text: str,
        *,
        clear_first: bool = True,
        press_enter: bool = False,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")
        await self._note_action(
            session_id,
            "typing",
            {"selector": selector, "chars": len(text)},
        )
        await page.wait_for_selector(selector, timeout=timeout_ms, state="visible")
        if clear_first:
            with contextlib.suppress(Exception):
                await page.fill(selector, "", timeout=timeout_ms)
        await page.fill(selector, text, timeout=timeout_ms)
        if press_enter:
            with contextlib.suppress(Exception):
                await page.press(selector, "Enter", timeout=timeout_ms)
        sess = self._sessions[session_id]
        sess.current_url = page.url
        sess.page_title = await page.title()
        await self._emit("browser.session_updated", sess.to_public())
        return {
            "url": sess.current_url,
            "title": sess.page_title,
            "selector": selector,
            "typed_chars": len(text),
            "pressed_enter": press_enter,
        }

    async def wait_for(
        self,
        session_id: str,
        *,
        selector: str | None = None,
        state: str = "visible",
        url_contains: str | None = None,
        timeout_ms: int = 15_000,
    ) -> dict[str, Any]:
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")
        if not selector and not url_contains:
            raise ValueError("wait_for requires selector or url_contains")

        await self._note_action(
            session_id,
            "waiting",
            {
                "selector": selector,
                "state": state,
                "url_contains": url_contains,
                "timeout_ms": timeout_ms,
            },
        )
        if selector:
            await page.wait_for_selector(selector, state=state, timeout=timeout_ms)
        if url_contains:
            await page.wait_for_url(
                f"**{url_contains}**",
                timeout=timeout_ms,
            )
        sess = self._sessions[session_id]
        sess.current_url = page.url
        sess.page_title = await page.title()
        await self._emit("browser.session_updated", sess.to_public())
        return {
            "ok": True,
            "url": sess.current_url,
            "title": sess.page_title,
            "selector": selector,
            "state": state,
            "url_contains": url_contains,
        }

    async def eval_js(
        self,
        session_id: str,
        script: str,
        *,
        arg: Any = None,
    ) -> dict[str, Any]:
        page = self._pages.get(session_id)
        if page is None:
            raise KeyError(f"no active browser session: {session_id}")
        await self._note_action(session_id, "evaluating_js", {"chars": len(script)})
        result = await page.evaluate(script, arg)
        sess = self._sessions[session_id]
        sess.current_url = page.url
        sess.page_title = await page.title()
        await self._emit("browser.session_updated", sess.to_public())
        return {
            "url": sess.current_url,
            "title": sess.page_title,
            "result": result,
        }

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

    @staticmethod
    def _site_key(url: str) -> str:
        host = (urlparse(url).hostname or "").lower().strip()
        return host or "unknown-site"

    @staticmethod
    def _attempt_scope_key(plan_id: str | None, session_id: str) -> str:
        return f"plan:{plan_id}" if plan_id else f"session:{session_id}"

    async def _captcha_present(self, page: Any) -> bool:
        checks = (
            'iframe[src*="recaptcha"]',
            '[class*="g-recaptcha"]',
            'iframe[src*="hcaptcha"]',
            '[class*="h-captcha"]',
            'iframe[src*="turnstile"]',
            '[class*="cf-turnstile"]',
            '[id*="captcha" i]',
            '[class*="captcha" i]',
        )
        for sel in checks:
            with contextlib.suppress(Exception):
                if await page.query_selector(sel):
                    return True
        with contextlib.suppress(Exception):
            body = (await page.inner_text("body"))[:6000].lower()
            hints = (
                "captcha",
                "i am not a robot",
                "verify you are human",
                "security challenge",
                "cloudflare",
            )
            if any(h in body for h in hints):
                return True
        return False

    async def fill_signup_form_once(
        self,
        session_id: str,
        url: str,
        fields: dict[str, str],
        *,
        plan_id: str | None,
        submit: bool = True,
    ) -> dict[str, Any]:
        site = self._site_key(url)
        scope = self._attempt_scope_key(plan_id, session_id)
        key = (scope, site)
        attempts = self._signup_attempts.get(key, 0)
        if attempts >= 1:
            return {
                "url": url,
                "site": site,
                "submitted": False,
                "attempts": attempts,
                "handoff_required": True,
                "handoff_reason": (
                    "Max automated signup attempts reached for this site. "
                    "Take over manually in Live View to continue."
                ),
            }
        self._signup_attempts[key] = attempts + 1

        result = await self.fill_contact_form(
            session_id=session_id,
            url=url,
            fields=fields,
            submit=submit,
        )
        result["site"] = site
        result["attempts"] = self._signup_attempts[key]
        result["handoff_required"] = False
        result["handoff_reason"] = ""
        page = self._pages.get(session_id)
        if page is not None and await self._captcha_present(page):
            result["handoff_required"] = True
            result["handoff_reason"] = (
                "CAPTCHA or human verification detected. "
                "Please take over manually in Live View."
            )
        return result

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

    async def _click(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        selector = str(args["selector"])
        timeout_ms = int(args.get("timeout_ms", 15_000))
        try:
            result = await manager.click(
                session_id, selector, timeout_ms=timeout_ms,
            )
        except KeyError:
            return ToolOutcome(content=f"no such session: {session_id}", is_error=True)
        except Exception as e:
            return ToolOutcome(
                content=f"click failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"clicked {selector}\n"
                f"→ {result['url']}\n"
                f"title: {result['title']}"
            ),
            data=result,
        )

    async def _type(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        selector = str(args["selector"])
        text = str(args.get("text", ""))
        clear_first = bool(args.get("clear_first", True))
        press_enter = bool(args.get("press_enter", False))
        timeout_ms = int(args.get("timeout_ms", 15_000))
        try:
            result = await manager.type_text(
                session_id,
                selector,
                text,
                clear_first=clear_first,
                press_enter=press_enter,
                timeout_ms=timeout_ms,
            )
        except KeyError:
            return ToolOutcome(content=f"no such session: {session_id}", is_error=True)
        except Exception as e:
            return ToolOutcome(
                content=f"type failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"typed into {selector} ({result['typed_chars']} chars)\n"
                f"→ {result['url']}\n"
                f"title: {result['title']}"
            ),
            data=result,
        )

    async def _wait_for(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        selector = args.get("selector")
        state = str(args.get("state", "visible"))
        url_contains = args.get("url_contains")
        timeout_ms = int(args.get("timeout_ms", 15_000))
        try:
            result = await manager.wait_for(
                session_id,
                selector=str(selector) if selector is not None else None,
                state=state,
                url_contains=(
                    str(url_contains) if url_contains is not None else None
                ),
                timeout_ms=timeout_ms,
            )
        except KeyError:
            return ToolOutcome(content=f"no such session: {session_id}", is_error=True)
        except Exception as e:
            return ToolOutcome(
                content=f"wait failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"wait satisfied\n"
                f"→ {result['url']}\n"
                f"title: {result['title']}"
            ),
            data=result,
        )

    async def _eval_js(args: dict, ctx: ToolContext) -> ToolOutcome:
        session_id = str(args["session_id"])
        script = str(args["script"])
        arg = args.get("arg")
        try:
            result = await manager.eval_js(session_id, script, arg=arg)
        except KeyError:
            return ToolOutcome(content=f"no such session: {session_id}", is_error=True)
        except Exception as e:
            return ToolOutcome(
                content=f"eval failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=(
                f"eval_js complete\n"
                f"→ {result['url']}\n"
                f"title: {result['title']}"
            ),
            data=result,
        )

    async def _fill_account(args: dict, ctx: ToolContext) -> ToolOutcome:
        """Account-signup flavor of form fill.

        Same browser automation surface as ``browser_form_fill`` but a
        separate tool name/risk so policy can distinguish operational
        service onboarding from person-targeted outreach.
        """
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
                    "browser_form_fill_account requires a non-empty "
                    "'fields' object (e.g. {\"email\": \"...\", "
                    "\"password\": \"...\"})."
                ),
                is_error=True,
            )
        fields_str = {str(k): str(v) for k, v in fields.items()}
        submit = bool(args.get("submit", True))
        try:
            result = await manager.fill_signup_form_once(
                session_id, url, fields_str, submit=submit,
                plan_id=ctx.plan_id,
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
        if result.get("handoff_required"):
            return ToolOutcome(
                content=result.get("handoff_reason") or "manual handoff required",
                data=result,
                is_error=True,
            )
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
    click_tool = Tool(
        name="browser_click",
        description=(
            "Click an element in an open browser session by CSS selector. "
            "Use for JS-heavy flows where form heuristics are not enough."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 1000},
            },
            "required": ["session_id", "selector"],
        },
        risk=RiskClass.BROWSE,
        handler=_click,
    )
    type_tool = Tool(
        name="browser_type",
        description=(
            "Type into a specific element in an open browser session by "
            "CSS selector."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "text": {"type": "string"},
                "clear_first": {"type": "boolean"},
                "press_enter": {"type": "boolean"},
                "timeout_ms": {"type": "integer", "minimum": 1000},
            },
            "required": ["session_id", "selector", "text"],
        },
        risk=RiskClass.BROWSE,
        handler=_type,
    )
    wait_for_tool = Tool(
        name="browser_wait_for",
        description=(
            "Wait for a selector state and/or URL fragment in an open "
            "browser session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "selector": {"type": "string"},
                "state": {
                    "type": "string",
                    "enum": ["attached", "detached", "visible", "hidden"],
                },
                "url_contains": {"type": "string"},
                "timeout_ms": {"type": "integer", "minimum": 1000},
            },
            "required": ["session_id"],
        },
        risk=RiskClass.BROWSE,
        handler=_wait_for,
    )
    eval_tool = Tool(
        name="browser_eval_js",
        description=(
            "Evaluate JavaScript inside the current page context of an "
            "open browser session."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "script": {"type": "string"},
                "arg": {},
            },
            "required": ["session_id", "script"],
        },
        risk=RiskClass.BROWSE,
        handler=_eval_js,
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
    fill_account_tool = Tool(
        name="browser_form_fill_account",
        description=(
            "Navigate a Browserbase session to a service signup/settings "
            "form, match inputs/textareas against supplied fields, fill "
            "them, and optionally submit. Use this for account creation "
            "or operational dashboard onboarding using PILK's own identity "
            "(not person-targeted outreach)."
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
        risk=RiskClass.NET_WRITE,
        handler=_fill_account,
    )
    return [
        open_tool,
        navigate_tool,
        click_tool,
        type_tool,
        wait_for_tool,
        eval_tool,
        close_tool,
        fill_tool,
        fill_account_tool,
    ]
