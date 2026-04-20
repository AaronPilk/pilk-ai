import { useCallback, useEffect, useMemo, useState } from "react";
import {
  fetchBrainNote,
  fetchBrainNotes,
  searchBrain,
  type BrainNote,
  type BrainSearchHit,
} from "../state/api";

/** Brain — read-only browser for the Obsidian vault at
 * ~/PILK-brain/ (override via PILK_BRAIN_VAULT_PATH). Writes still
 * happen through the `brain_note_write` tool inside the agent loop;
 * this page exists so the operator can see what PILK has written
 * without switching to Obsidian or Finder. */
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

  useEffect(() => {
    if (selected === null) {
      setBody(null);
      return;
    }
    setBodyLoading(true);
    fetchBrainNote(selected)
      .then((r) => {
        setBody(r.body);
      })
      .catch((e: unknown) => {
        setBody(
          `# ${selected}\n\n> Couldn't load — ${e instanceof Error ? e.message : String(e)}`,
        );
      })
      .finally(() => setBodyLoading(false));
  }, [selected]);

  // Group notes by folder for the tree. Empty-folder notes (root level)
  // get grouped under "".
  const groupedByFolder = useMemo(() => {
    const out = new Map<string, BrainNote[]>();
    for (const n of notes) {
      const key = n.folder;
      if (!out.has(key)) out.set(key, []);
      out.get(key)!.push(n);
    }
    // Sort folders: root first, then alphabetical. "daily" folder is
    // special — PILK writes daily journal entries there, and seeing it
    // bubble up is what makes the vault feel alive.
    const sorted = Array.from(out.entries()).sort((a, b) => {
      if (a[0] === "") return -1;
      if (b[0] === "") return 1;
      if (a[0] === "daily") return -1;
      if (b[0] === "daily") return 1;
      return a[0].localeCompare(b[0]);
    });
    return sorted;
  }, [notes]);

  const runSearch = async () => {
    const query = q.trim();
    if (query.length < 2) {
      setHits(null);
      return;
    }
    setSearching(true);
    try {
      const r = await searchBrain(query);
      setHits(r.hits);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setHits([]);
    } finally {
      setSearching(false);
    }
  };

  const clearSearch = () => {
    setQ("");
    setHits(null);
  };

  return (
    <div className="brain-page">
      <header className="brain-head">
        <div className="brain-head-title">
          <h1>Brain</h1>
          <p>
            PILK's long-term notes. Writes happen inside the agent
            loop; this view is read-only. Open the same folder in
            Obsidian for graph + backlinks.
          </p>
        </div>
        <div className="brain-head-meta">
          <div className="brain-head-count">
            {notes.length} {notes.length === 1 ? "note" : "notes"}
          </div>
          {root && <div className="brain-head-root" title={root}>{root}</div>}
        </div>
      </header>

      <div className="brain-search-row">
        <input
          type="search"
          className="brain-search-input"
          placeholder="Search across every note…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void runSearch();
            if (e.key === "Escape") clearSearch();
          }}
        />
        <button
          type="button"
          className="btn btn--primary"
          onClick={() => void runSearch()}
          disabled={searching || q.trim().length < 2}
        >
          {searching ? "Searching…" : "Search"}
        </button>
        {hits !== null && (
          <button
            type="button"
            className="btn btn--ghost"
            onClick={clearSearch}
          >
            Clear
          </button>
        )}
      </div>

      {err && <div className="brain-error">{err}</div>}

      <div className="brain-body">
        <aside className="brain-tree">
          {loading ? (
            <div className="brain-empty">Reading vault…</div>
          ) : hits !== null ? (
            <SearchResults
              hits={hits}
              onOpen={(p) => {
                setSelected(p);
                clearSearch();
              }}
            />
          ) : notes.length === 0 ? (
            <div className="brain-empty">
              Vault's empty. Once PILK starts writing notes (daily
              journal entries, long-form workspace knowledge), they
              show up here.
            </div>
          ) : (
            groupedByFolder.map(([folder, rows]) => (
              <div className="brain-folder" key={folder || "__root__"}>
                <div className="brain-folder-head">
                  {folder === "" ? "(root)" : folder}
                  <span className="brain-folder-count">{rows.length}</span>
                </div>
                <ul className="brain-folder-list">
                  {rows.map((n) => (
                    <li key={n.path}>
                      <button
                        type="button"
                        className={
                          selected === n.path
                            ? "brain-note-btn brain-note-btn--active"
                            : "brain-note-btn"
                        }
                        onClick={() => setSelected(n.path)}
                      >
                        <span className="brain-note-stem">{n.stem}</span>
                        <span className="brain-note-meta">
                          {n.mtime ? relativeTime(n.mtime) : ""}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ))
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
                  // Wikilinks open the matching note if we have one.
                  // Fall back to searching for the target otherwise.
                  const hit = notes.find(
                    (n) =>
                      n.stem.toLowerCase() === target.toLowerCase() ||
                      n.path.toLowerCase() === `${target.toLowerCase()}.md`,
                  );
                  if (hit) {
                    setSelected(hit.path);
                  } else {
                    setQ(target);
                    void runSearch();
                  }
                }}
              />
            </article>
          )}
        </main>
      </div>
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
        Matches
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

/** Tiny markdown renderer for the vault view. Obsidian notes are
 * mostly plain text + headings + lists + wikilinks, so we ship a
 * minimal parser instead of pulling in react-markdown. Handles:
 *  - # / ## / ### headings
 *  - * / - bullet lists
 *  - 1. numbered lists
 *  - **bold**, *italic*, `inline code`
 *  - ```fenced code blocks```
 *  - [link](url)
 *  - [[wikilink]] (dispatches to onWikilinkClick)
 *  - blank-line paragraphs
 * Anything else renders as plain text — which is the graceful
 * fallback the operator would want for an unknown construct. */
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
    // Paragraph — consume until blank line.
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

/** Split inline text into chunks: [[wikilink]], [link](url), **bold**,
 * *italic*, `code`, plain. Conservative — any unmatched syntax falls
 * back to literal characters so odd notes never crash the renderer. */
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
      const target = match[2];
      parts.push(
        <button
          key={key++}
          type="button"
          className="brain-wikilink"
          onClick={() => onWikilink(target)}
        >
          {target}
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
  return iso.slice(0, 10); // fallback to date
}
