import { $, esc, modal, button } from "./ui.js";
import { drawKrill } from "./ocean.js";
import { depthLabel, depthNames } from "./scoring.js";
import { playCue, soundButton, syncSoundButtons } from "./effects.js";

const lineupMs = 2500;
const descentMs = 1900;
export const diveDurationMs = lineupMs + descentMs;

export function teamDive(round, board, teamId, audible, elapsed) {
  const results = new Map(
    round.results.map((result) => [result.teamId, result]),
  );
  const crews = board
    .map((team) => {
      const result = results.get(team.id);
      return { ...team, result, before: team.total - (result?.points ?? 0) };
    })
    .sort(
      (a, b) =>
        b.before - a.before || a.name.localeCompare(b.name) || a.id - b.id,
    );
  const cellClass = (crew) => (crew.id === teamId ? ' class="own-lane"' : "");
  const axis = [0, 1, 2, 3, 4, 5]
    .map(
      (tier) =>
        '<span data-band="' + tier + '">' + depthLabel(tier) + "</span>",
    )
    .join("");
  modal(
    "Round " + round.seq + " · The dive",
    '<div class="fleet-dive"><div class="fleet-meta"><span class="kicker" role="status" id="dive-status">At the surface</span><span>' +
      crews.length +
      (crews.length === 1 ? " team" : " teams") +
      "</span></div>" +
      '<div class="fleet-scroll" tabindex="0" role="region" aria-label="Team dive lanes"><table class="fleet-table" aria-label="Team scores and dive depths"><thead><tr><th scope="col" class="fleet-axis">Crew<br>Score</th>' +
      crews
        .map(
          (c) =>
            '<th scope="col"' +
            cellClass(c) +
            '><span class="crew-name">' +
            esc(c.name) +
            (c.id === teamId ? " · you" : "") +
            '</span><strong class="crew-score"><span data-team-score>' +
            c.before +
            "</span> pts</strong></th>",
        )
        .join("") +
      '</tr></thead><tbody><tr><th scope="row" class="fleet-axis"><div class="water-column"><div class="lane-range band-labels">' +
      axis +
      "</div></div></th>" +
      crews
        .map(
          (c) =>
            "<td" +
            cellClass(c) +
            '><div class="water-column"><div class="lane-range"><div class="diver" data-depth="' +
            (c.result?.depth ?? 0) +
            '"><canvas aria-hidden="true" width="32" height="28"></canvas></div></div></div></td>',
        )
        .join("") +
      '</tr></tbody><tfoot><tr><th scope="row" class="fleet-axis">Round<br>result</th>' +
      crews
        .map(
          (c) =>
            "<td" +
            cellClass(c) +
            '><div class="lane-result tier-' +
            (c.result?.depth ?? 0) +
            '"><b>' +
            (c.result ? "+" + c.result.points + " pts" : "Sat out") +
            "</b>" +
            (c.result?.depth
              ? '<span class="lane-answer">' +
                esc(c.result.canonical) +
                "</span>"
              : "") +
            "<small>" +
            (c.result
              ? depthLabel(c.result.depth) + " · " + depthNames[c.result.depth]
              : "Joined after lock") +
            "</small></div></td>",
        )
        .join("") +
      '</tr></tfoot></table></div><div class="fleet-nav" hidden><button type="button" aria-label="Previous teams">‹</button><span>Scroll through teams</span><button type="button" aria-label="Next teams">›</button></div></div>' +
      '<div class="actions dive-actions">' +
      button("Show all results", "dismiss-dive", "secondary") +
      soundButton(true) +
      "</div>",
  );
  const chart = $(".fleet-dive"),
    scroller = $(".fleet-scroll", chart),
    nav = $(".fleet-nav", chart);
  const markers = [...chart.querySelectorAll(".diver")];
  const scores = [...chart.querySelectorAll("[data-team-score]")];
  const headers = [...chart.querySelectorAll("thead th:not(.fleet-axis)")];
  const status = $("#dive-status");
  const motion = matchMedia("(prefers-reduced-motion: reduce)");
  for (const marker of markers) {
    drawKrill($("canvas", marker));
    marker.style.setProperty("--drop", Number(marker.dataset.depth) * 20 + "%");
  }
  syncSoundButtons();
  function updateNavigation() {
    const overflow = scroller.scrollWidth - scroller.clientWidth;
    nav.hidden = overflow <= 2;
    nav.firstElementChild.disabled = scroller.scrollLeft <= 1;
    nav.lastElementChild.disabled = scroller.scrollLeft >= overflow - 1;
  }
  nav.addEventListener("click", (event) => {
    const control = event.target.closest("button");
    if (!control) return;
    const direction = control === nav.firstElementChild ? -1 : 1;
    const target =
      scroller.scrollLeft +
      direction *
        (scroller.clientWidth - $("thead .fleet-axis", chart).offsetWidth);
    const lefts = headers.map((h) => h.offsetLeft - headers[0].offsetLeft);
    const left = lefts.reduce(
      (nearest, x) =>
        Math.abs(x - target) < Math.abs(nearest - target) ? x : nearest,
      lefts[0],
    );
    scroller.scrollTo({
      left,
      behavior: motion.matches ? "instant" : "smooth",
    });
  });
  scroller.addEventListener("scroll", updateNavigation, { passive: true });
  const observer = new ResizeObserver(updateNavigation);
  observer.observe(scroller);
  updateNavigation();
  const own = $("thead .own-lane", chart);
  if (own)
    scroller.scrollLeft =
      own.getBoundingClientRect().left -
      scroller.getBoundingClientRect().left -
      (scroller.clientWidth - own.offsetWidth) / 2;

  const start = performance.now() - elapsed;
  let frame,
    started = false;
  const cueTier =
    results.get(teamId)?.depth ??
    crews.reduce((deepest, c) => Math.max(deepest, c.result?.depth ?? 0), 0);
  function draw(now) {
    const progress = motion.matches
      ? 1
      : Math.min(1, Math.max(0, (now - start - lineupMs) / descentMs));
    if (progress > 0 && !started) {
      started = true;
      status.textContent = "Diving together";
      chart.classList.add("descending");
      if (audible) playCue("dive", cueTier);
    }
    const travel = progress * progress * (3 - 2 * progress);
    chart.style.setProperty("--progress", travel);
    if (started)
      crews.forEach((c, i) => {
        const total = String(
          c.before + Math.round((c.total - c.before) * travel),
        );
        if (scores[i].textContent !== total) scores[i].textContent = total;
      });
    if (progress === 1) {
      status.textContent = "Dive complete";
      chart.classList.add("landed");
    } else frame = requestAnimationFrame(draw);
  }
  function settle() {
    if (motion.matches && !chart.classList.contains("landed")) {
      cancelAnimationFrame(frame);
      draw(performance.now());
    }
  }
  motion.addEventListener("change", settle);
  draw(performance.now());
  return () => {
    cancelAnimationFrame(frame);
    observer.disconnect();
    motion.removeEventListener("change", settle);
  };
}
