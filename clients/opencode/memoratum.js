// Memoratum OpenCode plugin — recall into the model's context, capture at
// session compaction.
//
// Two exports, one core:
//   server(input) — OpenCode 1.19.x and other released builds (V1 hooks)
//   setup(ctx)    — V2 builds (ctx.session.hook)
// A released build calls server(); a V2 build calls setup(). Each runtime
// uses only one, and the behaviour is identical.
//
// Copyright (C) 2026 Memoratum contributors — SPDX-License-Identifier: MIT
// (see LICENSE-MIT; client code exception to repo AGPL).

const BASE = (process.env.MEMORATUM_URL || "http://localhost:6767").replace(/\/+$/, "")
const KEY = process.env.MEMORATUM_API_KEY || ""

const TIMEOUT_MS = 2000
const MAX_BODY = 262144
const RECALL_LIMIT = 3
const RECALL_BUDGET = 600
const RECALL_LINE = 200
const STORE_LIMIT = 20000
const CAPTURE_TAIL = 20

function warn(what, error) {
  console.warn(`[memoratum] ${what} failed: ${error instanceof Error ? error.message : String(error)}`)
}

// Read a response body under a hard size cap, so a runaway server cannot
// make the plugin allocate without bound.
async function readBody(res) {
  if (!res.body) {
    const text = await res.text()
    if (text.length > MAX_BODY) throw new Error("response too large")
    return text
  }
  const reader = res.body.getReader()
  const chunks = []
  let size = 0
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    size += value.byteLength
    if (size > MAX_BODY) {
      await reader.cancel()
      throw new Error("response too large")
    }
    chunks.push(value)
  }
  const buffer = new Uint8Array(size)
  let offset = 0
  for (const chunk of chunks) {
    buffer.set(chunk, offset)
    offset += chunk.byteLength
  }
  return new TextDecoder().decode(buffer)
}

async function api(path, body) {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...(KEY ? { Authorization: `Bearer ${KEY}` } : {}) },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  })
  if (!res.ok) throw new Error(`${path} -> ${res.status}`)
  const text = await readBody(res)
  return text ? JSON.parse(text) : {}
}

function projectTag(projectId) {
  const override = (process.env.MEMORATUM_TAG || "").trim()
  if (override) return override
  return `opencode:${projectId || "default"}`
}

function partsToText(parts) {
  if (!Array.isArray(parts)) return ""
  return parts
    .filter((part) => part && part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join(" ")
    .trim()
}

// Accepts the released SDK shape ({info, parts}) and the V2 message shape
// ({role, content}) so both capture paths share one extractor.
function toText(messages) {
  if (!Array.isArray(messages)) return ""
  const out = []
  for (const message of messages) {
    const role = message?.info?.role || message?.role || ""
    let parts = message?.parts
    if (!parts) {
      if (typeof message?.content === "string") parts = [{ type: "text", text: message.content }]
      else if (Array.isArray(message?.content)) parts = message.content
      else parts = []
    }
    const text = partsToText(parts)
    if (text) out.push(`${role}: ${text}`)
  }
  return out.join("\n\n")
}

async function recall(tag, query) {
  if (!query || !query.trim()) return ""
  const data = await api("/v4/search", {
    q: query.slice(0, 500),
    containerTag: tag,
    searchMode: "memories",
    limit: RECALL_LIMIT,
  })
  const lines = (data.results || [])
    .map((hit) => String(hit.memory || "").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .map((text) => `- ${text.slice(0, RECALL_LINE)}`)
  let block = "[memoratum]"
  for (const line of lines) {
    if (block.length + line.length + 1 > RECALL_BUDGET) break
    block += `\n${line}`
  }
  return block === "[memoratum]" ? "" : block
}

async function store(tag, text, customId) {
  const content = text.slice(0, STORE_LIMIT)
  if (!content.trim()) return
  await api("/v3/documents", { content, containerTag: tag, customId })
}

// Per-plugin state: the pending query, the cached recall block and the id of
// the last captured message, all keyed by session.
function createMemory(tag) {
  const pending = new Map()
  const cache = new Map()
  const captured = new Map()
  return {
    tag,
    captured,
    observe(sessionID, query) {
      pending.set(sessionID, query)
      cache.delete(sessionID)
    },
    async block(sessionID) {
      if (cache.has(sessionID)) return cache.get(sessionID)
      let block = ""
      try {
        block = await recall(tag, pending.get(sessionID))
      } catch (error) {
        warn("recall", error)
      }
      cache.set(sessionID, block)
      return block
    },
  }
}

// Store the part of the transcript that has not been captured yet. The
// custom id carries the last message id, so a repeated compaction replaces
// the document instead of duplicating it.
async function capture(memory, sessionID, messages) {
  try {
    const list = Array.isArray(messages) ? messages : []
    const lastId = memory.captured.get(sessionID)
    let slice = list
    if (lastId) {
      const index = list.findIndex((message) => message?.info?.id === lastId)
      slice = index >= 0 ? list.slice(index + 1) : list.slice(-CAPTURE_TAIL)
    }
    const text = toText(slice)
    if (!text.trim()) return
    const lastMessageId = slice[slice.length - 1]?.info?.id || "end"
    await store(memory.tag, text, `oc:${sessionID}:${lastMessageId}`)
    memory.captured.set(sessionID, lastMessageId)
  } catch (error) {
    warn("capture", error)
  }
}

export default {
  id: "memoratum",

  async server(input) {
    const memory = createMemory(projectTag(input?.project?.id))
    return {
      // A new user prompt is the query for the next recall.
      "chat.message": async (hook, output) => {
        memory.observe(hook.sessionID, partsToText(output?.parts))
      },

      // Memory goes into the system layer, never into the user's prompt: the
      // prompt is persisted verbatim, so injecting there would rewrite the
      // transcript and compound on every later recall.
      "experimental.chat.system.transform": async (hook, output) => {
        const block = await memory.block(hook.sessionID)
        if (block && Array.isArray(output?.system)) output.system.push(block)
      },

      // Compaction is the capture boundary: the transcript is complete, and
      // one document per compaction keeps the dreaming calls down.
      "experimental.session.compacting": async (hook) => {
        let messages = []
        try {
          const res = await input.client.session.messages({ path: { id: hook.sessionID } })
          messages = res?.data ?? []
        } catch (error) {
          warn("transcript", error)
          return
        }
        await capture(memory, hook.sessionID, messages)
      },
    }
  },

  async setup(ctx) {
    const memory = createMemory(projectTag(ctx?.location?.project?.id))

    await ctx.session.hook("prompt", (event) => {
      memory.observe(event.sessionID, String(event.prompt?.text || ""))
    })

    await ctx.session.hook("context", async (event) => {
      const block = await memory.block(event.sessionID)
      if (block && Array.isArray(event.system)) event.system.push({ type: "text", text: block })
    })

    await ctx.session.hook("compaction", async (event) => {
      await capture(memory, event.sessionID, event.messages)
    })
  },
}
