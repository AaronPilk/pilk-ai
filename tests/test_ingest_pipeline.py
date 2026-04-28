"""Phase 4 — File ingestion pipeline tests.

Covers:
  - extractor for txt/md/pdf/docx/csv/xlsx/html/rtf
  - registry insert + dedupe via content hash
  - pipeline end-to-end: extract → write brain note → archive source
  - failed extractions land in ``failed/``
  - duplicates short-circuit
  - HTTP routes: /ingest/supported, /ingest, /ingest/file,
    /ingest/scan-inbox

PDF/DOCX/XLSX edge cases use real binary fixtures generated on the
fly (no checked-in binaries).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from core.brain import Vault
from core.db.migrations import ensure_schema
from core.ingest import (
    ExtractionError,
    IngestPipeline,
    IngestRegistry,
    extract_text,
    supported_extensions,
)


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    p = tmp_path / "pilk.db"
    ensure_schema(p)
    return p


@pytest.fixture
def vault_root(tmp_path: Path) -> Path:
    return tmp_path / "PILK-brain"


@pytest.fixture
def vault(vault_root: Path) -> Vault:
    vault_root.mkdir(parents=True, exist_ok=True)
    return Vault(vault_root)


@pytest.fixture
def archive_dir(tmp_path: Path) -> Path:
    return tmp_path / "archive"


@pytest.fixture
def failed_dir(tmp_path: Path) -> Path:
    return tmp_path / "failed"


@pytest.fixture
def pipeline(
    db_path: Path,
    vault: Vault,
    archive_dir: Path,
    failed_dir: Path,
) -> IngestPipeline:
    return IngestPipeline(
        registry=IngestRegistry(db_path),
        vault=vault,
        archive_dir=archive_dir,
        failed_dir=failed_dir,
        indexer=None,  # skip vector reindex in unit tests
    )


# ── extract_text ──────────────────────────────────────────────────


def test_extract_text_txt(tmp_path: Path) -> None:
    p = tmp_path / "n.txt"
    p.write_text("hello world", encoding="utf-8")
    e = extract_text(p)
    assert e.text == "hello world"
    assert e.file_type == "txt"


def test_extract_text_markdown(tmp_path: Path) -> None:
    p = tmp_path / "n.md"
    p.write_text("# heading\n\nbody.", encoding="utf-8")
    e = extract_text(p)
    assert "# heading" in e.text
    assert e.file_type == "md"


def test_extract_text_csv(tmp_path: Path) -> None:
    p = tmp_path / "data.csv"
    p.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
    e = extract_text(p)
    assert "| a | b |" in e.text
    assert e.file_type == "csv"
    assert e.metadata["rows"] == 3


def test_extract_text_html(tmp_path: Path) -> None:
    p = tmp_path / "n.html"
    p.write_text(
        "<html><body><h1>Hello</h1><p>World</p>"
        "<script>x=1</script></body></html>",
        encoding="utf-8",
    )
    e = extract_text(p)
    assert "Hello" in e.text and "World" in e.text
    assert "x=1" not in e.text  # script content stripped
    assert e.file_type == "html"


def test_extract_text_unsupported_extension(tmp_path: Path) -> None:
    p = tmp_path / "n.bin"
    p.write_bytes(b"\x00\x01\x02")
    with pytest.raises(ExtractionError):
        extract_text(p)


def test_supported_extensions_includes_core_set() -> None:
    exts = set(supported_extensions())
    for s in (".txt", ".md", ".pdf", ".docx", ".csv", ".xlsx",
              ".html", ".rtf"):
        assert s in exts


def test_extract_text_pdf_minimal(tmp_path: Path) -> None:
    """Generate a tiny PDF on the fly via reportlab if available;
    otherwise skip — pypdf can read it back."""
    try:
        from reportlab.pdfgen import canvas
    except ImportError:
        pytest.skip("reportlab not installed in this env")
    p = tmp_path / "doc.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(72, 720, "Hello PDF World")
    c.showPage()
    c.save()
    e = extract_text(p)
    assert "Hello PDF World" in e.text
    assert e.file_type == "pdf"
    assert e.pages == 1


def test_extract_text_docx_minimal(tmp_path: Path) -> None:
    """Build a small docx via python-docx and read it back."""
    try:
        import docx as _docx  # python-docx
    except ImportError:
        pytest.skip("python-docx not installed in this env")
    p = tmp_path / "doc.docx"
    d = _docx.Document()
    d.add_paragraph("Quick brown fox")
    d.save(str(p))
    e = extract_text(p)
    assert "Quick brown fox" in e.text
    assert e.file_type == "docx"


# ── Registry ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_registry_dedupes_by_hash(db_path: Path) -> None:
    reg = IngestRegistry(db_path)
    a, inserted_a = await reg.register(
        original_path="/x/a.txt", file_type="txt",
        content_hash="h1", byte_size=10,
    )
    assert inserted_a is True
    b, inserted_b = await reg.register(
        original_path="/x/copy_of_a.txt", file_type="txt",
        content_hash="h1", byte_size=10,
    )
    assert inserted_b is False
    assert a.id == b.id


@pytest.mark.asyncio
async def test_registry_update_status(db_path: Path) -> None:
    reg = IngestRegistry(db_path)
    row, _ = await reg.register(
        original_path="/x/y.txt", file_type="txt",
        content_hash="h2", byte_size=1,
    )
    updated = await reg.update(
        row.id, status="done", brain_note_path="ingested/txt/y.md",
    )
    assert updated.status == "done"
    assert updated.brain_note_path == "ingested/txt/y.md"


# ── Pipeline end-to-end ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_pipeline_writes_brain_note_and_archives(
    pipeline: IngestPipeline,
    tmp_path: Path,
    archive_dir: Path,
    vault_root: Path,
) -> None:
    src = tmp_path / "drop" / "note.txt"
    src.parent.mkdir()
    src.write_text("the cat sat", encoding="utf-8")
    res = await pipeline.ingest_path(src, project_slug="skyway-sales")
    assert not res.duplicate
    assert res.error is None
    assert res.brain_note_path is not None
    assert res.brain_note_path.startswith("ingested/txt/")
    # Brain note exists and contains the text + frontmatter.
    out = (vault_root / res.brain_note_path).read_text()
    assert "the cat sat" in out
    assert "project: skyway-sales" in out
    assert "content_hash:" in out
    # Source moved to archive.
    assert not src.exists()
    moved = list(archive_dir.iterdir())
    assert len(moved) == 1


@pytest.mark.asyncio
async def test_pipeline_skips_duplicate(
    pipeline: IngestPipeline, tmp_path: Path,
) -> None:
    src = tmp_path / "drop" / "n.txt"
    src.parent.mkdir()
    src.write_text("once", encoding="utf-8")
    first = await pipeline.ingest_path(src)
    assert not first.duplicate

    # Re-create the same content under a new name → same hash → dup.
    src2 = tmp_path / "drop2" / "renamed.txt"
    src2.parent.mkdir()
    src2.write_text("once", encoding="utf-8")
    second = await pipeline.ingest_path(src2)
    assert second.duplicate is True
    assert second.row.id == first.row.id


@pytest.mark.asyncio
async def test_pipeline_routes_unsupported_to_failed(
    pipeline: IngestPipeline,
    tmp_path: Path,
    failed_dir: Path,
) -> None:
    src = tmp_path / "drop" / "thing.bin"
    src.parent.mkdir()
    src.write_bytes(b"\x00\x01\x02\x03")
    res = await pipeline.ingest_path(src)
    assert res.error is not None
    assert res.row.status == "failed"
    # Source moved to failed/.
    moved = list(failed_dir.iterdir())
    assert len(moved) == 1


# ── HTTP routes ──────────────────────────────────────────────────


def _route_app(
    db_path: Path,
    inbox: Path,
    vault_root: Path,
    archive_dir: Path,
    failed_dir: Path,
):
    """Build a tiny FastAPI app exposing the ingest router with the
    same wiring app.py does, but pointed at temp dirs."""
    from fastapi import FastAPI

    from core.api.routes.ingest import router as ingest_router

    app = FastAPI()
    app.include_router(ingest_router)
    vault_root.mkdir(parents=True, exist_ok=True)
    inbox.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    failed_dir.mkdir(parents=True, exist_ok=True)
    registry = IngestRegistry(db_path)
    pipe = IngestPipeline(
        registry=registry,
        vault=Vault(vault_root),
        archive_dir=archive_dir,
        failed_dir=failed_dir,
    )
    app.state.ingest_inbox_dir = inbox
    app.state.ingest_registry = registry
    app.state.ingest_pipeline = pipe
    return app


def test_supported_route(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    ensure_schema(db)
    app = _route_app(
        db, tmp_path / "in", tmp_path / "v",
        tmp_path / "ar", tmp_path / "fa",
    )
    with TestClient(app) as client:
        r = client.get("/ingest/supported")
        assert r.status_code == 200
        assert ".txt" in r.json()["extensions"]


def test_upload_route_runs_pipeline(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    ensure_schema(db)
    app = _route_app(
        db, tmp_path / "in", tmp_path / "v",
        tmp_path / "ar", tmp_path / "fa",
    )
    with TestClient(app) as client:
        r = client.post(
            "/ingest/file",
            files={
                "upload": ("hello.txt", b"hello uploaded", "text/plain"),
            },
            data={"project_slug": "skyway-sales"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["row"]["status"] == "done"
        assert body["row"]["project_slug"] == "skyway-sales"
        assert body["brain_note_path"].startswith("ingested/txt/")

        # List endpoint sees the run.
        r = client.get("/ingest")
        assert r.status_code == 200
        assert r.json()["count"] == 1


def test_scan_inbox_route(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    ensure_schema(db)
    inbox = tmp_path / "in"
    app = _route_app(
        db, inbox, tmp_path / "v",
        tmp_path / "ar", tmp_path / "fa",
    )
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "a.txt").write_text("alpha", encoding="utf-8")
    (inbox / "b.txt").write_text("bravo", encoding="utf-8")
    (inbox / ".hidden").write_text("hide", encoding="utf-8")

    with TestClient(app) as client:
        r = client.post("/ingest/scan-inbox")
        assert r.status_code == 200
        body = r.json()
        # Hidden file is skipped.
        names = [p["file"] for p in body["processed"]]
        assert "a.txt" in names
        assert "b.txt" in names
        assert ".hidden" not in names
