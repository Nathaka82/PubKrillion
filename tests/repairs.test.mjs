import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { randomBytes } from "node:crypto";
import { spawnSync } from "node:child_process";
import { catalogueQuestions } from "../server/catalogue.ts";
import { validateQuestion, matchAnswer } from "../server/rules.ts";
import { Game, digest } from "../server/store.ts";
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

test("catalogue upgrade keeps room edits, shared play history and original snapshots", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "krill-upgrade-")),
    file = join(dir, "quiz.sqlite");
  let game = new Game(file);
  t.after(async () => {
    game.db.close();
    await rm(dir, { recursive: true, force: true });
  });
  const { code } = game.create("Old room");
  const { revision, ...canonical } = game.question(code, "tube-lines");
  const { coverage, context, ...oldFields } = canonical;
  oldFields.answers = oldFields.answers.map((a, i) => ({ ...a, depth: Number("11223334455"[i]) }));
  const oldStarter = { ...oldFields, topic: "Travel" };
  const hashes = JSON.parse(
    await readFile("data/catalogue-v1-hashes.json", "utf8"),
  );
  assert.equal(digest(JSON.stringify(oldStarter)), hashes["tube-lines"]);
  game.run(
    "INSERT INTO library(content,room,id) VALUES(?,?,?)",
    JSON.stringify(oldStarter),
    code,
    canonical.id,
  );
  const legacy = {
    ...canonical,
    id: "general-tube-lines",
    title: "Society Tube lines",
  };
  game.importPack(code, { questions: [legacy] });
  const { id } = game.start(game.room(code), {
    questionId: legacy.id,
    seconds: 0,
    previous: null,
  });
  const readSnapshot = () =>
    game.one("SELECT snapshot FROM rounds WHERE id=?", id).snapshot;
  const snapshot = readSnapshot();
  assert.equal(JSON.parse(snapshot).title, legacy.title);
  const customRoom = game.create("Two custom questions").code;
  game.run(
    "INSERT INTO library(content,room,id) VALUES(?,?,?)",
    JSON.stringify({ ...canonical, title: "Host starter" }),
    customRoom,
    canonical.id,
  );
  game.importPack(customRoom, { questions: [legacy] });
  game.db.exec("PRAGMA user_version=3");
  game.db.close();
  game = new Game(file);
  assert.equal(game.round(code, id).question, canonical.id);
  assert.equal(readSnapshot(), snapshot);
  assert.equal(game.question(customRoom, canonical.id).title, "Host starter");
  assert.equal(game.question(customRoom, legacy.id).title, legacy.title);
  const library = game.library(code);
  assert.equal(
    library.some((q) => q.id === legacy.id),
    false,
  );
  const shared = library.find((q) => q.id === canonical.id);
  assert.equal(shared.title, legacy.title);
  assert.equal(shared.plays, 1);
  assert.equal(
    game.importPack(code, { questions: [canonical] }, true).added,
    0,
  );
});

test("existing rooms inherit categories and prompts while open round snapshots retain the original wording", async (t) => {
  const dir = await mkdtemp(join(tmpdir(), "krill-categories-"));
  const file = join(dir, "quiz.sqlite");
  let game = new Game(file);
  t.after(async () => {
    game.db.close();
    await rm(dir, { recursive: true, force: true });
  });
  const { code } = game.create("Friday night");
  const id = "general-paris-venues";
  const { revision, ...current } = game.question(code, id);
  const old = {
    ...current,
    context: undefined,
    answers: current.answers.map((a, i) => ({ ...a, depth: Number("34212525412245341221131533451335454"[i]) })),
    topic: "General Knowledge",
    prompt: "Name a venue in the Paris 2024 Olympic venues file.",
  };
  const priorRelease = { ...old, topic: "Sport", prompt: current.prompt };
  const priorHashes = JSON.parse(await readFile("data/catalogue-v3-hashes.json", "utf8"));
  assert.equal(digest(JSON.stringify(priorRelease)), priorHashes[id]);
  const previousRoom = game.create("Previous release").code;
  const editedRoom = game.create("Edited release").code;
  for (const [room, bank] of [[previousRoom, priorRelease], [editedRoom, { ...priorRelease, title: "Our venues" }]])
    game.run("INSERT INTO library(room,id,content) VALUES(?,?,?)", room, id, JSON.stringify(bank));
  game.run(
    "INSERT INTO library(room,id,content) VALUES(?,?,?)",
    code,
    id,
    JSON.stringify(old),
  );
  const round = game.start(game.room(code), {
    previous: null,
    questionId: id,
    seconds: 0,
  });
  const snapshot = game.one(
    "SELECT snapshot FROM rounds WHERE id=?",
    round.id,
  ).snapshot;
  game.join(game.room(code), { name: "Sharks" });
  const team = game.one("SELECT * FROM teams WHERE room=?", code);
  game.tx(() =>
    game.submit(game.room(code), game.round(code, round.id), team, {
      answer: old.answers[0].name,
      revision: 0,
    }),
  );
  // A pre-upgrade host would still be looking at the lock button.
  game.run(
    "UPDATE rounds SET phase='open',last_team_id=NULL WHERE id=?",
    round.id,
  );
  game.db.exec("PRAGMA user_version=4");
  game.db.close();
  game = new Game(file);
  assert.equal(game.question(code, id).prompt, current.prompt);
  assert.equal(game.question(code, id).topic, "Sport");
  assert.equal(game.question(code, id).revision, 2);
  assert.deepEqual(game.question(previousRoom, id), { ...current, revision: 2 });
  assert.equal(game.question(editedRoom, id).title, "Our venues");
  assert.equal(game.round(code, round.id).phase, "locked");
  assert.equal(
    game.one("SELECT snapshot FROM rounds WHERE id=?", round.id).snapshot,
    snapshot,
  );
  const fresh = game.create("Another room").code;
  game.save(code, id, { ...current, revision: 2, title: "Our venues" });
  assert.equal(game.question(fresh, id).title, current.title);
  assert.equal(game.importPack(code, { questions: [old] }).skipped, 1);
  assert.equal(game.question(code, id).title, "Our venues");
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
