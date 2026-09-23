import { useState } from "react";
import { PREDICATE_COLORS } from "./GraphView";

export function Legend({ predicates }: { predicates: string[] }) {
  const [open, setOpen] = useState(true);
  const shown = predicates.filter((p) => p in PREDICATE_COLORS);
  const other = predicates.filter((p) => !(p in PREDICATE_COLORS));
  if (shown.length === 0 && other.length === 0) return null;
  return (
    <div className="card" aria-label="Edge legend" style={{ marginBottom: "0.5rem" }}>
      <button
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        style={{ border: "none", background: "none", padding: 0, minHeight: "unset" }}
      >
        <span className="muted">Edge colors {open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <ul style={{ listStyle: "none", padding: 0, margin: "0.5rem 0 0", display: "flex", gap: "0.75rem", flexWrap: "wrap" }}>
          {shown.map((p) => (
            <li key={p} className="mono">
              <span
                aria-hidden="true"
                style={{ display: "inline-block", width: 16, height: 3, background: PREDICATE_COLORS[p], marginRight: 6, verticalAlign: "middle" }}
              />
              {p}
            </li>
          ))}
          {other.length > 0 && (
            <li className="mono muted">+{other.length} other relations in slate</li>
          )}
        </ul>
      )}
    </div>
  );
}
