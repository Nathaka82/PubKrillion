// Both playback paths use these pitches, envelopes and timings.
export function score(cue, tier, now, note) {
  function bell(time, hz, level = 0.16, decay = 0.45) {
    note(time, hz, decay, level);
    note(time, hz * 2.01, decay * 0.35, level * 0.22);
  }
  switch (cue) {
    case "preview":
      bell(now, 660);
      break;
    case "open":
      bell(now, 392, 0.17, 0.58);
      bell(now + 0.14, 587.33, 0.15, 0.65);
      bell(now + 0.35, 587.33, 0.035, 0.42);
      break;
    case "saved":
      note(now, 420, 0.12, 0.14, 590);
      bell(now + 0.09, 783.99, 0.16, 0.34);
      break;
    case "tick":
      note(now, 690, 0.095, 0.09, 490);
      break;
    case "lock":
      note(now, 349.23, 0.14, 0.13, 261.63, "triangle");
      bell(now + 0.09, 196, 0.15, 0.32);
      break;
    case "dive": {
      if (!tier) {
        note(now, 185, 0.25, 0.12, 146.83);
        break;
      }
      const root = [0, 440, 392, 329.63, 293.66, 220][tier];
      if (!root) break;
      note(now, root * 1.35, 0.45, 0.13, root);
      bell(now + 0.09, root * 2, 0.15, 0.55);
      bell(now + 0.25, root * 2, 0.045, 0.4);
      if (tier >= 4) bell(now + 0.19, root * 3, 0.1, 0.55);
      if (tier === 5) bell(now + 0.3, root * 4, 0.1, 0.7);
      break;
    }
    case "finish":
      [392, 493.88, 587.33, 783.99].forEach((hz, i) =>
        bell(now + i * 0.11, hz, 0.16, i === 3 ? 0.72 : 0.38),
      );
      break;
  }
}
