import test from "node:test";
import assert from "node:assert/strict";
import {
  catalogue,
  catalogueQuestions,
  previewPack,
} from "../server/catalogue.ts";
import { validateQuestion, matchAnswer } from "../server/rules.ts";
import { fixture, client } from "./fixture.mjs";

test("released packs contain distinct, playable questions and scoped source lists", () => {
  const ids = new Set();
  const banks = new Map();
  assert.deepEqual(
    catalogue.map((p) => p.id),
    ["general", "screen", "music", "geography", "animals", "ultimate"],
  );
  for (const pack of catalogue) {
    const questions = catalogueQuestions(pack.id);
    assert.equal(questions.length, 30);
    assert.equal(questions.length, pack.count);
    assert.equal(
      questions.reduce((sum, q) => sum + q.answers.length, 0),
      pack.answers,
    );
    assert.equal(new Set(questions.map((q) => q.prompt)).size, 30);
    if (pack.id !== "ultimate")
      assert.ok(questions.filter((q) => q.answers.length >= 30).length >= 20);
    const preview = previewPack(pack.id);
    for (const [i, q] of questions.entries()) {
      assert.ok(!ids.has(q.id));
      ids.add(q.id);
      assert.match(q.source, /^https?:\/\//);
      assert.ok(q.coverage.length > 18);
      assert.equal(preview[i].count, q.answers.length);
      assert.ok(!("answers" in preview[i]));
      const validated = validateQuestion(q, q.id);
      banks.set(q.id, validated);
      for (const a of validated.answers)
        assert.equal(
          matchAnswer(a.name, validated.answers, q.matching)?.answer,
          a,
          q.id + " " + a.name,
        );
    }
  }
  assert.equal(ids.size, 180);
  const answer = (id, term) => {
    const q = banks.get(id);
    return matchAnswer(term, q.answers, q.matching);
  };
  for (const [id, input, name] of [
    ["animals-british-list", "Fratercula arctica", "Puffin"],
    ["general-elements", "Aluminum", "Aluminium"],
    ["general-elements", "Og", "Oganesson"],
    ["general-bible", "First Thessalonians", "1 Thessalonians"],
    ["screen-disney", "Zootopia", "Zootropolis"],
    ["geography-lakes", "Lake Victoria", "Victoria"],
    ["music-beatles", "Hey Jude", "Hey Jude"],
    ["ultimate-throws", "flick", "Forehand"],
  ])
    assert.equal(answer(id, input)?.answer.name, name, id);
  assert.equal(banks.get("animals-british-list").answers.length, 636);
  assert.equal(banks.get("general-paris-events").answers.length, 329);
  assert.notEqual(
    answer("general-paris-events", "Judo Men +100 kg")?.answer.name,
    answer("general-paris-events", "Judo Men -100 kg")?.answer.name,
  );
  assert.equal(answer("ultimate-throws", "high release flick")?.exact, false);
  assert.equal(answer("ultimate-throws", "huck"), undefined);
  for (const [instrument, family] of [
    ["sousaphone", "Brass"],
    ["bass clarinet", "Woodwind"],
    ["viola", "Strings"],
  ]) {
    assert.equal(answer("music-orchestra", instrument)?.answer.name, family);
    assert.equal(answer("music-orchestra", instrument)?.exact, false);
  }
  assert.equal(answer("music-orchestra", "alloy"), undefined);
  assert.equal(answer("ultimate-pass-purpose", "huck")?.answer.name, "Huck");
  assert.equal(answer("geography-un", "Vatican City"), undefined);
  assert.equal(answer("screen-simpsons", "Homer Simpson").answer.depth, 1);
});
test("inferred entities, synonyms and examples use the active bank", () => {
  const match = (guess, names) =>
    matchAnswer(
      guess,
      names.map((name) => ({ name, depth: 1, aliases: [] })),
    );
  for (const [guess, type] of [
    ["8BitDo SFC30", "Games controller"],
    ["iPhone 12 Pro Max", "Mobile phone"],
    ["puffin", "Seabird"],
    ["bass guitar", "Musical instrument"],
  ]) {
    const result = match(guess, [type]);
    assert.equal(result?.answer.name, type, guess);
    assert.equal(result.exact, false, guess);
  }
  assert.equal(match("iPhone 12 Pro Max", ["Games controller"]), undefined);
  assert.equal(match("housing controller", ["Games controller"]), undefined);
  const examples = [
    { name: "Backhand", aliases: [], examples: ["variation"], depth: 1 },
    { name: "Forehand", aliases: ["flick"], examples: ["variation"], depth: 2 },
  ];
  assert.equal(matchAnswer("variation", examples, "names"), undefined);
  assert.equal(matchAnswer("flick", examples, "names").exact, true);
});
test("host installs work on retry and preserve edited questions and round snapshots", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    team = client(f);
  assert.equal((await team("/catalogue")).status, 401);
  assert.equal((await team("/catalogue/ultimate")).status, 401);
  await host("/login", "POST", { password: f.password });
  const room = (await host("/rooms", "POST", { name: "Pack night" })).data;
  const root = "/rooms/" + room.code,
    install = root + "/packs/ultimate",
    qpath = root + "/library/ultimate-throws";
  assert.equal((await team(install, "POST", {})).status, 401);
  assert.equal((await host("/catalogue/missing")).status, 404);
  assert.equal((await host("/catalogue/ultimate")).data.length, 30);
  for (const pack of catalogue.filter((p) => p.id !== "ultimate")) {
    const installed = await host(root + "/packs/" + pack.id, "POST", {});
    const skipped = { general: 5, geography: 1 }[pack.id] ?? 0;
    assert.deepEqual(
      installed.data,
      { added: 30 - skipped, skipped },
      pack.title,
    );
  }
  const adds = await Promise.all([
    host(install, "POST", {}),
    host(install, "POST", {}),
  ]);
  assert.deepEqual(
    adds.map((x) => x.data.added).sort((a, b) => a - b),
    [0, 30],
  );
  await team(root + "/join", "POST", { name: "Red kites" });
  const round = await host(root + "/start", "POST", {
    questionId: "ultimate-throws",
    previous: null,
    seconds: 0,
  });
  assert.equal(round.status, 200);
  const r = root + "/rounds/" + round.data.id;
  const prior = (await host(qpath)).data;
  assert.equal(
    (
      await host(qpath, "PUT", {
        ...prior,
        title: "Our throwing round",
        answers: [{ name: "Club special", aliases: [], depth: 5 }],
      })
    ).status,
    200,
  );
  assert.deepEqual((await host(install, "POST", {})).data, {
    added: 0,
    skipped: 30,
  });
  assert.equal((await host(qpath)).data.title, "Our throwing round");
  const inferred = await team(r + "/answer", "POST", {
    answer: "high release flick",
    revision: 0,
  });
  assert.equal(inferred.status, 422);
  assert.equal(inferred.data.suggestion, "Forehand");
  let live = (await team(root + "/state")).data;
  assert.equal(live.mine, null);
  assert.ok(!JSON.stringify(live).includes("Scoober"));
  assert.equal(
    (await team(r + "/answer", "POST", { answer: "Forehand", revision: 0 }))
      .status,
    200,
  );
  assert.equal(
    (await team(r + "/answer", "POST", { answer: "Hammer", revision: 0 }))
      .status,
    409,
  );
  await host(r + "/lock", "POST", {});
  await host(r + "/reveal", "POST", {});
  assert.equal((await team(root + "/state")).data.round.results[0].points, 10);
});
