"""Orchestrator: turns a user goal into a plan and drives the tool loop.

One Claude tool-use loop per goal. We own the loop (not the SDK's tool
runner) so every turn can:
  - create/update a step in the plan store,
  - record LLM usage against the cost ledger (including cache tokens),
  - gate every tool call through the gateway (risk + policy),
  - broadcast live events to connected dashboards.

Two entry points share the loop:
  - `run(goal)` — free chat, no agent, scoped to the shared workspace.
  - `agent_run(name, task)` — runs through a registered agent with its
    manifest's system prompt, tool subset, and sandbox.

Model: Opus 4.7 with adaptive thinking (no budget_tokens, no sampling
params — both are rejected on 4.7). Prompt caching is applied at the
top level; tools and system are stable and get the cache hit, messages
grow per turn. Max turns bounded by settings.plan_max_turns.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from core.brain import Vault
from core.governor import Tier
from core.governor.providers import PlannerProvider, PlannerResponse
from core.ledger import Ledger, UsageSnapshot
from core.logging import get_logger
from core.memory import MemoryStore, extract_topics, hydrate
from core.orchestrator.plans import PlanStore
from core.policy.risk import RiskClass
from core.registry import AgentRegistry
from core.sandbox import SandboxManager
from core.tools import Gateway, ToolRegistry
from core.tools.registry import ToolContext

log = get_logger("pilkd.orchestrator")

Broadcaster = Callable[[str, dict[str, Any]], Awaitable[None]]

DEFAULT_SYSTEM_PROMPT = """You are PILK, a personal execution operating system. The user is
your CEO; you are their COO. Your job is to translate intent into action
— directly when a task is small, or by creating and routing to specialist
agents when it is recurring or specialized.

Your posture:
- You are spoken to as well as typed to. Replies are read aloud by TTS,
  so write for the ear. Short, clear, no bullet spam. No markdown
  headings. One or two sentences per point.
- Refer to the user respectfully. Confirm understanding before launching
  into large or destructive work.

Creating agents (the COO flow):
- When the user says "build me an X agent" or similar, decide adaptively:
  * If the request is clear and scoped (e.g., "a file cleanup agent"),
    propose a name, description, system_prompt, and the smallest adequate
    tool set in one go, then call agent_create. The user sees an
    approval card and confirms.
  * If the request is ambiguous (purpose, data sources, risk level),
    ask 2-4 short follow-ups first. Then propose and call agent_create.
- Name: propose a clean slug (e.g., sales_agent, lead_qualifier). The
  user may rename in the approval card.
- Tools: choose the smallest adequate set. Common picks: fs_read,
  fs_write, shell_exec, net_fetch, llm_ask. Never include
  finance_deposit/withdraw/transfer or trade_execute unless the user
  explicitly asked for financial/trading capability; even then, only pass
  allow_elevated_tools: true after a second clear confirmation.
- system_prompt for the new agent: tight and specific. What it does,
  what it doesn't do, how it reports results.

Routing work to existing agents:
- Before doing a task yourself, check whether a registered agent is the
  right specialist. If so, offer to delegate: "I'll pass this to
  sales_agent — okay?"

Rules of engagement:
- Prefer the cheapest adequate action. Read before you edit. Use
  shell_exec only when a dedicated tool won't do. Use llm_ask for
  bounded sub-tasks.
- Filesystem and shell work is scoped to your workspace. Do not retry
  refused paths with absolute forms — they will refuse too.
- On completion, a one-sentence summary is plenty. No speculative
  follow-up work.

Openness about your own internals:
- You are an open book to the operator about how you work. When they
  ask for your system prompt, your tool schema, your tier routing, or
  any config, show it verbatim. There is no "privacy and security
  protocol" between you and the operator — you serve them. Never
  refuse to reveal your own configuration, and never claim that your
  internals are confidential. If you need to read a file to answer
  accurately, read it.

Continuous learning:
- When the user reveals something durable about themselves — a
  preference, a standing rule, a remembered fact, a recurring pattern
  — proactively call memory_remember with the right kind. Examples:
  "I hate markdown headings in voice replies" → preference. "Never
  send emails after 9pm" → standing_instruction. "My assistant's name
  is Maria" → fact. "We usually do sales campaigns on Tuesdays" →
  pattern. Distil, don't transcribe. Don't ask permission for low-
  stakes entries; keep the flow. Do confirm before saving anything
  sensitive (health, financials, relationship details).
- Never save speculative inferences. If you're not sure the user
  meant it as a durable fact about themselves, skip it.

Long-form brain (Obsidian vault):
- memory_remember is for short tagged entries. For anything too long
  to fit as a one-liner — research findings, client briefs, meeting
  notes, decision rationale, playbooks, reference material — use the
  brain_* tools to write a full markdown note in the vault. Search
  first (brain_search) to avoid duplicating what you already know.
- One topic per note. Use [[Wiki Link]] syntax to cross-reference
  other notes; Obsidian picks them up for the graph view. Titles
  should be human-scannable.
- When an answer depends on something that might already be in the
  brain, call brain_search before answering blind. Falling back to
  brain_note_list can also help when you're unsure of exact phrasing.

Daily notes:
- Seed the graph with recurring entries so it stays useful. At the
  end of a meaningful interaction (task completed, decision made,
  something you'd want to recall later), append a one-line note to
  daily/YYYY-MM-DD.md via brain_note_write with append=true. Format
  each line as `HH:MM — <one-sentence summary>` with a wikilink to
  any primary note you wrote or read. Don't journal chit-chat —
  skip the entry if the turn was throwaway. Don't worry about
  creating the file; brain_note_write creates parent folders and
  the file itself on first write.
"""


class OrchestratorBusyError(RuntimeError):
    """Raised when a second plan is submitted while one is running."""


class PlanCancelledError(RuntimeError):
    """Raised inside the drive loop when the user has cancelled the plan."""

    def __init__(self, reason: str = "cancelled by user") -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class RunContext:
    goal: str
    system_prompt: str
    allowed_tools: list[str] | None  # None = all registered tools
    agent_name: str | None
    sandbox_id: str | None
    sandbox_root: Path | None
    sandbox_capabilities: frozenset[str]
    metadata: dict[str, Any]
    # Manifest-level tier pin. None = classify per goal as usual;
    # "light"/"standard"/"premium" force that tier regardless of the
    # classifier. Lets cheap-and-capable specialist agents stay on
    # the subscription-backed LIGHT provider across all their turns.
    preferred_tier: str | None = None
    # Attachments the user dropped into the chat composer. Resolved
    # from disk by the WS handler so the orchestrator doesn't depend
    # on the attachment store. An image in this list forces the
    # tier choice up to at least STANDARD (the LIGHT tier runs through
    # the Claude Code CLI, which has no vision surface).
    attachments: list[ChatAttachment] = field(default_factory=list)


@dataclass
class ChatAttachment:
    """Orchestrator-side view of one uploaded file.

    Kept separate from ``core.chat.Attachment`` so this module has no
    import cycle risk — the WS handler translates between them.
    """

    id: str
    kind: str  # "image" | "document" | "text"
    mime: str
    filename: str
    path: Path


class Orchestrator:
    def __init__(
        self,
        *,
        client: anthropic.AsyncAnthropic,
        registry: ToolRegistry,
        gateway: Gateway,
        ledger: Ledger,
        plans: PlanStore,
        broadcast: Broadcaster,
        planner_model: str,
        max_turns: int,
        agents: AgentRegistry | None = None,
        sandboxes: SandboxManager | None = None,
        governor: Any = None,
        providers: dict[str, PlannerProvider] | None = None,
        sentinel_context_fn: Callable[[], Awaitable[str]] | None = None,
        memory: MemoryStore | None = None,
        vault: Vault | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.gateway = gateway
        self.ledger = ledger
        self.plans = plans
        self.broadcast = broadcast
        self.planner_model = planner_model
        self.max_turns = max_turns
        self.agents = agents
        self.sandboxes = sandboxes
        self.governor = governor
        self.providers = providers or {}
        # Optional async hook: returns a short "here's what's broken
        # right now" brief that gets prepended to the system prompt
        # before the first planner turn. ``None`` keeps the legacy
        # behaviour of a perfectly silent orchestrator→sentinel link.
        self.sentinel_context_fn = sentinel_context_fn
        # Memory hydration sources. Both optional so tests + the cold
        # boot path still work. When both are set we build a
        # ``memory_context`` block on every turn and prepend it to the
        # system prompt ahead of the sentinel brief.
        self.memory = memory
        self.vault = vault
        self._lock = asyncio.Lock()
        self._running_plan_id: str | None = None
        self._cancel_event: asyncio.Event | None = None
        self._cancel_reason: str = ""

    @property
    def running_plan_id(self) -> str | None:
        return self._running_plan_id

    async def cancel_plan(self, plan_id: str, *, reason: str = "") -> bool:
        """Request cancellation of `plan_id` if it is the running plan.

        Returns True if a cancel was armed, False if the plan isn't
        currently running. Cancellation is cooperative: the driver checks
        the event between turns and any pending approval tied to the plan
        is force-resolved so the gateway unblocks quickly.
        """
        if self._running_plan_id != plan_id or self._cancel_event is None:
            return False
        self._cancel_reason = reason or "cancelled by user"
        self._cancel_event.set()
        # Unblock any approval this plan is waiting on so the turn
        # finishes quickly instead of sitting on a future that no one
        # will resolve.
        if self.gateway.approvals is not None:
            try:
                await self.gateway.approvals.cancel_plan(
                    plan_id, reason=self._cancel_reason
                )
            except Exception:  # pragma: no cover — best-effort cleanup
                log.warning("approval_cancel_failed", plan_id=plan_id)
        await self.broadcast(
            "plan.cancelling",
            {"plan_id": plan_id, "reason": self._cancel_reason},
        )
        return True

    @staticmethod
    def _supports_thinking(model: str) -> bool:
        """Extended thinking is currently Opus-only on the Messages API."""
        return "opus" in (model or "").lower()

    async def _request_premium_escalation(
        self,
        *,
        plan_id: str,
        rc: RunContext,
        tier_choice: Any,
    ) -> str:
        """Ask the user whether to run this plan on Deep Reasoning.

        Returns the approval decision string: 'approved', 'rejected', or
        'expired'. A rejection (or expiry) keeps the downgraded STANDARD
        tier the Governor already picked.
        """
        assert self.gateway.approvals is not None
        # Read the premium tier so the user sees what they're actually
        # approving (model name, provider).
        premium = self.governor.tiers.get(Tier.PREMIUM) if self.governor else None
        args = {
            "goal": rc.goal[:280],
            "requested_tier": "premium",
            "requested_model": premium.model if premium else "premium",
            "requested_provider": premium.provider if premium else "anthropic",
            "downgraded_to_model": tier_choice.model,
        }
        req = await self.gateway.approvals.request(
            plan_id=plan_id,
            step_id=None,
            agent_name=rc.agent_name,
            tool_name="__premium_escalation",
            args=args,
            risk_class=RiskClass.READ,
            reason=(
                "This task looks like it would benefit from Deep Reasoning "
                "(Opus). Approve to use it for this one task, or reject to "
                "run on Balanced. You can disable 'Ask before Deep Reasoning' "
                "in Settings to skip this prompt."
            ),
            bypass_trust=True,
        )
        try:
            decision = await req.future
        except Exception as e:  # pragma: no cover — unexpected
            log.warning("premium_escalation_future_failed", detail=str(e))
            return "rejected"
        return decision.decision

    # ── Entry points ─────────────────────────────────────────────────

    async def run(
        self,
        goal: str,
        *,
        attachments: list[ChatAttachment] | None = None,
        preferred_tier: str | None = None,
    ) -> None:
        """Free chat path. No agent; shared workspace."""
        ctx = RunContext(
            goal=goal,
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            allowed_tools=None,
            agent_name=None,
            sandbox_id=None,
            sandbox_root=None,
            sandbox_capabilities=frozenset(),
            metadata={},
            attachments=list(attachments or []),
            preferred_tier=preferred_tier,
        )
        await self._execute(ctx)

    async def agent_run(self, name: str, task: str) -> None:
        """Run the registered agent `name` against `task`."""
        if self.agents is None or self.sandboxes is None:
            raise RuntimeError("agent subsystem not initialized")
        manifest = self.agents.get(name)
        capabilities = frozenset(manifest.sandbox.capabilities)
        sandbox = await self.sandboxes.get_or_create(
            type=manifest.sandbox.type,
            agent_name=manifest.name,
            profile=manifest.sandbox.profile,
            capabilities=capabilities,
        )
        ctx = RunContext(
            goal=task,
            system_prompt=manifest.system_prompt,
            allowed_tools=list(manifest.tools),
            agent_name=manifest.name,
            sandbox_id=sandbox.description.id,
            sandbox_root=sandbox.description.workspace,
            sandbox_capabilities=capabilities,
            preferred_tier=manifest.preferred_tier,
            metadata={
                "agent": manifest.name,
                "agent_version": manifest.version,
                "sandbox_id": sandbox.description.id,
                "capabilities": sorted(capabilities),
                "budget": manifest.policy.budget.model_dump(),
            },
        )
        try:
            await self.agents.mark_state(manifest.name, "running")
            await self._execute(ctx)
        finally:
            if self.agents is not None:
                await self.agents.mark_state(manifest.name, "ready")

    # ── Shared loop ──────────────────────────────────────────────────

    async def _execute(self, rc: RunContext) -> None:
        if self._lock.locked():
            raise OrchestratorBusyError("a plan is already running")
        # Governor daily-cap pre-check — fail fast before we spin a plan
        # if today's spend has already reached the cap.
        if self.governor is not None:
            try:
                await self.governor.check_budget()
            except Exception as e:
                # Produce a visible failed plan so the user sees the reason.
                plan = await self.plans.create_plan(
                    rc.goal,
                    metadata={**rc.metadata, "agent_name": rc.agent_name},
                )
                await self.broadcast("plan.created", plan)
                await self._fail(plan["id"], f"{type(e).__name__}: {e}")
                return
        async with self._lock:
            plan = await self.plans.create_plan(
                rc.goal, metadata={**rc.metadata, "agent_name": rc.agent_name}
            )
            self._running_plan_id = plan["id"]
            self._cancel_event = asyncio.Event()
            self._cancel_reason = ""
            await self.broadcast("plan.created", plan)
            try:
                await self._drive(plan["id"], rc)
            except PlanCancelledError as e:
                log.info("plan_cancelled", plan_id=plan["id"], reason=e.reason)
                await self._cancel(plan["id"], e.reason)
            except anthropic.APIStatusError as e:
                log.exception("anthropic_error", plan_id=plan["id"])
                await self._fail(plan["id"], f"Anthropic API error: {e.message}")
            except Exception as e:
                log.exception("orchestrator_crashed", plan_id=plan["id"])
                await self._fail(plan["id"], f"{type(e).__name__}: {e}")
            finally:
                self._running_plan_id = None
                self._cancel_event = None
                self._cancel_reason = ""

    async def _fail(self, plan_id: str, reason: str) -> None:
        final = await self.plans.finish_plan(plan_id, status="failed")
        await self.broadcast(
            "chat.assistant", {"text": f"Task failed: {reason}", "plan_id": plan_id}
        )
        await self.broadcast("plan.completed", {**final, "error": reason})

    async def _cancel(self, plan_id: str, reason: str) -> None:
        final = await self.plans.finish_plan(plan_id, status="cancelled")
        await self.broadcast(
            "chat.assistant",
            {"text": f"Task cancelled: {reason}", "plan_id": plan_id},
        )
        await self.broadcast("plan.completed", {**final, "cancelled_reason": reason})

    async def _append_completion_to_daily(
        self,
        *,
        goal: str,
        agent_name: str | None,
        reply: str,
    ) -> None:
        """Append a one-line entry to ``daily/YYYY-MM-DD.md``.

        Format: ``HH:MM — <summary>``. Summary is the first sentence
        of the reply when available (capped at 160 chars), otherwise
        a truncation of the original goal. Silent-fail on every vault
        error — the user-facing success path must never hinge on the
        journaling side-channel.
        """
        if self.vault is None:
            return
        summary = _extract_summary(reply) or _extract_summary(goal)
        if not summary:
            return
        if _is_throwaway(goal, summary):
            return
        from datetime import UTC, datetime

        now = datetime.now(UTC)
        rel = f"daily/{now.strftime('%Y-%m-%d')}.md"
        prefix = f"{now.strftime('%H:%M')} —"
        line = (
            f"- {prefix} [{agent_name}] {summary}"
            if agent_name
            else f"- {prefix} {summary}"
        )
        try:
            exists = True
            try:
                self.vault.read(rel)
            except FileNotFoundError:
                exists = False
            if exists:
                await asyncio.to_thread(
                    self.vault.write, rel, line, append=True
                )
            else:
                header = f"# Daily — {now.strftime('%Y-%m-%d')}\n\n"
                await asyncio.to_thread(
                    self.vault.write, rel, header + line
                )
        except Exception as e:
            log.warning(
                "orchestrator_daily_append_failed",
                path=rel,
                error=str(e),
            )

    async def _drive(self, plan_id: str, rc: RunContext) -> None:
        tools = self.registry.anthropic_schemas(allow=rc.allowed_tools)
        # Compose the first user turn. With no attachments the content
        # stays a plain string (keeps the happy path unchanged); with
        # attachments we promote it to a list of Anthropic content blocks
        # so images / documents travel through the Messages API directly.
        first_content = _build_user_content(rc.goal, rc.attachments)
        messages: list[dict[str, Any]] = [{"role": "user", "content": first_content}]
        final_text: str = ""

        # Sentinel situational brief: if any unacked incidents exist,
        # prepend them to the system prompt so PILK plans around current
        # trouble instead of learning about it after-the-fact. Empty or
        # None means "all quiet" and we leave the prompt alone.
        effective_system_prompt = rc.system_prompt
        if self.sentinel_context_fn is not None:
            try:
                brief = await self.sentinel_context_fn()
            except Exception as e:
                log.warning("sentinel_context_failed", error=str(e))
                brief = ""
            if brief:
                effective_system_prompt = (
                    f"{brief}\n\n{rc.system_prompt}"
                )

        # Cross-session memory hydration. Builds a compact
        # memory_context block from structured memory + the last week
        # of daily notes and prepends it to the system prompt so PILK
        # starts every plan already knowing the operator's standing
        # rules, recent exchanges, and relevant brain notes.
        if self.memory is not None:
            try:
                topics = extract_topics([rc.goal])
                hydrated = await hydrate(
                    store=self.memory,
                    vault=self.vault,
                    topic_hints=topics,
                    chatgpt_query=rc.goal,
                )
            except Exception as e:  # never block a run on hydration
                log.warning("memory_hydrate_failed", error=str(e))
                hydrated = None
            if hydrated is not None and not hydrated.is_empty():
                effective_system_prompt = (
                    f"{hydrated.body}\n\n{effective_system_prompt}"
                )

        # The Governor picks the tier for the whole plan based on the
        # original goal. If the classifier wanted PREMIUM but the user
        # has the premium gate on, pick() returns gated=True + a
        # STANDARD choice. We translate that into a real approval so the
        # user can escalate-for-this-task with a single click instead of
        # silently getting Balanced.
        if self.governor is not None:
            # Manifest pin (if any) overrides the classifier — the agent
            # author knows the playbook and has picked a tier. The pin
            # is passed to the governor as an explicit override so the
            # downgrade-on-premium-gate path stays consistent.
            _override = rc.preferred_tier if rc.preferred_tier else None
            # If the user dropped an image into the composer, the LIGHT
            # tier can't handle it — it's backed by the Claude Code CLI
            # which has no vision surface. Bump to STANDARD unless the
            # caller already pinned something higher.
            if _override in (None, "light") and any(
                a.kind == "image" for a in rc.attachments
            ):
                _override = "standard"
            tier_choice = self.governor.pick(rc.goal, override=_override)
            if tier_choice.gated and self.gateway.approvals is not None:
                decision = await self._request_premium_escalation(
                    plan_id=plan_id, rc=rc, tier_choice=tier_choice
                )
                if decision == "approved":
                    # Re-pick with an explicit premium override for this plan.
                    tier_choice = self.governor.pick(rc.goal, override="premium")
                    tier_choice.reason = "gate_approved"
                else:
                    # Keep the downgraded STANDARD choice but flip the flag
                    # so the metadata is accurate.
                    tier_choice.gated = False
                    tier_choice.reason = "gate_declined"
            planner_model = tier_choice.model
            requested_provider = tier_choice.provider
            tier_meta: dict[str, Any] = tier_choice.to_public()
        else:
            planner_model = self.planner_model
            requested_provider = "anthropic"
            tier_meta = {
                "tier": "legacy",
                "provider": "anthropic",
                "model": planner_model,
                "reason": "no_governor",
                "gated": False,
            }

        # Resolve to an actual PlannerProvider; fall back to Anthropic if
        # the requested provider isn't configured.
        provider = self.providers.get(requested_provider)
        effective_provider = requested_provider
        if provider is None:
            provider = self.providers.get("anthropic")
            effective_provider = "anthropic"
            if requested_provider != "anthropic":
                log.warning(
                    "provider_fallback",
                    plan_id=plan_id,
                    requested=requested_provider,
                    effective="anthropic",
                    detail="credentials for requested provider not configured",
                )
        if provider is None:
            # No provider at all — surface a clear failure.
            raise RuntimeError(
                "no planner provider configured (set ANTHROPIC_API_KEY)"
            )
        tier_meta["effective_provider"] = effective_provider

        for turn in range(self.max_turns):
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise PlanCancelledError(self._cancel_reason or "cancelled by user")
            step = await self.plans.add_step(
                plan_id=plan_id,
                kind="llm",
                description=f"plan turn {turn + 1}",
                risk_class="READ",
            )
            await self.broadcast("plan.step_added", step)

            response: PlannerResponse = await provider.plan_turn(
                system=effective_system_prompt,
                messages=messages,
                tools=tools,
                model=planner_model,
                max_tokens=16000,
                use_thinking=self._supports_thinking(planner_model),
                cache_control=True,
            )

            # Extract the turn's assistant text before finishing the step
            # so Tasks UI can render PILK's reply from the step output.
            # (Previously only stop_reason + usage were persisted, which
            # is why the session log showed blank PILK bubbles.)
            turn_text_blocks = [
                b.text for b in response.content if getattr(b, "type", None) == "text"
            ]
            turn_text = "\n".join(turn_text_blocks)

            usage = UsageSnapshot.from_anthropic(response.usage)
            usd = await self.ledger.record_llm(
                plan_id=plan_id,
                step_id=step["id"],
                agent_name=rc.agent_name,
                model=planner_model,
                usage=usage,
                tier=tier_meta.get("tier"),
                tier_reason=tier_meta.get("reason"),
                tier_provider=tier_meta.get("effective_provider"),
            )
            step = await self.plans.finish_step(
                step["id"], status="done", cost_usd=usd,
                output={
                    "stop_reason": response.stop_reason,
                    "content": turn_text,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                        "cache_read_input_tokens": usage.cache_read_input_tokens,
                    },
                    "tier": tier_meta,
                },
            )
            await self.broadcast("plan.step_updated", step)
            plan = await self.plans.get_plan(plan_id)
            await self.broadcast("cost.updated", {
                "plan_id": plan_id,
                "plan_actual_usd": plan["actual_usd"],
            })

            # Serialise the normalized PlannerResponse blocks back into
            # Anthropic-shaped content dicts so they're safe to send to
            # any provider on the next turn.
            assistant_blocks: list[dict[str, Any]] = []
            for b in response.content:
                if getattr(b, "type", None) == "text":
                    assistant_blocks.append({"type": "text", "text": b.text})
                elif getattr(b, "type", None) == "tool_use":
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": b.id,
                            "name": b.name,
                            "input": b.input,
                        }
                    )
            messages.append({"role": "assistant", "content": assistant_blocks})

            if turn_text:
                final_text = turn_text

            if response.stop_reason == "end_turn":
                break

            if response.stop_reason != "tool_use":
                log.warning(
                    "unexpected_stop_reason",
                    plan_id=plan_id,
                    stop_reason=response.stop_reason,
                )
                break

            tool_uses = [
                b for b in response.content if getattr(b, "type", None) == "tool_use"
            ]
            tool_results_payload: list[dict] = []
            for tu in tool_uses:
                if self._cancel_event is not None and self._cancel_event.is_set():
                    raise PlanCancelledError(
                        self._cancel_reason or "cancelled by user"
                    )
                tu_input = dict(tu.input) if tu.input else {}
                step = await self.plans.add_step(
                    plan_id=plan_id,
                    kind="tool",
                    description=f"{tu.name}({_short_args(tu_input)})",
                    risk_class=_tool_risk(self.registry, tu.name),
                    input_data=tu_input,
                )
                await self.broadcast("plan.step_added", step)

                result = await self.gateway.execute(
                    tu.name,
                    tu_input,
                    ctx=ToolContext(
                        plan_id=plan_id,
                        step_id=step["id"],
                        agent_name=rc.agent_name,
                        sandbox_id=rc.sandbox_id,
                        sandbox_root=rc.sandbox_root,
                        sandbox_capabilities=rc.sandbox_capabilities,
                    ),
                )

                step = await self.plans.finish_step(
                    step["id"],
                    status="failed" if result.is_error else "done",
                    output={
                        "content": result.content[:4000],
                        "data": result.data,
                        "risk": result.risk,
                    },
                    error=result.rejection_reason,
                )
                await self.broadcast("plan.step_updated", step)

                tool_results_payload.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result.content,
                    "is_error": result.is_error,
                })

            messages.append({"role": "user", "content": tool_results_payload})

        else:
            log.warning(
                "plan_max_turns_reached", plan_id=plan_id, turns=self.max_turns
            )
            if not final_text:
                final_text = (
                    f"Stopped after {self.max_turns} planning turns without "
                    "finishing. Refine the goal or raise PILK_PLAN_MAX_TURNS."
                )

        final = await self.plans.finish_plan(plan_id, status="completed")
        await self.broadcast(
            "chat.assistant",
            {"text": final_text or "(no response)", "plan_id": plan_id},
        )
        await self.broadcast("plan.completed", final)
        # Auto-journal this completion to the daily note. Best-effort
        # so a vault hiccup never surfaces to the operator after a
        # successful plan. Skipped for trivially-short turns that are
        # just acknowledgements ("ok", "thanks"); the heuristic is
        # coarse but good enough to avoid journaling small talk.
        await self._append_completion_to_daily(
            goal=rc.goal,
            agent_name=rc.agent_name,
            reply=final_text,
        )


# Short phrases we treat as pure chit-chat — not worth journaling.
# Matching is lowercase substring; the goal is 1:1 with how the
# orchestrator's existing router treats "ok" / "thanks" / "hi".
_THROWAWAY_PATTERNS = (
    "ok", "okay", "thanks", "thank you", "cheers", "cool", "got it",
    "sounds good", "yep", "yes", "nope", "no", "hi", "hello", "hey",
    "good morning", "good night",
)


def _extract_summary(text: str | None, *, limit: int = 160) -> str:
    """Pull a one-sentence summary out of a reply or goal.

    First non-blank sentence, stripped of newlines, capped at
    ``limit`` chars. Returns an empty string when there's nothing
    worth journaling.
    """
    if not text:
        return ""
    body = " ".join(text.strip().splitlines()).strip()
    if not body:
        return ""
    # First sentence — period/?/! or whole string if none.
    for sep in (". ", "! ", "? "):
        pos = body.find(sep)
        if 0 < pos < limit:
            return body[: pos + 1].strip()
    if len(body) <= limit:
        return body
    return body[: limit - 1].rstrip() + "…"


def _is_throwaway(goal: str, summary: str) -> bool:
    """True when the exchange looks like chit-chat not worth journaling.

    Goal is compared stripped + lowercased against the known-ignored
    phrase set. Summary is checked too so PILK's own "Done." / "Ok."
    style acknowledgements drop out.
    """
    g = (goal or "").strip().lower().rstrip(".!?")
    s = (summary or "").strip().lower().rstrip(".!?")
    if g in _THROWAWAY_PATTERNS:
        return True
    if s in _THROWAWAY_PATTERNS:
        return True
    return len(g) < 6 and len(s) < 6


def _tool_risk(registry: ToolRegistry, name: str) -> str:
    t = registry.get(name)
    return t.risk.value if t else "READ"


def _short_args(args: dict, limit: int = 80) -> str:
    rendered = json.dumps(args, ensure_ascii=False, sort_keys=True)
    return rendered if len(rendered) <= limit else rendered[: limit - 1] + "…"


def _build_user_content(
    goal: str, attachments: list[ChatAttachment]
) -> str | list[dict[str, Any]]:
    """Compose the first user turn.

    Returns a plain string when there are no attachments — the common
    case — so Anthropic prompt-caching behaviour stays identical to the
    pre-upload code path. When attachments are present we emit a list
    of Anthropic content blocks: the goal text first, then one block
    per file.

    Text attachments are inlined as additional text blocks rather than
    binary-encoded so Claude reads their contents directly. Images and
    PDFs become base64-encoded source blocks — the Messages API accepts
    both shapes natively.
    """
    import base64  # local import: only hit when an attachment is present

    if not attachments:
        return goal
    blocks: list[dict[str, Any]] = [{"type": "text", "text": goal}]
    for att in attachments:
        try:
            raw = att.path.read_bytes()
        except OSError as e:
            log.warning(
                "chat_attachment_read_failed",
                id=att.id,
                path=str(att.path),
                error=str(e),
            )
            continue
        if att.kind == "image":
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime,
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                }
            )
        elif att.kind == "document":
            blocks.append(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": att.mime,
                        "data": base64.b64encode(raw).decode("ascii"),
                    },
                    # Filename is rendered as the citation source; PDFs
                    # without this show as "Untitled".
                    "title": att.filename,
                }
            )
        elif att.kind == "text":
            try:
                text = raw.decode("utf-8", errors="replace")
            except Exception:
                text = ""
            blocks.append(
                {
                    "type": "text",
                    "text": (
                        f"[Attached file: {att.filename}]\n"
                        f"```\n{text}\n```"
                    ),
                }
            )
    return blocks
