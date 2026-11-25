import test from "node:test";
import assert from "node:assert/strict";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
import { fixture, client } from "./fixture.mjs";
import { validateQuestion, matchAnswer } from "../server/rules.ts";
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
      questionId: "summer-hosts",
      seconds: 0,
    });
    const round = game.round(code, id);
    const bank = game.question(code, "summer-hosts").answers;
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

test("all starter categories, aliases, typos and ambiguous examples", async () => {
  const pack = JSON.parse(await readFile("data/questions.json", "utf8"));
  assert.equal(pack.length, 20);
  assert.equal(new Set(pack.map((x) => x.id)).size, 20);
  for (const raw of pack) {
    const q = validateQuestion(raw, raw.id);
    assert.ok(q.answers.filter((a) => a.depth === 5).length <= Math.max(1, Math.floor(q.answers.length / 20)));
    for (const a of q.answers)
      for (const name of [a.name, ...a.aliases])
        assert.deepEqual(matchAnswer(name, q.answers), {
          answer: a,
          exact: true,
        });
  }
  const cities = pack.find((x) => x.id === "summer-hosts").answers;
  const close = matchAnswer("Barcelnoa", cities);
  assert.equal(close.answer.name, "Barcelona");
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

test("bulk import, paging, semantic receipts and immutable large rounds", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    team = client(f);
  await ok(host("/login", "POST", { password: f.password }));
  const { code } = await ok(host("/rooms", "POST", { name: "Large library" }));
  const root = "/rooms/" + code;
  const sample = await ok(host("/packs/sample"));
  assert.equal((await team("/packs/sample")).status, 401);
  assert.deepEqual(await ok(host(root + "/packs", "POST", sample)), {
    added: 1,
    skipped: 1,
  });
  const q = {
    id: "large",
    title: "Large archive",
    topic: "Archive",
    prompt: "Name an entry.",
    matching: "names",
    answers: Array.from({ length: 10000 }, (_, i) => ({
      name: "Specimen " + i,
      depth: (i % 5) + 1,
      aliases: ["Catalogue entry " + i],
    })),
  };
  const extra = Array.from({ length: 999 }, (_, i) => ({
    ...sample.questions[0],
    id: "small-" + i,
    title: "Device set " + i,
  }));
  const pack = { questions: [q, ...extra] };
  assert.equal((await team(root + "/packs", "POST", pack)).status, 401);
  assert.deepEqual(await ok(host(root + "/packs", "POST", pack)), {
    added: 1000,
    skipped: 0,
  });
  assert.deepEqual(
    await ok(host(root + "/packs", "POST", { questions: [q] })),
    { added: 0, skipped: 1 },
  );
  const failed = await host(root + "/packs", "POST", {
    questions: [
      { ...q, id: "rollback" },
      { ...q, title: "Conflicting question" },
    ],
  });
  assert.equal(failed.status, 409);
  assert.equal((await host(root + "/library/rollback")).status, 404);
  assert.equal((await ok(host(root + "/library"))).length, 1198);
  const part = await ok(
    host(root + "/library/large/answers?depth=5&offset=40"),
  );
  assert.equal(part.total, 10000);
  assert.equal(part.filtered, 2000);
  assert.equal(part.answers.length, 40);
  assert.ok(part.answers.every((a) => a.depth === 5));
  await ok(team(root + "/join", "POST", { name: "Group one" }));
  const start = async (questionId, previous) =>
    (
      await ok(
        host(root + "/start", "POST", { questionId, previous, seconds: 0 }),
      )
    ).id;
  const gaming = await start(sample.questions[0].id, null);
  const gamingPath = root + "/rounds/" + gaming;
  const inferred = await team(gamingPath + "/answer", "POST", {
    answer: "Xbox controller",
    revision: 0,
  });
  assert.equal(inferred.status, 422);
  assert.equal(inferred.data.suggestion, "Games controller");
  assert.equal((await ok(team(root + "/state"))).mine, null);
  await ok(
    team(gamingPath + "/answer", "POST", {
      answer: inferred.data.suggestion,
      revision: 0,
    }),
  );
  const locked = (await ok(team(root + "/state"))).mine;
  assert.equal(locked.canonical ?? locked.raw, "Games controller");
  await ok(host(gamingPath + "/lock", "POST", {}));
  await ok(host(gamingPath + "/reveal", "POST", {}));
  assert.equal((await ok(team(root + "/state"))).round.results[0].points, 10);
  const id = await start("large", gaming);
  const path = root + "/rounds/" + id;
  const live = await ok(host(root + "/state?view=host"));
  assert.equal(live.round.answerCount, 10000);
  assert.ok(JSON.stringify(live).length < 10000 && !("accepted" in live.round));
  assert.equal((await team(path + "/answers")).status, 401);
  await ok(
    host(root + "/library/large", "PUT", {
      ...q,
      revision: 1,
      answers: [q.answers[0]],
    }),
  );
  const last = await ok(host(path + "/answers?q=Catalogue%20entry%209999"));
  assert.equal(last.answers[0].name, "Specimen 9999");
  assert.equal(last.filtered, 1);
  const receipt = await ok(
    team(path + "/answer", "POST", {
      answer: "Catalogue entry 9999",
      revision: 0,
    }),
  );
  assert.equal(receipt.canonical, "Specimen 9999");
  assert.equal(
    (
      await team(path + "/answer", "POST", {
        answer: "Specimen 0",
        revision: 0,
      })
    ).status,
    409,
  );
  await f.stop();
  await f.start();
  assert.equal(
    (await ok(host(path + "/answers?offset=9960"))).answers.length,
    40,
  );
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
      questionId: "summer-hosts",
      seconds: 0,
    }),
  );
  const round = path + "/rounds/" + id;
  assert.ok(
    (await ok(host(path + "/state?view=host"))).round.accepted.length > 1,
  );
  const send = (answer) =>
    team(round + "/answer", "POST", { answer, revision: 0 });
  const candidates = ["London", "Barcelona"];
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
    assert.equal(pack.length, 197);
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
    const start = async (previous, questionId = "summer-hosts", seconds = 0) =>
      ok(host(root + "/start", "POST", { previous, seconds, questionId }));
    const { id } = await start(null);
    assert.equal(
      (
        await host(root + "/start", "POST", {
          previous: null,
          seconds: 0,
          questionId: "summer-hosts",
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
              ? "Barcelnoa"
              : "zzzz";
        const result = await q(path + "/answer", "POST", {
          answer: guess,
          revision: 0,
        });
        assert.equal(result.status, i % 3 === 0 ? 200 : 422);
        assert.equal(
          result.data.suggestion,
          guess === "Barcelnoa" ? "Barcelona" : undefined,
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
          ok(q(path + "/answer", "POST", { answer: "Barcelona", revision: 0 })),
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
    for (const attempt of ["zzzz", "Barcelnoa"]) {
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
          answer: "Barcelona",
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
          x.canonical === "Barcelona" && x.points === 15 && x.metres === 300,
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
    const q = await ok(host(root + "/library/summer-hosts"));
    await ok(
      host(root + "/library/summer-hosts", "PUT", {
        ...q,
        title: "Changed title",
      }),
    );
    assert.notEqual((await hs()).round.title, "Changed title");
    const custom = await ok(
      host(root + "/library", "POST", {
        title: "Event chair",
        topic: "Society",
        prompt: "Name the chair.",
        source: "",
        answers: [{ name: "The chair", aliases: [], depth: 3 }],
      }),
    );
    const second = await start(id, custom.id, 1);
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
    const third = await start(second.id, "summer-hosts", 1);
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
      host(path + "/judge", "POST", { key: "paris", name: "Barcelona" }),
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
          questionId: "summer-hosts",
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
