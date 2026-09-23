import { useState } from "react";

export function TagSwitcher({ tag, onTag }: { tag: string; onTag: (t: string) => void }) {
  const [value, setValue] = useState("");
  const [recent, setRecent] = useState<string[]>(["default", "graphify:lunee"]);
  const open = (t: string) => {
    const name = t.trim();
    if (!name) return;
    setRecent((r) => [name, ...r.filter((x) => x !== name)].slice(0, 8));
    onTag(name);
  };
  return (
    <section aria-label="Tags">
      <h2>Vault</h2>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          open(value);
          setValue("");
        }}
      >
        <label className="muted" htmlFor="tag-open">
          Open tag{" "}
        </label>
        <input
          id="tag-open"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="graphify:lunee"
        />
      </form>
      <ul style={{ listStyle: "none", padding: 0 }}>
        {recent.map((t) => (
          <li key={t}>
            <button
              style={{ width: "100%", textAlign: "left" }}
              aria-current={t === tag ? "page" : undefined}
              onClick={() => open(t)}
            >
              <span className="mono">{t}</span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
