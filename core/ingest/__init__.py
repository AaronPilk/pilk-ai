"""File ingestion pipeline.

Operator-pulled by default. Drops files into ``~/PILK/inbox/`` (or
uploads via ``POST /ingest/file``) → extract text → classify →
write a brain note → vector-index it → record in ``ingested_files``.

Existing markdown files in the brain vault are NEVER deleted or
rewritten by this pipeline. Source files are moved to a safe
archive under ``~/PILK/inbox/archive/`` after success and to
``~/PILK/inbox/failed/`` on failure with error metadata.

Modules:
  ``extract``  — text extraction for txt/md/pdf/docx/csv/xlsx
  ``registry`` — DB ops over ``ingested_files``
  ``pipeline`` — orchestrates extract → brain → vector
  ``watcher``  — optional polling watcher (disabled by default)
"""

from core.ingest.extract import (
    ExtractedText,
    ExtractionError,
    extract_text,
    supported_extensions,
)
from core.ingest.pipeline import (
    IngestPipeline,
    IngestResult,
)
from core.ingest.registry import (
    IngestRegistry,
    IngestRow,
)

__all__ = [
    "ExtractedText",
    "ExtractionError",
    "IngestPipeline",
    "IngestRegistry",
    "IngestResult",
    "IngestRow",
    "extract_text",
    "supported_extensions",
]
