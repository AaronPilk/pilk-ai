/** Custom canvas-based graph view for the Brain page.
 *
 * This replaces the physics-based react-force-graph-2d implementation
 * because the force simulation never settles on 600+ sparsely-linked
 * vaults and the cloud drifts off-screen — users saw a "falling"
 * graph they couldn't click.
 *
 * Layout is fully static: nodes are grouped by top-level folder, each
 * folder becomes a cluster laid out on a phyllotaxis (sunflower)
 * spiral around the canvas center. Within each cluster, nodes are
 * also placed on a sunflower spiral — dense at the middle, thinning
 * outward. Positions are computed once and never move.
 *
 * Animation is purely cosmetic: each node has a tiny independent
 * phase offset that drives a sine-wave pulse on its radius. Keeps the
 * view feeling alive without ever shifting a node's center.
 *
 * Pan (drag) + zoom (scroll) + click-to-select are implemented via
 * plain canvas hit-testing — no dependencies.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  fetchBrainGraph,
  type BrainGraphEdge,
  type BrainGraphNode,
} from "../state/api";

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
  return Math.max(3, Math.min(12, Math.log10(sizeBytes + 2) * 2.2));
}

interface LaidOutNode {
  id: string;
  label: string;
  folder: string;
  x: number;
  y: number;
  radius: number;
  color: string;
  phase: number; // per-node pulse offset
}

const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5)); // ~2.3999 rad

function computeLayout(
  nodes: BrainGraphNode[],
  width: number,
  height: number,
): LaidOutNode[] {
  if (nodes.length === 0) return [];

  // Group by top-level folder so clusters are meaningful at a glance.
  const groups = new Map<string, BrainGraphNode[]>();
  for (const n of nodes) {
    const top = (n.folder.split("/", 1)[0] ?? "") || "(root)";
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top)!.push(n);
  }

  const folderList = [...groups.entries()].sort(
    (a, b) => b[1].length - a[1].length,
  );

  const cx = width / 2;
  const cy = height / 2;

  // Scale cluster spacing with canvas size so a tiny window still
  // shows everything and a huge monitor spreads clusters apart.
  const outerScale = Math.min(width, height) / 900;
  const clusterGap = 110 * Math.max(0.6, outerScale);
  const innerGap = 9 * Math.max(0.8, outerScale);

  const out: LaidOutNode[] = [];
  folderList.forEach(([folder, groupNodes], fi) => {
    // Cluster center on a sunflower spiral — dense in the middle,
    // spiraling outward. Gives an organic "galaxy of galaxies" look.
    const clusterAngle = fi * GOLDEN_ANGLE;
    const clusterRadius = Math.sqrt(fi + 1) * clusterGap;
    const gcx = cx + Math.cos(clusterAngle) * clusterRadius;
    const gcy = cy + Math.sin(clusterAngle) * clusterRadius;

    const colour = colourFor(folder);

    // Within each cluster, same spiral trick.
    groupNodes.forEach((node, ni) => {
      const nAngle = ni * GOLDEN_ANGLE;
      const nRadius = Math.sqrt(ni + 1) * innerGap;
      const x = gcx + Math.cos(nAngle) * nRadius;
      const y = gcy + Math.sin(nAngle) * nRadius;
      out.push({
        id: node.id,
        label: node.label,
        folder: node.folder,
        x,
        y,
        radius: radiusFor(node.size),
        color: colour,
        // Deterministic phase — same node always starts at the same
        // point of its pulse cycle across renders.
        phase: hashString(node.id) % 1000 / 1000 * Math.PI * 2,
      });
    });
  });

  return out;
}

function hashString(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h * 31 + s.charCodeAt(i)) >>> 0;
  }
  return h;
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

  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [size, setSize] = useState({ w: 0, h: 0 });

  // Camera in layout coordinates. pan (x,y) is where the layout
  // origin (cx,cy) lives in canvas pixels; zoom is a scalar.
  const cameraRef = useRef({ panX: 0, panY: 0, zoom: 1 });
  const [, forceRerender] = useState(0);
  const bumpCam = useCallback(() => forceRerender((n) => n + 1), []);

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

  // Size tracking via callback ref — fires whenever the DOM node is
  // attached, regardless of which conditional branch is rendering.
  const attachContainer = useCallback((el: HTMLDivElement | null) => {
    if (!el) return;
    const measure = () => {
      const rect = el.getBoundingClientRect();
      setSize({
        w: Math.max(320, Math.floor(rect.width)),
        h: Math.max(240, Math.floor(rect.height)),
      });
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    const prior = (el as HTMLDivElement & { __ro?: ResizeObserver }).__ro;
    if (prior) prior.disconnect();
    (el as HTMLDivElement & { __ro?: ResizeObserver }).__ro = ro;
  }, []);

  const layout = useMemo(() => {
    if (!nodes || size.w === 0) return [];
    return computeLayout(nodes, size.w, size.h - 40);
  }, [nodes, size.w, size.h]);

  const nodeIndex = useMemo(() => {
    const m = new Map<string, LaidOutNode>();
    for (const n of layout) m.set(n.id, n);
    return m;
  }, [layout]);

  // Reset camera whenever the layout regenerates so the first render
  // is always framed correctly.
  useEffect(() => {
    cameraRef.current = { panX: 0, panY: 0, zoom: 1 };
    bumpCam();
  }, [layout, bumpCam]);

  // Render loop — redraws every frame so the pulse animation plays.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0) return;
    let raf = 0;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size.w * dpr;
    canvas.height = (size.h - 40) * dpr;
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h - 40}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const start = performance.now();

    const render = () => {
      const t = (performance.now() - start) / 1000;
      const { panX, panY, zoom } = cameraRef.current;

      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      ctx.clearRect(0, 0, size.w, size.h - 40);

      // Camera transform: center the layout, then apply pan + zoom.
      const ox = size.w / 2 + panX;
      const oy = (size.h - 40) / 2 + panY;

      ctx.save();
      ctx.translate(ox, oy);
      ctx.scale(zoom, zoom);
      ctx.translate(-size.w / 2, -(size.h - 40) / 2);

      // Edges first so nodes sit on top.
      if (edges) {
        ctx.strokeStyle = "rgba(255,255,255,0.10)";
        ctx.lineWidth = 0.6 / zoom;
        ctx.beginPath();
        for (const e of edges) {
          const a = nodeIndex.get(e.source);
          const b = nodeIndex.get(e.target);
          if (!a || !b) continue;
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
        }
        ctx.stroke();
      }

      // Nodes with subtle pulse.
      for (const n of layout) {
        const pulse = 1 + Math.sin(t * 1.4 + n.phase) * 0.08;
        const r = n.radius * pulse;
        const isSelected = selected === n.id;

        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        ctx.fillStyle = isSelected ? "#ffffff" : n.color;
        ctx.fill();

        if (isSelected) {
          ctx.lineWidth = 1.5 / zoom;
          ctx.strokeStyle = "rgba(255,255,255,0.8)";
          ctx.stroke();
        }

        if (zoom > 1.4 || isSelected) {
          const fontSize = 11 / zoom;
          ctx.font = `${fontSize}px -apple-system, system-ui, sans-serif`;
          ctx.fillStyle = isSelected
            ? "#ffffff"
            : "rgba(255,255,255,0.72)";
          ctx.textAlign = "left";
          ctx.textBaseline = "middle";
          ctx.fillText(n.label, n.x + r + 3 / zoom, n.y);
        }
      }

      ctx.restore();
      raf = requestAnimationFrame(render);
    };
    raf = requestAnimationFrame(render);
    return () => cancelAnimationFrame(raf);
  }, [layout, edges, nodeIndex, selected, size.w, size.h]);

  // Hit-test in layout space.
  const pickAt = useCallback(
    (clientX: number, clientY: number): LaidOutNode | null => {
      const canvas = canvasRef.current;
      if (!canvas) return null;
      const rect = canvas.getBoundingClientRect();
      const { panX, panY, zoom } = cameraRef.current;
      const ox = size.w / 2 + panX;
      const oy = (size.h - 40) / 2 + panY;
      const cxInCanvas = clientX - rect.left;
      const cyInCanvas = clientY - rect.top;
      // Inverse of the render transform.
      const lx = (cxInCanvas - ox) / zoom + size.w / 2;
      const ly = (cyInCanvas - oy) / zoom + (size.h - 40) / 2;
      // Closest node within its radius (with a small tolerance).
      let best: LaidOutNode | null = null;
      let bestDist = Infinity;
      for (const n of layout) {
        const dx = lx - n.x;
        const dy = ly - n.y;
        const d = Math.sqrt(dx * dx + dy * dy);
        const hit = n.radius + 4 / zoom;
        if (d < hit && d < bestDist) {
          best = n;
          bestDist = d;
        }
      }
      return best;
    },
    [layout, size.w, size.h],
  );

  // Pan / click / zoom handlers.
  const dragRef = useRef<{
    startX: number;
    startY: number;
    panX0: number;
    panY0: number;
    moved: boolean;
  } | null>(null);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      (e.target as Element).setPointerCapture?.(e.pointerId);
      const { panX, panY } = cameraRef.current;
      dragRef.current = {
        startX: e.clientX,
        startY: e.clientY,
        panX0: panX,
        panY0: panY,
        moved: false,
      };
    },
    [],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const d = dragRef.current;
      if (!d) return;
      const dx = e.clientX - d.startX;
      const dy = e.clientY - d.startY;
      if (Math.abs(dx) + Math.abs(dy) > 3) d.moved = true;
      cameraRef.current = {
        ...cameraRef.current,
        panX: d.panX0 + dx,
        panY: d.panY0 + dy,
      };
    },
    [],
  );

  const onPointerUp = useCallback(
    (e: React.PointerEvent<HTMLCanvasElement>) => {
      const d = dragRef.current;
      dragRef.current = null;
      if (!d || d.moved) return;
      const hit = pickAt(e.clientX, e.clientY);
      if (hit) onSelect(hit.id);
    },
    [pickAt, onSelect],
  );

  const onWheel = useCallback(
    (e: React.WheelEvent<HTMLCanvasElement>) => {
      e.preventDefault();
      const { panX, panY, zoom } = cameraRef.current;
      const factor = Math.exp(-e.deltaY * 0.0015);
      const newZoom = Math.min(6, Math.max(0.25, zoom * factor));
      // Zoom toward cursor for a natural feel.
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const cxInCanvas = e.clientX - rect.left;
      const cyInCanvas = e.clientY - rect.top;
      const ox = size.w / 2 + panX;
      const oy = (size.h - 40) / 2 + panY;
      const scale = newZoom / zoom;
      const newPanX = cxInCanvas - (cxInCanvas - ox) * scale - size.w / 2;
      const newPanY = cyInCanvas - (cyInCanvas - oy) * scale - (size.h - 40) / 2;
      cameraRef.current = { panX: newPanX, panY: newPanY, zoom: newZoom };
    },
    [size.w, size.h],
  );

  const recenter = useCallback(() => {
    cameraRef.current = { panX: 0, panY: 0, zoom: 1 };
    bumpCam();
  }, [bumpCam]);

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
      ) : (
        <canvas
          ref={canvasRef}
          className="brain-graph-surface"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          onWheel={onWheel}
        />
      )}
    </div>
  );
}
