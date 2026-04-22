import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
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
import { BrainGraph } from "./BrainGraph";

type Tab = "list" | "graph";

/** Brain — browser for the Obsidian vault at
 * ~/PILK-brain/ (override via PILK_BRAIN_VAULT_PATH). This session
 * adds three things on top of the earlier v1 list view:
 *
 *   - Tabs (List / Graph) — the graph view is a force-directed
 *     rendering of every note and their `[[wiki-link]]` edges.
 *   - Nested folder tree with expand/collapse + counts.
 *   - Live search as you type (debounced), backlinks on the note
 *     view's right rail, tighter typography.
 *
 * Writes still happen inside the agent loop via `brain_note_write`;
 * this view stays read-only.
 */
export default function Brain() {
  const [notes, setNotes] = useState<BrainNote[]>([]);
  const [root, setRoot] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);

  const [selected, setSelected] = useState<string | null>(null);
  const [body, setBody] = useState<string | null>(null);
  const [bodyLoading, setBodyLoading] = useState(false);

  const [q, setQ] = useState("");
  const [hits, setHits] = useState<BrainSearchHit[] | null>(null);
  const [searching, setSearching] = useState(false);

  const [tab, setTab] = useState<Tab>("list");
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));

  const [backlinks, setBacklinks] = useState<BrainBacklink[] | null>(null);

  const load = useCallback(async () => {
    try {
      const r = await fetchBrainNotes();
      setNotes(r.notes);
      setRoot(r.root);
      setErr(null);
      if (r.notes.length > 0 && selected === null) {
        setSelected(r.notes[0].path);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [selected]);

  useEffect(() => {
    load();
  }, [load]);

  // Body loader.
  useEffect(() => {
    if (selected === null) {
      setBody(null);
      return;
    }
    setBodyLoading(true);
    fetchBrainNote(selected)
      .then((r) => setBody(r.body))
      .catch((e: unknown) => {
        setBody(
          `# ${selected}\n\n> Couldn't load — ${
            e instanceof Error ? e.message : String(e)
          }`,
        );
      })
      .finally(() => setBodyLoading(false));
  }, [selected]);

  // Backlinks loader — fires alongside the body so the rail is live
  // by the time the reader finishes the first paragraph.
  useEffect(() => {
    if (selected === null) {
      setBacklinks(null);
      return;
    }
    let cancelled = false;
    fetchBrainBacklinks(selected)
      .then((r) => {
        if (!cancelled) setBacklinks(r.links);
      })
      .catch(() => {
        if (!cancelled) setBacklinks([]);
      });
    return () => {
      cancelled = true;
    };
  }, [selected]);

  // Debounced live search — 200 ms after the last keystroke, refresh
  // results. Short queries (< 2 chars) clear the result list rather
  // than firing an empty search.
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

  const folderTree = useMemo(() => buildFolderTree(notes), [notes]);

  const clearSearch = () => {
    setQ("");
    setHits(null);
  };

  const openNote = useCallback((path: string) => {
    setSelected(path);
    setTab("list");
  }, []);

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
          {root && <div className="brain-head-root" title={root}>{root}</div>}
        </div>
      </header>

      <div className="brain-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={tab === "list"}
          className={
            tab === "list" ? "brain-tab brain-tab--active" : "brain-tab"
          }
          onClick={() => setTab("list")}
        >
          List
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === "graph"}
          className={
            tab === "graph" ? "brain-tab brain-tab--active" : "brain-tab"
          }
          onClick={() => setTab("graph")}
        >
          Graph
        </button>
      </div>

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

      {tab === "graph" ? (
        <div className="brain-graph-wrap">
          <BrainGraph
            enabled={tab === "graph"}
            selected={selected}
            onSelect={openNote}
          />
        </div>
      ) : (
        <div className="brain-body">
          <aside className="brain-tree">
            {loading ? (
              <div className="brain-empty">Reading vault…</div>
            ) : hits !== null ? (
              <SearchResults hits={hits} onOpen={(p) => openNote(p)} />
            ) : notes.length === 0 ? (
              <div className="brain-empty">
                Vault's empty. Once PILK starts writing notes (daily
                journal entries, long-form workspace knowledge), they
                show up here.
              </div>
            ) : (
              <FolderTree
                tree={folderTree}
                expanded={expanded}
                toggleFolder={(path) =>
                  setExpanded((prev) => {
                    const next = new Set(prev);
                    if (next.has(path)) next.delete(path);
                    else next.add(path);
                    return next;
                  })
                }
                selected={selected}
                onSelect={openNote}
              />
            )}
          </aside>

          <main className="brain-view">
            {selected === null ? (
              <div className="brain-empty">Select a note from the list.</div>
            ) : bodyLoading ? (
              <div className="brain-empty">Loading {selected}…</div>
            ) : body === null ? (
              <div className="brain-empty">Couldn't load {selected}.</div>
            ) : (
              <article className="brain-note">
                <div className="brain-note-path">{selected}</div>
                <MarkdownBody
                  source={body}
                  onWikilinkClick={(target) => {
                    const hit = notes.find(
                      (n) =>
                        n.stem.toLowerCase() === target.toLowerCase() ||
                        n.path.toLowerCase() ===
                          `${target.toLowerCase()}.md`,
                    );
                    if (hit) openNote(hit.path);
                    else setQ(target);
                  }}
                />
              </article>
            )}
          </main>

          <aside className="brain-backlinks">
            <div className="brain-backlinks-head">Backlinks</div>
            {selected === null ? (
              <div className="brain-backlinks-empty">
                Open a note to see who links to it.
              </div>
            ) : backlinks === null ? (
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
                      onClick={() => openNote(b.path)}
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
      )}
    </div>
  );
}

// ── Folder tree ────────────────────────────────────────────────────


interface TreeNode {
  name: string;           // display segment (e.g. "docs")
  path: string;           // full folder path (e.g. "ingested/docs")
  children: TreeNode[];
  notes: BrainNote[];
}

function buildFolderTree(notes: BrainNote[]): TreeNode {
  const root: TreeNode = { name: "", path: "", children: [], notes: [] };
  for (const n of notes) {
    if (!n.folder) {
      root.notes.push(n);
      continue;
    }
    const parts = n.folder.split("/");
    let cursor = root;
    let acc = "";
    for (const part of parts) {
      acc = acc ? `${acc}/${part}` : part;
      let child = cursor.children.find((c) => c.name === part);
      if (!child) {
        child = { name: part, path: acc, children: [], notes: [] };
        cursor.children.push(child);
      }
      cursor = child;
    }
    cursor.notes.push(n);
  }
  // Sort each level: daily first, then alphabetical folders, then
  // individual notes alphabetically.
  const sort = (node: TreeNode) => {
    node.children.sort((a, b) => {
      if (a.name === "daily") return -1;
      if (b.name === "daily") return 1;
      return a.name.localeCompare(b.name);
    });
    node.notes.sort((a, b) => a.stem.localeCompare(b.stem));
    node.children.forEach(sort);
  };
  sort(root);
  return root;
}

function countDescendants(node: TreeNode): number {
  return (
    node.notes.length +
    node.children.reduce((acc, c) => acc + countDescendants(c), 0)
  );
}

function FolderTree({
  tree,
  expanded,
  toggleFolder,
  selected,
  onSelect,
}: {
  tree: TreeNode;
  expanded: Set<string>;
  toggleFolder: (path: string) => void;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <div className="brain-tree-inner">
      {tree.notes.length > 0 && (
        <FolderBlock
          node={{ name: "(root)", path: "", children: [], notes: tree.notes }}
          expanded={expanded}
          toggleFolder={toggleFolder}
          selected={selected}
          onSelect={onSelect}
          depth={0}
        />
      )}
      {tree.children.map((child) => (
        <FolderBlock
          key={child.path}
          node={child}
          expanded={expanded}
          toggleFolder={toggleFolder}
          selected={selected}
          onSelect={onSelect}
          depth={0}
        />
      ))}
    </div>
  );
}

function FolderBlock({
  node,
  expanded,
  toggleFolder,
  selected,
  onSelect,
  depth,
}: {
  node: TreeNode;
  expanded: Set<string>;
  toggleFolder: (path: string) => void;
  selected: string | null;
  onSelect: (path: string) => void;
  depth: number;
}) {
  const isOpen = expanded.has(node.path);
  const count = countDescendants(node);
  return (
    <div
      className="brain-folder"
      style={{ paddingLeft: `${depth * 8}px` }}
    >
      <button
        type="button"
        className="brain-folder-head brain-folder-head--btn"
        onClick={() => toggleFolder(node.path)}
        aria-expanded={isOpen}
      >
        <span className="brain-folder-chev">{isOpen ? "▾" : "▸"}</span>
        <span className="brain-folder-name">{node.name || "(root)"}</span>
        <span className="brain-folder-count">{count}</span>
      </button>
      {isOpen && (
        <>
          <ul className="brain-folder-list">
            {node.notes.map((n) => (
              <li key={n.path}>
                <button
                  type="button"
                  className={
                    selected === n.path
                      ? "brain-note-btn brain-note-btn--active"
                      : "brain-note-btn"
                  }
                  onClick={() => onSelect(n.path)}
                >
                  <span className="brain-note-stem">{n.stem}</span>
                  <span className="brain-note-meta">
                    {n.mtime ? relativeTime(n.mtime) : ""}
                  </span>
                </button>
              </li>
            ))}
          </ul>
          {node.children.map((child) => (
            <FolderBlock
              key={child.path}
              node={child}
              expanded={expanded}
              toggleFolder={toggleFolder}
              selected={selected}
              onSelect={onSelect}
              depth={depth + 1}
            />
          ))}
        </>
      )}
    </div>
  );
}

function SearchResults({
  hits,
  onOpen,
}: {
  hits: BrainSearchHit[];
  onOpen: (path: string) => void;
}) {
  if (hits.length === 0) {
    return (
      <div className="brain-empty">No matches. Try shorter terms.</div>
    );
  }
  return (
    <div className="brain-search-results">
      <div className="brain-folder-head">
        <span className="brain-folder-name">Matches</span>
        <span className="brain-folder-count">{hits.length}</span>
      </div>
      <ul className="brain-folder-list">
        {hits.map((h, i) => (
          <li key={`${h.path}-${h.line}-${i}`}>
            <button
              type="button"
              className="brain-note-btn brain-search-hit"
              onClick={() => onOpen(h.path)}
            >
              <span className="brain-note-stem">{h.path}</span>
              <span className="brain-search-line">L{h.line}</span>
              <span className="brain-search-snippet">{h.snippet}</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// ── Markdown renderer (unchanged from v1) ──────────────────────────


/** Tiny markdown renderer for the vault view. Obsidian notes are
 * mostly plain text + headings + lists + wikilinks, so we ship a
 * minimal parser instead of pulling in react-markdown. */
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
      i++; // consume closing fence (if present)
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
      blocks.push({ kind: (`h${level}` as "h1" | "h2" | "h3"), text: h[2] });
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
      // Strip any `|display` suffix from the wikilink target so
      // the button label matches Obsidian's visible text.
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
