import type { GraphFact } from "./types";

export interface NoteRelation {
  predicate: string;
  target: string;
  factId: string;
  live: boolean;
}

export interface Note {
  subject: string;
  out: NoteRelation[];
  backlinks: NoteRelation[];
  history: NoteRelation[];
  files: string[];
  communities: string[];
}

function fileOf(fact: GraphFact): string {
  const meta = fact.metadata as Record<string, unknown>;
  const loc = typeof meta.location === "string" ? `:${meta.location}` : "";
  const m = /@ (.+)$/.exec(fact.subject);
  return `${m ? m[1] : fact.subject}${loc}`;
}

export function buildNotes(facts: GraphFact[]): Map<string, Note> {
  const notes = new Map<string, Note>();
  const get = (subject: string): Note => {
    let n = notes.get(subject);
    if (!n) {
      n = { subject, out: [], backlinks: [], history: [], files: [], communities: [] };
      notes.set(subject, n);
    }
    return n;
  };
  for (const f of facts) {
    const rel: NoteRelation = {
      predicate: f.predicate,
      target: f.object,
      factId: f.id,
      live: f.valid_to == null,
    };
    const from = get(f.subject);
    (f.valid_to == null ? from.out : from.history).push(rel);
    const to = get(f.object);
    (f.valid_to == null ? to.backlinks : to.history).push({
      predicate: f.predicate,
      target: f.subject,
      factId: f.id,
      live: f.valid_to == null,
    });
    const meta = f.metadata as Record<string, unknown>;
    if (typeof meta.community === "string") {
      if (!from.communities.includes(meta.community)) from.communities.push(meta.community);
    }
    const file = fileOf(f);
    if (!from.files.includes(file)) from.files.push(file);
  }
  return notes;
}

/** Split note body text into text/[[wikilink]] segments for rendering. */
export function linkSegments(text: string): Array<{ kind: "text" | "link"; value: string }> {
  const out: Array<{ kind: "text" | "link"; value: string }> = [];
  const re = /\[\[([^\]]+)\]\]/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) out.push({ kind: "text", value: text.slice(last, m.index) });
    out.push({ kind: "link", value: m[1] });
    last = m.index + m[0].length;
  }
  if (last < text.length) out.push({ kind: "text", value: text.slice(last) });
  return out;
}
