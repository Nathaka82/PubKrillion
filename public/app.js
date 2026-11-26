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
      if (feedback && !$("#modal").open && document.activeElement === document.body) {
        const target = feedback.suggestion ? $("#answer-feedback") : $("#team-answer");
        target?.focus({ preventScroll: true });
        target?.scrollIntoView({ block: "nearest" });
      }
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
          (r.sourceJudged ? "" : '<details class="top-gap"><summary>Correct a ruling</summary>' +
          review(r) +
          "</details>") +
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
    ? { ...missed, raw: missed.name + (missed.lastGuess ? " · “" + missed.lastGuess + "”" : " · missed submission"), count: 1 }
    : round?.review?.find((x) => x.key === key);
  if (!row) return;
  const current = row.depth ? row.canonical : "";
  const option = (answer) => new Option(
    answer.name + " · " + round.pointValues[answer.depth] + " pts",
    answer.name,
    false,
    answer.name === current,
  );
  modal(
    "Judge answer",
    '<p class="helptext">“' +
      esc(row.raw) +
      "” · " +
      row.count +
      (row.count === 1 ? " team" : " teams") +
      "</p>" + (missed?.reviewEvidence ? JSON.parse(missed.reviewEvidence).map(e=>'<p><a target="_blank" rel="noopener noreferrer" href="'+esc(e.url)+'">'+esc(e.label)+'</a></p>').join("") : "") +
    '<form id="judge-form" class="stack top-gap" data-round="' +
      id +
      (missed
        ? '" data-team="' + teamId + '" data-previous="' + missed.previous
        : '" data-key="' + esc(key)) +
      '"><label><span id="ruling-mode-label">Ruling</span><select name="ruling" aria-labelledby="ruling-mode-label"><option value="listed">Use a listed answer</option><option value="unlisted">Award an unlisted answer</option><option value="miss">Miss · 0 points</option></select></label>' +
      '<div class="stack" id="listed-ruling">' +
      (round.accepted ? "" : '<label>Search answers or aliases<input type="search" id="ruling-search" maxlength="140"></label>') +
      '<label><span id="ruling-name-label">Score as</span><select required name="name" aria-labelledby="ruling-name-label"' + (round.accepted ? "" : ' size="5"') + '></select></label>' +
      (round.accepted ? "" : '<div class="bank-pages"><button type="button" id="ruling-prev">Previous</button><span id="ruling-count" role="status"></span><button type="button" id="ruling-next">Next</button></div>') +
      '</div><div class="stack" id="unlisted-ruling" hidden><label>Unlisted answer<input name="newName" maxlength="140" required disabled></label>' +
      '<label>Rarity for this round<select name="newDepth" disabled>' +
      round.pointValues.map((score, i) => i && (!round.unlistedDepth || i === round.unlistedDepth) ? '<option value="'+i+'">'+depthNames[i]+' · '+score+' points</option>' : "").join("") +
      '</select></label><p class="fine">Teams with the same submitted guess receive the same ruling.</p></div><button class="primary">Save ruling</button></form>',
  );
  const form = $("#judge-form");
  const field = form.elements.namedItem("name");
  form.elements.newName.value = missed?.lastGuess ?? "";
  const mode = form.elements.ruling;
  mode.onchange = () => {
    $("#listed-ruling").hidden = field.disabled = mode.value !== "listed";
    $("#unlisted-ruling").hidden = form.elements.newName.disabled =
      form.elements.newDepth.disabled = mode.value !== "unlisted";
  };
  if (round.accepted) {
    field.replaceChildren(new Option("Choose an answer", ""), ...round.accepted.map(option));
    field.value = current;
    return;
  }
  const search = $("#ruling-search"), count = $("#ruling-count");
  const prev = $("#ruling-prev"), next = $("#ruling-next");
  let offset = 0, ticket = 0, timer;
  function load(start, delay = 0) {
    const request = ++ticket;
    clearTimeout(timer);
    field.replaceChildren();
    prev.disabled = next.disabled = true;
    count.textContent = "Searching…";
    timer = setTimeout(async () => {
      try {
        const params = new URLSearchParams({ q: search.value, offset: start });
        const part = await api(endpoint("/rounds/" + id + "/answers?" + params));
        if (!field.isConnected || ticket !== request) return;
        field.replaceChildren(...part.answers.map(option));
        if (!part.answers.some((a) => a.name === current)) field.selectedIndex = -1;
        offset = part.offset;
        count.textContent = part.filtered ? (offset + 1) + "–" + (offset + part.answers.length) + " of " + part.filtered : "No matching answers";
        prev.disabled = !offset;
        next.disabled = offset + part.answers.length >= part.filtered;
      } catch (e) {
        if (field.isConnected && request === ticket) {
          count.textContent = "Search unavailable. Edit your search to retry.";
          showError(e);
        }
      }
    }, delay);
  }
  search.value = current;
  search.oninput = () => load(0, 150);
  prev.onclick = () => load(offset - 40);
  next.onclick = () => load(offset + 40);
  load(0);
}
let pickerMode = "pick";
let pickerPosition = null;
let questionPage = 0;
let pickerCategory = "";
let omitSpecialist = false, omitExplicit = false;
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
    const spellings = names => names.map(name => "<bdi>" + esc(name) + "</bdi>").join(", ");
    rows.innerHTML = part.answers
      .map(a => {
        const longList = a.aliases.length > 4 || a.aliases.join(", ").length > 100;
        const sharedTitle = a.sharedNames?.find(shared => shared.name === a.name);
        return (
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
          (sharedTitle ? " · This name alone scores " + scale[sharedTitle.depth] + " pts" : "") +
          (a.needsDetail?.includes(a.name) ? " · This name alone is ambiguous" : "") +
          (a.aliases.length && !longList
            ? " · Also accepts " + spellings(a.aliases)
            : "") +
          "</small>" +
          (longList ? "<details><summary>Also accepts " + a.aliases.length + " alternate " +
            (a.aliases.length === 1 ? "name" : "names") + "</summary><p>" + spellings(a.aliases) + "</p></details>" : "") +
          (a.needsDetail?.length || a.sharedNames?.length
            ? "<details><summary>Other recorded names</summary>" +
              (a.needsDetail?.length ? "<p>Use a more specific name for " + spellings(a.needsDetail) + ".</p>" : "") +
              (a.sharedNames || []).map(shared => "<p><bdi>" + esc(shared.name) + "</bdi> identifies multiple eligible answers and scores " +
                scale[shared.depth] + " pts.</p>").join("") + "</details>" : "") +
          evidenceLinks(a.evidence) +
          (a.examples?.length
            ? "<details><summary>Suggested examples</summary><small>" +
              esc(a.examples.join(", ")) +
              "</small></details>"
            : "") +
          "</div><b>" +
          scale[a.depth] +
          " pts</b></li>");
      })
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
  const query = filter.trim().toLocaleLowerCase("en-GB");
  const available = library.filter((q) => {
    const notes = (q.context ?? "").split(" · ");
    return !(omitSpecialist && notes.includes("Specialist")) &&
      !(omitExplicit && notes.includes("Explicit titles"));
  });
  if (!pickerCategory && !query) {
    const counts = new Map();
    for (const q of available)
      counts.set(q.topic, (counts.get(q.topic) ?? 0) + 1);
    return [...counts]
      .sort(([a], [b]) => {
        if (a === b) return 0;
        if (a === "General Knowledge") return -1;
        if (b === "General Knowledge") return 1;
        return a.localeCompare(b, "en-GB");
      })
      .map(
        ([topic, count]) =>
          "<li>" +
          button(
            "<strong>" +
              esc(topic) +
              "</strong><small>" +
              count +
              (count === 1 ? " question" : " questions") +
              "</small>",
            "choose-category",
            "item-main",
            'data-topic="' + esc(topic) + '"',
          ) +
          "</li>",
      )
      .join("");
  }
  const entries = available
    .filter(
      (q) =>
        (!pickerCategory || q.topic === pickerCategory) &&
        [q.title, q.topic, q.prompt, q.context].join(" ")
          .toLocaleLowerCase("en-GB")
          .includes(query),
    )
    .sort(
      (a, b) => a.plays - b.plays || a.title.localeCompare(b.title, "en-GB"),
    );
  const header = pickerCategory
    ? '<li class="category-heading"><strong>' +
      esc(pickerCategory) +
      "</strong>" +
      button("All categories", "all-categories", "text-btn") +
      "</li>"
    : "";
  return (
    header +
    (entries.length
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
                  (q.context ? esc(q.context) + " · " : "") +
                  (q.plays ? q.plays + " played" : "Unused") +
                  "</small>",
                pickerMode === "pick" ? "configure" : q.generated ? "regenerate" : "edit",
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
      : '<li class="empty">No matching questions.</li>')
  );
}
function evidenceLinks(evidence) {
  if (!evidence?.length) return "";
  const items = new Set();
  const link = (url, label) => '<li><a target="_blank" rel="noopener noreferrer" href="' + esc(url) + '">' + label + '</a></li>';
  for (const record of evidence) {
    if (record.readershipItems?.length && record.url) {
      items.add(link(record.url, "Wikimedia readership over 52 weeks"));
      for (const entry of record.readershipItems) {
        items.add(link(entry.url, esc(entry.item) + " · " + Number(entry.views).toLocaleString() + " page views"));
      }
      continue;
    }
    if (record.relationshipLabel && record.targetLabel && record.url) {
      items.add(link(record.url, esc(record.relationshipLabel) + " · <bdi>" + esc(record.targetLabel) + "</bdi>"));
      continue;
    }
    if (record.nameProperty && record.url) {
      items.add(link(record.url, esc(record.nameProperty) + " · " + '<bdi lang="' + esc(record.language) + '">' + esc(record.name) + '</bdi>'));
      continue;
    }
    if (record.namesChecked && record.url) {
      items.add(link(record.url, "Names and translations recorded on Wikidata"));
      continue;
    }
    if (record.qualifiedNames?.length && record.url) {
      items.add(link(record.url, "Archived titles with verified qualifiers · " +
        record.qualifiedNames.map(name => "<bdi>" + esc(name) + "</bdi>").join(", ")));
      continue;
    }
    if (record.dates?.length) {
      for (const claim of record.dates) {
        const value = claim.value;
        let display = value;
        if (/^\d{4}-\d{2}(?:-\d{2})?$/.test(value)) {
          const exactDay = value.length === 10;
          const instant = new Date((exactDay ? value : value + "-01") + "T00:00:00Z");
          if (Number.isFinite(instant.getTime())) {
            display = instant.toLocaleDateString(undefined, {
              year: "numeric", month: "long", ...(exactDay ? { day: "numeric" } : {}), timeZone: "UTC",
            });
          }
        }
        items.add(link(claim.url, esc(record.relation || "Recorded date") + " · " +
          '<time datetime="' + esc(value) + '">' + esc(display) + '</time>'));
      }
      continue;
    }
    if (!record.url) continue;
    const unit = {length:"cm",mass:"g",year:"years"}[record.property];
    const label = unit ? record.basis+" "+record.low+(record.high!==record.low?"–"+record.high:"")+" "+unit : record.source;
    const readable = label?.startsWith("wikidata-query-") ? "Wikidata source record" : label;
    items.add(link(record.url,esc(readable || "Source record")));
  }
  return items.size ? '<details><summary>Evidence</summary><ul>'+[...items].join("")+'</ul></details>' : "";
}
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
let lastResearch;
function generateForm(prompt = "") {
  modal("Generate a question",
    '<form class="stack" id="generate-form"><label>Question for players<textarea name="prompt" maxlength="450" required placeholder="Name a film directed by Akira Kurosawa">'+esc(prompt)+'</textarea></label>'+
    '<p>Ask for a subject, or narrow it with a relationship such as “with publisher Nintendo”.</p>'+
    '<label><input type="checkbox" name="research"> Check live Wikidata for updates</label>'+
    '<button class="primary">Preview answers</button>'+
    button("Draft questions from an idea", "draft-idea")+
    button("Back to questions","back-to-questions","text-btn")+'</form>');
}

function ideaForm(idea, completeOnly = false) {
  modal("Draft questions from an idea",
    '<form class="stack" id="idea-form"><label>Your idea<textarea name="idea" maxlength="450" required placeholder="Japanese cinema for a mixed-age table">'+esc(idea)+'</textarea></label>'+
    '<label><input type="checkbox" name="completeOnly"'+(completeOnly ? ' checked' : '')+'> Only use complete source registers</label>'+
    (state?.questionDrafts ? '<label><input type="checkbox" name="useModel"> Ask OpenAI for wording if the catalogue has no match</label>' : '')+
    '<button class="primary">Suggest questions</button><button type="submit" value="surprise" formnovalidate>Surprise me</button>'+button("Enter a question directly", "generate", "text-btn")+'</form>');
}

async function suggestQuestions(form, surprise) {
  const idea = form.elements.idea.value;
  const completeOnly = form.elements.completeOnly.checked;
  const useModel = !!form.elements.useModel?.checked;
  const dialog = $("#modal");
  const current = () => form.isConnected && dialog.open && idea === form.elements.idea.value &&
    completeOnly === form.elements.completeOnly.checked && useModel === !!form.elements.useModel?.checked;
  const status = document.createElement("p");
  status.setAttribute("role", "status");
  status.textContent = "Looking for questions in the sources…";
  form.append(status);
  try {
    const request = { idea, completeOnly, surprise, prepareOnSelect: true, waitForSource: true, useModel };
    let drafts = await api(endpoint("/drafts"), "POST", request);
    while (drafts.awaitingEdition && current()) {
      status.textContent = "The builder is checking the source. Questions will appear here when the result is published.";
      await new Promise(resolve => {
        function done() {
          clearTimeout(timer);
          form.removeEventListener("input",done);
          dialog.removeEventListener("close",done);
          resolve();
        }
        const timer = setTimeout(done,5000);
        form.addEventListener("input",done,{once:true});
        dialog.addEventListener("close",done,{once:true});
      });
      if (!current()) return;
      const { edition } = await api(endpoint("/drafts/edition"));
      if (current() && edition && edition !== drafts.awaitingEdition)
        drafts = await api(endpoint("/drafts"),"POST",request);
    }
    if (!current()) return;
    const { jobs, note } = drafts;
    modal("Choose a question", '<div class="stack" id="draft-options">'+
      (note ? '<p role="status">'+esc(note)+'</p>' : '')+
      jobs.map(j => '<section class="draft-card" data-job="'+esc(j.id)+'"><h3>'+esc(j.prompt)+'</h3>'+
        [...new Set(Array.from(j.prompt.matchAll(/\((Q[1-9]\d{0,11})\)/g),match=>match[1]))]
          .map(id=>'<p><a target="_blank" rel="noopener noreferrer" href="https://www.wikidata.org/wiki/'+id+'">View Wikidata entry '+id+'</a></p>').join("")+
        (j.records ? '<p>'+j.records.toLocaleString()+(j.complete ? ' entries in the complete register' : ' matching records in the source edition')+'</p>' : '')+
        (j.deferred ? button("Preview this round", "prepare-draft", "primary") :
          '<p data-progress role="status">Preparing playable answers…</p>')+'</section>').join("")+
      button("Edit your idea", "draft-idea", "text-btn", 'data-idea="'+esc(idea)+'" data-complete="'+completeOnly+'"')+'</div>');
    const list = $("#draft-options");
    const pending = new Map(jobs.filter(j => !j.deferred).map(j => [j.id, j]));
    while (pending.size && list.isConnected && $("#modal").open) {
      await Promise.all([...pending.keys()].map(async id => {
        const result = await api(endpoint("/generate/"+id));
        if (!list.isConnected || !$("#modal").open) return;
        const card = list.querySelector('[data-job="'+CSS.escape(id)+'"]');
        const info = card.querySelector("[data-progress]");
        if (result.status === "ready") {
          const q = result.preview;
          info.textContent = q.answerCount.toLocaleString()+" source-backed answers"+
            (q.examples?.length ? " · "+q.examples.join(", ") : "");
          const open = document.createElement("button");
          open.className = "primary";
          open.textContent = "Preview this round";
          open.onclick = () => previewResearch(id, q);
          card.append(open);
        } else if (result.status === "waiting_source") {
          info.textContent = result.message;
          return;
        } else if (result.status === "choose_topic") {
          info.textContent = result.message;
          const select = document.createElement("button");
          select.textContent = "See matching questions";
          select.onclick = () => { ideaForm(result.prompt); $("#idea-form").requestSubmit(); };
          card.append(select);
        } else if (result.status === "failed") {
          info.textContent = result.message;
        } else return;
        pending.delete(id);
      }));
      if (pending.size) await new Promise(done => setTimeout(done, 800));
    }
  } finally {
    status.remove();
  }
}
function previewResearch(job, q) {
  lastResearch = {job,q};
  const readershipSource = q.readership
    ? ' <a href="https://qrank.toolforge.org/" target="_blank" rel="noopener noreferrer">How Wikimedia readership is measured</a>.'
    : '';
  modal("Question preview", '<div class="stack"><h3>'+esc(q.prompt)+'</h3><p>'+esc(q.interpretation)+'</p><p><strong>'+
    q.answerCount.toLocaleString()+' verified answers</strong> · '+esc(q.coverage)+'</p><p>'+
    q.gaps.missing.toLocaleString()+' candidates lack supporting evidence. '+q.gaps.borderline.toLocaleString()+' are borderline.</p>'+
    (q.sourceJudged ? '<p>'+ (q.observedTeams ? 'Rarity calibrated with '+q.observedTeams.toLocaleString()+' verified first choices.' : 'Rarity uses source estimates until enough teams have played.')+' '+q.counts[5].toLocaleString()+' answers earn the top score.</p>' : '')+
    '<details><summary>Sources and scoring</summary><ul>'+q.sources.map(s=>'<li><a target="_blank" rel="noopener noreferrer" href="'+esc(s.url)+'">'+esc(s.label || s.id)+'</a> · '+esc(s.revision.slice(0,14))+' · loaded '+esc(new Date(s.checked*1000).toLocaleDateString())+'</li>').join("")+'</ul><p>'+esc(q.scoring || "Rarity estimates are fixed when the round opens. The top score requires evidence of unusually low recall.")+readershipSource+'</p></details>'+
    button("Browse answers","preview-members")+button("Save to library","save-generated","primary")+
    (q.plan?.worldSource ? button("Refresh from Wikidata", "refresh-research") : "")+
    button("Edit prompt","revise-prompt","text-btn")+'</div>');
}
async function generate(f, prompt = f.elements.prompt.value, research = f.elements.research.checked) {
  return awaitPreview(f, () => api(endpoint("/generate"), "POST", {prompt, research}));
}
async function awaitPreview(f, start) {
  const status=document.createElement("p");
  status.setAttribute("role","status");
  status.textContent="Checking sources and compiling answers…";
  f.append(status);
  try {
    const job=await start();
    while (f.isConnected && $("#modal").open) {
      const result=await api(endpoint("/generate/"+job.id));
      if(!f.isConnected || !$("#modal").open) return;
      if(result.status==="ready") {
        const preview=result.preview;
        return previewResearch(job.id,result.worldNotice ? {...preview,coverage:preview.coverage+" "+result.worldNotice} : preview);
      }
      if(result.status==="failed") throw new Error(result.message);
      if(result.status==="choose_topic") {
        ideaForm(result.prompt);
        $("#idea-form").requestSubmit();
        return;
      }
      if(result.status==="waiting_source") {
        status.textContent=result.message;
        await new Promise(done=>setTimeout(done,5000));
        continue;
      }
      status.textContent=result.installingWorld ? "Preparing the cross-domain knowledge index for first use. Your question is queued…" :
        result.status==="queued" ? "Waiting for the research worker…" : "Checking evidence and scoring answers…";
      await new Promise(done=>setTimeout(done,600));
    }
  } finally {status.remove();}
}
async function picker(mode, restore = false) {
  const stamp = page;
  if (!restore || mode !== pickerMode) pickerPosition = null;
  questionPage = pickerPosition?.page ?? 0;
  pickerCategory = pickerPosition?.category ?? "";
  pickerMode = mode;
  const entries = await api(endpoint("/library"));
  if (!during(stamp)) return;
  library = entries;
  modal(
    mode === "pick" ? "Start a round" : "Question library",
    '<div class="stack question-picker"><div><label><span>Find a question</span><input class="filter" id="question-filter" placeholder="Search by title, topic, region or era"></label>' +
      '<details class="picker-filters"><summary>Filters' + (omitSpecialist || omitExplicit ? " (active)" : "") + '</summary><div>' +
      '<label><input type="checkbox" id="omit-specialist"' + (omitSpecialist ? " checked" : "") + '>Hide specialist rounds</label>' +
      '<label><input type="checkbox" id="omit-explicit"' + (omitExplicit ? " checked" : "") + '>Hide explicit titles</label></div></details></div>' +
      '<ul class="menu-list" id="question-list" aria-label="Questions">' +
      listEntries(pickerPosition?.filter) +
      "</ul>" +
      '<div class="picker-tools">' +
      button("Generate from a prompt", "generate") +
      button(icon("add") + " New question", "new") +
      (mode === "library" ? button("Import questions", "import-pack") : "") +
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
      (q.generated ? '" data-revision="' + esc(q.revision) : "") +
      '" data-previous="' +
      esc(previous) +
      '"><p class="kicker">' +
      esc(q.topic) +
      "</p><h3>" +
      esc(q.prompt) +
      "</h3>" +
      (q.sourceNotice ? '<p role="status">'+esc(q.sourceNotice)+'</p>' : '') +
      (q.context ? '<p class="fine">' + esc(q.context) + "</p>" : "") +
      (q.plays
        ? '<p class="inline-note">This question has already been played ' +
          q.plays +
          " time(s).</p>"
        : "") +
      '<label>Countdown<select name="seconds"><option value="0">No time limit</option><option value="30">30 seconds</option><option value="60">1 minute</option><option value="90">90 seconds</option><option value="120">2 minutes</option><option value="180">3 minutes</option></select></label><button class="primary"'+(q.sourceNotice ? " disabled" : "")+'>Open question</button>' +
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
        topic: pickerCategory,
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
      '"></label><label>Category<input list="question-categories" name="topic" maxlength="60" required value="' +
      esc(q.topic) +
      '"></label><datalist id="question-categories">' +
      [...new Set(library.map((entry) => entry.topic))]
        .sort()
        .map((topic) => '<option value="' + esc(topic) + '"></option>')
        .join("") +
      '</datalist><label>Prompt<textarea name="prompt" rows="2" maxlength="400" required>' +
      esc(q.prompt) +
      '</textarea></label><label>Source URL (optional)<input type="url" name="source" value="' +
      esc(q.source) +
      '"></label><label>Picker notes (region, era or content)<input name="context" maxlength="200" value="' +
      esc(q.context ?? "") +
      '"></label><label>Coverage (optional)<textarea name="coverage" rows="2" maxlength="900">' +
      esc(q.coverage ?? "") +
      '</textarea></label><label>Answer matching<select name="matching"><option value="categories">Categories · recognise specific examples</option><option value="names">Names and titles · allow shortened names</option><option value="full">Full names or codes · allow spelling corrections</option></select></label><div><h3>Accepted answers</h3><p class="fine">Use British display names. Separate alternative names with a vertical bar ( | ).</p></div><div class="editor-rows"></div><div class="bank-pages" id="edit-pages"></div>' +
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
    t.setAttribute("aria-describedby", "answer-help");
  }
  if (["question-filter", "omit-specialist", "omit-explicit"].includes(t.id)) {
    omitSpecialist = $("#omit-specialist").checked;
    omitExplicit = $("#omit-explicit").checked;
    $(".picker-filters summary").textContent = omitSpecialist || omitExplicit ? "Filters (active)" : "Filters";
    questionPage = 0;
    $("#question-list").innerHTML = listEntries($("#question-filter").value);
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
      category: pickerCategory,
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
    case "choose-category":
    case "all-categories":
      pickerCategory = el.dataset.topic ?? "";
      questionPage = 0;
      $("#question-filter").value = "";
      list.innerHTML = listEntries();
      list.scrollTop = 0;
      list.querySelector("button")?.focus({ preventScroll: true });
      return;
    case "import-pack":
      return modal(
        "Import questions",
        '<form id="pack-form" class="stack"><p>Choose a JSON file from your device. Each question includes its accepted answers and rarity tiers.</p>' +
          '<label>Question pack<input name="pack" type="file" accept=".json,application/json" required></label><small>Up to 16 MiB per file. Identical questions are skipped.</small>' +
          '<button class="primary">Import questions</button>' +
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
    case "generate": return generateForm();
    case "draft-idea": return ideaForm(el.dataset.idea ?? el.form?.elements.prompt?.value ?? "", el.dataset.complete === "true");
    case "prepare-draft": {
      const card = el.closest("[data-job]");
      // Keep selection and status together, including for screen readers and small displays.
      modal("Prepare question", '<div class="stack" id="selected-draft"><h3>'+esc(card.querySelector("h3").textContent)+'</h3></div>');
      const id = card.dataset.job;
      return awaitPreview($("#selected-draft"), () => api(endpoint("/generate/"+id+"/prepare"), "POST", {}));
    }
    case "regenerate": return generateForm(library.find(q=>q.id===el.dataset.id)?.prompt);
    case "preview-members": return acceptedAnswers(endpoint("/generate/"+lastResearch.job+"/answers"),points,button("Back to preview","research-preview"),el);
    case "research-preview": return previewResearch(lastResearch.job,lastResearch.q);
    case "refresh-research": return generate(el.closest(".stack"), lastResearch.q.prompt, true);
    case "revise-prompt": return generateForm(lastResearch.q.prompt);
    case "save-generated": {
      const saved=await api(endpoint("/generate/"+lastResearch.job+"/save"),"POST",{});
      await picker(pickerMode);
      if (pickerMode==="pick") configure(saved.id);
      else notice("Generated question saved.");
      return;
    }
    case "request-review": {
      const raw=draft()?.text.trim();
      if (!raw || !state.round) return;
      const result=await api(endpoint("/rounds/"+state.round.id+"/appeal"),"POST",{answer:raw});
      state.appeal=result.raw;
      renderRoom();
      return;
    }
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
      case "generate-form": await generate(f); break;
      case "idea-form": await suggestQuestions(f, e.submitter?.value === "surprise"); break;
      case "answer-form": {
        const answer = values.answer.trim();
        if (!answer) throw new Error("Enter an answer.");
        if (Array.from(answer).length > 140) throw new Error("Enter an answer of 140 characters or fewer.");
        await queueAnswer(answer, state.mine?.revision ?? 0);
        break;
      }
      case "start-form":
        await api(endpoint("/start"), "POST", {
          questionId: f.dataset.id,
          ...(f.dataset.revision ? {revision:Number(f.dataset.revision)} : {}),
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
          ...(values.ruling === "miss" ? { depth: 0 } :
            values.ruling === "unlisted" ? { name: values.newName, depth: Number(values.newDepth), unlisted: true } :
            { name: values.name }),
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
