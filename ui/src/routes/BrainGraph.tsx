/** Force-directed graph view for the Brain page.
 *
 * Pulls `{nodes, edges}` from `/brain/graph`, feeds them to
 * react-force-graph-2d, and reports node clicks to the parent so the
 * main Brain layout can open the matching note. Colouring is derived
 * from each node's top-level folder so clusters are obvious at a
 * glance (Obsidian-style).
 *
 * Runs client-side only — no lifecycle churn when the Brain page is
 * on the List tab. Data is lazy-loaded via the `enabled` prop.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D, { type ForceGraphMethods } from "react-force-graph-2d";
import {
  fetchBrainGraph,
  type BrainGraphEdge,
  type BrainGraphNode,
} from "../state/api";

// Stable colour per top-level folder — picked from the dashboard's
// accent palette so Graph looks native to the rest of the UI. Unknown
// folders fall through to the neutral grey.
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
  // Deterministic hash → HSL so even unmapped folders get a stable
  // colour across reloads (clusters don't reshuffle on every fetch).
  let h = 0;
  for (let i = 0; i < folder.length; i++) {
    h = (h * 31 + folder.charCodeAt(i)) >>> 0;
  }
  return `hsl(${h % 360}, 55%, 65%)`;
}

function radiusFor(sizeBytes: number): number {
  // Log scaling so a 2 MB note doesn't dwarf a 200 B note.
  return Math.max(3, Math.min(14, Math.log10(sizeBytes + 2) * 2.4));
}

interface InternalNode extends BrainGraphNode {
  color: string;
  radius: number;
  // react-force-graph mutates node objects in place with current
  // simulation coordinates; typing them as optional here makes the
  // canvas-object callback below type-safe without casting.
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
  const containerRef = useRef<HTMLDivElement | null>(null);
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

  // Track the container size so the canvas fills its slot without
  // react-force-graph's default 800x600.
  useEffect(() => {
    if (!containerRef.current) return;
    const ro = new ResizeObserver(() => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      setViewport({ w: Math.max(320, rect.width), h: Math.max(240, rect.height) });
    });
    ro.observe(containerRef.current);
    return () => ro.disconnect();
  }, [enabled]);

  const graphData = useMemo(() => {
    if (!nodes || !edges) return { nodes: [] as InternalNode[], links: [] };
    const prepared: InternalNode[] = nodes.map((n) => ({
      ...n,
      color: colourFor(n.folder),
      radius: radiusFor(n.size),
    }));
    // react-force-graph uses {source, target} on links — same shape
    // the backend emits.
    return { nodes: prepared, links: edges };
  }, [nodes, edges]);

  if (!enabled) {
    return null;
  }
  if (loading && nodes === null) {
    return <div className="brain-graph-empty">Building graph…</div>;
  }
  if (err) {
    return (
      <div className="brain-graph-empty brain-graph-error">
        Couldn't load graph: {err}
        <button type="button" className="btn" onClick={() => void load()}>
          Retry
        </button>
      </div>
    );
  }
  if (nodes && nodes.length === 0) {
    return (
      <div className="brain-graph-empty">
        The vault is empty — once PILK ingests or writes a note the
        graph fills in.
      </div>
    );
  }

  const resetView = useCallback(() => {
    if (!fgRef.current) return;
    // Zoom the camera so every node fits in the viewport. `40` is the
    // padding in px. `400` is the easing duration.
    fgRef.current.zoomToFit(400, 40);
  }, []);

  // Re-fit on resize so a large graph doesn't creep off-screen when
  // the window grows / the DevTools pane opens.
  useEffect(() => {
    if (viewport.w === 0 || viewport.h === 0) return;
    const t = window.setTimeout(() => resetView(), 80);
    return () => window.clearTimeout(t);
  }, [viewport.w, viewport.h, resetView]);

  // Gentle center-seeking force — without this, a mostly-disconnected
  // graph (freshly-ingested docs have no wiki-links between them)
  // drifts out of view because d3-force's default has no gravity.
  useEffect(() => {
    const fg = fgRef.current;
    if (!fg || !nodes || nodes.length === 0) return;
    // Charge defaults at -30. Boost repulsion so clusters separate
    // cleanly; the center force below keeps the whole graph anchored.
    const charge = fg.d3Force("charge");
    if (charge) {
      (charge as unknown as { strength: (v: number) => unknown }).strength(-40);
    }
    // Re-seat every node toward (0,0) with a soft pull. Strength
    // scales gently with node count so a 50-node graph isn't cramped
    // and a 5000-node graph doesn't explode.
    fg.d3Force(
      "center-pull-x",
      (() => {
        let strength = 0.05;
        const force = (alpha: number) => {
          for (const n of (nodes as unknown as Array<{ x?: number; vx?: number }>) ?? []) {
            if (typeof n.x === "number" && typeof n.vx === "number") {
              n.vx -= n.x * strength * alpha;
            }
          }
        };
        (force as unknown as { initialize: (n: unknown) => void }).initialize = () => {};
        (force as unknown as { strength: (v: number) => unknown }).strength = (v: number) => {
          strength = v;
          return force;
        };
        return force;
      })(),
    );
    fg.d3Force(
      "center-pull-y",
      (() => {
        let strength = 0.05;
        const force = (alpha: number) => {
          for (const n of (nodes as unknown as Array<{ y?: number; vy?: number }>) ?? []) {
            if (typeof n.y === "number" && typeof n.vy === "number") {
              n.vy -= n.y * strength * alpha;
            }
          }
        };
        (force as unknown as { initialize: (n: unknown) => void }).initialize = () => {};
        (force as unknown as { strength: (v: number) => unknown }).strength = (v: number) => {
          strength = v;
          return force;
        };
        return force;
      })(),
    );
    // Kick the simulation so the new forces apply.
    fg.d3ReheatSimulation();
  }, [nodes]);

  return (
    <div ref={containerRef} className="brain-graph-canvas">
      <div className="brain-graph-meta">
        <span>
          {nodes?.length ?? 0} node{nodes?.length === 1 ? "" : "s"} ·{" "}
          {edges?.length ?? 0} link{edges?.length === 1 ? "" : "s"}
        </span>
        <div className="brain-graph-meta-actions">
          <button
            type="button"
            className="btn btn--ghost"
            onClick={resetView}
            title="Re-centre and zoom so every node fits on screen"
          >
            Reset view
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
      {viewport.w > 0 && viewport.h > 0 && (
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
          // Longer cooldown so 600+ disconnected nodes have time to
          // spread + settle before the simulation freezes.
          cooldownTicks={300}
          // Auto-fit once the simulation stops jittering.
          onEngineStop={resetView}
          // Click hit-zone is radius × this multiplier. Default 1;
          // bump so a 4-px node is clickable without pixel-peeping.
          onNodeClick={(node) => {
            const n = node as unknown as InternalNode;
            onSelect(n.id);
          }}
          nodePointerAreaPaint={(node, color, ctx) => {
            const n = node as unknown as InternalNode;
            ctx.fillStyle = color;
            ctx.beginPath();
            // Pad the hit-zone by 4 px so tiny nodes are catchable.
            ctx.arc(
              n.x ?? 0,
              n.y ?? 0,
              n.radius + 4,
              0,
              2 * Math.PI,
              false,
            );
            ctx.fill();
          }}
          nodeCanvasObject={(node, ctx, globalScale) => {
            const n = node as unknown as InternalNode;
            const isSelected = selected === n.id;
            const label = n.label;
            // Node circle
            ctx.beginPath();
            ctx.arc(n.x ?? 0, n.y ?? 0, n.radius, 0, 2 * Math.PI, false);
            ctx.fillStyle = isSelected ? "#ffffff" : n.color;
            ctx.fill();
            if (isSelected) {
              ctx.lineWidth = 1.5;
              ctx.strokeStyle = "rgba(255,255,255,0.8)";
              ctx.stroke();
            }
            // Label — only render when zoomed in enough to be legible,
            // so the canvas doesn't turn to tag soup at default zoom.
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
              label,
              (n.x ?? 0) + n.radius + 3 / globalScale,
              n.y ?? 0,
            );
          }}
        />
      )}
    </div>
  );
}
