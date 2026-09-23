import { useState } from "react";
import { CommandPalette } from "./components/CommandPalette";
import { KeyField } from "./components/KeyField";
import { Shell } from "./shell/Shell";
import { TagSwitcher } from "./shell/TagSwitcher";
import { useServerStatus } from "./shell/useServerStatus";
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
  const status = useServerStatus(tag, keyEpoch);

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
        </div>
      </Shell>
    </>
  );
}
