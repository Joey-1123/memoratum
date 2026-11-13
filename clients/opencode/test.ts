// Memoratum OpenCode plugin behaviour tests.
// Run: node --experimental-strip-types test.ts
import { strict as assert } from "node:assert"
import plugin from "./memoratum.js"

type Call = { url: string; body: Record<string, any> }

const calls: Call[] = []
let searchResult: { results: Array<{ memory?: string }> } = { results: [] }
let failure: Error | null = null
let searchStatus = 200

// @ts-ignore stub fetch
globalThis.fetch = async (url: string, opts: { method?: string; body?: string }) => {
  const body = opts.body ? JSON.parse(opts.body) : {}
  calls.push({ url, body })
  if (failure) throw failure
  if (url.includes("/v4/search")) {
    return {
      ok: searchStatus === 200,
      status: searchStatus,
      text: async () => JSON.stringify(searchResult),
    }
  }
  if (url.includes("/v3/documents")) {
    return { ok: true, status: 201, text: async () => JSON.stringify({ id: "d1", status: "queued" }) }
  }
  return { ok: false, status: 500, text: async () => "" }
}

const message = (id: string, role: string, text: string) => ({
  info: { id, role },
  parts: [{ type: "text", text }],
})

function makeInput(source: { messages: Array<ReturnType<typeof message>> } = { messages: [] }) {
  return {
    project: { id: "proj-1" },
    directory: "/work/project",
    client: {
      session: {
        messages: async (_options: unknown) => ({ data: source.messages }),
      },
    },
  }
}

const reset = () => {
  calls.length = 0
  searchResult = { results: [] }
  failure = null
  searchStatus = 200
  delete process.env.MEMORATUM_TAG
}

// --- the export shape the loader requires -----------------------------------

assert.equal(typeof plugin.id, "string", "the plugin must export an id")
assert.equal(plugin.id, "memoratum")
assert.equal(typeof plugin.server, "function", "released opencode calls server()")
assert.equal(typeof plugin.setup, "function", "V2 opencode calls setup()")

// --- recall ------------------------------------------------------------------

reset()
searchResult = { results: [{ memory: "we chose RRF because FTS5 has no vector index" }] }
{
  const hooks = await plugin.server(makeInput())
  const output = { system: ["base system prompt"] }
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "why RRF?" }] })
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, output)

  assert.equal(output.system.length, 2, "the memory block must be pushed onto the system array")
  assert.ok(output.system[1].startsWith("[memoratum]"), "the block must be labelled")
  assert.match(output.system[1], /RRF/, "the recalled fact must be in the block")

  const search = calls.find((call) => call.url.includes("/v4/search"))
  assert.ok(search, "recall must call /v4/search")
  assert.equal(search.body.containerTag, "opencode:proj-1", "the tag must come from the project id")
  assert.equal(search.body.searchMode, "memories", "recall must query facts, not chunks")
  assert.equal(search.body.q, "why RRF?")
}

// --- the user's prompt is never rewritten ------------------------------------

reset()
searchResult = { results: [{ memory: "a fact" }] }
{
  const hooks = await plugin.server(makeInput())
  const parts = [{ type: "text", text: "the user said this" }]
  const output = { message: {}, parts }
  await hooks["chat.message"]({ sessionID: "s1" }, output)
  assert.deepEqual(output.parts, parts, "the prompt parts must not be modified")
  assert.equal(output.parts[0].text, "the user said this")
}

// --- the injected block stays inside its budget ------------------------------

reset()
searchResult = {
  results: Array.from({ length: 5 }, (_, index) => ({ memory: `fact number ${index} `.repeat(40) })),
}
{
  const hooks = await plugin.server(makeInput())
  const output = { system: [] as string[] }
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "anything" }] })
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, output)
  assert.equal(output.system.length, 1)
  assert.ok(output.system[0].length <= 600, `the block was ${output.system[0].length} chars, budget is 600`)
}

// --- a server outage must not break the session ------------------------------

reset()
failure = new Error("connection refused")
{
  const hooks = await plugin.server(makeInput())
  const output = { system: ["base"] }
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "hello" }] })
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, output)
  assert.equal(output.system.length, 1, "nothing may be injected when recall fails")
}

reset()
searchStatus = 503
{
  const hooks = await plugin.server(makeInput())
  const output = { system: ["base"] }
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "hello" }] })
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, output)
  assert.equal(output.system.length, 1, "nothing may be injected when the server errors")
}

// --- recall happens once per turn, not once per model call -------------------

reset()
searchResult = { results: [{ memory: "a fact" }] }
{
  const hooks = await plugin.server(makeInput())
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "hello" }] })
  const first = { system: [] as string[] }
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, first)
  const second = { system: [] as string[] }
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, second)
  assert.equal(calls.filter((call) => call.url.includes("/v4/search")).length, 1, "recall must be cached per turn")
  assert.equal(second.system.length, 1, "the cached block must still be injected")
}

// --- the operator can pin one tag for every project --------------------------

reset()
process.env.MEMORATUM_TAG = "team-shared"
searchResult = { results: [{ memory: "a fact" }] }
{
  const hooks = await plugin.server(makeInput())
  await hooks["chat.message"]({ sessionID: "s1" }, { parts: [{ type: "text", text: "hello" }] })
  await hooks["experimental.chat.system.transform"]({ sessionID: "s1", model: {} }, { system: [] })
  const search = calls.find((call) => call.url.includes("/v4/search"))
  assert.equal(search?.body.containerTag, "team-shared", "MEMORATUM_TAG must win over the project id")
}
delete process.env.MEMORATUM_TAG

// --- capture at compaction ----------------------------------------------------

reset()
{
  const transcript = { messages: [message("msg_1", "user", "we should use RRF"), message("msg_2", "assistant", "agreed, RRF it is")] }
  const hooks = await plugin.server(makeInput(transcript))
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})

  const post = calls.find((call) => call.url.includes("/v3/documents"))
  assert.ok(post, "compaction must store the transcript")
  assert.equal(post.body.containerTag, "opencode:proj-1")
  assert.equal(post.body.customId, "oc:s1:msg_2", "the custom id must carry the last captured message id")
  assert.match(post.body.content, /we should use RRF/)
  assert.match(post.body.content, /agreed, RRF it is/)
}

// --- a repeated compaction must not duplicate the document --------------------

reset()
{
  const transcript = { messages: [message("msg_1", "user", "one"), message("msg_2", "assistant", "two")] }
  const hooks = await plugin.server(makeInput(transcript))
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})
  const posts = calls.filter((call) => call.url.includes("/v3/documents"))
  assert.equal(posts.length, 1, "a repeated compaction must not store a second document")
}

// --- a second compaction stores only what is new ------------------------------

reset()
{
  const transcript = { messages: [message("msg_1", "user", "one"), message("msg_2", "assistant", "two")] }
  const hooks = await plugin.server(makeInput(transcript))
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})

  transcript.messages = [...transcript.messages, message("msg_3", "user", "three")]
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})

  const posts = calls.filter((call) => call.url.includes("/v3/documents"))
  assert.equal(posts.length, 2, "a second compaction stores a second segment")
  assert.equal(posts[1].body.customId, "oc:s1:msg_3")
  assert.ok(!posts[1].body.content.includes("one"), "the second segment must not repeat the first")
  assert.match(posts[1].body.content, /three/)
}

// --- a restart must not duplicate: the same segment gets the same id ----------

reset()
{
  const transcript = { messages: [message("msg_1", "user", "one")] }
  const hooks = await plugin.server(makeInput(transcript))
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})
  const restarted = await plugin.server(makeInput(transcript))
  await restarted["experimental.session.compacting"]({ sessionID: "s1" }, {})
  const posts = calls.filter((call) => call.url.includes("/v3/documents"))
  assert.equal(posts[0].body.customId, posts[1].body.customId, "the same segment must reuse the same id")
}

// --- capture must not throw when the transcript is unavailable -----------------

reset()
{
  const hooks = await plugin.server({
    project: { id: "proj-1" },
    directory: "/work/project",
    client: {
      session: {
        messages: async () => {
          throw new Error("server gone")
        },
      },
    },
  })
  await hooks["experimental.session.compacting"]({ sessionID: "s1" }, {})
  assert.equal(calls.filter((call) => call.url.includes("/v3/documents")).length, 0)
}

// --- the V2 adapter registers the same three hooks ---------------------------

reset()
{
  const registered: Record<string, (event: any) => Promise<void>> = {}
  const ctx = {
    location: { project: { id: "proj-2" } },
    session: {
      hook: async (name: string, callback: (event: any) => Promise<void>) => {
        registered[name] = callback
        return { dispose: async () => {} }
      },
    },
  }
  await plugin.setup(ctx)
  assert.ok(registered.prompt, "the V2 adapter must register the prompt hook")
  assert.ok(registered.context, "the V2 adapter must register the context hook")
  assert.ok(registered.compaction, "the V2 adapter must register the compaction hook")

  searchResult = { results: [{ memory: "a fact" }] }
  await registered.prompt({ sessionID: "s2", prompt: { text: "what did we decide?" } })
  const event = { sessionID: "s2", system: [] as any[] }
  await registered.context(event)
  assert.equal(event.system.length, 1, "the V2 adapter must push a system part")
  assert.equal(event.system[0].type, "text")
  assert.match(event.system[0].text, /a fact/)
  const search = calls.find((call) => call.url.includes("/v4/search"))
  assert.equal(search?.body.containerTag, "opencode:proj-2")

  await registered.compaction({ sessionID: "s2", messages: [message("msg_9", "user", "remember this")] })
  const post = calls.find((call) => call.url.includes("/v3/documents"))
  assert.ok(post, "the V2 compaction hook must store the transcript")
  assert.equal(post.body.customId, "oc:s2:msg_9")
}

console.log("OPENCODE_PLUGIN_ASSERTS_OK")
