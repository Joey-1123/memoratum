import { useEffect, useRef } from "react";
import { MultiDirectedGraph } from "graphology";
import forceAtlas2 from "graphology-layout-forceatlas2";
import Sigma from "sigma";
import type { GraphFact } from "../types";

function colorFor(text: string): string {
  let h = 0;
  for (let i = 0; i < text.length; i++) h = (h * 31 + text.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 60% 55%)`;
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
    for (const f of facts) {
      if (!graph.hasNode(f.subject)) graph.addNode(f.subject, { label: f.subject, size: 3, color: colorFor(f.subject) });
      if (!graph.hasNode(f.object)) graph.addNode(f.object, { label: f.object, size: 3, color: colorFor(f.object) });
      graph.addEdge(f.subject, f.object, { label: f.predicate, size: 1 });
      push(f.subject, f);
      push(f.object, f);
    }
    if (graph.order === 0) return;
    const names = graph.nodes();
    names.forEach((name, i) => {
      const angle = (i / Math.max(names.length, 1)) * Math.PI * 2;
      graph.setNodeAttribute(name, "x", Math.cos(angle));
      graph.setNodeAttribute(name, "y", Math.sin(angle));
    });
    forceAtlas2.assign(graph, { iterations: 120 });

    const renderer = new Sigma(graph, el, {
      labelRenderedSizeThreshold: 9,
      defaultEdgeColor: "#2b3648",
    });
    renderer.on("clickNode", ({ node }) => {
      selectRef.current(String(node), byNode.get(String(node)) ?? []);
    });
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (!reduced) {
      renderer.on("enterNode", ({ node }) => {
        graph.setNodeAttribute(node, "highlighted", true);
      });
      renderer.on("leaveNode", ({ node }) => {
        graph.setNodeAttribute(node, "highlighted", false);
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
  return <div ref={containerRef} style={{ height: "70vh", background: "var(--surface)" }} role="application" aria-label="Memory graph. Click a node to inspect it." />;
}
