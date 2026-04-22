/** Brain — Apple-style card grid over the knowledge vault.
 *
 * Left sidebar — category selector (All Notes / Daily / Sessions /
 * Clients / People / Projects / Ideas / Memory / Ingested / Playbooks).
 *
 * Main column — a card grid of the active category's notes, grouped
 * by recency (Today / Yesterday / This Week / This Month / Earlier).
 * Each group paginates independently so a category with 600 ingested
 * emails doesn't force the operator into a 20-second scroll — they
 * see ~12 recent cards per group and can click "Show more" for the
 * long tail. Global search filters across all categories in real
 * time (client-side, no extra API call per keystroke).
 *
 * Clicking a card opens the note in a centered modal reader that
 * floats over the grid rather than pushing content aside — same
 * pattern Apple Mail uses for reading a single message. The reader
 * supports inline edit / delete for real notes; memory-store
 * projections are read-only (edit them from the Memory tab).
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
  fetchMemory,
  updateBrainNote,
  uploadBrainNote,
  type BrainBacklink,
  type BrainNote,
  type MemoryEntry,
  type MemoryKind,
} from "../state/api";

// Virtual-path scheme for memory-store projections. Never collides
// with a real vault path (vault rejects `:` in names).
const MEMORY_VIRTUAL_PREFIX = "memory://";

function isMemoryVirtualPath(p: string): boolean {
  return p.startsWith(MEMORY_VIRTUAL_PREFIX);
}

function memoryVirtualPath(id: string): string {
  return `${MEMORY_VIRTUAL_PREFIX}${id}`;
}

const MEMORY_KIND_LABEL: Record<MemoryKind, string> = {
  preference: "Preference",
  standing_instruction: "Standing Instruction",
  fact: "Fact",
  pattern: "Pattern",
};

function memoryToNote(entry: MemoryEntry): BrainNote {
  return {
    path: memoryVirtualPath(entry.id),
    folder: "memory",
    stem: entry.title || MEMORY_KIND_LABEL[entry.kind],
    size: (entry.body || "").length,
    mtime: entry.updated_at || entry.created_at || null,
  };
}

function memoryToBody(entry: MemoryEntry): string {
  const header = `# ${entry.title || MEMORY_KIND_LABEL[entry.kind]}\n`;
  const meta = `> ${MEMORY_KIND_LABEL[entry.kind]} · source: ${entry.source || "unknown"}`;
  const body = (entry.body || "").trim();
  return `${header}\n${meta}\n\n${body}\n`;
}

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
  const [memoryById, setMemoryById] = useState<Map<string, MemoryEntry>>(
    () => new Map(),
  );
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const [activeCategory, setActiveCategory] = useState<CategoryId>("all");
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [toast, setToast] = useState<string | null>(null);
  const [uploadOpen, setUploadOpen] = useState(false);

  const load = useCallback(async () => {
    try {
      // Fetch the vault + the structured memory store in parallel.
      // Memory errors are non-fatal — a missing memory store shouldn't
      // take the whole Brain page down.
      const [vault, memory] = await Promise.allSettled([
        fetchBrainNotes(),
        fetchMemory(),
      ]);
      if (vault.status === "fulfilled") {
        setNotes(vault.value.notes);
      }
      if (memory.status === "fulfilled") {
        const m = new Map<string, MemoryEntry>();
        for (const e of memory.value.entries) m.set(e.id, e);
        setMemoryById(m);
      }
      if (vault.status === "rejected") {
        setErr(
          vault.reason instanceof Error
            ? vault.reason.message
            : String(vault.reason),
        );
      } else {
        setErr(null);
      }
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

  // Memory store entries projected as virtual BrainNotes. They live
  // in the Memory category alongside any real `memory/*.md` files.
  const memoryNotes = useMemo(() => {
    const out: BrainNote[] = [];
    for (const e of memoryById.values()) out.push(memoryToNote(e));
    return out;
  }, [memoryById]);

  // Everything we surface in the UI — real vault files plus virtual
  // memory-store projections. Kept as one collection so filtering,
  // search, and counts all work uniformly.
  const allNotes = useMemo(
    () => [...notes, ...memoryNotes],
    [notes, memoryNotes],
  );

  const counts = useMemo(() => {
    const m: Record<CategoryId, number> = {
      all: allNotes.length,
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
    for (const n of allNotes) m[categoryOf(n)] += 1;
    return m;
  }, [allNotes]);

  // Filter + sort the list for the middle column.
  const listed = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = allNotes;
    if (activeCategory !== "all") {
      rows = rows.filter((n) => categoryOf(n) === activeCategory);
    }
    if (q.length > 0) {
      rows = rows.filter((n) => {
        // Memory virtual paths aren't meaningful to the operator, so
        // don't include them in the haystack — search on title/folder.
        const pathHay = isMemoryVirtualPath(n.path) ? "" : n.path;
        const hay = (n.stem + " " + n.folder + " " + pathHay).toLowerCase();
        return hay.includes(q);
      });
    }
    return [...rows].sort((a, b) => {
      if (!a.mtime && !b.mtime) return a.stem.localeCompare(b.stem);
      if (!a.mtime) return 1;
      if (!b.mtime) return -1;
      return b.mtime.localeCompare(a.mtime);
    });
  }, [allNotes, activeCategory, query]);

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
    <div className="brain3">
      <aside className="brain3-sidebar">
        <div className="brain3-sidebar-head">Brain</div>
        <nav className="brain3-cat-list">
          {CATEGORIES.map((c) => {
            const count = counts[c.id];
            const active = c.id === activeCategory;
            return (
              <button
                key={c.id}
                type="button"
                className={
                  active
                    ? "brain3-cat brain3-cat--active"
                    : "brain3-cat"
                }
                onClick={() => {
                  setActiveCategory(c.id);
                  setQuery("");
                }}
              >
                <span className="brain3-cat-label">{c.label}</span>
                <span className="brain3-cat-count">{count}</span>
              </button>
            );
          })}
        </nav>
      </aside>

      <section className="brain3-main">
        <header className="brain3-topbar">
          <div className="brain3-topbar-title-wrap">
            <h1 className="brain3-topbar-title">
              {activeCategoryObj.label}
            </h1>
            <p className="brain3-topbar-sub">
              {loading
                ? "Reading vault…"
                : query
                  ? `${listed.length} ${listed.length === 1 ? "match" : "matches"} for "${query}"`
                  : `${listed.length} ${listed.length === 1 ? "note" : "notes"}`}
            </p>
          </div>
          <div className="brain3-topbar-actions">
            <div className="brain3-search-wrap">
              <svg
                className="brain3-search-icon"
                width="14"
                height="14"
                viewBox="0 0 16 16"
                aria-hidden="true"
              >
                <path
                  d="M11 11l3 3M7 12a5 5 0 1 1 0-10 5 5 0 0 1 0 10z"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                />
              </svg>
              <input
                type="search"
                className="brain3-search"
                placeholder="Search…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </div>
            <button
              type="button"
              className="btn btn--primary brain3-upload-btn"
              onClick={() => setUploadOpen(true)}
            >
              Upload to Brain
            </button>
          </div>
        </header>

        {err && <div className="brain3-error">{err}</div>}

        <div className="brain3-scroll">
          {loading ? (
            <div className="brain3-empty">Reading vault…</div>
          ) : listed.length === 0 ? (
            <div className="brain3-empty">
              {query
                ? "No matches for your search."
                : activeCategory === "all"
                  ? "Vault is empty. Upload a doc or let PILK start writing notes."
                  : "Nothing in this category yet."}
            </div>
          ) : (
            <CardGrid
              notes={listed}
              onOpen={(p) => setSelectedPath(p)}
            />
          )}
        </div>
      </section>

      {selectedPath && (
        <NoteReader
          key={selectedPath}
          path={selectedPath}
          note={allNotes.find((n) => n.path === selectedPath) ?? null}
          memoryEntry={
            isMemoryVirtualPath(selectedPath)
              ? memoryById.get(
                  selectedPath.slice(MEMORY_VIRTUAL_PREFIX.length),
                ) ?? null
              : null
          }
          onDeleted={handleNoteDeleted}
          onSaved={handleNoteSaved}
          onClose={() => setSelectedPath(null)}
        />
      )}

      {uploadOpen && (
        <UploadModal
          defaultCategory={activeCategory === "all" ? "ingested" : activeCategory}
          onClose={() => setUploadOpen(false)}
          onUploaded={handleUploaded}
        />
      )}

      {toast && <div className="brain3-toast">{toast}</div>}
    </div>
  );
}

// ── Card grid with time-grouped, paginated sections ────────────────
//
// Split the notes into Today / Yesterday / This Week / This Month /
// Earlier buckets so a category with hundreds of notes doesn't
// demand an endless scroll. Each group paginates independently —
// we default to ``INITIAL_PER_GROUP`` cards per group and let the
// operator click "Show N more" to load the long tail incrementally.

const INITIAL_PER_GROUP = 12;
const PAGE_STEP = 24;

type TimeGroup = {
  key: string;
  label: string;
  notes: BrainNote[];
};

function groupNotesByRecency(notes: BrainNote[]): TimeGroup[] {
  const now = Date.now();
  const DAY = 86400_000;
  const buckets: Record<string, BrainNote[]> = {
    today: [],
    yesterday: [],
    week: [],
    month: [],
    earlier: [],
  };
  for (const n of notes) {
    if (!n.mtime) {
      buckets.earlier.push(n);
      continue;
    }
    const age = now - Date.parse(n.mtime);
    if (age < DAY) buckets.today.push(n);
    else if (age < 2 * DAY) buckets.yesterday.push(n);
    else if (age < 7 * DAY) buckets.week.push(n);
    else if (age < 30 * DAY) buckets.month.push(n);
    else buckets.earlier.push(n);
  }
  const out: TimeGroup[] = [
    { key: "today", label: "Today", notes: buckets.today },
    { key: "yesterday", label: "Yesterday", notes: buckets.yesterday },
    { key: "week", label: "This Week", notes: buckets.week },
    { key: "month", label: "This Month", notes: buckets.month },
    { key: "earlier", label: "Earlier", notes: buckets.earlier },
  ];
  return out.filter((g) => g.notes.length > 0);
}

function CardGrid({
  notes,
  onOpen,
}: {
  notes: BrainNote[];
  onOpen: (path: string) => void;
}) {
  // Per-group pagination state. Keyed by group label so pagination
  // survives re-renders as long as the same group exists; when the
  // filter changes and groups differ, untracked groups fall back to
  // the default initial count.
  const [shownByGroup, setShownByGroup] = useState<Record<string, number>>(
    {},
  );

  // Reset pagination whenever the underlying set of notes changes
  // identity (i.e. category switch or search typed) so the operator
  // starts at the top of each new context instead of carrying an
  // expanded "Earlier" bucket from the previous view.
  useEffect(() => {
    setShownByGroup({});
  }, [notes]);

  const groups = useMemo(() => groupNotesByRecency(notes), [notes]);

  return (
    <div className="brain3-groups">
      {groups.map((g) => {
        const shown = shownByGroup[g.key] ?? INITIAL_PER_GROUP;
        const visible = g.notes.slice(0, shown);
        const remaining = g.notes.length - visible.length;
        return (
          <section key={g.key} className="brain3-group">
            <header className="brain3-group-head">
              <h2 className="brain3-group-title">{g.label}</h2>
              <span className="brain3-group-count">
                {g.notes.length}
              </span>
            </header>
            <div className="brain3-card-grid">
              {visible.map((n) => (
                <NoteCard
                  key={n.path}
                  note={n}
                  onClick={() => onOpen(n.path)}
                />
              ))}
            </div>
            {remaining > 0 && (
              <div className="brain3-group-more">
                <button
                  type="button"
                  className="brain3-show-more"
                  onClick={() =>
                    setShownByGroup((prev) => ({
                      ...prev,
                      [g.key]: shown + PAGE_STEP,
                    }))
                  }
                >
                  Show {Math.min(remaining, PAGE_STEP)} more
                  <span className="brain3-show-more-total">
                    of {remaining}
                  </span>
                </button>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}

function NoteCard({
  note,
  onClick,
}: {
  note: BrainNote;
  onClick: () => void;
}) {
  const topFolderName = topFolder(note.folder || note.path);
  return (
    <button type="button" className="brain3-card" onClick={onClick}>
      <div className="brain3-card-head">
        <span
          className="brain3-card-badge"
          data-tone={folderTone(topFolderName)}
        >
          {prettyFolder(note.folder)}
        </span>
        <span className="brain3-card-time">
          {note.mtime ? relativeTime(note.mtime) : "—"}
        </span>
      </div>
      <div className="brain3-card-title">{note.stem}</div>
      <div className="brain3-card-foot">
        <span className="brain3-card-size">{formatSize(note.size)}</span>
      </div>
    </button>
  );
}

function folderTone(folder: string): string {
  // Map each category to a muted accent so cards are visually
  // clusterable at a glance without looking like Skittles.
  const map: Record<string, string> = {
    daily: "blue",
    sessions: "violet",
    clients: "green",
    people: "pink",
    contacts: "pink",
    projects: "amber",
    ideas: "yellow",
    memory: "teal",
    ingested: "gray",
    playbooks: "indigo",
  };
  return map[folder] || "gray";
}

// ── Note reader modal (centered over the card grid) ──────────────

function NoteReader({
  path,
  note,
  memoryEntry,
  onDeleted,
  onSaved,
  onClose,
}: {
  path: string;
  note: BrainNote | null;
  memoryEntry: MemoryEntry | null;
  onDeleted: (path: string) => void;
  onSaved: (updated: BrainNote) => void;
  onClose: () => void;
}) {
  // Escape closes the reader. We check `confirmDelete` via ref so a
  // stale closure doesn't dismiss the reader when the user is really
  // trying to dismiss a confirm dialog nested inside it.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);
  const isMemory = isMemoryVirtualPath(path);
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
    setEditing(false);
    setErr(null);

    // Memory-store projections render straight from the cached entry —
    // no network call, no backlinks. The Memory tab remains the place
    // to edit them.
    if (isMemory) {
      if (memoryEntry) {
        const rendered = memoryToBody(memoryEntry);
        setBody(rendered);
        setDraft(rendered);
      } else {
        setBody(null);
      }
      setBacklinks([]);
      setBodyLoading(false);
      return;
    }

    let cancelled = false;
    setBodyLoading(true);
    setBody(null);
    setBacklinks(null);

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
  }, [path, isMemory, memoryEntry]);

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
    <div
      className="brain3-reader-backdrop"
      onClick={(e) => {
        // Backdrop click closes — but not when the click started
        // inside the reader (e.g. text selection dragged onto the
        // backdrop), which would feel broken.
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <article
        className="brain3-reader"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="brain3-reader-head">
          <div className="brain3-reader-title-wrap">
            <h2 className="brain3-reader-title">
              {note?.stem ?? (isMemory ? "Memory entry" : path.replace(/\.md$/, ""))}
            </h2>
            <div className="brain3-reader-meta">
              <span>
                {isMemory
                  ? memoryEntry
                    ? `Memory · ${MEMORY_KIND_LABEL[memoryEntry.kind]}`
                    : "Memory"
                  : prettyFolder(
                      note?.folder ?? path.split("/").slice(0, -1).join("/"),
                    )}
              </span>
              {note?.mtime && <span>·</span>}
              {note?.mtime && <span>{formatDate(note.mtime)}</span>}
            </div>
          </div>
          <div className="brain3-reader-actions">
            {isMemory ? (
              <>
                <span className="brain3-reader-hint">
                  Managed in Memory tab
                </span>
                <button
                  type="button"
                  className="brain3-reader-close"
                  onClick={onClose}
                  aria-label="Close"
                >
                  ×
                </button>
              </>
            ) : !editing ? (
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
                  className="btn brain3-danger"
                  onClick={() => setConfirmDelete(true)}
                  disabled={deleting}
                >
                  Delete
                </button>
                <button
                  type="button"
                  className="brain3-reader-close"
                  onClick={onClose}
                  aria-label="Close"
                >
                  ×
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

        {err && <div className="brain3-error">{err}</div>}

        <div className="brain3-reader-body">
          {bodyLoading ? (
            <div className="brain3-empty">Loading…</div>
          ) : editing ? (
            <textarea
              className="brain3-reader-editor"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              spellCheck
            />
          ) : body === null ? (
            <div className="brain3-empty">Couldn't load this note.</div>
          ) : (
            <MarkdownBody source={body} />
          )}
        </div>

        {backlinks && backlinks.length > 0 && !editing && (
          <footer className="brain3-reader-backlinks">
            <div className="brain3-backlinks-head">Linked from</div>
            <ul className="brain3-backlinks-list">
              {backlinks.map((b, i) => (
                <li key={`${b.path}-${b.line}-${i}`}>
                  <span className="brain3-backlink-title">
                    {stemFromPath(b.path)}
                  </span>
                  <span className="brain3-backlink-snippet">{b.snippet}</span>
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
    </div>
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
