/** Force-directed graph view for the Brain page.
 *
 * Pulls `{nodes, edges}` from `/brain/graph`, feeds them to
 * react-force-graph-2d, and reports node clicks to the parent so the
 * main Brain layout can open the matching note. Colouring is derived
 * from each node's top-level folder so clusters are obvious at a
 * glance (Obsidian-style).
 *
 * Container is always mounted with a callback ref so sizing isn't
 * racing the conditional loading states. The outer shell is also
 * wrapped in an ErrorBoundary at the call site, so any runtime
 * exception inside react-force-graph-2d shows a message rather than
 * unmounting the whole Brain page.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import {
  fetchBrainGraph,
  type BrainGraphEdge,
  type BrainGraphNode,
} from "../state/api";

// Stable colour per top-level folder — picked from the dashboard's
// accent palette so Graph looks native to the rest of the UI. Unknown
// folders fall through to a deterministic HSL hash.
const FOLDER_PALETTE: Record<string, string> = {
  "": "#9aa5b1",
  daily: "#8ba7ff",
  inbox: "#ffb872",
  ingested: "#65d19b",
  "ingested/docs": "#65d19b",
  "ingested/claude-code": "#5bc0be",
  "ingested/chatgpt": "#a388ee",
  "ingested/gmail": "#f29ca3",
  "standing-instructions": "#ffd166",
  ugc_runs: "#ff7acc",
  creative_briefs: "#ffb872",
};

function colourFor(folder: string): string {
  if (folder in FOLDER_PALETTE) return FOLDER_PALETTE[folder];
  const top = folder.split("/", 1)[0] ?? "";
  if (top in FOLDER_PALETTE) return FOLDER_PALETTE[top];
  let h = 0;
  for (let i = 0; i < folder.length; i++) {
    h = (h * 31 + folder.charCodeAt(i)) >>> 0;
  }
  return `hsl(${h % 360}, 55%, 65%)`;
}

function radiusFor(sizeBytes: number): number {
  return Math.max(3, Math.min(14, Math.log10(sizeBytes + 2) * 2.4));
}

interface InternalNode extends BrainGraphNode {
  color: string;
  radius: number;
  x?: number;
  y?: number;
}

export function BrainGraph({
  enabled,
  selected,
  onSelect,
}: {
  enabled: boolean;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const [nodes, setNodes] = useState<BrainGraphNode[] | null>(null);
  const [edges, setEdges] = useState<BrainGraphEdge[] | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const fgRef = useRef<ForceGraphMethods | undefined>(undefined);
  const [viewport, setViewport] = useState({ w: 0, h: 0 });

  const load = useCallback(async () => {
    setLoading(true);
    setErr(null);
    try {
      const r = await fetchBrainGraph();
      setNodes(r.nodes);
      setEdges(r.edges);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (enabled && nodes === null) void load();
  }, [enabled, nodes, load]);

  // Callback-ref pattern: measure + observe the container the moment
  // React attaches it, regardless of which loading/ready state is
  // rendering inside. This was the race that kept the canvas at 0x0
  // and caused the "empty" look after a graph tab switch.
  const attachContainer = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      setViewport({
        w: Math.max(320, Math.floor(rect.width)),
        h: Math.max(240, Math.floor(rect.height)),
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    // Stash the observer on the element so we can disconnect it when
    // React swaps to a new node (or on unmount when el is null above).
    const priorObserver = (
      el as HTMLDivElement & { __brainRO?: ResizeObserver }
    ).__brainRO;
    if (priorObserver) priorObserver.disconnect();
    (el as HTMLDivElement & { __brainRO?: ResizeObserver }).__brainRO = ro;
  }, []);

  const graphData = useMemo(() => {
    if (!nodes || !edges) {
      return { nodes: [] as InternalNode[], links: [] as BrainGraphEdge[] };
    }
    const prepared: InternalNode[] = nodes.map((n) => ({
      ...n,
      color: colourFor(n.folder),
      radius: radiusFor(n.size),
    }));
    return { nodes: prepared, links: edges };
  }, [nodes, edges]);

  const handleEngineStop = useCallback(() => {
    try {
      fgRef.current?.zoomToFit(400, 40);
    } catch {
      // zoomToFit throws if the canvas isn't mounted yet — safe to ignore.
    }
  }, []);

  const recenter = useCallback(() => {
    try {
      fgRef.current?.zoomToFit(400, 40);
    } catch {
      // ignore
    }
  }, []);

  // Follow the cloud as the simulation settles. onEngineStop alone
  // isn't enough on 600+ node sparse graphs — the engine takes many
  // seconds to settle and during that time the barycentre drifts
  // off-screen. We poll zoomToFit for the first ~6s after the graph
  // mounts so the camera stays glued to the cloud, then stop so the
  // user can pan freely.
  useEffect(() => {
    if (!enabled || !nodes || nodes.length === 0) return;
    if (viewport.w === 0 || viewport.h === 0) return;
    let ticks = 0;
    const id = window.setInterval(() => {
      try {
        fgRef.current?.zoomToFit(300, 40);
      } catch {
        // ignore
      }
      ticks += 1;
      if (ticks >= 12) window.clearInterval(id); // 12 × 500ms = 6s
    }, 500);
    return () => window.clearInterval(id);
  }, [enabled, nodes, viewport.w, viewport.h]);

  if (!enabled) return null;

  return (
    <div ref={attachContainer} className="brain-graph-canvas">
      <div className="brain-graph-meta">
        <span>
          {nodes?.length ?? 0} node{nodes?.length === 1 ? "" : "s"} ·{" "}
          {edges?.length ?? 0} link{edges?.length === 1 ? "" : "s"}
        </span>
        <div className="brain-graph-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={recenter}
            disabled={!nodes || nodes.length === 0}
          >
            Recenter
          </button>
          <button
            type="button"
            className="btn btn--ghost"
            onClick={() => void load()}
          >
            Refresh
          </button>
        </div>
      </div>

      {loading && nodes === null ? (
        <div className="brain-graph-empty">Building graph…</div>
      ) : err ? (
        <div className="brain-graph-empty brain-graph-error">
          Couldn't load graph: {err}
          <button type="button" className="btn" onClick={() => void load()}>
            Retry
          </button>
        </div>
      ) : nodes && nodes.length === 0 ? (
        <div className="brain-graph-empty">
          The vault is empty — once PILK ingests or writes a note the
          graph fills in.
        </div>
      ) : viewport.w > 0 && viewport.h > 0 ? (
        <ForceGraph2D
          ref={fgRef}
          graphData={graphData}
          width={viewport.w}
          height={viewport.h - 40}
          backgroundColor="rgba(0,0,0,0)"
          nodeRelSize={4}
          nodeId="id"
          linkColor={() => "rgba(255,255,255,0.12)"}
          linkDirectionalParticles={0}
          linkWidth={0.6}
          cooldownTicks={180}
          onEngineStop={handleEngineStop}
          onNodeClick={(node) => {
            const n = node as unknown as InternalNode;
            onSelect(n.id);
          }}
          nodePointerAreaPaint={(node, color, ctx) => {
            const n = node as unknown as InternalNode;
            ctx.fillStyle = color;
            ctx.beginPath();
            ctx.arc(n.x ?? 0, n.y ?? 0, n.radius + 4, 0, 2 * Math.PI, false);
            ctx.fill();
          }}
          nodeCanvasObject={(node, ctx, globalScale) => {
            const n = node as unknown as InternalNode;
            const isSelected = selected === n.id;
            ctx.beginPath();
            ctx.arc(n.x ?? 0, n.y ?? 0, n.radius, 0, 2 * Math.PI, false);
            ctx.fillStyle = isSelected ? "#ffffff" : n.color;
            ctx.fill();
            if (isSelected) {
              ctx.lineWidth = 1.5;
              ctx.strokeStyle = "rgba(255,255,255,0.8)";
              ctx.stroke();
            }
            const showLabel = globalScale > 1.2 || isSelected;
            if (!showLabel) return;
            const fontSize = 11 / globalScale;
            ctx.font = `${fontSize}px -apple-system, system-ui, sans-serif`;
            ctx.fillStyle = isSelected
              ? "#ffffff"
              : "rgba(255,255,255,0.72)";
            ctx.textAlign = "left";
            ctx.textBaseline = "middle";
            ctx.fillText(
              n.label,
              (n.x ?? 0) + n.radius + 3 / globalScale,
              n.y ?? 0,
            );
          }}
        />
      ) : (
        <div className="brain-graph-empty">Measuring canvas…</div>
      )}
    </div>
  );
}
