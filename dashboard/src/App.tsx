import { useState } from "react";
import { CommandPalette } from "./components/CommandPalette";
import { KeyField } from "./components/KeyField";
import { GraphPanel } from "./views/Inspector";
import { SearchView } from "./views/SearchView";
import { TagsView } from "./views/TagsView";

type View = "tags" | "graph" | "search";

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
          <button aria-current={view === "search" ? "page" : undefined} onClick={() => setView("search")}>
            Search
          </button>
        </nav>
        <span className="spacer" />
        <KeyField onChange={() => setKeyEpoch((n) => n + 1)} />
      </header>
      <CommandPalette
        actions={[
          { id: "tags", label: "Go to Tags", run: () => setView("tags") },
          { id: "graph", label: "Go to Graph", run: () => setView("graph") },
          { id: "search", label: "Go to Search", run: () => setView("search") },
        ]}
      />
      <main key={keyEpoch}>
        {view === "tags" && (
          <TagsView
            onSelect={(t) => {
              setTag(t);
              setView("graph");
            }}
          />
        )}
        {view === "graph" && <GraphPanel key={tag} tag={tag} />}
        {view === "search" && <SearchView key={`s-${tag}`} tag={tag} />}
      </main>
    </>
  );
}
