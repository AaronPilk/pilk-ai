import { useEffect, useRef, useState } from "react";
import { ChevronDown, FolderPlus, Check, Loader2 } from "lucide-react";
import {
  createProject,
  fetchProjects,
  setActiveProject,
  type ProjectEntry,
} from "../state/api";

// Lightweight dropdown for switching the active project. Lives in the
// TopBar so it's visible on every screen — every master agent scopes
// to whichever project is active here.
export default function ProjectSwitcher() {
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [projects, setProjects] = useState<ProjectEntry[]>([]);
  const [active, setActive] = useState<string>("default");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newSlug, setNewSlug] = useState("");
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const ref = useRef<HTMLDivElement>(null);

  const refresh = async () => {
    try {
      const r = await fetchProjects();
      setProjects(r.projects);
      setActive(r.active);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    }
  };

  useEffect(() => {
    void refresh();
  }, []);

  // Close on outside click.
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setCreating(false);
      }
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const onSwitch = async (slug: string) => {
    if (slug === active) {
      setOpen(false);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await setActiveProject(slug);
      await refresh();
      setOpen(false);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setLoading(false);
    }
  };

  const onCreate = async () => {
    if (!newName.trim()) {
      setError("Name is required.");
      return;
    }
    const slug =
      newSlug.trim().toLowerCase() ||
      newName
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-+|-+$/g, "")
        .slice(0, 64);
    if (!slug) {
      setError("Slug couldn't be derived from name. Add a slug manually.");
      return;
    }
    setLoading(true);
    setError(null);
    try {
      await createProject({
        slug,
        name: newName.trim(),
        description: newDesc.trim(),
      });
      // Auto-switch to the new project so the operator can start
      // working in it immediately.
      await setActiveProject(slug);
      await refresh();
      setNewSlug("");
      setNewName("");
      setNewDesc("");
      setCreating(false);
      setOpen(false);
    } catch (e: any) {
      setError(e?.message ?? String(e));
    } finally {
      setLoading(false);
    }
  };

  const activeEntry = projects.find((p) => p.slug === active);

  return (
    <div ref={ref} className="project-switcher">
      <button
        type="button"
        className="project-switcher__trigger"
        onClick={() => setOpen((v) => !v)}
        title="Switch active project"
      >
        <span className="project-switcher__label">PROJECT</span>
        <span className="project-switcher__name">
          {activeEntry?.name ?? active}
        </span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div className="project-switcher__menu">
          {!creating ? (
            <>
              <div className="project-switcher__list">
                {projects.length === 0 && (
                  <div className="project-switcher__empty">
                    No projects yet.
                  </div>
                )}
                {projects.map((p) => (
                  <button
                    key={p.slug}
                    type="button"
                    className={`project-switcher__item${
                      p.slug === active
                        ? " project-switcher__item--active"
                        : ""
                    }`}
                    onClick={() => void onSwitch(p.slug)}
                    disabled={loading}
                  >
                    <span className="project-switcher__item-name">
                      {p.name}
                    </span>
                    <span className="project-switcher__item-slug">
                      {p.slug}
                    </span>
                    {p.slug === active && (
                      <Check size={12} className="project-switcher__check" />
                    )}
                  </button>
                ))}
              </div>
              <button
                type="button"
                className="project-switcher__new"
                onClick={() => {
                  setCreating(true);
                  setError(null);
                }}
              >
                <FolderPlus size={12} /> New project
              </button>
            </>
          ) : (
            <div className="project-switcher__form">
              <label className="project-switcher__field">
                <span>Name</span>
                <input
                  type="text"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  placeholder="Skyway Sales"
                  disabled={loading}
                  autoFocus
                />
              </label>
              <label className="project-switcher__field">
                <span>Slug (optional)</span>
                <input
                  type="text"
                  value={newSlug}
                  onChange={(e) => setNewSlug(e.target.value)}
                  placeholder="skyway-sales"
                  disabled={loading}
                />
              </label>
              <label className="project-switcher__field">
                <span>Describe the project</span>
                <textarea
                  rows={5}
                  value={newDesc}
                  onChange={(e) => setNewDesc(e.target.value)}
                  placeholder="Voice, audience, goals — anything PILK should know before working in this project."
                  disabled={loading}
                />
              </label>
              <div className="project-switcher__actions">
                <button
                  type="button"
                  className="btn btn--primary"
                  onClick={() => void onCreate()}
                  disabled={loading || !newName.trim()}
                >
                  {loading ? (
                    <Loader2
                      size={12}
                      className="project-switcher__spin"
                    />
                  ) : null}
                  Create + switch
                </button>
                <button
                  type="button"
                  className="btn btn--ghost"
                  onClick={() => {
                    setCreating(false);
                    setError(null);
                  }}
                  disabled={loading}
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
          {error && <div className="project-switcher__error">{error}</div>}
        </div>
      )}
    </div>
  );
}
