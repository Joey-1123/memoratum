// Memoratum TS SDK contract (RED). Run: node --experimental-strip-types test.ts
import { strict as assert } from "node:assert";
import { Client } from "./index.ts";

const calls: Array<[string, unknown]> = [];
// @ts-ignore stub fetch
globalThis.fetch = async (url: string, opts: { body?: string }) => {
  const body = JSON.parse(opts.body ?? "{}");
  calls.push([url, body]);
  const payload = url.endsWith("/v3/documents")
    ? { id: "d1", status: "done" }
    : { results: [{ memory: "user loves Paris", similarity: 0.9 }], timing: 1, total: 1 };
  return { ok: true, status: 200, json: async () => payload };
};

const c = new Client({ baseURL: "http://x", apiKey: "k" });
const doc = await c.add("hello", { containerTag: "u1" });
assert.equal(doc.id, "d1");
assert.equal(calls[0][1].containerTag, "u1");
const res = await c.search("paris", { containerTag: "u1", searchMode: "memories" });
assert.equal(res.total, 1);
assert.equal(res.results[0].memory, "user loves Paris");
console.log("TS_SDK_ASSERTS_OK");
