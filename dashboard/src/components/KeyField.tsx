import { useState } from "react";
import { api } from "../api";

export function KeyField({ onChange }: { onChange: () => void }) {
  const [value, setValue] = useState(() => sessionStorage.getItem("memoratum_key") ?? "");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        api.setKey(value.trim());
        onChange();
      }}
    >
      <label className="muted" htmlFor="api-key">
        Admin key{" "}
      </label>
      <input
        id="api-key"
        type="password"
        autoComplete="off"
        placeholder="mm_… (memory only, never stored)"
        value={value}
        onChange={(e) => setValue(e.target.value)}
      />{" "}
      <button type="submit">Use</button>
    </form>
  );
}
