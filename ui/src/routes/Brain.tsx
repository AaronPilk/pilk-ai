import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from "react";
import {
  fetchBrainBacklinks,
  fetchBrainNote,
  fetchBrainNotes,
  searchBrain,
  type BrainBacklink,
  type BrainNote,
  type BrainSearchHit,
} from "../state/api";

/** Brain — browser for the Obsidian vault at ~/PILK-brain/ (override
 * via PILK_BRAIN_VAULT_PATH). Presents every note as a card in a
 * responsive grid, filterable by top-level folder and searchable
 * across body text. Clicking a card opens the full note in an overlay
 * with markdown rendering + backlinks rail.
 *
 * Writes still happen inside the agent loop via `brain_note_write`;
 * this view stays read-only.
 */
export default function Brain() {
  const [notes, setNotes] = useState<BrainNote[]>([]);
  const [root, setRoot] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<BrainSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [folderFilter, setFolderFilter] = useState<string>("");

  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [selectedBody, setSelectedBody] = useState<string | null>(null);
  const [selectedBacklinks, setSelectedBacklinks] =
    useState<BrainBacklink[] | null>(null);
  const [bodyLoading, setBodyLoading] = useState(false);

  const load = useCallback(async () => {
    try {
      const r = await fetchBrainNotes();
      setNotes(r.notes);
      setRoot(r.root);
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

  // Debounced live search.
  const debounceRef = useRef<number | null>(null);
  useEffect(() => {
    if (debounceRef.current !== null) {
      window.clearTimeout(debounceRef.current);
    }
    const query = q.trim();
    if (query.length < 2) {
      setHits(null);
      setSearching(false);
      return;
    }
    setSearching(true);
    debounceRef.current = window.setTimeout(async () => {
      try {
        const r = await searchBrain(query);
        setHits(r.hits);
      } catch (e) {
        setErr(e instanceof Error ? e.message : String(e));
        setHits([]);
      } finally {
        setSearching(false);
      }
    }, 200);
    return () => {
      if (debounceRef.current !== null) {
        window.clearTimeout(debounceRef.current);
      }
    };
  }, [q]);

  // Note body + backlinks loader — fires whenever a card is opened.
  useEffect(() => {
    if (selectedPath === null) {
      setSelectedBody(null);
      setSelectedBacklinks(null);
      return;
    }
    let cancelled = false;
    setBodyLoading(true);
    setSelectedBody(null);
    setSelectedBacklinks(null);

    fetchBrainNote(selectedPath)
      .then((r) => {
        if (!cancelled) setSelectedBody(r.body);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setSelectedBody(
          `# ${selectedPath}\n\n> Couldn't load — ${
            e instanceof Error ? e.message : String(e)
          }`,
        );
      })
      .finally(() => {
        if (!cancelled) setBodyLoading(false);
      });

    fetchBrainBacklinks(selectedPath)
      .then((r) => {
        if (!cancelled) setSelectedBacklinks(r.links);
      })
      .catch(() => {
        if (!cancelled) setSelectedBacklinks([]);
      });

    return () => {
      cancelled = true;
    };
  }, [selectedPath]);

  // Dismiss overlay on Escape.
  useEffect(() => {
    if (selectedPath === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setSelectedPath(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selectedPath]);

  // Top-level folder chips derived from loaded notes.
  const folderChips = useMemo(() => {
    const counts = new Map<string, number>();
    for (const n of notes) {
      const top = n.folder.split("/", 1)[0] || "(root)";
      counts.set(top, (counts.get(top) ?? 0) + 1);
    }
    return [...counts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([folder, count]) => ({ folder, count }));
  }, [notes]);

  // Filter cards by selected folder + sort by mtime (freshest first).
  const cards = useMemo(() => {
    const filtered = folderFilter
      ? notes.filter((n) => {
          const top = n.folder.split("/", 1)[0] || "(root)";
          return top === folderFilter;
        })
      : notes;
    return [...filtered].sort((a, b) => {
      if (!a.mtime && !b.mtime) return a.stem.localeCompare(b.stem);
      if (!a.mtime) return 1;
      if (!b.mtime) return -1;
      return b.mtime.localeCompare(a.mtime);
    });
  }, [notes, folderFilter]);

  const clearSearch = () => {
    setQ("");
    setHits(null);
  };

  const noteByPath = useMemo(() => {
    const m = new Map<string, BrainNote>();
    for (const n of notes) m.set(n.path, n);
    return m;
  }, [notes]);

  const selectedNote =
    selectedPath !== null ? noteByPath.get(selectedPath) ?? null : null;

  return (
    <div className="brain-page">
      <header className="brain-head">
        <div className="brain-head-title">
          <h1>Brain</h1>
          <p>
            PILK's long-term notes. Writes happen inside the agent
            loop; this view is read-only.
          </p>
        </div>
        <div className="brain-head-meta">
          <div className="brain-head-count">
            {notes.length} {notes.length === 1 ? "note" : "notes"}
          </div>
          {root && (
            <div className="brain-head-root" title={root}>
              {root}
            </div>
          )}
        </div>
      </header>

      <div className="brain-search-row">
        <input
          type="search"
          className="brain-search-input"
          placeholder="Search across every note (type to filter)…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") clearSearch();
          }}
        />
        {hits !== null && (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={clearSearch}
          >
            Clear
          </button>
        )}
        {searching && (
          <span className="brain-search-status">Searching…</span>
        )}
      </div>

      {err && <div className="brain-error">{err}</div>}

      {hits === null && (
        <div className="brain-chip-row" role="tablist">
          <button
            type="button"
            className={
              folderFilter === ""
                ? "brain-chip brain-chip--active"
                : "brain-chip"
            }
            onClick={() => setFolderFilter("")}
          >
            All <span className="brain-chip-count">{notes.length}</span>
          </button>
          {folderChips.map((c) => (
            <button
              type="button"
              key={c.folder}
              className={
                folderFilter === c.folder
                  ? "brain-chip brain-chip--active"
                  : "brain-chip"
              }
              onClick={() => setFolderFilter(c.folder)}
              style={{ "--chip-color": folderColor(c.folder) } as CSSProperties}
            >
              {c.folder}
              <span className="brain-chip-count">{c.count}</span>
            </button>
          ))}
        </div>
      )}

      <div className="brain-grid-wrap">
        {loading ? (
          <div className="brain-empty">Reading vault…</div>
        ) : hits !== null ? (
          <SearchResults
            hits={hits}
            notesByPath={noteByPath}
            onOpen={(p) => setSelectedPath(p)}
          />
        ) : cards.length === 0 ? (
          <div className="brain-empty">
            {folderFilter
              ? `Nothing in ${folderFilter} yet.`
              : "Vault's empty. Once PILK starts writing notes, they show up here."}
          </div>
        ) : (
          <div className="brain-card-grid">
            {cards.map((n) => (
              <NoteCard
                key={n.path}
                note={n}
                onClick={() => setSelectedPath(n.path)}
              />
            ))}
          </div>
        )}
      </div>

      {selectedPath !== null && (
        <NoteOverlay
          path={selectedPath}
          note={selectedNote}
          body={selectedBody}
          bodyLoading={bodyLoading}
          backlinks={selectedBacklinks}
          onClose={() => setSelectedPath(null)}
          onOpenOther={(p) => setSelectedPath(p)}
          notesByPath={noteByPath}
        />
      )}
    </div>
  );
}

// ── Card ───────────────────────────────────────────────────────────

function NoteCard({
  note,
  onClick,
}: {
  note: BrainNote;
  onClick: () => void;
}) {
  const topFolder = note.folder.split("/", 1)[0] || "(root)";
  return (
    <button type="button" className="brain-card" onClick={onClick}>
      <div className="brain-card-head">
        <span
          className="brain-card-folder"
          style={{ "--chip-color": folderColor(topFolder) } as CSSProperties}
        >
          {note.folder || "(root)"}
        </span>
        <span className="brain-card-time">
          {note.mtime ? relativeTime(note.mtime) : ""}
        </span>
      </div>
      <div className="brain-card-title">{note.stem}</div>
      <div className="brain-card-foot">
        <span className="brain-card-path">{note.path}</span>
        <span className="brain-card-size">{formatSize(note.size)}</span>
      </div>
    </button>
  );
}

// ── Search results (flat card list with snippets) ──────────────────

function SearchResults({
  hits,
  notesByPath,
  onOpen,
}: {
  hits: BrainSearchHit[];
  notesByPath: Map<string, BrainNote>;
  onOpen: (path: string) => void;
}) {
  if (hits.length === 0) {
    return <div className="brain-empty">No matches. Try shorter terms.</div>;
  }
  return (
    <div className="brain-card-grid">
      {hits.map((h, i) => {
        const n = notesByPath.get(h.path);
        const topFolder =
          (n?.folder ?? "").split("/", 1)[0] || "(root)";
        return (
          <button
            type="button"
            key={`${h.path}-${h.line}-${i}`}
            className="brain-card brain-card--hit"
            onClick={() => onOpen(h.path)}
          >
            <div className="brain-card-head">
              <span
                className="brain-card-folder"
                style={{ "--chip-color": folderColor(topFolder) } as CSSProperties}
              >
                {n?.folder || "(root)"}
              </span>
              <span className="brain-card-time">L{h.line}</span>
            </div>
            <div className="brain-card-title">{n?.stem ?? h.path}</div>
            <div className="brain-card-snippet">{h.snippet}</div>
            <div className="brain-card-foot">
              <span className="brain-card-path">{h.path}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

// ── Overlay (full-note modal) ──────────────────────────────────────

function NoteOverlay({
  path,
  note,
  body,
  bodyLoading,
  backlinks,
  onClose,
  onOpenOther,
  notesByPath,
}: {
  path: string;
  note: BrainNote | null;
  body: string | null;
  bodyLoading: boolean;
  backlinks: BrainBacklink[] | null;
  onClose: () => void;
  onOpenOther: (p: string) => void;
  notesByPath: Map<string, BrainNote>;
}) {
  const handleWikilink = useCallback(
    (target: string) => {
      const lower = target.toLowerCase();
      for (const [p, n] of notesByPath) {
        if (n.stem.toLowerCase() === lower || p.toLowerCase() === `${lower}.md`) {
          onOpenOther(p);
          return;
        }
      }
    },
    [notesByPath, onOpenOther],
  );

  return (
    <div
      className="brain-overlay"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div className="brain-overlay-dialog" role="dialog" aria-modal="true">
        <header className="brain-overlay-head">
          <div className="brain-overlay-title">
            <div className="brain-overlay-stem">{note?.stem ?? path}</div>
            <div className="brain-overlay-path">{path}</div>
          </div>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={onClose}
            aria-label="Close"
          >
            Close
          </button>
        </header>

        <div className="brain-overlay-body">
          <article className="brain-overlay-article">
            {bodyLoading ? (
              <div className="brain-empty">Loading…</div>
            ) : body === null ? (
              <div className="brain-empty">Couldn't load {path}.</div>
            ) : (
              <MarkdownBody source={body} onWikilinkClick={handleWikilink} />
            )}
          </article>

          <aside className="brain-overlay-backlinks">
            <div className="brain-backlinks-head">Backlinks</div>
            {backlinks === null ? (
              <div className="brain-backlinks-empty">Checking…</div>
            ) : backlinks.length === 0 ? (
              <div className="brain-backlinks-empty">
                Nothing links here yet.
              </div>
            ) : (
              <ul className="brain-backlinks-list">
                {backlinks.map((b, i) => (
                  <li key={`${b.path}-${b.line}-${i}`}>
                    <button
                      type="button"
                      className="brain-backlink-btn"
                      onClick={() => onOpenOther(b.path)}
                    >
                      <span className="brain-backlink-path">{b.path}</span>
                      <span className="brain-backlink-snippet">
                        {b.snippet}
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </aside>
        </div>
      </div>
    </div>
  );
}

// ── Formatting helpers ─────────────────────────────────────────────

function folderColor(folder: string): string {
  const palette: Record<string, string> = {
    daily: "#8ba7ff",
    inbox: "#ffb872",
    ingested: "#65d19b",
    "standing-instructions": "#ffd166",
    ugc_runs: "#ff7acc",
    creative_briefs: "#ffb872",
    "(root)": "#9aa5b1",
  };
  if (folder in palette) return palette[folder];
  let h = 0;
  for (let i = 0; i < folder.length; i++) {
    h = (h * 31 + folder.charCodeAt(i)) >>> 0;
  }
  return `hsl(${h % 360}, 55%, 65%)`;
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

// ── Markdown renderer (unchanged) ──────────────────────────────────

function MarkdownBody({
  source,
  onWikilinkClick,
}: {
  source: string;
  onWikilinkClick: (target: string) => void;
}) {
  const blocks = useMemo(() => parseMarkdown(source), [source]);
  return (
    <div className="brain-markdown">
      {blocks.map((b, i) => renderBlock(b, i, onWikilinkClick))}
    </div>
  );
}

type Block =
  | { kind: "h1" | "h2" | "h3"; text: string }
  | { kind: "p"; text: string }
  | { kind: "ul"; items: string[] }
  | { kind: "ol"; items: string[] }
  | { kind: "pre"; text: string }
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
      !/^\s*\d+\.\s+/.test(lines[i])
    ) {
      paraLines.push(lines[i]);
      i++;
    }
    blocks.push({ kind: "p", text: paraLines.join(" ") });
  }
  return blocks;
}

function renderBlock(
  b: Block,
  key: number,
  onWikilink: (target: string) => void,
): JSX.Element {
  switch (b.kind) {
    case "h1":
      return <h1 key={key}>{renderInline(b.text, onWikilink)}</h1>;
    case "h2":
      return <h2 key={key}>{renderInline(b.text, onWikilink)}</h2>;
    case "h3":
      return <h3 key={key}>{renderInline(b.text, onWikilink)}</h3>;
    case "p":
      return <p key={key}>{renderInline(b.text, onWikilink)}</p>;
    case "ul":
      return (
        <ul key={key}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it, onWikilink)}</li>
          ))}
        </ul>
      );
    case "ol":
      return (
        <ol key={key}>
          {b.items.map((it, j) => (
            <li key={j}>{renderInline(it, onWikilink)}</li>
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

function renderInline(
  text: string,
  onWikilink: (target: string) => void,
): (string | JSX.Element)[] {
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
        <button
          key={key++}
          type="button"
          className="brain-wikilink"
          onClick={() => onWikilink(target.trim())}
        >
          {(display ?? target).trim()}
        </button>,
      );
    } else if (match[3]) {
      parts.push(
        <a
          key={key++}
          href={match[5]}
          target="_blank"
          rel="noopener noreferrer"
          className="brain-link"
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
