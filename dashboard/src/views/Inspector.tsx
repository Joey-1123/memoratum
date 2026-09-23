import { Suspense, lazy, useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { GraphFact } from "../types";
import { useTimeScrub } from "../components/TimeScrub";
import { GraphView } from "./GraphView";
import { Legend } from "./Legend";

const ThreeGraphView = lazy(() =>
  import("./ThreeGraphView").then((m) => ({ default: m.ThreeGraphView }))
);

export interface Selection {
  node: string;
  facts: GraphFact[];
}

export function Inspector({ tag, selection, onClose, onOpenNote }: { tag: string; selection: Selection; onClose: () => void; onOpenNote: (s: string) => void }) {
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
        <button onClick={() => onOpenNote(selection.node)}>Open note</button>
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
        <dl style={{ margin: 0 }}>
          {current.map((f) => (
            <div key={f.id} style={{ display: "flex", gap: "0.5rem", padding: "0.2rem 0", borderBottom: "1px solid var(--border)" }}>
              <dt className="mono" style={{ color: "var(--accent-dim)", minWidth: 88 }}>{f.predicate}</dt>
              <dd className="mono" style={{ margin: 0, overflowWrap: "anywhere" }}>{f.object}</dd>
            </div>
          ))}
          {current.length === 0 && <p className="muted">No live relations.</p>}
        </dl>
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

export function GraphPanel({ tag, onOpenNote }: { tag: string; onOpenNote: (s: string) => void }) {
  const [facts, setFacts] = useState<GraphFact[]>([]);
  const [error, setError] = useState("");
  const [selection, setSelection] = useState<Selection | null>(null);
  const [mode3d, setMode3d] = useState(false);

  useEffect(() => {
    setSelection(null);
    api
      .facts(tag, 10000)
      .then((r) => setFacts(r.facts))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [tag]);

  const { visible, scrubber } = useTimeScrub(facts);
  const predicates = useMemo(
    () => [...new Set(visible.map((f) => f.predicate))].sort(),
    [visible]
  );

  if (error)
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  return (
    <div>
      <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: "0.5rem" }}>
        <div style={{ flex: 1 }}>{scrubber}</div>
        <button onClick={() => setMode3d((m) => !m)} aria-pressed={mode3d}>
          {mode3d ? "2D graph" : "3D present"}
        </button>
      </div>
      {!mode3d && <Legend predicates={predicates} />}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 320px", gap: "1rem" }}>
        {mode3d ? (
          <Suspense fallback={<div className="skeleton" aria-label="Loading 3D view" />}>
            <ThreeGraphView
              facts={visible}
              onSelect={(node, nodeFacts) => setSelection({ node, facts: nodeFacts })}
            />
          </Suspense>
        ) : (
          <GraphView
            facts={visible}
            onSelect={(node, nodeFacts) => setSelection({ node, facts: nodeFacts })}
          />
        )}
      {selection ? (
        <Inspector tag={tag} selection={selection} onClose={() => setSelection(null)} onOpenNote={onOpenNote} />
      ) : (
        <div className="card" role="status">
          <h3>Nothing selected</h3>
          <p className="muted">Click a node to inspect it — including exactly what the AI would receive.</p>
        </div>
      )}
      </div>
    </div>
  );
}
