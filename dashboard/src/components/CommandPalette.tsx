import { useEffect, useRef, useState } from "react";

export interface PaletteAction {
  id: string;
  label: string;
  run: () => void;
}

export function CommandPalette({ actions }: { actions: PaletteAction[] }) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const prevFocus = useRef<Element | null>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        prevFocus.current = document.activeElement;
        setOpen((o) => !o);
        setQ("");
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
    else prevFocus.current instanceof HTMLElement && prevFocus.current.focus();
  }, [open ]);

  if (!open) return null;
  const matches = actions.filter((a) => a.label.toLowerCase().includes(q.toLowerCase()));
  return (
    <div
      role="dialog"
      aria-label="Command palette"
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.5)",
        display: "flex",
        justifyContent: "center",
        paddingTop: "15vh",
        zIndex: 50,
      }}
      onClick={() => setOpen(false)}
    >
      <div className="card" style={{ height: "fit-content", width: "min(480px, 90vw)" }} onClick={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          aria-label="Commands"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="Type a command… (Esc to close)"
          style={{ width: "100%" }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && matches[0]) {
              matches[0].run();
              setOpen(false);
            }
          }}
        />
        <ul style={{ listStyle: "none", padding: 0 }}>
          {matches.map((a) => (
            <li key={a.id}>
              <button
                style={{ width: "100%", textAlign: "left" }}
                onClick={() => {
                  a.run();
                  setOpen(false);
                }}
              >
                {a.label}
              </button>
            </li>
          ))}
          {matches.length === 0 && <li className="muted">No matches.</li>}
        </ul>
      </div>
    </div>
  );
}
