"""Apple Messages — local iMessage/SMS reader.

Not an OAuth integration. Messages.app on macOS stores threads + text
in a local SQLite database at ~/Library/Messages/chat.db. PILK opens
that file read-only and surfaces recent threads on Home. It only
works when:

- PILK runs on the user's macOS machine (the Mac that owns the
  account signed into Messages)
- The Python process has been granted Full Disk Access in
  System Settings → Privacy & Security → Full Disk Access

Read-only in v1. Sending iMessages programmatically requires
AppleScript shelling into Messages.app and is deferred.
"""

from core.integrations.apple.messages import (
    MessagesStatus,
    check_messages_status,
    make_messages_tools,
    read_thread,
    recent_threads,
)

__all__ = [
    "MessagesStatus",
    "check_messages_status",
    "make_messages_tools",
    "read_thread",
    "recent_threads",
]
