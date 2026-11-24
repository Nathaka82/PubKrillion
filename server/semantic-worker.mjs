import { parentPort, workerData } from "node:worker_threads";
import { DatabaseSync } from "node:sqlite";
import { existsSync } from "node:fs";
import { setImmediate as yieldTurn } from "node:timers/promises";
import { encode, dot, relevance } from "./encoder.mjs";
import { normal } from "./rules.ts";
import {
  concept,
  definition,
  nounSenses,
  isBroaderTerm,
} from "./vocabulary.ts";

const banks = new Map();
let db;
try {
  db = new DatabaseSync(workerData.cache);
  db.exec(
    "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=1500; CREATE TABLE IF NOT EXISTS vectors(text TEXT PRIMARY KEY, vector BLOB NOT NULL) WITHOUT ROWID",
  );
} catch {
  db = undefined;
}
const bundledFile = new URL("../data/minilm/catalogue.sqlite", import.meta.url);
let bundled;
if (!workerData.build && existsSync(bundledFile)) {
  try {
    bundled = new DatabaseSync(bundledFile, { readOnly: true }).prepare(
      "SELECT vector FROM vectors WHERE text=?",
    );
  } catch {}
}
const read = db?.prepare("SELECT vector FROM vectors WHERE text=?");
const save = db?.prepare("INSERT OR IGNORE INTO vectors VALUES(?,?)");
let compute = Promise.resolve();
function vectors(texts) {
  const job = compute.then(async () => {
    const result = new Map();
    const missing = [];
    for (const text of new Set(texts)) {
      let cached;
      try {
        cached = (bundled?.get(text) ?? read?.get(text))?.vector;
      } catch {}
      if (cached?.length === 384)
        result.set(
          text,
          Float32Array.from(
            new Int8Array(cached.buffer, cached.byteOffset, cached.length),
            (x) => x / 127,
          ),
        );
      else missing.push(text);
    }
    for (let i = 0; i < missing.length; i += 12) {
      const chunk = missing.slice(i, i + 12);
      const encoded = await encode(chunk);
      const writes = [];
      for (const [j, v] of encoded.entries()) {
        const packed = Int8Array.from(v, (x) => Math.round(x * 127));
        result.set(
          chunk[j],
          Float32Array.from(packed, (x) => x / 127),
        );
        writes.push([chunk[j], new Uint8Array(packed.buffer)]);
      }
      if (save) {
        try {
          db.exec("BEGIN");
          for (const [text, bytes] of writes) save.run(text, bytes);
          db.exec("COMMIT");
        } catch (error) {
          try {
            db.exec("ROLLBACK");
          } catch {}
          if (workerData.build) throw error;
        }
      }
    }
    return result;
  });
  compute = job.catch(() => {});
  return job;
}
function documents(a) {
  const senses = a.wordnetSenses?.length
    ? a.wordnetSenses
    : nounSenses(concept(normal(a.name)));
  return {
    label: a.name,
    glosses: [...new Set(senses.slice(0, 3).map(definition).filter(Boolean))],
  };
}
async function prepare(bank, q) {
  const context = q.topic + ". " + q.prompt;
  const topic = (await vectors([context])).get(context);
  // Yield between short batches so a new guess in another room takes priority.
  for (let start = 0; start < q.answers.length; start += 8) {
    if (banks.get(bank.key) !== bank) return;
    const docs = q.answers.slice(start, start + 8).map(documents);
    const embeds = await vectors(docs.flatMap((d) => [d.label, ...d.glosses]));
    for (const { label, glosses } of docs) {
      const labelVector = embeds.get(label);
      const senses = glosses.map((g, i) => {
        const v = embeds.get(g);
        return { v, g, context: dot(topic, v) + (i === 0 ? 0.06 : 0) };
      });
      const best = Math.max(...senses.map((s) => s.context));
      const relevant = senses.filter((s) => s.context >= best - 0.1);
      const passage = [label, ...relevant.map((s) => s.g)].join(". ");
      bank.rows.push({
        label,
        labelVector,
        glossVectors: relevant.map((s) => s.v),
        passage,
      });
    }
    await yieldTurn();
  }
}
const filler = new Set(
  "a an the of to for that which who is are it in at with something someone one those some thing things person type kind what and name".split(
    " ",
  ),
);
function contentWords(text) {
  return normal(text)
    .split(" ")
    .filter((w) => !filler.has(w))
    .map((w) => (w.length > 5 ? w.replace(/(?:ing|ed|s)$/, "") : w));
}
async function rank(bank, raw) {
  const key = normal(raw),
    words = key.split(" ");
  if (
    contentWords(raw).every((w) => bank.promptWords.has(w)) ||
    /\b(?:not|isnt|isn t|except|without)\b/.test(key)
  )
    return {};
  const query = (await encode([raw]))[0];
  const shortlist = [];
  for (const row of bank.rows) {
    const label = dot(query, row.labelVector);
    const score = Math.max(
      label,
      ...row.glossVectors.map((v) => 0.7 * dot(query, v) + 0.3 * label),
    );
    if (shortlist.length === 5 && score <= shortlist[4].score) continue;
    shortlist.push({ row, score });
    shortlist.sort((a, b) => b.score - a.score);
    if (shortlist.length > 5) shortlist.pop();
  }
  if (!shortlist.length) return {};
  const { row: winner, score: first } = shortlist[0];
  const second = shortlist[1]?.score ?? -1;
  const name = normal(winner.label);
  const numerical = key.match(/\d+/g);
  if (numerical && numerical.join(" ") !== (name.match(/\d+/g) ?? []).join(" "))
    return {};
  // Related labels may be different people, species or instruments. A known
  // dictionary term needs identity evidence; the lexical matcher checks its
  // synonyms and ancestors before reaching this stage.
  const source = nounSenses(concept(key)),
    target = nounSenses(concept(name));
  if (
    (source.length &&
      target.length &&
      !source.some((s) => target.includes(s))) ||
    isBroaderTerm(key, name)
  )
    return {};
  // Cosine distance retrieves candidates. The second model reads guess and
  // candidate together to check borderline paraphrases against the definition.
  if (first < (words.length === 1 ? 0.86 : 0.49)) return {};
  if (first - second < 0.065) return first >= 0.62 ? { ambiguous: true } : {};
  if (first >= 0.73) return { name: winner.label };
  const scores = await relevance(
    "What is the name of " + raw + "?",
    shortlist.map((item) => item.row.passage),
  );
  const runner = Math.max(-Infinity, ...scores.slice(1));
  if (scores[0] < runner) return {};
  if (first >= 0.65 && scores[0] >= -3 && scores[0] - runner >= 1)
    return { name: winner.label };
  if (
    shortlist.length > 1 &&
    first - second >= 0.18 &&
    scores[0] >= -3 &&
    scores[0] - runner >= 3
  )
    return { name: winner.label };
  const evidence = new Set(contentWords(winner.passage));
  const shared = new Set(contentWords(raw).filter((w) => evidence.has(w)));
  if (
    shortlist.length > 1 &&
    shared.size >= 2 &&
    scores[0] >= -5.5 &&
    scores[0] - runner >= 2.5 &&
    first - second >= 0.1
  )
    return { name: winner.label };
  return {};
}
parentPort.on("message", async (m) => {
  if (m.type === "drop") {
    banks.delete(m.key);
    return;
  }
  if (m.type === "prepare") {
    const bank = {
      key: m.key,
      rows: [],
      promptWords: new Set(contentWords(m.q.topic + " " + m.q.prompt)),
    };
    banks.set(m.key, bank);
    bank.ready = prepare(bank, m.q).catch((error) => {
      parentPort.postMessage({ failed: String(error) });
      throw error;
    });
    bank.ready.then(
      () => parentPort.postMessage({ prepared: m.key }),
      () => {},
    );
    return;
  }
  let result = {};
  const bank = banks.get(m.key);
  try {
    if (bank) {
      await bank.ready;
      if (Date.now() < m.until && banks.get(m.key) === bank) {
        // Serialise query inference with index batches on the same WASM session.
        const job = compute.then(() =>
          Date.now() < m.until ? rank(bank, m.raw) : { busy: true },
        );
        compute = job.catch(() => {});
        result = await job;
      } else result = { busy: true };
    }
  } catch (error) {
    parentPort.postMessage({ failed: String(error) });
  }
  parentPort.postMessage({ id: m.id, result });
});
