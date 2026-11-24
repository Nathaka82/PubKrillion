import {
  createServer,
  type IncomingMessage,
  type ServerResponse,
} from "node:http";
import { readFile, stat } from "node:fs/promises";
import { dirname, resolve, extname, sep } from "node:path";
import { fileURLToPath } from "node:url";
import { timingSafeEqual } from "node:crypto";
import QRCode from "qrcode";
import { catalogue, catalogueQuestions, previewPack } from "./catalogue.ts";
import { publishWebAssets } from "./web-assets.ts";
import { SemanticMatcher } from "./semantic.ts";
import { Game, UnmatchedGuess, digest, secret } from "./store.ts";
import { Fault, demand, answerPage } from "./rules.ts";

const password = process.env.HOST_PASSWORD;
if (password === undefined)
  throw new Error("Set HOST_PASSWORD in .env or run npm run setup.");
const hostPasswordDigest = Buffer.from(digest(password), "hex");
const dbPath = process.env.DB_PATH || "storage/quiz.sqlite";
const game = new Game(dbPath);
const semantic = new SemanticMatcher(dbPath);
const publicDir = fileURLToPath(new URL("../public/", import.meta.url));
const origin = process.env.PUBLIC_ORIGIN
  ? new URL(process.env.PUBLIC_ORIGIN).origin
  : "";
const frontend = process.env.FRONTEND_URL
  ? new URL(process.env.FRONTEND_URL)
  : null;
if (
  frontend &&
  (frontend.search ||
    frontend.hash ||
    frontend.username ||
    frontend.password ||
    (frontend.protocol !== "https:" &&
      !(
        process.env.NODE_ENV === "test" &&
        frontend.protocol === "http:" &&
        frontend.hostname === "127.0.0.1"
      )))
)
  throw Error("FRONTEND_URL must be the HTTPS Pages site URL.");
if (frontend && !frontend.pathname.endsWith("/")) frontend.pathname += "/";
function splitFrontend(req: IncomingMessage) {
  return (
    !!frontend &&
    frontend.origin !== (origin || "http://" + req.headers.host) &&
    req.headers.origin === frontend.origin
  );
}
const production = process.env.NODE_ENV === "production";
if (production && !origin.startsWith("https://"))
  throw new Error("Production needs an HTTPS PUBLIC_ORIGIN.");
const revision = process.env.APP_REVISION || "local";
const assets = production
  ? await publishWebAssets(
      publicDir,
      resolve(dirname(dbPath), "web-assets"),
      revision,
    )
  : null;
const mime: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".ttf": "font/ttf",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".wav": "audio/wav",
  ".txt": "text/plain; charset=utf-8",
};
const attempts = new Map<string, { n: number; until: number }>();
const expireLimits = setInterval(() => {
  for (const [k, v] of attempts) if (v.until <= Date.now()) attempts.delete(k);
}, 60000);
expireLimits.unref();
function limit(key: string, max: number, millis: number) {
  let item = attempts.get(key);
  if (!item || item.until <= Date.now()) {
    item = { n: 0, until: Date.now() + millis };
    attempts.set(key, item);
  }
  demand(
    ++item.n <= max,
    429,
    "Too many attempts. Try again in a few minutes.",
  );
}
function cookie(req: IncomingMessage, name: string) {
  if (splitFrontend(req))
    return (
      /^Bearer ([A-Za-z0-9_-]+)$/.exec(req.headers.authorization ?? "")?.[1] ??
      ""
    );
  return (
    (req.headers.cookie ?? "")
      .split(";")
      .map((x) => x.trim())
      .find((x) => x.startsWith(name + "="))
      ?.slice(name.length + 1) ?? ""
  );
}
function setSession(
  res: ServerResponse,
  name: string,
  value: string,
  age: number,
) {
  res.setHeader(
    "Set-Cookie",
    name +
      "=" +
      value +
      "; Path=/; HttpOnly; SameSite=Lax; Max-Age=" +
      age +
      (origin.startsWith("https:") ? "; Secure" : ""),
  );
}
function isHost(req: IncomingMessage) {
  return !!game.one(
    "SELECT 1 FROM sessions WHERE token=? AND expires>?",
    digest(cookie(req, "dive_host")),
    Date.now(),
  );
}
function requireHost(req: IncomingMessage) {
  demand(isHost(req), 401, "Sign in to the host console.");
}
function send(res: ServerResponse, data: unknown, status = 200) {
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
  });
  res.end(JSON.stringify(data));
}
async function bodyOf(req: IncomingMessage, maxBytes: number) {
  demand(
    req.headers["content-type"]?.split(";")[0] === "application/json",
    415,
    "Send a JSON request.",
  );
  const chunks: Buffer[] = [];
  let n = 0;
  for await (const chunk of req) {
    n += chunk.length;
    demand(
      n <= maxBytes,
      413,
      "File is too large. Split the pack into smaller files.",
    );
    chunks.push(chunk);
  }
  try {
    const b = JSON.parse(Buffer.concat(chunks).toString() || "{}");
    demand(
      b && typeof b === "object" && !Array.isArray(b),
      400,
      "Invalid request.",
    );
    return b;
  } catch {
    throw new Fault(400, "Invalid JSON request.");
  }
}
const server = createServer(async (req, res) => {
  try {
    res.setHeader("X-Content-Type-Options", "nosniff");
    res.setHeader("Referrer-Policy", "same-origin");
    res.setHeader(
      "Content-Security-Policy",
      "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'",
    );
    const url = new URL(req.url ?? "/", "http://localhost");
    const path = url.pathname,
      method = req.method || "GET";
    if (path.startsWith("/api/") && splitFrontend(req)) {
      res.setHeader("Vary", "Origin");
      res.setHeader("Access-Control-Allow-Origin", frontend!.origin);
      res.setHeader("Access-Control-Expose-Headers", "ETag, X-Quiz-Session");
      if (method === "OPTIONS") {
        res.setHeader(
          "Access-Control-Allow-Headers",
          "Authorization, Content-Type, If-None-Match",
        );
        res.setHeader(
          "Access-Control-Allow-Methods",
          "GET, POST, PUT, DELETE, OPTIONS",
        );
        res.writeHead(204);
        res.end();
        return;
      }
    }
    if (path === "/healthz") {
      game.one("SELECT 1");
      send(res, { ok: true, revision });
      return;
    }
    if (!path.startsWith("/api/")) {
      demand(method === "GET" || method === "HEAD", 405, "Method unavailable.");
      const entry = path === "/" || path === "/index.html";
      if (entry && assets) {
        res.writeHead(200, {
          "Content-Type": mime[".html"],
          "Content-Length": assets.html.length,
          "Cache-Control": "no-store",
        });
        res.end(method === "HEAD" ? undefined : assets.html);
        return;
      }
      const versioned = assets && /^\/assets\/([a-f0-9]{64})(\/.*)$/.exec(path);
      const root = versioned
        ? resolve(assets!.store, versioned[1])
        : resolve(publicDir);
      const requested = decodeURIComponent(
        versioned ? versioned[2] : entry ? "/index.html" : path,
      );
      const target = resolve(root, "." + requested);
      demand(
        target.startsWith(root + sep) &&
          mime[extname(target)] &&
          !(versioned && extname(target) === ".html"),
        404,
        "Page not found.",
      );
      const file = await stat(target).catch(() => null);
      demand(file && file.isFile(), 404, "Page not found.");
      res.writeHead(200, {
        "Content-Type": mime[extname(target)],
        "Content-Length": file.size,
        "Cache-Control": entry
          ? "no-store"
          : versioned
            ? "public, max-age=31536000, immutable"
            : "no-cache",
      });
      res.end(method === "HEAD" ? undefined : await readFile(target));
      return;
    }
    const base = origin || "http://" + req.headers.host;
    if (method !== "GET" && req.headers.origin)
      demand(
        req.headers.origin === base || splitFrontend(req),
        403,
        "This request came from another origin.",
      );
    const bankWrite =
      (method === "POST" || method === "PUT") &&
      /^\/api\/rooms\/[A-Z2-9]{6}\/(?:packs|library(?:\/[\w-]+)?)$/.test(path);
    if (bankWrite) requireHost(req);
    const body =
      method === "GET"
        ? {}
        : await bodyOf(req, bankWrite ? 16 * 1024 * 1024 : 524288);
    const ip =
      process.env.TRUST_PROXY === "1"
        ? String(req.headers["x-forwarded-for"] ?? req.socket.remoteAddress)
            .split(",")
            .at(-1)!
            .trim()
        : String(req.socket.remoteAddress);
    if (path === "/api/login" && method === "POST") {
      limit("login:" + ip, 15, 300000);
      demand(
        typeof body.password === "string" &&
          timingSafeEqual(
            Buffer.from(digest(body.password), "hex"),
            hostPasswordDigest,
          ),
        401,
        "Incorrect host password.",
      );
      attempts.delete("login:" + ip);
      const token = secret();
      game.run("DELETE FROM sessions WHERE expires<=?", Date.now());
      game.run(
        "INSERT INTO sessions VALUES(?,?)",
        digest(token),
        Date.now() + 7 * 86400000,
      );
      if (splitFrontend(req)) res.setHeader("X-Quiz-Session", token);
      else setSession(res, "dive_host", token, 604800);
      send(res, { ok: true });
      return;
    }
    if (path === "/api/logout" && method === "POST") {
      game.run(
        "DELETE FROM sessions WHERE token=?",
        digest(cookie(req, "dive_host")),
      );
      if (!splitFrontend(req)) setSession(res, "dive_host", "", 0);
      send(res, { ok: true });
      return;
    }
    if (path === "/api/packs/sample" && method === "GET") {
      requireHost(req);
      res.setHeader(
        "Content-Disposition",
        'attachment; filename="pub-sampler.json"',
      );
      send(
        res,
        JSON.parse(
          await readFile(
            new URL("../data/pub-sampler.json", import.meta.url),
            "utf8",
          ),
        ),
      );
      return;
    }
    const catalogueRoute = path.match(/^\/api\/catalogue(?:\/([a-z]+))?$/);
    if (catalogueRoute && method === "GET") {
      requireHost(req);
      send(res, catalogueRoute[1] ? previewPack(catalogueRoute[1]) : catalogue);
      return;
    }
    if (path === "/api/rooms") {
      requireHost(req);
      if (method === "POST") {
        send(res, game.create(body.name), 201);
        return;
      }
      if (method === "GET") {
        send(res, {
          rooms: game.all(
            "SELECT code,name,finished,created,(SELECT COUNT(*) FROM teams t WHERE t.room=rooms.code) AS teams FROM rooms ORDER BY created DESC",
          ),
        });
        return;
      }
    }
    const m = path.match(
      /^\/api\/rooms\/([A-Z2-9]{6})(?:\/([a-z]+)(?:\/([\w-]+))?(?:\/([a-z]+))?)?$/,
    );
    demand(m, 404, "Room or route not found.");
    const [, code, action, id, op] = m;
    const r = game.room(code);
    const teamCookie = "dive_team_" + code;
    const team = game.one(
      "SELECT id,name FROM teams WHERE room=? AND token=?",
      code,
      digest(cookie(req, teamCookie)),
    );
    if (action === "info" && method === "GET") {
      send(res, { name: r.name, finished: !!r.finished, joined: !!team });
      return;
    }
    if (action === "join" && method === "POST") {
      if (team && !body.transfer) {
        send(res, { ok: true });
        return;
      }
      limit("join:" + ip + code, 150, 60000);
      if (body.transfer) limit("recover:" + ip + code, 20, 300000);
      const token = game.tx(() => game.join(r, body));
      if (splitFrontend(req)) res.setHeader("X-Quiz-Session", token);
      else setSession(res, teamCookie, token, 30 * 86400);
      send(res, { ok: true });
      return;
    }
    const hostView = url.searchParams.get("view") === "host";
    if (action === "state" && method === "GET") {
      if (hostView) requireHost(req);
      else demand(team, 401, "Join or recover your team to continue.");
      const fresh = game.one("SELECT version FROM rooms WHERE code=?", code)!;
      const etag =
        '"' + fresh.version + "-" + (hostView ? "host" : team!.id) + '"';
      res.setHeader("ETag", etag);
      if (req.headers["if-none-match"] === etag) {
        res.writeHead(304, { "Cache-Control": "no-store" });
        res.end();
        return;
      }
      const state = game.state(r, hostView, hostView ? undefined : team);
      if (state.round?.phase === "open")
        semantic.prepare(game.roundBank(game.round(code, state.round.id)));
      send(res, state);
      return;
    }
    if (
      op === "answers" &&
      id &&
      method === "GET" &&
      (action === "rounds" || action === "library")
    ) {
      requireHost(req);
      const q =
        action === "rounds"
          ? game.roundBank(game.round(code, Number(id)))
          : game.question(code, id);
      send(res, answerPage(q, url.searchParams));
      return;
    }
    if (action === "rounds" && id && !op && method === "GET") {
      if (hostView) requireHost(req);
      else demand(team, 401, "Join the room first.");
      const round = game.round(code, Number(id));
      demand(
        hostView || round.phase === "revealed",
        403,
        "Results are available after reveal.",
      );
      send(res, game.detail(round, hostView));
      return;
    }
    if (action === "rounds" && op === "answer" && method === "POST") {
      demand(team, 401, "Rejoin or recover your team to answer.");
      const round = game.round(code, Number(id));
      try {
        send(
          res,
          game.tx(() => game.submit(r, round, team, body)),
        );
      } catch (error) {
        if (!(error instanceof UnmatchedGuess)) throw error;
        const suggestion = await semantic.suggest(error.question, error.raw);
        // No transaction spans model inference. A lock, transfer or concurrent
        // submission can change who is eligible while we await the worker.
        const fresh = game.room(code),
          current = game.round(code, Number(id));
        const authenticated = game.one(
          "SELECT id FROM teams WHERE room=? AND token=?",
          code,
          digest(cookie(req, teamCookie)),
        );
        demand(
          authenticated?.id === team.id,
          401,
          "Rejoin or recover your team to answer.",
        );
        send(
          res,
          game.tx(() => game.submit(fresh, current, team, body, suggestion)),
        );
      }
      return;
    }
    requireHost(req);
    if (action === "library" && id && op === "prepare" && method === "POST") {
      semantic.prepare(game.question(code, id));
      send(res, { ok: true });
      return;
    }
    if (action === "qr" && method === "GET") {
      const png = await QRCode.toBuffer(
        (splitFrontend(req) ? frontend!.href : base + "/") + "#/join/" + code,
        {
          width: 300,
          margin: 3,
          color: { dark: "#071525", light: "#ffffff" },
        },
      );
      res.writeHead(200, {
        "Content-Type": "image/png",
        "Cache-Control": "private, no-store",
      });
      res.end(png);
      return;
    }
    if (action === "library" && method === "GET") {
      send(res, id ? game.question(code, id) : game.library(code));
      return;
    }
    const result = game.tx(() => {
      if (action === "packs" && id && !op && method === "POST" && !r.finished)
        return game.importPack(
          code,
          { questions: catalogueQuestions(id) },
          true,
        );
      if (action === "packs" && !id && method === "POST" && !r.finished)
        return game.importPack(code, body);
      if (!action && method === "DELETE")
        return game.deleteRoom(code, body.name);
      if (action === "transfer" && method === "POST" && !r.finished)
        return game.transfer(code, Number(body.teamId));
      if (
        action === "library" &&
        !r.finished &&
        ((method === "POST" && !id) || (method === "PUT" && id))
      )
        return game.save(code, id, body);
      if (action === "start" && method === "POST") return game.start(r, body);
      if (action === "finish" && method === "POST") {
        demand(
          r.current === body.previous &&
            (!r.current || game.round(code, r.current).phase === "revealed"),
          409,
          "Reveal the current round before finishing.",
        );
        game.run(
          "UPDATE rooms SET finished=1,version=version+1 WHERE code=?",
          code,
        );
        return { ok: true };
      }
      if (action === "rounds" && id && method === "POST") {
        const round = game.round(code, Number(id));
        if (op === "judge") {
          game.judge(r, round, body);
          return { ok: true };
        }
        demand(
          !r.finished && r.current === round.id,
          409,
          "The room has moved on.",
        );
        if (op === "lock") {
          demand(
            round.phase !== "revealed",
            409,
            "This round has already been revealed.",
          );
          game.lock(code, round.id);
          return { ok: true };
        } else if (op === "extend") {
          demand(
            round.phase === "open" &&
              round.deadline &&
              round.deadline === body.previous &&
              round.deadline > Date.now(),
            409,
            "The timer has changed or expired.",
          );
          game.run(
            "UPDATE rounds SET deadline=deadline+30000 WHERE id=?",
            round.id,
          );
        } else if (op === "reveal") {
          demand(round.phase !== "open", 409, "Lock the answers first.");
          if (round.phase === "revealed") return { ok: true };
          game.run(
            "UPDATE rounds SET phase='revealed',revealed_at=? WHERE id=?",
            Date.now(),
            round.id,
          );
        } else throw new Fault(404, "Unknown round action.");
        game.bump(code);
        return { ok: true };
      }
      throw new Fault(404, "Action unavailable.");
    });
    if (action === "start" && result && "id" in result)
      semantic.prepare(game.roundBank(game.round(code, Number(result.id))));
    send(res, result);
  } catch (e) {
    if (!(e instanceof Fault)) console.error(e);
    if (!res.headersSent)
      send(
        res,
        {
          ...(e instanceof Fault && e.suggestion
            ? { suggestion: e.suggestion }
            : {}),
          error:
            e instanceof Fault
              ? e.message
              : "Something went wrong saving this request. Please retry.",
        },
        e instanceof Fault ? e.status : 500,
      );
    else res.end();
  }
});
server.requestTimeout = 20000;
server.listen(
  Number(process.env.PORT || 3000),
  process.env.BIND_ADDRESS || "127.0.0.1",
  () => {
    const addr = server.address();
    console.log(
      "Krill Club listening on " +
        (typeof addr === "object" && addr
          ? addr.address + ":" + addr.port
          : addr),
    );
  },
);
function stop() {
  server.close(() => {
    void semantic.close();
    game.db.close();
    process.exit(0);
  });
  server.closeIdleConnections();
}
process.on("SIGTERM", stop);
process.on("SIGINT", stop);
