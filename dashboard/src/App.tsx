import { useState } from "react";
import { CommandPalette } from "./components/CommandPalette";
import { HoverCard } from "./components/HoverCard";
import { KeyField } from "./components/KeyField";
import { Explorer } from "./shell/Explorer";
import { Shell } from "./shell/Shell";
import { TagSwitcher } from "./shell/TagSwitcher";
import { useServerStatus } from "./shell/useServerStatus";
import { ExportView } from "./views/ExportView";
import { GraphPanel } from "./views/Inspector";
import { ImportView } from "./views/ImportView";
import { NotePane } from "./views/NotePane";
import { SearchView } from "./views/SearchView";
import { TagsView } from "./views/TagsView";

type View = "tags" | "graph" | "search" | "import" | "export" | "note";

export function App() {
  const [view, setView] = useState<View>("tags");
  const [tag, setTag] = useState("default");
  const [keyEpoch, setKeyEpoch] = useState(0);
  const [notes, setNotes] = useState<string[]>([]);
  const [activeNote, setActiveNote] = useState<string | null>(null);
  const [hover, setHover] = useState<{ subject: string; x: number; y: number } | null>(null);
  const status = useServerStatus(tag, keyEpoch);

  const onHover = (subject: string | null, x: number, y: number) => {
    setHover(subject ? { subject, x, y } : null);
  };
  const openNote = (subject: string) => {
    setNotes((n) => (n.includes(subject) ? n : [...n, subject].slice(-8)));
    setActiveNote(subject);
    setView("note");
  };
  const closeNote = (subject: string) => {
    setNotes((n) => {
      const rest = n.filter((x) => x !== subject);
      if (activeNote === subject) setActiveNote(rest[rest.length - 1] ?? null);
      return rest;
    });
  };

  return (
    <>
      <CommandPalette
        actions={[
          { id: "tags", label: "Go to Tags", run: () => setView("tags") },
          { id: "graph", label: "Go to Graph", run: () => setView("graph") },
          { id: "search", label: "Go to Search", run: () => setView("search") },
          { id: "import", label: "Go to Import", run: () => setView("import") },
          { id: "export", label: "Go to Export", run: () => setView("export") },
        ]}
      />
      <Shell
        view={view}
        onView={setView}
        status={status}
        left={
          <>
            <TagSwitcher
              tag={tag}
              onTag={(t) => {
                setTag(t);
                setView("graph");
              }}
            />
            <Explorer tag={tag} onOpen={openNote} />
            <section aria-label="Session">
              <h2>Session</h2>
              <KeyField onChange={() => setKeyEpoch((n) => n + 1)} />
            </section>
          </>
        }
        right={
          <section aria-label="Context">
            <h2>Context</h2>
            <p className="mono muted">{status.tag}</p>
            <p>
              {status.documents} documents · {status.chunks} chunks · {status.facts} facts
            </p>
            <p className="muted">Open a node in the graph to inspect it here in the next slice.</p>
          </section>
        }
      >
        <div key={keyEpoch}>
          {notes.length > 0 && (
            <div role="tablist" aria-label="Open notes" style={{ display: "flex", gap: "0.25rem", marginBottom: "0.5rem" }}>
              {notes.map((n) => (
                <span key={n} style={{ display: "inline-flex" }}>
                  <button
                    role="tab"
                    aria-selected={view === "note" && activeNote === n}
                    onClick={() => {
                      setActiveNote(n);
                      setView("note");
                    }}
                    title={n}
                  >
                    {n.length > 24 ? `${n.slice(0, 24)}…` : n}
                  </button>
                  <button aria-label={`Close ${n}`} onClick={() => closeNote(n)}>
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          {view === "tags" && (
            <TagsView
              onSelect={(t) => {
                setTag(t);
                setView("graph");
              }}
            />
          )}
          {view === "graph" && <GraphPanel key={tag} tag={tag} onOpenNote={openNote} />}
          {view === "search" && <SearchView key={`s-${tag}`} tag={tag} />}
          {view === "import" && (
            <ImportView
              onImported={(t) => {
                setTag(t);
                setView("graph");
              }}
            />
          )}
          {view === "export" && <ExportView key={`e-${tag}`} tag={tag} />}
          {view === "note" && activeNote && (
            <NotePane
              key={`${tag}:${activeNote}`}
              tag={tag}
              subject={activeNote}
              onOpen={openNote}
              onHover={onHover}
            />
          )}
        </div>
        {hover && <HoverCard tag={tag} subject={hover.subject} x={hover.x} y={hover.y} />}
      </Shell>
    </>
  );
}
