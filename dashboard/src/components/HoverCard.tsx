import { useEffect, useState } from "react";
import { api } from "../api";
import { buildNotes } from "../notes";

export function HoverCard({ tag, subject, x, y }: { tag: string; subject: string; x: number; y: number }) {
  const [lines, setLines] = useState<string[] | null>(null);

  useEffect(() => {
    let live = true;
    api
      .facts(tag, 10000)
      .then((r) => {
        if (!live) return;
        const note = buildNotes(r.facts).get(subject);
        setLines(note ? note.out.slice(0, 4).map((rel) => `${rel.predicate} :: ${rel.target}`) : ["No relations."]);
      })
      .catch(() => live && setLines(["Preview unavailable."]));
    return () => {
      live = false;
    };
  }, [tag, subject]);

  const style: React.CSSProperties = {
    position: "fixed",
    left: Math.min(x + 12, window.innerWidth - 280),
    top: Math.min(y + 12, window.innerHeight - 160),
    width: 260,
    zIndex: 60,
    pointerEvents: "none",
  };
  return (
    <div className="card" style={style} role="status" aria-label={`Preview of ${subject}`}>
      <strong className="mono">{subject}</strong>
      {lines == null ? (
        <div className="skeleton" aria-label="Loading preview" />
      ) : (
        <ul style={{ paddingLeft: "1rem", margin: "0.25rem 0" }}>
          {lines.map((l, i) => (
            <li key={i} className="mono muted">
              {l}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
