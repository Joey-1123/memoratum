import { useEffect, useState } from "react";
import { motion } from "motion/react";
import { api } from "../api";
import type { Profile } from "../types";

const KNOWN_TAGS = ["default"];

export function TagsView({ onSelect }: { onSelect: (tag: string) => void }) {
  const [tag, setTag] = useState("");
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

  async function load(tags: string[]) {
    setLoading(true);
    setError("");
    try {
      setProfiles(await Promise.all(tags.map((t) => api.profile(t))));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(KNOWN_TAGS);
  }, []);

  return (
    <section aria-label="Container tags">
      <h2>Tags</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const t = tag.trim();
          if (t) {
            void load([t, ...profiles.map((p) => p.containerTag)]);
            setTag("");
          }
        }}
      >
        <label className="muted" htmlFor="tag-input">
          Inspect tag{" "}
        </label>
        <input
          id="tag-input"
          value={tag}
          onChange={(e) => setTag(e.target.value)}
          placeholder="graphify:lunee"
        />{" "}
        <button type="submit">Load</button>
      </form>
      {loading && (
        <div className="grid" aria-busy="true" aria-label="Loading tags">
          {[0, 1].map((i) => (
            <div key={i} className="skeleton" />
          ))}
        </div>
      )}
      {error && (
        <p className="error" role="alert">
          {error}
        </p>
      )}
      {!loading && !error && profiles.length === 0 && (
        <div role="status" className="card">
          <h3>No tags loaded</h3>
          <p className="muted">Enter a container tag above, or ingest documents via the API first.</p>
        </div>
      )}
      <div className="grid">
        {profiles.map((p, i) => (
          <motion.article
            key={p.containerTag}
            className="card"
            initial={{ opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: 0.25, delay: i * 0.05 }}
          >
            <h3 className="mono">{p.containerTag}</h3>
            <p className="stat">
              {p.stats.facts} <span className="muted" style={{ fontSize: "0.85rem" }}>facts</span>
            </p>
            <p className="muted">
              {p.stats.documents} documents · {p.stats.chunks} chunks
            </p>
            <button onClick={() => onSelect(p.containerTag)}>Open graph</button>
          </motion.article>
        ))}
      </div>
    </section>
  );
}
