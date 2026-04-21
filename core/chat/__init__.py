"""Chat subpackage — currently just the attachment store for PR #83.

Kept separate from the orchestrator so the upload endpoint can live
alongside a thin dataclass + disk-backed store without pulling the
whole chat loop into import order.
"""

from core.chat.attachments import (
    Attachment,
    AttachmentError,
    AttachmentStore,
    attachment_kind_from_mime,
    is_allowed_mime,
)

__all__ = [
    "Attachment",
    "AttachmentError",
    "AttachmentStore",
    "attachment_kind_from_mime",
    "is_allowed_mime",
]
