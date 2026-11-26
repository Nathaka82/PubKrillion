import { apiOrigin } from "./config.js";
import { mountOcean, drawKrill } from "./ocean.js";
import { points, depthNames, depthLabel } from "./scoring.js";
export const $ = (s, root = document) => root.querySelector(s);
export const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const colors = ["", "#7ce4cb", "#62cef6", "#8f9eff", "#ce92f3", "#ffbc78"];
const glyphs = [
  [],
  [
    "..........oo..",
    "...oooo..ooo..",
    "..ooooo..oo...",
    "ooooooooo.....",
    ".oo.oo.ooo....",
    ".....o........",
  ],
  [
    ".....ooo......",
    "...ooooooo....",
    "..ooooooooo...",
    "oo.oo.oo.oo...",
    "...o..o..o....",
    "..oo..oo.oo...",
  ],
  [
    ".....oo.......",
    "....oooo......",
    "...oooooo.....",
    "...oooooo.....",
    "..oooooooo....",
    "...o.oo.o.....",
    "..oo.o..oo....",
  ],
  [
    "....ooo.......",
    "...o..oi......",
    "...o..........",
    ".ooooooo...oo.",
    "ooooooooo.ooo.",
    ".oo..ooooooo..",
    "...oooo.......",
  ],
  [
    "..o.....o.....",
    "...o...o......",
    "....ooooo.....",
    "...ooooooo....",
    "..oo..ooooo...",
    "..oo...ooo....",
    "...ooooo......",
    ".....o.o......",
  ],
];
export function drawSpecies(canvas) {
  const tier = Number(canvas.dataset.tier),
    pattern = glyphs[tier];
  if (!pattern) return;
  const ctx = canvas.getContext("2d");
  canvas.width = 16;
  canvas.height = 10;
  ctx.clearRect(0, 0, 16, 10);
  pattern.forEach((row, y) =>
    [...row].forEach((c, x) => {
      if (c !== ".") {
        ctx.fillStyle = c === "i" ? "#eafcff" : colors[tier];
        ctx.fillRect(x + 1, y + 1, 1, 1);
      }
    }),
  );
}
const icons = {
  speaker:
    '<path d="M3 9h4l5-4v14l-5-4H3zM15 9a4 4 0 0 1 0 6m3-9a8 8 0 0 1 0 12"/>',
  mutedSpeaker: '<path d="M3 9h4l5-4v14l-5-4H3zM16 9l5 6m0-6-5 6"/>',
  key: '<path d="M7 4v16M17 4v16M3 9h18M3 15h18"/>',
  wheel:
    '<circle cx="12" cy="12" r="6"/><circle cx="12" cy="12" r="2"/><path d="M12 2v8m0 4v8M2 12h8m4 0h8M5 5l5 5m4 4 5 5M5 19l5-5m4-4 5-5"/>',
  book: '<path d="M3 5c3-1 6 0 9 2 3-2 6-3 9-2v13c-4-1-6 0-9 2-3-2-5-3-9-2zM12 7v13"/>',
  lock: '<rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/>',
  waves:
    '<path d="M2 7q3-4 6 0t6 0 6 0m-18 5q3-4 6 0t6 0 6 0m-18 5q3-4 6 0t6 0 6 0"/>',
  users:
    '<circle cx="9" cy="8" r="3"/><path d="M3 20v-2a6 6 0 0 1 12 0v2m0-15a3 3 0 0 1 0 6m2 3a5 5 0 0 1 4 5"/>',
  timer: '<circle cx="12" cy="14" r="8"/><path d="M12 10v4l3 1M9 2h6m-3 0v4"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  add: '<path d="M12 4v16M4 12h16"/>',
  flag: '<path d="M5 22V3m0 1q4-3 8 0t7 0v10q-4 3-8 0t-7 0"/>',
  trophy:
    '<path d="M7 3h10v6a5 5 0 0 1-10 0zM7 5H3v2q0 5 5 5m9-7h4v2q0 5-5 5m-4 2v5m-5 2h10"/>',
  check: '<path d="m4 12 5 5L20 6"/>',
  signal: '<path d="M4 20v-4m5 4v-8m5 8V8m5 12V4"/>',
  anchor:
    '<circle cx="12" cy="5" r="2"/><path d="M12 7v14M3 13q1 7 9 8 8-1 9-8m-18 0 4 2m14-2-4 2M8 10h8"/>',
};
export const icon = (name) =>
  '<svg class="icon" aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
  (icons[name] ?? "") +
  "</svg>";
export const art = (mode = "surface") =>
  '<canvas class="seascape" data-ocean="' +
  mode +
  '" aria-hidden="true"></canvas>';
let cleanup = () => {};
export function paint(html) {
  cleanup();
  $("#app").innerHTML = html;
  const jobs = [...document.querySelectorAll("#app [data-ocean]")].map((c) =>
    mountOcean(c, { mode: c.dataset.ocean }),
  );
  document.querySelectorAll("[data-tier]").forEach(drawSpecies);
  cleanup = () => jobs.forEach((fn) => fn());
}
export function brand() {
  drawKrill($("#brand-krill"));
}
export function depthGuide() {
  return (
    '<section class="depth-guide" aria-label="Rarity scoring"><div class="section-line"><span>' +
    icon("waves") +
    ' Depth guide</span><i></i><small>RARITY SCORING</small></div><div class="tier-grid">' +
    depthNames
      .slice(1)
      .map(
        (n, i) =>
          '<div class="tier tier-' +
          (i + 1) +
          '"><canvas data-tier="' +
          (i + 1) +
          '" aria-hidden="true"></canvas><div><strong>' +
          n +
          "</strong><small>" +
          points[i + 1] +
          " points</small><small>" +
          depthLabel(i + 1) +
          "</small></div></div>",
      )
      .join("") +
    "</div></section>"
  );
}
export const heading = (k, t, blurb = "") =>
  '<div class="heading"><p class="kicker">' +
  esc(k) +
  "</p><h1>" +
  esc(t) +
  "</h1>" +
  (blurb ? "<p>" + esc(blurb) + "</p>" : "") +
  "</div>";
export function notice(message) {
  const box = $("#toast");
  box.textContent = message;
  box.hidden = false;
  clearTimeout(notice.timer);
  notice.timer = setTimeout(() => (box.hidden = true), 4300);
}
export function connect(offline) {
  const box = $("#connection");
  box.hidden = !offline;
  box.textContent = offline ? "Connection lost · reconnecting…" : "";
}
export function modal(title, body) {
  const dialog = $("#modal");
  $("#dialog-content").innerHTML =
    '<div class="dialog-head"><h2 id="dialog-title">' +
    esc(title) +
    '</h2><button type="button" class="close" data-close aria-label="Close dialog">×</button></div>' +
    body +
    '<p class="error" id="modal-error" role="alert"></p>';
  if (!dialog.open) dialog.showModal();
}
export function closeModal() {
  if ($("#modal").open) $("#modal").close();
}
export function how(sourceJudged) {
  const missing = sourceJudged
    ? "The game checks answers against the round’s sources. If it asks for a fuller name, try the complete title or another recorded spelling."
    : "When Ask host to review appears, you can send your last guess to the host.";
  modal(
    "How to play",
    '<ol class="how"><li><strong>Join a crew</strong><p>Use the host’s room code and choose a team name. One phone is enough for your whole team.</p></li><li><strong>Think of a rare answer</strong><p>Tap Dive to submit. If you see “Did you mean?”, confirm the suggestion or edit your answer. Your first accepted answer is final. ' + missing + '</p></li><li><strong>Watch the dive</strong><p>Teams start side by side with their current scores and dive together. Answers share five rarity bands. Several answers can tie. Scores are set before play using recall data or estimates of how familiar a name is. A miss stays at the surface.</p></li></ol><button class="primary wide" data-close>Got it</button>',
  );
}
export const splitHosting =
  Boolean(apiOrigin) && new URL(apiOrigin).origin !== location.origin;
let hostSession;
function splitSession(key, value) {
  if (!key.endsWith(":host")) return storage(key, value);
  if (value !== undefined) {
    hostSession = value;
    try {
      if (value) sessionStorage.setItem(key, value);
      else sessionStorage.removeItem(key);
    } catch {}
  }
  if (hostSession !== undefined) return hostSession;
  try {
    return sessionStorage.getItem(key);
  } catch {
    return null;
  }
}
export async function api(
  path,
  method = "GET",
  body,
  headers = {},
  binary = false,
) {
  const teamRoom = /^\/rooms\/([A-Z2-9]{6})(?:\/|$)/.exec(path)?.[1];
  const role =
    teamRoom && !location.hash.startsWith("#/host") ? teamRoom : "host";
  const key =
    "quiz-session:" + apiOrigin + ":" + location.pathname + ":" + role;
  const token = splitHosting && splitSession(key);
  let res;
  try {
    res = await fetch(apiOrigin + "/api" + path, {
      method,
      cache: "no-store",
      credentials: splitHosting ? "omit" : "same-origin",
      redirect: "error",
      headers: {
        ...(body ? { "Content-Type": "application/json" } : {}),
        ...(token ? { Authorization: "Bearer " + token } : {}),
        ...headers,
      },
      body: body ? JSON.stringify(body) : undefined,
      signal: AbortSignal.timeout(
        method !== "GET" && /\/(?:packs|drafts|library(?:\/[\w-]+)?)$/.test(path)
          ? 120000
          : 10000,
      ),
    });
  } catch {
    const e = new Error("Connection lost. Check your signal and try again.");
    e.network = true;
    throw e;
  }
  if (splitHosting && res.ok) {
    const session = res.headers.get("X-Quiz-Session");
    if (session) splitSession(key, session);
    if (path === "/logout") splitSession(key, null);
  }
  if (binary && res.ok) return res.blob();
  if (res.status === 304) return { unchanged: true };
  let json;
  try {
    json = await res.json();
  } catch {
    const e = new Error(
      "The quiz server is temporarily unavailable. Please retry.",
    );
    e.network = true;
    throw e;
  }
  if (!res.ok) {
    const e = new Error(json.error || "Request failed.");
    e.suggestion = json.suggestion;
    e.status = res.status;
    e.network = res.status >= 500 || res.status === 408;
    throw e;
  }
  if (path.includes("/state")) json.etag = res.headers.get("ETag");
  return json;
}
export function button(label, action, kind = "secondary", extra = "") {
  return (
    '<button type="button" class="' +
    kind +
    '" data-action="' +
    action +
    '" ' +
    extra +
    ">" +
    label +
    "</button>"
  );
}
// Entries here shadow a stale disk value only while persistence is unavailable.
const temporaryStorage = new Map();
export const volatileStorage = (key) => temporaryStorage.has(key);
export function storage(key, val) {
  if (val === undefined) {
    if (temporaryStorage.has(key)) return temporaryStorage.get(key);
    try {
      return JSON.parse(localStorage.getItem(key) ?? "null");
    } catch {
      return temporaryStorage.get(key) ?? null;
    }
  }
  temporaryStorage.set(key, val);
  try {
    if (val === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(val));
    temporaryStorage.delete(key);
  } catch {}
  return val;
}
