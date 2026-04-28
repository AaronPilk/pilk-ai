"""Self-capabilities refresher.

Reads:
  - git HEAD + recent commit messages from the source tree
  - registered tool names from the live ``ToolRegistry``
  - registered route paths from FastAPI
  - registered workflow names from app.state
  - safe-default flags (intelligence daemon, alert settings)

Sends one Anthropic call asking for a plain-English self-summary
written from PILK's POV ("I can do X, here's how I'm wired today,
here's what's OFF by default"). The result lands at
``standing-instructions/pilk-capabilities.md`` in the brain vault
with YAML frontmatter recording the commit hash + timestamp so
the next boot can short-circuit when nothing changed.

The refresher NEVER auto-fires anything else. It does not run
workflows, send Telegram, or call other tools. Failures log a
warning and leave the existing note in place.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from core.brain import Vault
from core.config import Settings
from core.logging import get_logger

log = get_logger("pilkd.self_capabilities")

# Where the note lives. PILK reads standing-instructions every
# chat, so this path puts the summary directly into context.
NOTE_RELATIVE_PATH = "standing-instructions/pilk-capabilities.md"

# Use the same model PILK's planner uses by default — cheap,
# capable, already configured. Override via Settings if needed.
DEFAULT_MODEL = "claude-haiku-4-5"

# How many recent commits to pass to the LLM. Enough for "what
# changed lately" without bloating the prompt.
COMMIT_LOG_LIMIT = 30

# Token budget for the summary itself. The note tends to land
# under 1500 output tokens; keep a bit of headroom.
MAX_OUTPUT_TOKENS = 2000


@dataclass
class RefreshOutcome:
    """Returned to whatever called ``refresh_if_stale``.

    ``status`` is one of:
      - ``up_to_date``         — HEAD matches recorded hash, no work
      - ``refreshed``          — note was rewritten
      - ``skipped_no_anthropic`` — no API key configured
      - ``skipped_disabled``   — operator turned the feature off
      - ``failed``             — LLM call or write failed; existing
                                 note preserved (if any)
    """

    status: str
    head_commit: str | None = None
    previous_commit: str | None = None
    note_path: str | None = None
    cost_usd_estimate: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class SelfCapabilitiesRefresher:
    """Build + persist PILK's self-capabilities note.

    Stateless — caller passes the live registry / app / settings on
    each call. That keeps the boot wiring explicit and the test
    surface tiny.
    """

    def __init__(
        self,
        *,
        vault: Vault,
        repo_root: Path,
        anthropic_client: anthropic.AsyncAnthropic | None,
        model: str = DEFAULT_MODEL,
    ) -> None:
        self._vault = vault
        self._repo_root = Path(repo_root)
        self._client = anthropic_client
        self._model = model

    async def refresh_if_stale(
        self,
        *,
        registry: Any,
        settings: Settings,
        workflows: dict[str, Any] | None = None,
        alert_settings_snapshot: Any = None,
        force: bool = False,
    ) -> RefreshOutcome:
        """Check git HEAD, refresh the brain note if it's drifted.

        ``force=True`` skips the staleness check and always rewrites.
        Useful for the manual refresh route.
        """
        if self._client is None:
            log.info("self_capabilities_skipped_no_anthropic_client")
            return RefreshOutcome(status="skipped_no_anthropic")
        head = self._read_git_head()
        previous = self._read_recorded_commit()
        if not force and head and previous and head == previous:
            log.info(
                "self_capabilities_up_to_date",
                head=head,
            )
            return RefreshOutcome(
                status="up_to_date",
                head_commit=head,
                previous_commit=previous,
                note_path=NOTE_RELATIVE_PATH,
            )
        commit_log = self._read_recent_commits()
        tool_inventory = self._summarise_tools(registry)
        workflow_inventory = self._summarise_workflows(workflows or {})
        defaults_snapshot = self._summarise_defaults(
            settings=settings,
            alert_settings=alert_settings_snapshot,
        )
        try:
            body = await self._llm_summarise(
                head=head,
                previous=previous,
                commit_log=commit_log,
                tool_inventory=tool_inventory,
                workflow_inventory=workflow_inventory,
                defaults_snapshot=defaults_snapshot,
            )
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "self_capabilities_llm_failed", error=str(e),
            )
            return RefreshOutcome(
                status="failed",
                head_commit=head,
                previous_commit=previous,
                error=str(e),
            )
        try:
            note_text = self._compose_note(
                head=head,
                previous=previous,
                body=body,
                tool_count=len(tool_inventory),
                workflow_count=len(workflow_inventory),
            )
            self._vault.write(NOTE_RELATIVE_PATH, note_text)
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "self_capabilities_write_failed", error=str(e),
            )
            return RefreshOutcome(
                status="failed",
                head_commit=head,
                previous_commit=previous,
                error=str(e),
            )
        log.info(
            "self_capabilities_refreshed",
            head=head,
            previous=previous,
            tools=len(tool_inventory),
            workflows=len(workflow_inventory),
        )
        return RefreshOutcome(
            status="refreshed",
            head_commit=head,
            previous_commit=previous,
            note_path=NOTE_RELATIVE_PATH,
            metadata={
                "tools": len(tool_inventory),
                "workflows": len(workflow_inventory),
            },
        )

    # ── internal helpers ─────────────────────────────────────────

    def _read_git_head(self) -> str | None:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=self._repo_root,
                stderr=subprocess.DEVNULL,
            )
            return out.decode("utf-8").strip()
        except (OSError, subprocess.SubprocessError):
            return None

    def _read_recent_commits(self) -> list[str]:
        try:
            out = subprocess.check_output(
                [
                    "git", "log",
                    f"-{COMMIT_LOG_LIMIT}",
                    "--pretty=format:%h %s",
                ],
                cwd=self._repo_root,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        return [
            ln for ln in out.decode("utf-8").splitlines() if ln.strip()
        ]

    def _read_recorded_commit(self) -> str | None:
        path = self._vault.resolve(NOTE_RELATIVE_PATH)
        if not path.exists():
            return None
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        m = re.search(
            r"^last_refreshed_commit:\s*([0-9a-f]+)\s*$",
            text,
            flags=re.MULTILINE,
        )
        return m.group(1) if m else None

    @staticmethod
    def _summarise_tools(registry: Any) -> list[dict[str, str]]:
        if registry is None:
            return []
        out: list[dict[str, str]] = []
        try:
            tools = registry.all()
        except Exception:
            return []
        for t in tools:
            risk = getattr(t.risk, "value", str(t.risk))
            out.append(
                {
                    "name": t.name,
                    "risk": risk,
                    "description": (
                        t.description or ""
                    )[:240].replace("\n", " "),
                }
            )
        return out

    @staticmethod
    def _summarise_workflows(
        workflows: dict[str, Any],
    ) -> list[dict[str, str]]:
        out: list[dict[str, str]] = []
        for name, wf in (workflows or {}).items():
            description = getattr(wf, "description", "") or ""
            trigger = getattr(wf, "trigger", "operator") or "operator"
            out.append(
                {
                    "name": name,
                    "trigger": trigger,
                    "description": description[:240].replace("\n", " "),
                }
            )
        return out

    @staticmethod
    def _summarise_defaults(
        *,
        settings: Settings,
        alert_settings: Any,
    ) -> dict[str, Any]:
        out: dict[str, Any] = {
            "intelligence_daemon_enabled": bool(
                getattr(settings, "intelligence_daemon_enabled", False)
            ),
            "computer_control_enabled": (
                str(
                    getattr(settings, "computer_control_enabled", "") or ""
                ).strip().lower()
                == "true"
            ),
            "computer_control_daily_limit": int(
                getattr(settings, "computer_control_daily_limit", 0) or 0
            ),
        }
        if alert_settings is not None:
            for k in (
                "telegram_enabled",
                "daily_brief_scheduled",
                "weekly_brief_scheduled",
                "digest_only",
                "max_per_day",
                "min_score",
            ):
                v = getattr(alert_settings, k, None)
                if v is not None:
                    out[f"alerts_{k}"] = v
        return out

    async def _llm_summarise(
        self,
        *,
        head: str | None,
        previous: str | None,
        commit_log: list[str],
        tool_inventory: list[dict[str, str]],
        workflow_inventory: list[dict[str, str]],
        defaults_snapshot: dict[str, Any],
    ) -> str:
        prompt = _build_prompt(
            head=head,
            previous=previous,
            commit_log=commit_log,
            tool_inventory=tool_inventory,
            workflow_inventory=workflow_inventory,
            defaults_snapshot=defaults_snapshot,
        )
        assert self._client is not None  # type-narrow for mypy
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=MAX_OUTPUT_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        # Anthropic returns blocks; concat the text.
        parts: list[str] = []
        for block in getattr(resp, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    def _compose_note(
        *,
        head: str | None,
        previous: str | None,
        body: str,
        tool_count: int,
        workflow_count: int,
    ) -> str:
        now = datetime.now(UTC).isoformat()
        frontmatter_lines = [
            "---",
            "kind: pilk_self_capabilities",
            f"last_refreshed_commit: {head or 'unknown'}",
            f"previous_refreshed_commit: {previous or 'none'}",
            f"last_refreshed_at: {now}",
            f"tool_count: {tool_count}",
            f"workflow_count: {workflow_count}",
            "auto_refresh: true",
            "---",
            "",
        ]
        return "\n".join(frontmatter_lines) + body.strip() + "\n"


def _build_prompt(
    *,
    head: str | None,
    previous: str | None,
    commit_log: list[str],
    tool_inventory: list[dict[str, str]],
    workflow_inventory: list[dict[str, str]],
    defaults_snapshot: dict[str, Any],
) -> str:
    """Assemble the Anthropic prompt. Plain text, no caching headers
    — this fires once per real deploy so cache hits are rare."""
    parts: list[str] = []
    parts.append(
        "You are PILK summarizing your own current capabilities for "
        "your own reference. Aaron (the operator) just deployed new "
        "code and wants you to know what's actually live right now.\n\n"
        "Write a markdown note (no surrounding code fences). Keep it "
        "under ~700 words. Use these section headings:\n"
        "  ## What I can do today\n"
        "  ## What's new since the last summary\n"
        "  ## What's OFF by default\n"
        "  ## How I should think about using these\n\n"
        "Style: plain English from your POV (\"I can…\"). No file "
        "paths, no commit hashes, no jargon. Talk features. Do NOT "
        "include a top-level # heading — the file already has "
        "frontmatter."
    )
    parts.append(f"\nCURRENT COMMIT: {head or 'unknown'}")
    if previous:
        parts.append(f"PREVIOUS SUMMARIZED COMMIT: {previous}")
    if commit_log:
        parts.append("\nRECENT COMMITS (newest first):")
        for ln in commit_log:
            parts.append(f"  {ln}")
    if tool_inventory:
        parts.append(
            f"\nTOOLS I CAN CALL ({len(tool_inventory)} total). "
            "Group by purpose, don't list all alphabetically:"
        )
        # Truncate to keep prompt tight; the LLM only needs a
        # representative sample to group by purpose.
        for t in tool_inventory[:200]:
            parts.append(
                f"  - {t['name']} [{t['risk']}] {t['description']}"
            )
        if len(tool_inventory) > 200:
            parts.append(
                f"  …({len(tool_inventory) - 200} more)"
            )
    if workflow_inventory:
        parts.append("\nWORKFLOWS REGISTERED:")
        for wf in workflow_inventory:
            parts.append(
                f"  - {wf['name']} (trigger: {wf['trigger']}) "
                f"{wf['description']}"
            )
    if defaults_snapshot:
        parts.append("\nCURRENT DEFAULTS / SAFETY POSTURE:")
        for k, v in defaults_snapshot.items():
            parts.append(f"  - {k}: {v}")
    parts.append(
        "\nWrite the note now. End with a one-line reminder: "
        "\"Updated automatically when new code ships.\""
    )
    return "\n".join(parts)


__all__ = [
    "NOTE_RELATIVE_PATH",
    "RefreshOutcome",
    "SelfCapabilitiesRefresher",
]
