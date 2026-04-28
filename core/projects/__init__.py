"""Project scoping for PILK.

Every master agent operates inside an "active project". A project is a
top-level folder under the brain vault's ``projects/`` directory that
holds the masters' knowledge, scripts, voice, contacts, and per-project
overrides. Switching projects re-points the masters at a different
folder so the same Master Sales agent can be a hard-closing real-estate
wholesaler in one project and a consultative agency rep in another —
because the scripts, voice, and wins it reads from disk are different.

The active project lives in a single state file
(``~/PILK/state/active_project.txt``) so the choice survives daemon
restarts. A "default" project is auto-created on first boot so the
system works out of the box without project setup.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

# Master domain folders auto-created inside every new project. These
# match the 5 master agents — Sales, Content, Comms, Reporting, and
# Brain. Each domain gets a voice.md (tonality scratchpad) and a
# wins.md (auto-updated by Master Reporting with what's worked) so
# the operator has somewhere to drop scripts/PDFs the moment the
# project exists.
MASTER_DOMAINS = ("sales", "content", "comms", "reporting", "brain")

# Slug pattern: lowercase letters, digits, hyphens. Starts with a
# letter or digit. Keeps filesystem paths sane and avoids accidental
# collisions with hidden / system folders.
VALID_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")

# The catch-all project. Used as the fallback active project on first
# boot and any time the active-project state file points at a
# project that no longer exists (e.g. operator deleted the folder by
# hand). Always present.
DEFAULT_SLUG = "default"


@dataclass(frozen=True)
class ProjectInfo:
    slug: str
    name: str
    description: str
    is_active: bool


class ProjectsManager:
    """Source of truth for project state.

    Wired into FastAPI app.state at boot. The orchestrator + masters
    read ``manager.active`` to figure out which project folder to
    scope to; the HTTP routes call ``list``, ``create``, ``set_active``.
    """

    def __init__(self, *, brain_root: Path, state_dir: Path) -> None:
        self.brain_root = brain_root
        self.projects_dir = brain_root / "projects"
        self.state_dir = state_dir
        self.active_file = state_dir / "active_project.txt"
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_default()
        self._ensure_email_scaffolding_for_existing()

    def _ensure_default(self) -> None:
        target = self.projects_dir / DEFAULT_SLUG
        if target.is_dir():
            return
        self._create_layout(
            slug=DEFAULT_SLUG,
            name="Default",
            description=(
                "Catch-all project. Anything not scoped to a specific "
                "client or initiative lives here. Move work into a "
                "named project once it has its own voice, scripts, or "
                "client context."
            ),
        )

    def _ensure_email_scaffolding_for_existing(self) -> None:
        """Idempotently add the Email Studio brand profile + drafts
        folder to every existing project that doesn't have them yet.

        Lets us evolve the project skeleton over time without losing
        the operator's data. The check is cheap (a handful of
        ``mkdir(exist_ok=True)`` and skipped writes when files exist)
        so it runs on every boot."""
        if not self.projects_dir.is_dir():
            return
        for project_dir in self.projects_dir.iterdir():
            if not project_dir.is_dir():
                continue
            if project_dir.name.startswith("_") or project_dir.name.startswith(
                "."
            ):
                continue
            content_dir = project_dir / "content"
            if not content_dir.is_dir():
                # Project pre-dates the masters layout; skip — we don't
                # want to silently rewrite a vault folder that someone
                # may have re-purposed.
                continue
            brand_dir = content_dir / "brand"
            try:
                brand_dir.mkdir(exist_ok=True)
                self._maybe_seed(
                    brand_dir / "voice.md",
                    "# Email voice & tonality\n\nFill in how this brand "
                    "should sound in emails.\n",
                )
                self._maybe_seed(
                    brand_dir / "audience.md",
                    "# Email audience\n\nWho reads these emails.\n",
                )
                self._maybe_seed(
                    brand_dir / "colors.md",
                    "# Brand colors\n\nPrimary: #\nSecondary: #\nAccent: #\n",
                )
                self._maybe_seed(
                    brand_dir / "logo.md",
                    "# Brand logo\n\nURL: \nWidth: \nAlt text: \n",
                )
                self._maybe_seed(
                    brand_dir / "footer.md",
                    "# Email footer\n\nCompany: \nAddress: \n"
                    "Unsubscribe: \nReply-to: \n",
                )
                samples = brand_dir / "samples"
                samples.mkdir(exist_ok=True)
                self._maybe_seed(
                    samples / "README.md",
                    "# Sample emails this brand has loved\n",
                )
                drafts = content_dir / "email_drafts"
                drafts.mkdir(exist_ok=True)
                self._maybe_seed(
                    drafts / "README.md",
                    "# Email drafts (output folder)\n\nMaster Content "
                    "writes generated emails here.\n",
                )
            except OSError:
                # Best-effort scaffolding; don't crash boot if a single
                # project's folder is unwritable.
                continue

    @staticmethod
    def _maybe_seed(path, body: str) -> None:
        if path.exists():
            return
        path.write_text(body, encoding="utf-8")

    @property
    def active_slug(self) -> str:
        """Return the currently active project's slug.

        Falls through to ``default`` if the state file is missing or
        points at a project that no longer exists on disk."""
        try:
            raw = self.active_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return DEFAULT_SLUG
        except OSError:
            return DEFAULT_SLUG
        if not raw:
            return DEFAULT_SLUG
        candidate = self.projects_dir / raw
        if not candidate.is_dir():
            return DEFAULT_SLUG
        return raw

    def set_active(self, slug: str) -> str:
        """Switch the active project. Returns the slug after validation."""
        slug = slug.strip().lower()
        if not VALID_SLUG.match(slug):
            raise ValueError(
                f"invalid project slug '{slug}' — use lowercase "
                "letters, digits, and hyphens"
            )
        target = self.projects_dir / slug
        if not target.is_dir():
            raise ValueError(
                f"project '{slug}' does not exist; create it first"
            )
        self.active_file.write_text(slug, encoding="utf-8")
        return slug

    def list(self) -> list[ProjectInfo]:
        if not self.projects_dir.is_dir():
            return []
        active = self.active_slug
        out: list[ProjectInfo] = []
        for p in sorted(self.projects_dir.iterdir()):
            if not p.is_dir():
                continue
            if p.name.startswith("_") or p.name.startswith("."):
                continue
            if not VALID_SLUG.match(p.name):
                continue
            meta = self._read_meta(p)
            out.append(
                ProjectInfo(
                    slug=p.name,
                    name=meta["name"] or p.name,
                    description=meta["description"],
                    is_active=p.name == active,
                )
            )
        return out

    def get(self, slug: str) -> ProjectInfo | None:
        target = self.projects_dir / slug
        if not target.is_dir():
            return None
        meta = self._read_meta(target)
        return ProjectInfo(
            slug=slug,
            name=meta["name"] or slug,
            description=meta["description"],
            is_active=slug == self.active_slug,
        )

    def create(
        self,
        *,
        slug: str,
        name: str,
        description: str,
    ) -> ProjectInfo:
        slug = slug.strip().lower()
        if not VALID_SLUG.match(slug):
            raise ValueError(
                f"invalid project slug '{slug}' — use lowercase "
                "letters, digits, and hyphens (1-64 chars)"
            )
        target = self.projects_dir / slug
        if target.exists():
            raise ValueError(
                f"project '{slug}' already exists"
            )
        return self._create_layout(slug=slug, name=name, description=description)

    def _create_layout(
        self, *, slug: str, name: str, description: str,
    ) -> ProjectInfo:
        target = self.projects_dir / slug
        target.mkdir(parents=True, exist_ok=False)
        for domain in MASTER_DOMAINS:
            d = target / domain
            d.mkdir()
            (d / "voice.md").write_text(
                f"# {domain.title()} voice & tonality\n\n"
                "Drop notes here about the tone, brand voice, examples, "
                "and style this master should use for "
                f"this project. Master {domain.title()} reads this "
                "automatically before each task in this project.\n",
                encoding="utf-8",
            )
            (d / "wins.md").write_text(
                f"# {domain.title()} wins — what's worked\n\n"
                "Auto-updated by Master Reporting with patterns that "
                "produced results (replies, conversions, engagement). "
                f"Master {domain.title()} biases future work toward "
                "what's listed here. Edit by hand any time.\n",
                encoding="utf-8",
            )
        # Email Studio scaffolding for Master Content. Every project
        # gets a brand profile skeleton + a drafts folder so newsletter
        # / drip / blast tasks have a stable place to land. Files start
        # empty (apart from the prompts) so the operator can fill them
        # in once and re-use across every email this project produces.
        content_dir = target / "content"
        brand_dir = content_dir / "brand"
        brand_dir.mkdir()
        (brand_dir / "voice.md").write_text(
            "# Email voice & tonality\n\n"
            "How this brand should sound in emails — formal vs. casual, "
            "long-form vs. punchy, sales-y vs. consultative. Add 2-3 "
            "example sentences that nail the tone. Master Content "
            "matches this exactly when generating newsletters / "
            "drips / blasts.\n",
            encoding="utf-8",
        )
        (brand_dir / "audience.md").write_text(
            "# Email audience\n\n"
            "Who reads these emails — job titles, interests, pain "
            "points, what they've already heard from this brand. The "
            "more specific, the better the copy.\n",
            encoding="utf-8",
        )
        (brand_dir / "colors.md").write_text(
            "# Brand colors\n\n"
            "Primary: #\nSecondary: #\nAccent: #\nText (dark): #111\n"
            "Text (light): #ffffff\n\nMaster Content uses these as "
            "inline styles in the generated HTML. Hex codes only.\n",
            encoding="utf-8",
        )
        (brand_dir / "logo.md").write_text(
            "# Brand logo\n\n"
            "URL: \n"
            "Width: e.g. 180px\n"
            "Alt text: \n\n"
            "Master Content drops the logo at the top of every email "
            "using these values.\n",
            encoding="utf-8",
        )
        (brand_dir / "footer.md").write_text(
            "# Email footer (CAN-SPAM compliant)\n\n"
            "Company name: \n"
            "Physical address: \n"
            "Unsubscribe blurb: e.g. 'You're getting this because you "
            "subscribed at example.com. Unsubscribe any time.'\n"
            "Reply-to email: \n\n"
            "Master Content stitches these into the bottom of every "
            "generated email so every send is compliant.\n",
            encoding="utf-8",
        )
        (brand_dir / "samples").mkdir()
        (brand_dir / "samples" / "README.md").write_text(
            "# Sample emails this brand has loved\n\n"
            "Drop past emails (markdown or HTML, one per file) that "
            "the client liked or that performed well. Master Content "
            "studies these before writing new ones — they're the "
            "single best voice-matching signal we have.\n",
            encoding="utf-8",
        )
        (content_dir / "email_drafts").mkdir()
        (content_dir / "email_drafts" / "README.md").write_text(
            "# Email drafts (output folder)\n\n"
            "Master Content saves generated emails here, one markdown "
            "file per email named ``YYYY-MM-DD-<slug>.md``. Each file "
            "carries the subject, preheader, markdown body, and the "
            "inline HTML ready to paste into HubSpot / GoHighLevel / "
            "any ESP.\n\n"
            "Workflow: Master Content writes here → you review and "
            "tweak the markdown → you copy the HTML block into the "
            "ESP. (Direct push to HubSpot / GHL marketing campaigns "
            "is on the roadmap; for now drafts are the deliverable.)\n",
            encoding="utf-8",
        )
        # Top-level project description — first thing every master reads
        # when it spins up inside this project.
        (target / "project.md").write_text(
            _format_project_md(name=name, description=description),
            encoding="utf-8",
        )
        return ProjectInfo(
            slug=slug,
            name=name,
            description=description,
            is_active=slug == self.active_slug,
        )

    def _read_meta(self, project_dir: Path) -> dict[str, str]:
        meta_file = project_dir / "project.md"
        try:
            text = meta_file.read_text(encoding="utf-8")
        except OSError:
            return {"name": "", "description": ""}
        # Heuristic parse: first H1 is the name, the rest is description.
        # Markdown is the storage format; this is just for the UI list.
        lines = text.splitlines()
        name = ""
        body_start = 0
        for i, line in enumerate(lines):
            if line.startswith("# ") and not name:
                name = line[2:].strip()
                body_start = i + 1
                break
        body = "\n".join(lines[body_start:]).strip()
        return {"name": name, "description": body}

    def project_dir(self, slug: str | None = None) -> Path:
        """Return the absolute path to a project's folder. Uses the
        active project when slug is None."""
        return self.projects_dir / (slug or self.active_slug)

    def domain_dir(self, domain: str, slug: str | None = None) -> Path:
        """Return the absolute path to one master's domain folder
        inside a project. ``domain`` should be one of MASTER_DOMAINS."""
        return self.project_dir(slug) / domain


def _format_project_md(*, name: str, description: str) -> str:
    return (
        f"# {name}\n\n"
        f"{description.strip() or '(No description provided yet.)'}\n\n"
        "---\n\n"
        "Drop project-specific scripts, PDFs, and reference docs into "
        "the master subfolders (`sales/`, `content/`, `comms/`, "
        "`reporting/`, `brain/`). The matching master agent reads "
        "everything in its folder before each task in this project.\n"
    )


__all__ = [
    "DEFAULT_SLUG",
    "MASTER_DOMAINS",
    "ProjectInfo",
    "ProjectsManager",
    "VALID_SLUG",
]
