import assert from "node:assert/strict";
import { mkdir } from "node:fs/promises";
import { chromium, webkit, devices } from "playwright";
import { fixture, client } from "./fixture.mjs";

async function observeEffects(page) {
  await page.addInitScript(() => {
    const probe = (window.effectProbe = { notes: 0, peak: 0, ticks: [] });
    new MutationObserver((records) => {
      for (const record of records)
        for (const node of record.addedNodes) {
          if (node.nodeType === 1 && node.matches(".clock-pulse"))
            probe.ticks.push(node.textContent);
        }
    }).observe(document, { subtree: true, childList: true });
    if (!window.AudioContext && !window.webkitAudioContext) {
      const play = HTMLMediaElement.prototype.play;
      HTMLMediaElement.prototype.play = function () {
        probe.notes++;
        const url = this.src;
        const promise = play.call(this);
        promise.then(
          async () => {
            if (!url.endsWith("/preview.wav")) return;
            const response = await fetch(url);
            const samples = new Int16Array(await response.arrayBuffer(), 44);
            for (const sample of samples)
              probe.peak = Math.max(probe.peak, Math.abs(sample) / 32768);
            probe.mediaPlayed = true;
          },
          () => {},
        );
        return promise;
      };
      return;
    }
    const start = OscillatorNode.prototype.start;
    OscillatorNode.prototype.start = function (...args) {
      probe.notes++;
      return start.apply(this, args);
    };
    // Sample the real output bus before the physical speaker.
    const connect = AudioNode.prototype.connect;
    AudioNode.prototype.connect = function (target, ...args) {
      if (target === this.context.destination && this instanceof GainNode) {
        const meter = this.context.createAnalyser();
        connect.call(this, meter);
        const samples = new Float32Array(meter.fftSize);
        setInterval(() => {
          meter.getFloatTimeDomainData(samples);
          for (const sample of samples)
            probe.peak = Math.max(probe.peak, Math.abs(sample));
        }, 20);
      }
      return connect.call(this, target, ...args);
    };
  });
}

async function checkAmbientMotion(page) {
  const first = page.locator("#ambient-krill canvas").first();
  assert.equal(
    await first.evaluate((el) => getComputedStyle(el).animationName),
    "none",
  );
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.waitForFunction(
    () =>
      getComputedStyle(document.querySelector("#ambient-krill canvas"))
        .animationName === "krill-swim",
  );
  const start = await first.evaluate((el) => getComputedStyle(el).transform);
  await page.waitForFunction(
    (initial) =>
      getComputedStyle(document.querySelector("#ambient-krill canvas"))
        .transform !== initial,
    start,
  );
  await page.emulateMedia({ reducedMotion: "reduce" });
}

async function checkQuestionPicker(host) {
  const original = host.viewportSize();
  const list = host.locator("#question-list");
  const search = host.getByRole("textbox", { name: "Find a question" });
  try {
    const filters = host.locator(".picker-filters summary");
    await filters.click();
    await search.fill("ultimate frisbee");
    assert.ok(await list.locator("[data-action=configure]").count() > 0);
    await host.getByLabel("Hide specialist rounds").check();
    await host.getByText("No matching questions.").waitFor();
    await host.getByLabel("Hide specialist rounds").uncheck();
    await search.fill("explicit titles");
    assert.ok(await list.locator("[data-action=configure]").count() > 0);
    await host.getByLabel("Hide explicit titles").check();
    await host.getByText("No matching questions.").waitFor();
    await host.getByLabel("Hide explicit titles").uncheck();
    await filters.click();
    await host.setViewportSize({ width: 320, height: 280 });
    await list.waitFor();
    await host.waitForFunction(() => {
      const dialog = document.querySelector("#modal");
      return Math.abs(dialog.getBoundingClientRect().height - 256) < 2;
    });
    await search.fill("Music");
    const fits = await list.evaluate((el) => {
      const dialog = el.closest("dialog");
      const bounds = dialog.getBoundingClientRect();
      const footer = dialog
        .querySelector('[data-action="new"]')
        .getBoundingClientRect();
      return (
        dialog.scrollHeight <= dialog.clientHeight + 1 &&
        dialog.scrollWidth <= dialog.clientWidth + 1 &&
        el.clientHeight >= 44 &&
        el.scrollHeight > el.clientHeight &&
        bounds.top >= 0 &&
        footer.bottom <= bounds.bottom &&
        bounds.bottom <= visualViewport.height + 1
      );
    });
    assert.ok(
      fits,
      "Search, scrollable results and New question fit on a short phone screen",
    );
    for (const action of ["configure", "bank-answers"]) {
      const last = list.locator('[data-action="' + action + '"]').last();
      const id = await last.getAttribute("data-id");
      const atBottom = await list.evaluate((el) => {
        el.scrollTop = el.scrollHeight;
        return el.scrollTop;
      });
      assert.ok(atBottom > 0);
      await last.click();
      await host.getByRole("button", { name: "Back to questions" }).click();
      await last.waitFor();
      assert.equal(await search.inputValue(), "Music");
      assert.ok(
        Math.abs((await list.evaluate((el) => el.scrollTop)) - atBottom) <= 2,
      );
      assert.deepEqual(
        await host.evaluate(() => [
          document.activeElement.dataset.id,
          document.activeElement.dataset.action,
        ]),
        [id, action],
      );
    }
    await search.fill("summer olympic cities");
    assert.equal(await list.locator("li").count(), 1);
    assert.equal(await list.evaluate((el) => el.scrollTop), 0);
  } finally {
    await host.setViewportSize(original);
  }
}

async function request(actor, path, data) {
  const response = await actor.request.fetch(f.url + "/api" + path, {
    method: data ? "POST" : "GET",
    data,
  });
  assert.ok(response.ok(), await response.text());
  return response.json();
}

async function interruptedRounds(host, team) {
  const first = await request(host, "/rooms", { name: "Previous room" });
  const second = await request(host, "/rooms", { name: "Next room" });
  const root = "/rooms/" + second.code;
  await host.goto(f.url + "/#/host/" + first.code);
  await host.getByRole("heading", { name: "Previous room" }).waitFor();
  const gate = Promise.withResolvers();
  const held = Promise.withResolvers();
  await host.route(
    "**/rooms/" + first.code + "/state?view=host",
    async (route) => {
      const response = await host.request.get(route.request().url());
      held.resolve();
      await gate.promise;
      await route.fulfill({ response });
    },
    { times: 1 },
  );
  try {
    await Promise.race([
      held.promise,
      host.waitForTimeout(5000).then(() => {
        throw Error("Previous room never polled");
      }),
    ]);
    await host.evaluate((code) => {
      location.hash = "/host/" + code;
    }, second.code);
    await host.getByRole("heading", { name: "Next room" }).waitFor();
  } finally {
    gate.resolve();
  }

  await request(team, root + "/join", { name: "Returning crew" });
  const start = async (previous) =>
    (
      await request(host, root + "/start", {
        questionId: "summer-hosts",
        previous,
        seconds: 0,
      })
    ).id;
  const reveal = async (id) => {
    await request(host, root + "/rounds/" + id + "/lock", {});
    await request(host, root + "/rounds/" + id + "/reveal", {});
  };
  let id = await start(null);
  // Only the new room's own polling can deliver this change to the host.
  await host
    .getByRole("button", { name: "Lock with 1 team outstanding" })
    .waitFor();
  await team.emulateMedia({ reducedMotion: "no-preference" });
  await team.goto(f.url + "/#/play/" + second.code);
  await team.locator("#team-answer").waitFor();
  await team.getByRole("button", { name: "How to play" }).click();
  await reveal(id);
  const watch = team.getByRole("button", { name: "Watch this round’s dive" });
  await team
    .locator('#app [data-action="replay"]')
    .waitFor({ state: "attached" });
  await team.getByRole("button", { name: "Got it" }).click();
  await team
    .getByRole("dialog")
    .getByRole("button", { name: "Show all results" })
    .click();
  await team.getByRole("button", { name: "Replay dive" }).waitFor();
  await team.reload();
  await team.getByRole("button", { name: "Replay dive" }).waitFor();
  assert.equal(await team.locator("dialog[open]").count(), 0);

  id = await start(id);
  await team.locator("#team-answer").waitFor();
  const setHidden = (hidden) =>
    team.evaluate((value) => {
      if (value)
        Object.defineProperty(document, "hidden", {
          value: true,
          configurable: true,
        });
      else delete document.hidden;
      document.dispatchEvent(new Event("visibilitychange"));
    }, hidden);
  await setHidden(true);
  await reveal(id);
  await team.evaluate(() => window.dispatchEvent(new Event("online")));
  await team
    .locator('#app [data-action="replay"]')
    .waitFor({ state: "attached" });
  assert.equal(await team.locator("dialog[open]").count(), 0);
  await setHidden(false);
  await team
    .getByRole("dialog")
    .getByRole("button", { name: "Show all results" })
    .click();
  await team.getByRole("button", { name: "Replay dive" }).waitFor();

  id = await start(id);
  await team.locator("#team-answer").waitFor();
  await setHidden(true);
  await reveal(id);
  await team
    .locator('#app [data-action="replay"]')
    .waitFor({ state: "attached" });
  const { round } = await request(host, root + "/state?view=host");
  await team.waitForFunction((at) => Date.now() >= at + 4500, round.revealedAt);
  await setHidden(false);
  await watch.click();
  await team.getByRole("dialog").getByText("At the surface").waitFor();
  await team.getByRole("button", { name: "Show all results" }).click();
  await team.getByRole("button", { name: "Replay dive" }).waitFor();

  id = await start(id);
  await team.locator("#team-answer").waitFor();
  await team.getByRole("button", { name: "How to play" }).click();
  await reveal(id);
  await team
    .locator('#app [data-action="replay"]')
    .waitFor({ state: "attached" });
  id = await start(id);
  await team.getByRole("button", { name: "Got it" }).click();
  await team.locator("#team-answer").waitFor();
  assert.equal(await team.locator("dialog[open]").count(), 0);
  await setHidden(true);
  await reveal(id);
  await request(host, root + "/finish", { previous: id });
  await setHidden(false);
  await team.getByRole("heading", { name: "Final podium" }).waitFor();
  await watch.click();
  await team
    .getByRole("dialog")
    .getByRole("button", { name: "Show all results" })
    .click();
  await team.getByRole("button", { name: "Replay dive" }).waitFor();
  await team.emulateMedia({ reducedMotion: "reduce" });
}

async function fullRoomReveal(f, host, team) {
  const { code } = await request(host, "/rooms", {
    name: "Full room rehearsal",
  });
  const root = "/rooms/" + code;
  const picker = await request(host, root + "/library");
  const category = picker.find((q) => q.title === "The first 36 elements");
  const bank = await request(host, root + "/library/" + category.id);
  const observers = Array.from({ length: 29 }, () => client(f));
  const lastTeam = "Z".repeat(32);
  await request(team, root + "/join", { name: "My crew" });
  await team.goto(f.url + "/#/play/" + code);
  await team.getByRole("heading", { name: "Waiting for the host" }).waitFor();
  const toggle = team.getByRole("button", { name: "Sound effects" });
  await toggle.click();
  await toggle.click();
  const warmup = await request(host, root + "/start", {
    previous: null,
    questionId: category.id,
    seconds: 0,
  });
  const first = root + "/rounds/" + warmup.id;
  await request(team, first + "/answer", {
    answer: bank.answers[0].name,
    revision: 0,
  });
  await request(host, first + "/lock", {});
  await request(host, first + "/reveal", {});
  await Promise.all(
    observers.map(async (actor, i) => {
      assert.equal(
        (
          await actor(root + "/join", "POST", {
            name: i === 28 ? lastTeam : "Crew " + (i + 2),
          })
        ).status,
        200,
      );
    }),
  );
  const round = await request(host, root + "/start", {
    previous: warmup.id,
    questionId: category.id,
    seconds: 12,
  });
  const r = root + "/rounds/" + round.id;
  await team.getByRole("button", { name: "Dive — submit answer" }).waitFor();
  await team.emulateMedia({ reducedMotion: "no-preference" });
  await request(team, r + "/answer", {
    answer: bank.answers[0].name,
    revision: 0,
  });
  await Promise.all(
    observers.map(async (actor, i) => {
      const answer = i === 28 ? "Needs a ruling" : bank.answers[i + 1].name;
      assert.equal(
        (await actor(r + "/answer", "POST", { answer, revision: 0 })).status,
        i === 28 ? 422 : 200,
      );
    }),
  );
  await host.goto(f.url + "/#/host/" + code);
  await host.getByText("29 / 30 teams submitted").waitFor();
  await host
    .getByRole("button", { name: "Lock with 1 team outstanding" })
    .waitFor();
  await host.getByText("Team status", { exact: true }).click();
  const hostStatus = host.locator("#team-status");
  await hostStatus
    .locator("section")
    .filter({ hasText: "Still thinking" })
    .getByText(lastTeam, { exact: true })
    .waitFor();
  assert.ok(
    await host.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
  await host
    .getByRole("button", { name: "Reveal answers" })
    .waitFor({ timeout: 16000 });
  assert.deepEqual(await team.evaluate(() => window.effectProbe.ticks), [
    "0:03",
    "0:02",
    "0:01",
  ]);
  await hostStatus
    .locator("section")
    .filter({ hasText: "Missed" })
    .getByText(lastTeam, { exact: true })
    .waitFor();
  assert.equal(
    (await client(f)(root + "/join", "POST", { name: "Arrived after lock" }))
      .status,
    200,
  );
  await host
    .locator(".board")
    .getByText("Arrived after lock", { exact: true })
    .waitFor();
  assert.equal(await hostStatus.locator("li").count(), 30);
  await host.waitForFunction(
    () => !document.querySelector('[data-action="reveal"]')?.disabled,
  );
  await team.emulateMedia({ reducedMotion: "no-preference" });
  await request(host, r + "/reveal", {});
  const chart = team.locator(".fleet-dive");
  await chart.getByText("At the surface").waitFor();
  assert.equal(await chart.locator(".diver").count(), 31);
  const before = Number(
    await chart.locator("thead .own-lane [data-team-score]").textContent(),
  );
  assert.ok(before > 0);
  assert.equal(
    await chart
      .locator(".diver")
      .evaluateAll(
        (markers) =>
          new Set(markers.map((m) => m.getBoundingClientRect().top)).size,
      ),
    1,
  );
  const diveSound = team
    .getByRole("dialog")
    .getByRole("button", { name: "Sound effects" });
  await diveSound.click();
  assert.equal(await diveSound.getAttribute("aria-pressed"), "false");
  const mutedNotes = await team.evaluate(() => window.effectProbe.notes);
  await chart.getByText("Diving together").waitFor();
  await chart.getByText("Dive complete").waitFor();
  assert.equal(
    Number(
      await chart.locator("thead .own-lane [data-team-score]").textContent(),
    ),
    before * 2,
  );
  assert.equal(
    await chart
      .locator(".diver")
      .evaluateAll((markers) =>
        markers.every(
          (m) =>
            Math.abs(
              m.offsetTop -
                (m.parentElement.clientHeight * Number(m.dataset.depth)) / 5,
            ) < 2,
        ),
      ),
    true,
  );
  await chart.getByRole("button", { name: "Next teams" }).click();
  await team.waitForFunction(
    () => document.querySelector(".fleet-scroll").scrollLeft > 5,
  );
  assert.equal(await team.evaluate(() => window.effectProbe.notes), mutedNotes);
  await team.screenshot({
    path: "test-results/mobile-descent.png",
    animations: "allow",
  });
  await team.getByRole("button", { name: "Show all results" }).click();
  assert.equal(await team.locator(".reveal-line").count(), 30);
  assert.equal(await team.locator(".board li").count(), 31);
  assert.equal(await team.locator(".reveal-line.mine").count(), 1);
  const miss = team.locator(".reveal-line").filter({ hasText: "Miss" });
  await miss.getByText("0 m", { exact: true }).waitFor();
  await miss.getByText("+0 pts", { exact: true }).waitFor();
  assert.ok(
    await team.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  );
}

const f = await fixture();
await mkdir("test-results", { recursive: true });
try {
  for (const [name, engine, device] of [
    ["chromium", chromium, "Pixel 7"],
    ["webkit", webkit, "iPhone 13"],
  ]) {
    const browser = await engine.launch();
    try {
      const h = await browser.newContext({
        ...devices[device],
        reducedMotion: "reduce",
      });
      const p = await browser.newContext({
        ...devices[device],
        reducedMotion: "reduce",
      });
      const host = await h.newPage(),
        team = await p.newPage();
      await observeEffects(team);
      const errors = [];
      for (const page of [host, team]) {
        page.on("pageerror", (e) => errors.push(e.message));
        page.setDefaultTimeout(10000);
      }
      await host.goto(f.url);
      await host.getByRole("heading", { name: "The Pub Dive" }).waitFor();
      await checkAmbientMotion(host);
      await host.evaluate(() => document.fonts.ready);
      await host.screenshot({
        path: "test-results/" + name + "-home.png",
        fullPage: true,
      });
      await host.getByRole("button", { name: "How to play" }).click();
      await host.getByRole("button", { name: "Got it" }).click();
      await host.getByRole("link", { name: "Host console" }).click();
      await host.getByLabel("Host password").fill(f.password);
      await host.getByRole("button", { name: "Sign in" }).click();
      await host.getByLabel("Event name").fill("Night Dive " + name);
      await host.getByRole("button", { name: "Create room" }).click();
      await host.getByRole("button", { name: "Invite teams" }).click();
      const invite = await host.getByLabel("Join link").inputValue();
      assert.match(invite, /#\/join\/[A-Z2-9]{6}$/);
      assert.ok(
        await host.locator("img.qr").evaluate(async (img) => {
          await img.decode();
          return img.naturalWidth > 0;
        }),
      );
      await host.getByRole("button", { name: "Close dialog" }).click();
      await team.goto(f.url);
      const sound = team.getByRole("button", { name: "Sound effects" });
      assert.equal(await team.evaluate(() => window.effectProbe.notes), 0);
      await sound.click();
      await team.reload();
      assert.equal(await sound.getAttribute("aria-pressed"), "false");
      await sound.click();
      await team.waitForFunction(
        () => window.effectProbe.notes > 0 && window.effectProbe.peak > 0.001,
      );
      if (name === "webkit")
        await team.waitForFunction(
          () =>
            !!window.AudioContext ||
            !!window.webkitAudioContext ||
            window.effectProbe.mediaPlayed,
        );
      await team
        .getByRole("textbox", { name: "Room code" })
        .fill(invite.slice(-6));
      await team.getByRole("button", { name: "Join room" }).click();
      await team.getByLabel("Team name").fill("Lanternfish");
      await team.getByRole("button", { name: "Join room" }).click();
      await team
        .getByRole("heading", { name: "Waiting for the host" })
        .waitFor();
      await host.getByRole("button", { name: "Start round" }).click();
      await checkQuestionPicker(host);
      await host
        .getByPlaceholder("Search by title, topic, region or era")
        .fill("summer olympic cities");
      await host
        .getByRole("button", { name: /Summer Olympic cities/ })
        .first()
        .click();
      await host.getByLabel("Countdown").selectOption("60");
      await host
        .getByRole("button", { name: "Accepted answers", exact: true })
        .click();
      await host.getByLabel("Rarity").selectOption("5");
      assert.ok((await host.locator('[data-rarity="5"]:visible').count()) > 0);
      assert.equal(await host.locator('[data-rarity="1"]:visible').count(), 0);
      await host.getByRole("button", { name: "Back to round setup" }).click();
      assert.equal(await host.getByLabel("Countdown").inputValue(), "60");
      await host.getByRole("button", { name: "Back to questions" }).click();
      await host
        .getByPlaceholder("Search by title, topic, region or era")
        .fill("first 36 elements");
      await host.locator('#question-list [data-action="configure"]').click();
      assert.equal(await host.getByLabel("Countdown").inputValue(), "0");
      await host.getByRole("button", { name: "Back to questions" }).click();
      await host
        .getByPlaceholder("Search by title, topic, region or era")
        .fill("summer olympic cities");
      await host.locator('#question-list [data-action="configure"]').click();
      await host.getByLabel("Countdown").selectOption("60");
      await host
        .getByRole("button", { name: "Accepted answers", exact: true })
        .click();
      await host.getByRole("button", { name: "Back to round setup" }).click();
      await host.getByRole("button", { name: "Open question" }).click();
      await host.getByText("0 / 1 teams submitted").waitFor();
      const secondsLeft = await host
        .locator("[data-deadline]")
        .evaluate((el) => (Number(el.dataset.deadline) - Date.now()) / 1000);
      assert.ok(secondsLeft > 50 && secondsLeft <= 60);
      await host
        .getByRole("button", { name: "Lock with 1 team outstanding" })
        .waitFor();
      await host.getByText("Team status", { exact: true }).click();
      const hostStatus = host.locator("#team-status");
      await hostStatus
        .locator("section")
        .filter({ hasText: "Still thinking" })
        .getByText("Lanternfish", { exact: true })
        .waitFor();
      assert.equal(await team.locator("#team-status").count(), 0);
      await host.locator('[data-action="round-answers"]').click();
      await host.getByLabel("Rarity").selectOption("2");
      await host
        .getByRole("dialog")
        .getByText("Barcelona", { exact: true })
        .waitFor();
      await host.getByRole("button", { name: "Close dialog" }).click();
      await team
        .getByRole("textbox", { name: "Your team’s answer" })
        .fill("Not a city");
      await team.getByRole("button", { name: "Dive — submit answer" }).click();
      await team
        .getByText("We don’t recognise that answer. Try another spelling or ask the host to review it.")
        .waitFor();
      await host.getByText("0 / 1 teams submitted").waitFor();
      await team
        .getByRole("textbox", { name: "Your team’s answer" })
        .fill("Barcelnoa");
      await team.screenshot({
        path: "test-results/" + name + "-question.png",
        fullPage: true,
      });
      await team.getByRole("button", { name: "Dive — submit answer" }).click();
      await team.getByText("Did you mean: Barcelona?").waitFor();
      await team.reload();
      await team.getByRole("button", { name: "Edit answer" }).click();
      assert.equal(
        await team.getByRole("button", { name: "Submit Barcelona" }).count(),
        0,
      );
      await team
        .getByRole("button", { name: "Dive — submit answer", exact: true })
        .click();
      await team.getByText("Did you mean: Barcelona?").waitFor();
      const beforeRetry = await team.evaluate(() => window.effectProbe.notes);
      await p.setOffline(true);
      await team.getByRole("button", { name: "Submit Barcelona" }).click();
      await team.getByText("Awaiting confirmation").waitFor();
      await hostStatus
        .locator("section")
        .filter({ hasText: "Still thinking" })
        .getByText("Lanternfish", { exact: true })
        .waitFor();
      assert.equal(await team.locator("#answer-form").count(), 0);
      // A gateway may lose the response after the database commits the answer.
      // A retry must recover that acknowledgement without scoring twice.
      await team.route(
        "**/rounds/*/answer",
        async (route) => {
          await route.fetch();
          await route.fulfill({
            status: 502,
            contentType: "text/html",
            body: "Temporary gateway issue",
          });
        },
        { times: 1 },
      );
      await p.setOffline(false);
      await team
        .locator("[data-saved-answer]")
        .getByText("Barcelona", { exact: true })
        .waitFor();
      assert.equal(
        await team
          .locator("#answer-form, [data-saved-answer].cue-enter")
          .count(),
        0,
      );
      const savedNotes = await team.evaluate(() => window.effectProbe.notes);
      assert.ok(savedNotes > beforeRetry);
      await team.waitForTimeout(1700);
      assert.equal(
        await team.evaluate(() => window.effectProbe.notes),
        savedNotes,
      );
      await team.reload();
      await team.getByText("Answer locked in", { exact: true }).waitFor();
      assert.equal(await team.locator("#team-answer").count(), 0);
      await hostStatus
        .locator("section")
        .filter({ hasText: "Submitted" })
        .getByText("Lanternfish", { exact: true })
        .waitFor();
      assert.equal(await host.locator('[data-action="lock"]').count(), 0);
      await host.getByRole("button", { name: "Reveal answers" }).click();
      await team.getByText("Barcelona", { exact: true }).waitFor();
      await team
        .locator(".reveal-line.mine")
        .getByText("+15 pts", { exact: true })
        .waitFor();
      await team
        .locator(".reveal-line.mine")
        .getByText("300 m", { exact: true })
        .waitFor();
      await team.screenshot({
        path: "test-results/" + name + "-results.png",
        fullPage: true,
      });
      await team
        .getByRole("button", { name: "Watch this round’s dive" })
        .click();
      await team.getByRole("dialog").getByText("Dive complete").waitFor();
      await team.getByRole("button", { name: "Show all results" }).click();
      await team.getByRole("button", { name: "Replay dive" }).waitFor();
      assert.equal(
        await team.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      await host
        .getByRole("button", { name: "Questions", exact: true })
        .click();
      await host.getByRole("button", { name: /^Sport \d+ questions$/ }).click();
      const category = host.locator("#question-list");
      await category
        .getByRole("button", { name: /Ways to throw a disc/ })
        .first()
        .waitFor();
      await category
        .getByRole("button", { name: /Formula One champions/ })
        .first()
        .waitFor();
      await category.getByRole("button", { name: "All categories" }).click();
      await host.getByRole("button", { name: "Import questions" }).click();
      await host
        .getByLabel("Question pack", { exact: true })
        .setInputFiles("data/pub-sampler.json");
      await host.getByRole("button", { name: "Import questions" }).click();
      await host.getByRole("heading", { name: "Question library" }).waitFor();
      await host
        .getByRole("textbox", { name: "Find a question" })
        .fill("Change from your travels");
      await host
        .getByRole("button", {
          name: "Accepted answers for Change from your travels",
        })
        .click();
      await host.getByText("1–40 of 269", { exact: true }).waitFor();
      await host.getByRole("button", { name: "Next", exact: true }).click();
      await host.getByText("41–80 of 269", { exact: true }).waitFor();
      await host.getByLabel("Find an answer").fill("GBP");
      await host.getByText("British Pound", { exact: true }).waitFor();
      assert.equal(await host.locator("#bank-rows li").count(), 1);
      await host.getByRole("button", { name: "Back to questions" }).click();
      await host.locator('#question-list [data-action="edit"]').click();
      assert.equal(
        await host.getByLabel("Answer matching").inputValue(),
        "full",
      );
      assert.equal(await host.locator(".editor-row").count(), 25);
      await host.getByRole("button", { name: "Next answers" }).click();
      await host.getByText("26–50 of 269", { exact: true }).waitFor();
      await host.getByRole("button", { name: "Save question" }).click();
      await host.getByRole("button", { name: "New question" }).click();
      await host.getByLabel("Picker title").fill("Our society");
      await host.getByLabel("Category", { exact: true }).fill("Society");
      await host.getByLabel("Prompt", { exact: true }).fill("Name a mascot.");
      await host
        .getByRole("textbox", { name: "Accepted answer", exact: true })
        .fill("Krill");
      await host.getByRole("button", { name: "Save question" }).click();
      await host.getByRole("heading", { name: "Question library" }).waitFor();
      await host.getByRole("button", { name: "Close dialog" }).click();
      await host.getByRole("button", { name: "Start round" }).click();
      await host.getByLabel("Find a question").fill("Ways to throw a disc");
      await host.locator('#question-list [data-action="configure"]').click();
      await host.getByRole("button", { name: "Open question" }).click();
      await team.getByLabel("Your team’s answer").fill("high release flick");
      await team.getByRole("button", { name: "Dive — submit answer" }).click();
      await team.getByRole("button", { name: "Submit Forehand" }).click();
      await team
        .locator("[data-saved-answer]")
        .getByText("Forehand", { exact: true })
        .waitFor();
      await host.getByText("1 / 1 teams submitted").waitFor();
      await host.getByRole("button", { name: "Reveal answers" }).click();
      await team
        .locator(".reveal-line.mine")
        .getByText("Forehand", { exact: true })
        .waitFor();
      await host.getByRole("button", { name: "Finish quiz" }).click();
      await host
        .getByRole("dialog")
        .getByRole("button", { name: "Finish quiz" })
        .click();
      await team.getByRole("heading", { name: "Final podium" }).waitFor();
      await team.getByText("Round history (2)").click();
      await team.getByRole("button", { name: /Round 1 · Summer/ }).click();
      await team
        .getByRole("dialog")
        .getByText("Barcelona", { exact: true })
        .waitFor();
      await host.getByRole("link", { name: "Host console" }).click();
      await host
        .getByRole("button", { name: "Delete room Night Dive " + name })
        .click();
      const confirm = host
        .getByRole("dialog")
        .getByRole("button", { name: "Delete room", exact: true });
      await host
        .getByLabel("Type the room name to confirm")
        .fill("night dive " + name);
      assert.equal(await confirm.isDisabled(), true);
      await host
        .getByLabel("Type the room name to confirm")
        .fill("Night Dive " + name);
      await confirm.click();
      await host.getByText("Room deleted.", { exact: true }).waitFor();
      await interruptedRounds(host, team);
      assert.deepEqual(errors, []);
      console.log(name + " mobile flow passed");
      if (name === "chromium") {
        const desktop = await browser.newPage({
          viewport: { width: 1440, height: 960 },
          deviceScaleFactor: 1,
        });
        await desktop.goto(f.url);
        await desktop.getByRole("heading", { name: "The Pub Dive" }).waitFor();
        await desktop.evaluate(() => document.fonts.ready);
        await desktop.waitForTimeout(120);
        await desktop.screenshot({ path: "test-results/desktop-home.png" });
      }
      await fullRoomReveal(f, host, team);
      assert.deepEqual(errors, []);
      console.log(
        name +
          " 30-team host roster, timed lock, dive and phone standings passed",
      );
    } finally {
      await browser.close();
    }
  }
} finally {
  await f.close();
}
