import { useEffect, useState } from "react";
import { api } from "../api";
import type { AuditEvent, UsageCounter } from "../types";

function timeLabel(value: number): string {
  return new Date(value * 1000).toLocaleString();
}

function scopeLabel(tag: string | null, org: string | null): string {
  return [tag ?? "wildcard", org ?? "all orgs"].join(" · ");
}

export function GovernanceView() {
  const [events, setEvents] = useState<AuditEvent[]>([]);
  const [usage, setUsage] = useState<UsageCounter[]>([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  async function load(nextOffset: number) {
    setLoading(true);
    setError("");
    try {
      const [audit, usagePage] = await Promise.all([
        api.audit(50, nextOffset),
        api.usage(50, nextOffset),
      ]);
      setEvents(audit.events);
      setUsage(usagePage.usage);
      setTotal(Math.max(audit.total, usagePage.total));
      setOffset(nextOffset);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(0);
  }, []);

  return (
    <section aria-label="Governance and local accounting">
      <div style={{ display: "flex", justifyContent: "space-between", gap: "1rem", alignItems: "center" }}>
        <div>
          <h2>Governance</h2>
          <p className="muted">Local audit activity and usage counters. Nothing leaves this server.</p>
        </div>
        <button type="button" onClick={() => void load(offset)} disabled={loading}>
          Refresh
        </button>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      {loading && <div className="skeleton" aria-label="Loading governance data" />}
      {!loading && !error && (
        <>
          <h3>Audit trail <span className="muted">({total} records)</span></h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <caption className="muted" style={{ textAlign: "left", captionSide: "top" }}>
                Most recent local accountability events
              </caption>
              <thead>
                <tr>
                  {[
                    "Time",
                    "Action",
                    "Actor",
                    "Scope",
                    "Resource",
                    "Outcome",
                  ].map((label) => (
                    <th key={label} scope="col" style={{ textAlign: "left", padding: "0.4rem" }}>
                      {label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {events.map((event) => (
                  <tr key={event.id} style={{ borderTop: "1px solid var(--background-modifier-border)" }}>
                    <td className="mono">{timeLabel(event.createdAt)}</td>
                    <td>{event.action}</td>
                    <td>{event.actorKind}</td>
                    <td>{scopeLabel(event.containerTag, event.orgId)}</td>
                    <td className="mono">{event.resourceType ?? "—"}</td>
                    <td>{event.outcome}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", margin: "0.75rem 0 1.5rem" }}>
            <button type="button" disabled={offset === 0 || loading} onClick={() => void load(Math.max(0, offset - 50))}>
              Previous
            </button>
            <span className="muted mono">{events.length ? offset + 1 : 0}–{offset + events.length}</span>
            <button type="button" disabled={offset + 50 >= total || loading} onClick={() => void load(offset + 50)}>
              Next
            </button>
          </div>

          <h3>Usage by local key fingerprint</h3>
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <caption className="muted" style={{ textAlign: "left", captionSide: "top" }}>
                Raw credentials are never returned
              </caption>
              <thead>
                <tr>
                  {["Fingerprint", "Scope", "Requests", "Searches", "Documents", "Facts", "Input chars"].map(
                    (label) => (
                      <th key={label} scope="col" style={{ textAlign: "left", padding: "0.4rem" }}>
                        {label}
                      </th>
                    )
                  )}
                </tr>
              </thead>
              <tbody>
                {usage.map((row) => (
                  <tr key={`${row.keyFingerprint}:${row.containerTag}:${row.orgId}`} style={{ borderTop: "1px solid var(--background-modifier-border)" }}>
                    <td className="mono">{row.keyFingerprint}</td>
                    <td>{scopeLabel(row.containerTag, row.orgId)}</td>
                    <td className="num">{row.requests}</td>
                    <td className="num">{row.searches}</td>
                    <td className="num">{row.documentWrites}</td>
                    <td className="num">{row.factWrites}</td>
                    <td className="num">{row.inputChars}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </section>
  );
}
