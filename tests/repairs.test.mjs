import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { catalogueQuestions } from "../server/catalogue.ts";
import { validateQuestion, matchAnswer } from "../server/rules.ts";
import { Game } from "../server/store.ts";
import { fixture, client } from "./fixture.mjs";

test("natural names, full aliases and clear typos resolve within their question", () => {
  function bank(pack, id) {
    const q = validateQuestion(
      catalogueQuestions(pack).find((q) => q.id === id),
      id,
    );
    return (guess) => matchAnswer(guess, q.answers, q.matching);
  }
  const pm = bank("general", "general-prime-ministers");
  for (const suffix of ["Younger", "Elder"]) {
    assert.equal(
      pm("Pitt the " + suffix)?.answer.name,
      "William Pitt the " + suffix,
    );
    assert.equal(pm(suffix)?.answer.name, "William Pitt the " + suffix);
    assert.equal(pm(suffix)?.exact, false);
  }
  assert.equal(pm("Pitt"), undefined);
  const bond = bank("music", "music-bond-music");
  assert.equal(bond("Kingston Calypso")?.exact, true);
  assert.equal(bond("Three Blind Mice")?.answer.name, "Kingston Calypso");
  assert.equal(bond("Kingstno Calypso")?.exact, false);
  assert.equal(
    bond("From Russia with Love")?.answer.name.includes(
      "From Russia with Love",
    ),
    true,
  );
  const throws = bank("ultimate", "ultimate-throws");
  assert.equal(throws("High releaze flick")?.answer.name, "Forehand");
  assert.equal(throws("High releaze flick")?.exact, false);
  const aliases = [
    { name: "Threebanded Sheephead", aliases: ["sea donkey"], depth: 3 },
    { name: "The Northern Lights", aliases: [], depth: 2 },
    { name: "Apollo 11", aliases: [], depth: 1 },
  ];
  for (const [guess, name] of [
    ["sea monkey", "Threebanded Sheephead"],
    ["Northern Ligths", "The Northern Lights"],
    ["northernlights", "The Northern Lights"],
  ]) {
    const result = matchAnswer(guess, aliases, "names");
    assert.equal(result?.answer.name, name, guess);
    assert.equal(result.exact, false);
  }
  for (const guess of ["donkey", "Lights", "Apollo"])
    assert.equal(matchAnswer(guess, aliases, "names")?.exact, false);
  for (const guess of ["Apollo 12"])
    assert.equal(matchAnswer(guess, aliases, "names"), undefined, guess);
  const tied = ["Catfish", "Batfish"].map((name) => ({
    name,
    aliases: [],
    depth: 1,
  }));
  assert.equal(matchAnswer("Ratfish", tied, "names"), undefined);
});

test("lost join and transfer responses can be reclaimed by the original phone", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f);
  await host("/login", "POST", { password: f.password });
  const { code } = (await host("/rooms", "POST", { name: "Receipt check" }))
    .data;
  const path = "/rooms/" + code;
  const key = () => randomBytes(32).toString("hex");
  const create = { name: "Jellyfish", retryKey: key() };
  assert.equal((await client(f)(path + "/join", "POST", create)).status, 200);
  assert.equal(
    (await client(f)(path + "/join", "POST", { ...create, retryKey: key() }))
      .status,
    409,
  );
  await f.stop();
  await f.start();
  const first = client(f);
  assert.equal((await first(path + "/join", "POST", create)).status, 200);
  const team = (await first(path + "/state")).data.team;
  const transfer = (await host(path + "/transfer", "POST", { teamId: team.id }))
    .data.transfer;
  const recover = { transfer, retryKey: key() };
  assert.equal((await client(f)(path + "/join", "POST", recover)).status, 200);
  assert.equal(
    (await client(f)(path + "/join", "POST", { transfer, retryKey: key() }))
      .status,
    400,
  );
  const recovered = client(f);
  assert.equal((await recovered(path + "/join", "POST", recover)).status, 200);
  assert.deepEqual((await recovered(path + "/state")).data.team, team);
  assert.equal((await first(path + "/state")).status, 401);
  assert.equal((await client(f)(path + "/join", "POST", create)).status, 409);
  const second = (await host(path + "/transfer", "POST", { teamId: team.id }))
    .data.transfer;
  assert.equal(
    (
      await client(f)(path + "/join", "POST", {
        transfer: second,
        retryKey: key(),
      })
    ).status,
    200,
  );
  assert.equal((await client(f)(path + "/join", "POST", recover)).status, 400);
});

test("missed answers can be corrected per eligible team and audited after reveal", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    early = client(f),
    late = client(f);
  await host("/login", "POST", { password: f.password });
  const path =
    "/rooms/" +
    (await host("/rooms", "POST", { name: "Missed dive" })).data.code;
  await early(path + "/join", "POST", { name: "Jellies" });
  const { id: teamId } = (await early(path + "/state")).data.team;
  const round =
    path +
    "/rounds/" +
    (
      await host(path + "/start", "POST", {
        previous: null,
        seconds: 0,
        questionId: "tube-lines",
      })
    ).data.id;
  assert.equal(
    (await host(round + "/judge", "POST", { teamId, name: "Bakerloo" })).status,
    409,
  );
  await host(round + "/lock", "POST", {});
  await late(path + "/join", "POST", { name: "Penguins" });
  const lateId = (await late(path + "/state")).data.team.id;
  assert.equal(
    (await host(round + "/judge", "POST", { teamId: lateId, name: "Bakerloo" }))
      .status,
    404,
  );
  assert.equal(
    (await early(round + "/judge", "POST", { teamId, name: "Bakerloo" }))
      .status,
    401,
  );
  await host(round + "/reveal", "POST", {});
  assert.equal(
    (
      await host(round + "/judge", "POST", {
        teamId,
        name: "Bakerloo",
        previous: 0,
      })
    ).status,
    200,
  );
  const state = (await host(path + "/state?view=host")).data;
  assert.equal(state.round.results[0].points, 85);
  assert.equal(state.board.find((t) => t.id === teamId)?.total, 85);
  assert.equal(state.round.rulings[0].key, "Jellies");
  assert.equal(state.received, 0);
  assert.equal((await early(path + "/state")).data.mine, null);
  assert.equal(
    (await host(round + "/judge", "POST", { teamId, depth: 0, previous: 0 }))
      .status,
    409,
  );
  assert.equal(
    (
      await host(round + "/judge", "POST", {
        teamId,
        depth: 0,
        previous: state.round.missedTeams[0].previous,
      })
    ).status,
    200,
  );
  const after = (await host(path + "/state?view=host")).data;
  assert.equal(after.round.rulings[0].canonical, "Miss");
  assert.deepEqual(
    after.round.results.map((x) => x.points),
    [0],
  );
});

test("retiring the builder preserves scores, historical banks and outstanding reviews", async t => {
  const dir = await mkdtemp(join(tmpdir(), "krill-upgrade-"));
  const file = join(dir, "quiz.sqlite");
  t.after(() => rm(dir, {recursive:true, force:true}));
  let game = new Game(file);
  const {code} = game.create("Old event");
  game.join(game.room(code), {name:"Divers"});
  const team = game.one("SELECT id FROM teams WHERE room=?",code);
  const {id} = game.start(game.room(code), {questionId:"tube-lines", previous:null, seconds:0});
  game.submit(game.room(code), game.round(code,id), team, {answer:"Victoria", revision:0});
  const snapshot = {...game.question("tube-lines"),id:"legacy",knowledge:"old-bank",answers:[]};
  game.run("UPDATE rounds SET question=?,snapshot=?,phase='locked' WHERE id=?",snapshot.id,JSON.stringify(snapshot),id);
  game.db.exec("CREATE TABLE library(id TEXT); CREATE TABLE kb_member(bank,entity,name,depth,aliases); CREATE TABLE kb_appeal(round,team,raw); PRAGMA user_version=7;");
  game.run("INSERT INTO kb_member VALUES('old-bank','one','Victoria',2,'[]')");
  game.db.exec("CREATE TABLE kb_round_proof(round,bank,entity)");
  game.run("INSERT INTO kb_member VALUES('new-bank','two','Circle',3,?)", JSON.stringify(["Circular"]));
  game.run("INSERT INTO kb_round_proof VALUES(?,'new-bank','two')", id);
  game.run("INSERT INTO round_additions VALUES(?,'circle','Circle',2)", id);
  game.run("INSERT INTO kb_appeal VALUES(?,?,?)", id, team.id, "Victoria");
  const { retireQuestionBuilder } = await import("../server/upgrade.ts");
  assert.throws(() => game.tx(() => retireQuestionBuilder(game.db)), /reveal/);
  assert.equal(game.one("PRAGMA user_version").user_version, 7);
  game.run("UPDATE rounds SET phase='revealed' WHERE id=?",id);
  const before = game.state(game.room(code),true).board;
  game.db.close();
  game = new Game(file);
  try {
    assert.equal(game.one("PRAGMA user_version").user_version,8);
    assert.equal(game.all("SELECT name FROM sqlite_schema WHERE name GLOB 'kb_*' OR name='library'").length,0);
    assert.equal(game.one("SELECT requested FROM review_guesses WHERE round=? AND team=?", id,team.id).requested,1);
    assert.deepEqual(game.state(game.room(code),true).board,before);
    assert.deepEqual(game.judgingBank(game.round(code,id)).answers, [
      {name:"Victoria",depth:2,aliases:[]}, {name:"Circle",depth:2,aliases:["Circular"]},
    ]);
    assert.throws(() => game.question("legacy"), /not found/);
  } finally { game.db.close(); }
});

test("offline restore replaces a damaged database and preserves its sidecars", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "krill-restore-")),
    file = join(dir, "quiz.sqlite");
  t.after(() => rm(dir, { recursive: true, force: true }));
  const game = new Game(file);
  const { code } = game.create("Survivors");
  game.db.close();
  const run = (action, name) =>
    spawnSync(process.execPath, ["scripts/database.mjs", action, name], {
      env: { ...process.env, DB_PATH: file, BACKUP_DIR: join(dir, "backups") },
      encoding: "utf8",
    });
  assert.equal(run("backup", "good.sqlite").status, 0);
  await writeFile(file, "damaged");
  await writeFile(file + "-wal", "old WAL");
  await writeFile(join(dir, "backups", "bad.sqlite"), "unreadable source");
  assert.notEqual(run("restore", "bad.sqlite").status, 0);
  assert.equal(await readFile(file, "utf8"), "damaged");
  const restored = run("restore", "good.sqlite");
  assert.equal(restored.status, 0, restored.stderr);
  const folder = (await readdir(dir)).find((name) =>
    name.includes(".before-restore-"),
  );
  assert.equal(
    await readFile(join(dir, folder, "database.sqlite"), "utf8"),
    "damaged",
  );
  assert.equal(
    await readFile(join(dir, folder, "database.sqlite-wal"), "utf8"),
    "old WAL",
  );
  const reopened = new Game(file);
  try {
    assert.equal(reopened.room(code).name, "Survivors");
  } finally {
    reopened.db.close();
  }
});
