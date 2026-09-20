import { useState } from "react";
import { KeyField } from "./components/KeyField";
import { TagsView } from "./views/TagsView";

type View = "tags" | "graph";

export function App() {
  const [view, setView] = useState<View>("tags");
  const [tag, setTag] = useState("default");
  const [keyEpoch, setKeyEpoch] = useState(0);

  return (
    <>
      <header className="topbar">
        <h1>Memoratum Console</h1>
        <nav aria-label="Views">
          <button aria-current={view === "tags" ? "page" : undefined} onClick={() => setView("tags")}>
            Tags
          </button>
          <button aria-current={view === "graph" ? "page" : undefined} onClick={() => setView("graph")}>
            Graph
          </button>
        </nav>
        <span className="spacer" />
        <KeyField onChange={() => setKeyEpoch((n) => n + 1)} />
      </header>
      <main key={keyEpoch}>
        {view === "tags" && (
          <TagsView
            onSelect={(t) => {
              setTag(t);
              setView("graph");
            }}
          />
        )}
        {view === "graph" && (
          <section aria-label="Graph">
            <h2 className="mono">{tag}</h2>
            <p className="muted">Interactive graph lands in the next milestone.</p>
          </section>
        )}
      </main>
    </>
  );
}
