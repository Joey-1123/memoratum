import { useState } from "react";
import { CommandPalette } from "./components/CommandPalette";
import { KeyField } from "./components/KeyField";
import { ExportView } from "./views/ExportView";
import { GraphPanel } from "./views/Inspector";
import { ImportView } from "./views/ImportView";
import { SearchView } from "./views/SearchView";
import { TagsView } from "./views/TagsView";

type View = "tags" | "graph" | "search" | "import" | "export";

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
          <button aria-current={view === "import" ? "page" : undefined} onClick={() => setView("import")}>
            Import
          </button>
          <button aria-current={view === "export" ? "page" : undefined} onClick={() => setView("export")}>
            Export
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
          { id: "import", label: "Go to Import", run: () => setView("import") },
          { id: "export", label: "Go to Export", run: () => setView("export") },
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
        {view === "import" && (
          <ImportView
            onImported={(t) => {
              setTag(t);
              setView("graph");
            }}
          />
        )}
        {view === "export" && <ExportView key={`e-${tag}`} tag={tag} />}
      </main>
    </>
  );
}
