/** Brain — CRM-style three-panel knowledge manager.
 *
 * Layout:
 *
 *   ┌──────────────┬────────────────────────────────────────────┐
 *   │ sidebar      │ top bar  (title · search · Upload)         │
 *   │ — Sales Ops  ├────────────────────────────────────────────┤
 *   │ — Clients    │ 2-3 column card grid (24 per page)         │
 *   │ — Trading    │ prev / next pagination                     │
 *   │ — Personal   │                                            │
 *   │ — Projects   │                                            │
 *   │ — Chat Arch. │                                            │
 *   │ — Inbox      │                                            │
 *   │ — All Notes  │                                            │
 *   └──────────────┴────────────────────────────────────────────┘
 *
 * Clicking a card opens a centered reader modal with the rendered
 * markdown body, backlinks, and delete-with-confirm. Uploads land in
 * the active category's upload_folder; ingester-owned categories
 * (chat_archive / inbox) disable the button.
 *
 * Data endpoints (all new in this PR):
 *   GET /brain/categories         — sidebar
 *   GET /brain/notes              — paginated grid (category + q)
 *   GET /brain/notes/{path}       — detail pane
 *
 * Mutations (upload / delete) reuse the existing endpoints, which
 * share the same Vault object PILK's own brain_note_write tool uses,
 * so everything added here is visible to future agent reasoning.
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
  fetchBrainCategories,
  fetchBrainNoteDetail,
  fetchBrainNotesPage,
  uploadBrainNote,
  type BrainBacklink,
  type BrainCategory,
  type BrainNote,
  type BrainNoteDetail,
} from "../state/api";

// ── Chat Archive topic clustering ───────────────────────────────────

/** Keyword rules mirror the backend's classify_topic() so the UI
 * shows the same buckets the index uses. Priority-ordered. */
const TOPIC_RULES: Array<{ id: string; label: string; re: RegExp }> = [
  {
    id: "trading", label: "Trading",
    re: /\b(xauusd|gold|forex|trade|trading|chart|candle|pip|scalp|bullish|bearish)\b/i,
  },
  {
    id: "brand", label: "Brand Building",
    re: /\b(nv|brand|branding|logo|packaging|label|palette|typography|visual\s+identity)\b/i,
  },
  {
    id: "business", label: "Business Ideas",
    re: /\b(pitch|revenue|offer|saas|startup|funnel|lead|mrr|arr|pricing|churn)\b/i,
  },
  {
    id: "personal", label: "Personal",
    re: /\b(health|relationship|relationships|mindset|diet|workout|journal|therapy)\b/i,
  },
  {
    id: "tech", label: "Tech / Dev",
    re: /\b(code|coding|api|deploy|agent|python|javascript|typescript|react|fastapi|docker)\b/i,
  },
];

function classifyTopic(note: BrainNote): string {
  const hay = `${note.title || ""} ${note.stem || ""}`;
  for (const rule of TOPIC_RULES) {
    if (rule.re.test(hay)) return rule.id;
  }
  return "general";
}

// ── helpers ─────────────────────────────────────────────────────────

function formatDate(s: string | null): string {
  if (!s) return "";
  const d = new Date(s);
  if (Number.isNaN(d.getTime())) return "";
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  if (sameDay) {
    return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  }
  return d.toLocaleDateString(undefined, {
    month: "short", day: "numeric",
    year: d.getFullYear() === now.getFullYear() ? undefined : "numeric",
  });
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── main ────────────────────────────────────────────────────────────

export default function Brain(): JSX.Element {
  const [categories, setCategories] = useState<BrainCategory[]>([]);
  const [activeCategory, setActiveCategory] = useState<string>("all");
  const [activeTopic, setActiveTopic] = useState<string>("all");
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [notes, setNotes] = useState<BrainNote[]>([]);
  const [pagination, setPagination] = useState({
    total: 0, pages: 1, pageSize: 24,
  });
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const loadCategories = useCallback(async () => {
    try {
      const r = await fetchBrainCategories();
      setCategories(r.categories);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  const loadNotes = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchBrainNotesPage({
        category: activeCategory,
        page,
        pageSize: 24,
        q: query.trim() || undefined,
      });
      setNotes(r.notes);
      setPagination({
        total: r.total, pages: r.pages, pageSize: r.page_size,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [activeCategory, page, query]);

  useEffect(() => { loadCategories(); }, [loadCategories]);
  useEffect(() => { loadNotes(); }, [loadNotes]);
  useEffect(() => { setActiveTopic("all"); setPage(1); }, [activeCategory]);
  useEffect(() => { setPage(1); }, [query]);

  const activeCategoryMeta = useMemo(
    () => categories.find((c) => c.id === activeCategory) ?? null,
    [categories, activeCategory],
  );

  const isChatArchive = activeCategory === "chat_archive";
  const filteredNotes = useMemo(() => {
    if (!isChatArchive || activeTopic === "all") return notes;
    return notes.filter((n) => classifyTopic(n) === activeTopic);
  }, [notes, isChatArchive, activeTopic]);

  const onUploaded = useCallback(
    async (msg: string) => {
      setShowUpload(false);
      await loadCategories();
      await loadNotes();
      setErr(msg);
      setTimeout(() => setErr((e) => (e === msg ? null : e)), 3500);
    },
    [loadCategories, loadNotes],
  );

  const onDeleted = useCallback(
    async (path: string) => {
      try {
        await deleteBrainNote(path);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
        return;
      }
      setSelectedPath(null);
      await loadCategories();
      await loadNotes();
    },
    [loadCategories, loadNotes],
  );

  return (
    <div className="brain3-root">
      <Sidebar
        categories={categories}
        active={activeCategory}
        onPick={setActiveCategory}
      />
      <main className="brain3-main">
        <TopBar
          categoryLabel={activeCategoryMeta?.label ?? "All Notes"}
          categoryIcon={activeCategoryMeta?.icon ?? "📁"}
          total={pagination.total}
          query={query}
          onQuery={setQuery}
          onUpload={() => setShowUpload(true)}
          uploadEnabled={
            !!(activeCategoryMeta?.upload_folder) || activeCategory === "all"
          }
        />
        {isChatArchive && (
          <TopicChips active={activeTopic} onPick={setActiveTopic} />
        )}
        {err && <div className="brain3-banner">{err}</div>}
        {loading ? (
          <div className="brain3-empty">Loading…</div>
        ) : filteredNotes.length === 0 ? (
          <div className="brain3-empty">
            No notes in this category{query ? " matching your search" : ""}.
          </div>
        ) : (
          <div className="brain3-grid">
            {filteredNotes.map((n) => (
              <NoteCard
                key={n.path}
                note={n}
                onOpen={() => setSelectedPath(n.path)}
              />
            ))}
          </div>
        )}
        {pagination.pages > 1 && (
          <Pagination
            page={page}
            pages={pagination.pages}
            onPage={setPage}
          />
        )}
      </main>

      {selectedPath && (
        <DetailModal
          path={selectedPath}
          onClose={() => setSelectedPath(null)}
          onDeleted={onDeleted}
        />
      )}
      {showUpload && activeCategoryMeta && (
        <UploadModal
          category={activeCategoryMeta}
          onClose={() => setShowUpload(false)}
          onUploaded={onUploaded}
        />
      )}
    </div>
  );
}

// ── sidebar ─────────────────────────────────────────────────────────

function Sidebar({
  categories, active, onPick,
}: {
  categories: BrainCategory[];
  active: string;
  onPick: (id: string) => void;
}): JSX.Element {
  return (
    <aside className="brain3-sidebar">
      <div className="brain3-sidebar-title">Brain</div>
      <nav className="brain3-sidebar-list">
        {categories.map((c) => (
          <button
            key={c.id}
            type="button"
            className={
              "brain3-sidebar-item" +
              (c.id === active ? " brain3-sidebar-item--active" : "")
            }
            onClick={() => onPick(c.id)}
          >
            <span className="brain3-sidebar-icon">{c.icon}</span>
            <span className="brain3-sidebar-label">{c.label}</span>
            <span className="brain3-sidebar-count">{c.count}</span>
          </button>
        ))}
      </nav>
    </aside>
  );
}

// ── top bar ─────────────────────────────────────────────────────────

function TopBar({
  categoryLabel, categoryIcon, total, query, onQuery, onUpload, uploadEnabled,
}: {
  categoryLabel: string;
  categoryIcon: string;
  total: number;
  query: string;
  onQuery: (v: string) => void;
  onUpload: () => void;
  uploadEnabled: boolean;
}): JSX.Element {
  return (
    <header className="brain3-topbar">
      <div className="brain3-topbar-title">
        <span aria-hidden="true" className="brain3-topbar-icon">
          {categoryIcon}
        </span>
        <span>{categoryLabel}</span>
        <span className="brain3-topbar-count">{total}</span>
      </div>
      <div className="brain3-topbar-right">
        <div className="brain3-search">
          <span aria-hidden="true">🔍</span>
          <input
            type="search"
            className="brain3-search-input"
            placeholder="Search this category"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
          />
        </div>
        <button
          type="button"
          className="brain3-upload-btn"
          onClick={onUpload}
          disabled={!uploadEnabled}
          title={
            uploadEnabled
              ? "Upload to this category"
              : "Ingester-owned category — uploads don't apply"
          }
        >
          Upload
        </button>
      </div>
    </header>
  );
}

// ── topic chips (Chat Archive) ─────────────────────────────────────

function TopicChips({
  active, onPick,
}: {
  active: string;
  onPick: (id: string) => void;
}): JSX.Element {
  const chips = [
    { id: "all", label: "All" },
    ...TOPIC_RULES.map((r) => ({ id: r.id, label: r.label })),
    { id: "general", label: "General" },
  ];
  return (
    <div className="brain3-chips">
      {chips.map((c) => (
        <button
          key={c.id}
          type="button"
          className={
            "brain3-chip" + (c.id === active ? " brain3-chip--active" : "")
          }
          onClick={() => onPick(c.id)}
        >
          {c.label}
        </button>
      ))}
    </div>
  );
}

// ── grid card ──────────────────────────────────────────────────────

function NoteCard({
  note, onOpen,
}: {
  note: BrainNote;
  onOpen: () => void;
}): JSX.Element {
  const title = note.title || note.stem;
  const subtitle = note.title ? note.stem : note.folder;
  return (
    <button type="button" className="brain3-card" onClick={onOpen}>
      <div className="brain3-card-title">{title}</div>
      {subtitle && subtitle !== title && (
        <div className="brain3-card-subtitle">{subtitle}</div>
      )}
      <div className="brain3-card-meta">
        <span>{formatDate(note.mtime)}</span>
        <span aria-hidden="true">·</span>
        <span>{formatSize(note.size)}</span>
      </div>
    </button>
  );
}

// ── pagination ─────────────────────────────────────────────────────

function Pagination({
  page, pages, onPage,
}: {
  page: number;
  pages: number;
  onPage: (n: number) => void;
}): JSX.Element {
  return (
    <div className="brain3-pagination">
      <button
        type="button"
        className="brain3-pagination-btn"
        onClick={() => onPage(Math.max(1, page - 1))}
        disabled={page <= 1}
      >
        ← Prev
      </button>
      <div className="brain3-pagination-label">
        Page {page} of {pages}
      </div>
      <button
        type="button"
        className="brain3-pagination-btn"
        onClick={() => onPage(Math.min(pages, page + 1))}
        disabled={page >= pages}
      >
        Next →
      </button>
    </div>
  );
}

// ── detail modal ───────────────────────────────────────────────────

function DetailModal({
  path, onClose, onDeleted,
}: {
  path: string;
  onClose: () => void;
  onDeleted: (path: string) => void;
}): JSX.Element {
  const [detail, setDetail] = useState<BrainNoteDetail | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setErr(null);
    fetchBrainNoteDetail(path)
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch((e) => {
        if (!cancelled) setErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div
      className="brain3-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="brain3-modal" role="dialog" aria-modal="true">
        <header className="brain3-modal-head">
          <div className="brain3-modal-title">
            {detail?.note.title || detail?.note.stem || path}
          </div>
          <button
            type="button"
            className="brain3-modal-close"
            onClick={onClose}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        {err && <div className="brain3-banner">{err}</div>}
        {!detail && !err && <div className="brain3-empty">Loading…</div>}
        {detail && (
          <>
            <div className="brain3-modal-meta">
              {detail.note.folder && <span>{detail.note.folder}</span>}
              {detail.note.mtime && (
                <>
                  <span aria-hidden="true">·</span>
                  <span>{formatDate(detail.note.mtime)}</span>
                </>
              )}
              <span aria-hidden="true">·</span>
              <span>{formatSize(detail.note.size)}</span>
            </div>
            <article className="brain3-modal-body">
              <MarkdownRender body={detail.body} />
            </article>
            {detail.backlinks.length > 0 && (
              <aside className="brain3-backlinks">
                <div className="brain3-backlinks-title">Backlinks</div>
                <ul>
                  {detail.backlinks.map((bl: BrainBacklink) => (
                    <li key={`${bl.path}-${bl.line}`}>
                      <span className="brain3-backlinks-path">{bl.path}</span>
                      {bl.snippet && (
                        <span className="brain3-backlinks-snippet">
                          {bl.snippet}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              </aside>
            )}
            <footer className="brain3-modal-foot">
              {confirmDelete ? (
                <div className="brain3-confirm">
                  <span>Delete this note?</span>
                  <button
                    type="button"
                    className="brain3-btn brain3-btn--danger"
                    onClick={() => onDeleted(path)}
                  >
                    Delete
                  </button>
                  <button
                    type="button"
                    className="brain3-btn"
                    onClick={() => setConfirmDelete(false)}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  className="brain3-btn brain3-btn--subtle"
                  onClick={() => setConfirmDelete(true)}
                >
                  Delete
                </button>
              )}
            </footer>
          </>
        )}
      </div>
    </div>
  );
}

// Small dep-free renderer. Handles headings, bulleted lists, and
// paragraphs — that's what >95% of our vault notes are. Anything
// richer (tables, images) renders as plain text, which is acceptable
// for the reader pane given it's a preview, not an editor.
function MarkdownRender({ body }: { body: string }): JSX.Element {
  const clean = body.replace(/^---\s*\n[\s\S]*?\n---\s*\n?/, "");
  const blocks = clean.split(/\n{2,}/);
  return (
    <>
      {blocks.map((block, i) => {
        const h = /^(#{1,6})\s+(.+)$/.exec(block.trim());
        if (h) {
          const level = Math.min(h[1].length, 4);
          const Tag = `h${level}` as keyof JSX.IntrinsicElements;
          return <Tag key={i}>{h[2]}</Tag>;
        }
        if (block.trim().startsWith("- ")) {
          const items = block
            .split("\n")
            .filter((l) => l.trim().startsWith("- "));
          return (
            <ul key={i}>
              {items.map((l, j) => (
                <li key={j}>{l.trim().replace(/^-\s+/, "")}</li>
              ))}
            </ul>
          );
        }
        return <p key={i}>{block}</p>;
      })}
    </>
  );
}

// ── upload modal ───────────────────────────────────────────────────

function UploadModal({
  category, onClose, onUploaded,
}: {
  category: BrainCategory;
  onClose: () => void;
  onUploaded: (msg: string) => void;
}): JSX.Element {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [label, setLabel] = useState("");
  const [uploading, setUploading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const isZip = !!file && /\.zip$/i.test(file.name);

  const onFile = (e: ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0] ?? null;
    setFile(f);
    if (f && !label && !/\.zip$/i.test(f.name)) {
      setLabel(f.name.replace(/\.(pdf|txt|md|docx)$/i, ""));
    }
  };

  const submit = async () => {
    if (!file) {
      setErr("Pick a file first.");
      return;
    }
    const folder = isZip ? "" : category.upload_folder ?? "";
    const effectiveLabel = isZip ? "" : (label.trim() || file.name);
    setUploading(true);
    setErr(null);
    try {
      const r = await uploadBrainNote(file, effectiveLabel, folder);
      const count = r.imported;
      const msg =
        r.source_kind === "chatgpt_export"
          ? `Imported ${count} ChatGPT conversations — raw zip archived at ${r.archive_path ?? "ingested/chatgpt-archive/"}`
          : `Ingested into ${category.label} — ${count} note${count === 1 ? "" : "s"} created`;
      onUploaded(msg);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setUploading(false);
    }
  };

  return (
    <div
      className="brain3-modal-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget && !uploading) onClose();
      }}
    >
      <div className="brain3-modal brain3-modal--narrow" role="dialog" aria-modal="true">
        <header className="brain3-modal-head">
          <div className="brain3-modal-title">
            Upload to {category.label}
          </div>
          <button
            type="button"
            className="brain3-modal-close"
            onClick={onClose}
            disabled={uploading}
            aria-label="Close"
          >
            ×
          </button>
        </header>
        <div className="brain3-modal-body">
          <div className="brain3-field">
            <label className="brain3-field-label">File</label>
            <input
              ref={fileInputRef}
              type="file"
              accept=".pdf,.txt,.md,.zip,.docx,application/pdf,text/plain,application/zip"
              onChange={onFile}
              className="brain3-file-input"
            />
            {file && (
              <div className="brain3-file-chip">
                <span>{file.name}</span>
                <span className="brain3-file-size">
                  {formatSize(file.size)}
                </span>
              </div>
            )}
          </div>
          {isZip ? (
            <div className="brain3-field">
              <div className="brain3-hint">
                ChatGPT export detected — each conversation will land as
                its own note under Chat Archive.
              </div>
            </div>
          ) : (
            <div className="brain3-field">
              <label className="brain3-field-label">Title</label>
              <input
                type="text"
                className="brain3-input"
                placeholder="e.g. Sales script, product brief, reference"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
              />
            </div>
          )}
          {err && <div className="brain3-banner">{err}</div>}
        </div>
        <footer className="brain3-modal-foot">
          <button
            type="button"
            className="brain3-btn"
            onClick={onClose}
            disabled={uploading}
          >
            Cancel
          </button>
          <button
            type="button"
            className="brain3-btn brain3-btn--primary"
            onClick={submit}
            disabled={!file || uploading}
          >
            {uploading ? "Uploading…" : isZip ? "Import to Brain" : "Upload"}
          </button>
        </footer>
      </div>
    </div>
  );
}
