import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fixture, client } from "./fixture.mjs";
import { validateQuestion, matchAnswer, resolveAnswer } from "../server/rules.ts";
import { builtInQuestions } from "../server/catalogue.ts";
import { Game } from "../server/store.ts";
const ok = async (p) => {
  const r = await p;
  assert.ok(r.status >= 200 && r.status < 300, JSON.stringify(r));
  return r.data;
};

test("host login accepts the exact configured password at any length", async (t) => {
  for (const password of ["x", "", " a  b ", "z".repeat(513)]) {
    const f = await fixture(password);
    t.after(() => f.close());
    const host = client(f);
    for (const value of [
      {},
      {
        password:
          password.trim() === password ? password + "x" : password.trim(),
      },
    ]) {
      assert.equal((await host("/login", "POST", value)).status, 401);
    }
    await ok(host("/login", "POST", { password }));
    await ok(host("/rooms"));
  }
});

test("upgrade scores all five tiers consistently and keeps finished events on their original scale", async (t) => {
  const directory = await mkdtemp(join(tmpdir(), "krill-scoring-"));
  const path = join(directory, "quiz.sqlite");
  let game = new Game(path);
  t.after(async () => {
    game.db.close();
    await rm(directory, { recursive: true, force: true });
  });
  const codes = [];
  for (const finished of [0, 1]) {
    const { code } = game.create("Previous event");
    codes.push(code);
    const { id } = game.start(game.room(code), {
      previous: null,
      questionId: "geography-capitals",
      seconds: 0,
    });
    const round = game.round(code, id);
    const bank = game.question("geography-capitals").answers;
    for (let tier = 1; tier <= 5; tier++) {
      const name = "Crew " + tier;
      game.join(game.room(code), { name });
      const team = game.one(
        "SELECT id FROM teams WHERE room=? AND name=?",
        code,
        name,
      );
      game.submit(game.room(code), round, team, {
        answer: bank.find((a) => a.depth === tier).name,
        revision: 0,
      });
    }
    game.run("UPDATE rounds SET phase='revealed' WHERE id=?", id);
    game.run("UPDATE rooms SET finished=? WHERE code=?", finished, code);
  }
  // The previous release had neither column nor a saved roster for historical rounds.
  game.db.exec(
    "ALTER TABLE rooms DROP COLUMN score_version; ALTER TABLE rounds DROP COLUMN last_team_id; ALTER TABLE rounds DROP COLUMN revealed_at; PRAGMA user_version=0;",
  );
  game.db.close();
  game = new Game(path);
  for (let i = 0; i < codes.length; i++) {
    const state = game.state(game.room(codes[i]), true);
    const expected = i === 0 ? [10, 15, 60, 85, 100] : [1, 2, 4, 7, 10];
    assert.deepEqual(
      state.round.results.map((x) => x.points),
      expected,
    );
    assert.deepEqual(
      state.round.results.map((x) => x.metres),
      [50, 300, 1200, 4100, 7800],
    );
    assert.deepEqual(
      state.board.map((x) => x.total),
      expected.toReversed(),
    );
  }
});

test("shipped aliases, typos and ambiguous examples", () => {
  const pack = [...builtInQuestions.values()];
  for (const q of pack)
    for (const a of q.answers)
      for (const name of [a.name, ...a.aliases]) {
        const result = resolveAnswer(name, q.answers, q.matching);
        // Some shortened aliases identify several names. Those require more detail.
        if (result === null && name !== a.name) continue;
        assert.equal(result?.answer.name, a.name);
      }
  const cities = builtInQuestions.get("geography-capitals").answers;
  const close = matchAnswer("Addis Abbaa", cities);
  assert.equal(close.answer.name, "Addis Ababa");
  assert.equal(close.exact, false);
  assert.equal(matchAnswer("ZZZ", cities), undefined);
  assert.equal(
    matchAnswer(
      "Marty",
      ["Marta", "Marti"].map((name) => ({ name, depth: 1, aliases: [] })),
    ),
    undefined,
  );
  assert.throws(
    () =>
      validateQuestion(
        {
          ...pack[0],
          answers: [
            { name: "A", depth: 1, aliases: ["B"] },
            { name: "b", depth: 2, aliases: [] },
          ],
        },
        "bad",
      ),
    /Conflicting/,
  );
});
test("category matches are directional, British-localised and scoped to this round", () => {
  const match = (guess, names, mode = "categories") =>
    matchAnswer(
      guess,
      names.map((name) => ({ name, depth: 2, aliases: [] })),
      mode,
    );
  assert.equal(
    match("an Xbox Series X wireless controller", ["Games controller"]).exact,
    false,
  );
  assert.equal(match("xbox controler", ["Games controller"]).exact, false);
  assert.equal(
    match("Xbox controller", ["Games controller", "Xbox controller"]).answer
      .name,
    "Xbox controller",
  );
  assert.equal(match("games controller", ["Xbox controller"]), undefined);
  assert.equal(
    match("air traffic controller", ["Games controller"]),
    undefined,
  );
  assert.equal(
    matchAnswer("piano keyboard", [
      { name: "Computer keyboard", aliases: ["Keyboard"], depth: 1 },
    ]),
    undefined,
  );
  assert.equal(match("joypad", ["Games controller", "Gamepad"]), undefined);
  assert.equal(match("puffin", ["Seabird", "Fish"]).answer.name, "Seabird");
  assert.equal(match("sparrow", ["Bird"]).exact, false);
  assert.equal(
    match("bass guitar", ["Musical instrument"]).answer.name,
    "Musical instrument",
  );
  assert.equal(match("sparrow", ["Bird"], "names"), undefined);
  const q = {
    title: "Colours",
    topic: "Art",
    prompt: "Name a colour.",
    answers: [{ name: "Gray", depth: 2 }],
  };
  const [grey] = validateQuestion(q, "uk").answers;
  assert.equal(grey.name, "Grey");
  assert.ok(grey.aliases.includes("Gray"));
  assert.equal(
    validateQuestion(
      {
        ...q,
        matching: "names",
        answers: [{ name: "The Color Purple", depth: 1 }],
      },
      "film",
    ).answers[0].name,
    "The Color Purple",
  );
  assert.throws(
    () =>
      validateQuestion(
        { ...q, answers: [...q.answers, { name: "Grey", depth: 4 }] },
        "uk",
      ),
    /Conflicting/,
  );
});

test("large published banks page privately and survive a restart", async t => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f), team = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const room = "/rooms/" + (await ok(host("/rooms", "POST", {name:"Large bank"}))).code;
  await ok(team(room + "/join", "POST", {name:"Dolphins"}));
  const start = await ok(host(room + "/start", "POST", {previous:null, questionId:"general-currencies", seconds:0}));
  const round = room + "/rounds/" + start.id;
  const live = await ok(host(room + "/state?view=host"));
  assert.ok(live.round.answerCount > 80);
  assert.ok(JSON.stringify(live).length < 10000);
  assert.equal((await team(round + "/answers")).status, 401);
  const next = await ok(host(round + "/answers?offset=40"));
  assert.equal(next.answers.length, 40);
  const receipt = await ok(team(round + "/answer", "POST", {answer:"GBP", revision:0}));
  assert.equal(receipt.canonical, "British Pound");
  await f.stop();
  await f.start();
  assert.deepEqual(await ok(host(round + "/answers?offset=40")), next);
  assert.equal((await ok(team(room + "/state"))).mine.canonical, receipt.canonical);
});
test("the first accepted answer wins and room deletion requires the host and exact title", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    team = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const name = "Sea & Sky";
  const room = await ok(host("/rooms", "POST", { name }));
  const other = await ok(host("/rooms", "POST", { name }));
  const path = "/rooms/" + room.code;
  await ok(team(path + "/join", "POST", { name: "Krill crew" }));
  const { id } = await ok(
    host(path + "/start", "POST", {
      previous: null,
      questionId: "geography-capitals",
      seconds: 0,
    }),
  );
  const round = path + "/rounds/" + id;
  assert.ok(
    (await ok(host(path + "/state?view=host"))).round.answerCount > 1,
  );
  const send = (answer) =>
    team(round + "/answer", "POST", { answer, revision: 0 });
  const candidates = ["London", "Addis Ababa"];
  const replies = await Promise.all(candidates.map(send));
  assert.deepEqual(replies.map((r) => r.status).sort(), [200, 409]);
  const winner = candidates[replies.findIndex((r) => r.status === 200)];
  await ok(send(winner));
  await ok(host(round + "/lock", "POST", {}));
  await ok(
    host(round + "/judge", "POST", { key: winner.toLowerCase(), name: winner }),
  );
  await ok(host(round + "/reveal", "POST", {}));
  assert.equal((await team(path, "DELETE", { name })).status, 401);
  for (const typed of ["", name.toLowerCase(), name + " "]) {
    assert.equal((await host(path, "DELETE", { name: typed })).status, 400);
  }
  assert.equal((await ok(team(path + "/state"))).mine.raw, winner);
  await ok(host(path, "DELETE", { name }));
  assert.equal((await team(path + "/state")).status, 404);
  assert.equal((await host(round + "?view=host")).status, 404);
  assert.deepEqual(
    (await ok(host("/rooms"))).rooms.map((r) => r.code),
    [other.code],
  );
  assert.ok((await ok(host("/rooms/" + other.code + "/library"))).length > 0);
});
test(
  "30 teams, retry-safe submissions, judging, recovery, restarts, 21 rounds and final totals",
  { timeout: 90000 },
  async (t) => {
    const f = await fixture();
    t.after(() => f.close());
    const host = client(f),
      anon = client(f);
    assert.equal((await anon("/rooms")).status, 401);
    await ok(host("/login", "POST", { password: f.password }));
    const { code } = await ok(
      host("/rooms", "POST", { name: "Society crawl" }),
    );
    const root = "/rooms/" + code;
    const hs = () => ok(host(root + "/state?view=host"));
    const created = await hs();
    assert.equal(created.round, null);
    const pack = await ok(host(root + "/library"));
    assert.equal(pack.length, 182);
    assert.equal((await anon(root + "/library")).status, 401);
    const teams = Array.from({ length: 30 }, () => client(f));
    await Promise.all(
      teams.map((q, i) =>
        ok(
          q(root + "/join", "POST", {
            name: "Team " + String(i).padStart(2, "0"),
          }),
        ),
      ),
    );
    assert.equal(
      (await anon(root + "/join", "POST", { name: "Team 00" })).status,
      409,
    );
    const start = async (previous, questionId = "geography-capitals", seconds = 0) =>
      ok(host(root + "/start", "POST", { previous, seconds, questionId }));
    const { id } = await start(null);
    assert.equal(
      (
        await host(root + "/start", "POST", {
          previous: null,
          seconds: 0,
          questionId: "geography-capitals",
        })
      ).status,
      409,
    );
    const path = root + "/rounds/" + id;
    const s = await ok(teams[0](root + "/state"));
    assert.ok(!("accepted" in s.round) && !("results" in s.round));
    assert.ok(!("snapshot" in s.round));
    assert.ok(!("roster" in s) && !("received" in s));
    assert.equal((await teams[0](path)).status, 403);
    assert.equal((await teams[0](root + "/state?view=host")).status, 401);
    await Promise.all(
      teams.map(async (q, i) => {
        if (i === 29) return; // This team never tries an answer.
        const guess =
          i % 3 === 0
            ? i === 0
              ? "Paris"
              : "London"
            : i % 3 === 1 || i === 2
              ? "Addis Abbaa"
              : "zzzz";
        const result = await q(path + "/answer", "POST", {
          answer: guess,
          revision: 0,
        });
        assert.equal(result.status, i % 3 === 0 ? 200 : 422);
        assert.equal(
          result.data.suggestion,
          guess === "Addis Abbaa" ? "Addis Ababa" : undefined,
        );
      }),
    );
    const pending = await hs();
    assert.equal(pending.received, 10);
    assert.equal(pending.roster.length, pending.eligible);
    assert.deepEqual(
      pending.roster.map((t) => t.name),
      Array.from(
        { length: 30 },
        (_, i) => "Team " + String(i).padStart(2, "0"),
      ),
    );
    for (const [i, entry] of pending.roster.entries()) {
      assert.deepEqual(Object.keys(entry).sort(), ["id", "name", "submitted"]);
      assert.equal(entry.submitted, i % 3 === 0);
    }
    assert.equal((await ok(teams[1](root + "/state"))).mine, null);
    await Promise.all(
      teams
        .filter((_, i) => i % 3 === 1)
        .map((q) =>
          ok(q(path + "/answer", "POST", { answer: "Addis Ababa", revision: 0 })),
        ),
    );
    await Promise.all(
      Array.from({ length: 5 }, () =>
        ok(
          teams[0](path + "/answer", "POST", { answer: "Paris", revision: 0 }),
        ),
      ),
    );
    assert.equal(
      (
        await teams[0](path + "/answer", "POST", {
          answer: "Berlin",
          revision: 1,
        })
      ).status,
      409,
    );
    for (const attempt of ["zzzz", "Addis Abbaa"]) {
      assert.equal(
        (
          await teams[0](path + "/answer", "POST", {
            answer: attempt,
            revision: 2,
          })
        ).status,
        409,
      );
    }
    assert.deepEqual((await ok(teams[0](root + "/state"))).mine, {
      raw: "Paris",
      revision: 1,
    });
    const beforeLock = await hs();
    assert.equal(beforeLock.received, 20);
    await ok(host(path + "/lock", "POST", {}));
    assert.equal(
      (
        await teams[1](path + "/answer", "POST", {
          answer: "Tokyo",
          revision: 1,
        })
      ).status,
      409,
    );
    assert.equal(
      (
        await teams[2](path + "/answer", "POST", {
          answer: "Addis Ababa",
          revision: 0,
        })
      ).status,
      409,
    );
    assert.equal(
      (
        await host(path + "/judge", "POST", {
          key: "paris",
          name: "Example",
          depth: 4,
        })
      ).status,
      422,
    );
    const late = client(f);
    await ok(late(root + "/join", "POST", { name: "Late arrival" }));
    await ok(host(path + "/lock", "POST", {}));
    assert.equal((await hs()).eligible, 30);
    assert.deepEqual((await hs()).roster, beforeLock.roster);
    assert.equal((await ok(late(root + "/state"))).eligibleForRound, false);
    await ok(host(path + "/reveal", "POST", {}));
    const revealTime = (await hs()).round.revealedAt;
    assert.ok(Number.isInteger(revealTime));
    await ok(host(path + "/reveal", "POST", {}));
    const first = await hs();
    assert.equal(first.round.revealedAt, revealTime);
    assert.equal(first.round.results.length, 30);
    assert.equal(
      first.round.results.filter(
        (x) => x.canonical === "Miss" && x.points === 0 && x.metres === 0,
      ).length,
      10,
    );
    assert.equal(
      first.round.results.filter(
        (x) =>
          x.canonical === "Addis Ababa" && x.points === 15 && x.metres === 300,
      ).length,
      10,
    );
    assert.equal(
      first.round.results.some((x) => x.team === "Late arrival"),
      false,
    );
    assert.equal(
      first.board.reduce((sum, x) => sum + x.total, 0),
      250,
    );
    assert.equal(first.board.filter((x) => x.rank === 1).length, 10);
    assert.equal(first.board[10].rank, 11);
    const target = first.board.find((x) => x.name === "Team 00");
    const superseded = await ok(
      host(root + "/transfer", "POST", { teamId: target.id }),
    );
    const transfer = await ok(
      host(root + "/transfer", "POST", { teamId: target.id }),
    );
    assert.match(transfer.transfer, /^[A-HJ-NP-Z2-9]{5}$/);
    assert.notEqual(transfer.transfer, superseded.transfer);
    assert.equal((await anon(root + "/join", "POST", superseded)).status, 400);
    const replacement = client(f);
    await ok(replacement(root + "/join", "POST", transfer));
    assert.equal((await teams[0](root + "/state")).status, 401);
    assert.equal((await anon(root + "/join", "POST", transfer)).status, 400);
    teams[0] = replacement;
    const second = await start(id, "ultimate-throws", 1);
    const timed = root + "/rounds/" + second.id;
    const extra = await hs();
    await ok(
      host(timed + "/extend", "POST", { previous: extra.round.deadline }),
    );
    assert.equal(
      (
        await host(timed + "/extend", "POST", {
          previous: extra.round.deadline,
        })
      ).status,
      409,
    );
    await ok(host(timed + "/lock", "POST", {}));
    await ok(host(timed + "/reveal", "POST", {}));
    const third = await start(second.id, "geography-capitals", 1);
    await f.stop();
    await new Promise((r) => setTimeout(r, 1100));
    await f.start();
    assert.equal((await hs()).round.phase, "locked");
    assert.equal((await ok(teams[0](root + "/state"))).team.name, "Team 00");
    await ok(host(root + "/rounds/" + third.id + "/reveal", "POST", {}));
    assert.equal(
      (await hs()).round.results.filter((x) => x.canonical === "Miss").length,
      31,
    );
    let prev = third.id;
    for (let n = 4; n <= 21; n++) {
      const r = await start(prev);
      const p = root + "/rounds/" + r.id;
      await ok(
        teams[0](p + "/answer", "POST", { answer: "London", revision: 0 }),
      );
      await ok(host(p + "/lock", "POST", {}));
      await ok(host(p + "/reveal", "POST", {}));
      prev = r.id;
    }
    await ok(
      host(path + "/judge", "POST", { key: "paris", name: "Addis Ababa" }),
    );
    const final = await hs();
    assert.equal(final.history.length, 21);
    assert.equal(
      final.board.reduce((sum, x) => sum + x.total, 0),
      435,
    );
    assert.equal(final.board[0].name, "Team 00");
    assert.equal((await ok(host(path + "?view=host"))).rulings.length, 1);
    await ok(host(root + "/finish", "POST", { previous: prev }));
    assert.equal((await hs()).room.finished, true);
    assert.equal(
      (
        await host(root + "/start", "POST", {
          previous: prev,
          seconds: 0,
          questionId: "geography-capitals",
        })
      ).status,
      409,
    );
    const env = {
      ...process.env,
      DB_PATH: f.file,
      BACKUP_DIR: join(f.dir, "backups"),
    };
    execFileSync(
      process.execPath,
      ["scripts/database.mjs", "backup", "test.sqlite"],
      { env },
    );
    await ok(host("/rooms", "POST", { name: "After backup" }));
    await f.stop();
    execFileSync(
      process.execPath,
      ["scripts/database.mjs", "restore", "test.sqlite"],
      { env },
    );
    await f.start();
    assert.equal((await ok(host("/rooms"))).rooms.length, 1);
    assert.equal((await hs()).history.length, 21);
    assert.deepEqual((await hs()).board, final.board);
  },
);
