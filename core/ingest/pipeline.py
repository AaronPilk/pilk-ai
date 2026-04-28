"""Ingestion pipeline — extract → write brain note → re-index.

End-to-end flow:

  1. Caller passes a ``Path`` (already on disk somewhere).
  2. We hash the bytes; if a row with that hash exists in
     ``ingested_files`` we short-circuit with ``duplicate=True``.
  3. Extract text via ``core.ingest.extract.extract_text``.
  4. Write a clean markdown note under
     ``~/PILK-brain/ingested/<file-type>/<slugged-stem>.md``
     with YAML frontmatter (source, ingested_at, file_type,
     hash, project_slug if any).
  5. Optionally trigger a vector re-index of the new note.
  6. Move the source file to ``inbox/archive/`` (success) or
     ``inbox/failed/`` (extraction error).
  7. Update the ``ingested_files`` row throughout.

Existing markdown files in the brain vault are NEVER deleted or
rewritten. We only ever ADD new notes under ``ingested/<type>/``.

Summarisation is opt-in per-call (``summarize=True`` triggers a
short LLM call against ``llm_ask``). It's off in this batch's
default path — the user spec says "Summarize if model available"
but we don't gate the pipeline on it.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.brain import Vault
from core.ingest.extract import (
    ExtractedText,
    ExtractionError,
    extract_text,
)
from core.ingest.registry import IngestRegistry, IngestRow
from core.logging import get_logger

log = get_logger("pilkd.ingest.pipeline")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(s: str, *, fallback: str = "untitled") -> str:
    cleaned = _SLUG_RE.sub("-", s.lower()).strip("-")
    return cleaned or fallback


@dataclass
class IngestResult:
    row: IngestRow
    duplicate: bool
    error: str | None = None
    brain_note_path: str | None = None


class IngestPipeline:
    """Run a single file through the pipeline. Stateless — caller
    holds references to the registry, vault, and (optionally) an
    indexer instance."""

    def __init__(
        self,
        *,
        registry: IngestRegistry,
        vault: Vault,
        archive_dir: Path,
        failed_dir: Path,
        indexer: Any | None = None,  # core.brain.vector.Indexer
    ) -> None:
        self.registry = registry
        self.vault = vault
        self.archive_dir = Path(archive_dir)
        self.failed_dir = Path(failed_dir)
        self.indexer = indexer
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.failed_dir.mkdir(parents=True, exist_ok=True)

    async def ingest_path(
        self,
        source_path: Path,
        *,
        project_slug: str | None = None,
        move_after: bool = True,
        reindex: bool = True,
    ) -> IngestResult:
        if not source_path.exists():
            raise FileNotFoundError(str(source_path))
        try:
            data = source_path.read_bytes()
        except OSError as e:
            raise IOError(f"could not read {source_path}: {e}") from e
        digest = hashlib.sha256(data).hexdigest()
        size = len(data)
        suf = source_path.suffix.lower().lstrip(".") or "bin"
        row, inserted = await self.registry.register(
            original_path=str(source_path),
            file_type=suf,
            content_hash=digest,
            byte_size=size,
            project_slug=project_slug,
            metadata={
                "ingested_via": "pipeline",
            },
        )
        if not inserted:
            log.info(
                "ingest_duplicate_skipped",
                hash=digest, original=str(source_path),
            )
            return IngestResult(
                row=row,
                duplicate=True,
                brain_note_path=row.brain_note_path,
            )
        try:
            await self.registry.update(row.id, status="extracting")
            extracted = extract_text(source_path)
        except ExtractionError as e:
            log.warning(
                "ingest_extraction_failed",
                file=str(source_path), error=str(e),
            )
            await self.registry.update(
                row.id, status="failed", error=str(e),
            )
            stored = self._move(source_path, self.failed_dir)
            await self.registry.update(
                row.id, stored_path=str(stored),
            )
            row = await self.registry.get(row.id) or row
            return IngestResult(row=row, duplicate=False, error=str(e))
        except Exception as e:  # noqa: BLE001 — defensive
            log.warning(
                "ingest_unexpected_failure",
                file=str(source_path), error=str(e),
            )
            await self.registry.update(
                row.id, status="failed", error=f"unexpected: {e}",
            )
            row = await self.registry.get(row.id) or row
            return IngestResult(row=row, duplicate=False, error=str(e))
        # Write the brain note.
        try:
            note_path = self._compose_note(
                source_path=source_path,
                project_slug=project_slug,
                extracted=extracted,
                content_hash=digest,
            )
            await self.registry.update(
                row.id,
                status="indexing",
                brain_note_path=note_path,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("ingest_brain_write_failed", error=str(e))
            await self.registry.update(
                row.id, status="failed",
                error=f"brain_write: {e}",
            )
            stored = self._move(source_path, self.failed_dir)
            await self.registry.update(
                row.id, stored_path=str(stored),
            )
            row = await self.registry.get(row.id) or row
            return IngestResult(row=row, duplicate=False, error=str(e))
        # Re-index the new note (best-effort).
        if reindex and self.indexer is not None:
            try:
                await self.indexer.index_all()
            except Exception as e:  # noqa: BLE001
                log.warning("ingest_reindex_failed", error=str(e))
        # Move source to archive.
        if move_after:
            try:
                stored = self._move(source_path, self.archive_dir)
                await self.registry.update(
                    row.id, stored_path=str(stored),
                )
            except Exception as e:  # noqa: BLE001
                log.warning("ingest_archive_move_failed", error=str(e))
        await self.registry.update(row.id, status="done")
        row = await self.registry.get(row.id) or row
        return IngestResult(
            row=row,
            duplicate=False,
            brain_note_path=note_path,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _move(self, src: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        target = dest_dir / src.name
        # If the target exists, suffix with a counter so we don't
        # silently overwrite a previously archived file.
        if target.exists():
            stem, suffix = target.stem, target.suffix
            for i in range(1, 1000):
                alt = dest_dir / f"{stem}-{i}{suffix}"
                if not alt.exists():
                    target = alt
                    break
        shutil.move(str(src), str(target))
        return target

    def _compose_note(
        self,
        *,
        source_path: Path,
        project_slug: str | None,
        extracted: ExtractedText,
        content_hash: str,
    ) -> str:
        """Write the markdown note and return its brain-relative
        path. We always write under ``ingested/<file_type>/`` so
        the existing markdown brain layout stays sane."""
        when = datetime.now(UTC).strftime("%Y-%m-%d")
        stem = _slug(source_path.stem)
        rel = f"ingested/{extracted.file_type}/{when}/{stem}.md"
        # Avoid clobbering an existing note for the same hash.
        i = 1
        while self.vault.resolve(rel).exists():
            rel = (
                f"ingested/{extracted.file_type}/{when}/{stem}-{i}.md"
            )
            i += 1
        # YAML frontmatter so search + filters can work off
        # structured fields in addition to the body.
        frontmatter = (
            "---\n"
            f"source_path: {source_path.name}\n"
            f"original_path: {str(source_path)}\n"
            f"file_type: {extracted.file_type}\n"
            f"content_hash: {content_hash}\n"
            f"ingested_at: {datetime.now(UTC).isoformat()}\n"
        )
        if project_slug:
            frontmatter += f"project: {project_slug}\n"
        if extracted.pages is not None:
            frontmatter += f"pages: {extracted.pages}\n"
        if extracted.metadata:
            for k, v in extracted.metadata.items():
                frontmatter += f"{k}: {v}\n"
        frontmatter += "---\n\n"
        title = source_path.stem
        body = (
            frontmatter
            + f"# {title}\n\n"
            + extracted.text.strip()
            + "\n"
        )
        self.vault.write(rel, body)
        return rel


__all__ = ["IngestPipeline", "IngestResult"]
