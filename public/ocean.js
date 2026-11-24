// A low-resolution ocean, painted on an integer grid and enlarged with nearest-neighbour sampling.
const TAU = Math.PI * 2;

function seeded(seed) {
  return () => {
    seed ^= seed << 13;
    seed ^= seed >>> 17;
    seed ^= seed << 5;
    return (seed >>> 0) / 4294967296;
  };
}

function rect(ctx, x, y, width, height, colour) {
  ctx.fillStyle = colour;
  ctx.fillRect(
    Math.round(x),
    Math.round(y),
    Math.round(width),
    Math.round(height),
  );
}

function line(ctx, x, y, endX, endY, colour) {
  x = Math.round(x);
  y = Math.round(y);
  endX = Math.round(endX);
  endY = Math.round(endY);
  const dx = Math.abs(endX - x),
    dy = -Math.abs(endY - y);
  const sx = x < endX ? 1 : -1,
    sy = y < endY ? 1 : -1;
  let error = dx + dy;
  ctx.fillStyle = colour;
  for (;;) {
    ctx.fillRect(x, y, 1, 1);
    if (x === endX && y === endY) break;
    const doubled = error * 2;
    if (doubled >= dy) {
      error += dy;
      x += sx;
    }
    if (doubled <= dx) {
      error += dx;
      y += sy;
    }
  }
}

function polygon(ctx, points, colour) {
  const top = Math.min(...points.map((p) => p[1]));
  const bottom = Math.max(...points.map((p) => p[1]));
  ctx.fillStyle = colour;
  for (let y = Math.floor(top); y < bottom; y++) {
    const cuts = [];
    const centre = y + 0.5;
    for (let i = 0; i < points.length; i++) {
      const [x1, y1] = points[i],
        [x2, y2] = points[(i + 1) % points.length];
      if ((y1 <= centre && y2 > centre) || (y2 <= centre && y1 > centre)) {
        cuts.push(x1 + ((centre - y1) * (x2 - x1)) / (y2 - y1));
      }
    }
    cuts.sort((a, b) => a - b);
    for (let i = 0; i < cuts.length; i += 2) {
      const x = Math.ceil(cuts[i] - 0.5);
      ctx.fillRect(x, y, Math.ceil(cuts[i + 1] - 0.5) - x, 1);
    }
  }
}

function sprite(width, height, painter) {
  const sheet = document.createElement("canvas");
  sheet.width = width;
  sheet.height = height;
  painter(sheet.getContext("2d"));
  return sheet;
}

function paintKrill(c) {
  const dark = "#812650",
    mid = "#c83c66",
    coral = "#fc607c",
    rose = "#ff8e99",
    light = "#ffd0bd";
  // Antennae, rostrum and a segmented abdomen that curls into the tail fan.
  [
    [2, 12, 4, 7],
    [4, 7, 9, 3],
    [9, 3, 15, 2],
    [15, 2, 19, 3],
    [3, 13, 7, 9],
    [7, 9, 13, 6],
    [13, 6, 20, 6],
  ].forEach((p) => line(c, ...p, coral));
  polygon(
    c,
    [
      [3, 12],
      [9, 12],
      [11, 9],
      [18, 8],
      [24, 10],
      [28, 14],
      [29, 18],
      [27, 21],
      [23, 24],
      [17, 25],
      [13, 23],
      [17, 21],
      [20, 21],
      [23, 18],
      [21, 15],
      [15, 15],
      [12, 18],
      [8, 17],
      [7, 14],
    ],
    dark,
  );
  polygon(
    c,
    [
      [4, 12],
      [10, 12],
      [11, 10],
      [18, 9],
      [23, 10],
      [27, 13],
      [28, 17],
      [26, 20],
      [22, 23],
      [18, 24],
      [14, 22],
      [20, 21],
      [24, 17],
      [22, 14],
      [16, 14],
      [12, 16],
      [8, 15],
    ],
    coral,
  );
  polygon(
    c,
    [
      [10, 12],
      [12, 10],
      [17, 9],
      [20, 10],
      [17, 11],
      [16, 13],
      [12, 13],
    ],
    rose,
  );
  polygon(
    c,
    [
      [12, 22],
      [11, 19],
      [14, 20],
      [17, 22],
      [16, 24],
      [12, 25],
      [10, 24],
      [13, 23],
      [8, 23],
      [9, 21],
    ],
    coral,
  );
  [
    [18, 9, 18, 11],
    [21, 11, 20, 13],
    [24, 12, 22, 14],
    [27, 14, 24, 16],
    [27, 17, 25, 17],
    [25, 20, 23, 19],
    [22, 22, 21, 21],
    [15, 16, 14, 19],
    [12, 16, 11, 19],
    [10, 16, 8, 18],
  ].forEach((p) => line(c, ...p, mid));
  [
    [12, 10, 16, 9],
    [21, 10, 23, 11],
    [25, 12, 27, 14],
    [17, 23, 19, 23],
    [9, 25, 13, 24],
  ].forEach((p) => line(c, ...p, rose));
  rect(c, 8, 11, 3, 3, "#321d3a");
  rect(c, 8, 11, 1, 1, light);
  line(c, 7, 15, 3, 17, rose);
  line(c, 10, 17, 7, 20, coral);
}

export function drawKrill(canvas) {
  if (!canvas) return;
  canvas.width = 32;
  canvas.height = 28;
  canvas.style.imageRendering = "pixelated";
  const ctx = canvas.getContext("2d");
  if (ctx) paintKrill(ctx);
}

export function mountAmbientKrill(water) {
  // Keep a few swimmers in the narrow channels beside the cards on phones.
  const shoal = [
    [2, 22, 18],
    [4, 24, 12],
    [97, 41, 20],
    [99, 44, 13],
    [2, 72, 16],
    [96, 89, 15],
    [18, 11, 14],
    [30, 61, 17],
    [67, 33, 13],
    [82, 77, 20],
  ];
  shoal.forEach(([x, y, size], i) => {
    const krill = document.createElement("canvas");
    drawKrill(krill);
    krill.style.left = x + "%";
    krill.style.top = y + "%";
    krill.style.width = size + "px";
    krill.style.setProperty("--facing", x > 50 ? -1 : 1);
    krill.style.setProperty(
      "--drift",
      (x > 50 ? -1 : 1) * (4 + (i % 5)) + "px",
    );
    krill.style.animationDuration = 10 + (i % 6) + "s";
    krill.style.animationDelay = -i * 2.3 + "s";
    water.append(krill);
  });
  const pauseWhenHidden = () =>
    water.classList.toggle("paused", document.hidden);
  document.addEventListener("visibilitychange", pauseWhenHidden);
  pauseWhenHidden();
}

function ship(c) {
  const hull = "#25314c",
    shadow = "#17283e",
    steel = "#8491a1",
    mist = "#b9c5c0";
  // Mast, pennant, roof antenna and suspended rigging.
  line(c, 16, 8, 16, 27, steel);
  line(c, 16, 10, 5, 25, "#697789");
  line(c, 16, 13, 29, 23, "#858595");
  rect(c, 15, 6, 2, 2, "#f9d491");
  rect(c, 12, 14, 9, 1, "#9c9fad");
  polygon(
    c,
    [
      [17, 8],
      [25, 8],
      [23, 10],
      [27, 11],
      [21, 12],
      [17, 11],
    ],
    "#ed667a",
  );
  rect(c, 17, 8, 5, 1, "#ffa299");
  rect(c, 38, 9, 1, 8, steel);
  rect(c, 37, 8, 3, 1, "#e5b6a0");
  rect(c, 42, 11, 1, 5, steel);
  // Aft gantry and deck crates.
  rect(c, 4, 21, 2, 9, hull);
  rect(c, 4, 20, 8, 2, steel);
  rect(c, 11, 21, 1, 7, steel);
  rect(c, 8, 26, 5, 3, "#987858");
  rect(c, 9, 26, 3, 1, "#c59b70");
  rect(c, 18, 24, 6, 5, hull);
  rect(c, 18, 24, 6, 2, "#e29372");
  // The wheelhouse has individual amber-lit windows and a stepped windscreen.
  polygon(
    c,
    [
      [26, 18],
      [29, 14],
      [40, 14],
      [44, 17],
      [46, 21],
      [49, 22],
      [49, 27],
      [25, 27],
    ],
    mist,
  );
  rect(c, 28, 14, 13, 2, "#e6d3b4");
  rect(c, 27, 16, 17, 1, "#727e8d");
  rect(c, 28, 18, 4, 4, shadow);
  rect(c, 33, 18, 4, 4, shadow);
  rect(c, 38, 18, 3, 4, shadow);
  rect(c, 29, 18, 3, 2, "#ffd789");
  rect(c, 34, 18, 3, 2, "#ffa56d");
  rect(c, 39, 18, 2, 2, "#ffd789");
  line(c, 42, 18, 44, 21, "#48708a");
  rect(c, 25, 23, 23, 2, "#7b8590");
  rect(c, 25, 25, 25, 3, "#c7ac95");
  rect(c, 36, 23, 4, 5, hull);
  rect(c, 37, 23, 2, 2, "#efbb81");
  // Foredeck rail, two navigation lights, coral hull stripe and waterline.
  rect(c, 49, 24, 12, 1, "#c5b9ab");
  [49, 53, 57, 60].forEach((x) => rect(c, x, 25, 1, 4, steel));
  polygon(
    c,
    [
      [1, 27],
      [25, 27],
      [29, 28],
      [49, 28],
      [54, 26],
      [65, 25],
      [61, 31],
      [55, 35],
      [10, 35],
      [4, 32],
    ],
    shadow,
  );
  polygon(
    c,
    [
      [2, 27],
      [25, 27],
      [29, 28],
      [50, 28],
      [55, 26],
      [64, 25],
      [61, 29],
      [54, 31],
      [7, 31],
    ],
    "#ce656e",
  );
  line(c, 3, 27, 24, 27, "#f8b3a0");
  line(c, 54, 26, 63, 25, "#ffb097");
  rect(c, 10, 31, 44, 2, hull);
  rect(c, 12, 31, 14, 1, "#68758b");
  [29, 33, 37].forEach((x) => rect(c, x, 29, 2, 1, "#ffe298"));
  rect(c, 4, 25, 1, 1, "#ff9b8c");
  rect(c, 60, 24, 1, 1, "#b5efd8");
}

function cloud(c, x, y, width, height, bright) {
  const contour = [
    [0, 0.83],
    [0.06, 0.68],
    [0.19, 0.67],
    [0.23, 0.4],
    [0.32, 0.37],
    [0.37, 0.16],
    [0.5, 0.14],
    [0.56, 0.32],
    [0.65, 0.34],
    [0.73, 0.56],
    [0.83, 0.57],
    [0.87, 0.71],
    [0.96, 0.76],
    [1, 0.94],
    [0.87, 1],
    [0.44, 0.97],
    [0.17, 1],
  ];
  polygon(
    c,
    contour.map(([px, py]) => [x + px * width, y + py * height]),
    bright ? "#393855" : "#252c49",
  );
  const bottom = Math.floor(y + height);
  rect(
    c,
    x + width * 0.1,
    bottom - 1,
    width * 0.72,
    1,
    bright ? "#65506b" : "#3d3c59",
  );
  rect(
    c,
    x + width * 0.35,
    bottom + 2,
    width * 0.39,
    1,
    bright ? "#4f4365" : "#313550",
  );
}

function ridge(c, w, base, amplitude, random, colour, roughness) {
  const step = Math.max(4, Math.round(w / roughness));
  const shape = [[0, base + 1]];
  let elevation = random() * 0.5;
  for (let x = 0; x <= w + step; x += step) {
    elevation = elevation * 0.42 + random() * 0.58;
    const island = 0.7 + 0.3 * Math.cos((x / w) * TAU * 2.5);
    shape.push([x, base - Math.round(elevation * amplitude * island)]);
  }
  shape.push([w + step, base + 1]);
  polygon(c, shape, colour);
}

function paintSky(c, w, water, random) {
  const grad = c.createLinearGradient(0, 0, 0, water);
  grad.addColorStop(0, "#0b1229");
  grad.addColorStop(0.5, "#1b2746");
  grad.addColorStop(0.83, "#47415e");
  grad.addColorStop(1, "#ab747d");
  c.fillStyle = grad;
  c.fillRect(0, 0, w, water);
  for (let i = 0; i < (w * water) / 145; i++) {
    const x = Math.floor(random() * w),
      y = Math.floor(random() * water * 0.85);
    rect(
      c,
      x,
      y,
      1,
      1,
      random() > 0.84 ? "#e0dbce" : random() > 0.5 ? "#9caac0" : "#626d8b",
    );
    if (random() > 0.988 && y < water * 0.6) {
      rect(c, x - 1, y, 3, 1, "#adbfd1");
      rect(c, x, y - 1, 1, 3, "#c5d2d9");
    }
  }
  const moonX = Math.round(w * 0.845),
    moonY = Math.round(water * 0.24);
  const moonSize = Math.max(5, Math.min(11, Math.round(water * 0.11)));
  for (let y = -moonSize; y <= moonSize; y++)
    for (let x = -moonSize; x <= moonSize; x++) {
      if (
        x * x + y * y > moonSize * moonSize ||
        (x - moonSize * 0.47) ** 2 + (y + moonSize * 0.23) ** 2 <
          moonSize * moonSize * 0.72
      )
        continue;
      rect(
        c,
        moonX + x,
        moonY + y,
        1,
        1,
        y > moonSize * 0.55 ? "#b48f9f" : "#f6d4b9",
      );
    }
  [
    [-0.06, 0.44, 0.21, 0.14],
    [0.21, 0.51, 0.14, 0.12],
    [0.51, 0.28, 0.18, 0.13],
    [0.76, 0.64, 0.14, 0.094],
    [0.94, 0.36, 0.12, 0.1],
  ].forEach(([x, y, cw, ch], i) =>
    cloud(
      c,
      x * w,
      y * water,
      cw * w,
      Math.max(5, ch * water),
      i === 1 || i === 3,
    ),
  );
  ridge(c, w, water - 2, Math.max(4, water * 0.15), random, "#48415c", 31);
  ridge(c, w, water, Math.max(3, water * 0.095), random, "#30354f", 48);
  // Small harbour lights on a far shore, set back from the boat.
  for (let i = 0; i < Math.max(3, w / 75); i++)
    rect(c, random() * w, water - 2, 1, 1, "#d2a289");
}

function daylightCloud(c, x, y, width, height, flipped) {
  // Paint each horizontal scanline of overlapping cloud lobes, with sunlight from the upper right.
  const lobes = [
    [0.13, 0.69, 0.13, 0.2],
    [0.26, 0.58, 0.17, 0.34],
    [0.43, 0.46, 0.18, 0.45],
    [0.56, 0.55, 0.15, 0.32],
    [0.69, 0.65, 0.15, 0.26],
    [0.8, 0.74, 0.15, 0.16],
    [0.91, 0.8, 0.09, 0.1],
  ];
  c.save();
  c.translate(Math.round(x + (flipped ? width : 0)), Math.round(y));
  if (flipped) c.scale(-1, 1);
  for (const [cx, cy, rx, ry] of lobes) {
    const mid = cy * height,
      radius = ry * height;
    for (let row = Math.floor(mid - radius); row <= mid + radius; row++) {
      const ny = (row + 0.5 - mid) / radius;
      if (Math.abs(ny) > 1) continue;
      const half = rx * width * Math.sqrt(1 - ny * ny),
        left = Math.round(cx * width - half),
        right = Math.round(cx * width + half);
      const shade =
        ny > 0.65
          ? "#c5dce7"
          : ny > 0.36
            ? "#dcebf0"
            : ny < -0.33
              ? "#fffcf1"
              : "#f7f7eb";
      rect(c, left, row, right - left, 1, shade);
    }
  }
  rect(c, width * 0.23, height * 0.91, width * 0.52, 1, "#c5dce7");
  c.restore();
}

function paintDaylight(c, w, water, random) {
  const sky = c.createLinearGradient(0, 0, 0, water);
  sky.addColorStop(0, "#68b6d9");
  sky.addColorStop(0.4, "#81c7e1");
  sky.addColorStop(0.75, "#b7dfec");
  sky.addColorStop(1, "#edf7f7");
  c.fillStyle = sky;
  c.fillRect(0, 0, w, water);
  const sunX = Math.round(w * 0.842),
    sunY = Math.round(water * 0.56);
  const radius = Math.max(
    3,
    Math.min(15, Math.round(Math.min(w * 0.035, water * 0.085))),
  );
  const halo = c.createRadialGradient(
    sunX,
    sunY,
    radius * 0.5,
    sunX,
    sunY,
    radius * 2.8,
  );
  halo.addColorStop(0, "rgba(255,231,159,.85)");
  halo.addColorStop(0.45, "rgba(255,231,166,.43)");
  halo.addColorStop(1, "rgba(255,239,190,0)");
  c.fillStyle = halo;
  c.fillRect(sunX - radius * 3, sunY - radius * 3, radius * 6, radius * 6);
  for (let dy = -radius; dy <= radius; dy++) {
    const dx = Math.floor(Math.sqrt(radius * radius - dy * dy));
    rect(
      c,
      sunX - dx,
      sunY + dy,
      dx * 2 + 1,
      1,
      dy > radius * 0.65 ? "#ffdd95" : "#ffeaac",
    );
  }
  for (let i = 0; i < w / 12; i++)
    rect(c, random() * w, random() * water, 1, 1, "rgba(241,253,248,.36)");
  [
    [-0.13, 0.63, 0.22, 0.1],
    [0.06, 0.42, 0.22, 0.13],
    [0.22, 0.7, 0.23, 0.13],
    [0.48, 0.81, 0.16, 0.09],
    [0.56, 0.18, 0.13, 0.08],
    [0.68, 0.66, 0.1, 0.065],
    [0.91, 0.79, 0.2, 0.12],
  ].forEach(([x, y, width, height], i) => {
    daylightCloud(
      c,
      x * w,
      y * water,
      width * w,
      Math.max(5, height * water, width * w * 0.14),
      i === 0 || i === 4,
    );
  });
}

function kelp(c, x, y, length, random, colour, highlight) {
  const tilt = (random() - 0.5) * 0.2;
  let lastX = x,
    lastY = y;
  for (let n = 1; n <= length; n++) {
    const stem = Math.round(x + Math.sin(n * 0.22 + x) * 1.5 + n * tilt);
    line(c, lastX, lastY, stem, y - n, colour);
    lastX = stem;
    lastY = y - n;
    if (n % 5 === 1 && n + 3 < length) {
      const direction = n % 2 === 0 ? 1 : -1,
        span = 2 + Math.round(random() * 2);
      polygon(
        c,
        [
          [stem, y - n + 1],
          [stem + direction * span, y - n - 1],
          [stem + direction * (span + 1), y - n - 4],
          [stem + direction, y - n - 2],
        ],
        colour,
      );
      if (highlight && n % 3 === 0)
        line(c, stem, y - n, stem + direction * 2, y - n - 1, highlight);
    }
  }
}

function seabed(c, w, h, water, random) {
  const depth = h - water;
  const profile = (x) =>
    h -
    Math.round(
      (0.045 +
        0.095 * Math.cos((x / w) * Math.PI * 2 - 0.12) ** 6 +
        0.025 * Math.sin(x * 0.081) +
        0.012 * Math.cos(x * 0.16)) *
        depth,
    );
  const shape = [[0, h]];
  for (let x = 0; x <= w + 3; x += 3) shape.push([x, profile(x)]);
  shape.push([w + 3, h]);
  polygon(c, shape, "#071a29");
  for (let i = 0; i < Math.max(25, w * 0.13); i++) {
    const x = Math.floor(random() * w),
      y = profile(x) + Math.floor(random() * 8);
    const size = 1 + Math.floor(random() * 5);
    polygon(
      c,
      [
        [x - size, y],
        [x - size + 2, y - size],
        [x + 1, y - size - 1],
        [x + size, y - 2],
        [x + size + 1, y],
      ],
      "#122e39",
    );
    if (i % 3 === 0) rect(c, x - 1, y - size, 2, 1, "#21505a");
  }
  for (let i = 0; i < Math.max(18, w * 0.07); i++) {
    const edge = random() ** 1.9 * 0.3,
      x = Math.round(w * (i % 2 ? edge : 1 - edge));
    kelp(
      c,
      x,
      profile(x) + 2,
      Math.max(4, Math.round((0.065 + random() * 0.215) * depth)),
      random,
      i % 3 === 0 ? "#114554" : "#0a3445",
      i % 3 === 0 ? "#1d6570" : null,
    );
  }
}

function waterScene(c, w, h, water, random, deep, daylight) {
  const depth = h - water;
  const gradient = c.createLinearGradient(0, water, 0, h);
  const colours = daylight
    ? ["#285f84", "#214e70", "#12334d", "#0b2237", "#071828"]
    : [
        deep ? "#164958" : "#177a86",
        deep ? "#103e51" : "#10586d",
        "#0c344d",
        "#0a243a",
        "#0a1d30",
      ];
  [0, 0.13, 0.48, 0.82, 1].forEach((offset, i) =>
    gradient.addColorStop(offset, colours[i]),
  );
  c.fillStyle = gradient;
  c.fillRect(0, water, w, depth);
  for (let i = 0; i < 11; i++) {
    const x = random() * w,
      spread = (0.02 + random() * 0.06) * w;
    polygon(
      c,
      [
        [x, water],
        [x + spread, water],
        [x - depth * 0.37, water + depth * 0.8],
        [x - depth * 0.4, water + depth * 0.74],
      ],
      "rgba(100,221,212,0.034)",
    );
  }
  for (let i = 0; i < (w * depth) / 110; i++) {
    const x = random() * w,
      y = random() ** 1.8;
    rect(
      c,
      x,
      water + depth * y,
      1 + (y < 0.15 ? random() * 4 : 0),
      1,
      y < 0.15 ? "rgba(87,187,179,.29)" : "rgba(110,186,192,.21)",
    );
  }
  if (!deep) {
    rect(c, 0, water, w, 1, daylight ? "#658faa" : "#69ccbf");
    rect(c, 0, water + 1, w, 1, daylight ? "#376887" : "#359e9d");
    for (let i = 0; i < w / 7; i++) {
      const y = water + 1 + random() ** 2 * depth * 0.16;
      rect(
        c,
        random() * w,
        y,
        1 + random() * 9,
        1,
        random() > 0.65
          ? daylight
            ? "#6995a9"
            : "#62aaad"
          : daylight
            ? "#407795"
            : "#338a99",
      );
    }
    for (let i = 0; i < 15; i++) {
      const layer = random();
      rect(
        c,
        w * 0.845 + (random() - 0.5) * (5 + layer * 16),
        water + 2 + layer * depth * 0.13,
        2 + random() * 4,
        1,
        "rgba(223,195,169,.42)",
      );
    }
  }
  const reefBase = h - Math.round(depth * 0.07);
  ridge(c, w, reefBase, depth * 0.26, random, "#0b2c3e", 28);
  rect(c, 0, reefBase, w, h - reefBase, "#0b2c3e");
  for (let i = 0; i < w / 16; i++) {
    const x = random() * w;
    kelp(
      c,
      x,
      reefBase,
      Math.max(3, depth * (0.1 + random() * 0.19)),
      random,
      "#103849",
      null,
    );
  }
  seabed(c, w, h, water, random);
}

function fish(c, x, y, size, direction, palette) {
  c.save();
  c.translate(Math.round(x), Math.round(y));
  c.scale(direction * size, size);
  polygon(
    c,
    [
      [-6, -1],
      [-4, -2],
      [1, -2],
      [3, -1],
      [5, 0],
      [3, 1],
      [0, 2],
      [-4, 1],
      [-6, 0],
      [-9, 2],
      [-9, -4],
    ],
    palette[0],
  );
  rect(c, -3, -2, 4, 1, palette[1]);
  rect(c, 2, -1, 1, 1, palette[2]);
  c.restore();
}

const schools = [
  [0.19, 0.28, 5, 1],
  [0.83, 0.47, 4, -1],
  [0.41, 0.66, 3, -1],
];

export function mountOcean(canvas, options = {}) {
  if (!canvas) return () => {};
  const target = canvas.getContext("2d");
  if (!target) return () => {};
  const deep = options.mode === "deep";
  const daylight = options.mode === "daylight";
  const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
  const vessel = sprite(66, 36, ship),
    shrimp = sprite(32, 28, paintKrill);
  const frame = document.createElement("canvas");
  const ctx = frame.getContext("2d");
  canvas.style.imageRendering = "pixelated";
  let backing,
    w = 0,
    h = 0,
    water = 0,
    shipScale = 1,
    near = true,
    timer = 0,
    tick = 0,
    disposed = false;

  function paint() {
    if (!backing || disposed) return;
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(backing, 0, 0);
    const t = tick * 0.15,
      depth = h - water;
    if (!deep) {
      const boatX = Math.round(w * (daylight ? 0.72 : 0.69)),
        boatY = water + 1 + Math.round(Math.sin(t * 0.65) * 0.8);
      const width = Math.round(66 * shipScale),
        height = Math.round(36 * shipScale);
      ctx.drawImage(
        vessel,
        boatX - Math.round(width / 2),
        boatY - height,
        width,
        height,
      );
      for (let n = 0; n < 7; n++) {
        const drift = Math.round(Math.sin(t * 0.44 + n) * 3);
        rect(
          ctx,
          boatX - width * 0.43 + n * width * 0.14 + drift,
          boatY + 1 + (n % 3),
          3 + (n % 4),
          1,
          n % 2 ? "#91bab2" : "#578f98",
        );
      }
    }
    schools.forEach(([cx, cy, count, dir], index) => {
      const unit = w < 240 ? 0.72 : 1;
      for (let n = 0; n < count; n++) {
        const x =
          w * cx + dir * (n % 3) * 13 * unit + Math.sin(t * 0.23 + index) * 5;
        const y =
          water +
          depth * cy +
          Math.floor(n / 3) * 7 +
          (n % 3) * 2 +
          Math.sin(t * 0.5 + n) * 1.1;
        fish(
          ctx,
          x,
          y,
          unit,
          dir,
          index === 0
            ? ["#153e51", "#376773", "#8cafa7"]
            : ["#0c2a3c", "#1d4756", "#46717b"],
        );
      }
    });
    for (let n = 0; n < 14; n++) {
      const run = 0.12 + ((n * 0.347 + t * 0.018) % 0.66);
      const x = Math.round(
          w * ((n * 0.271 + 0.11) % 1) + Math.sin(t * 0.38 + n) * 1.4,
        ),
        y = Math.round(h - run * depth);
      if (n % 4 === 0) {
        rect(ctx, x, y, 2, 1, "#417580");
        rect(ctx, x - 1, y + 1, 1, 1, "#34646f");
      } else rect(ctx, x, y, 1, 1, "#365f70");
    }
    const krillSize = Math.max(7, Math.min(14, Math.round(depth * 0.105)));
    for (let n = 0; n < 3; n++) {
      const x = Math.round(
          w * (0.58 + n * 0.028) + Math.sin(t * 0.4 + n) * 1.2,
        ),
        y = Math.round(
          water + depth * (0.67 + (n % 2) * 0.05) + Math.sin(t * 0.6 + n) * 1.2,
        );
      ctx.drawImage(shrimp, x, y, krillSize, Math.round(krillSize * 0.875));
    }
    target.imageSmoothingEnabled = false;
    target.drawImage(frame, 0, 0, canvas.width, canvas.height);
  }

  function resize() {
    const box = canvas.getBoundingClientRect();
    if (!box.width || !box.height) return;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const outW = Math.max(1, Math.round(box.width * dpr)),
      outH = Math.max(1, Math.round(box.height * dpr));
    if (backing && outW === canvas.width && outH === canvas.height) return;
    canvas.width = outW;
    canvas.height = outH;
    const pixelSize = Math.max(2, box.width / 550, box.height / 250);
    w = Math.max(1, Math.round(box.width / pixelSize));
    h = Math.max(1, Math.round(box.height / pixelSize));
    frame.width = w;
    frame.height = h;
    water = deep ? 0 : Math.max(1, Math.round(h * (daylight ? 0.65 : 0.47)));
    shipScale = Math.max(0.42, Math.min(1.35, w / 295, water / 48));
    backing = sprite(w, h, (c) => {
      const random = seeded(178031);
      if (daylight) paintDaylight(c, w, water, random);
      else if (!deep) paintSky(c, w, water, random);
      waterScene(c, w, h, water, random, deep, daylight);
    });
    paint();
  }
  function animate() {
    timer = 0;
    if (disposed || !near || document.hidden || motion.matches) return;
    tick++;
    paint();
    timer = window.setTimeout(animate, 150);
  }
  function sync() {
    window.clearTimeout(timer);
    timer = 0;
    if (motion.matches) {
      tick = 0;
      paint();
    } else if (near && !document.hidden) animate();
  }
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);
  const visibility = new IntersectionObserver((entries) => {
    near = entries[0].isIntersecting;
    sync();
  });
  visibility.observe(canvas);
  motion.addEventListener("change", sync);
  document.addEventListener("visibilitychange", sync);
  resize();
  sync();
  return () => {
    disposed = true;
    window.clearTimeout(timer);
    observer.disconnect();
    visibility.disconnect();
    motion.removeEventListener("change", sync);
    document.removeEventListener("visibilitychange", sync);
  };
}
