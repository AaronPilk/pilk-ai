"""``gmail_draft_best_of_n_as_*`` — cross-model best-of-N email drafting.

PILK supplies 1-3 draft bodies it wrote inline (subscription Opus, free
at the margin). The tool asks GPT-5.5 (API-billed) for 2 stylistically
distinct alternatives, then runs a Haiku judge that ranks every
candidate group-relatively on four axes (hook, clarity, tone fit, CTA
strength). Only the winner is saved to Gmail Drafts; rejected bodies +
judge reasoning are written to a brain-vault telemetry note for later
analysis.

Designed so the OpenAI HTTP call and the Gmail save can be replaced
with stubs in tests — the factory accepts ``openai_caller`` and
``save_draft_caller`` overrides for that purpose.

Failure modes are all fail-soft:
- GPT-5.5 down or API key missing → rank just the Opus candidates.
- Haiku judge down → fall back to the operator's first Opus draft.
- Gmail save fails → propagate as a clean tool error (only true
  show-stopper; nothing useful happens without a saved draft).
- Telemetry write fails → log + continue; the draft is the deliverable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.mime.text import MIMEText
from typing import TYPE_CHECKING, Any

import httpx

from core.identity import AccountsStore
from core.integrations.google.accounts import GoogleRole
from core.integrations.google.gmail import _do_draft_save
from core.integrations.google.oauth import credentials_from_blob
from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import AccountBinding, Tool, ToolContext, ToolOutcome

if TYPE_CHECKING:
    from anthropic import AsyncAnthropic

    from core.brain.vault import Vault

log = get_logger("pilkd.gmail_bon")

OPENAI_BASE_URL = "https://api.openai.com/v1"
OPENAI_BON_MODEL_ENV = "PILK_OPENAI_BON_MODEL"
DEFAULT_OPENAI_BON_MODEL = "gpt-5.5"
HAIKU_JUDGE_MODEL = "claude-haiku-4-5"
GPT_VARIANT_COUNT = 2
MAX_OPUS_CANDIDATES = 3
GENERATOR_MAX_TOKENS = 1500
JUDGE_MAX_TOKENS = 1500

# Hooks for tests. ``openai_caller(api_key, model, system, user) -> str``
# returns the assistant's text content. ``save_draft_caller(creds,
# raw_b64url, thread_id) -> dict`` returns Gmail's draft response.
OpenAICaller = Callable[[str, str, str, str], Awaitable[str]]
SaveDraftCaller = Callable[[Any, str, str | None], Awaitable[dict]]

_ROLE_TOOL_NAMES: dict[GoogleRole, str] = {
    "system": "gmail_draft_best_of_n_as_pilk",
    "user": "gmail_draft_best_of_n_as_me",
}

_ROLE_NOUN: dict[GoogleRole, str] = {
    "system": "PILK's operational Gmail",
    "user": "your working Gmail",
}

_GPT_GENERATOR_SYSTEM = (
    "You are drafting alternative versions of an outbound email for a "
    "professional outreach context. Another model has already produced "
    "its takes. Your job is to bring DIFFERENT angles, hooks, or tone "
    "choices that the operator might prefer over those drafts.\n\n"
    "Output JSON ONLY, no prose. Shape:\n"
    '{"drafts": ["body 1...", "body 2..."]}\n\n'
    "Rules:\n"
    "- Write 2 stylistically distinct draft bodies. Different lengths "
    "and openings encouraged.\n"
    "- Match the brief's intent. Do NOT invent new claims.\n"
    "- Plain text only. No markdown headings, no signature blocks, no "
    "subject line — body only.\n"
    '- If the brief is unworkable, return {"drafts": []}.\n'
)

_JUDGE_SYSTEM = (
    "You are PILK's outbound copy curator. You receive several draft "
    "email bodies aimed at the same recipient/subject. Rank them "
    "group-relatively on four axes:\n\n"
    "- hook: how strong is the opening line at earning a read?\n"
    "- clarity: is the value prop unmistakable?\n"
    "- tone_fit: does it match a professional, warm outbound tone?\n"
    "- cta_strength: is the next step clear and easy to take?\n\n"
    "Output JSON ONLY, no prose. Shape:\n"
    '{"rankings": [{"index": <0-based int>, "hook": 0.0-1.0, '
    '"clarity": 0.0-1.0, "tone_fit": 0.0-1.0, "cta_strength": 0.0-1.0, '
    '"verdict": "keep|drop", "reason": "one short sentence"}]}\n\n'
    "Rules:\n"
    "- Score relatively against the OTHER drafts, not in absolute "
    "terms. Surface the strongest of THIS group.\n"
    "- Include EVERY draft exactly once, identified by 0-based index.\n"
    "- Mark verdict 'drop' for clearly weaker drafts so the winner is "
    "unambiguous.\n"
)


def _resolve_openai_model() -> str:
    raw = os.getenv(OPENAI_BON_MODEL_ENV, DEFAULT_OPENAI_BON_MODEL).strip()
    return raw or DEFAULT_OPENAI_BON_MODEL


async def _default_openai_caller(
    api_key: str,
    model: str,
    system: str,
    user_message: str,
) -> str:
    """Direct httpx call to OpenAI Chat Completions. Mirrors the
    pattern used by ``OpenAIPlannerProvider`` — no SDK dep."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        r = await client.post(
            f"{OPENAI_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
                "max_completion_tokens": GENERATOR_MAX_TOKENS,
            },
        )
        if r.status_code >= 400:
            raise RuntimeError(
                f"openai {r.status_code} ({model}): {r.text[:300]}"
            )
        data = r.json()
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    return str(msg.get("content") or "").strip()


async def _default_save_draft_caller(
    creds: Any, raw_b64url: str, thread_id: str | None,
) -> dict:
    return await asyncio.to_thread(_do_draft_save, creds, raw_b64url, thread_id)


def _strip_fence(text: str) -> str:
    """Drop a leading ``` / ```json fence if present so json.loads
    sees raw JSON. Matches the convention used elsewhere in the
    codebase (memory distill, brain ingestors)."""
    body = text.strip()
    if body.startswith("```"):
        first_nl = body.find("\n")
        if first_nl > 0:
            body = body[first_nl + 1 :]
        if body.endswith("```"):
            body = body[:-3]
        body = body.strip()
    return body


def _parse_drafts_json(text: str) -> list[str]:
    """Pull the ``drafts`` array out of GPT-5.5's response. Returns
    [] on any parse problem so the caller can fall back gracefully."""
    body = _strip_fence(text)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("gmail_bon_drafts_non_json", body_prefix=body[:120])
        return []
    if not isinstance(data, dict):
        return []
    drafts = data.get("drafts")
    if not isinstance(drafts, list):
        return []
    return [
        str(d).strip() for d in drafts
        if isinstance(d, str) and str(d).strip()
    ]


def _parse_rankings_json(text: str) -> list[dict[str, Any]]:
    body = _strip_fence(text)
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        log.warning("gmail_bon_rankings_non_json", body_prefix=body[:120])
        return []
    if not isinstance(data, dict):
        return []
    rankings = data.get("rankings")
    return rankings if isinstance(rankings, list) else []


def _score_rankings(
    rankings: list[dict[str, Any]],
    candidate_count: int,
) -> list[tuple[int, float, dict[str, Any]]]:
    """Convert raw rankings to sorted (index, score, ranking) tuples.
    'drop' verdict forces score to 0; otherwise score is the mean of
    the four axis values that came back as numbers. Each index is
    counted at most once."""
    seen: set[int] = set()
    out: list[tuple[int, float, dict[str, Any]]] = []
    for r in rankings:
        if not isinstance(r, dict):
            continue
        idx = r.get("index")
        if not isinstance(idx, int) or idx < 0 or idx >= candidate_count:
            continue
        if idx in seen:
            continue
        seen.add(idx)
        verdict = str(r.get("verdict") or "").strip().lower()
        if verdict == "drop":
            score = 0.0
        else:
            axes = [
                r.get("hook"),
                r.get("clarity"),
                r.get("tone_fit"),
                r.get("cta_strength"),
            ]
            nums = [float(a) for a in axes if isinstance(a, (int, float))]
            score = sum(nums) / len(nums) if nums else 0.0
        out.append((idx, score, r))
    out.sort(key=lambda x: x[1], reverse=True)
    return out


def _slugify(text: str, *, limit: int = 40) -> str:
    cleaned = "".join(
        c if c.isalnum() else "-" for c in text
    )[:limit].strip("-").lower()
    return cleaned or "untitled"


def _telemetry_note(
    *,
    winner_idx: int,
    candidates: list[dict[str, str]],
    scored: list[tuple[int, float, dict[str, Any]]] | None,
    to: str,
    subject: str,
    draft_id: str,
) -> str:
    """Markdown record of one BoN run for the Obsidian vault."""
    ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Gmail Best-of-N Run",
        "",
        f"- **When:** {ts}",
        f"- **To:** {to}",
        f"- **Subject:** {subject}",
        (
            f"- **Winner:** index {winner_idx} "
            f"(source: {candidates[winner_idx]['source']})"
        ),
        f"- **Saved draft:** {draft_id}",
        "",
        "## Candidates",
        "",
    ]
    rank_map: dict[int, dict[str, Any]] = {}
    for idx, score, rank in scored or []:
        r = dict(rank)
        r["score"] = score
        rank_map[idx] = r
    for i, c in enumerate(candidates):
        r = rank_map.get(i, {})
        verdict = r.get("verdict") or "—"
        score = r.get("score")
        score_str = f"{score:.2f}" if isinstance(score, float) else "—"
        reason = r.get("reason") or ""
        lines.extend(
            [
                (
                    f"### [{i}] source={c['source']}  "
                    f"verdict={verdict}  score={score_str}"
                ),
                f"_{reason}_" if reason else "",
                "",
                "```",
                c["body"],
                "```",
                "",
            ]
        )
    return "\n".join(lines)


def make_gmail_draft_best_of_n_tool(
    role: GoogleRole,
    accounts: AccountsStore,
    anthropic_client: AsyncAnthropic,
    openai_api_key: str | None,
    vault: Vault,
    *,
    openai_caller: OpenAICaller | None = None,
    save_draft_caller: SaveDraftCaller | None = None,
    ledger: Any | None = None,
) -> Tool:
    """Build the role-specific BoN tool. Two test seams: ``openai_caller``
    (defaults to httpx → OpenAI) and ``save_draft_caller`` (defaults to
    the Gmail SDK draft-save). Either can be swapped out by tests
    without monkey-patching modules."""

    binding = AccountBinding(provider="google", role=role)
    tool_name = _ROLE_TOOL_NAMES[role]
    noun = _ROLE_NOUN[role]
    open_ai_call = openai_caller or _default_openai_caller
    save_call = save_draft_caller or _default_save_draft_caller

    def _load_creds() -> tuple[Any | None, Any | None]:
        account = accounts.resolve_binding(binding)
        if account is None:
            return None, None
        tokens = accounts.load_tokens(account.account_id)
        if tokens is None:
            return None, account
        blob = {
            "access_token": tokens.access_token,
            "refresh_token": tokens.refresh_token,
            "client_id": tokens.client_id,
            "client_secret": tokens.client_secret,
            "scopes": tokens.scopes,
            "token_uri": tokens.token_uri,
            "email": account.email,
        }
        return credentials_from_blob(blob), account

    not_linked_msg = (
        f"No Google account linked for role '{role}'. Connect one in "
        "Settings → Connected accounts before drafting."
    )

    async def _generate_gpt_variants(
        brief: str,
        to: str,
        subject: str,
        opus_candidates: list[str],
    ) -> list[str]:
        """Ask GPT-5.5 for stylistically distinct alternatives.
        Returns [] on missing key or any failure."""
        if not openai_api_key:
            log.info("gmail_bon_gpt_skipped", reason="no_api_key")
            return []
        opus_block = "\n".join(
            f"[opus {i + 1}]\n{c}\n"
            for i, c in enumerate(opus_candidates)
        )
        user_message = (
            f"Brief: {brief}\n"
            f"Recipient: {to}\n"
            f"Subject: {subject}\n\n"
            "Drafts already produced by the other model "
            "(write yours stylistically different):\n\n"
            f"{opus_block}"
        )
        model = _resolve_openai_model()
        try:
            text = await open_ai_call(
                openai_api_key, model, _GPT_GENERATOR_SYSTEM, user_message,
            )
        except Exception:
            log.exception("gmail_bon_gpt_failed", model=model, role=role)
            return []
        return _parse_drafts_json(text)[:GPT_VARIANT_COUNT]

    async def _judge(
        candidates: list[dict[str, str]],
        subject: str,
    ) -> list[tuple[int, float, dict[str, Any]]] | None:
        """Run the Haiku judge. Returns sorted (idx, score, ranking)
        tuples, or ``None`` on failure so the caller can fall back."""
        listing = "\n\n".join(
            f"[{i}] (source: {c['source']})\n{c['body']}"
            for i, c in enumerate(candidates)
        )
        user_message = (
            f"Subject: {subject}\n\n"
            f"Rank these {len(candidates)} draft bodies:\n\n{listing}"
        )
        try:
            resp = await anthropic_client.messages.create(
                model=HAIKU_JUDGE_MODEL,
                max_tokens=JUDGE_MAX_TOKENS,
                system=_JUDGE_SYSTEM,
                messages=[{"role": "user", "content": user_message}],
            )
        except Exception:
            log.exception("gmail_bon_judge_failed")
            return None
        # Cost-tracking — best-effort; never let ledger failure
        # break the actual ranking flow. Previously bypassed
        # cost_entries and showed up as untracked Anthropic spend.
        if ledger is not None:
            try:
                await ledger.record_anthropic_response(
                    model=HAIKU_JUDGE_MODEL,
                    response=resp,
                    agent_name=f"gmail_draft_best_of_n_{role}",
                )
            except Exception:  # noqa: BLE001
                pass
        text = ""
        for block in resp.content or []:
            if getattr(block, "type", None) == "text":
                text += getattr(block, "text", "")
        rankings = _parse_rankings_json(text)
        if not rankings:
            return None
        scored = _score_rankings(rankings, len(candidates))
        return scored or None

    async def _handler(args: dict, ctx: ToolContext) -> ToolOutcome:
        to = str(args.get("to") or "").strip()
        if not to or "@" not in to:
            return ToolOutcome(
                content=f"{tool_name} requires a valid 'to' address.",
                is_error=True,
            )
        subject = str(args.get("subject") or "").strip()
        if not subject:
            return ToolOutcome(
                content=f"{tool_name} requires a 'subject'.",
                is_error=True,
            )
        brief = str(args.get("brief") or "").strip()
        if not brief:
            return ToolOutcome(
                content=(
                    f"{tool_name} requires a 'brief' so GPT-5.5 has "
                    "context to write alternative versions."
                ),
                is_error=True,
            )
        raw_opus = args.get("opus_candidates")
        if not isinstance(raw_opus, list):
            return ToolOutcome(
                content=(
                    f"{tool_name} requires 'opus_candidates' as a list "
                    "of 1+ draft body strings PILK has already written."
                ),
                is_error=True,
            )
        opus_candidates = [
            str(x).strip()
            for x in raw_opus
            if isinstance(x, str) and str(x).strip()
        ][:MAX_OPUS_CANDIDATES]
        if not opus_candidates:
            return ToolOutcome(
                content=(
                    f"{tool_name} needs at least one non-empty draft "
                    "in 'opus_candidates'."
                ),
                is_error=True,
            )
        cc = str(args.get("cc") or "").strip()
        bcc = str(args.get("bcc") or "").strip()
        thread_id = (
            str(args.get("reply_to_thread_id") or "").strip() or None
        )

        creds, _account = _load_creds()
        if creds is None:
            return ToolOutcome(content=not_linked_msg, is_error=True)

        gpt_variants = await _generate_gpt_variants(
            brief=brief,
            to=to,
            subject=subject,
            opus_candidates=opus_candidates,
        )

        candidates: list[dict[str, str]] = [
            {"body": b, "source": "opus"} for b in opus_candidates
        ] + [
            {"body": b, "source": "gpt-5.5"} for b in gpt_variants
        ]

        scored: list[tuple[int, float, dict[str, Any]]] | None = None
        if len(candidates) > 1:
            scored = await _judge(candidates, subject)

        if scored:
            winner_idx, winner_score, winner_rank = scored[0]
        else:
            log.info(
                "gmail_bon_judge_fallback",
                reason="no_ranking",
                opus_count=len(opus_candidates),
                gpt_count=len(gpt_variants),
            )
            winner_idx = 0
            winner_score = 0.0
            winner_rank = {}

        winner_body = candidates[winner_idx]["body"]
        winner_source = candidates[winner_idx]["source"]

        try:
            msg = MIMEText(winner_body, "plain", "utf-8")
            msg["to"] = to
            msg["subject"] = subject
            if cc:
                msg["cc"] = cc
            if bcc:
                msg["bcc"] = bcc
            if getattr(creds, "email", None):
                msg["from"] = creds.email
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")
            draft = await save_call(creds, raw, thread_id)
            draft_id = str(draft.get("id") or "")
        except Exception as e:
            log.exception("gmail_bon_draft_save_failed", role=role)
            return ToolOutcome(
                content=(
                    f"{tool_name} draft save failed: "
                    f"{type(e).__name__}: {e}"
                ),
                is_error=True,
            )

        telemetry_path = ""
        try:
            day = datetime.now(UTC).strftime("%Y-%m-%d")
            slug = _slugify(subject)
            telemetry_path = (
                f"best_of_n_logs/{day}/{draft_id[:8]}-{slug}.md"
            )
            note_body = _telemetry_note(
                winner_idx=winner_idx,
                candidates=candidates,
                scored=scored,
                to=to,
                subject=subject,
                draft_id=draft_id,
            )
            vault.write(telemetry_path, note_body)
        except Exception:
            log.exception("gmail_bon_telemetry_write_failed")
            telemetry_path = ""

        log.info(
            "gmail_bon_completed",
            role=role,
            opus_count=len(opus_candidates),
            gpt_count=len(gpt_variants),
            winner_source=winner_source,
            winner_score=winner_score,
            judge_used=scored is not None,
        )

        return ToolOutcome(
            content=(
                f"Saved best-of-{len(candidates)} draft to {to} "
                f"(subject: {subject}). Winner from {winner_source} "
                f"(score {winner_score:.2f}). "
                f"Draft {draft_id[:12]}… — review in Gmail drafts, "
                "then send when ready."
            ),
            data={
                "draft_id": draft_id,
                "winner_index": winner_idx,
                "winner_source": winner_source,
                "winner_body": winner_body,
                "score": winner_score,
                "judge_reasoning": str(winner_rank.get("reason") or ""),
                "candidates_evaluated": len(candidates),
                "opus_count": len(opus_candidates),
                "gpt_count": len(gpt_variants),
                "telemetry_log": telemetry_path,
                "to": to,
                "subject": subject,
            },
        )

    return Tool(
        name=tool_name,
        description=(
            f"Cross-model best-of-N email drafting for {noun}. PILK "
            "supplies 1-3 draft bodies it has already written; this "
            "tool generates 2 alternative versions via GPT-5.5, has "
            "Haiku rank all candidates group-relatively on hook / "
            "clarity / tone_fit / cta_strength, and saves only the "
            "winner to Gmail Drafts. The operator reviews the polished "
            "draft in Gmail before sending. Use for cold outreach, "
            "important follow-ups, or anything where draft quality "
            "noticeably affects response rate. Don't use for trivial "
            "replies — overkill."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "to": {
                    "type": "string",
                    "description": "Recipient email.",
                },
                "subject": {"type": "string"},
                "brief": {
                    "type": "string",
                    "description": (
                        "What this email needs to accomplish. GPT-5.5 "
                        "uses this to write its alternatives, so be "
                        "specific about the goal, the recipient's "
                        "context, and any constraints on tone or claims."
                    ),
                },
                "opus_candidates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": MAX_OPUS_CANDIDATES,
                    "description": (
                        "1-3 draft bodies you've written. Plain text. "
                        "Distinct angles encouraged so the judge has "
                        "real choice."
                    ),
                },
                "cc": {"type": "string"},
                "bcc": {"type": "string"},
                "reply_to_thread_id": {
                    "type": "string",
                    "description": (
                        "Optional Gmail threadId to attach the winning "
                        "draft to (for replies)."
                    ),
                },
            },
            "required": ["to", "subject", "brief", "opus_candidates"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=_handler,
        account_binding=binding,
    )
