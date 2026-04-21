"""Apple on-device integrations — Messages + Contacts.

Not OAuth. Both talk to local macOS surfaces:

- Messages.app owns ``~/Library/Messages/chat.db`` (read-only SQLite)
  and an AppleScript interface for sending. The reader needs Full
  Disk Access; the sender needs Automation permission for Messages.
- Contacts.app owns the address book and exposes read via AppleScript.
  First call prompts for Automation + Contacts permission.

Both surfaces only work when PILK runs on the Mac whose apps are
signed in. On non-macOS the tools register but every call cleanly
surfaces ``macOS-only`` rather than crashing.
"""

from core.integrations.apple.contacts import (
    ContactsSearchError,
    make_contacts_tools,
    search_contacts,
)
from core.integrations.apple.messages import (
    MessagesSendError,
    MessagesStatus,
    check_messages_status,
    make_messages_tools,
    read_thread,
    recent_threads,
    send_message,
)

__all__ = [
    "ContactsSearchError",
    "MessagesSendError",
    "MessagesStatus",
    "check_messages_status",
    "make_contacts_tools",
    "make_messages_tools",
    "read_thread",
    "recent_threads",
    "search_contacts",
    "send_message",
]
