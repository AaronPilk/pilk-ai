import type { MemoryEntry } from "../state/api";
import { humanizeMemoryKind, relativeTime } from "../lib/humanize";

export default function MemoryRow({
  entry,
  onDelete,
}: {
  entry: MemoryEntry;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="memory-row">
      <div className="memory-row-main">
        <div className="memory-row-head">
          <span className="memory-row-chip">
            {humanizeMemoryKind(entry.kind)}
          </span>
          <span className="memory-row-time">
            {relativeTime(entry.created_at)}
          </span>
        </div>
        <div className="memory-row-title">{entry.title}</div>
        {entry.body && <div className="memory-row-body">{entry.body}</div>}
      </div>
      <button
        type="button"
        className="memory-row-delete"
        onClick={() => onDelete(entry.id)}
        title="Forget this entry"
        aria-label="Forget this entry"
      >
        ✕
      </button>
    </div>
  );
}
