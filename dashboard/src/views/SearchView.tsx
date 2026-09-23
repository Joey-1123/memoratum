import { useState } from "react";
import { motion } from "motion/react";
import { api } from "../api";
import type { SearchHit } from "../types";

type Mode = "hybrid" | "memories" | "documents";

export function SearchView({ tag }: { tag: string }) {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState<Mode>("hybrid");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [timing, setTiming] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function run(e?: React.FormEvent) {
    e?.preventDefault();
    if (!q.trim()) return;
    setLoading(true);
    setError("");
    try {
      const r = await api.search({ q, containerTag: tag, searchMode: mode, limit: 10 });
      setHits(r.results);
      setTiming((r as { timing?: number }).timing ?? 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section aria-label="Search memory">
      <h2>
        Search <span className="mono muted">{tag}</span>
      </h2>
      <form onSubmit={run} style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <label className="muted" htmlFor="q">
          Query{" "}
        </label>
        <input
          id="q"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="what does the user love?"
          style={{ flex: 1, minWidth: "200px" }}
        />
        <label className="muted" htmlFor="mode">
          Mode{" "}
        </label>
        <select id="mode" value={mode} onChange={(e) => setMode(e.target.value as Mode)}>
          <option value="hybrid">hybrid</option>
          <option value="memories">memories</option>
          <option value="documents">documents</option>
        </select>
        <button type="submit">Search</button>
      </form>
      {loading && <div className="skeleton" aria-label="Searching" />}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!loading && !error && hits.length > 0 && (
        <p className="muted" role="status">
          {hits.length} hits
          {timing ? ` in ${timing}ms` : ""}
        </p>
      )}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {hits.map((h, i) => (
          <motion.li
            key={h.id}
            className="card"
            style={{ marginBottom: "0.5rem" }}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.2, delay: i * 0.03 }}
          >
            <div>{h.memory ?? h.chunk}</div>
            <div
              aria-label={`similarity ${h.similarity}`}
              style={{ height: 4, background: "var(--surface-2)", marginTop: "0.5rem" }}
            >
              <div style={{ width: `${Math.min(100, h.similarity * 100)}%`, height: "100%", background: "var(--interactive-accent)" }} />
            </div>
          </motion.li>
        ))}
      </ul>
    </section>
  );
}
