import assert from "node:assert/strict";
import {mkdir} from "node:fs/promises";
import { chromium, webkit, devices } from "playwright";
import { fixture, client } from "./fixture.mjs";
const f = await fixture(undefined, { OPENAI_API_KEY: "" });
try {
  for (const [engine, device] of [
    [chromium, "Pixel 7"],
    [webkit, "iPhone 13"],
  ]) {
    const browser = await engine.launch();
    try {
      const p = await browser.newPage({
        ...devices[device],
        reducedMotion: "reduce",
      });
      const errors = [];
      p.on("pageerror", (e) => errors.push(e.message));
      await p.goto(f.url + "/#/host");
      await p.getByLabel("Host password").fill(f.password);
      await p.getByRole("button", { name: "Sign in" }).click();
      await p.getByLabel("Event name").fill(device + " evidence");
      await p.getByRole("button", { name: "Create room" }).click();
      await p.getByRole("button", { name: "Invite teams" }).click();
      const code = (await p.getByLabel("Join link").inputValue()).slice(-6);
      await p.getByRole("button", { name: "Close dialog" }).click();
      const players = Array.from({ length: 3 }, () => client(f));
      await Promise.all(
        players.map((request, i) =>
          request("/rooms/" + code + "/join", "POST", { name: "Player " + i }),
        ),
      );
      await p.getByRole("button", { name: "Start round" }).click();
      await p.getByRole("button", { name: "Generate from a prompt" }).click();
      await p.getByRole("button", { name: "Draft questions from an idea" }).click();
      await p.getByLabel("Only use complete source registers").check();
      await p.getByRole("button", { name: "Surprise me" }).click();
      await p.getByText("There are no published questions to sample yet.").waitFor();
      await p.getByRole("button", { name: "Edit your idea" }).click();
      assert.ok(await p.getByLabel("Only use complete source registers").isChecked());
      await p.getByLabel("Only use complete source registers").uncheck();
      let awaited = false, resumed = false;
      await p.route("**/drafts",async route => {
        const body=route.request().postDataJSON();
        if (!awaited && body.waitForSource && body.idea.startsWith("Name a fish")) {
          awaited = true;
          await route.fulfill({status:202,json:{awaitingEdition:"previous",jobs:[],note:""}});
        } else {
          resumed = awaited;
          await route.continue();
        }
      });
      await p.route("**/drafts/edition",route => route.fulfill({json:{edition:"updated"}}));
      await p.getByLabel("Your idea").fill("Name a fish that is bigger than a pear");
      await p.getByRole("button", { name: "Suggest questions" }).click();
      await p.getByText(/The builder is checking the source/).waitFor();
      await p.getByRole("button", { name: "Preview this round" }).click({ timeout: 90000 });
      await p
        .getByRole("heading", { name: "Question preview" })
        .waitFor({ timeout: 90000 });
      await p.getByRole("button", { name: "Browse answers" }).click();
      await p.getByLabel("Find an answer").fill("Coelacanth");
      assert.ok(awaited && resumed,"a pending source check should show its questions automatically");
      await p
        .locator("#bank-rows")
        .getByText("Coelacanth", { exact: true })
        .first()
        .waitFor();
      await p.getByRole("button", { name: "Back to preview" }).click();
      await p.getByRole("button", { name: "Save to library" }).click();
      await p.getByRole("button", { name: "Open question" }).click();
      await p
        .getByRole("heading", {
          name: "Name a fish that is bigger than a pear",
        })
        .waitFor();
      assert.equal(
        await p.evaluate(
          () => document.documentElement.scrollWidth <= innerWidth,
        ),
        true,
      );
      const path = "/rooms/" + code;
      await p.locator("#help-button").click();
      await p.getByText(/The game checks answers against the round’s sources/).waitFor();
      assert.equal(await p.getByText(/Ask host to review/).count(), 0);
      await p.getByRole("button", { name: "Got it" }).click();
      const current = (await players[0](path + "/state")).data.round;
      const receipts = await Promise.all(
        players.map((request, i) =>
          request(path + "/rounds/" + current.id + "/answer", "POST", {
            answer: ["Crested oarfish", "Coelacanth", "Arapaima"][i],
            revision: 0,
          }),
        ),
      );
      assert.ok(receipts.every(r => r.status === 200), JSON.stringify(receipts));
      await p.setViewportSize({ width: 360, height: 740 });
      await p.getByRole("button", { name: "Reveal answers" }).click();
      await p
        .getByRole("button", { name: /Replay dive|Watch this round.*dive/ })
        .click();
      await p.locator(".fleet-table").waitFor();
      const fits = await p.locator(".lane-answer").evaluateAll(
        (nodes) =>
          nodes.length === 3 &&
          nodes.every((n) => {
            const bounds = n.getBoundingClientRect(),
              cell = n.closest("td").getBoundingClientRect();
            return (
              bounds.left >= cell.left + 2 && bounds.right <= cell.right - 2
            );
          }),
      );
      assert.ok(
        fits,
        "All three answer labels must fit inside their own lane.",
      );
      await mkdir("test-results",{recursive:true});
      await p.screenshot({path:"test-results/knowledge-dive-"+engine.name()+".png"});
      assert.deepEqual(errors, []);
      console.log(
        device +
          " host generated, searched, saved and opened the sourced round.",
      );
    } finally {
      await browser.close();
    }
  }
} finally {
  await f.close();
}
