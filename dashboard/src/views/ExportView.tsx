import { useState } from "react";
import { api } from "../api";
import type { GraphFact } from "../types";

interface Note {
  name: string;
  content: string;
}

function subjectNotes(tag: string, facts: GraphFact[]): Note[] {
  const bySubject = new Map<string, GraphFact[]>();
  for (const f of facts) {
    const list = bySubject.get(f.subject) ?? [];
    list.push(f);
    bySubject.set(f.subject, list);
  }
  return [...bySubject.entries()].map(([subject, fs]) => {
    const live = fs.filter((f) => f.valid_to == null);
    const dead = fs.filter((f) => f.valid_to != null);
    const lines = live.map((f) => `${f.predicate} :: ${f.object}`);
    const history = dead.map((f) => `  - ${f.predicate} :: ${f.object} (superseded)`);
    const body = [
      "---",
      `containerTag: ${tag}`,
      `memoratum-subject: ${subject}`,
      "---",
      "",
      ...lines,
      ...(history.length ? ["", "## History", ...history] : []),
      "",
    ].join("\n");
    const safe = subject.replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 80) || "untitled";
    return { name: `${safe}.md`, content: body };
  });
}

function download(name: string, content: string) {
  const url = URL.createObjectURL(new Blob([content], { type: "text/markdown" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

export function ExportView({ tag }: { tag: string }) {
  const [notes, setNotes] = useState<Note[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function build() {
    setLoading(true);
    setError("");
    try {
      const r = await api.facts(tag, 10000);
      setNotes(subjectNotes(tag, r.facts));
      setOpen(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  const current = notes.find((n) => n.name === open);
  return (
    <section aria-label="Export vault">
      <h2>
        Export <span className="mono muted">{tag}</span>
      </h2>
      <p className="muted">
        Render the tag as vault-style markdown notes (frontmatter carries the subject for re-import).
      </p>
      <button onClick={build}>Build notes</button>{" "}
      <button
        disabled={notes.length === 0}
        onClick={() => download(`${tag}-vault.json`, JSON.stringify(notes, null, 2))}
      >
        Download JSON bundle
      </button>
      {loading && <div className="skeleton" aria-label="Building notes" />}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      <div style={{ display: "grid", gridTemplateColumns: "240px 1fr", gap: "1rem", marginTop: "1rem" }}>
        <ul style={{ listStyle: "none", padding: 0 }}>
          {notes.map((n) => (
            <li key={n.name}>
              <button style={{ width: "100%", textAlign: "left" }} onClick={() => setOpen(n.name)}>
                {n.name}
              </button>
            </li>
          ))}
        </ul>
        <div>
          {current ? (
            <>
              <button onClick={() => download(current.name, current.content)}>Download .md</button>
              <pre className="mono" style={{ whiteSpace: "pre-wrap" }}>
                {current.content}
              </pre>
            </>
          ) : (
            notes.length > 0 && <p className="muted">Select a note to preview.</p>
          )}
        </div>
      </div>
    </section>
  );
}
