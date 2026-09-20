import { useState } from "react";

export function ImportView({ onImported }: { onImported: (tag: string) => void }) {
  const [dir, setDir] = useState("");
  const [tag, setTag] = useState("");
  const [result, setResult] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function run(e: React.FormEvent) {
    e.preventDefault();
    setLoading(true);
    setError("");
    setResult("");
    try {
      const key = sessionStorage.getItem("memoratum_key") ?? "";
      const res = await fetch("/v4/import", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(key ? { Authorization: `Bearer ${key}` } : {}),
        },
        body: JSON.stringify({ graph_dir: dir, tag }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.error?.message ?? `HTTP ${res.status}`);
      setResult(`upserted ${body.facts_upserted} facts, deleted ${body.facts_deleted}`);
      onImported(`graphify:${tag}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  return (
    <section aria-label="Import graph">
      <h2>Import</h2>
      <p className="muted">Import a graphify snapshot from a server-local directory into a tag.</p>
      <form onSubmit={run} style={{ display: "flex", gap: "0.5rem", flexWrap: "wrap" }}>
        <label className="muted" htmlFor="imp-dir">
          Directory{" "}
        </label>
        <input
          id="imp-dir"
          value={dir}
          onChange={(e) => setDir(e.target.value)}
          placeholder="/path/to/graphify-out"
          style={{ flex: 1, minWidth: "220px" }}
        />
        <label className="muted" htmlFor="imp-tag">
          Tag{" "}
        </label>
        <input id="imp-tag" value={tag} onChange={(e) => setTag(e.target.value)} placeholder="lunee" />
        <button type="submit">Import</button>
      </form>
      {loading && <div className="skeleton" aria-label="Importing" />}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {result && (
        <p role="status">
          {result}
        </p>
      )}
    </section>
  );
}
