import { esc, icon, art, button, heading, depthGuide } from "./ui.js";
import { depthLabel } from "./scoring.js";
export function home() {
  return (
    '<section class="hero">' +
    art("daylight") +
    '<div class="hero-copy"><h1>The Pub Dive</h1><p class="kicker">Multiplayer pub quiz</p><p>Find a rare answer and take your team deeper.</p></div><div class="depth-meter" aria-hidden="true"><span>0 m</span><span>50</span><span>100</span></div></section>' +
    '<div class="landing-cards"><section class="card"><div class="card-title"><span class="icon-tile">' +
    icon("users") +
    '</span><h2>Join a dive</h2></div><p>Enter the room code from your quiz host.</p><form class="join-form" id="code-form"><label class="input-with-icon"><span class="sr">Room code</span>' +
    icon("key") +
    '<input class="code-input" name="code" placeholder="Room code" maxlength="6" minlength="6" required autocomplete="off" autocapitalize="characters" spellcheck="false"></label><button class="primary">Join room</button></form></section>' +
    '<section class="card"><div class="card-title"><span class="icon-tile coral">' +
    icon("wheel") +
    '</span><h2>Host a quiz</h2></div><p>Open a room and lead your crew through the rounds.</p><a class="action-link host-link" href="#/host">' +
    icon("wheel") +
    " Host console</a></section></div>" +
    depthGuide()
  );
}
export function signin() {
  return (
    '<div class="narrow"><a class="back" href="#/">Home</a><div class="mini-scene">' +
    art() +
    '<p class="kicker">Quizmaster</p><h1>Host console</h1></div><div class="panel top-gap">' +
    heading(
      "Host access",
      "Sign in",
      "Use the host password configured for this server.",
    ) +
    '<form id="login-form" class="stack"><label>Host password<input type="password" autocomplete="current-password" name="password"></label><button class="primary">Sign in</button><p class="error" role="alert"></p></form></div></div>'
  );
}
export function dashboard(rooms) {
  return (
    '<div class="narrow"><a class="back" href="#/">Home</a><div class="mini-scene">' +
    art() +
    '<p class="kicker">Quizmaster</p><h1>Host console</h1></div><section class="panel top-gap">' +
    heading(
      "New event",
      "Open a room",
      "Teams can stay in the same room for the whole crawl.",
    ) +
    '<form id="create-form" class="stack"><label>Event name<input name="name" maxlength="70" placeholder="Society pub crawl" required></label><button class="primary">Create room</button></form></section><section class="panel top-gap"><div class="row spread"><h2>Your rooms</h2>' +
    button("Sign out", "logout", "text-btn") +
    "</div>" +
    (rooms.length
      ? '<ul class="menu-list">' +
        rooms
          .map(
            (r) =>
              '<li><a class="item-main" href="#/host/' +
              r.code +
              '"><strong>' +
              esc(r.name) +
              "</strong><small>" +
              r.code +
              " · " +
              r.teams +
              " teams · " +
              (r.finished ? "Finished" : "Open") +
              "</small></a>" +
              button(
                "Delete",
                "delete-room",
                "text-btn delete-room",
                'data-code="' +
                  r.code +
                  '" data-name="' +
                  esc(r.name) +
                  '" aria-label="Delete room ' +
                  esc(r.name) +
                  '"',
              ) +
              "</li>",
          )
          .join("") +
        "</ul>"
      : '<p class="empty">Your event rooms will appear here.</p>') +
    "</section></div>"
  );
}
export function join(code, info, transfer = false) {
  return (
    '<div class="narrow"><a class="back" href="#/">Home</a><div class="mini-scene">' +
    art() +
    '<p class="kicker">Room ' +
    esc(code) +
    "</p><h1>" +
    esc(info.name) +
    '</h1></div><div class="panel top-gap">' +
    (info.finished
      ? heading(
          "Final standings",
          "Quiz finished",
          "This room is closed to new teams.",
        )
      : heading(
          "Team check-in",
          transfer ? "Recover your team" : "Join the crew",
          transfer
            ? "Enter the one-time recovery code from your host to keep your score."
            : "Pick a team name. Everyone on your team can play together on this phone.",
        ) +
        '<form class="stack" id="join-form">' +
        (transfer
          ? '<label>Recovery code<input name="transfer" class="code-input" required maxlength="10" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="5-character code"></label>'
          : '<label>Team name<input name="name" placeholder="Your team name" required minlength="2" maxlength="32" autocomplete="off"></label>') +
        '<button class="primary">' +
        (transfer ? "Recover team" : "Join room") +
        '</button><p class="error" role="alert"></p></form><a class="text-btn top-gap" href="#/join/' +
        code +
        (transfer ? "" : "/recover") +
        '">' +
        (transfer ? "Join as a new team" : "Playing on a replacement phone?") +
        "</a>") +
    "</div></div>"
  );
}
function board(s) {
  const latestDive = new Map(
    s.round?.results?.map((x) => [x.teamId, depthLabel(x.depth)]),
  );
  return (
    '<section class="panel"><div class="board-head">' +
    icon("trophy") +
    "<h2>" +
    (s.room.finished ? "Final standings" : "Leaderboard") +
    "</h2></div>" +
    (s.board.length
      ? '<ol class="board">' +
        s.board
          .map(
            (x) =>
              '<li class="' +
              (x.id === s.team?.id ? "me" : "") +
              '"><span class="rank">' +
              x.rank +
              "</span><b>" +
              esc(x.name) +
              (x.id === s.team?.id ? " · you" : "") +
              (latestDive.has(x.id)
                ? '<small class="last-dive">Last dive · ' +
                  latestDive.get(x.id) +
                  "</small>"
                : "") +
              "</b>" +
              (x.movement
                ? '<small class="movement" title="Rank change">' +
                  (x.movement > 0 ? "↑" : "↓") +
                  Math.abs(x.movement) +
                  "</small>"
                : "") +
              (x.gain ? '<small class="gain">+' + x.gain + "</small>" : "") +
              '<span class="score">' +
              x.total +
              " <small>pt</small></span></li>",
          )
          .join("") +
        "</ol>"
      : '<p class="empty">Teams appear here as they join.</p>') +
    "</section>"
  );
}
export function groups(r) {
  const seen = new Map();
  for (const x of r.results ?? []) {
    const key = x.depth + "|" + x.canonical;
    const g = seen.get(key) ?? {
      name: x.canonical,
      depth: x.depth,
      points: x.points,
      entries: [],
    };
    g.entries.push(x);
    seen.set(key, g);
  }
  return [...seen.values()];
}
export function results(r, teamId) {
  const g = groups(r);
  return g.length
    ? '<ul class="reveal-list">' +
        g
          .map(
            (a) =>
              '<li class="reveal-line tier-' +
              a.depth +
              (a.entries.some((x) => x.teamId === teamId) ? " mine" : "") +
              '">' +
              (a.depth
                ? '<canvas data-tier="' +
                  a.depth +
                  '" aria-hidden="true"></canvas>'
                : "<span>·</span>") +
              "<div><strong>" +
              esc(a.name) +
              "</strong><small>" +
              depthLabel(a.depth) +
              "</small><small>" +
              a.entries
                .map((x) => esc(x.team) + (x.teamId === teamId ? " (you)" : ""))
                .join(" · ") +
              '</small></div><span class="score">+' +
              a.points +
              " pts</span></li>",
          )
          .join("") +
        "</ul>"
    : '<p class="empty">No answers were submitted this round.</p>';
}
export function review(r) {
  const list = [
    ...(r.review ?? []),
    ...(r.missedTeams ?? []).map((t) => ({
      ...t,
      raw: t.name + (t.lastGuess ? " · “" + t.lastGuess + "”" : " · missed submission"),
      count: 1,
      teamId: t.id,
    })),
  ];
  return list.length
    ? "<div>" +
        list
          .map(
            (x) =>
              '<div class="review-line"><span>' +
              esc(x.raw) +
              "<small>" +
              x.count +
              " " +
              (x.count === 1 ? "team" : "teams") +
              " · " +
              r.pointValues[x.depth] +
              " pts · " +
              depthLabel(x.depth) +
              "</small></span>" +
              button(
                "Correct",
                "judge",
                "secondary mini",
                'data-round="' +
                  r.id +
                  (x.teamId
                    ? '" data-team="' + x.teamId
                    : '" data-key="' + esc(x.key)) +
                  '"',
              ) +
              "</div>",
          )
          .join("") +
        "</div>"
    : "";
}
export function audit(r) {
  return r.rulings?.length
    ? "<details><summary>Ruling history (" +
        r.rulings.length +
        ")</summary>" +
        r.rulings
          .map(
            (x) =>
              '<p class="answer-log">' +
              esc(x.key) +
              " → " +
              esc(x.canonical) +
              " · " +
              r.pointValues[x.depth] +
              " points</p>",
          )
          .join("") +
        "</details>"
    : "";
}
function timing(r) {
  return r.deadline && r.phase === "open"
    ? '<span class="timer" data-deadline="' +
        r.deadline +
        '">' +
        icon("timer") +
        " <span></span></span>"
    : "";
}
function teamStatus(s) {
  if (!s.roster) return "";
  function group(title, submitted) {
    const teams = s.roster.filter((t) => submitted === "review" ? t.requested : t.submitted === submitted && !t.requested);
    return (
      "<section><h3>" +
      title +
      " <small>(" +
      teams.length +
      ")</small></h3>" +
      (teams.length
        ? "<ul>" +
          teams.map((t) => "<li>" + esc(t.name) + "</li>").join("") +
          "</ul>"
        : '<p class="fine">None</p>') +
      "</section>"
    );
  }
  return (
    '<details id="team-status" class="team-status" data-round="' +
    s.round.id +
    '"><summary>Team status</summary>' +
    group(s.round.phase === "open" ? "Still thinking" : "Missed", false) +
    (s.roster.some(t=>t.requested) ? group("Asked for review", "review") : "") +
    group("Submitted", true) +
    "</details>"
  );
}
function roundPanel(s, host, draft, unseenDive) {
  const r = s.round;
  const replay =
    r?.phase === "revealed" && s.board.length
      ? unseenDive
        ? button("Watch this round’s dive", "replay", "primary")
        : button("Replay dive", "replay", "text-btn")
      : "";
  if (s.room.finished)
    return (
      '<section class="panel">' +
      heading("Event complete", "Final podium") +
      replay +
      (s.board.length
        ? '<div class="podium">' +
          s.board
            .filter((x) => x.rank <= 3)
            .map(
              (x) =>
                '<div><span class="rank">#' +
                x.rank +
                "</span> <strong>" +
                esc(x.name) +
                "</strong> · " +
                x.total +
                " pts</div>",
            )
            .join("") +
          "</div>"
        : '<p class="empty">No teams joined this event.</p>') +
      "</section>"
    );
  if (!r)
    return (
      '<section class="panel flow">' +
      heading(
        "Surface lobby",
        host ? "Ready when you are" : "Waiting for the host",
        host
          ? "Share the room with your teams, then pick a question whenever you’re ready."
          : "Your team is in. The first question will appear here when the host starts the round.",
      ) +
      (host ? button(icon("add") + " Start round", "pick", "primary") : "") +
      "</section>"
    );
  const top =
    '<div class="round-meta"><span class="pill ' +
    (r.phase === "open" ? "live" : "") +
    '">Round ' +
    r.seq +
    "</span><span>" +
    esc(r.topic) +
    "</span>" +
    timing(r) +
    '</div><h2 class="question">' +
    esc(r.prompt) +
    "</h2>";
  if (r.phase === "revealed")
    return (
      '<section class="panel">' +
      top +
      '<div class="row spread"><span class="kicker">Dive results</span>' +
      replay +
      "</div>" +
      results(r, s.team?.id) +
      (s.mine
        ? '<p class="fine top-gap">You submitted “' +
          esc(s.mine.raw) + "”." + (s.mine.canonical ? " Scored as “"+esc(s.mine.canonical)+"”." : "") + "</p>"
        : "") +
      (host
        ? '<div class="actions">' +
          button(icon("add") + " Start round", "pick", "primary") +
          "</div>"
        : '<p class="fine top-gap">Waiting for the host to start another round.</p>') +
      "</section>"
    );
  if (host) {
    const outstanding = s.eligible - s.received;
    const lockLabel = outstanding
      ? "Lock with " +
        outstanding +
        (outstanding === 1 ? " team" : " teams") +
        " outstanding"
      : "Lock answers";
    return (
      '<section class="panel">' +
      top +
      '<p class="tally">' +
      s.received +
      " / " +
      s.eligible +
      " teams submitted</p>" +
      (r.phase === "open"
        ? '<div class="actions">' +
          button(icon("lock") + " " + lockLabel, "lock", "primary") +
          (r.deadline ? button("+30 seconds", "extend") : "") +
          "</div>"
        : '<p class="fine top-gap">' +
          (s.eligible > s.received
            ? s.eligible - s.received + " missed this round."
            : "All submissions are ready to reveal.") +
          "</p>" +
          '<div class="actions">' +
          button(icon("waves") + " Reveal answers", "reveal", "primary") +
          "</div>") +
      teamStatus(s) +
      (r.phase === "locked" && (r.review?.length || r.missedTeams?.length)
        ? "<details><summary>Correct a ruling</summary>" +
          review(r) +
          "</details>"
        : "") +
      "</section>"
    );
  }
  if (r.phase === "locked" && !s.mine)
    return (
      '<section class="panel">' +
      top +
      '<p class="inline-note">Answers locked. The host is preparing the reveal.</p>' +
      (s.eligibleForRound && !r.sourceJudged
          ? '<p class="fine top-gap">No answer was locked in. The host can review your last submitted guess.</p>'
        : "") +
      "</section>"
    );
  const saved = s.mine?.canonical ?? s.mine?.raw;
  const pending = draft?.pending;
  return (
    '<section class="panel answer-panel"><div class="prompt-water"><div class="round-meta"><span class="kicker">Round ' +
    r.seq +
    "</span><span>" +
    esc(r.topic) +
    "</span>" +
    timing(r) +
    '</div><h2 class="question">' +
    esc(r.prompt) +
    '</h2>' + (r.playerRule ? '<details class="fine"><summary>What counts?</summary><p>'+esc(r.playerRule)+'</p></details>' : "") + '<p class="depth-hint">Rarer answers sink deeper</p></div><div class="answer-deck">' +
    (saved
      ? '<div role="status" class="answer-receipt" data-saved-answer>' +
        icon("check") +
        "<div><small>Answer locked in</small><strong>" +
        esc(saved) +
        '</strong></div></div><p class="answer-help">Waiting for the round reveal.</p>'
      : pending
        ? '<div role="status" class="answer-receipt pending">' +
          icon("signal") +
          "<div><small>Awaiting confirmation</small><strong>" +
          esc(pending.text) +
          '</strong></div></div><p class="answer-help">Keep this page open. Your answer will retry if the signal drops.</p>'
        : '<form id="answer-form"><label class="sr" for="team-answer">Your team’s answer</label><div class="answer-entry"><input id="team-answer" name="answer" required maxlength="280" autocomplete="off" enterkeyhint="send" placeholder="Type your answer…" aria-describedby="answer-help' +
          (draft?.feedback ? " answer-feedback" : "") + '" value="' +
          esc(draft?.text ?? "") +
          '"><button class="primary" aria-label="Dive — submit answer">Dive <span aria-hidden="true">▼</span></button></div><p class="error" role="alert"></p></form>') +
    (!saved && !pending && draft?.feedback
      ? '<div id="answer-feedback" class="inline-note top-gap" role="status" tabindex="-1">' +
        (draft.feedback.suggestion
          ? "Did you mean: <strong>" +
            esc(draft.feedback.suggestion) +
            '</strong>?<div class="actions">' +
            button(
              "Submit " + esc(draft.feedback.suggestion),
              "confirm-suggestion",
              "primary",
            ) +
            button("Edit answer", "edit-answer") +
            "</div>"
          : esc(draft.feedback.message)) +
        (r.sourceJudged ? "" : s.appeal === draft.text.trim() ? '<p>Sent to the host for review.</p>' : button("Ask host to review","request-review","secondary")) +
        "</div>"
      : "") +
    (!saved && !pending
      ? '<p class="answer-help" id="answer-help">' +
        icon("lock") +
        " Your first accepted answer is final.</p>"
      : "") +
    "</div></section>"
  );
}
export function roomView(s, host, draft, unseenDive) {
  const h = s.history.length
    ? '<details class="panel history" id="round-history"><summary>Round history (' +
      s.history.length +
      ')</summary><ul class="menu-list">' +
      s.history
        .map(
          (x) =>
            "<li>" +
            button(
              "Round " + x.seq + " · " + esc(x.title),
              "history",
              "item-main",
              'data-round="' + x.id + '"',
            ) +
            "</li>",
        )
        .join("") +
      "</ul></details>"
    : "";
  const toolbar = host
    ? '<section class="panel flow"><h2>Room controls</h2><div class="row">' +
      button(icon("users") + " Invite teams", "invite") +
      (s.room.finished ? "" : button(icon("book") + " Questions", "library")) +
      (s.round
        ? button(
            "Accepted answers",
            "round-answers",
            "secondary",
            'data-round="' + s.round.id + '"',
          )
        : "") +
      "</div>" +
      (s.board.length && !s.room.finished
        ? button("Transfer a team", "transfer", "text-btn")
        : "") +
      (!s.room.finished
        ? "<div>" +
          button(
            icon("flag") + " Finish quiz",
            "finish",
            "text-btn",
            s.round && s.round.phase !== "revealed" ? "disabled" : "",
          ) +
          "</div>"
        : "") +
      "</section>"
    : "";
  const stat = host
    ? s.board.length
    : (s.board.find((x) => x.id === s.team.id)?.total ?? 0);
  return (
    (host ? '<a class="back" href="#/host">Host console</a>' : "") +
    '<div class="mini-scene room-scene">' +
    art(
      s.round?.phase === "revealed" ? "deep" : s.round ? "surface" : "daylight",
    ) +
    '<p class="kicker">' +
    (host ? "Host console" : esc(s.team.name)) +
    " · " +
    esc(s.room.code) +
    "</p><h1>" +
    esc(s.room.name) +
    '</h1><div class="scene-hud"><small>' +
    (host ? "Teams" : "Score") +
    "</small><strong>" +
    stat +
    '</strong></div></div><div class="split top-gap"><div>' +
    roundPanel(s, host, draft, unseenDive) +
    h +
    '</div><aside class="flow">' +
    toolbar +
    board(s) +
    "</aside></div>"
  );
}
