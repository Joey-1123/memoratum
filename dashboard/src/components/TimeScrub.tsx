import { useMemo, useState } from "react";
import type { GraphFact } from "../types";

export function useTimeScrub(facts: GraphFact[]) {
  const bounds = useMemo(() => {
    const times = facts.flatMap((f) => [f.valid_from, f.valid_to ?? Date.now() / 1000]);
    if (!times.length) return null;
    return { min: Math.min(...times), max: Math.max(...times) };
  }, [facts]);
  const [cursor, setCursor] = useState<number | null>(null);

  const visible = useMemo(() => {
    if (cursor == null) return facts;
    return facts.filter((f) => f.valid_from <= cursor && (f.valid_to == null || f.valid_to > cursor));
  }, [facts, cursor]);

  if (!bounds) return { visible, scrubber: null as React.ReactNode };
  const fmt = (t: number) => new Date(t * 1000).toLocaleDateString();
  const scrubber = (
    <div style={{ display: "flex", gap: "0.5rem", alignItems: "center", marginBottom: "0.5rem" }}>
      <label className="muted" htmlFor="time-scrub">
        Valid at{" "}
      </label>
      <input
        id="time-scrub"
        type="range"
        min={bounds.min}
        max={bounds.max}
        step={(bounds.max - bounds.min) / 100 || 1}
        value={cursor ?? bounds.max}
        onChange={(e) => setCursor(Number(e.target.value))}
        style={{ flex: 1 }}
      />
      <span className="mono muted">{fmt(cursor ?? bounds.max)}</span>
      {cursor != null && (
        <button onClick={() => setCursor(null)}>
          Now
        </button>
      )}
    </div>
  );
  return { visible, scrubber };
}
