/** Brain — CRM-style knowledge manager over the Obsidian vault.
 *
 * Left sidebar groups every note into Apple-Settings-style categories
 * by folder prefix. Middle column lists the notes in the active
 * category. Right pane renders the selected note's markdown with
 * inline edit / delete. Top bar has a global search + an "Upload to
 * Brain" button that ingests PDFs/.txt files into any category.
 *
 * Writes flow through the same Vault on the backend that the agent's
 * `brain_note_write` tool uses, so nothing added here is hidden from
 * PILK during future conversations.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";
import {
  deleteBrainNote,
  fetchBrainBacklinks,
  fetchBrainNote,
  fetchBrainNotes,
  updateBrainNote,
  uploadBrainNote,
  type BrainBacklink,
  type BrainNote,
} from "../state/api";

// ── Category model ─────────────────────────────────────────────────

type CategoryId =
  | "daily"
  | "sessions"
  | "clients"
  | "people"
  | "projects"
  | "ideas"
  | "memory"
  | "ingested"
  | "playbooks"
  | "all";

type Category = {
  id: CategoryId;
  label: string;
  folders: string[];        // folder prefixes that belong to this category
  uploadFolder: string;     // default folder when uploading into this category
};

const CATEGORIES: Category[] = [
  { id: "all", label: "All Notes", folders: [], uploadFolder: "ingested" },
  { id: "daily", label: "Daily Notes", folders: ["daily"], uploadFolder: "daily" },
  { id: "sessions", label: "Sessions", folders: ["sessions"], uploadFolder: "sessions" },
  { id: "clients", label: "Clients", folders: ["clients"], uploadFolder: "clients" },
  { id: "people", label: "People", folders: ["people", "contacts"], uploadFolder: "people" },
  { id: "projects", label: "Projects", folders: ["projects"], uploadFolder: "projects" },
  { id: "ideas", label: "Ideas", folders: ["ideas"], uploadFolder: "ideas" },
  { id: "memory", label: "Memory", folders: ["memory"], uploadFolder: "memory" },
  { id: "ingested", label: "Ingested", folders: ["ingested"], uploadFolder: "ingested" },
  { id: "playbooks", label: "Playbooks", folders: ["playbooks"], uploadFolder: "playbooks" },
];

function topFolder(path: string): string {
  return path.split("/", 1)[0] || "";
}

function categoryOf(note: BrainNote): CategoryId {
  const top = topFolder(note.folder || note.path);
  for (const c of CATEGORIES) {
    if (c.id === "all") continue;
    if (c.folders.includes(top)) return c.id;
  }
  return "all";
}

// ── Root component ─────────────────────────────────────────────────

export default function Brain() {
  const [notes, setNotes] = useState<BrainNote[]>([]);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const [activeCategory, setActiveCategory] = useState<CategoryId>("all");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetchBrainNotes();
      setNotes(r.notes);
      setErr(null);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  // Auto-dismiss toast.
  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(null), 2400);
    return () => window.clearTimeout(t);
  }, [toast]);

  const counts = useMemo(() => {
    const m: Record<CategoryId, number> = {
      all: notes.length,
      daily: 0,
      sessions: 0,
      clients: 0,
      people: 0,
      projects: 0,
      ideas: 0,
      memory: 0,
      ingested: 0,
      playbooks: 0,
    };
    for (const n of notes) m[categoryOf(n)] += 1;
    return m;
  }, [notes]);

  // Filter + sort the list for the middle column.
  const listed = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = notes;
    if (activeCategory !== "all") {
      rows = rows.filter((n) => categoryOf(n) === activeCategory);
    }
    if (q.length > 0) {
      rows = rows.filter((n) => {
        const hay = (n.stem + " " + n.folder + " " + n.path).toLowerCase();
        return hay.includes(q);
      });
    }
    return [...rows].sort((a, b) => {
      if (!a.mtime && !b.mtime) return a.stem.localeCompare(b.stem);
      if (!a.mtime) return 1;
      if (!b.mtime) return -1;
      return b.mtime.localeCompare(a.mtime);
    });
  }, [notes, activeCategory, query]);

  // Keep the selected note valid when the category / filter changes.
  useEffect(() => {
    if (selectedPath && !listed.some((n) => n.path === selectedPath)) {
      setSelectedPath(null);
    }
  }, [listed, selectedPath]);

  const activeCategoryObj =
    CATEGORIES.find((c) => c.id === activeCategory) ?? CATEGORIES[0];

  const handleNoteDeleted = useCallback((path: string) => {
    setNotes((prev) => prev.filter((n) => n.path !== path));
    setSelectedPath((cur) => (cur === path ? null : cur));
    setToast("Note deleted");
  }, []);

  const handleNoteSaved = useCallback((updated: BrainNote) => {
    setNotes((prev) =>
      prev.map((n) => (n.path === updated.path ? { ...n, ...updated } : n)),
    );
    setToast("Saved");
  }, []);

  const handleUploaded = useCallback(
    (note: BrainNote) => {
      setNotes((prev) => {
        const without = prev.filter((n) => n.path !== note.path);
        return [note, ...without];
      });
      setSelectedPath(note.path);
      setUploadOpen(false);
      setToast("Uploaded to Brain");
      // Refresh in the background so sizes/mtimes match the server's truth.
      void load();
    },
    [load],
  );

  return (
    <div className="brain2">
      <aside className="brain2-sidebar">
        <div className="brain2-sidebar-head">Brain</div>
        <nav className="brain2-cat-list">
          {CATEGORIES.map((c) => {
            const count = counts[c.id];
            const active = c.id === activeCategory;
            return (
              <button
                key={c.id}
                type="button"
                className={
                  active
                    ? "brain2-cat brain2-cat--active"
                    : "brain2-cat"
                }
                onClick={() => {
                  setActiveCategory(c.id);
                  setSelectedPath(null);
                }}
              >
                <span className="brain2-cat-label">{c.label}</span>
                <span className="brain2-cat-count">{count}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      <section className="brain2-main">
        <header className="brain2-topbar">
          <input
            type="search"
            className="brain2-search"
            placeholder="Search notes…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button
            type="button"
            className="btn btn--primary brain2-upload-btn"
            onClick={() => setUploadOpen(true)}
          >
            Upload to Brain
          </button>
        </header>

        {err && <div className="brain2-error">{err}</div>}

        <div className="brain2-body">
          <div className="brain2-list-col">
            <div className="brain2-list-head">
              <div className="brain2-list-title">{activeCategoryObj.label}</div>
              <div className="brain2-list-sub">
                {loading
                  ? "Reading vault…"
                  : `${listed.length} ${listed.length === 1 ? "note" : "notes"}`}
              </div>
            </div>
            <div className="brain2-list-scroll">
              {loading ? (
                <div className="brain2-empty">Reading vault…</div>
              ) : listed.length === 0 ? (
                <div className="brain2-empty">
                  {query
                    ? "No matches for your search."
                    : activeCategory === "all"
                      ? "Vault is empty. Upload a doc or let PILK start writing notes."
                      : "Nothing in this category yet."}
                </div>
              ) : (
                <ul className="brain2-list">
                  {listed.map((n) => (
                    <li key={n.path}>
                      <button
                        type="button"
                        className={
                          selectedPath === n.path
                            ? "brain2-row brain2-row--active"
                            : "brain2-row"
                        }
                        onClick={() => setSelectedPath(n.path)}
                      >
                        <div className="brain2-row-title">{n.stem}</div>
                        <div className="brain2-row-meta">
                          <span className="brain2-row-cat">
                            {prettyFolder(n.folder)}
                          </span>
                          <span className="brain2-row-time">
                            {n.mtime ? relativeTime(n.mtime) : "—"}
                          </span>
                        </div>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>

          <div className="brain2-detail-col">
            {selectedPath ? (
              <NoteDetail
                key={selectedPath}
                path={selectedPath}
                note={notes.find((n) => n.path === selectedPath) ?? null}
                onDeleted={handleNoteDeleted}
                onSaved={handleNoteSaved}
                onClose={() => setSelectedPath(null)}
              />
            ) : (
              <div className="brain2-detail-empty">
                <div className="brain2-detail-empty-mark">◦</div>
                <div className="brain2-detail-empty-title">
                  Select a note
                </div>
                <div className="brain2-detail-empty-sub">
                  Pick something from the list, or upload a new doc.
                </div>
              </div>
            )}
          </div>
        </div>
      </section>

      {uploadOpen && (
        <UploadModal
          defaultCategory={activeCategory === "all" ? "ingested" : activeCategory}
          onClose={() => setUploadOpen(false)}
          onUploaded={handleUploaded}
        />
      )}

      {toast && <div className="brain2-toast">{toast}</div>}
    </div>
  );
}

// ── Note detail pane ───────────────────────────────────────────────

function NoteDetail({
  path,
  note,
  onDeleted,
  onSaved,
  onClose,
}: {
  path: string;
  note: BrainNote | null;
  onDeleted: (path: string) => void;
  onSaved: (updated: BrainNote) => void;
  onClose: () => void;
}) {
  const [body, setBody] = useState<string | null>(null);
  const [bodyLoading, setBodyLoading] = useState(true);
  const [backlinks, setBacklinks] = useState<BrainBacklink[] | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setBodyLoading(true);
    setBody(null);
    setBacklinks(null);
    setEditing(false);
    setErr(null);

    fetchBrainNote(path)
      .then((r) => {
        if (cancelled) return;
        setBody(r.body);
        setDraft(r.body);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setErr(e instanceof Error ? e.message : String(e));
      })
      .finally(() => {
        if (!cancelled) setBodyLoading(false);
      });

    fetchBrainBacklinks(path)
      .then((r) => {
        if (!cancelled) setBacklinks(r.links);
      })
      .catch(() => {
        if (!cancelled) setBacklinks([]);
      });

    return () => {
      cancelled = true;
    };
  }, [path]);

  const save = async () => {
    setSaving(true);
    setErr(null);
    try {
      const r = await updateBrainNote(path, draft);
      setBody(draft);
      setEditing(false);
      onSaved(r.note);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    setDeleting(true);
    setErr(null);
    try {
      await deleteBrainNote(path);
      onDeleted(path);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  };

  return (
    <article className="brain2-detail">
      <header className="brain2-detail-head">
        <div className="brain2-detail-title-wrap">
          <div className="brain2-detail-title">
            {note?.stem ?? path.replace(/\.md$/, "")}
          </div>
          <div className="brain2-detail-meta">
            <span>{prettyFolder(note?.folder ?? path.split("/").slice(0, -1).join("/"))}</span>
            {note?.mtime && <span>·</span>}
            {note?.mtime && <span>{formatDate(note.mtime)}</span>}
          </div>
        </div>
        <div className="brain2-detail-actions">
          {!editing ? (
            <>
              <button
                type="button"
                className="btn"
                onClick={() => setEditing(true)}
                disabled={bodyLoading || body === null}
              >
                Edit
              </button>
              <button
                type="button"
                className="btn brain2-danger"
                onClick={() => setConfirmDelete(true)}
                disabled={deleting}
              >
                Delete
              </button>
              <button
                type="button"
                className="btn"
                onClick={onClose}
                aria-label="Close"
              >
                Close
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                className="btn"
                onClick={() => {
                  setEditing(false);
                  setDraft(body ?? "");
                }}
                disabled={saving}
              >
                Cancel
              </button>
              <button
                type="button"
                className="btn btn--primary"
                onClick={save}
                disabled={saving}
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </>
          )}
        </div>
      </header>

      {err && <div className="brain2-error">{err}</div>}

      <div className="brain2-detail-body">
        {bodyLoading ? (
          <div className="brain2-empty">Loading…</div>
        ) : editing ? (
          <textarea
            className="brain2-detail-editor"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            spellCheck
          />
        ) : body === null ? (
          <div className="brain2-empty">Couldn't load this note.</div>
        ) : (
          <MarkdownBody source={body} />
        )}
      </div>

      {backlinks && backlinks.length > 0 && !editing && (
        <footer className="brain2-detail-backlinks">
          <div className="brain2-backlinks-head">Linked from</div>
          <ul className="brain2-backlinks-list">
            {backlinks.map((b, i) => (
              <li key={`${b.path}-${b.line}-${i}`}>
                <span className="brain2-backlink-title">
                  {stemFromPath(b.path)}
                </span>
                <span className="brain2-backlink-snippet">{b.snippet}</span>
              </li>
            ))}
          </ul>
        </footer>
      )}

      {confirmDelete && (
        <ConfirmDialog
          message={`Delete "${note?.stem ?? path}"? This removes the file from the vault.`}
          confirmLabel={deleting ? "Deleting…" : "Delete"}
          busy={deleting}
          onCancel={() => setConfirmDelete(false)}
          onConfirm={doDelete}
        />
      )}
    </article>
  );
}

// ── Upload modal ───────────────────────────────────────────────────

function UploadModal({
  defaultCategory,
  onClose,
  onUploaded,
}: {
  defaultCategory: CategoryId;
  onClose: () => void;
  onUploaded: (note: BrainNote) => void;
}) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [category, setCategory] = useState<CategoryId>(
    defaultCategory === "all" ? "ingested" : defaultCategory,
  );
  const [uploading, setUploading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    if (f && !label) {
      setLabel(f.name.replace(/\.(pdf|txt)$/i, ""));
    }
  };

  const submit = async () => {
    if (!file) {
      setErr("Pick a PDF or .txt file first.");
      return;
    }
    const cat = CATEGORIES.find((c) => c.id === category) ?? CATEGORIES[0];
    const folder = cat.uploadFolder;
    setUploading(true);
    setErr(null);
    try {
      const r = await uploadBrainNote(file, label.trim() || file.name, folder);
      onUploaded(r.note);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div
      className="brain2-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !uploading) onClose();
      }}
    >
      <div className="brain2-modal" role="dialog" aria-modal="true">
        <header className="brain2-modal-head">
          <div className="brain2-modal-title">Upload to Brain</div>
          <div className="brain2-modal-sub">
            Drop a PDF or .txt file. PILK extracts the text and files it
            under the category you pick.
          </div>
        </header>

        <div className="brain2-modal-body">
          <div className="brain2-field">
            <label className="brain2-field-label">File</label>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.txt,application/pdf,text/plain"
              onChange={onFile}
              className="brain2-file-input"
            />
            {file && (
              <div className="brain2-file-chip">
                <span>{file.name}</span>
                <span className="brain2-file-size">{formatSize(file.size)}</span>
              </div>
            )}
          </div>

          <div className="brain2-field">
            <label className="brain2-field-label">
              What is this?
            </label>
            <input
              type="text"
              className="brain2-input"
              placeholder="e.g. Sales script, product brief, reference doc"
              value={label}
              onChange={(e) => setLabel(e.target.value)}
            />
          </div>

          <div className="brain2-field">
            <label className="brain2-field-label">Category</label>
            <select
              className="brain2-select"
              value={category}
              onChange={(e) => setCategory(e.target.value as CategoryId)}
            >
              {CATEGORIES.filter((c) => c.id !== "all").map((c) => (
                <option key={c.id} value={c.id}>
                  {c.label}
                </option>
              ))}
            </select>
          </div>

          {err && <div className="brain2-error">{err}</div>}
        </div>

        <footer className="brain2-modal-foot">
          <button
            type="button"
            className="btn"
            onClick={onClose}
            disabled={uploading}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn btn--primary"
            onClick={submit}
            disabled={uploading || !file}
          >
            {uploading ? "Uploading…" : "Upload"}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ── Confirm dialog ────────────────────────────────────────────────

function ConfirmDialog({
  message,
  confirmLabel,
  busy,
  onCancel,
  onConfirm,
}: {
  message: string;
  confirmLabel: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  return (
    <div
      className="brain2-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !busy) onCancel();
      }}
    >
      <div className="brain2-modal brain2-modal--small" role="dialog" aria-modal="true">
        <div className="brain2-modal-body">
          <div className="brain2-confirm-msg">{message}</div>
        </div>
        <footer className="brain2-modal-foot">
          <button
            type="button"
            className="btn"
            onClick={onCancel}
            disabled={busy}
          >
            Cancel
          </button>
          <button
            type="button"
            className="btn brain2-danger"
            onClick={onConfirm}
            disabled={busy}
          >
            {confirmLabel}
          </button>
        </footer>
      </div>
    </div>
  );
}

// ── Formatting helpers ────────────────────────────────────────────

function prettyFolder(folder: string): string {
  if (!folder) return "Root";
  const top = folder.split("/", 1)[0] || folder;
  const match = CATEGORIES.find(
    (c) => c.id !== "all" && c.folders.includes(top),
  );
  if (match) {
    const rest = folder.slice(top.length).replace(/^\//, "");
    return rest ? `${match.label} · ${rest}` : match.label;
  }
  return folder;
}

function stemFromPath(p: string): string {
  const parts = p.split("/");
  const last = parts[parts.length - 1] || p;
  return last.replace(/\.md$/, "");
}

function formatSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function relativeTime(iso: string): string {
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, (Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  if (secs < 86400 * 30) return `${Math.round(secs / 86400)}d`;
  return iso.slice(0, 10);
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

// ── Markdown renderer ─────────────────────────────────────────────
// Kept inline (rather than pulling in react-markdown) to stay
// dependency-free and match the styled-badge treatment of
// [[wikilinks]] used elsewhere in the app.

function MarkdownBody({ source }: { source: string }) {
  const blocks = useMemo(() => parseMarkdown(source), [source]);
  return (
    <div className="brain2-markdown">
      {blocks.map((b, i) => renderBlock(b, i))}
    </div>
  );
}

type Block =
  | { kind: "h1" | "h2" | "h3"; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "pre"; text: string }
  | { kind: "blockquote"; text: string }
  | { kind: "hr" };

function parseMarkdown(src: string): Block[] {
  const lines = src.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === "") {
      i++;
      continue;
    }
    if (line.startsWith("```")) {
      const buf: string[] = [];
      i++;
      while (i < lines.length && !lines[i].startsWith("```")) {
        buf.push(lines[i]);
        i++;
      }
      i++;
      blocks.push({ kind: "pre", text: buf.join("\n") });
      continue;
    }
    if (/^\s*---+\s*$/.test(line)) {
      blocks.push({ kind: "hr" });
      i++;
      continue;
    }
    const h = line.match(/^(#{1,3})\s+(.*)$/);
    if (h) {
      const level = h[1].length as 1 | 2 | 3;
      blocks.push({ kind: `h${level}` as "h1" | "h2" | "h3", text: h[2] });
      i++;
      continue;
    }
    if (/^\s*>\s?/.test(line)) {
      const buf: string[] = [];
      while (i < lines.length && /^\s*>\s?/.test(lines[i])) {
        buf.push(lines[i].replace(/^\s*>\s?/, ""));
        i++;
      }
      blocks.push({ kind: "blockquote", text: buf.join(" ") });
      continue;
    }
    if (/^\s*[-*]\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*[-*]\s+/, ""));
        i++;
      }
      blocks.push({ kind: "ul", items });
      continue;
    }
    if (/^\s*\d+\.\s+/.test(line)) {
      const items: string[] = [];
      while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) {
        items.push(lines[i].replace(/^\s*\d+\.\s+/, ""));
        i++;
      }
      blocks.push({ kind: "ol", items });
      continue;
    }
    const paraLines: string[] = [line];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() !== "" &&
      !lines[i].startsWith("#") &&
      !lines[i].startsWith("```") &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !/^\s*\d+\.\s+/.test(lines[i]) &&
      !/^\s*>\s?/.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push({ kind: "p", text: paraLines.join(" ") });
  }
  return blocks;
}

function renderBlock(b: Block, key: number): JSX.Element {
  switch (b.kind) {
    case "h1":
      return <h1 key={key}>{renderInline(b.text)}</h1>;
    case "h2":
      return <h2 key={key}>{renderInline(b.text)}</h2>;
    case "h3":
      return <h3 key={key}>{renderInline(b.text)}</h3>;
    case "p":
      return <p key={key}>{renderInline(b.text)}</p>;
    case "blockquote":
      return <blockquote key={key}>{renderInline(b.text)}</blockquote>;
    case "ul":
      return (
        <ul key={key}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it)}</li>
          ))}
        </ul>
      );
    case "ol":
      return (
        <ol key={key}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it)}</li>
          ))}
        </ol>
      );
    case "pre":
      return (
        <pre key={key}>
          <code>{b.text}</code>
        </pre>
      );
    case "hr":
      return <hr key={key} />;
  }
}

function renderInline(text: string): (string | JSX.Element)[] {
  const parts: (string | JSX.Element)[] = [];
  const pattern =
    /(\[\[([^\]]+)\]\])|(\[([^\]]+)\]\(([^)]+)\))|(\*\*([^*]+)\*\*)|(\*([^*]+)\*)|(`([^`]+)`)/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) {
      parts.push(text.slice(last, match.index));
    }
    if (match[1]) {
      const raw = match[2];
      const [target, display] = raw.split("|", 2);
      parts.push(
        <span key={key++} className="brain2-wikilink">
          {(display ?? target).trim()}
        </span>,
      );
    } else if (match[3]) {
      parts.push(
        <a
          key={key++}
          href={match[5]}
          target="_blank"
          rel="noopener noreferrer"
          className="brain2-link"
        >
          {match[4]}
        </a>,
      );
    } else if (match[6]) {
      parts.push(<strong key={key++}>{match[7]}</strong>);
    } else if (match[8]) {
      parts.push(<em key={key++}>{match[9]}</em>);
    } else if (match[10]) {
      parts.push(<code key={key++}>{match[11]}</code>);
    }
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push(text.slice(last));
  return parts;
}
