"""Unit tests for the ingester pure functions + the two
brain_ingest_* tools wired on a tmp_path-backed vault.

We pointedly DON'T spin up a real ~/.claude directory — the Claude
Code scanner takes a root path, so the tests hand it a tmp_path root
seeded with fake JSONL files. That keeps the tests isolated + fast."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from core.brain import Vault
from core.integrations.ingesters.chatgpt import (
    ChatGPTIngestError,
    parse_export,
    render_conversation_note,
)
from core.integrations.ingesters.claude_code import (
    render_project_note,
    scan_projects,
)
from core.tools.builtin.brain_ingest import make_brain_ingest_tools
from core.tools.registry import ToolContext


@pytest.fixture
def vault(tmp_path: Path) -> Vault:
    v = Vault(tmp_path / "brain")
    v.ensure_initialized()
    return v


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    (tmp_path / "workspace").mkdir()
    return ToolContext(sandbox_root=tmp_path / "workspace")


# ── Claude Code ingester ────────────────────────────────────────


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines),
        encoding="utf-8",
    )


def test_scan_projects_empty_root(tmp_path: Path) -> None:
    assert scan_projects(tmp_path / "does-not-exist") == []
    assert scan_projects(tmp_path / "empty") == []  # missing still fine


def test_scan_projects_aggregates_sessions(tmp_path: Path) -> None:
    """Each project dir should produce one aggregate with all its
    .jsonl sessions — no matter how many."""
    root = tmp_path / "projects"
    proj = root / "-Users-aaron-pilk-ai"
    _write_jsonl(
        proj / "session-a.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "Hello pilk"},
                "timestamp": "2026-04-01T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Hi — what's up?"},
                    ]
                },
                "timestamp": "2026-04-01T10:00:10Z",
            },
        ],
    )
    _write_jsonl(
        proj / "session-b.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "Ship the roadmap"},
                "timestamp": "2026-04-15T10:00:00Z",
            },
            # System messages are skipped.
            {"type": "system", "message": {"content": "boot"}},
            {
                "type": "assistant",
                "message": {"content": "On it."},
                "timestamp": "2026-04-15T10:00:10Z",
            },
        ],
    )
    projects = scan_projects(root)
    assert len(projects) == 1
    p = projects[0]
    assert p.slug == "-Users-aaron-pilk-ai"
    assert len(p.sessions) == 2
    # Newest session first.
    assert p.sessions[0].session_id == "session-b"


def test_scan_projects_tolerates_bad_lines(tmp_path: Path) -> None:
    """Garbage lines + unknown types must not kill the session — we
    skip them and keep going."""
    root = tmp_path / "projects"
    proj = root / "dirty"
    proj.mkdir(parents=True)
    (proj / "s.jsonl").write_text(
        "not-json\n"
        + json.dumps({"type": "user", "message": {"content": "one"}}) + "\n"
        + "{\"bogus\"}\n"
        + json.dumps({"type": "unknown", "message": {"content": "x"}}) + "\n"
        + json.dumps({"type": "assistant", "message": {"content": "two"}}),
        encoding="utf-8",
    )
    projects = scan_projects(root)
    # Session has exactly the 2 valid user+assistant turns.
    assert len(projects) == 1
    assert len(projects[0].sessions) == 1
    turns = projects[0].sessions[0].turns
    assert [t.role for t in turns] == ["user", "assistant"]


def test_scan_projects_skips_empty_dirs(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    (root / "empty-project").mkdir(parents=True)
    assert scan_projects(root) == []


def test_render_project_note_shape(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    proj = root / "-Users-aaron-brand"
    _write_jsonl(
        proj / "s.jsonl",
        [
            {
                "type": "user",
                "message": {"content": "hi"},
                "timestamp": "2026-04-01T10:00:00Z",
            },
            {
                "type": "assistant",
                "message": {"content": "ack"},
                "timestamp": "2026-04-01T10:00:01Z",
            },
        ],
    )
    projects = scan_projects(root)
    note = render_project_note(projects[0])
    # Path stem matches the last path segment, slug-ified.
    assert note.path == "ingested/claude-code/brand.md"
    assert note.title == "brand"
    assert "Claude Code project" in note.body
    # Both user + assistant headings are present.
    assert "### User" in note.body
    assert "### Assistant" in note.body


def test_render_project_note_coerces_content_blocks(tmp_path: Path) -> None:
    """Content-block lists should flatten to text; tool_use entries
    should NOT expand their arguments but should leave a breadcrumb."""
    root = tmp_path / "projects"
    proj = root / "blocks"
    _write_jsonl(
        proj / "s.jsonl",
        [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Running a scan"},
                        {"type": "tool_use", "name": "fs_read"},
                        {"type": "text", "text": "Done."},
                    ],
                },
            },
        ],
    )
    projects = scan_projects(root)
    note = render_project_note(projects[0])
    assert "Running a scan" in note.body
    assert "fs_read" in note.body
    assert "Done." in note.body


# ── ChatGPT ingester ────────────────────────────────────────────


def _make_export_json(convs: list[dict]) -> bytes:
    return json.dumps(convs).encode("utf-8")


def _make_export_zip(tmp_path: Path, convs: list[dict]) -> Path:
    out = tmp_path / "chatgpt-export.zip"
    with zipfile.ZipFile(out, "w") as zf:
        zf.writestr("conversations.json", _make_export_json(convs))
    return out


def _conv(id_: str, title: str, mapping: dict, ts: float = 1_700_000_000.0):
    return {
        "id": id_,
        "title": title,
        "create_time": ts,
        "update_time": ts + 60,
        "mapping": mapping,
    }


def test_parse_export_reads_zip(tmp_path: Path) -> None:
    mapping = {
        "n1": {
            "id": "n1", "parent": None, "children": ["n2"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["hello"]},
                "create_time": 1,
            },
        },
        "n2": {
            "id": "n2", "parent": "n1", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["world"]},
                "create_time": 2,
            },
        },
    }
    zip_path = _make_export_zip(
        tmp_path, [_conv("abc", "Test conv", mapping)],
    )
    convs = parse_export(zip_path)
    assert len(convs) == 1
    assert [t.role for t in convs[0].turns] == ["user", "assistant"]
    assert convs[0].turns[0].text == "hello"
    assert convs[0].turns[1].text == "world"


def test_parse_export_reads_raw_json(tmp_path: Path) -> None:
    mapping = {
        "n1": {
            "id": "n1", "parent": None, "children": [],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["only"]},
                "create_time": 1,
            },
        },
    }
    p = tmp_path / "conversations.json"
    p.write_bytes(_make_export_json([_conv("x", "Just one", mapping)]))
    assert len(parse_export(p)) == 1


def test_parse_export_picks_newest_branch(tmp_path: Path) -> None:
    """If a node has multiple children, we walk the branch with the
    LATEST create_time — that's the branch the operator saw."""
    mapping = {
        "root": {
            "id": "root", "parent": None, "children": ["a", "b"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["prompt"]},
                "create_time": 1,
            },
        },
        "a": {  # older retry branch
            "id": "a", "parent": "root", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["old-answer"]},
                "create_time": 2,
            },
        },
        "b": {  # newer, picked branch
            "id": "b", "parent": "root", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["fresh-answer"]},
                "create_time": 9,
            },
        },
    }
    p = tmp_path / "c.json"
    p.write_bytes(_make_export_json([_conv("x", "branched", mapping)]))
    convs = parse_export(p)
    assert len(convs[0].turns) == 2
    assert convs[0].turns[1].text == "fresh-answer"


def test_parse_export_skips_empty_and_system(tmp_path: Path) -> None:
    mapping = {
        "r": {
            "id": "r", "parent": None, "children": ["sys"],
            "message": {
                "author": {"role": "system"},
                "content": {"parts": [""]},
                "create_time": 1,
            },
        },
        "sys": {
            "id": "sys", "parent": "r", "children": [],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["real"]},
                "create_time": 2,
            },
        },
    }
    p = tmp_path / "c.json"
    p.write_bytes(_make_export_json([_conv("x", "mixed", mapping)]))
    convs = parse_export(p)
    # Only the user turn lands — system is skipped.
    assert len(convs[0].turns) == 1
    assert convs[0].turns[0].role == "user"


def test_parse_export_rejects_bad_zip(tmp_path: Path) -> None:
    bogus = tmp_path / "x.zip"
    bogus.write_bytes(b"not a zip")
    with pytest.raises(ChatGPTIngestError):
        parse_export(bogus)


def test_parse_export_reads_sharded_zip(tmp_path: Path) -> None:
    """New-format ChatGPT exports split the conversation list across
    ``conversations-000.json``, ``conversations-001.json``, …. We
    merge the shards in numeric order rather than failing."""
    def _mk(cid: str, text: str, ts: float) -> dict:
        return _conv(
            cid,
            f"conv-{cid}",
            {
                "n1": {
                    "id": "n1", "parent": None, "children": [],
                    "message": {
                        "author": {"role": "user"},
                        "content": {"parts": [text]},
                        "create_time": ts,
                    },
                },
            },
            ts=ts,
        )

    out = tmp_path / "sharded-export.zip"
    with zipfile.ZipFile(out, "w") as zf:
        # Deliberately write shard 1 before shard 0 to prove we sort.
        zf.writestr(
            "conversations-001.json",
            _make_export_json([_mk("b", "second", 1_700_000_200.0)]),
        )
        zf.writestr(
            "conversations-000.json",
            _make_export_json([_mk("a", "first", 1_700_000_100.0)]),
        )
        # Random sibling file that must NOT be picked up.
        zf.writestr("user.json", b"{}")

    convs = parse_export(out)
    # Both shards merged — ordering is by updated_at DESC (parse_export
    # sorts newest-first), so the shard-1 conversation comes first.
    assert [c.conversation_id for c in convs] == ["b", "a"]
    assert {c.turns[0].text for c in convs} == {"first", "second"}


def test_parse_export_rejects_zip_without_conversations(tmp_path: Path) -> None:
    """A zip that contains neither ``conversations.json`` nor any
    ``conversations-NNN.json`` shard raises with a sample of what it
    actually had, so the operator can tell which file they picked."""
    bogus = tmp_path / "not-chatgpt.zip"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("README.md", b"hi")
        zf.writestr("data.csv", b"a,b,c")
    with pytest.raises(ChatGPTIngestError, match="saw: "):
        parse_export(bogus)


def test_render_conversation_note_path_shape(tmp_path: Path) -> None:
    """The filename stem encodes the update_time + a sanitised title,
    so Obsidian's file browser sorts by date naturally."""
    mapping = {
        "n": {
            "id": "n", "parent": None, "children": [],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["hi"]},
                "create_time": 1_707_000_000,
            },
        },
    }
    p = tmp_path / "c.json"
    p.write_bytes(_make_export_json(
        [_conv("x", "Meta <-> Google comparison!", mapping, ts=1_707_000_000)]
    ))
    convs = parse_export(p)
    note = render_conversation_note(convs[0])
    assert note.path.startswith("ingested/chatgpt/2024-02-")
    assert note.path.endswith(".md")
    # Sanitisation removed the < and >.
    assert "<" not in note.path
    assert ">" not in note.path


# ── tool layer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_claude_code_tool_writes_to_vault(
    vault: Vault, ctx: ToolContext, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Point DEFAULT_ROOT at a tmp projects tree.
    root = tmp_path / "claude-projects"
    proj = root / "-Users-aaron-foo"
    _write_jsonl(
        proj / "s.jsonl",
        [
            {"type": "user", "message": {"content": "q"}},
            {"type": "assistant", "message": {"content": "a"}},
        ],
    )
    import core.tools.builtin.brain_ingest as mod
    monkeypatch.setattr(mod, "CLAUDE_DEFAULT_ROOT", root)

    tools = make_brain_ingest_tools(vault)
    claude_tool = next(t for t in tools if t.name == "brain_ingest_claude_code")
    out = await claude_tool.handler({}, ctx)
    assert not out.is_error
    assert out.data["projects_scanned"] == 1
    written = out.data["written"][0]
    assert written["path"].startswith("ingested/claude-code/")
    # Note actually landed in the vault.
    assert (vault.root / written["path"]).is_file()


@pytest.mark.asyncio
async def test_claude_code_tool_empty_root_returns_clean(
    vault: Vault, ctx: ToolContext, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.tools.builtin.brain_ingest as mod
    monkeypatch.setattr(mod, "CLAUDE_DEFAULT_ROOT", tmp_path / "empty")

    tools = make_brain_ingest_tools(vault)
    claude_tool = next(t for t in tools if t.name == "brain_ingest_claude_code")
    out = await claude_tool.handler({}, ctx)
    assert not out.is_error
    assert out.data["written"] == []
    assert "No Claude Code projects" in out.content


@pytest.mark.asyncio
async def test_chatgpt_tool_writes_to_vault(
    vault: Vault, ctx: ToolContext, tmp_path: Path,
) -> None:
    mapping = {
        "n1": {
            "id": "n1", "parent": None, "children": ["n2"],
            "message": {
                "author": {"role": "user"},
                "content": {"parts": ["draft a campaign"]},
                "create_time": 1,
            },
        },
        "n2": {
            "id": "n2", "parent": "n1", "children": [],
            "message": {
                "author": {"role": "assistant"},
                "content": {"parts": ["here's the outline…"]},
                "create_time": 2,
            },
        },
    }
    # Drop the export inside the workspace under a specific name the
    # tool call expects. _make_export_zip writes to
    # <dir>/chatgpt-export.zip — we then rename to chatgpt.zip.
    src = _make_export_zip(
        ctx.sandbox_root,
        [_conv("conv-1", "Campaign draft", mapping)],
    )
    export = ctx.sandbox_root / "chatgpt.zip"
    src.replace(export)
    assert export.is_file()
    _ = tmp_path  # used implicitly via fixtures

    tools = make_brain_ingest_tools(vault)
    gpt_tool = next(t for t in tools if t.name == "brain_ingest_chatgpt")
    out = await gpt_tool.handler({"path": "chatgpt.zip"}, ctx)
    assert not out.is_error
    assert out.data["conversations_scanned"] == 1
    written = out.data["written"][0]
    assert written["path"].startswith("ingested/chatgpt/")
    assert (vault.root / written["path"]).is_file()


@pytest.mark.asyncio
async def test_chatgpt_tool_requires_path(
    vault: Vault, ctx: ToolContext,
) -> None:
    tools = make_brain_ingest_tools(vault)
    gpt_tool = next(t for t in tools if t.name == "brain_ingest_chatgpt")
    out = await gpt_tool.handler({}, ctx)
    assert out.is_error
    assert "path" in out.content


@pytest.mark.asyncio
async def test_chatgpt_tool_rejects_workspace_escape(
    vault: Vault, ctx: ToolContext,
) -> None:
    tools = make_brain_ingest_tools(vault)
    gpt_tool = next(t for t in tools if t.name == "brain_ingest_chatgpt")
    out = await gpt_tool.handler(
        {"path": "../../etc/passwd.zip"}, ctx,
    )
    assert out.is_error
    assert "escapes workspace" in out.content


@pytest.mark.asyncio
async def test_chatgpt_tool_missing_file_hint(
    vault: Vault, ctx: ToolContext,
) -> None:
    tools = make_brain_ingest_tools(vault)
    gpt_tool = next(t for t in tools if t.name == "brain_ingest_chatgpt")
    out = await gpt_tool.handler({"path": "nope.zip"}, ctx)
    assert out.is_error
    # Hint should tell the operator where to get the export.
    assert "Export" in out.content or "export" in out.content
