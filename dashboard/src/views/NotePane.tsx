import { useEffect, useState } from "react";
import { api } from "../api";
import { buildNotes, linkSegments, type Note } from "../notes";

export function NotePane({
  tag,
  subject,
  onOpen,
  onHover,
}: {
  tag: string;
  subject: string;
  onOpen: (subject: string) => void;
  onHover: (subject: string | null, x: number, y: number) => void;
}) {
  const [note, setNote] = useState<Note | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .facts(tag, 10000)
      .then((r) => setNote(buildNotes(r.facts).get(subject) ?? null))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, [tag, subject]);

  if (error)
    return (
      <p className="error" role="alert">
        {error}
      </p>
    );
  if (!note) return <div className="skeleton" aria-label="Loading note" />;
  return (
    <article aria-label={`Note ${subject}`}>
      <h2>{subject}</h2>
      {note.files.length > 0 && <p className="mono muted">{note.files.slice(0, 3).join(" · ")}</p>}
      <h3>Relations</h3>
      <ul>
        {note.out.map((r) => (
          <li key={r.factId}>
            <span className="muted">{r.predicate} </span>
            <LinkText text={r.target} onOpen={onOpen} onHover={onHover} />
          </li>
        ))}
        {note.out.length === 0 && <li className="muted">No outgoing relations.</li>}
      </ul>
      <h3>Referenced by</h3>
      <ul>
        {note.backlinks.map((r) => (
          <li key={r.factId}>
            <LinkText text={r.target} onOpen={onOpen} onHover={onHover} />{" "}
            <span className="muted">{r.predicate}</span>
          </li>
        ))}
        {note.backlinks.length === 0 && <li className="muted">No backlinks.</li>}
      </ul>
      {note.history.length > 0 && (
        <>
          <h3>History</h3>
          <ul>
            {note.history.map((r) => (
              <li key={r.factId} className="muted">
                {r.predicate} :: {r.target} (superseded)
              </li>
            ))}
          </ul>
        </>
      )}
    </article>
  );
}

function LinkText({
  text,
  onOpen,
  onHover,
}: {
  text: string;
  onOpen: (s: string) => void;
  onHover: (s: string | null, x: number, y: number) => void;
}) {
  return (
    <>
      {linkSegments(`[[${text}]]`).map((seg, i) =>
        seg.kind === "link" ? (
          <button
            key={i}
            style={{ padding: 0, border: "none", background: "none", color: "var(--accent)", cursor: "pointer", minHeight: "unset" }}
            onClick={() => onOpen(seg.value)}
            onMouseEnter={(e) => onHover(seg.value, e.clientX, e.clientY)}
            onMouseLeave={() => onHover(null, 0, 0)}
            onFocus={(e) => {
              const r = (e.target as HTMLElement).getBoundingClientRect();
              onHover(seg.value, r.right, r.top);
            }}
            onBlur={() => onHover(null, 0, 0)}
          >
            {seg.value}
          </button>
        ) : (
          <span key={i}>{seg.value}</span>
        )
      )}
    </>
  );
}
