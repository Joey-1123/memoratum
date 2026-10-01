import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { MemoryHistoryEntry, MemoryRecord } from "../types";

function timeLabel(value: number | string | undefined): string {
  if (value == null) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

export function MemoryLifecycleView({ onChanged }: { onChanged?: () => void }) {
  const [scope, setScope] = useState("user_id:dashboard");
  const [page, setPage] = useState(1);
  const [memories, setMemories] = useState<MemoryRecord[]>([]);
  const [count, setCount] = useState(0);
  const [selected, setSelected] = useState<MemoryRecord | null>(null);
  const [draft, setDraft] = useState("");
  const [history, setHistory] = useState<MemoryHistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const entityScope = scope.startsWith("mem0:") ? scope : `mem0:${scope.trim()}`;

  const load = useCallback(async () => {
    const [key, value] = entityScope.split(":");
    if (!key || !value) {
      setError("Enter a user, agent, app, or run scope.");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const result = await api.memoryPage({ [key]: value }, page, 50);
      setMemories(result.results);
      setCount(result.count);
      setSelected(null);
      setHistory([]);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [entityScope, page]);

  useEffect(() => {
    void load();
  }, [load]);

  async function openMemory(memory: MemoryRecord) {
    setSelected(memory);
    setDraft(memory.memory);
    setError("");
    try {
      setHistory(await api.memoryHistory(memory.id));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  async function save() {
    if (!selected || !draft.trim()) return;
    setSaving(true);
    setError("");
    try {
      const updated = await api.updateMemory(selected.id, { text: draft });
      setSelected(updated);
      setHistory(await api.memoryHistory(updated.id));
      await load();
      onChanged?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!selected || !window.confirm(`Delete ${selected.id}?`)) return;
    setSaving(true);
    setError("");
    try {
      await api.deleteMemory(selected.id);
      setSelected(null);
      setHistory([]);
      await load();
      onChanged?.();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSaving(false);
    }
  }

  return (
    <section aria-label="Memory lifecycle">
      <div className="view-heading">
        <div>
          <h2>Memory lifecycle</h2>
          <p className="muted">Inspect, edit, delete, and audit canonical memories.</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>
          Refresh
        </button>
      </div>
      <form
        className="toolbar"
        onSubmit={(event) => {
          event.preventDefault();
          setPage(1);
          void load();
        }}
      >
        <label htmlFor="memory-scope">Entity scope</label>
        <input
          id="memory-scope"
          value={scope}
          onChange={(event) => setScope(event.target.value)}
          placeholder="user_id:alice"
        />
        <button type="submit" disabled={loading}>
          Load memories
        </button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {loading && <div className="skeleton" aria-label="Loading memories" />}
      {!loading && memories.length === 0 && !error && (
        <p className="empty-state" role="status">No active memories in this scope.</p>
      )}
      {memories.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <caption className="sr-only">Canonical memories in the selected scope</caption>
            <thead>
              <tr><th scope="col">Memory</th><th scope="col">Version</th><th scope="col">Updated</th></tr>
            </thead>
            <tbody>
              {memories.map((memory) => (
                <tr key={memory.id} className={selected?.id === memory.id ? "selected" : undefined}>
                  <td>
                    <button className="link-button" type="button" onClick={() => void openMemory(memory)}>
                      {memory.memory}
                    </button>
                    <div className="mono muted">{memory.id}</div>
                  </td>
                  <td className="num">{memory.version}</td>
                  <td>{timeLabel(memory.updated_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {count > memories.length && (
        <div className="pagination">
          <button type="button" disabled={page <= 1} onClick={() => setPage((value) => value - 1)}>Previous</button>
          <span>Page {page} · {count} memories</span>
          <button type="button" disabled={page * 50 >= count} onClick={() => setPage((value) => value + 1)}>Next</button>
        </div>
      )}
      {selected && (
        <section className="card detail-card" aria-label={`Edit ${selected.id}`}>
          <div className="view-heading">
            <h3>Edit memory</h3>
            <button type="button" onClick={() => setSelected(null)} aria-label="Close memory editor">×</button>
          </div>
          <label htmlFor="memory-text">Memory text</label>
          <textarea id="memory-text" value={draft} onChange={(event) => setDraft(event.target.value)} rows={4} />
          <div className="actions">
            <button type="button" onClick={() => void save()} disabled={saving || !draft.trim()}>Save changes</button>
            <button type="button" className="danger-button" onClick={() => void remove()} disabled={saving}>Delete</button>
          </div>
          <h4>History</h4>
          {history.length === 0 ? <p className="muted">No history loaded.</p> : (
            <ol className="history-list">
              {history.map((entry) => (
                <li key={entry.id}>
                  <strong>{entry.event}</strong> · v{entry.version} · {timeLabel(entry.created_at)}
                  {entry.old_memory && <div className="muted">Previous: {entry.old_memory}</div>}
                  {entry.new_memory && <div>Current: {entry.new_memory}</div>}
                </li>
              ))}
            </ol>
          )}
        </section>
      )}
    </section>
  );
}
