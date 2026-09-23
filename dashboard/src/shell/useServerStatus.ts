import { useEffect, useState } from "react";
import { api } from "../api";

export interface ServerStatus {
  ok: boolean;
  tag: string;
  documents: number;
  chunks: number;
  facts: number;
  latencyMs: number | null;
}

export function useServerStatus(tag: string, refreshKey: number): ServerStatus {
  const [status, setStatus] = useState<ServerStatus>({
    ok: false,
    tag,
    documents: 0,
    chunks: 0,
    facts: 0,
    latencyMs: null,
  });
  useEffect(() => {
    let live = true;
    const started = performance.now();
    Promise.all([
      fetch("/health").then((r) => r.ok),
      api.profile(tag).catch(() => null),
    ])
      .then(([ok, profile]) => {
        if (!live) return;
        setStatus({
          ok,
          tag,
          documents: profile?.stats.documents ?? 0,
          chunks: profile?.stats.chunks ?? 0,
          facts: profile?.stats.facts ?? 0,
          latencyMs: Math.round(performance.now() - started),
        });
      })
      .catch(() => live && setStatus((s) => ({ ...s, ok: false })));
    return () => {
      live = false;
    };
  }, [tag, refreshKey]);
  return status;
}
