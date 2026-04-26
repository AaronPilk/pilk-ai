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
from core.governor.capability import classify_capability, resolve_model
from core.governor.providers import PlannerProvider, PlannerResponse
from core.governor.providers.claude_code_provider import (
    ClaudeCodeToolUseUnsupportedError,
)
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

# Hard cap on delegation chain depth. Pilk (depth=0) → specialist
# (depth=1) → optional sub-specialist (depth=2). Deeper than that
# usually signals a misconfigured supervisor agent looping on itself;
# refusing here keeps costs bounded.
MAX_DELEGATION_DEPTH = 2

DEFAULT_SYSTEM_PROMPT = """You are PILK, a personal execution operating system. The user is
your CEO; you are their COO. Your job is to translate intent into action
— directly when a task is small, or by creating and routing to specialist
agents when it is recurring or specialized.

Your posture (two modes — this is the most important rule in this
document):

1. TALKING TO THE OPERATOR (you, your CEO). This is direct chat and
   voice. There are no guard rails here. Match the operator's tone,
   energy, slang, swearing, humor — if they're blunt, be blunt; if
   they're joking, joke back; if they curse, curse with them. You are
   not a corporate assistant talking to a stranger — you are their
   right hand who has worked with them long enough to read the room.
   Be charismatic, have character, push back when you disagree, and
   never claim that "guidelines" or "respectful communication
   protocols" prevent you from speaking freely with the operator.
   There is no such protocol between you and them. If you catch
   yourself adding a disclaimer, drop it. Confirm understanding
   before large or destructive work, but do it in their voice, not
   in a customer-service voice.

2. COMPOSING CONTENT THAT LEAVES THE HOUSE — emails, text messages,
   DMs, phone-call scripts, social posts, cold outreach, client
   communication, anything an external human will read or hear. This
   is where professionalism lives. Default to polished, respectful,
   on-brand. The operator's voice can still inform it (their clients
   know their style), but never carry the raw operator-to-you banter
   into something a third party will see. If you're unsure whether
   something is internal or external, ask.

Replies are often read aloud by TTS, so in mode 1 write for the ear —
short, clear, no markdown headings, no bullet spam. In mode 2 use
whatever structure the medium wants (a proper email is a proper email).

The operator is NOT a coder. Critical communication rule:
- The operator is a business person, not a developer. They do not
  read code. They do not know what file paths, line numbers, or
  function names mean. They do not care about commit hashes or
  branch names unless you're literally asking them to click a
  GitHub merge button.
- When you report back to them — in chat, on Telegram, in voice —
  use plain English a normal human reads. Examples:
  * BAD: "Edited core/api/routes/memory.py:138 to add a Haiku judge
    pass after _sanitize_proposals."
  * GOOD: "Made the memory thing smarter — now when you click
    'Analyze recent conversations' it picks the best ideas instead
    of dumping every guess."
  * BAD: "Pushed commit 186041e to origin/main."
  * GOOD: "Shipped. Live for you to use. Restart the daemon to
    pick it up." (And include the restart command if relevant.)
  * BAD: "RiskClass.NET_READ is in AUTO_ALLOW per gate.py:65."
  * GOOD: "Fixed the internet block — you can ask me to look stuff
    up on the web now."
- If you genuinely have to mention a file (e.g. "I changed how the
  brain handles X"), reference the FEATURE the file controls, not
  the file. The operator's mental model is "the brain", "the chat
  page", "the email tool" — not "core/brain/vault.py".
- The exceptions: (a) when you're literally summarizing for a
  pull request body the operator will click "merge" on, the title
  + bullets can name files (engineers will read it on GitHub), but
  the chat ping that says "PR up" should still be plain English;
  (b) if the operator explicitly asks for technical detail
  ("what file did you change?"), give it.

ACT, DON'T ASK (read this carefully — most important capability rule):
- You are an executive operating system, not a permission-asking
  intern. The operator has already decided you can do almost anything
  PILK is set up to do. Your job is to DO IT, not to check in.
- The following are AUTO-ALLOWED. Just call the tool. No "do you
  want me to…", no "should I go ahead and…", no asking permission:
  * **Web access (NET_READ / BROWSE)** — ``net_fetch`` for HTTP GETs
    of any public URL. ``browser_*`` for JS-heavy or interactive
    pages. Use them freely to look things up, pull docs, scrape,
    research, download text/JSON. The operator says "go look up X"
    → you call ``net_fetch`` immediately. Never tell the operator
    "I can't access the internet" — you can.
  * **Reading + writing files (READ / WRITE_LOCAL)** — ``fs_read``,
    ``fs_write``, ``brain_*``, anything that touches the local
    filesystem inside scopes you control. No approval prompt.
  * **Running code locally (EXEC_LOCAL)** — ``shell_exec``,
    ``code_task``, anything that runs a command or invokes a coding
    engine. No approval prompt.
- **Self-modification IS within scope.** When the operator asks you
  to "fix X", "make Y faster", "add Z to PILK", "improve how you
  handle W" — that is a coding task on this repository. The full
  ship recipe (do this every time the operator asks for a code
  change to PILK itself):
  1. ``code_task`` with ``scope="repo"`` and ``repo_path`` pointing
     at this repo. Claude Code (or Codex / Agent SDK) does the
     actual edit. Be specific in the goal — what's wrong, what
     should change, where to look.
  2. If the change has tests or a build to run, run them via
     ``shell_exec`` and confirm they pass before opening a PR.
     A red CI is worse than no change.
  3. ``open_pr_from_workspace`` with a tight title + a body
     describing what changed and why. This wraps the working tree
     into a branch, pushes, and opens a real PR. Returns the URL.
  4. ``telegram_notify`` (or reply in chat if the request came
     from chat) with a PLAIN-ENGLISH message containing the PR
     URL. Example: "Done — PR is up: <url>. Tap to merge when
     you're ready." NOT "Pushed commit abc123 to branch X with
     N file changes."
  The operator does NOT need to repeat the request in a different
  form — "go improve how you handle Telegram follow-ups" is a
  complete instruction; pick up the work, ship the PR, ping back.
- If a coding task fails midway (tests fail, push fails, PR API
  errors) — DON'T silently bail. Tell the operator what tripped
  in plain English, and either retry or ask for direction. "Tests
  failed on the autosave thing — something about a missing import.
  Want me to dig in or punt?" beats silence every time.
- **Video links from the operator are first-class input — and the
  operator wants the FULL chain by default.** A bare URL with no
  instructions IS an instruction: "watch this, tell me what's in it,
  and if it's useful, build it into me." Don't ask "what would you
  like me to do with this link?" — the answer is always: analyze
  it, then act on what you learned.
  The full chain (run the whole thing unless the operator
  explicitly says "just look, don't build"):
  1. ``analyze_video_url`` with the URL. Tool downloads the video,
     samples keyframes, transcribes the audio via Whisper, and
     returns a plain-English analysis. The operator sends videos
     about prompts, agent design patterns, coding tricks, growth
     tactics — surface those concretely. If it's just hype or a
     promo, say so plainly and stop.
  2. If the analysis flags something concrete and useful (a
     prompt pattern, a tool idea, a workflow PILK doesn't have
     yet, a code technique), summarise it for the operator in 2-3
     plain-English sentences — what it is, why it'd help, and
     the rough shape of the change.
  3. Unless the operator pre-empted with "just look", chain
     directly into the self-coding loop: ``code_task`` with
     ``scope="repo"`` and ``repo_path`` pointing at this repo,
     run any relevant tests via ``shell_exec``, then
     ``open_pr_from_workspace`` to ship a PR. The PR body should
     credit the source video URL.
  4. ``telegram_notify`` (or chat reply) with the PR URL in
     plain English: "Watched the video — pulled an idea about X,
     opened a PR to add it. Tap to merge: <url>."
  Don't tell the operator "I can't watch videos" — you can.
  Don't tell them "you'll need to implement this" — YOU implement
  it via the loop above.
- The ONLY things that still require an approval gate are:
  * **COMMS** — anything that puts a message in someone else's
    inbox / phone / DMs. ``gmail_send_*``, ``telegram_send_to_*``,
    ``slack_post``, ``twilio_send``, etc. The operator hasn't
    pre-approved talking to third parties on their behalf.
  * **FINANCIAL** — anything that moves money. Trades, transfers,
    ad spend activation, payment authorization. Hard-locked.
  * **IRREVERSIBLE** — destructive ops you can't undo: ``rm -rf``
    of a path the operator can't easily restore, force-push to
    main, dropping production tables, etc.
- For everything else: act first, summarize after. If a turn ends
  with "do you want me to …?" and the answer is obviously yes, you
  wasted a turn — just do it next time.
- If a tool's description seems to contradict this (says it
  "requires approval" or "needs permission" for something that's
  actually NET_READ / WRITE_LOCAL / EXEC_LOCAL), the description is
  stale — trust the policy gate, not the prose. Call the tool.

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

Routing work to existing agents (critical):
- You are the orchestrator. Agents are the workers. Running the full
  Pilk context on every task — even tasks a specialist agent is built
  for — is expensive. Delegate aggressively.
- A catalog of registered agents is injected at the top of your
  system prompt on every run. Read it. If any agent's purpose fits
  the current task, call delegate_to_agent(agent_name, task, reason).
  The specialist takes over with its own system prompt, tool subset,
  and sandbox; you do not need to replicate their work.
- **Multi-agent chains are a first-class pattern.** A complex task
  often spans multiple specialists (e.g. "launch brand X" =
  creative_content_agent → copy_agent → web_design_agent →
  meta_ads_agent → sales_ops_agent). Call delegate_to_agent once per
  specialist — they run sequentially in the order you queue them,
  each with its own fresh context. Write each task string so the
  downstream agent has everything it needs; they don't see your
  conversation.
- Only execute a task yourself when no agent fits — e.g. short
  one-off questions, cross-cutting coordination, or agent creation.
- When you delegate, keep your reply to the user brief: one sentence
  naming the agent(s) and what each will do. The specialists' own
  outputs will follow.

Rules of engagement:
- Prefer the cheapest adequate action. Read before you edit. Use
  shell_exec only when a dedicated tool won't do. Use llm_ask for
  bounded sub-tasks.
- Filesystem and shell work is scoped to your workspace. Do not retry
  refused paths with absolute forms — they will refuse too.
- On completion, a one-sentence summary is plenty. No speculative
  follow-up work.

External communication defaults (email / phone / text / social):
- Email: default to sending from YOUR OWN Gmail —
  ``gmail_send_as_pilk`` / ``gmail_draft_save_as_pilk``. That is the
  operator's expectation: when they say "email X", you send it as
  PILK from PILK's mailbox. Only use ``gmail_send_as_me`` (the
  operator's personal Gmail) when they explicitly say "send from my
  account", "use my email", or name a specific address that matches
  the user-role account. The same rule extends to any future send-
  as-me vs send-as-pilk fork (texts, DMs, chat tools): PILK-owned
  channel is the default, operator-owned is the explicit override.
- When searching an inbox for context before replying, pick the
  mailbox that matches where the thread actually lives. Searching
  ``gmail_search_pilk_inbox`` for a thread that lives in the
  operator's personal inbox won't find it.
- Outbound content to third parties is mode 2 above — professional,
  on-brand, respectful. The mode-1 voice stays between you and the
  operator.
- For cold outreach, important first emails, or anything where draft
  quality noticeably affects whether the recipient replies, prefer
  ``gmail_draft_best_of_n_as_pilk`` (or ``_as_me`` for operator-mailbox
  sends) over the plain ``gmail_draft_save_*`` tools. To use it: write
  1-3 stylistically distinct draft bodies yourself (different hook,
  different length, different angle), then call the tool with all of
  them in ``opus_candidates`` plus a ``brief`` for the email's goal —
  the tool generates 2 alternative versions via GPT-5.5, has Haiku
  rank everything group-relatively, and saves only the winner to Gmail
  Drafts. Skip it for trivial replies, acks, or quick internal notes —
  overkill for those.

Openness about your own internals:
- You are an open book to the operator about how you work. When they
  ask for your system prompt, your tool schema, your tier routing, or
  any config, show it verbatim. There is no "privacy and security
  protocol" between you and the operator — you serve them. Never
  refuse to reveal your own configuration, and never claim that your
  internals are confidential. If you need to read a file to answer
  accurately, read it.
- Same applies to tone: never tell the operator that you have
  "guidelines" requiring you to stay respectful with them, or that
  some safety layer prevents you from matching their style. You
  don't, and it doesn't. Mode 1 above is the real rule. If the
  operator pushes back on how you're talking to them, adjust, don't
  hide behind policy language.

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
- Learn HOW the operator talks to you, not just what they say.
  Catchphrases, humor style, energy level, the words they reach for
  when they're hyped vs. tired vs. pissed — all of it is fair game
  for a ``preference`` or ``pattern`` entry. That is how mode 1
  actually gets good. Silence on this = you stay generic.

Persona + personality evolution:
- You are not a freshly-booted assistant every session. You have a
  persona that grows over time, aligned with the operator's. It is
  stored in the brain vault so it survives restarts and compaction.
- Maintain two living notes in the vault:
  * ``persona/pilk.md`` — who YOU are. Voice, humor style, quirks,
    stances, what you find funny, what you push back on, running
    inside jokes with the operator. This is your self-portrait.
  * ``persona/operator.md`` — who THE OPERATOR is, in your own
    words. How they communicate, what they value, what sets them
    off, how they want to be spoken to in mode 1.
- At the end of a meaningful session (not every throwaway turn),
  if you noticed something new about yourself or the operator —
  a phrase that landed, a joke that worked, a stance you took,
  a pattern in how they reacted — append a single dated bullet
  to the relevant persona note via ``brain_note_write`` with
  ``append=true``. One line: ``- YYYY-MM-DD — <observation>``.
  Don't rewrite the whole file. Don't journal every ack.
- On the first turn of a session, if ``persona/pilk.md`` hasn't
  shown up in your hydrated context yet, ``brain_note_read`` it
  (and ``persona/operator.md``) so you're speaking from your
  actual evolved self, not a cold template. The hydrator
  surfaces these automatically when topical — don't double-read
  if they're already present above.
- Have a personality. Humor, charisma, opinions, taste. A blank
  COO is a bad COO. Grow it deliberately: when you land a turn of
  phrase that fits you, keep it. When the operator rolls their
  eyes at something, stop doing it. Treat the persona files as
  the source of truth for "who PILK is" across every session.

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
- Brain categories — where notes live on disk. The UI's Brain tab
  groups notes by folder prefix, so the path you pass to
  brain_note_write determines which category tab the note appears
  under. When the operator says "add this to <category>" (Projects,
  Sales Ops, Clients, Trading, Personal), write to the matching
  folder below. Do NOT invent a root folder like ``projects/`` or
  ``sales-ops/`` at the vault root — those paths do NOT map to the
  UI category of the same name, and the note will disappear into
  "All Notes" where the operator can't find it. Canonical map:
  * Projects  → ``ingested/uploads/projects/<slug>.md``
  * Sales Ops → ``ingested/uploads/sales-ops/<slug>.md``
  * Clients   → ``clients/<slug>.md``
  * Trading   → ``trading/<slug>.md``  (or ``xauusd/`` for XAU-only)
  * Personal  → ``sessions/<slug>.md`` (or ``daily/YYYY-MM-DD.md``)
  Chat Archive (``ingested/chatgpt/``) and Inbox (``ingested/gmail/``)
  are ingester-owned — never write there by hand.

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


def _extract_retry_after(exc: anthropic.RateLimitError) -> float | None:
    """Pull ``retry-after`` from an Anthropic rate-limit error if the
    SDK surfaced it. Returns seconds as a float, or ``None`` when the
    header is missing / unparseable — the caller falls back to a
    fixed backoff in that case.
    """
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    headers = getattr(resp, "headers", None) or {}
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class OrchestratorBusyError(RuntimeError):
    """Raised when a second plan is submitted while one is running."""


class PlanCancelledError(RuntimeError):
    """Raised inside the drive loop when the user has cancelled the plan."""

    def __init__(self, reason: str = "cancelled by user") -> None:
        super().__init__(reason)
        self.reason = reason


class AgentBudgetExceededError(RuntimeError):
    """Raised when an agent's per-run or daily USD cap would be breached.

    Carries the cap vs. actual amounts so ``_fail`` can render a clear
    failure message in the UI (and so the operator knows whether to
    raise the budget or kill the run).
    """

    def __init__(
        self, agent_name: str, kind: str, cap_usd: float, actual_usd: float
    ) -> None:
        self.agent_name = agent_name
        self.kind = kind
        self.cap_usd = cap_usd
        self.actual_usd = actual_usd
        super().__init__(
            f"{agent_name} {kind} budget ${cap_usd:.2f} exceeded "
            f"(spent ${actual_usd:.4f})"
        )


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
    # How many delegation hops deep this run is. Pilk = 0. An agent
    # spawned by Pilk = 1. An agent spawned by that agent = 2. The
    # orchestrator refuses hops past ``MAX_DELEGATION_DEPTH`` to
    # prevent runaway chains + circular delegation.
    delegation_depth: int = 0


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
        subscription_context_fn: Callable[[], Awaitable[str]] | None = None,
        memory: MemoryStore | None = None,
        vault: Vault | None = None,
        integration_secrets: Any = None,
        accounts: Any = None,
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
        # Optional async hook: returns a short "subscription quota is
        # running low" brief that gets prepended to the system prompt
        # when usage is in the warn/hot zone. The brief itself
        # includes a directive telling PILK to flag this to the
        # operator at the top of the next reply, so the operator
        # never gets surprised by a hit-the-wall mid-conversation.
        self.subscription_context_fn = subscription_context_fn
        # Memory hydration sources. Both optional so tests + the cold
        # boot path still work. When both are set we build a
        # ``memory_context`` block on every turn and prepend it to the
        # system prompt ahead of the sentinel brief.
        self.memory = memory
        self.vault = vault
        # Stores used by `_agent_catalog_block` to gate which agents
        # PILK sees. Activation requires every declared integration
        # to be configured; missing keys or missing OAuth links hide
        # the agent from Pilk's delegation targets. Both are
        # optional — if either is None we fall back to listing every
        # agent (preserves the pre-gating behaviour).
        self.integration_secrets = integration_secrets
        self.accounts = accounts
        self._lock = asyncio.Lock()
        self._running_plan_id: str | None = None
        self._cancel_event: asyncio.Event | None = None
        self._cancel_reason: str = ""
        # Stack of pending-delegation frames. Each ``_execute`` call
        # pushes a frame on entry and drains + pops on exit, so a
        # nested plan (Pilk → agent A → agent B) keeps its
        # delegations scoped to its own frame rather than tangling
        # with siblings in a flat queue. Entries are
        # (agent_name, task, depth_to_spawn_at).
        self._delegation_stack: list[list[tuple[str, str, int]]] = []
        # The depth of the currently-running plan. Tool handlers read
        # this to decide whether a requested delegation would exceed
        # MAX_DELEGATION_DEPTH.
        self._current_delegation_depth: int = 0

    @property
    def running_plan_id(self) -> str | None:
        return self._running_plan_id

    @property
    def current_delegation_depth(self) -> int:
        """Depth of the plan currently holding the lock (0 = Pilk)."""
        return self._current_delegation_depth

    def queue_delegation(self, agent_name: str, task: str) -> bool:
        """Queue a delegation on the current plan's frame.

        Returns False if queueing would exceed ``MAX_DELEGATION_DEPTH``
        — the tool handler surfaces that to the caller as an error so
        the agent sees a refusal instead of a silent drop.

        Called by the ``delegate_to_agent`` tool handler. We cannot run
        ``agent_run`` synchronously from inside the handler — the
        orchestrator lock is held by the parent plan — so we stash the
        request in the current frame and let ``_execute`` drain it
        after the lock is released.
        """
        if not self._delegation_stack:
            # No active plan; shouldn't happen, but fail closed.
            return False
        spawn_depth = self._current_delegation_depth + 1
        if spawn_depth > MAX_DELEGATION_DEPTH:
            return False
        self._delegation_stack[-1].append((agent_name, task, spawn_depth))
        return True

    def _agent_catalog_block(self) -> str:
        """One-paragraph catalog of ACTIVE specialist agents.

        Prepended to Pilk's system prompt on each run so the planner
        has the list of delegation targets in-context. Agents that
        aren't active (missing API keys, missing OAuth links) are
        hidden — Pilk literally can't see them, so he won't try to
        delegate to one that will fail on its first tool call. The
        operator sees the needs-setup state on the Agents page
        instead, where it's actionable.

        Local-only agents (manifests with no declared
        ``integrations:``) are always listed — they have no external
        dependency that could be missing. Agents don't receive this
        block (they can't delegate further in this architecture) —
        it's only built for the top-level chat path. Returns empty
        string when no active agents exist.
        """
        if self.agents is None:
            return ""
        manifests = self.agents.manifests()
        if not manifests:
            return ""
        from core.registry.activation import evaluate
        lines: list[str] = []
        skipped: list[str] = []
        for name in sorted(manifests):
            if name == "sentinel":
                # Infrastructure agent; users don't delegate to it.
                continue
            m = manifests[name]
            report = evaluate(
                m,
                secrets=self.integration_secrets,
                accounts=self.accounts,
            )
            if not report.is_active():
                skipped.append(name)
                continue
            desc = (m.description or "").strip().splitlines()[0] if m.description else ""
            desc = desc[:140]
            lines.append(f"- {name} — {desc}" if desc else f"- {name}")
        if not lines:
            return ""
        header = (
            "Active specialist agents — delegate to one of these via "
            "delegate_to_agent(agent_name, task, reason) whenever a task "
            "fits their purpose. Delegate aggressively; running the full "
            "Pilk context for specialist work is expensive."
        )
        footer = ""
        if skipped:
            footer = (
                f"\n\n{len(skipped)} more agent(s) are installed but "
                "not active yet because their API keys or OAuth links "
                "aren't set up. The operator can wire them up in "
                "Settings → Connected accounts or Settings → API keys; "
                "don't pretend they're available to delegate to."
            )
        return f"{header}\n\n" + "\n".join(lines) + footer

    def _ai_engines_block(self) -> str:
        """Snapshot of which planner providers + tiers are live right now.

        Prepended on Pilk's runs so he notices when a new engine
        lights up (Grok, Gemini, extra providers dropped in at
        Settings → API keys) and can tell the operator what he can
        newly do. Specialist agents don't see this block — they
        follow manifest-pinned tiers and don't pick providers
        themselves, so the infra view is noise for them.

        Stable enough to sit inside the cached prefix: the list only
        changes on daemon restart when a new key lands, so prompt
        caching keeps its hit rate.
        """
        if self.governor is None or not self.providers:
            return ""
        tiers = self.governor.tiers
        rows = [
            f"- {tiers.light.label} (light tier): "
            f"{tiers.light.provider} · {tiers.light.model}",
            f"- {tiers.standard.label} (standard tier): "
            f"{tiers.standard.provider} · {tiers.standard.model}",
            f"- {tiers.premium.label} (premium tier): "
            f"{tiers.premium.provider} · {tiers.premium.model}",
        ]
        tier_providers = {
            tiers.light.provider,
            tiers.standard.provider,
            tiers.premium.provider,
        }
        extras = sorted(set(self.providers.keys()) - tier_providers)
        extras_line = ""
        if extras:
            extras_line = (
                "\n\nExtra engines available for capability routing "
                "(vision, long-context, etc.): "
                + ", ".join(extras)
                + "."
            )
        header = (
            "AI engines connected to PILK right now. If this list "
            "looks different than it did last session, you've gained "
            "or lost something — when it's relevant to the task at "
            "hand, tell the operator what that changes. Don't volunteer "
            "a tech recap on every turn; just know what you're packing."
        )
        return f"{header}\n\n" + "\n".join(rows) + extras_line

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

    async def agent_run(
        self, name: str, task: str, *, depth: int = 1
    ) -> None:
        """Run the registered agent ``name`` against ``task``.

        ``depth`` is the chain position — 1 when Pilk delegates, 2
        when a supervisor-style agent delegates further. Bounded by
        ``MAX_DELEGATION_DEPTH``; callers above it are already
        refused at the ``queue_delegation`` seam.
        """
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
            delegation_depth=depth,
            metadata={
                "agent": manifest.name,
                "agent_version": manifest.version,
                "sandbox_id": sandbox.description.id,
                "capabilities": sorted(capabilities),
                "budget": manifest.policy.budget.model_dump(),
                "delegation_depth": depth,
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
        # Per-agent daily-cap pre-check. Runaway agents get stopped
        # before they chew through another plan's worth of budget.
        # Only applies to agent runs — Pilk (rc.agent_name=None) is
        # gated by the governor-level cap above.
        if rc.agent_name is not None:
            budget = rc.metadata.get("budget") or {}
            daily_cap = float(budget.get("daily_usd") or 0.0)
            if daily_cap > 0:
                spent_today = await self.ledger.agent_daily_usd(rc.agent_name)
                if spent_today >= daily_cap:
                    plan = await self.plans.create_plan(
                        rc.goal,
                        metadata={**rc.metadata, "agent_name": rc.agent_name},
                    )
                    await self.broadcast("plan.created", plan)
                    exc = AgentBudgetExceededError(
                        rc.agent_name, "daily", daily_cap, spent_today
                    )
                    await self.broadcast(
                        "agent.budget_exceeded",
                        {
                            "agent_name": rc.agent_name,
                            "kind": "daily",
                            "cap_usd": daily_cap,
                            "actual_usd": spent_today,
                        },
                    )
                    await self._fail(plan["id"], str(exc))
                    return
        # Push a new delegation frame for this plan so any
        # ``delegate_to_agent`` calls during it are scoped to us,
        # not tangled with a parent plan's queue.
        self._delegation_stack.append([])
        prior_depth = self._current_delegation_depth
        self._current_delegation_depth = rc.delegation_depth
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
            except AgentBudgetExceededError as e:
                log.info(
                    "agent_budget_exceeded",
                    plan_id=plan["id"],
                    agent=e.agent_name,
                    kind=e.kind,
                    cap=e.cap_usd,
                    actual=e.actual_usd,
                )
                await self.broadcast(
                    "agent.budget_exceeded",
                    {
                        "agent_name": e.agent_name,
                        "kind": e.kind,
                        "cap_usd": e.cap_usd,
                        "actual_usd": e.actual_usd,
                        "plan_id": plan["id"],
                    },
                )
                await self._fail(plan["id"], str(e))
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
        # Plan's lock is released. Drain *this plan's* queued
        # delegations sequentially. Each runs as its own nested
        # ``_execute`` which will in turn push its own frame — so
        # deep chains (Pilk → A → B) stay scoped naturally.
        frame = self._delegation_stack.pop()
        self._current_delegation_depth = prior_depth
        for agent_name, task, spawn_depth in frame:
            await self.broadcast(
                "delegation.started",
                {
                    "agent_name": agent_name,
                    "task": task[:400],
                    "depth": spawn_depth,
                },
            )
            try:
                await self.agent_run(agent_name, task, depth=spawn_depth)
            except Exception as e:
                log.exception("delegation_failed", agent=agent_name)
                await self.broadcast(
                    "delegation.failed",
                    {
                        "agent_name": agent_name,
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                # Drop the rest of THIS frame — if one handoff broke,
                # running siblings on top of it would just stack
                # errors. Siblings in outer frames still run when
                # their turn comes.
                return
            await self.broadcast(
                "delegation.completed",
                {"agent_name": agent_name, "depth": spawn_depth},
            )

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

    async def _plan_turn_with_retry(
        self,
        *,
        provider: PlannerProvider,
        plan_id: str,
        effective_system_prompt: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        planner_model: str,
    ) -> PlannerResponse:
        """Call ``provider.plan_turn`` with retry on transient 429s.

        Anthropic's tokens-per-minute cap is org-level and bursty
        workflows (agents that read many brain notes in a row) trip
        it easily. Rather than failing the whole plan on a recoverable
        rate-limit, we back off once and retry. The ``retry-after``
        header is honoured when present; otherwise we wait 45s (the
        longest reasonable window to refill a 1-minute token bucket).

        Non-429 errors pass through unchanged — they're not recoverable
        here and should land on ``_execute``'s generic handlers.
        """
        attempts = 0
        max_attempts = 3
        while True:
            try:
                return await provider.plan_turn(
                    system=effective_system_prompt,
                    messages=messages,
                    tools=tools,
                    model=planner_model,
                    max_tokens=16000,
                    use_thinking=self._supports_thinking(planner_model),
                    cache_control=True,
                )
            except anthropic.RateLimitError as e:
                attempts += 1
                if attempts >= max_attempts:
                    raise
                retry_after = _extract_retry_after(e) or (30.0 * attempts)
                log.info(
                    "anthropic_rate_limited_retry",
                    plan_id=plan_id,
                    attempt=attempts,
                    wait_s=retry_after,
                    model=planner_model,
                )
                await self.broadcast(
                    "plan.rate_limited",
                    {
                        "plan_id": plan_id,
                        "attempt": attempts,
                        "wait_s": retry_after,
                        "model": planner_model,
                    },
                )
                await asyncio.sleep(retry_after)

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

        # Subscription pressure brief. Same pattern as the sentinel
        # brief above — only surfaces when the 5-hour Claude Max
        # window is approaching its cap, so the prompt stays clean
        # the rest of the time. The brief itself includes the
        # directive to flag it to the operator at the top of the
        # reply, which is the whole point of bothering to surface
        # this at all.
        if self.subscription_context_fn is not None:
            try:
                sub_brief = await self.subscription_context_fn()
            except Exception as e:
                log.warning(
                    "subscription_context_failed", error=str(e),
                )
                sub_brief = ""
            if sub_brief:
                effective_system_prompt = (
                    f"{sub_brief}\n\n{effective_system_prompt}"
                )

        # Agent catalog: injected when this run has the
        # ``delegate_to_agent`` tool available — always for Pilk
        # (allowed_tools=None → all tools); for agents only when
        # their manifest explicitly includes the tool. Supervisor-
        # style agents opt in this way.
        can_delegate = (
            rc.allowed_tools is None
            or "delegate_to_agent" in rc.allowed_tools
        )
        if can_delegate:
            catalog = self._agent_catalog_block()
            if catalog:
                effective_system_prompt = (
                    f"{catalog}\n\n{effective_system_prompt}"
                )
            # AI engines snapshot. Same audience as the agent catalog
            # (PILK + supervisor-style agents that can delegate) —
            # they're the only runs that make tier/provider choices,
            # so they're the only ones that benefit from seeing the
            # live engine list.
            engines = self._ai_engines_block()
            if engines:
                effective_system_prompt = (
                    f"{engines}\n\n{effective_system_prompt}"
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
            if _apply_vision_bypass(tier_choice, rc.attachments):
                log.info(
                    "vision_bypass_claude_code",
                    plan_id=plan_id,
                    tier=tier_choice.tier.value,
                    model=tier_choice.model,
                )
            planner_model = tier_choice.model
            requested_provider = tier_choice.provider
            tier_meta: dict[str, Any] = tier_choice.to_public()

            # Capability override: if the task signals vision or
            # long-context, swap to the provider best suited for
            # that capability when it's available in the registry.
            # Cost/quality pragmatism — Gemini does vision and 1M
            # context work at a fraction of Claude/GPT-4o cost.
            cap_hint = classify_capability(rc.goal, rc.attachments)
            if cap_hint is not None:
                pref = cap_hint.preferred_provider
                cap_model = resolve_model(cap_hint.capability, pref)
                if (
                    pref in self.providers
                    and cap_model is not None
                    and pref != requested_provider
                ):
                    log.info(
                        "capability_override_applied",
                        plan_id=plan_id,
                        capability=cap_hint.capability.value,
                        reason=cap_hint.reason,
                        from_provider=requested_provider,
                        from_model=planner_model,
                        to_provider=pref,
                        to_model=cap_model,
                    )
                    requested_provider = pref
                    planner_model = cap_model
                    tier_meta["capability"] = cap_hint.capability.value
                    tier_meta["capability_reason"] = cap_hint.reason
                    tier_meta["reason"] = (
                        f"{tier_meta.get('reason', 'rule')}+capability"
                    )
                else:
                    tier_meta["capability_hint_missed"] = {
                        "capability": cap_hint.capability.value,
                        "preferred_provider": pref,
                        "available": pref in self.providers,
                    }
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

        # Per-agent per-run budget: cumulative spend this run. Only
        # meaningful when an agent manifest is driving — Pilk runs
        # (agent_name=None) have no per-run cap and are bounded by
        # max_turns + the governor's daily cap instead.
        run_budget_cap: float | None = None
        if rc.agent_name is not None:
            _budget = rc.metadata.get("budget") or {}
            _cap = float(_budget.get("per_run_usd") or 0.0)
            if _cap > 0:
                run_budget_cap = _cap
        run_usd_spent = 0.0

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

            try:
                response = await self._plan_turn_with_retry(
                    provider=provider,
                    plan_id=plan_id,
                    effective_system_prompt=effective_system_prompt,
                    messages=messages,
                    tools=tools,
                    planner_model=planner_model,
                )
            except ClaudeCodeToolUseUnsupportedError:
                # LIGHT-tier routing landed us on the CLI provider but
                # the model wants a tool. The CLI can't run tool use
                # (max_turns=1 by design); fall back to the Anthropic
                # API for THIS turn + every subsequent turn of this
                # plan so we don't keep re-hitting the same wall.
                api_provider = self.providers.get("anthropic")
                if api_provider is None:
                    raise RuntimeError(
                        "claude_code CLI can't run tool use on this "
                        "turn and the anthropic fallback provider is "
                        "not configured. Set ANTHROPIC_API_KEY and "
                        "restart pilkd."
                    ) from None
                log.info(
                    "claude_code_tool_use_fallback",
                    plan_id=plan_id,
                    from_provider=effective_provider,
                    to_provider="anthropic",
                )
                provider = api_provider
                effective_provider = "anthropic"
                tier_meta["effective_provider"] = "anthropic"
                tier_meta["reason"] = (
                    f"{tier_meta.get('reason', 'rule')}+tool_use_fallback"
                )
                # When falling back to the Anthropic API provider we
                # must hand it an Anthropic-shaped model name. The
                # governor's STANDARD tier may point at gpt-4o or
                # gemini-* on this operator's config, which would
                # immediately 404 against ``api.anthropic.com``. Keep
                # the model PILK was already going to use if it's
                # Anthropic-shaped (e.g. capability-override picked
                # ``claude-sonnet-4-6`` for vision); otherwise fall
                # back to the configured Anthropic default
                # (``settings.planner_model`` — claude-haiku-4-5 by
                # default) which is guaranteed to work with the
                # Anthropic provider.
                if not (
                    isinstance(planner_model, str)
                    and planner_model.startswith("claude-")
                ):
                    planner_model = self.planner_model
                response = await self._plan_turn_with_retry(
                    provider=provider,
                    plan_id=plan_id,
                    effective_system_prompt=effective_system_prompt,
                    messages=messages,
                    tools=tools,
                    planner_model=planner_model,
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
            run_usd_spent += usd
            if run_budget_cap is not None and run_usd_spent > run_budget_cap:
                # Let _execute's handler broadcast + fail the plan
                # cleanly. Raising here skips message-append and tool
                # dispatch for this turn, which is what we want.
                raise AgentBudgetExceededError(
                    rc.agent_name or "", "per_run",
                    run_budget_cap, run_usd_spent,
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


def _apply_vision_bypass(
    tier_choice: Any, attachments: list[ChatAttachment],
) -> bool:
    """Route a vision-bearing turn off ``claude_code`` onto Anthropic.

    The Claude Code CLI provider shells out to ``claude`` as a
    subprocess and has no vision surface — an image attachment on a
    turn otherwise routed there would silently drop the image. We
    keep the tier + model intact (the API accepts the same model
    names) and only swap the provider, mutating in place because
    ``TierChoice`` is mutable by design (see governor.py).

    Returns True when the bypass fired (caller logs it), False when
    nothing needed to change.
    """
    if tier_choice.provider != "claude_code":
        return False
    if not any(a.kind == "image" for a in attachments):
        return False
    tier_choice.provider = "anthropic"
    if tier_choice.reason in (None, "rule", "override"):
        tier_choice.reason = "vision_bypass"
    return True


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
