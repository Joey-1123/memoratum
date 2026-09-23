import { useEffect, useState } from "react";
import { api } from "../api";
import { buildNotes } from "../notes";

export function Explorer({ tag, onOpen }: { tag: string; onOpen: (subject: string) => void }) {
  const [subjects, setSubjects] = useState<string[]>([]);
  const [filter, setFilter] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .facts(tag, 10000)
      .then((r) => setSubjects([...buildNotes(r.facts).keys()].sort()))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [tag]);

  const shown = subjects.filter((s) => s.toLowerCase().includes(filter.toLowerCase())).slice(0, 200);
  return (
    <section aria-label="Notes explorer">
      <h2>Explorer</h2>
      <input
        aria-label="Filter notes"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter…"
      />
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <ul style={{ listStyle: "none", padding: 0 }}>
        {shown.map((s) => (
          <li key={s}>
            <button style={{ width: "100%", textAlign: "left" }} onClick={() => onOpen(s)} title={s}>
              <span className="mono">{s.length > 32 ? `${s.slice(0, 32)}…` : s}</span>
            </button>
          </li>
        ))}
      </ul>
      {subjects.length > 200 && <p className="muted">Showing first 200 of {subjects.length}.</p>}
    </section>
  );
}
