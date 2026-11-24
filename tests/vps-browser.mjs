import assert from "node:assert/strict";
import { createInterface } from "node:readline";
import { chromium, devices } from "playwright";

const origin = process.argv[2];
const command = createInterface({ input: process.stdin })[
  Symbol.asyncIterator
]();
const browser = await chromium.launch();
try {
  const options = {
    ...devices["Pixel 7"],
    baseURL: origin,
    ignoreHTTPSErrors: true,
    reducedMotion: "reduce",
  };
  const host = await browser.newContext(options);
  const team = await browser.newContext(options);
  async function api(context, path, data) {
    const r = await context.request.fetch(
      "/api" + path,
      data === undefined ? {} : { method: "POST", data },
    );
    assert.ok(r.ok(), path + " " + (await r.text()));
    return r.json();
  }
  await api(host, "/login", { password: "rehearsal" });
  const room =
    "/rooms/" + (await api(host, "/rooms", { name: "Upgrade rehearsal" })).code;
  await api(team, room + "/join", { name: "Seahorses" });
  const round =
    room +
    "/rounds/" +
    (
      await api(host, room + "/start", {
        questionId: "tube-lines",
        seconds: 0,
        previous: null,
      })
    ).id;
  await api(team, round + "/answer", { answer: "Bakerloo", revision: 0 });
  await api(host, round + "/lock", {});
  await api(host, round + "/reveal", {});
  const page = await team.newPage();
  await page.goto("/#/play/" + room.split("/").at(-1));
  await page.waitForFunction(() => globalThis.krillReleaseProbe === "A");
  const getURLs = () =>
    page.evaluate(() => ({
      script: document.querySelector("script[type=module]").src,
      css: document.querySelector("link[rel=stylesheet]").href,
      font: document.querySelector("link[as=font]").href,
      icon: document.querySelector("link[rel=icon]").href,
    }));
  const old = await getURLs();
  const bytes = async (url) => {
    const response = await team.request.get(url);
    assert.equal(response.status(), 200);
    assert.match(response.headers()["cache-control"], /immutable/);
    return response.body();
  };
  const oldSound = new URL("./sounds/saved.wav", old.script).href;
  const original = await bytes(oldSound);
  // Prime the browser's HTTP cache as well as verifying the origin bytes.
  const audio = async (url) =>
    page.evaluate(
      async (u) =>
        Array.from(new Uint8Array(await (await fetch(u)).arrayBuffer())).slice(
          -8,
        ),
      url,
    );
  const oldSample = await audio(oldSound);
  const time = await page.evaluate(() => performance.timeOrigin);
  console.log("READY");
  assert.equal((await command.next()).value, "UPGRADED");
  assert.equal(await page.evaluate(() => performance.timeOrigin), time);
  assert.equal(await page.evaluate(() => globalThis.krillReleaseProbe), "A");
  assert.deepEqual(await bytes(oldSound), original);
  const response = await page.reload();
  assert.equal(response.headers()["cache-control"], "no-store");
  await page.waitForFunction(() => globalThis.krillReleaseProbe === "B");
  const fresh = await getURLs();
  for (const key of Object.keys(old)) assert.notEqual(old[key], fresh[key]);
  assert.notDeepEqual(
    await audio(new URL("./sounds/saved.wav", fresh.script).href),
    oldSample,
  );
  for (const url of [
    fresh.css,
    fresh.font,
    fresh.icon,
    new URL("./ui.js", fresh.script).href,
  ])
    await bytes(url);
  async function verifyRoom() {
    const state = await api(team, room + "/state");
    assert.equal(state.team.name, "Seahorses");
    assert.equal(state.board[0].total, 85);
    assert.equal(
      (await api(host, room + "/state?view=host")).round.phase,
      "revealed",
    );
  }
  await verifyRoom();
  console.log("VERIFIED");
  assert.equal((await command.next()).value, "ROLLED_BACK");
  await page.reload();
  await page.waitForFunction(() => globalThis.krillReleaseProbe === "A");
  await verifyRoom();
  console.log("DONE");
} finally {
  await browser.close();
  process.stdin.destroy();
}
