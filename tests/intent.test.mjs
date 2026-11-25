import test from "node:test";
import assert from "node:assert/strict";
import { SemanticMatcher } from "../server/semantic.ts";
import { matchAnswer, resolveAnswer } from "../server/rules.ts";
import { client, fixture } from "./fixture.mjs";

function question(topic, prompt, names, matching = "categories") {
  return {
    id: "custom",
    title: topic,
    topic,
    prompt,
    matching,
    answers: names.map((name) => ({ name, aliases: [], depth: 2 })),
  };
}
test("short names are indexed by answer identity within each round", () => {
  const players = question(
    "Tennis",
    "Name a tennis player.",
    ["Rafael Nadal", "Roger Federer", "Serena Williams", "Venus Williams"],
    "names",
  );
  const match = (guess) =>
    resolveAnswer(guess, players.answers, players.matching);
  assert.equal(match("Nadal").exact, false);
  assert.equal(match("Nadal").answer.name, "Rafael Nadal");
  assert.equal(match("Williams"), null);
  const edited = players.answers.map((a) => ({
    ...a,
    aliases: a.name === "Venus Williams" ? ["Williams"] : [],
  }));
  assert.equal(resolveAnswer("Williams", edited, "names"), null);
  assert.equal(match("Serrena").answer.name, "Serena Williams");
  assert.equal(match("Serrena").exact, false);
  assert.equal(match("Rafael Williams"), undefined);
  const pitt = question(
    "History",
    "Name a prime minister.",
    ["William Pitt the Younger", "William Pitt the Elder"],
    "names",
  );
  assert.equal(resolveAnswer("Pitt", pitt.answers, "names"), null);
  for (const term of ["Younger", "Pitt Younger"])
    assert.equal(matchAnswer(term, pitt.answers, "names").exact, false);
  for (const term of ["Yonger", "Young"])
    assert.equal(matchAnswer(term, pitt.answers, "names").exact, false);
  assert.equal(
    matchAnswer("Pitt", pitt.answers.slice(0, 1), "names").exact,
    false,
  );
});

test("paraphrase suggestions work on fresh banks with no authored variants", async (t) => {
  const matcher = new SemanticMatcher(":memory:");
  t.after(() => matcher.close());
  const cases = [
    [
      question(
        "Football positions",
        "Name a position in association football.",
        ["Goalkeeper", "Defender", "Striker", "Midfielder"],
      ),
      [
        ["the player in goal", "Goalkeeper"],
        ["a person watching the goal", null],
        ["someone who plays football", null],
      ],
    ],
    [
      question("Everyday devices", "Name a measuring device.", [
        "Thermometer",
        "Barometer",
        "Stopwatch",
      ]),
      [
        ["the thing that measures how hot it is", "Thermometer"],
        ["measuring stuff", null],
      ],
    ],
    [
      question("Accessories", "Name a gaming accessory.", ["Games controller"]),
      [
        ["air traffic controller", null],
        ["housing controller", null],
        ["iPhone 12 Pro Max", null],
      ],
    ],
    [
      question("Creative jobs", "Name a creative profession.", [
        "Photographer",
        "Painter",
        "Architect",
        "Sculptor",
      ]),
      [
        ["someone who takes photographs for a living", "Photographer"],
        ["a visitor to an art gallery", null],
      ],
    ],
    [
      question("Safety equipment", "Name a piece of safety equipment.", [
        "Fire extinguisher",
        "Smoke alarm",
        "Fire blanket",
      ]),
      [
        ["something for putting out small fires", "Fire extinguisher"],
        ["smoke in the sky", null],
      ],
    ],
  ];
  for (const [q, pairs] of cases)
    for (const [raw, want] of pairs) {
      assert.equal(
        matchAnswer(raw, q.answers, q.matching),
        undefined,
        raw + " must exercise inference",
      );
      assert.equal((await matcher.suggest(q, raw)).name ?? null, want, raw);
    }
});
// Kept out of threshold tuning. A missed paraphrase is preferable to offering
// a wrong answer, so measure recall separately from incorrect suggestions.
test("held-out appliance, music and transport guesses", async (t) => {
  const matcher = new SemanticMatcher(":memory:");
  t.after(() => matcher.close());
  const cases = [
    [
      "Household appliances",
      "Name a household appliance.",
      ["Dishwasher", "Washing machine", "Toaster", "Vacuum cleaner"],
      [
        ["the machine that washes the dishes", "Dishwasher"],
        ["the appliance that sucks dust off the carpet", "Vacuum cleaner"],
        ["a carpet in a shop", null],
        ["someone washing up by hand", null],
      ],
    ],
    [
      "Musical instruments",
      "Name a musical instrument.",
      ["Accordion", "Harp", "Flute", "Trombone"],
      [
        ["the brass instrument with a sliding tube", "Trombone"],
        ["the musician performing on stage", null],
        ["a length of copper pipe", null],
      ],
    ],
    [
      "Transport",
      "Name a means of transport.",
      ["Bicycle", "Helicopter", "Ferry", "Tram"],
      [
        ["a boat that carries passengers across a river", "Ferry"],
        ["a river flowing under a bridge", null],
        ["a cyclist waiting for the bus", null],
      ],
    ],
  ];
  let positives = 0;
  for (const [topic, prompt, names, items] of cases) {
    const q = question(topic, prompt, names);
    for (const [raw, want] of items) {
      const answer = (await matcher.suggest(q, raw)).name;
      if (answer) {
        assert.equal(answer, want, raw);
        positives++;
      }
    }
  }
  assert.ok(
    positives >= 3,
    "At least three of the four unseen paraphrases should resolve",
  );
});

test("a team can edit, confirm once, or miss the deadline after a model suggestion", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    first = client(f),
    second = client(f);
  await host("/login", "POST", { password: f.password });
  const room =
    "/rooms/" + (await host("/rooms", "POST", { name: "New pack" })).data.code;
  await first(room + "/join", "POST", { name: "Dolphins" });
  await second(room + "/join", "POST", { name: "Puffins" });
  const q = question(
    "Football positions",
    "Name a position in association football.",
    ["Goalkeeper", "Defender", "Striker", "Midfielder"],
  );
  assert.equal(
    (await host(room + "/packs", "POST", { questions: [q] })).status,
    200,
  );
  const prep = room + "/library/custom/prepare";
  assert.equal((await first(prep, "POST", {})).status, 401);
  assert.equal((await host(prep, "POST", {})).status, 200);
  const round =
    room +
    "/rounds/" +
    (
      await host(room + "/start", "POST", {
        questionId: "custom",
        previous: null,
        seconds: 0,
      })
    ).data.id;
  const submit = (who, answer) =>
    who(round + "/answer", "POST", { answer, revision: 0 });
  const suggestions = await Promise.all([
    submit(first, "the player in goal"),
    submit(second, "the player in goal"),
  ]);
  for (const result of suggestions) {
    assert.equal(result.status, 422);
    assert.equal(result.data.suggestion, "Goalkeeper");
  }
  assert.equal((await host(room + "/state?view=host")).data.received, 0);
  assert.equal(
    (await submit(first, "weather forecast")).data.suggestion,
    undefined,
  );
  assert.equal((await submit(first, "Goalkeeper")).status, 200);
  assert.equal((await submit(first, "Goalkeeper")).status, 200);
  assert.equal((await submit(first, "Defender")).status, 409);
  await host(round + "/lock", "POST", {});
  assert.equal((await submit(second, "Goalkeeper")).status, 409);
  await host(round + "/reveal", "POST", {});
  assert.deepEqual(
    (await host(room + "/state?view=host")).data.board.map((t) => t.total),
    [15, 0],
  );
});
test("in-flight guesses respect concurrent submissions, locks and transfers", async (t) => {
  const f = await fixture();
  t.after(() => f.close());
  const host = client(f),
    a = client(f),
    b = client(f),
    c = client(f);
  await host("/login", "POST", { password: f.password });
  const room =
    "/rooms/" +
    (await host("/rooms", "POST", { name: "Concurrent guesses" })).data.code;
  for (const [team, name] of [
    [a, "First"],
    [b, "Second"],
    [c, "Third"],
  ])
    await team(room + "/join", "POST", { name });
  const bId = (await b(room + "/state")).data.team.id;
  const labels = Array.from(
    { length: 1500 },
    (_, i) => "Society specimen " + (i + 1),
  );
  const q = question("Archive", "Name a society specimen.", labels, "names");
  await host(room + "/packs", "POST", { questions: [q] });
  const round =
    room +
    "/rounds/" +
    (
      await host(room + "/start", "POST", {
        questionId: q.id,
        previous: null,
        seconds: 0,
      })
    ).data.id;
  const submit = (team, answer) =>
    team(round + "/answer", "POST", { answer, revision: 0 });
  const requests = [a, b, c].map((team) =>
    submit(team, "a type of prehistoric creature"),
  );
  await new Promise((done) => setTimeout(done, 100));
  assert.equal((await submit(a, labels[0])).status, 200);
  const { transfer } = (await host(room + "/transfer", "POST", { teamId: bId }))
    .data;
  await client(f)(room + "/join", "POST", { transfer });
  await host(round + "/lock", "POST", {});
  const [saved, moved, closed] = await Promise.all(requests);
  assert.equal(saved.status, 409);
  assert.equal(moved.status, 401);
  assert.equal(closed.status, 409);
  assert.equal((await host(room + "/state?view=host")).data.received, 1);
});

test("lexical matching works with model inference disabled", async (t) => {
  const f = await fixture(undefined, { QUIZ_SEMANTIC: "off" });
  t.after(() => f.close());
  const host = client(f),
    team = client(f);
  await host("/login", "POST", { password: f.password });
  const room =
    "/rooms/" + (await host("/rooms", "POST", { name: "Fallback" })).data.code;
  await team(room + "/join", "POST", { name: "Krill" });
  const round =
    room +
    "/rounds/" +
    (
      await host(room + "/start", "POST", {
        questionId: "tube-lines",
        previous: null,
        seconds: 0,
      })
    ).data.id;
  const submit = (answer) =>
    team(round + "/answer", "POST", { answer, revision: 0 });
  assert.equal((await submit("unrelated daydream")).data.suggestion, undefined);
  assert.equal((await submit("Bakerlooo")).data.suggestion, "Bakerloo");
  assert.equal((await submit("Bakerloo")).status, 200);
});
