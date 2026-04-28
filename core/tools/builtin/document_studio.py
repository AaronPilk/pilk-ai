"""Document Studio — render markdown into polished deliverables.

Two tools live here:

* ``document_render_email_html`` — markdown → inline-CSS HTML safe to
  paste into an email body (Gmail, Outlook, Apple Mail). Used by Master
  Comms whenever the deliverable is a long doc that would otherwise
  arrive as a wall of unrendered markdown characters.

* ``document_render_pdf`` — markdown → PDF file saved to the active
  project's drafts folder (or a caller-specified path). Used when the
  operator wants something to forward, attach, or print.

Pitch decks go through the existing ``slides_create`` tool — there's a
dedicated section in Master Content's playbook explaining how to call
it with a structured outline. This module doesn't wrap that.

WeasyPrint depends on Pango/Cairo native libraries. On macOS those
land via ``brew install pango``. The PDF tool sets
``DYLD_FALLBACK_LIBRARY_PATH`` programmatically so weasyprint finds
the dylibs even when pilkd is launched from a non-interactive shell —
otherwise the import partially succeeds but ``write_pdf()`` blows up
the first time it tries to load the library.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Set DYLD paths BEFORE weasyprint or any of its lazy library loads —
# importing weasyprint itself doesn't trigger the dlopen, but the first
# render does. Setting at module-load time is the only reliable hook.
_HOMEBREW_LIB = "/opt/homebrew/lib"
if Path(_HOMEBREW_LIB).is_dir():
    for var in ("DYLD_FALLBACK_LIBRARY_PATH", "DYLD_LIBRARY_PATH"):
        existing = os.environ.get(var, "")
        if _HOMEBREW_LIB not in existing.split(os.pathsep):
            os.environ[var] = (
                f"{_HOMEBREW_LIB}{os.pathsep}{existing}" if existing
                else _HOMEBREW_LIB
            )

from core.logging import get_logger
from core.policy.risk import RiskClass
from core.tools.registry import Tool, ToolContext, ToolOutcome

log = get_logger("pilkd.tools.document_studio")


# ── Email-safe HTML rendering ────────────────────────────────────────


# Inline CSS used by the email renderer. Email clients (Gmail web,
# Gmail mobile, Outlook desktop, Apple Mail) only support a narrow CSS
# subset — no <style> blocks survive Gmail's sanitizer, no flexbox in
# Outlook, no CSS variables. Everything inlines onto each element.
# Conservative typography choices: system stack so it renders without
# downloads, ~600px width so Outlook doesn't blow it up, dark text on
# white background for readability across all clients.
_EMAIL_BODY_STYLE = (
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
    "Roboto,Helvetica,Arial,sans-serif;"
    "font-size:15px;line-height:1.55;color:#1a1a1a;"
    "max-width:640px;margin:0 auto;padding:24px;"
    "background:#ffffff;"
)
_EMAIL_H1_STYLE = (
    "font-size:24px;line-height:1.25;font-weight:600;"
    "margin:0 0 16px 0;color:#111;"
)
_EMAIL_H2_STYLE = (
    "font-size:19px;line-height:1.3;font-weight:600;"
    "margin:24px 0 12px 0;color:#111;"
    "padding-bottom:6px;border-bottom:1px solid #eaeaea;"
)
_EMAIL_H3_STYLE = (
    "font-size:16px;line-height:1.3;font-weight:600;"
    "margin:20px 0 8px 0;color:#222;"
)
_EMAIL_P_STYLE = "margin:0 0 14px 0;"
_EMAIL_UL_STYLE = "margin:0 0 14px 0;padding-left:22px;"
_EMAIL_LI_STYLE = "margin:0 0 6px 0;"
_EMAIL_CODE_STYLE = (
    "font-family:'SF Mono',Menlo,Monaco,Consolas,monospace;"
    "font-size:13px;background:#f5f5f5;padding:2px 5px;"
    "border-radius:3px;color:#c7254e;"
)
_EMAIL_PRE_STYLE = (
    "font-family:'SF Mono',Menlo,Monaco,Consolas,monospace;"
    "font-size:13px;background:#f5f5f5;padding:14px;"
    "border-radius:6px;overflow-x:auto;line-height:1.45;"
    "margin:0 0 14px 0;color:#1a1a1a;border:1px solid #e5e5e5;"
)
_EMAIL_BLOCKQUOTE_STYLE = (
    "margin:0 0 14px 0;padding:8px 14px;border-left:3px solid #d0d0d0;"
    "color:#555;background:#fafafa;"
)
_EMAIL_TABLE_STYLE = (
    "border-collapse:collapse;margin:0 0 14px 0;width:100%;"
)
_EMAIL_TH_STYLE = (
    "border:1px solid #e0e0e0;padding:8px 12px;background:#f5f5f5;"
    "text-align:left;font-weight:600;"
)
_EMAIL_TD_STYLE = "border:1px solid #e0e0e0;padding:8px 12px;"
_EMAIL_HR_STYLE = "border:0;border-top:1px solid #e5e5e5;margin:24px 0;"
_EMAIL_A_STYLE = "color:#7c3aed;text-decoration:underline;"


def _inline_styles(html: str) -> str:
    """Add inline ``style="..."`` attributes to every common tag.

    Lightweight, no external deps. Replaces opening tags only — closing
    tags don't carry styles. This produces email-client-safe output for
    Gmail, Outlook, and Apple Mail simultaneously.
    """
    repls = [
        ("<h1>", f'<h1 style="{_EMAIL_H1_STYLE}">'),
        ("<h2>", f'<h2 style="{_EMAIL_H2_STYLE}">'),
        ("<h3>", f'<h3 style="{_EMAIL_H3_STYLE}">'),
        ("<h4>", f'<h4 style="{_EMAIL_H3_STYLE}">'),
        ("<h5>", f'<h5 style="{_EMAIL_H3_STYLE}">'),
        ("<h6>", f'<h6 style="{_EMAIL_H3_STYLE}">'),
        ("<p>", f'<p style="{_EMAIL_P_STYLE}">'),
        ("<ul>", f'<ul style="{_EMAIL_UL_STYLE}">'),
        ("<ol>", f'<ol style="{_EMAIL_UL_STYLE}">'),
        ("<li>", f'<li style="{_EMAIL_LI_STYLE}">'),
        ("<code>", f'<code style="{_EMAIL_CODE_STYLE}">'),
        ("<pre>", f'<pre style="{_EMAIL_PRE_STYLE}">'),
        ("<blockquote>", f'<blockquote style="{_EMAIL_BLOCKQUOTE_STYLE}">'),
        ("<table>", f'<table style="{_EMAIL_TABLE_STYLE}">'),
        ("<th>", f'<th style="{_EMAIL_TH_STYLE}">'),
        ("<td>", f'<td style="{_EMAIL_TD_STYLE}">'),
        ("<hr>", f'<hr style="{_EMAIL_HR_STYLE}">'),
        ("<hr/>", f'<hr style="{_EMAIL_HR_STYLE}"/>'),
        ("<hr />", f'<hr style="{_EMAIL_HR_STYLE}"/>'),
        ("<a href=", f'<a style="{_EMAIL_A_STYLE}" href='),
    ]
    for old, new in repls:
        html = html.replace(old, new)
    return html


def _markdown_to_email_html(markdown_text: str, *, title: str | None) -> str:
    """Convert markdown source into email-ready inline-CSS HTML."""
    import markdown as md_lib

    body_html = md_lib.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists"],
        output_format="html5",
    )
    body_html = _inline_styles(body_html)
    title_block = (
        f'<title>{title}</title>'
        if title
        else ""
    )
    return (
        "<!DOCTYPE html>\n"
        f"<html><head><meta charset=\"utf-8\">{title_block}</head>\n"
        f"<body style=\"margin:0;background:#ffffff;\">"
        f"<div style=\"{_EMAIL_BODY_STYLE}\">"
        f"{body_html}"
        "</div></body></html>"
    )


def _email_html_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        markdown_text = args.get("markdown")
        if not isinstance(markdown_text, str) or not markdown_text.strip():
            return ToolOutcome(
                content=(
                    "document_render_email_html requires non-empty "
                    "'markdown' input."
                ),
                is_error=True,
            )
        title = args.get("title")
        if title is not None and not isinstance(title, str):
            title = None
        try:
            html = _markdown_to_email_html(markdown_text, title=title)
        except Exception as e:  # pragma: no cover — defensive
            log.warning("email_html_render_failed", error=str(e))
            return ToolOutcome(
                content=f"render failed: {type(e).__name__}: {e}",
                is_error=True,
            )
        return ToolOutcome(
            content=html,
            data={
                "html": html,
                "chars": len(html),
                "title": title,
            },
        )

    return Tool(
        name="document_render_email_html",
        description=(
            "Convert a markdown document into clean inline-CSS HTML "
            "ready to paste into an email body (Gmail / Outlook / "
            "Apple Mail compatible). Use this BEFORE calling "
            "gmail_send_as_me whenever the body is more than a few "
            "lines or contains markdown formatting (headers, lists, "
            "tables, code blocks) — otherwise the recipient sees the "
            "raw '#' and '*' characters. Returns the full HTML "
            "document as content; pass it as the email body. Title "
            "is optional but recommended; it lands in <title> for "
            "clients that show it."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": (
                        "Markdown source. Headers, lists, tables, "
                        "code blocks, blockquotes, links all "
                        "supported via fenced_code + tables + "
                        "sane_lists extensions."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Optional document title (lands in <title>)."
                    ),
                },
            },
            "required": ["markdown"],
        },
        risk=RiskClass.READ,
        handler=handler,
    )


# ── PDF rendering ────────────────────────────────────────────────────


_PDF_CSS = """
@page {
    size: Letter;
    margin: 0.85in 0.9in;
    @bottom-center {
        content: counter(page) " / " counter(pages);
        font-family: -apple-system, sans-serif;
        font-size: 9pt;
        color: #888;
    }
}
body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                 Roboto, Helvetica, Arial, sans-serif;
    font-size: 11pt;
    line-height: 1.5;
    color: #1a1a1a;
}
h1 {
    font-size: 22pt; font-weight: 600; color: #111;
    margin: 0 0 14pt 0; padding-bottom: 6pt;
    border-bottom: 1.5pt solid #d0d0d0;
}
h2 {
    font-size: 16pt; font-weight: 600; color: #111;
    margin: 22pt 0 10pt 0;
    page-break-after: avoid;
}
h3 {
    font-size: 13pt; font-weight: 600; color: #222;
    margin: 18pt 0 8pt 0;
    page-break-after: avoid;
}
p { margin: 0 0 10pt 0; }
ul, ol { margin: 0 0 10pt 0; padding-left: 20pt; }
li { margin: 0 0 4pt 0; }
code {
    font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
    font-size: 10pt; background: #f5f5f5;
    padding: 1pt 4pt; border-radius: 2pt; color: #c7254e;
}
pre {
    font-family: "SF Mono", Menlo, Monaco, Consolas, monospace;
    font-size: 9.5pt; background: #f8f8f8; color: #1a1a1a;
    padding: 10pt; border-radius: 4pt; line-height: 1.4;
    border: 0.5pt solid #e5e5e5; margin: 0 0 10pt 0;
    white-space: pre-wrap; word-wrap: break-word;
    page-break-inside: avoid;
}
pre code { background: transparent; padding: 0; color: inherit; }
blockquote {
    margin: 0 0 10pt 0; padding: 6pt 12pt;
    border-left: 2.5pt solid #c0c0c0; color: #555;
    background: #fafafa;
}
table {
    border-collapse: collapse; width: 100%;
    margin: 0 0 10pt 0;
    page-break-inside: avoid;
}
th, td {
    border: 0.5pt solid #d0d0d0; padding: 5pt 8pt;
    text-align: left; vertical-align: top;
}
th { background: #f3f3f3; font-weight: 600; }
hr { border: 0; border-top: 0.5pt solid #d0d0d0; margin: 16pt 0; }
a { color: #7c3aed; text-decoration: underline; }
"""


def _markdown_to_pdf_bytes(
    markdown_text: str, *, title: str | None
) -> bytes:
    import markdown as md_lib
    import weasyprint  # type: ignore

    body_html = md_lib.markdown(
        markdown_text,
        extensions=["fenced_code", "tables", "sane_lists", "toc"],
        output_format="html5",
    )
    title_tag = f"<title>{title}</title>" if title else ""
    full_html = (
        "<!DOCTYPE html><html><head>"
        f'<meta charset="utf-8">{title_tag}'
        f"<style>{_PDF_CSS}</style>"
        "</head><body>"
        f"{body_html}"
        "</body></html>"
    )
    return weasyprint.HTML(string=full_html).write_pdf()


def _slugify(s: str) -> str:
    import re

    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s[:60] or "document"


def _resolve_pdf_output(
    args: dict[str, Any], slug_hint: str,
) -> Path:
    """Pick where the rendered PDF lands.

    Order:
      1. Explicit ``output_path`` from args (relative paths resolved
         against the operator's home).
      2. Active project's ``content/email_drafts/`` folder, named by
         today's date + a slug from the title.
      3. ``~/Desktop/`` as a last-resort fallback when no projects
         manager is wired.
    """
    explicit = args.get("output_path")
    if isinstance(explicit, str) and explicit.strip():
        p = Path(explicit.strip()).expanduser()
        if not p.is_absolute():
            p = Path.home() / p
        return p

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    slug = _slugify(slug_hint)
    filename = f"{today}-{slug}.pdf"

    try:
        from core.config import get_settings

        settings = get_settings()
        active_file = settings.home / "state" / "active_project.txt"
        if active_file.is_file():
            active_slug = active_file.read_text(encoding="utf-8").strip() or "default"
        else:
            active_slug = "default"
        brain_root = Path(settings.brain_vault_path)
        project_drafts = (
            brain_root / "projects" / active_slug
            / "content" / "email_drafts"
        )
        if project_drafts.is_dir():
            return project_drafts / filename
    except Exception:
        pass

    return Path.home() / "Desktop" / filename


def _pdf_tool() -> Tool:
    async def handler(
        args: dict[str, Any], _ctx: ToolContext
    ) -> ToolOutcome:
        markdown_text = args.get("markdown")
        if not isinstance(markdown_text, str) or not markdown_text.strip():
            return ToolOutcome(
                content="document_render_pdf requires non-empty 'markdown'.",
                is_error=True,
            )
        title = args.get("title")
        title_str = title if isinstance(title, str) and title.strip() else "Document"
        try:
            pdf_bytes = _markdown_to_pdf_bytes(
                markdown_text, title=title_str,
            )
        except OSError as e:
            return ToolOutcome(
                content=(
                    f"PDF render failed — weasyprint couldn't load a "
                    f"native library: {e}. On macOS run "
                    "`brew install pango` and restart pilkd."
                ),
                is_error=True,
            )
        except Exception as e:  # pragma: no cover — defensive
            log.warning("pdf_render_failed", error=str(e))
            return ToolOutcome(
                content=f"PDF render failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        out_path = _resolve_pdf_output(args, slug_hint=title_str)
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(pdf_bytes)
        except OSError as e:
            return ToolOutcome(
                content=f"PDF write failed: {type(e).__name__}: {e}",
                is_error=True,
            )

        size_kb = len(pdf_bytes) / 1024
        return ToolOutcome(
            content=(
                f"Rendered '{title_str}' → {out_path} "
                f"({size_kb:.1f} KB). Attach this file to an email or "
                "open it from Finder to share."
            ),
            data={
                "path": str(out_path),
                "title": title_str,
                "bytes": len(pdf_bytes),
            },
        )

    return Tool(
        name="document_render_pdf",
        description=(
            "Convert a markdown document into a polished PDF (Letter "
            "size, system fonts, page numbers in footer). Saves to "
            "the active project's email_drafts folder by default; "
            "pass output_path to override. Use for: docs to attach to "
            "emails, reports to forward, anything the operator wants "
            "to print or share. Returns the saved file path. NOTE: "
            "this tool requires Pango/Cairo native libs — the first "
            "use after a fresh install needs `brew install pango`."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "markdown": {
                    "type": "string",
                    "description": "Markdown source.",
                },
                "title": {
                    "type": "string",
                    "description": (
                        "Document title (lands in PDF metadata + "
                        "filename slug). Recommended."
                    ),
                },
                "output_path": {
                    "type": "string",
                    "description": (
                        "Optional absolute or home-relative path. "
                        "Defaults to "
                        "projects/<active>/content/email_drafts/"
                        "YYYY-MM-DD-<slug>.pdf."
                    ),
                },
            },
            "required": ["markdown"],
        },
        risk=RiskClass.WRITE_LOCAL,
        handler=handler,
    )


def make_document_studio_tools() -> list[Tool]:
    """Return the Document Studio tool list. Registered unconditionally
    at boot — both tools degrade gracefully when their underlying
    libraries aren't available (handler surfaces a clean error message
    instead of crashing the daemon)."""
    return [_email_html_tool(), _pdf_tool()]


__all__ = ["make_document_studio_tools"]
