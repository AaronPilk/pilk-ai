"""Heading-aware markdown chunker.

Splits a markdown note into reasonably-sized chunks while preserving
heading context. Each chunk carries the nearest heading so the
search layer can show "in section X" without re-parsing the file.

Design choices kept deliberately simple:
- One chunk = one section (or part of one) under a heading.
- Sections longer than ``chunk_chars`` are split into overlapping
  windows so a long doc produces multiple chunks but consecutive
  windows share a small tail/head overlap. Overlap ~10% of the
  target size — small enough not to bloat storage, large enough to
  avoid splitting a sentence in half across two chunks.
- We don't tokenize. The OpenAI embedder caps each input at 8192
  tokens (~32k chars); our default ``chunk_chars=2000`` is far
  below that. Text-only chars are a good enough proxy.
- YAML frontmatter and code fences are kept intact within their
  chunk — splitting them mid-fence would corrupt the embedding.

If/when this becomes a bottleneck we can swap in a smarter parser.
For now, keep it readable and correct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Chunk:
    """One unit of text that gets embedded and stored.

    ``heading`` is the most recent ATX heading in scope when the
    chunk starts, or ``None`` if the file has no headings. ``idx``
    is the zero-based ordinal within the file — paired with the
    file's ``brain_path`` it uniquely identifies the chunk."""

    idx: int
    heading: str | None
    content: str


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")


class Chunker:
    def __init__(
        self,
        *,
        chunk_chars: int = 2000,
        overlap_chars: int = 200,
        min_chunk_chars: int = 80,
    ) -> None:
        if chunk_chars <= 0:
            raise ValueError("chunk_chars must be > 0")
        if overlap_chars < 0 or overlap_chars >= chunk_chars:
            raise ValueError(
                "overlap_chars must be in [0, chunk_chars)"
            )
        self._chunk_chars = chunk_chars
        self._overlap_chars = overlap_chars
        self._min_chunk_chars = min_chunk_chars

    def chunk(self, markdown: str) -> list[Chunk]:
        """Return chunks in document order. The list may be empty
        if the input is shorter than ``min_chunk_chars``."""
        if not markdown or not markdown.strip():
            return []

        # First pass: split into sections at ATX headings while
        # tracking the heading stack. We treat the body before the
        # first heading as one section (heading=None).
        sections: list[tuple[str | None, str]] = []
        current_heading: str | None = None
        current_buf: list[str] = []
        in_code_fence = False

        for line in markdown.splitlines(keepends=True):
            stripped = line.strip()
            # Track code fences so we don't treat ``# foo`` inside a
            # python comment as a section break.
            if stripped.startswith("```"):
                in_code_fence = not in_code_fence
                current_buf.append(line)
                continue
            if not in_code_fence:
                m = _HEADING_RE.match(stripped)
                if m:
                    # Flush the previous section.
                    if current_buf:
                        sections.append(
                            (current_heading, "".join(current_buf))
                        )
                    current_heading = m.group(2).strip()
                    current_buf = [line]
                    continue
            current_buf.append(line)
        if current_buf:
            sections.append((current_heading, "".join(current_buf)))

        # Second pass: each section is split into chunks of at most
        # ``chunk_chars`` characters with a small overlap between
        # consecutive windows. Sections shorter than the limit pass
        # through unchanged.
        chunks: list[Chunk] = []
        idx = 0
        for heading, body in sections:
            body = body.rstrip()
            if len(body) < self._min_chunk_chars:
                # Tiny sections (orphan headings, single-line stubs)
                # get attached to the next chunk if possible — drop
                # them on their own to keep noise out of the index.
                if not body.strip():
                    continue
            if len(body) <= self._chunk_chars:
                chunks.append(Chunk(idx=idx, heading=heading, content=body))
                idx += 1
                continue
            # Window into the long section.
            start = 0
            while start < len(body):
                end = min(start + self._chunk_chars, len(body))
                # Try to break on a paragraph boundary close to
                # ``end`` so we don't slice mid-sentence.
                cut = body.rfind("\n\n", start, end)
                if cut == -1 or cut - start < self._chunk_chars // 2:
                    cut = end
                else:
                    cut = min(cut + 2, end)  # include the blank line
                content = body[start:cut].rstrip()
                if len(content) >= self._min_chunk_chars:
                    chunks.append(
                        Chunk(idx=idx, heading=heading, content=content)
                    )
                    idx += 1
                if cut == len(body):
                    break
                start = max(cut - self._overlap_chars, start + 1)
        return chunks
