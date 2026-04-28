from __future__ import annotations

from pathlib import Path

from core.orchestrator.orchestrator import ChatAttachment, Orchestrator


def _image_attachment() -> ChatAttachment:
    return ChatAttachment(
        id="a1",
        kind="image",
        mime="image/png",
        filename="x.png",
        path=Path("/tmp/x.png"),
    )


def test_tool_capable_routing_detects_browser_signup_task() -> None:
    goal = "Go sign up for Trello and complete OAuth setup"
    assert Orchestrator._needs_tool_capable_execution(goal, []) is True


def test_tool_capable_routing_keeps_plain_chat_on_subscription() -> None:
    goal = "Give me a short pep talk for today"
    assert Orchestrator._needs_tool_capable_execution(goal, []) is False


def test_tool_capable_routing_flags_attachments_as_tool_capable() -> None:
    assert (
        Orchestrator._needs_tool_capable_execution(
            "What is in this image?",
            [_image_attachment()],
        )
        is True
    )

