import test from "node:test";
import assert from "node:assert/strict";
import {
  catalogue,
  builtInQuestions,
  catalogueQuestions,
  previewPack,
} from "../server/catalogue.ts";
import { validateQuestion, matchAnswer } from "../server/rules.ts";
import { fixture, client } from "./fixture.mjs";

test("published scope, regional spellings, equivalent editions and scarce top scores", () => {
  for (const [id, inputs] of [
    ["general-cheese", ["feta", "paneer", "halloumi"]],
    ["general-pasta", ["fusilli", "orecchiette", "bucatini", "angel hair"]],
    ["general-currencies", ["FRF", "French franc", "Deutsche Mark", "DEM", "ITL"]],
    ["geography-un", ["Deutschland", "日本", "España"]],
    ["general-women-wimbledon", ["Serena Williams", "Venus Williams", "Ash Barty"]],
    ["general-women-world-final", ["USA", "China PR", "England"]],
  ]) {
    const q = builtInQuestions.get(id);
    for (const input of inputs)
      assert.equal(matchAnswer(input, q.answers, q.matching)?.exact, true, id + " " + input);
  }
  for (const q of builtInQuestions.values()) {
    assert.ok(q.answers.filter((a) => a.depth === 5).length <= Math.max(1, Math.floor(q.answers.length / 20)), q.id);
  }
  for (const [id, input] of [["general-cheese", "grated cheese"], ["general-pasta", "matzo ball"]]) {
    const q = builtInQuestions.get(id);
    assert.equal(matchAnswer(input, q.answers, q.matching), undefined);
  }
  for (const [q, equivalent] of [["abba", "music-abba-albums"], ["german-borders", "geography-germany"]])
    assert.deepEqual(builtInQuestions.get(q).answers, builtInQuestions.get(equivalent).answers);
  assert.match(builtInQuestions.get("music-charts-2000").context, /Explicit titles/);
  assert.match(builtInQuestions.get("ultimate-throws").context, /Specialist/);
});

test("a missed team’s last wording can be awarded consistently within its round", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f), first = client(f), second = client(f);
  await host("/login", "POST", { password: f.password });
  const root = "/rooms/" + (await host("/rooms", "POST", { name: "Review" })).data.code;
  for (const [team, name] of [[first, "Divers"], [second, "Swimmers"]])
    await team(root + "/join", "POST", { name });
  const id = (await host(root + "/start", "POST", { questionId: "general-currencies", previous: null, seconds: 0 })).data.id;
  const round = root + "/rounds/" + id;
  for (const answer of ["Lira", "Test unit"]) {
    const reply = await first(round + "/answer", "POST", { answer, revision: 0 });
    assert.equal(reply.status, 422);
    if (answer === "Lira") assert.match(reply.data.error, /specific/);
  }
  assert.ok(!JSON.stringify((await second(root + "/state")).data).includes("Test unit"));
  await host(round + "/lock", "POST", {});
  const state = () => host(root + "/state?view=host").then((reply) => reply.data.round);
  const [one, two] = (await state()).missedTeams;
  assert.equal(one.lastGuess, "Test unit");
  const ruling = { teamId: one.id, previous: 0, name: "Test unit", depth: 3 };
  assert.equal((await host(round + "/judge", "POST", ruling)).status, 422);
  assert.equal((await host(round + "/judge", "POST", { ...ruling, unlisted: true })).status, 200);
  assert.equal((await host(round + "/judge", "POST", { ...ruling, unlisted: true, teamId: two.id, depth: 5 })).status, 200);
  assert.deepEqual((await state()).missedTeams.map((team) => team.depth), [3, 3]);
  assert.equal((await host(round + "/answers?q=Test%20unit")).data.filtered, 1);
  assert.equal((await host(root + "/library/general-currencies/answers?q=Test%20unit")).data.filtered, 0);
  assert.equal((await host(round + "/judge", "POST", { ...ruling, unlisted: true })).status, 409);
});

test("world currencies require a currency name or code and the final accepted answer closes the round", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    first = client(f),
    second = client(f);
  await host("/login", "POST", { password: f.password });
  const root =
    "/rooms/" +
    (await host("/rooms", "POST", { name: "Currency night" })).data.code;
  await first(root + "/join", "POST", { name: "Oysters" });
  await second(root + "/join", "POST", { name: "Turtles" });
  const sample = (await host("/packs/sample")).data;
  await host(root + "/packs", "POST", sample);
  const countries = catalogueQuestions("geography").find(
    (q) => q.id === "geography-un",
  ).answers;
  let previous = null;
  for (const id of ["general-currencies", "sampler-world-currencies"]) {
    const q = (await host(root + "/library/" + id)).data;
    for (const country of countries)
      assert.equal(
        matchAnswer(country.name, q.answers, q.matching),
        undefined,
        country.name,
      );
    const start = await host(root + "/start", "POST", {
      questionId: id,
      previous,
      seconds: 60,
    });
    assert.equal(start.status, 200);
    previous = start.data.id;
    const round = root + "/rounds/" + previous;
    const answer = (team, raw) =>
      team(round + "/answer", "POST", { answer: raw, revision: 0 });
    for (const raw of [
      "Japan",
      "Japanese",
      "New Zealand",
      "United Arab Emirates",
      "Dollar",
    ]) {
      const reply = await answer(first, raw);
      assert.equal(reply.status, 422, raw);
      assert.equal(reply.data.suggestion, undefined, raw);
    }
    const typo = await answer(first, "Japansee Yen");
    assert.equal(typo.status, 422);
    assert.equal(typo.data.suggestion, "Japanese Yen");
    const state = () => host(root + "/state?view=host").then((r) => r.data);
    assert.equal((await state()).received, 0);
    assert.equal((await answer(first, "Yen")).status, 200);
    assert.equal((await state()).round.phase, "open");
    assert.equal((await host(round + "/reveal", "POST", {})).status, 409);
    const final = await Promise.all([
      answer(second, "GBP"),
      answer(second, "GBP"),
    ]);
    assert.ok(final.every((r) => r.status === 200));
    assert.equal((await state()).round.phase, "locked");
    assert.equal((await state()).received, 2);
    assert.equal((await host(round + "/reveal", "POST", {})).status, 200);
    assert.equal((await state()).round.results.length, 2);
  }
});

test("released packs contain distinct, playable questions and scoped source lists", () => {
  const ids = new Set();
  const banks = new Map();
  assert.deepEqual(
    catalogue.map((p) => p.id),
    ["general", "screen", "music", "geography", "animals", "ultimate"],
  );
  for (const pack of catalogue) {
    const questions = catalogueQuestions(pack.id);
    assert.equal(questions.length, pack.count);
    assert.equal(questions.length, pack.count);
    assert.equal(
      questions.reduce((sum, q) => sum + q.answers.length, 0),
      pack.answers,
    );
    assert.equal(new Set(questions.map((q) => q.prompt)).size, pack.count);
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
  assert.equal(ids.size, 182);
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
test("all published categories are ready to play and legacy installs preserve edits and snapshots", async (t) => {
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
  const entries = (await host(root + "/library")).data;
  const grouped = new Map(entries.map((q) => [q.id, q.topic]));
  assert.equal(grouped.size, entries.length);
  assert.equal(grouped.get("general-f1"), "Sport");
  assert.equal(grouped.get("ultimate-throws"), "Sport");
  assert.equal(grouped.get("general-currencies"), "Geography");
  for (const pack of catalogue) {
    for (const q of catalogueQuestions(pack.id))
      assert.equal(grouped.get(q.id), q.topic);
    const installed = await host(root + "/packs/" + pack.id, "POST", {});
    assert.deepEqual(installed.data, { added: 0, skipped: pack.count }, pack.title);
  }
  const adds = await Promise.all([
    host(install, "POST", {}),
    host(install, "POST", {}),
  ]);
  assert.deepEqual(
    adds.map((x) => x.data.added).sort((a, b) => a - b),
    [0, 0],
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
  assert.equal(
    (await host(root + "/state?view=host")).data.round.phase,
    "locked",
  );
  const late = client(f);
  await late(root + "/join", "POST", { name: "Latecomers" });
  assert.equal((await late(root + "/state")).data.eligibleForRound, false);
  assert.equal((await team(root + "/state")).data.round.results, undefined);
  await host(r + "/reveal", "POST", {});
  assert.equal((await team(root + "/state")).data.round.results[0].points, 10);
});
