"""Cross-cutting delivery tools — shared across agents that need to
hand work products to humans.

Today: ``agent_email_deliver``. Future candidates include Slack file
posting, PDF drop to a shared Drive folder, etc. Keeping this its own
package so the pattern stays clear — every tool in here shares the
"agent output → external destination with format enforcement" shape.
"""

from core.tools.builtin.delivery.email import (
    SUBJECT_FORMAT,
    make_agent_email_deliver_tool,
)
from core.tools.builtin.delivery.gmail_draft_best_of_n import (
    make_gmail_draft_best_of_n_tool,
)

__all__ = [
    "SUBJECT_FORMAT",
    "make_agent_email_deliver_tool",
    "make_gmail_draft_best_of_n_tool",
]
