import type { ReactNode } from "react";
import { ExportIcon } from "./icons";
import { GraphIcon } from "./icons";
import { ImportIcon } from "./icons";
import { SearchIcon } from "./icons";
import { TagsIcon } from "./icons";
import type { ServerStatus } from "./useServerStatus";

const RIBBON = [
  { id: "tags", label: "Tags", Icon: TagsIcon },
  { id: "graph", label: "Graph", Icon: GraphIcon },
  { id: "search", label: "Search", Icon: SearchIcon },
  { id: "import", label: "Import", Icon: ImportIcon },
  { id: "export", label: "Export", Icon: ExportIcon },
] as const;

export function Shell({
  view,
  onView,
  status,
  left,
  children,
  right,
}: {
  view: string;
  onView: (v: "tags" | "graph" | "search" | "import" | "export") => void;
  status: ServerStatus;
  left: ReactNode;
  children: ReactNode;
  right: ReactNode;
}) {
  return (
    <div className="shell">
      <div className="ribbon" role="navigation" aria-label="Primary">
        {RIBBON.map((item) => (
          <button
            key={item.id}
            aria-label={item.label}
            aria-current={view === item.id ? "page" : undefined}
            title={item.label}
            onClick={() => onView(item.id)}
          >
            <item.Icon label="" />
          </button>
        ))}
      </div>
      <aside className="sidebar left" aria-label="Explorer">
        {left}
      </aside>
      <main className="center">{children}</main>
      <aside className="sidebar right" aria-label="Details">
        {right}
      </aside>
      <footer className="statusbar" aria-label="Server status">
        <span aria-label={status.ok ? "Server online" : "Server offline"}>
          <span className={status.ok ? "dot ok" : "dot bad"} aria-hidden="true" />{" "}
          {status.ok ? "online" : "offline"}
        </span>
        <span className="mono">
          {status.tag}: {status.facts} facts · {status.documents} docs
          {status.latencyMs != null ? ` · ${status.latencyMs}ms` : ""}
        </span>
      </footer>
    </div>
  );
}
