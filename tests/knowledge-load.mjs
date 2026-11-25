import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { createWriteStream } from "node:fs";
import { once } from "node:events";
import { join } from "node:path";
import { fixture, client } from "./fixture.mjs";
const f = await fixture(undefined, { QUIZ_SEMANTIC: "off" });
try {
  const members = 100000,
    teams = 30,
    host = client(f);
  const ok = async (p) => {
    const r = await p;
    assert.ok(r.status < 300, JSON.stringify(r.data));
    return r.data;
  };
  await ok(host("/login", "POST", { password: f.password }));
  const room = await ok(host("/rooms", "POST", { name: "Load trial" })),
    root = "/rooms/" + room.code;
  const folder = f.file + ".knowledge/feeds";
  await mkdir(folder, { recursive: true });
  const writer = createWriteStream(join(folder, "members.jsonl"));
  for (let i = 0; i < members; i++)
    if (
      !writer.write(
        JSON.stringify({
          id: "register:" + i,
          name: "Entry " + i,
          types: ["test register"],
          aliases: ["Item " + i],
        }) + "\n",
      )
    )
      await once(writer, "drain");
  writer.end();
  await once(writer, "close");
  await writeFile(
    join(folder, "register.manifest.json"),
    JSON.stringify({
      source: "register",
      revision: "one",
      terms: { "test register": ["registered entry"] },
      census: { "test register": members },
      url: "https://example.org/register",
      file: "members.jsonl",
    }),
  );
  async function preview() {
    const job = await ok(
      host(root + "/generate", "POST", { prompt: "Name a registered entry" }),
    );
    for (let i = 0; i < 1200; i++) {
      const r = await ok(host(root + "/generate/" + job.id));
      if (r.status === "ready") return { q: r.preview, id: job.id };
      assert.notEqual(r.status, "failed", r.message);
      await new Promise((done) => setTimeout(done, 100));
    }
    throw Error("Preview timed out");
  }
  const start = performance.now();
  const first = await preview();
  const cold = performance.now() - start;
  assert.equal(first.q.answerCount, members);
  const warmStart = performance.now();
  const cached = await preview();
  const warm = performance.now() - warmStart;
  assert.equal(cached.q.id, first.q.id);
  const saved = await ok(
    host(root + "/generate/" + first.id + "/save", "POST", {}),
  );
  const clients = Array.from({ length: teams }, () => client(f));
  await Promise.all(
    clients.map((c, i) => ok(c(root + "/join", "POST", { name: "Crew " + i }))),
  );
  const { id } = await ok(
    host(root + "/start", "POST", {
      questionId: saved.id,
      previous: null,
      seconds: 0,
    }),
  );
  const times = await Promise.all(
    clients.map(async (c, i) => {
      const start = performance.now();
      const r = await ok(
        c(root + "/rounds/" + id + "/answer", "POST", {
          answer: "Item " + (99999 - i),
          revision: 0,
        }),
      );
      assert.equal(r.canonical, "Entry " + (99999 - i));
      return performance.now() - start;
    }),
  );
  const page = await ok(host(root + "/rounds/" + id + "/answers?q=99999"));
  assert.equal(page.answers[0].name, "Entry 99999");
  const p95 = times.sort((a, b) => a - b)[Math.ceil(0.95 * teams) - 1];
  console.log(
    JSON.stringify({
      members,
      teams,
      coldSeconds: +(cold / 1000).toFixed(2),
      warmPreviewMs: Math.round(warm),
      submissionP95Ms: Math.round(p95),
      slowestMs: Math.round(times.at(-1)),
    }),
  );
  assert.ok(warm < 2000, "Warm preview exceeded two seconds");
  assert.ok(p95 < 250, "p95 exceeded 250 ms");
} finally {
  await f.close();
}
