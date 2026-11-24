import { score } from "./sound-score.js";
import { icon, storage } from "./ui.js";

const preference = "krill-club:sound";
const RealtimeAudio = window.AudioContext || window.webkitAudioContext;
const mediaSupported = Boolean(
  document.createElement("audio").canPlayType?.("audio/wav"),
);
const supported = Boolean(RealtimeAudio || mediaSupported);
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)");
let enabled = storage(preference) !== false;
let context, master, player, priming;
const voices = new Set();

export function soundButton(compact = false) {
  return (
    '<button type="button" data-sound class="sound-switch' +
    (compact ? " compact" : "") +
    '" aria-label="Sound effects">' +
    icon("speaker") +
    icon("mutedSpeaker") +
    "<span></span></button>"
  );
}

export function syncSoundButtons() {
  for (const button of document.querySelectorAll("[data-sound]")) {
    const on = supported && enabled;
    button.setAttribute("aria-pressed", String(on));
    button.disabled = !supported;
    button.title = supported
      ? on
        ? "Mute sound effects"
        : "Enable sound effects"
      : "Sound unavailable";
    button.querySelector("span").textContent = on ? "Sound on" : "Sound off";
  }
}

// Create and resume the context within a gesture so the first round works on phones.
async function unlock() {
  if (!supported || !enabled || document.hidden) return false;
  if (player) return priming ?? true;
  try {
    if (!context && RealtimeAudio) {
      context = new RealtimeAudio({ latencyHint: "interactive" });
      master = context.createGain();
      master.gain.value = 0.65;
      master.connect(context.destination);
    }
    if (context) {
      if (context.state !== "running") await context.resume();
      return enabled && !document.hidden && context.state === "running";
    }
  } catch {
    // Use the same score through the media element when realtime audio is unavailable.
  }
  if (!mediaSupported) return false;
  const media = document.createElement("audio");
  player = media;
  media.src = new URL("./sounds/silence.wav", import.meta.url).href;
  priming = media
    .play()
    .then(
      () => enabled && !document.hidden,
      () => {
        // A blocked or interrupted first gesture can be retried on the next one.
        player = null;
        return false;
      },
    )
    .finally(() => {
      priming = null;
    });
  return priming;
}

function silence() {
  player?.pause();
  for (const voice of voices) voice.stop();
  voices.clear();
}

export function initialiseEffects() {
  document.querySelector("#sound-control").innerHTML = soundButton();
  syncSoundButtons();
  for (const type of ["click", "keydown"]) {
    document.addEventListener(
      type,
      (event) => {
        if (
          event.isTrusted &&
          !event.target.closest?.("[data-sound]") &&
          !player &&
          context?.state !== "running"
        )
          void unlock();
      },
      true,
    );
  }
  document.addEventListener("click", async (event) => {
    if (!event.target.closest("[data-sound]")) return;
    enabled = !enabled;
    storage(preference, enabled);
    syncSoundButtons();
    if (!enabled) silence();
    else if (await unlock()) playCue("preview");
  });
  window.addEventListener("storage", (event) => {
    if (event.key !== preference) return;
    enabled = storage(preference) !== false;
    if (!enabled) silence();
    syncSoundButtons();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) silence();
  });
}

// Each note has its own short attack and release to keep even the timer ticks soft.
function tone(start, hz, duration, level, endHz = hz, wave = "sine") {
  const voice = context.createOscillator();
  const gain = context.createGain();
  voice.type = wave;
  voice.frequency.setValueAtTime(hz, start);
  voice.frequency.exponentialRampToValueAtTime(endHz, start + duration);
  gain.gain.setValueAtTime(0.0001, start);
  gain.gain.exponentialRampToValueAtTime(level, start + 0.009);
  gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
  voice.connect(gain);
  gain.connect(master);
  voices.add(voice);
  voice.onended = () => {
    voices.delete(voice);
    voice.disconnect();
    gain.disconnect();
  };
  voice.start(start);
  voice.stop(start + duration + 0.015);
}

export function playCue(cue, tier = 0) {
  if (!enabled || document.hidden) return;
  if (player && !priming) {
    const name = cue === "dive" ? "dive-" + tier : cue;
    player.src = new URL("./sounds/" + name + ".wav", import.meta.url).href;
    player.play().catch(() => {});
  } else if (context?.state === "running") {
    score(cue, tier, context.currentTime + 0.012, tone);
  }
}

export function enter(element, delay = 0) {
  if (!element || reducedMotion.matches || document.hidden) return;
  // A CSS class also lets a mid-animation OS preference change take effect.
  element.style.animationDelay = delay + "ms";
  element.classList.add("cue-enter");
}
