import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { WebhookDelivery } from "../types";

function timeLabel(value: number | null): string {
  return value == null ? "—" : new Date(value * 1000).toLocaleString();
}

export function WebhookDeliveryView() {
  const [projectID, setProjectID] = useState("local-project");
  const [deliveries, setDeliveries] = useState<WebhookDelivery[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    if (!projectID.trim()) return;
    setLoading(true);
    setError("");
    try {
      const result = await api.webhookDeliveries(projectID.trim());
      setDeliveries(result.deliveries);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setLoading(false);
    }
  }, [projectID]);

  useEffect(() => {
    void load();
  }, [load]);

  async function replay(delivery: WebhookDelivery) {
    setError("");
    try {
      await api.replayWebhookDelivery(delivery.id);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }

  return (
    <section aria-label="Webhook delivery history">
      <div className="view-heading">
        <div>
          <h2>Webhook deliveries</h2>
          <p className="muted">Local attempt history. Signing secrets are never shown here.</p>
        </div>
        <button type="button" onClick={() => void load()} disabled={loading}>Refresh</button>
      </div>
      <form className="toolbar" onSubmit={(event) => { event.preventDefault(); void load(); }}>
        <label htmlFor="delivery-project">Project ID</label>
        <input id="delivery-project" value={projectID} onChange={(event) => setProjectID(event.target.value)} />
        <button type="submit" disabled={loading}>Load deliveries</button>
      </form>
      {error && <p className="error" role="alert">{error}</p>}
      {loading && <div className="skeleton" aria-label="Loading webhook deliveries" />}
      {!loading && deliveries.length === 0 && !error && <p className="empty-state" role="status">No webhook deliveries yet.</p>}
      {deliveries.length > 0 && (
        <div className="table-wrap">
          <table className="data-table">
            <caption className="sr-only">Webhook delivery attempts</caption>
            <thead><tr><th scope="col">Event</th><th scope="col">Status</th><th scope="col">Attempts</th><th scope="col">Next retry</th><th scope="col">Action</th></tr></thead>
            <tbody>
              {deliveries.map((delivery) => (
                <tr key={delivery.id}>
                  <td><div>{delivery.event_type}</div><div className="mono muted">{delivery.id}</div></td>
                  <td><span className={`status status-${delivery.status}`}>{delivery.status}</span>{delivery.last_error && <div className="muted">{delivery.last_error}</div>}</td>
                  <td className="num">{delivery.attempts}</td>
                  <td>{timeLabel(delivery.next_attempt_at)}</td>
                  <td><button type="button" onClick={() => void replay(delivery)} disabled={delivery.status === "succeeded"}>Replay</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
