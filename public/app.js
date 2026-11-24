import {
  $,
  esc,
  icon,
  paint,
  brand,
  modal,
  closeModal,
  how,
  api,
  button,
  notice,
  connect,
  storage,
  volatileStorage,
  splitHosting,
  drawSpecies,
} from "./ui.js";
import {
  home,
  signin,
  dashboard,
  join,
  roomView,
  review,
  audit,
  results,
} from "./views.js";
import { teamDive, diveDurationMs } from "./dive.js";
import { mountAmbientKrill } from "./ocean.js";
import { points, depthNames, depthLabel } from "./scoring.js";
import { enter, initialiseEffects, playCue } from "./effects.js";

let page = 0,
  route = [],
  state = null,
  tag = "",
  offset = 0,
  pollTimer,
  pollRun = 0,
  detail = null,
  library = [],
  editing = null,
  stopDive,
  refreshTicket = 0,
  sendJob = null;
let lastRoomContent = "";
let previousClock, lastSavedCue;
const motionPreference = matchMedia("(prefers-reduced-motion: reduce)");
const code = () => route[1] ?? "";
const isHost = () => route[0] === "host";
const base = () => "/rooms/" + code();
const endpoint = (suffix) => base() + suffix;
const during = (s) => s === page;
const formValues = (f) => Object.fromEntries(new FormData(f));
const errorBox = (message) =>
  '<div class="narrow panel flow"><p class="kicker">Room access</p><h2>Couldn’t open this page</h2><p>' +
  esc(message) +
  '</p><a class="action-link" href="#/">Back to home</a> ' +
  button("Try again", "reload") +
  "</div>";
const draftKey = () =>
  state?.team && state?.round
    ? "dive-draft:" + code() + ":" + state.team.id + ":" + state.round.id
    : "";
const draft = () => storage(draftKey());
const setDraft = (v) => {
  if (draftKey()) storage(draftKey(), v);
};
function showError(e, form) {
  if (e.network) connect(true);
  const field = $("#modal").open
    ? $("#modal-error")
    : form?.querySelector(".error");
  if (field) field.textContent = e.message;
  else notice(e.message);
}
function updateClock() {
  document.querySelectorAll("[data-deadline]").forEach((el) => {
    const left = Math.max(
      0,
      Math.ceil((Number(el.dataset.deadline) - Date.now() - offset) / 1000),
    );
    const display =
      Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
    const key = state.round.id + ":" + el.dataset.deadline;
    const lastSecond = previousClock?.key === key ? previousClock.left : left;
    const beat =
      left > 0 && left <= 3 && lastSecond === left + 1 && !document.hidden;
    // Keep a small server-clock correction from repeating a tick.
    previousClock = { key, left: Math.min(lastSecond, left) };
    el.classList.toggle("time-low", left <= 5);
    if (beat) {
      playCue("tick");
      const digits = document.createElement("b");
      digits.className = "clock-pulse";
      digits.textContent = display;
      el.lastElementChild.replaceChildren(digits);
    } else if (el.lastElementChild.textContent !== display) {
      el.lastElementChild.textContent = display;
    }
  });
}
function savedFeedback(revision) {
  if (state?.round?.phase !== "open" || !state.team) return;
  const key = draftKey() + ":" + revision;
  if (lastSavedCue === key) return;
  lastSavedCue = key;
  playCue("saved");
  enter($("[data-saved-answer]"));
}
function standingsEntrance() {
  document
    .querySelectorAll(".board li:nth-child(-n+3), .board li.me")
    .forEach((row, i) => enter(row, i * 55));
}
const diveSeenKey = () =>
  "dive-seen:" +
  code() +
  ":" +
  (state.team?.id ?? "host") +
  ":" +
  state.round.id;
function unseenDive() {
  return (
    state?.round?.phase === "revealed" &&
    state.board.length > 0 &&
    !storage(diveSeenKey())
  );
}
function joinLiveDive(audible = false) {
  if (
    !unseenDive() ||
    state.room.finished ||
    document.hidden ||
    $("#modal").open ||
    motionPreference.matches
  )
    return;
  const r = state.round;
  const elapsed = Math.max(0, Date.now() + offset - r.revealedAt);
  if (r.revealedAt && elapsed < diveDurationMs) runDive(r, audible, elapsed);
}
let renderedRound;
function refreshTypingPanel(html) {
  const next = document.createElement("template");
  next.innerHTML = html;
  // A team can be composing a word. Keep the form and its ancestors mounted.
  for (const selector of [
    ".scene-hud strong",
    ".split > aside",
    ".answer-panel .round-meta",
    "#round-history",
  ]) {
    const old = $(selector),
      fresh = next.content.querySelector(selector);
    if (old && fresh && old.innerHTML !== fresh.innerHTML) {
      old.replaceChildren(...fresh.childNodes);
      old.querySelectorAll("[data-tier]").forEach(drawSpecies);
    }
  }
  const feedback = $("#answer-feedback"),
    fresh = next.content.querySelector("#answer-feedback");
  if (!fresh) feedback?.remove();
  else if (!feedback) $("#answer-form").after(fresh);
  else if (feedback.innerHTML !== fresh.innerHTML) feedback.replaceWith(fresh);
}
function renderRoom() {
  const active = document.activeElement;

  const historyOpen = $("#round-history")?.open;
  const roster = $("#team-status");
  const rosterOpen =
    roster?.open && roster.dataset.round === String(state.round?.id);
  const rosterFocused = active === roster?.querySelector("summary");
  const html = roomView(state, isHost(), draft(), unseenDive());
  if (html === lastRoomContent) return;
  lastRoomContent = html;
  const typing =
    renderedRound === draftKey() &&
    $("#answer-form") &&
    state.round?.phase === "open" &&
    !state.mine &&
    !draft()?.pending;
  renderedRound = draftKey();
  if (typing) refreshTypingPanel(html);
  else paint(html);
  updateClock();
  if (historyOpen && $("#round-history")) $("#round-history").open = true;
  const newRoster = $("#team-status");
  if (newRoster && rosterOpen) newRoster.open = true;
  if (newRoster && rosterFocused)
    newRoster.querySelector("summary").focus({ preventScroll: true });
}
async function refresh(force = false, quiet = false) {
  const stamp = page,
    ticket = ++refreshTicket;
  let data;
  try {
    data = await api(
      endpoint("/state") + (isHost() ? "?view=host" : ""),
      "GET",
      null,
      !force && tag ? { "If-None-Match": tag } : {},
    );
  } catch (error) {
    if (!during(stamp) || ticket !== refreshTicket) return;
    throw error;
  }
  if (!during(stamp) || ticket !== refreshTicket) return;
  connect(false);
  if (data.unchanged) {
    joinLiveDive();
    if (draft()?.pending) void sendPending();
    return;
  }
  if (
    stopDive &&
    (state?.round?.id !== data.round?.id ||
      (data.room.finished && !state?.room.finished))
  ) {
    closeModal();
  }
  const previous = state;
  const live = Boolean(previous && !quiet && !document.hidden);
  state = data;
  tag = data.etag;
  offset = data.serverTime - Date.now();
  const d = draft();
  const acknowledged =
    d?.pending &&
    state.mine?.raw === d.pending.text.trim().replace(/\s+/gu, " ");
  if (state.mine && d?.pending)
    setDraft({ text: state.mine.raw, pending: null });
  else if (d?.pending && state.round?.phase !== "open") {
    setDraft({ ...d, pending: null });
    notice("The round closed before your latest answer was received.");
  }
  renderRoom();
  const r = state.round;
  const newPhase =
    previous?.round?.id !== r?.id || previous?.round?.phase !== r?.phase;
  if (live && state.room.finished && !previous.room.finished) {
    playCue("finish");
    document
      .querySelectorAll(".podium > div:nth-child(-n+3)")
      .forEach((row, i) => enter(row, i * 85));
    standingsEntrance();
  } else if (
    live &&
    newPhase &&
    (r?.phase === "open" || r?.phase === "locked")
  ) {
    playCue(r.phase === "open" ? "open" : "lock");
    enter($(".question"));
  }
  if (live && acknowledged && state.mine.revision > d.pending.revision)
    savedFeedback(state.mine.revision);
  if (
    live &&
    newPhase &&
    !state.room.finished &&
    unseenDive() &&
    motionPreference.matches &&
    !$("#modal").open
  ) {
    const result =
      r.results.find((x) => x.teamId === state.team?.id) ?? r.results.at(-1);
    if (result) playCue("dive", result.depth);
    standingsEntrance();
  }
  joinLiveDive(live && newPhase);
  if (draft()?.pending) void sendPending();
}
function schedule(stamp) {
  if (!during(stamp)) return;
  clearTimeout(pollTimer);
  pollTimer = setTimeout(() => pollRoom(stamp), document.hidden ? 4500 : 1500);
}
async function pollRoom(stamp, force = false, quiet = false) {
  if (!during(stamp)) return;
  const run = ++pollRun;
  clearTimeout(pollTimer);
  try {
    await refresh(force, quiet);
  } catch (e) {
    if (!during(stamp) || run !== pollRun) return;
    if (e.status === 404) {
      page++;
      closeModal();
      state = null;
      paint(
        '<section class="narrow panel flow"><h2>This room is no longer available</h2><a class="action-link" href="' +
          (isHost() ? "#/host" : "#/") +
          '">' +
          (isHost() ? "Your rooms" : "Back to home") +
          "</a></section>",
      );
      return;
    }
    if (e.status === 401) {
      if (isHost()) {
        page++;
        state = null;
        closeModal();
        paint(signin());
      } else location.hash = "/join/" + code();
      return;
    }
    if (e.network) connect(true);
    else showError(e);
  }
  if (run === pollRun) schedule(stamp);
}
async function refreshNow() {
  await refresh(true);
}
async function navigate() {
  const stamp = ++page;
  clearTimeout(pollTimer);
  closeModal();
  connect(false);
  state = null;
  tag = "";
  detail = null;
  library = [];
  editing = null;
  lastRoomContent = "";
  previousClock = null;
  route = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  window.scrollTo(0, 0);
  try {
    if (!route[0]) {
      paint(home());
      return;
    }
    if (
      !["host", "join", "play"].includes(route[0]) ||
      (route[1] && !/^[A-Z2-9]{6}$/.test(code()))
    ) {
      paint(errorBox("Check the room link or enter a six-character code."));
      return;
    }
    paint('<p class="panel" role="status">Connecting to the quiz…</p>');
    if (isHost() && !code()) {
      const { rooms } = await api("/rooms");
      if (during(stamp)) paint(dashboard(rooms));
      return;
    }
    if (!code()) {
      paint(home());
      return;
    }
    if (route[0] === "join") {
      const info = await api(endpoint("/info"));
      if (!during(stamp)) return;
      if (info.joined && route[2] !== "recover") {
        location.replace("#/play/" + code());
        return;
      }
      paint(join(code(), info, route[2] === "recover"));
      return;
    }
    await refresh(true);
    schedule(stamp);
  } catch (e) {
    if (!during(stamp)) return;
    if (e.status === 401 && isHost()) paint(signin());
    else if (e.status === 401 && route[0] === "play")
      location.replace("#/join/" + code());
    else paint(errorBox(e.message));
  }
}
function queueAnswer(text, revision) {
  if (
    !state?.team ||
    state.mine ||
    draft()?.pending ||
    state.round?.phase !== "open"
  )
    return;
  const pending = { text, revision };
  setDraft({ text, pending });
  renderRoom();
  if (volatileStorage(draftKey()))
    notice(
      "Phone storage is unavailable. Keep this page open until your answer is saved.",
    );
  return sendPending(pending);
}
async function sendPending(p = draft()?.pending) {
  if (!p || !state?.round || sendJob === draftKey()) return;
  const key = draftKey(),
    stamp = page,
    round = state.round.id;
  sendJob = key;
  try {
    const saved = await api(endpoint("/rounds/" + round + "/answer"), "POST", {
      answer: p.text,
      revision: p.revision,
    });
    if (!during(stamp) || draftKey() !== key) return;
    const latest = draft();
    if (latest?.pending?.text === p.text)
      setDraft({ ...latest, pending: null });
    if (!state.mine || state.mine.revision <= saved.revision) {
      state.mine = {
        raw: saved.raw ?? p.text,
        canonical: saved.canonical,
        revision: saved.revision,
      };
    }
    renderRoom();
    if (saved.revision > p.revision) savedFeedback(saved.revision);
    notice("Answer saved.");
    await refreshNow().catch((err) => {
      if (err.network) connect(true);
    });
  } catch (e) {
    if (!during(stamp) || draftKey() !== key) return;
    if (e.network) {
      connect(true);
      notice("Answer queued. Keep this page open to retry.");
    } else if (e.status === 422) {
      const latest = draft();
      const feedback =
        latest?.text.trim() === p.text
          ? {
              message: e.message,
              suggestion: e.suggestion,
              revision: p.revision,
            }
          : null;
      setDraft({ ...latest, pending: null, feedback });
      renderRoom();
    } else {
      setDraft({ ...draft(), pending: null });
      await refreshNow().catch(() => {});
      renderRoom();
      showError(e, $("#answer-form"));
    }
  } finally {
    if (sendJob === key) sendJob = null;
  }
}
function environmentOfRound(id) {
  return $("#modal").open && detail?.id === id
    ? detail
    : state?.round?.id === id
      ? state.round
      : null;
}
function openHistory(r) {
  detail = r;
  modal(
    "Round " + r.seq + " · " + r.title,
    '<p class="helptext">' +
      esc(r.prompt) +
      "</p>" +
      results(r, state.team?.id) +
      (isHost()
        ? button(
            "Accepted answers",
            "round-answers",
            "secondary",
            'data-round="' + r.id + '" data-from="history"',
          ) +
          '<details class="top-gap"><summary>Correct a ruling</summary>' +
          review(r) +
          "</details>" +
          audit(r)
        : ""),
  );
  document.querySelectorAll("#modal [data-tier]").forEach(drawSpecies);
}
async function historyById(id) {
  const stamp = page;
  const r = await api(
    endpoint("/rounds/" + id) + (isHost() ? "?view=host" : ""),
  );
  if (during(stamp)) openHistory(r);
}
function showJudge(id, key, teamId) {
  const round = environmentOfRound(id);
  const missed = teamId && round?.missedTeams?.find((t) => t.id === teamId);
  const row = missed
    ? { ...missed, raw: missed.name + " · missed submission", count: 1 }
    : round?.review?.find((x) => x.key === key);
  if (!row) return;
  const current = row.depth ? row.canonical : "";
  const control = round.accepted
    ? '<select name="name"><option value="">Miss · 0 points</option>' +
      round.accepted
        .map(
          (a) =>
            '<option value="' +
            esc(a.name) +
            '"' +
            (a.name === current ? " selected" : "") +
            ">" +
            esc(a.name) +
            " · " +
            round.pointValues[a.depth] +
            " pts · " +
            depthLabel(a.depth) +
            "</option>",
        )
        .join("") +
      "</select>"
    : '<input name="name" maxlength="140" list="ruling-options" placeholder="Search accepted answers" value="' +
      esc(current) +
      '"><datalist id="ruling-options"></datalist><small>Leave blank to score a miss.</small>';
  modal(
    "Judge answer",
    '<p class="helptext">“' +
      esc(row.raw) +
      "” · " +
      row.count +
      (row.count === 1 ? " team" : " teams") +
      '</p><form id="judge-form" class="stack top-gap" data-round="' +
      id +
      (missed
        ? '" data-team="' + teamId + '" data-previous="' + missed.previous
        : '" data-key="' + esc(key)) +
      '"><label>Score as' +
      control +
      '</label><button class="primary">Save ruling</button></form>',
  );
  const options = $("#ruling-options");
  if (options) {
    const field = $("#judge-form input");
    let timer;
    field.oninput = () => {
      clearTimeout(timer);
      const term = field.value;
      timer = setTimeout(async () => {
        if (!options.isConnected) return;
        try {
          const part = await api(
            endpoint(
              "/rounds/" + id + "/answers?q=" + encodeURIComponent(term),
            ),
          );
          if (options.isConnected && field.value === term)
            options.replaceChildren(
              ...part.answers.map((a) => new Option(a.name, a.name)),
            );
        } catch (e) {
          if (options.isConnected) showError(e);
        }
      }, 150);
    };
    field.oninput();
  }
}
let pickerMode = "pick";
let pickerPosition = null;
let questionPage = 0;
async function acceptedAnswers(path, scale, back, trigger) {
  const parent = trigger.closest("dialog");
  const first = await api(path);
  if (!trigger.isConnected || (parent && !parent.open)) return;
  modal(
    "Accepted answers",
    '<p class="helptext">' +
      esc(first.prompt) +
      "</p>" +
      coverageNote(first) +
      '<label class="field top-gap">Find an answer<input id="bank-search" maxlength="140" type="search"></label>' +
      '<label class="field">Rarity<select id="rarity-filter"><option value="">All rarities (' +
      first.total +
      ")</option>" +
      depthNames
        .slice(1)
        .map(
          (name, i) =>
            '<option value="' +
            (i + 1) +
            '">' +
            name +
            " (" +
            first.counts[i + 1] +
            ")</option>",
        )
        .join("") +
      '</select></label><ul class="answer-bank list-scroll" id="bank-rows"></ul><div class="bank-pages"><button type="button" id="bank-prev">Previous</button><span id="bank-count" role="status"></span><button type="button" id="bank-next">Next</button></div>' +
      back,
  );
  const rows = $("#bank-rows"),
    search = $("#bank-search"),
    rarity = $("#rarity-filter");
  let current,
    request = 0,
    timer;
  function paintPage(part) {
    current = part;
    rows.innerHTML = part.answers
      .map(
        (a) =>
          '<li data-rarity="' +
          a.depth +
          '" class="tier-' +
          a.depth +
          '"><canvas data-tier="' +
          a.depth +
          '" aria-hidden="true"></canvas><div><strong>' +
          esc(a.name) +
          "</strong><small>" +
          depthNames[a.depth] +
          " · " +
          depthLabel(a.depth) +
          (a.aliases.length
            ? " · Also accepts " + esc(a.aliases.join(", "))
            : "") +
          "</small>" +
          (a.examples?.length
            ? "<details><summary>Suggested examples</summary><small>" +
              esc(a.examples.join(", ")) +
              "</small></details>"
            : "") +
          "</div><b>" +
          scale[a.depth] +
          " pts</b></li>",
      )
      .join("");
    rows.scrollTop = 0;
    rows.querySelectorAll("[data-tier]").forEach(drawSpecies);
    $("#bank-count").textContent = part.filtered
      ? part.offset +
        1 +
        "–" +
        (part.offset + part.answers.length) +
        " of " +
        part.filtered
      : "No matching answers.";
    $("#bank-prev").hidden = part.offset === 0;
    $("#bank-next").hidden = part.offset + part.answers.length >= part.filtered;
  }
  function loadPage(offset, delay = 0) {
    const ticket = ++request;
    clearTimeout(timer);
    if (first.total === first.answers.length && !search.value) {
      const answers = first.answers.filter(
        (a) => !rarity.value || a.depth === Number(rarity.value),
      );
      paintPage({ ...first, answers, filtered: answers.length, offset: 0 });
      return;
    }
    timer = setTimeout(async () => {
      if (!rows.isConnected) return;
      try {
        const part = await api(
          path +
            "?" +
            new URLSearchParams({
              q: search.value,
              depth: rarity.value,
              offset,
            }),
        );
        if (rows.isConnected && ticket === request) paintPage(part);
      } catch (e) {
        if (rows.isConnected && ticket === request) showError(e);
      }
    }, delay);
  }
  search.oninput = () => loadPage(0, 150);
  rarity.onchange = () => loadPage(0);
  $("#bank-prev").onclick = () => loadPage(Math.max(0, current.offset - 40));
  $("#bank-next").onclick = () => loadPage(current.offset + 40);
  paintPage(first);
}
function listEntries(filter = "") {
  const entries = library.filter((q) =>
    (q.title + " " + q.topic + " " + q.prompt)
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  return entries.length
    ? entries
        .slice(questionPage * 50, (questionPage + 1) * 50)
        .map(
          (q) =>
            "<li>" +
            button(
              "<strong>" +
                esc(q.title) +
                "</strong><small>" +
                esc(q.topic) +
                " · " +
                (q.plays ? q.plays + " played" : "Unused") +
                "</small>",
              pickerMode === "pick" ? "configure" : "edit",
              "item-main",
              'data-id="' + esc(q.id) + '"',
            ) +
            button(
              "Answers",
              "bank-answers",
              "text-btn",
              'data-id="' +
                esc(q.id) +
                '" aria-label="Accepted answers for ' +
                esc(q.title) +
                '"',
            ) +
            "</li>",
        )
        .join("") +
        (entries.length > 50
          ? '<li class="bank-pages">' +
            (questionPage ? button("Previous 50", "previous-questions") : "") +
            "<span>Page " +
            (questionPage + 1) +
            " of " +
            Math.ceil(entries.length / 50) +
            "</span>" +
            ((questionPage + 1) * 50 < entries.length
              ? button("Next 50", "next-questions")
              : "") +
            "</li>"
          : "")
    : '<li class="empty">No matching questions.</li>';
}
const formatCount = new Intl.NumberFormat("en-GB");
function coverageNote(question) {
  if (!question.coverage) return "";
  return (
    '<p class="pack-scope">' +
    esc(question.coverage) +
    (question.source
      ? ' <a target="_blank" rel="noopener noreferrer" href="' +
        esc(question.source) +
        '">View source</a>'
      : "") +
    "</p>"
  );
}
async function browsePacks(trigger) {
  const packs = await api("/catalogue");
  if (!trigger.isConnected || !$("#modal").open) return;
  modal(
    "Question packs",
    '<ul class="pack-list" aria-label="Available packs">' +
      packs
        .map(
          (pack) =>
            "<li>" +
            button(
              "<strong>" +
                esc(pack.title) +
                "</strong><small>" +
                pack.count +
                " questions · " +
                formatCount.format(pack.answers) +
                " answers</small><span>" +
                esc(pack.description) +
                "</span>",
              "preview-pack",
              "pack-card",
              'data-id="' + esc(pack.id) + '"',
            ) +
            "</li>",
        )
        .join("") +
      "</ul>" +
      button("Back to questions", "back-to-questions", "text-btn"),
  );
}
async function previewQuestions(trigger) {
  const id = trigger.dataset.id;
  const questions = await api("/catalogue/" + id);
  if (!trigger.isConnected || !$("#modal").open) return;
  modal(
    trigger.querySelector("strong").textContent,
    '<div class="pack-preview"><p class="fine">Tap a question to see its scope and source.</p><ul class="pack-prompts">' +
      questions
        .map(
          (q) =>
            "<li><details><summary><strong>" +
            esc(q.title) +
            "</strong><small>" +
            formatCount.format(q.count) +
            " answers</small></summary><p>" +
            esc(q.prompt) +
            "</p>" +
            coverageNote(q) +
            "</details></li>",
        )
        .join("") +
      '</ul><div class="pack-actions">' +
      button(
        "Add " + questions.length + " questions to room",
        "install-pack",
        "primary",
        'data-id="' + esc(id) + '"',
      ) +
      button("Back to packs", "browse-packs", "text-btn") +
      "</div></div>",
  );
}
function fitQuestionPicker() {
  const viewport = window.visualViewport;
  const dialog = $("#modal");
  if (!viewport || !dialog.open || !$("#question-list", dialog)) return;
  dialog.style.setProperty("--picker-height", viewport.height + "px");
  dialog.style.setProperty(
    "--picker-centre",
    viewport.offsetTop + viewport.height / 2 + "px",
  );
}
async function picker(mode, restore = false) {
  const stamp = page;
  if (!restore || mode !== pickerMode) pickerPosition = null;
  questionPage = pickerPosition?.page ?? 0;
  pickerMode = mode;
  const entries = await api(endpoint("/library"));
  if (!during(stamp)) return;
  library = entries;
  modal(
    mode === "pick" ? "Start a round" : "Question library",
    '<div class="stack question-picker"><label><span>Find a question</span><input class="filter" id="question-filter" placeholder="Search by topic or title"></label><ul class="menu-list" id="question-list" aria-label="Questions">' +
      listEntries(pickerPosition?.filter) +
      "</ul>" +
      '<div class="picker-tools">' +
      button("Browse packs", "browse-packs", "primary") +
      button(icon("add") + " New question", "new") +
      (mode === "library" ? button("Import pack", "import-pack") : "") +
      "</div>" +
      "</div>",
  );
  fitQuestionPicker();
  if (pickerPosition) {
    const list = $("#question-list");
    $("#question-filter").value = pickerPosition.filter;
    const previous = [...list.querySelectorAll("button")].find(
      (el) =>
        el.dataset.id === pickerPosition.id &&
        el.dataset.action === pickerPosition.action,
    );
    previous?.focus({ preventScroll: true });
    list.scrollTop = pickerPosition.scrollTop;
  }
}
function configure(id, seconds = "0", previous = state.round?.id ?? "") {
  const q = library.find((q) => q.id === id);
  void api(endpoint("/library/" + id + "/prepare"), "POST", {}).catch(() => {});
  modal(
    "Start round " + ((state.round?.seq ?? 0) + 1),
    '<form class="stack" id="start-form" data-id="' +
      esc(id) +
      '" data-previous="' +
      esc(previous) +
      '"><p class="kicker">' +
      esc(q.topic) +
      "</p><h3>" +
      esc(q.prompt) +
      "</h3>" +
      (q.plays
        ? '<p class="inline-note">This category has already been played ' +
          q.plays +
          " time(s).</p>"
        : "") +
      '<label>Countdown<select name="seconds"><option value="0">Host locks manually</option><option value="30">30 seconds</option><option value="60">1 minute</option><option value="90">90 seconds</option><option value="120">2 minutes</option><option value="180">3 minutes</option></select></label><button class="primary">Open question</button>' +
      button(
        "Accepted answers",
        "bank-answers",
        "secondary",
        'data-id="' + esc(id) + '" data-from="configure"',
      ) +
      button("Back to questions", "back-to-questions", "text-btn") +
      "</form>",
  );
  $("#start-form [name=seconds]").value = seconds;
}
let editOffset = 0;
const editPageSize = 25;
function readAnswerRows() {
  document.querySelectorAll("#edit-form .editor-row").forEach((row, i) => {
    const previous = editing.answers[editOffset + i];
    const name = $(".answer-name", row).value;
    editing.answers[editOffset + i] = {
      name,
      ...(name === previous.name && previous.wordnetSenses
        ? { wordnetSenses: previous.wordnetSenses }
        : {}),
      depth: Number($(".answer-depth", row).value),
      examples: $(".examples", row)
        .value.split("|")
        .map((v) => v.trim())
        .filter(Boolean),
      aliases: $(".aliases", row)
        .value.split("|")
        .map((v) => v.trim())
        .filter(Boolean),
    };
  });
}
function paintAnswerRows() {
  editOffset = Math.min(
    editOffset,
    Math.max(
      0,
      Math.floor((editing.answers.length - 1) / editPageSize) * editPageSize,
    ),
  );
  const rows = editing.answers.slice(editOffset, editOffset + editPageSize);
  $(".editor-rows").innerHTML = rows.map(answerRow).join("");
  $("#edit-pages").innerHTML =
    editing.answers.length > editPageSize
      ? (editOffset ? button("Previous answers", "edit-prev") : "") +
        "<span>" +
        (editOffset + 1) +
        "–" +
        (editOffset + rows.length) +
        " of " +
        editing.answers.length +
        "</span>" +
        (editOffset + rows.length < editing.answers.length
          ? button("Next answers", "edit-next")
          : "")
      : "";
}
function answerRow(a) {
  return (
    '<div class="editor-row"><input aria-label="Accepted answer" class="answer-name" value="' +
    esc(a.name) +
    '" maxlength="140" placeholder="Answer" required><select aria-label="Answer depth" class="answer-depth">' +
    depthNames
      .slice(1)
      .map(
        (n, i) =>
          '<option value="' +
          (i + 1) +
          '" ' +
          (a.depth === i + 1 ? "selected" : "") +
          ">" +
          n +
          "</option>",
      )
      .join("") +
    '</select><input aria-label="Aliases, separated by vertical bars" class="aliases" value="' +
    esc(a.aliases.join(" | ")) +
    '" placeholder="Aliases (a | b)"><details class="example-editor"><summary>Examples to suggest</summary><input class="examples" aria-label="Examples to suggest, separated by vertical bars" value="' +
    esc((a.examples ?? []).join(" | ")) +
    '" placeholder="Specific examples (a | b)"></details><button type="button" class="remove-answer" data-action="del-row" aria-label="Remove answer">×</button></div>'
  );
}
async function editor(id) {
  const stamp = page;
  const q = id
    ? await api(endpoint("/library/" + id))
    : {
        title: "",
        topic: "",
        prompt: "",
        source: "",
        answers: [{ name: "", depth: 1, aliases: [] }],
      };
  if (!during(stamp)) return;
  editing = q;
  editOffset = 0;
  modal(
    id ? "Edit question" : "New question",
    '<form id="edit-form" class="editor stack"><label>Picker title<input name="title" maxlength="100" required value="' +
      esc(q.title) +
      '"></label><label>Topic<input name="topic" maxlength="60" required value="' +
      esc(q.topic) +
      '"></label><label>Prompt<textarea name="prompt" rows="2" maxlength="400" required>' +
      esc(q.prompt) +
      '</textarea></label><label>Source URL (optional)<input type="url" name="source" value="' +
      esc(q.source) +
      '"></label><label>Coverage (optional)<textarea name="coverage" rows="2" maxlength="900">' +
      esc(q.coverage ?? "") +
      '</textarea></label><label>Answer matching<select name="matching"><option value="categories">Categories · recognise specific examples</option><option value="names">Names and titles · aliases and spelling</option></select></label><div><h3>Accepted answers</h3><p class="fine">Use British display names. Separate alternative names with a vertical bar ( | ).</p></div><div class="editor-rows"></div><div class="bank-pages" id="edit-pages"></div>' +
      button("+ Add answer", "add-row") +
      '<button class="primary">Save question</button></form>',
  );
  $("#edit-form select[name=matching]").value =
    q.matching ?? (id ? "names" : "categories");
  paintAnswerRows();
}
function runDive(r, audible = true, elapsed = 0) {
  if (!state.board.length) return notice("No teams in this round.");
  stopDive?.();
  stopDive = teamDive(r, state.board, state.team?.id, audible, elapsed);
  storage(diveSeenKey(), true);
  renderRoom();
}
$("#modal").addEventListener("close", () => {
  if ($("#modal").open) return;
  if (stopDive && state?.round?.phase === "revealed") standingsEntrance();
  stopDive?.();
  stopDive = null;
  if (unseenDive() && !state.room.finished && !document.hidden)
    void pollRoom(page, false, true);
});
$("#help-button").addEventListener("click", how);
document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.name === "answer" && state?.round) {
    setDraft({ ...draft(), text: t.value, feedback: null });
    $("#answer-feedback")?.remove();
  }
  if (t.id === "question-filter") {
    questionPage = 0;
    $("#question-list").innerHTML = listEntries(t.value);
    $("#question-list").scrollTop = 0;
  }
  if (t.form?.id === "delete-form")
    t.form.querySelector("button.danger").disabled =
      t.value !== t.form.dataset.name;
  if (t.name === "code" || t.name === "transfer")
    t.value = t.value.toUpperCase().replace(/[^A-Z0-9]/g, "");
});
async function action(el) {
  const a = el.dataset.action;
  const id = Number(el.dataset.round);
  const list = el.closest("#question-list");
  if (list)
    pickerPosition = {
      filter: $("#question-filter").value,
      scrollTop: list.scrollTop,
      page: questionPage,
      id: el.dataset.id,
      action: a,
    };
  switch (a) {
    case "previous-questions":
    case "next-questions":
      questionPage += a === "next-questions" ? 1 : -1;
      list.innerHTML = listEntries($("#question-filter").value);
      list.scrollTop = 0;
      list.querySelector("button")?.focus({ preventScroll: true });
      return;
    case "browse-packs":
      return browsePacks(el);
    case "preview-pack":
      return previewQuestions(el);
    case "install-pack": {
      const result = await api(endpoint("/packs/" + el.dataset.id), "POST", {});
      if (el.isConnected && $("#modal").open) {
        await picker(pickerMode);
        notice(
          result.added + " added · " + result.skipped + " already in this room",
        );
      }
      return;
    }
    case "import-pack":
      return modal(
        "Import a question pack",
        '<form id="pack-form" class="stack"><p>Choose a JSON pack from your phone. Each question includes its accepted answers and rarity tiers.</p>' +
          '<label>Question pack<input name="pack" type="file" accept=".json,application/json" required></label><small>Up to 16 MiB per file. Identical questions are skipped.</small>' +
          '<button class="primary">Import questions</button><div><h3>Try the sample pack</h3><p class="fine">Gaming accessories and 162 world currencies.</p></div>' +
          button("Add sample questions", "sample-pack") +
          (splitHosting
            ? button("Download example JSON", "download-sample", "text-btn")
            : '<a class="text-btn" href="/api/packs/sample" download>Download example JSON</a>') +
          button("Back to questions", "back-to-questions", "text-btn") +
          "</form>",
      );
    case "download-sample": {
      const blob = await api("/packs/sample", "GET", null, {}, true);
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "pub-sampler.json";
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 30000);
      return;
    }
    case "sample-pack": {
      const data = await api("/packs/sample");
      if (el.isConnected && $("#modal").open)
        return importQuestions(data, $("#pack-form"));
      return;
    }
    case "confirm-suggestion": {
      const suggestion = draft()?.feedback;
      if (!suggestion?.suggestion || state?.round?.phase !== "open") return;
      return queueAnswer(suggestion.suggestion, suggestion.revision);
    }
    case "edit-answer":
      setDraft({ ...draft(), feedback: null });
      renderRoom();
      $("#answer-form input").focus();
      return;
    case "reload":
      return navigate();
    case "delete-room":
      modal(
        "Delete room?",
        '<p class="helptext">Deleting <strong>' +
          esc(el.dataset.name) +
          "</strong> (" +
          esc(el.dataset.code) +
          ') permanently removes its teams, questions and scores.</p><form id="delete-form" class="stack top-gap" data-code="' +
          esc(el.dataset.code) +
          '" data-name="' +
          esc(el.dataset.name) +
          '"><label>Type the room name to confirm<input name="name" autocomplete="off" spellcheck="false" required></label><div class="actions"><button type="button" class="secondary" data-close>Cancel</button><button class="danger" disabled>Delete room</button></div></form>',
      );
      return;
    case "round-answers": {
      const round = el.dataset.from === "history" ? detail : state.round;
      if (round?.id !== id) return;
      return acceptedAnswers(
        endpoint("/rounds/" + id + "/answers"),
        round.pointValues,
        el.dataset.from === "history"
          ? button(
              "Back to round",
              "history",
              "text-btn",
              'data-round="' + id + '"',
            )
          : "",
        el,
      );
    }
    case "bank-answers": {
      const setup = el.closest("#start-form");
      return acceptedAnswers(
        endpoint("/library/" + el.dataset.id + "/answers"),
        points,
        setup
          ? button(
              "Back to round setup",
              "configure",
              "text-btn",
              'data-id="' +
                esc(el.dataset.id) +
                '" data-seconds="' +
                esc(setup.elements.seconds.value) +
                '" data-previous="' +
                esc(setup.dataset.previous) +
                '"',
            )
          : button("Back to questions", "back-to-questions", "text-btn"),
        el,
      );
    }
    case "logout":
      await api("/logout", "POST", {});
      return navigate();
    case "pick":
    case "library":
      return picker(a);
    case "back-to-questions":
      return picker(pickerMode, true);
    case "new":
      return editor();
    case "edit":
      return editor(el.dataset.id);
    case "configure":
      return configure(el.dataset.id, el.dataset.seconds, el.dataset.previous);
    case "edit-prev":
    case "edit-next":
      readAnswerRows();
      editOffset += a === "edit-next" ? editPageSize : -editPageSize;
      paintAnswerRows();
      $(".editor-row input")?.focus();
      return;
    case "add-row":
      readAnswerRows();
      editing.answers.push({ name: "", depth: 1, aliases: [] });
      editOffset =
        Math.floor((editing.answers.length - 1) / editPageSize) * editPageSize;
      paintAnswerRows();
      $(".editor-row:last-child input").focus();
      return;
    case "del-row":
      readAnswerRows();
      editing.answers.splice(
        editOffset +
          [...document.querySelectorAll(".editor-row")].indexOf(
            el.closest(".editor-row"),
          ),
        1,
      );
      paintAnswerRows();
      return;
    case "replay":
      return runDive(state.round);
    case "dismiss-dive":
      return closeModal();
    case "judge":
      return showJudge(id, el.dataset.key, Number(el.dataset.team));
    case "history":
      return historyById(id);
    case "invite": {
      const stamp = page;
      const url = new URL("#/join/" + code(), location.href).href;
      let qr = "/api" + endpoint("/qr");
      if (splitHosting) {
        const blob = await api(endpoint("/qr"), "GET", null, {}, true);
        qr = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(reader.result);
          reader.onerror = reject;
          reader.readAsDataURL(blob);
        });
        if (!during(stamp)) return;
      }
      modal(
        "Invite teams",
        '<p class="helptext">Scan with a phone or enter this room code.</p><img class="qr" alt="QR code to join this room" src="' +
          esc(qr) +
          '"><strong class="code-display">' +
          code() +
          '</strong><label class="field">Join link<input readonly value="' +
          esc(url) +
          '"></label><div class="actions">' +
          button(icon("copy") + " Copy link", "copy", "primary wide") +
          "</div>",
      );
      return;
    }
    case "copy": {
      const input = $("#modal input");
      try {
        await navigator.clipboard.writeText(input.value);
        notice("Join link copied.");
      } catch {
        input.select();
        notice("Select and copy the join link.");
      }
      return;
    }
    case "transfer":
      modal(
        "Transfer a team",
        '<p class="helptext">Choose the team whose phone needs replacing. Give its private code to that team.</p><form id="transfer-form" class="stack top-gap"><label>Team<select name="teamId">' +
          state.board
            .map(
              (t) =>
                '<option value="' + t.id + '">' + esc(t.name) + "</option>",
            )
            .join("") +
          '</select></label><button class="primary">Generate transfer code</button></form>',
      );
      return;
    case "finish":
      modal(
        "Finish this quiz?",
        '<p class="helptext">This closes the room and publishes the final podium for every team.</p><div class="actions">' +
          button(
            "Finish quiz",
            "confirm-finish",
            "danger",
            'data-previous="' + (state.round?.id ?? "") + '"',
          ) +
          '<button class="secondary" data-close>Keep playing</button></div>',
      );
      return;
    case "confirm-finish":
      await api(endpoint("/finish"), "POST", {
        previous: Number(el.dataset.previous) || null,
      });
      closeModal();
      return refreshNow();
    case "lock":
    case "extend":
    case "reveal": {
      const r = state.round;
      await api(
        endpoint("/rounds/" + r.id + "/" + a),
        "POST",
        a === "extend" ? { previous: r.deadline } : {},
      );
      await refreshNow();
      return;
    }
  }
}
document.addEventListener("click", async (e) => {
  const close = e.target.closest("[data-close]");
  if (close) {
    closeModal();
    return;
  }
  const el = e.target.closest("[data-action]");
  if (!el || el.disabled) return;
  e.preventDefault();
  el.disabled = true;
  try {
    await action(el);
  } catch (err) {
    showError(err);
  } finally {
    if (el.isConnected) el.disabled = false;
  }
});
async function importQuestions(data, form) {
  const stamp = page;
  if (!form?.isConnected || !$("#modal").open) return;
  const result = await api(endpoint("/packs"), "POST", {
    questions: Array.isArray(data) ? data : data?.questions,
  });
  if (during(stamp) && form.isConnected && $("#modal").open) {
    await picker("library");
    notice(
      result.added + " added · " + result.skipped + " already in this room",
    );
  }
}
document.addEventListener("submit", async (e) => {
  const f = e.target;
  if (!(f instanceof HTMLFormElement)) return;
  e.preventDefault();
  if (f.dataset.busy) return;
  const values = formValues(f),
    submit = f.querySelector("button.primary");
  f.dataset.busy = "1";
  if (submit) submit.disabled = true;
  try {
    switch (f.id) {
      case "pack-form": {
        const file = f.elements.pack.files[0];
        if (!file || file.size > 16 * 1024 * 1024)
          throw new Error("Choose a JSON file up to 16 MiB.");
        let data;
        try {
          data = JSON.parse(await file.text());
        } catch {
          throw new Error("This file isn't valid JSON.");
        }
        await importQuestions(data, f);
        break;
      }
      case "delete-form":
        if (values.name !== f.dataset.name) break;
        await api("/rooms/" + f.dataset.code, "DELETE", { name: values.name });
        await navigate();
        notice("Room deleted.");
        break;
      case "code-form":
        location.hash = "/join/" + values.code.toUpperCase().trim();
        break;
      case "join-form": {
        const room = code(),
          key = "dive-join:" + room;
        let retryKey = storage(key);
        if (typeof retryKey !== "string") {
          const bytes = crypto.getRandomValues(new Uint8Array(32));
          retryKey = Array.from(bytes, (b) =>
            b.toString(16).padStart(2, "0"),
          ).join("");
          storage(key, retryKey);
        }
        await api(endpoint("/join"), "POST", { ...values, retryKey });
        storage(key, null);
        if (code() === room) location.replace("#/play/" + room);
        break;
      }
      case "login-form":
        await api("/login", "POST", values);
        await navigate();
        break;
      case "create-form": {
        const out = await api("/rooms", "POST", values);
        location.hash = "/host/" + out.code;
        break;
      }
      case "answer-form": {
        const answer = values.answer.trim();
        if (!answer) throw new Error("Enter an answer.");
        await queueAnswer(answer, state.mine?.revision ?? 0);
        break;
      }
      case "start-form":
        await api(endpoint("/start"), "POST", {
          questionId: f.dataset.id,
          previous: Number(f.dataset.previous) || null,
          seconds: Number(values.seconds),
        });
        closeModal();
        await refreshNow();
        break;
      case "edit-form": {
        readAnswerRows();
        const out = await api(
          endpoint("/library" + (editing.id ? "/" + editing.id : "")),
          editing.id ? "PUT" : "POST",
          { ...values, answers: editing.answers, revision: editing.revision },
        );
        await picker(pickerMode);
        if (pickerMode === "pick") configure(out.id);
        else notice("Question saved.");
        break;
      }
      case "judge-form": {
        const id = Number(f.dataset.round);
        await api(endpoint("/rounds/" + id + "/judge"), "POST", {
          ...(f.dataset.team
            ? {
                teamId: Number(f.dataset.team),
                previous: Number(f.dataset.previous),
              }
            : { key: f.dataset.key }),
          name: values.name,
          ...(values.name ? {} : { depth: 0 }),
        });
        closeModal();
        await refreshNow();
        if (state.round?.id !== id || state.round.phase === "revealed")
          await historyById(id);
        break;
      }
      case "transfer-form": {
        const out = await api(endpoint("/transfer"), "POST", {
          teamId: Number(values.teamId),
        });
        modal(
          "Recovery code",
          '<p class="helptext">On the replacement phone, open room ' +
            code() +
            ' and choose “Playing on a replacement phone?”. This code works once and expires in 10 minutes.</p><strong class="code-display">' +
            esc(out.transfer) +
            '</strong><button class="primary wide" data-close>Done</button>',
        );
        break;
      }
    }
  } catch (err) {
    showError(err, f);
  } finally {
    delete f.dataset.busy;
    if (submit?.isConnected) submit.disabled = false;
  }
});
window.addEventListener("hashchange", navigate);
window.addEventListener("online", () => {
  if (state) void pollRoom(page, true);
  else navigate();
});
window.addEventListener("offline", () => connect(true));
document.addEventListener("visibilitychange", () => {
  previousClock = null;
  if (state && !document.hidden) {
    void pollRoom(page, true, true);
  }
});
setInterval(updateClock, 500);
initialiseEffects();
brand();
mountAmbientKrill($("#ambient-krill"));
window.visualViewport?.addEventListener("resize", fitQuestionPicker);
window.visualViewport?.addEventListener("scroll", fitQuestionPicker);
navigate();
