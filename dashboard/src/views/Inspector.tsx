import { useEffect, useState } from "react";
import { api } from "../api";
import type { GraphFact } from "../types";
import { GraphView } from "./GraphView";

export interface Selection {
  node: string;
  facts: GraphFact[];
}

export function Inspector({ tag, selection, onClose }: { tag: string; selection: Selection; onClose: () => void }) {
  const [tab, setTab] = useState<"ai" | "relations" | "history" | "provenance">("ai");
  const [recall, setRecall] = useState("");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let live = true;
    setLoading(true);
    api
      .search({ q: selection.node, containerTag: tag, searchMode: "memories", limit: 3 })
      .then((r) => {
        if (!live) return;
        const lines = r.results.map((h) => `- ${h.memory ?? ""}`);
        setRecall(`[memoratum]\nRelevant memories:\n${lines.join("\n")}`);
      })
      .catch((e) => live && setRecall(`recall failed: ${e instanceof Error ? e.message : String(e)}`))
      .finally(() => live && setLoading(false));
    return () => {
      live = false;
    };
  }, [tag, selection.node]);

  const current = selection.facts.filter((f) => f.valid_to == null);
  const history = selection.facts.filter((f) => f.valid_to != null);

  return (
    <aside className="card" aria-label={`Details for ${selection.node}`} role="complementary">
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
        <h3 style={{ margin: 0, flex: 1 }}>{selection.node}</h3>
        <button onClick={onClose} aria-label="Close details">
          ×
        </button>
      </div>
      <nav aria-label="Detail tabs" style={{ display: "flex", gap: "0.25rem", margin: "0.5rem 0" }}>
        {(["ai", "relations", "history", "provenance"] as const).map((t) => (
          <button key={t} aria-current={tab === t ? "page" : undefined} onClick={() => setTab(t)}>
            {t === "ai" ? "AI view" : t[0].toUpperCase() + t.slice(1)}
          </button>
        ))}
      </nav>
      {tab === "ai" && (
        <div>
          <p className="muted">Exactly what the agent would receive for this node:</p>
          {loading ? (
            <div className="skeleton" aria-label="Loading recall preview" />
          ) : (
            <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
              {recall}
            </pre>
          )}
        </div>
      )}
      {tab === "relations" && (
        <ul>
          {current.map((f) => (
            <li key={f.id} className="mono">
              {f.subject} —<strong>{f.predicate}</strong>→ {f.object}
            </li>
          ))}
          {current.length === 0 && <li className="muted">No live relations.</li>}
        </ul>
      )}
      {tab === "history" && (
        <ul>
          {history.map((f) => (
            <li key={f.id} className="mono muted">
              {f.subject} —{f.predicate}→ {f.object} (superseded)
            </li>
          ))}
          {history.length === 0 && <li className="muted">No superseded facts.</li>}
        </ul>
      )}
      {tab === "provenance" && (
        <ul>
          {selection.facts.map((f) => (
            <li key={f.id} className="mono muted">
              {f.id} · conf {String((f.metadata as Record<string, unknown>).confidence ?? "?")} ·{" "}
              {String((f.metadata as Record<string, unknown>).community ?? "")}
            </li>
          ))}
        </ul>
      )}
    </aside>
  );
}

export function GraphPanel({ tag }: { tag: string }) {
  const [facts, setFacts] = useState<GraphFact[]>([]);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);

  useEffect(() => {
    setSelection(null);
    api
      .facts(tag, 10000)
      .then((r) => setFacts(r.facts))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [tag]);

  if (error)
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: "1rem" }}>
      <GraphView
        facts={facts}
        onSelect={(node, nodeFacts) => setSelection({ node, facts: nodeFacts })}
      />
      {selection ? (
        <Inspector tag={tag} selection={selection} onClose={() => setSelection(null)} />
      ) : (
        <div className="card" role="status">
          <h3>Nothing selected</h3>
          <p className="muted">Click a node to inspect it — including exactly what the AI would receive.</p>
        </div>
      )}
    </div>
  );
}
