import test from "node:test";
import assert from "node:assert/strict";
import { mkdir, unlink, writeFile } from "node:fs/promises";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import { fixture, client } from "./fixture.mjs";
import { questionDrafts } from "../server/question-drafts.ts";
import { additionalAnswer, indexedPage, lookup, restoreRoundNames } from "../server/knowledge.ts";
import { readFileSync } from "node:fs";

test("prefix and typo confirmations preserve the score of a qualified spelling", () => {
  const db = new DatabaseSync(":memory:");
  try {
    db.exec(readFileSync(new URL("../server/knowledge.sql", import.meta.url), "utf8"));
    db.prepare("INSERT INTO kb_bank VALUES('works','Name a work','{}')").run();
    for (const [id,name,aliases,depth] of [
      ["rare","Signal",["1989 Signal"],4], ["common","The Signal Album",["Signal"],1],
    ]) {
      db.prepare("INSERT INTO kb_member VALUES('works',?,?,?,?, '[]')").run(id,name,depth,JSON.stringify(aliases));
      for (const form of [name,...aliases]) {
        db.prepare("INSERT INTO kb_name VALUES('works',?,?)").run(form.toLowerCase(),id);
        if (form !== "Signal")
          db.prepare("INSERT INTO kb_answer_key VALUES('works',0,?,?,?,?)").run(form.toLowerCase(),id,name,depth);
      }
    }
    db.prepare("INSERT INTO kb_answer_key VALUES('works',0,'signal','','Signal',1)").run();
    for (const text of ["1989 Sig","1989 Signql"]) {
      const suggestion = lookup(db,"works",text);
      assert.equal(suggestion?.exact,false);
      assert.equal(suggestion.answer.name,"1989 Signal");
      const accepted = lookup(db,"works",suggestion.answer.name);
      assert.equal(accepted.entity,"rare");
      assert.equal(accepted.answer.depth,4);
    }
    assert.equal(lookup(db,"works","Signal").answer.depth,1);
    assert.equal(lookup(db,"works","1988 Signal"),undefined);
  } finally { db.close(); }
});

test("a later source change preserves accepted alternate names, their proof and their round score", () => {
  const db = new DatabaseSync(":memory:");
  try {
    db.exec("PRAGMA foreign_keys=OFF");
    db.exec(readFileSync(new URL("../server/knowledge.sql", import.meta.url), "utf8"));
    db.exec(`CREATE TABLE library(room, id, content);
      CREATE TABLE round_additions(round,key,name,depth,PRIMARY KEY(round,key));`);
    const question = { knowledge:"original", openRule:"same rule", nameIndex:2, counts:[0,0,0,1,0,0] };
    function member(bank, id, name, alias, depth) {
      db.prepare("INSERT OR IGNORE INTO kb_bank VALUES(?,?,'{}')").run(bank,"Test question");
      db.prepare("INSERT INTO kb_member VALUES(?,?,?,?,?,?)").run(bank,id,name,depth,
        JSON.stringify([alias]),JSON.stringify([{source:bank,url:"https://example.org/" + bank}]));
      db.prepare("INSERT INTO kb_text(bank,entity,terms) VALUES(?,?,?)").run(bank,id,name+" "+alias);
      for (const term of [name,alias]) {
        db.prepare("INSERT INTO kb_name VALUES(?,?,?)").run(bank,term.toLowerCase(),id);
        db.prepare("INSERT INTO kb_answer_key VALUES(?,0,?,?,?,?)").run(bank,term.toLowerCase(),id,name,depth);
      }
    }
    member("original","A","Existing exhibit","Original spelling",3);
    member("edition1","A","Existing exhibit","Local exhibit name",5);
    member("edition1","B","New exhibit","New regional name",4);
    db.prepare("INSERT INTO library VALUES('room','entry',?)").run(JSON.stringify({knowledge:"edition1",openRule:"same rule"}));
    const accept = raw => additionalAnswer(db,1,"room","entry",question,raw);
    assert.equal(accept("Local exhibit name").answer.depth,3);
    assert.equal(accept("New regional name").answer.depth,2);
    db.exec(`CREATE TABLE rounds(id,phase,room,question);
      INSERT INTO rounds VALUES(1,'open','room','entry');
      CREATE TABLE submissions(round,team,key,raw,canonical,depth);
      INSERT INTO submissions VALUES(1,1,'kb:B','New regional name','New exhibit',2);
      DELETE FROM kb_round_name;`);
    restoreRoundNames(db);
    member("edition2","C","Different exhibit","New regional name",1);
    db.prepare("UPDATE library SET content=?").run(JSON.stringify({knowledge:"edition2",openRule:"same rule"}));
    const replay = accept("NEW REGIONAL NAME");
    assert.deepEqual([replay.entity,replay.answer.name,replay.answer.depth],["B","New exhibit",2]);
    for (const [term, name, depth] of [["regional","New exhibit",2],["Local exhibit name","Existing exhibit",3]]) {
      const page = indexedPage(db,question,new URLSearchParams({q:term}),1);
      assert.deepEqual(page.answers.map(a=>[a.name,a.depth]),[[name,depth]]);
      assert(page.answers[0].evidence.some(fact=>fact.source==="edition1"));
    }
    assert.equal(indexedPage(db,question,new URLSearchParams({q:"Different exhibit"}),1).filtered,0);
  } finally { db.close(); }
});

test("host drafts reuse one structured request and reject incomplete responses", async (t) => {
  const saved = process.env.OPENAI_API_KEY;
  process.env.OPENAI_API_KEY = "mock-project-key";
  t.after(() => {
    if (saved === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = saved;
  });
  const questions = ["Name a film directed by Akira Kurosawa"];
  let status = "incomplete";
  const upstream = t.mock.method(globalThis, "fetch", async (url, init) => {
    assert.equal(url, "https://api.openai.com/v1/responses");
    assert.equal(init.headers.Authorization, "Bearer mock-project-key");
    const body = JSON.parse(init.body);
    assert.equal(body.store, false);
    assert.equal(body.service_tier, "default");
    assert.equal(body.input, "Japanese cinema");
    assert.equal(body.text.format.strict, true);
    assert.equal(body.tools, undefined);
    return Response.json({ status, output: [{ type: "message", content: [
      { type: "output_text", text: JSON.stringify({ questions }) },
    ] }] });
  });
  await assert.rejects(questionDrafts("draft-test", "Japanese cinema"), /No usable wording/);
  status = "completed";
  const drafts = await Promise.all([questionDrafts("draft-test", "Japanese cinema"),
    questionDrafts("draft-test", "Japanese cinema")]);
  assert.deepEqual(drafts, [questions, questions]);
  assert.deepEqual(await questionDrafts("draft-test", "Japanese cinema"), questions);
  assert.equal(upstream.mock.callCount(), 2);
  delete process.env.OPENAI_API_KEY;
  await assert.rejects(questionDrafts("draft-test", "Japanese cinema"), /OPENAI_API_KEY/);
});

const ok = async (promise) => {
  const r = await promise;
  assert.ok(r.status >= 200 && r.status < 300, JSON.stringify(r.data));
  return r.data;
};

function publishFixture(f) {
  const command = [
    "import sys", "from pathlib import Path", "sys.path.insert(0, 'tests')",
    "from test_catalogue import world_fixture, catalogue", "root = Path(sys.argv[1])",
    "world = root / 'publisher-input.sqlite'", "if not world.exists(): world_fixture(world)",
    "catalogue.bootstrap = lambda db: None",
    "catalogue.build(world, root / 'knowledge-catalogue.sqlite', root, offline=True)",
  ].join("\n");
  const result = spawnSync(process.env.KNOWLEDGE_PYTHON || (process.platform === "win32" ? "python" : "python3"),
    ["-c", command, f.dir], { windowsHide: true, encoding: "utf8" });
  assert.equal(result.status, 0, result.stderr);
}

test("source decisions distinguish symbols, scripts, typos and partially qualifying aliases during play", async t => {
  const f = await fixture(undefined, { QUIZ_SEMANTIC: "off", OPENAI_API_KEY: "" });
  t.after(() => f.close());
  const names = ["C", "C#", "C++", "का", "कि", "!!!", "Églantine"];
  const titles = ["Open C# Reference", "Open C++ Guide", "Annual 5.25 Record", "मेरी किताब",
    "Bright Stars", "Bright Starfish", "Matrix", "Piano", "Tango", "Tanog Edition", "Shrek", "Shera", "Scaer", "A*Star", "Oslo", "Kiwi"];
  const records = [...names, ...titles].map((name, i) => ({ id: String(i), name, types: ["notation"],
    aliases: name === "C" ? ["Regional notation", "Frame", "Scale"] : name === "C#" ? ["Frame"] : [],
    edges: [["used in", "Open Studio"]] }));
  records.push({ id: "excluded", name: "Another notation", types: ["notation"], aliases: ["Regional notation", "Scale"], edges: [] });
  await writeFile(join(f.dir, "notations.json"), JSON.stringify(records));
  await writeFile(join(f.dir, "notations.manifest.json"), JSON.stringify({ source: "notations",
    url: "https://example.org/register", revision: "1", file: "notations.json", terms: { notation: [] } }));
  publishFixture(f);
  const host = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const root = "/rooms/" + (await ok(host("/rooms", "POST", { name: "Names and symbols" }))).code;
  const players = names.map(() => client(f));
  for (const [i, player] of players.entries()) await ok(player(root + "/join", "POST", { name: "Crew " + i }));
  const sharedPlayer = client(f);
  await ok(sharedPlayer(root + "/join", "POST", { name: "Shared title" }));
  const { jobs: [job] } = await ok(host(root + "/drafts", "POST", { idea: "Open Studio" }));
  assert.equal(job.records, names.length + titles.length);
  let preview;
  for (let i = 0; i < 100; i++) {
    const result = await ok(host(root + "/generate/" + job.id));
    assert.notEqual(result.status, "failed", result.message);
    if (result.preview) { preview = result.preview; break; }
    await new Promise(resolve => setTimeout(resolve, 70));
  }
  assert.equal(preview?.answerCount, names.length + titles.length);
  for (const name of ["C#", "C++", "का", "कि", "!!!"]) {
    const page = await ok(host(root + "/generate/" + job.id + "/answers?q=" + encodeURIComponent(name)));
    assert.deepEqual(page.answers.map(row => row.name), [name]);
  }
  const saved = await ok(host(root + "/generate/" + job.id + "/save", "POST", {}));
  const { id } = await ok(host(root + "/start", "POST", { questionId: saved.id, previous: null, seconds: 0 }));
  const path = root + "/rounds/" + id;
  const submit = (p, answer) => p(path + "/answer", "POST", { answer, revision: 0 });
  for (const [input, expected] of [
    ["Open C# Ref", titles[0]], ["Open C# Refernce", titles[0]], ["Open C++ Gui", titles[1]],
    ["Annual 5.25 Rec", titles[2]], ["मेरी कि", titles[3]],
    ["Open C++ Reference", undefined], ["Open C# Guide", undefined], ["Annual 5 25 Rec", undefined],
    ["मिरी कि", undefined], ["Regional notatio", undefined], ["Reginal notation", undefined], ["Bright Star", undefined],
    ["Martix", "Matrix"], ["Pieno", "Piano"], ["Tanog", undefined], ["Sherk", undefined],
    ["Farme", "Frame"], ["Scael", undefined], ["A*Stir", "A*Star"], ["Osol", "Oslo"], ["Kawi", "Kiwi"],
  ]) {
    const reply = await submit(players[0], input);
    assert.equal(reply.status, 422, input);
    assert.equal(reply.data.suggestion, expected, input);
  }
  const mixed = await submit(players[0], "Regional notation");
  assert.equal(mixed.status, 422);
  assert.match(mixed.data.error, /specific/);
  assert.equal((await submit(players[0], "C+")).status, 422);
  const shared = await ok(host(root + "/generate/" + job.id + "/answers?q=Regional"));
  assert(shared.answers.some(row=>row.needsDetail?.includes("Regional notation")));
  assert(shared.answers.every(row=>!row.aliases.includes("Regional notation")));
  const typo = await submit(players[6], "Xglantine");
  assert.equal(typo.status, 422);
  assert.equal(typo.data.suggestion, "Églantine");
  const sharedTypo = await submit(sharedPlayer, "Farme");
  const sharedReceipt = await ok(submit(sharedPlayer, sharedTypo.data.suggestion));
  assert.equal(sharedReceipt.canonical ?? sharedReceipt.raw, "Frame");
  for (const [i, p] of players.entries()) {
    const receipt = await ok(submit(p, i === 6 ? "Eglantine" : names[i]));
    assert.equal(receipt.canonical ?? receipt.raw, names[i]);
  }
  await ok(host(path + "/reveal", "POST", {}));
  const revealed = await ok(host(path + "?view=host"));
  assert.deepEqual(new Set(revealed.results.map(row => row.canonical)), new Set([...names, "Frame"]));
  assert.ok(revealed.results.find(row => row.canonical === "Frame").points <= 15);
  assert.ok(revealed.results.every(row => row.points > 0));
});

test("a new target's preview survives a restart and completes when the builder publishes its facts", { timeout: 45000 }, async t => {
  const f = await fixture(undefined, { QUIZ_SEMANTIC: "off", OPENAI_API_KEY: "" });
  t.after(() => f.close());
  publishFixture(f);
  const host = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const { code } = await ok(host("/rooms", "POST", { name: "New publisher" }));
  const root = "/rooms/" + code;
  const prompt = "Name a work published by Cedar Studio";
  const pending = await host(root + "/drafts", "POST", {idea:prompt, waitForSource:true, prepareOnSelect:true});
  assert.equal(pending.status,202);
  assert.equal((await ok(host(root + "/drafts/edition"))).edition,pending.data.awaitingEdition);
  const job = await ok(host(root + "/generate","POST",{prompt}));
  async function waitFor(status) {
    for (let i=0;i<150;i++) {
      const result=await ok(host(root+"/generate/"+job.id));
      assert.notEqual(result.status,"failed",result.message);
      if (result.status===status) return result;
      await new Promise(resolve=>setTimeout(resolve,120));
    }
    assert.fail("Preview did not reach "+status);
  }
  await waitFor("waiting_source");
  assert.equal((await ok(host(root+"/generate","POST",{prompt}))).id,job.id);
  await f.stop();
  const input = new DatabaseSync(join(f.dir, "publisher-input.sqlite"));
  try {
    input.exec("INSERT INTO entity VALUES(999,'Cedar Studio'); INSERT INTO triple VALUES(1,123,999)");
  } finally { input.close(); }
  publishFixture(f);
  await f.start();
  assert.notEqual((await ok(host(root+"/drafts/edition"))).edition,pending.data.awaitingEdition);
  const result=await waitFor("ready");
  assert.equal(result.preview.answerCount,1);
  const choices=await ok(host(root+"/drafts","POST",{idea:prompt,waitForSource:true,prepareOnSelect:true}));
  assert.equal(choices.jobs[0].records,1);
});

test("independent catalogue refreshes a saved register while an open round keeps its edition", { timeout: 45000 }, async t => {
  const f = await fixture(undefined, { QUIZ_SEMANTIC: "off", OPENAI_API_KEY: "" });
  t.after(() => f.close());
  const publish = async (names) => {
    await writeFile(join(f.dir, "registry.json"), JSON.stringify(names.map((name, i) =>
      ({ id: "item-" + i, name, aliases: ["Alias " + i], types: ["exhibit"] }))));
    await writeFile(join(f.dir, "registry.manifest.json"), JSON.stringify({ source: "museum",
      url: "https://example.org/museum", revision: "1", file: "registry.json",
      terms: { exhibit: ["museum"] }, census: { exhibit: names.length } }));
    publishFixture(f);
  };
  await publish(["First exhibit", "Second exhibit"]);
  const host = client(f), team = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const { code } = await ok(host("/rooms", "POST", { name: "Museum catalogue" }));
  const root = "/rooms/" + code;
  await ok(team(root + "/join", "POST", { name: "Visitors" }));
  const response = await ok(host(root + "/drafts", "POST", { idea: "museum", completeOnly: true, prepareOnSelect: true }));
  const [job] = response.jobs;
  assert.equal(job.records, 2);
  assert.equal(job.complete, true);
  assert.equal(job.deferred, true);
  await f.stop();
  await f.start();
  const suggested = await ok(host(root + "/generate/" + job.id));
  assert.equal(suggested.status, "suggested");
  assert.equal(suggested.bank, null);
  const prepare = root + "/generate/" + job.id + "/prepare";
  assert.equal((await team(prepare, "POST", {})).status, 401);
  const retries = await Promise.all([ok(host(prepare, "POST", {})), ok(host(prepare, "POST", {}))]);
  assert.ok(retries.every(result => result.id === job.id));
  async function until(check) {
    for (let i = 0; i < 180; i++) {
      const value = await check();
      if (value) return value;
      await new Promise(resolve => setTimeout(resolve, 150));
    }
    assert.fail("The game did not publish the expected edition.");
  }
  await until(async () => {
    const result = await ok(host(root + "/generate/" + job.id));
    assert.notEqual(result.status, "failed", result.message);
    return result.status === "ready";
  });
  const saved = await ok(host(root + "/generate/" + job.id + "/save", "POST", {}));
  const round = await ok(host(root + "/start", "POST", { questionId: saved.id, previous: null, seconds: 0 }));
  await publish(["First exhibit", "Second exhibit", "New exhibit"]);
  await until(async () => (await ok(host(root + "/library/" + saved.id))).answerCount === 3);
  const path = root + "/rounds/" + round.id;
  assert.equal((await ok(host(path + "?view=host"))).answerCount, 2);
  assert.equal((await team(path + "/answer", "POST", { answer: "New exhibit", revision: 0 })).status, 422);
  const accepted = await ok(team(path + "/answer", "POST", { answer: "Alias 0", revision: 0 }));
  assert.equal(accepted.canonical, "First exhibit");
  const surprise = await ok(host(root + "/drafts", "POST", { surprise: true, completeOnly: true }));
  assert.ok(surprise.jobs.length && surprise.jobs.every(row => row.complete && row.records === 3));
  assert.equal((await host(root + "/generate/" + job.id + "/save", "POST", {})).status, 409);
  await ok(host(path + "/reveal", "POST", {}));
  await unlink(join(f.dir, "registry.manifest.json"));
  publishFixture(f);
  const next = () => host(root + "/start", "POST", { questionId: saved.id, previous: round.id, seconds: 0 });
  assert.equal((await next()).status, 409);
  const listed = async () => (await ok(host(root + "/library"))).find(q => q.id === saved.id);
  await until(async () => (await listed())?.sourceNotice?.includes("no longer"));
  await f.stop();
  await f.start();
  assert.equal((await next()).status, 409);
  assert.equal((await ok(host(path + "?view=host"))).results[0].canonical, "First exhibit");
  await publish(["First exhibit", "Second exhibit", "New exhibit", "Restored exhibit"]);
  await until(async () => !(await listed())?.sourceNotice);
  const restored = await ok(next());
  assert.equal((await ok(host(root + "/rounds/" + restored.id + "?view=host"))).answerCount, 4);
});
test(
  "source updates, natural comparisons, automatic rulings and immutable editions",
  { timeout: 160000 },
  async (t) => {
    const f = await fixture(undefined, { QUIZ_SEMANTIC: "off", OPENAI_API_KEY: "" });
    t.after(() => f.close());
    const host = client(f),
      a = client(f),
      b = client(f);
    await ok(host("/login", "POST", { password: f.password }));
    const { code } = await ok(
        host("/rooms", "POST", { name: "Knowledge test" }),
      ),
      root = "/rooms/" + code;
    assert.equal((await a(root + "/drafts", "POST", { idea: "Japanese cinema" })).status, 401);
    const sourced = await ok(host(root + "/drafts", "POST", { idea: "Name a colour" }));
    assert.equal(sourced.jobs[0].prompt, "Name a colour");
    const prompt = "Name a cheese";
    async function compile(words) {
      const job = await ok(host(root + "/generate", "POST", { prompt: words }));
      for (let i = 0; i < 400; i++) {
        const r = await ok(host(root + "/generate/" + job.id));
        if (r.status === "ready") return { job: job.id, ...r.preview };
        assert.notEqual(r.status, "failed", r.message);
        await new Promise((done) => setTimeout(done, 150));
      }
      assert.fail("Research did not finish");
    }
    const browse = (id, search) =>
      ok(
        host(
          root + "/generate/" + id + "/answers?q=" + encodeURIComponent(search),
        ),
      );
    const save = (q) =>
      ok(host(root + "/generate/" + q.job + "/save", "POST", {}));
    const fish = await compile("Name a fish that is bigger than a pear");
    assert.ok(fish.answerCount > 3000);
    assert.ok(fish.gaps.missing > 0 && fish.gaps.borderline > 0);
    assert.ok(fish.counts[5] > 0 && fish.counts[5] <= fish.answerCount / 100);
    for (const name of ["Coelacanth", "Oarfish", "Arapaima"])
      assert.ok((await browse(fish.job, name)).filtered > 0);
    const finals = await compile(
      "Name a team that reached the Women's World Cup final between 1991 and 2003",
    );
    assert.match(finals.coverage, /4 completed finals/);
    assert.equal((await browse(finals.job, "Spain")).filtered, 0);
    const first = await compile(prompt),
      firstId = (await save(first)).id;
    assert.ok(first.answerCount > 1100);
    await ok(a(root + "/join", "POST", { name: "One" }));
    await ok(b(root + "/join", "POST", { name: "Two" }));
    const round = await ok(
      host(root + "/start", "POST", {
        questionId: firstId,
        previous: null,
        seconds: 0,
      }),
    );
    const path = root + "/rounds/" + round.id;
    assert.equal((await a(root + "/generate/" + first.job)).status, 401);
    const feedFolder = f.file + ".knowledge/feeds";
    await mkdir(feedFolder, { recursive: true });
    const manifest = {
      source: "new-dairy",
      url: "https://example.org/register",
      revision: "one",
      terms: { cheese: [] },
      file: "records.jsonl",
    };
    await writeFile(
      join(feedFolder, "records.jsonl"),
      JSON.stringify({
        id: "dairy:42",
        name: "Tomorrow Wheel",
        aliases: ["Roue de demain"],
        types: ["cheese"],
      }) + "\n",
    );
    await writeFile(
      join(feedFolder, "dairy.manifest.json"),
      JSON.stringify(manifest),
    );
    const revised = await compile(prompt);
    let published;
    for (let i = 0; i < 120; i++) {
      published = await ok(host(root + "/library/" + firstId));
      if (published.knowledge === revised.id) break;
      await new Promise((done) => setTimeout(done, 150));
    }
    assert.equal(
      published.knowledge,
      revised.id,
      "Source refresh should update a saved question without a host edit",
    );
    assert.ok(revised.answerCount > first.answerCount);
    assert.equal(
      (await browse(revised.job, "Roue de demain")).answers[0].name,
      "Tomorrow Wheel",
    );
    assert.equal((await browse(first.job, "Tomorrow Wheel")).filtered, 0);
    assert.equal((await a(path + "/answer", "POST", { answer: "Invented cheese 123", revision: 0 })).status, 422);
    assert.equal((await a(path + "/appeal", "POST", { answer: "Invented cheese 123" })).status, 409);
    for (const [team, answer] of [[a, "Tomorrow Wheel"], [b, "Roue de demain"]])
      await ok(team(path + "/answer", "POST", { answer, revision: 0 }));
    assert.equal(
      (await a(path + "/appeal", "POST", { answer: "Tomorrow Wheel" })).status,
      409,
    );
    const locked = await ok(host(path + "?view=host"));
    assert.equal(locked.answerCount, first.answerCount + 1);
    assert.equal(locked.phase, "locked");
    const proven = await ok(host(path + "/answers?q=Tomorrow"));
    assert.ok(proven.answers.some(row => row.name === "Tomorrow Wheel" && row.evidence.length > 0));
    const observations = new DatabaseSync(f.file, { readOnly: true });
    try {
      assert.equal(observations.prepare("SELECT count(*) n FROM kb_pick WHERE round=? AND choice IS NULL").get(round.id).n, 2);
    } finally { observations.close(); }
    assert.equal((await host(path + "/judge", "POST", { key: "Tomorrow Wheel", depth: 0 })).status, 409);
    await ok(host(path + "/reveal", "POST", {}));
    const revealed = await ok(host(path + "?view=host"));
    assert.deepEqual(
      revealed.results.map((r) => r.canonical),
      ["Tomorrow Wheel", "Tomorrow Wheel"],
    );
    const next = await ok(
      host(root + "/start", "POST", {
        questionId: firstId,
        previous: round.id,
        seconds: 0,
      }),
    );
    assert.equal(
      (
        await ok(
          a(root + "/rounds/" + next.id + "/answer", "POST", {
            answer: "Roue de demain",
            revision: 0,
          }),
        )
      ).canonical,
      "Tomorrow Wheel",
    );
    await writeFile(join(feedFolder, "records.jsonl"), "");
    await writeFile(
      join(feedFolder, "dairy.manifest.json"),
      JSON.stringify({ ...manifest, revision: "withdrawn" }),
    );
    const withdrawn = await compile(prompt);
    assert.equal((await browse(withdrawn.job, "Tomorrow Wheel")).filtered, 0);
    assert.equal((await browse(revised.job, "Tomorrow Wheel")).filtered, 1);
    await f.stop();
    await f.start();
    assert.equal(
      (
        await ok(
          b(root + "/rounds/" + next.id + "/answer", "POST", {
            answer: "Roue de demain",
            revision: 0,
          }),
        )
      ).canonical,
      "Tomorrow Wheel",
    );
  },
);
