import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import { chromium, webkit, devices } from "playwright";
import { fixture, client } from "./fixture.mjs";

// Serve the real frontend from a separate origin under a repository subpath.
let backend;
const site = createServer(async (req, res) => {
  try {
    const path = new URL(req.url, "http://localhost").pathname;
    if (!path.startsWith("/society/") || path.startsWith("/society/api/")) {
      res.writeHead(404).end();
      return;
    }
    if (path === "/society/config.js") {
      res
        .writeHead(200, { "Content-Type": "text/javascript" })
        .end("export const apiOrigin=" + JSON.stringify(backend.url));
      return;
    }
    const asset = await fetch(backend.url + path.slice("/society".length));
    res.writeHead(asset.status, {
      "Content-Type": asset.headers.get("content-type"),
    });
    res.end(Buffer.from(await asset.arrayBuffer()));
  } catch {
    res.writeHead(500).end();
  }
});
site.listen(0, "127.0.0.1");
await once(site, "listening");
const origin = "http://127.0.0.1:" + site.address().port,
  pages = origin + "/society/";
try {
  backend = await fixture(undefined, { FRONTEND_URL: pages });
  const denied = await fetch(backend.url + "/api/login", {
    method: "POST",
    headers: {
      origin: "https://unknown.example",
      "content-type": "application/json",
    },
    body: JSON.stringify({ password: backend.password }),
  });
  assert.equal(denied.status, 403);
  assert.equal(denied.headers.get("access-control-allow-origin"), null);
  const admin = client(backend);
  await admin("/login", "POST", { password: backend.password });
  for (const [engine, device] of [
    [chromium, "Pixel 7"],
    [webkit, "iPhone 13"],
  ]) {
    const browser = await engine.launch();
    try {
      const host = await browser.newPage({
        ...devices[device],
        reducedMotion: "reduce",
      });
      const team = await browser.newPage({
        ...devices[device],
        reducedMotion: "reduce",
      });
      const errors = [];
      for (const p of [host, team]) {
        p.setDefaultTimeout(12000);
        p.on("pageerror", (err) => errors.push(err.message));
      }
      await host.goto(pages + "#/host");
      await host.getByLabel("Host password").fill(backend.password);
      await host.getByRole("button", { name: "Sign in" }).click();
      await host.getByLabel("Event name").fill("Pages " + device);
      await host.getByRole("button", { name: "Create room" }).click();
      await host.getByRole("button", { name: "Invite teams" }).click();
      const link = await host.getByLabel("Join link").inputValue();
      assert.ok(link.startsWith(pages + "#/join/"));
      assert.match(
        await host.locator("img.qr").getAttribute("src"),
        /^data:image\/png/,
      );
      const room = "/rooms/" + link.slice(-6);
      await host.getByRole("button", { name: "Close dialog" }).click();
      await team.goto(link);
      await team.getByLabel("Team name").fill("Lanternfish");
      await team.getByRole("button", { name: "Join room" }).click();
      await team.getByText("Waiting for the host").waitFor();
      await team.reload();
      await team.getByText("Waiting for the host").waitFor();
      assert.equal((await team.context().cookies(backend.url)).length, 0);
      assert.equal((await host.context().cookies(backend.url)).length, 0);
      await host.reload();
      await host.getByRole("button", { name: "Invite teams" }).waitFor();
      const start = await admin(room + "/start", "POST", {
        previous: null,
        seconds: 0,
        questionId: "tube-lines",
      });
      assert.equal(start.status, 200);
      const round = room + "/rounds/" + start.data.id;
      const field = team.getByLabel("Your team’s answer");
      await field.fill("Bakerloo");
      await field.evaluate((input) => {
        input.focus();
        input.setSelectionRange(2, 5);
        window.originalAnswer = input;
      });
      const other = client(backend);
      await other(room + "/join", "POST", { name: "Starfish" });
      await team
        .locator(".board")
        .getByText("Starfish", { exact: true })
        .waitFor();
      assert.ok(
        await field.evaluate(
          (input) =>
            input === window.originalAnswer &&
            document.activeElement === input &&
            input.selectionStart === 2 &&
            input.selectionEnd === 5,
        ),
      );
      await team.evaluate(() => {
        const set = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) {
          if (key.startsWith("dive-draft:"))
            throw new DOMException("Storage is full", "QuotaExceededError");
          return set.call(this, key, value);
        };
      });
      await field.fill("Bakerlooo");
      await team.getByRole("button", { name: "Submit answer" }).click();
      await team.getByText("Did you mean: Bakerloo?").waitFor();
      await team.getByRole("button", { name: "Submit Bakerloo" }).click();
      await team.getByText("Answer locked in", { exact: true }).waitFor();
      await admin(round + "/lock", "POST", {});
      await host.getByText("Correct a ruling", { exact: true }).click();
      await host
        .locator(".review-line")
        .filter({ hasText: "Starfish" })
        .getByRole("button", { name: "Correct" })
        .click();
      await host.getByLabel("Score as").selectOption("Victoria");
      await host.getByRole("button", { name: "Save ruling" }).click();
      await host.getByRole("dialog").waitFor({ state: "hidden" });
      await admin(round + "/reveal", "POST", {});
      const result = (await admin(room + "/state?view=host")).data.round;
      assert.equal(
        result.results.find((x) => x.team === "Starfish").points,
        15,
      );
      assert.equal(
        result.results.find((x) => x.team === "Lanternfish").points,
        85,
      );
      const custom = {
        id: "positions",
        title: "Positions",
        topic: "Football positions",
        prompt: "Name a position in association football.",
        matching: "categories",
        answers: ["Goalkeeper", "Defender", "Striker", "Midfielder"].map(
          (name) => ({ name, depth: 2 }),
        ),
      };
      await admin(room + "/packs", "POST", { questions: [custom] });
      const next = await admin(room + "/start", "POST", {
        previous: start.data.id,
        seconds: 0,
        questionId: custom.id,
      });
      assert.equal(next.status, 200);
      let attempts = 0;
      team.on("request", (request) => {
        if (request.url().endsWith("/answer")) attempts++;
      });
      await field.fill("the player in goal");
      await team.waitForResponse((res) => res.url().includes("/state"));
      assert.equal(attempts, 0);
      assert.equal(await team.locator("#answer-feedback").count(), 0);
      await team.getByRole("button", { name: "Submit answer" }).click();
      await team.getByText("Did you mean: Goalkeeper?").waitFor();
      await team.getByRole("button", { name: "Edit answer" }).click();
      assert.equal(await field.inputValue(), "the player in goal");
      assert.equal((await admin(room + "/state?view=host")).data.received, 0);
      await team.getByRole("button", { name: "Submit answer" }).click();
      await team.getByRole("button", { name: "Submit Goalkeeper" }).click();
      await team
        .locator("[data-saved-answer]")
        .getByText("Goalkeeper", { exact: true })
        .waitFor();
      assert.equal((await admin(room + "/state?view=host")).data.received, 1);
      assert.deepEqual(errors, []);
      console.log(
        device +
          " passed split hosting, persistent typing, storage-full correction and missed-team ruling.",
      );
    } finally {
      await browser.close();
    }
  }
} finally {
  await backend?.close();
  const closed = once(site, "close");
  site.close();
  site.closeAllConnections();
  await closed;
}
