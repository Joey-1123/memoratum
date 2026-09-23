import { useEffect, useRef } from "react";
import { MultiDirectedGraph } from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import Sigma from "sigma";
import type { GraphFact } from "../types";

/** Restrained categorical palette tuned for dark backgrounds. */
const COMMUNITY_COLORS = [
  "#5ec8d8",
  "#8fb573",
  "#d6a437",
  "#b48ede",
  "#de8e8e",
  "#6a9ede",
  "#5fb89a",
  "#c9c9c9",
];

export const PREDICATE_COLORS: Record<string, string> = {
  calls: "#5ec8d8",
  imports: "#8fb573",
  contains: "#6a7280",
  references: "#d6a437",
  inherits: "#b48ede",
  uses: "#5fb89a",
};
const DEFAULT_EDGE = "#2b3648";

function communityColor(community: string | undefined, index: Map<string, number>): string {
  if (!community) return "#6a7280";
  let i = index.get(community);
  if (i === undefined) {
    i = index.size % COMMUNITY_COLORS.length;
    index.set(community, i);
  }
  return COMMUNITY_COLORS[i];
}

function meta(fact: GraphFact): Record<string, unknown> {
  return (fact.metadata ?? {}) as Record<string, unknown>;
}

export function GraphView({
  facts,
  onSelect,
}: {
  facts: GraphFact[];
  onSelect: (node: string, facts: GraphFact[]) => void;
}) {
  const containerRef = useRef<HTMLDivElement>(null);
  const selectRef = useRef(onSelect);
  selectRef.current = onSelect;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const graph = new MultiDirectedGraph();
    const byNode = new Map<string, GraphFact[]>();
    const push = (name: string, fact: GraphFact) => {
      const list = byNode.get(name) ?? [];
      list.push(fact);
      byNode.set(name, list);
    };
    const communityIndex = new Map<string, number>();
    for (const f of facts) {
      if (!graph.hasNode(f.subject))
        graph.addNode(f.subject, { label: f.subject, size: 2, color: "#8a94a6" });
      if (!graph.hasNode(f.object))
        graph.addNode(f.object, { label: f.object, size: 2, color: "#8a94a6" });
      graph.addEdge(f.subject, f.object, {
        label: f.predicate,
        size: Math.min(3, 0.6 + (Number(meta(f).weight) || 0) * 0.8),
        color: PREDICATE_COLORS[f.predicate] ?? DEFAULT_EDGE,
      });
      push(f.subject, f);
      push(f.object, f);
    }
    if (graph.order === 0) return;
    // Degree sizing + community colors after the full edge set is known.
    graph.forEachNode((node) => {
      const degree = graph.degree(node);
      graph.setNodeAttribute(node, "size", 2 + Math.sqrt(degree) * 1.6);
    });
    const seen = new Map<string, number>();
    for (const f of facts) {
      for (const name of [f.subject, f.object]) {
        const community = meta(f).community;
        if (typeof community === "string" && !seen.has(name)) {
          seen.set(name, 1);
          graph.setNodeAttribute(name, "color", communityColor(community, communityIndex));
        }
      }
    }
    const names = graph.nodes();
    names.forEach((name, i) => {
      const angle = (i / Math.max(names.length, 1)) * Math.PI * 2;
      graph.setNodeAttribute(name, "x", Math.cos(angle));
      graph.setNodeAttribute(name, "y", Math.sin(angle));
    });
    forceAtlas2.assign(graph, { iterations: 150, settings: { gravity: 0.8, scalingRatio: 4 } });

    const renderer = new Sigma(graph, el, {
      labelRenderedSizeThreshold: 10,
      labelColor: { color: "#c7d0de" },
      defaultEdgeColor: DEFAULT_EDGE,
    });
    renderer.on("clickNode", ({ node }) => {
      selectRef.current(String(node), byNode.get(String(node)) ?? []);
    });
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let hovered: string | null = null;
    const applyFocus = () => {
      renderer.setSetting("nodeReducer", (_node, data) => {
        if (!hovered) return data;
        if (_node === hovered || graph.hasEdge(_node, hovered) || graph.hasEdge(hovered, _node))
          return { ...data, highlighted: true };
        return { ...data, color: "#1a2230", label: "", highlighted: false };
      });
      renderer.setSetting("edgeReducer", (_edge, data) => {
        if (!hovered) return data;
        const [s, t] = graph.extremities(_edge);
        if (s === hovered || t === hovered) return data;
        return { ...data, color: "#141b26", hidden: false };
      });
      renderer.refresh();
    };
    if (!reduced) {
      renderer.on("enterNode", ({ node }) => {
        hovered = String(node);
        applyFocus();
      });
      renderer.on("leaveNode", () => {
        hovered = null;
        applyFocus();
      });
    }
    const live = document.createElement("div");
    live.setAttribute("role", "status");
    live.setAttribute("aria-live", "polite");
    live.className = "muted";
    live.textContent = `${graph.order} nodes, ${graph.size} edges loaded.`;
    el.appendChild(live);
    return () => {
      renderer.kill();
      live.remove();
    };
  }, [facts]);

  if (facts.length === 0) {
    return (
      <div className="card" role="status">
        <h3>No facts in this tag</h3>
        <p className="muted">Import a graph or ingest documents to see nodes here.</p>
      </div>
    );
  }
  return <div ref={containerRef} style={{ height: "70vh", background: "var(--background-primary)" }} role="application" aria-label="Memory graph. Click a node to inspect it." />;
}
