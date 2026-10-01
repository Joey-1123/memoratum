// Mem0 TypeScript SDK contract (RED). Run: node --experimental-strip-types test.ts
import { strict as assert } from "node:assert";
import { Client } from "./index.ts";
import { Mem0CompatClient } from "./mem0-contract.ts";

const calls: Array<[string, string, unknown]> = [];
// @ts-ignore stub fetch
globalThis.fetch = async (
  url: string,
  opts: { method?: string; body?: string; headers?: Record<string, string> },
) => {
  const body = opts.body ? JSON.parse(opts.body) : {};
  calls.push([url, opts.method ?? "GET", body]);
  let payload: unknown = { results: [{ memory: "user loves Paris", similarity: 0.9 }], timing: 1, total: 1 };
  if (url.endsWith("/v1/ping/")) {
    payload = { status: "ok", org_id: "org-1", project_id: "project-1" };
  } else if (url.includes("/api/v1/orgs/organizations/org-1/projects/")) {
    payload = [{ id: "project-1", org_id: "org-1", name: "Contract" }];
  } else if (url.includes("/api/v1/webhooks/projects/project-1/")) {
    payload = [{ id: "wh-1", project_id: "project-1", name: "Hook", url: "https://example.com", event_types: ["memory_add"], is_active: true }];
  } else if (url.includes("/v1/memories/mem_1/history/")) {
    payload = [{ id: "h1", memory_id: "mem_1", event: "ADD", new_memory: "hello" }];
  } else if (url.includes("/v1/memories/mem_1/") && (opts.method ?? "GET") === "GET") {
    payload = { id: "mem_1", memory: "hello", metadata: {} };
  } else if (url.includes("/v1/memories/mem_1/") && opts.method === "PUT") {
    payload = { id: "mem_1", memory: body.text, metadata: body.metadata ?? {} };
  } else if (url.includes("/v1/memories/mem_1/") && opts.method === "DELETE") {
    payload = { message: "Memory deleted successfully!", cascade_count: 0 };
  } else if (url.includes("/v1/memories/") && opts.method === "DELETE") {
    payload = { message: "Delete in progress. This may take some time.", event_id: "e-delete" };
  } else if (url.includes("/v3/memories/") && opts.method === "POST" && !url.endsWith("/v3/memories/add/")) {
    payload = { count: 1, next: null, previous: null, results: [{ id: "mem_1", memory: "hello" }] };
  } else if (url.endsWith("/v3/memories/add/")) {
    payload = { event_id: "e1", status: "PENDING" };
  } else if (url.endsWith("/v1/batch/")) {
    payload = { message: `Successfully ${opts.method === "PUT" ? "updated" : "deleted"} 1 memories` };
  } else if (url.endsWith("/v3/documents")) {
    payload = { id: "d1", status: "done" };
  }
  return { ok: true, status: 200, json: async () => payload };
};

const c = new Client({ baseURL: "http://x", apiKey: "k" });
const doc = await c.add("hello", { containerTag: "u1" });
assert.equal(doc.id, "d1");
assert.equal((calls[0][2] as { containerTag: string }).containerTag, "u1");
const res = await c.search("paris", { containerTag: "u1", searchMode: "memories" });
assert.equal(res.total, 1);
assert.equal(res.results[0].memory, "user loves Paris");

const mem0 = new Mem0CompatClient("http://x", "k", globalThis.fetch);
const added = await mem0.add({
  messages: [{ role: "user", content: "hello" }],
  user_id: "u1",
});
assert.ok(added.status === "PENDING" || Array.isArray(added.results));
assert.equal(calls[2][0], "http://x/v3/memories/add/");
assert.equal((calls[2][2] as { user_id: string }).user_id, "u1");
const searched = await mem0.search({ query: "hello", filters: { user_id: "u1" } });
assert.equal(searched.results.length, 1);
assert.equal(calls[3][0], "http://x/v3/memories/search/");

const memory = await mem0.get("mem_1");
assert.equal(memory.memory, "hello");
assert.equal(calls[4][1], "GET");
const all = await mem0.getAll({ filters: { user_id: "u1" }, page: 1, page_size: 10 });
assert.equal(all.count, 1);
assert.equal(calls[5][0], "http://x/v3/memories/?page=1&page_size=10");
const updated = await mem0.update("mem_1", { text: "updated" });
assert.equal(updated.memory, "updated");
assert.equal(calls[6][1], "PUT");
const deleted = await mem0.delete("mem_1", true);
assert.match(deleted.message, /deleted/);
assert.equal(calls[7][1], "DELETE");
const history = await mem0.history("mem_1");
assert.equal(history[0].event, "ADD");
assert.equal(calls[8][1], "GET");
const batchUpdated = await mem0.batchUpdate([{ memory_id: "mem_1", text: "batch" }]);
assert.match(batchUpdated.message, /updated/);
assert.equal(calls[9][1], "PUT");
const batchDeleted = await mem0.batchDelete([{ memory_id: "mem_1" }]);
assert.match(batchDeleted.message, /deleted/);
assert.equal(calls[10][1], "DELETE");
const deletedAll = await mem0.deleteAll({ user_id: "u1" });
assert.equal(deletedAll.event_id, "e-delete");
assert.equal(calls[11][0], "http://x/v1/memories/?user_id=u1");
const ping = await mem0.ping();
assert.equal(ping.status, "ok");
const projects = await mem0.listProjects("org-1");
assert.equal(projects.length, 1);
const webhooks = await mem0.listWebhooks("project-1");
assert.equal(webhooks.length, 1);
console.log("TS_SDK_ASSERTS_OK");
