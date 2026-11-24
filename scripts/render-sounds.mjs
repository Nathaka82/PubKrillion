// Regenerate the bundled media clips after editing public/sound-score.js.
import { mkdirSync, writeFileSync } from "node:fs";
import { score } from "../public/sound-score.js";

function render(cue, tier = 0) {
  const rate = 22050,
    notes = [];
  score(
    cue,
    tier,
    0,
    (start, hz, duration, level, endHz = hz, wave = "sine") => {
      notes.push({ start, hz, duration, level, endHz, wave });
    },
  );
  // Some media decoders never report playback for files shorter than one buffer.
  const length = Math.ceil(
    rate * Math.max(0.45, ...notes.map((n) => n.start + n.duration + 0.01)),
  );
  const samples = new Float32Array(length);
  for (const n of notes) {
    const first = Math.round(n.start * rate);
    let phase = 0;
    for (let i = 0; i < n.duration * rate; i++) {
      const t = i / rate;
      const amplitude =
        t < 0.009
          ? 0.0001 * (n.level / 0.0001) ** (t / 0.009)
          : n.level *
            (0.0001 / n.level) ** ((t - 0.009) / (n.duration - 0.009));
      const sine = Math.sin(phase);
      samples[first + i] +=
        0.65 *
        amplitude *
        (n.wave === "triangle" ? (2 * Math.asin(sine)) / Math.PI : sine);
      phase +=
        (2 * Math.PI * n.hz * (n.endHz / n.hz) ** (t / n.duration)) / rate;
    }
  }
  const bytes = new ArrayBuffer(44 + length * 2),
    view = new DataView(bytes);
  function label(offset, s) {
    for (let i = 0; i < s.length; i++)
      view.setUint8(offset + i, s.charCodeAt(i));
  }
  label(0, "RIFF");
  view.setUint32(4, bytes.byteLength - 8, true);
  label(8, "WAVE");
  label(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  label(36, "data");
  view.setUint32(40, length * 2, true);
  samples.forEach((x, i) =>
    view.setInt16(
      44 + 2 * i,
      Math.round(Math.max(-1, Math.min(1, x)) * 32767),
      true,
    ),
  );
  return new Uint8Array(bytes);
}

const directory = new URL("../public/sounds/", import.meta.url);
mkdirSync(directory, { recursive: true });
for (const cue of [
  "silence",
  "preview",
  "open",
  "saved",
  "tick",
  "lock",
  "finish",
]) {
  writeFileSync(new URL(cue + ".wav", directory), render(cue));
}
for (let tier = 0; tier <= 5; tier++) {
  writeFileSync(
    new URL("dive-" + tier + ".wav", directory),
    render("dive", tier),
  );
}
