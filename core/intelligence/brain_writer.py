"""BrainWriter — persists scored intelligence items as markdown
notes inside the brain vault.

Path conventions (Batch 3D — additive over Batch 2):
  - Sources with no ``project_slug`` write to:
        ``~/PILK-brain/world/YYYY-MM-DD/<slug>.md``
    (unchanged from prior batches)
  - Sources with a ``project_slug`` write to:
        ``~/PILK-brain/projects/<project_slug>/world/YYYY-MM-DD/<slug>.md``
    The slug is sanitised against the same regex the projects
    manager uses, so an attacker-shaped value can't land outside
    ``projects/``.

Strict additive contract (unchanged):
  - Only ever creates new files. Never overwrites (collisions get
    a numeric suffix).
  - Never modifies, deletes, or moves any other vault file.
  - Never writes outside the resolved ``world/`` root — the path-
    escape guard checks the resolved root regardless of which
    branch (global vs project) the source picked.
  - Caps body + excerpt sizes so a runaway feed can't fill disk.

Format is YAML frontmatter + plain-English markdown so Obsidian /
Marked / VS Code render it as-is. Frontmatter holds machine-
readable provenance (source, URL, hashes, score) so a future
downstream consumer (vector indexer, brain ingester) can rebuild
metadata without re-fetching.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.brain import Vault
from core.intelligence.items import IntelItem
from core.intelligence.models import SourceSpec
from core.logging import get_logger

log = get_logger("pilkd.intelligence.brain_writer")

# Hard caps to keep one item from running away with disk space.
MAX_BODY_CHARS = 4000        # the operator's-summary section
MAX_EXCERPT_CHARS = 1500     # the raw-source excerpt block
MAX_TITLE_CHARS = 240        # used in slug + heading
MAX_SLUG_CHARS = 80          # filename safety net

# Project slug pattern — duplicates ``core.projects.VALID_SLUG`` on
# purpose so this module doesn't import the projects subsystem (the
# brain writer must stay usable in tests that don't boot the full
# project manager). Lowercase letters, digits, hyphens. 1–64 chars.
# Anything else falls back to the global ``world/`` root with a
# log line — better than crashing or writing somewhere unexpected.
_PROJECT_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


@dataclass
class WriteResult:
    path: str           # vault-relative path of the new note
    absolute_path: str  # filesystem path
    skipped: bool       # true if the item already had a brain_path or write was disallowed
    reason: str | None = None


class BrainWriter:
    """Writes intelligence items into the brain vault. One instance
    per daemon / refresh path. Stateless apart from the Vault handle.
    """

    def __init__(self, vault: Vault) -> None:
        self._vault = vault
        # Global ``world/`` root — used when a source has no
        # ``project_slug``. Project-scoped sources resolve to a
        # different root via ``_world_root_for(source)``.
        self._world_root = vault.root / "world"

    def write(
        self,
        *,
        item: IntelItem,
        source: SourceSpec,
        body: str | None,
        matched_topics: list[str] | None = None,
    ) -> WriteResult:
        """Persist ``item`` as a markdown note and return where it
        landed. Idempotent: if the item already has a ``brain_path``,
        the existing path is returned without rewriting the file.

        Picks one of two roots based on ``source.project_slug``:
          - None / empty → ``vault/world/<date>/...``
          - valid slug   → ``vault/projects/<slug>/world/<date>/...``
        Anything else (slug fails sanitisation) falls back to the
        global root with a log warning so a typo doesn't lose data.
        """
        # Idempotency: never overwrite a note we already wrote for
        # this item. The status updater on IntelItem records
        # brain_path; once set, we trust it.
        if item.brain_path:
            return WriteResult(
                path=item.brain_path,
                absolute_path=str(self._vault.root / item.brain_path),
                skipped=True,
                reason="item already has brain_path",
            )

        world_root = self._world_root_for(source)
        date_str = self._date_part(item)
        day_dir = world_root / date_str
        # mkdir is the only filesystem mutation outside the new file
        # itself. ``exist_ok=True`` covers the common case of multiple
        # items landing on the same UTC day.
        day_dir.mkdir(parents=True, exist_ok=True)

        slug = self._slugify(item.title)
        target = self._next_available_path(day_dir, slug)
        rel_path = target.relative_to(self._vault.root).as_posix()

        # Guard: ensure the resolved target is inside the resolved
        # world_root we just picked. Defence-in-depth against a
        # malicious title that somehow smuggles ``../`` past the
        # slugifier — should never trigger given the regex, but
        # cheap insurance and required by the per-project routing
        # since the operator can now influence the path through
        # ``source.project_slug``.
        try:
            target.resolve().relative_to(world_root.resolve())
        except ValueError:
            return WriteResult(
                path="",
                absolute_path="",
                skipped=True,
                reason=(
                    "refused: resolved path escaped the configured "
                    "world/ root"
                ),
            )

        body_block = (body or "").strip()
        if len(body_block) > MAX_BODY_CHARS:
            body_block = body_block[:MAX_BODY_CHARS] + " …[truncated]"

        excerpt = (body or "").strip()
        if len(excerpt) > MAX_EXCERPT_CHARS:
            excerpt = excerpt[:MAX_EXCERPT_CHARS] + " …[truncated]"

        document = self._render(
            item=item,
            source=source,
            body_block=body_block,
            excerpt=excerpt,
            matched_topics=matched_topics or [],
        )

        target.write_text(document, encoding="utf-8")
        log.info(
            "intel_brain_note_written",
            item_id=item.id,
            path=rel_path,
            chars=len(document),
        )
        return WriteResult(
            path=rel_path,
            absolute_path=str(target),
            skipped=False,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _render(
        self,
        *,
        item: IntelItem,
        source: SourceSpec,
        body_block: str,
        excerpt: str,
        matched_topics: list[str],
    ) -> str:
        title = (item.title or "(untitled)").strip()[:MAX_TITLE_CHARS]
        topics_yaml = (
            "[" + ", ".join(matched_topics) + "]"
            if matched_topics
            else "[]"
        )
        score_str = (
            str(item.score) if item.score is not None else "null"
        )
        published = item.published_at or "unknown"
        ingested = item.fetched_at
        score_reason = (item.score_reason or "").replace("\n", " ").strip()
        frontmatter = (
            "---\n"
            f"intel_id: {item.id}\n"
            f"title: {self._yaml_escape(title)}\n"
            f"source_slug: {source.slug}\n"
            f"source_kind: {source.kind}\n"
            f"source_label: {self._yaml_escape(source.label)}\n"
            f"url: {item.url}\n"
            f"canonical_url: {item.canonical_url}\n"
            f"published_at: {published}\n"
            f"ingested_at: {ingested}\n"
            f"score: {score_str}\n"
            f"priority: {source.default_priority}\n"
            f"topics: {topics_yaml}\n"
            f"project: {source.project_slug or 'null'}\n"
            f"status: {item.status}\n"
            f"content_hash: {item.content_hash}\n"
            "---\n\n"
        )
        body_section = (
            "## Body\n\n"
            f"{body_block}\n\n"
            if body_block
            else "## Body\n\n_No body content from feed._\n\n"
        )
        excerpt_section = ""
        if excerpt and excerpt != body_block:
            quoted = "\n".join("> " + line for line in excerpt.splitlines())
            excerpt_section = f"## Raw excerpt\n\n{quoted}\n\n"

        provenance = (
            "## Provenance\n\n"
            f"- Source: [{source.label}]({source.url}) "
            f"({source.kind})\n"
            f"- Item URL: {item.url}\n"
            f"- Canonical URL: {item.canonical_url}\n"
            f"- Published: {published}\n"
            f"- Ingested: {ingested}\n"
            f"- Content hash (sha256): `{item.content_hash}`\n"
        )
        if score_reason:
            provenance += f"- Score reason: {score_reason}\n"
        if matched_topics:
            provenance += (
                "- Matched topics: " + ", ".join(matched_topics) + "\n"
            )

        return (
            f"{frontmatter}"
            f"# {title}\n\n"
            f"**Source:** [{source.label}]({item.url})  "
            f"·  **Published:** {published}  "
            f"·  **Score:** {score_str}\n\n"
            f"{body_section}"
            f"{excerpt_section}"
            f"{provenance}"
        )

    @staticmethod
    def _yaml_escape(value: str) -> str:
        # Use a quoted scalar when the value contains any character
        # that breaks YAML parsing in the simple-style we emit. Cheap
        # enough to apply unconditionally for titles + labels.
        if not value:
            return "''"
        if any(c in value for c in (":", "#", "'", '"', "\n", "\\")):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        return value

    def _world_root_for(self, source: SourceSpec) -> Path:
        """Pick the brain root for a source.

        - ``project_slug=None`` (or empty / whitespace) →
          global ``<vault>/world/`` root (Batch 2 behaviour).
        - ``project_slug`` matching the slug pattern →
          ``<vault>/projects/<slug>/world/`` (per-project root).
        - Anything else (malformed slug, path-escape attempt, etc.)
          → fall back to the global root and log a warning. Better
          to land in the right vault under the wrong folder than to
          drop the operator's data on the floor or write somewhere
          unexpected.
        """
        slug = (source.project_slug or "").strip().lower()
        if not slug:
            return self._world_root
        if not _PROJECT_SLUG_RE.match(slug):
            log.warning(
                "intel_brain_writer_invalid_project_slug",
                source_id=source.id,
                project_slug=source.project_slug,
            )
            return self._world_root
        return self._vault.root / "projects" / slug / "world"

    @staticmethod
    def _date_part(item: IntelItem) -> str:
        # Use the published date when available so back-filled feeds
        # land in their natural day; fall back to fetched_at so even
        # publish-less items always have a folder.
        for candidate in (item.published_at, item.fetched_at):
            if not candidate:
                continue
            try:
                # Tolerate trailing 'Z' and missing TZ.
                ts = candidate.replace("Z", "+00:00")
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return datetime.now(UTC).strftime("%Y-%m-%d")

    @staticmethod
    def _slugify(title: str) -> str:
        # ASCII-only, lowercase, hyphen-separated. Caps length so
        # filenames stay reasonable on every filesystem.
        if not title or not title.strip():
            return "untitled"
        s = title.strip().lower()
        s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
        s = re.sub(r"[\s_]+", "-", s)
        s = re.sub(r"-+", "-", s).strip("-")
        if not s:
            return "untitled"
        return s[:MAX_SLUG_CHARS]

    @staticmethod
    def _next_available_path(folder: Path, slug: str) -> Path:
        """Pick the first ``slug.md`` / ``slug-2.md`` / ``slug-3.md``
        path that doesn't exist. Avoids overwrites without consulting
        the SQLite layer."""
        candidate = folder / f"{slug}.md"
        if not candidate.exists():
            return candidate
        for n in range(2, 100):
            candidate = folder / f"{slug}-{n}.md"
            if not candidate.exists():
                return candidate
        # Astronomically unlikely fall-through — append a timestamp
        # suffix so we still don't overwrite anything.
        suffix = datetime.now(UTC).strftime("%H%M%S")
        return folder / f"{slug}-{suffix}.md"


__all__ = ["BrainWriter", "WriteResult"]
