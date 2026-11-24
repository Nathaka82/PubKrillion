import { DatabaseSync } from "node:sqlite";
import { readFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { randomBytes, randomInt, randomUUID, createHash } from "node:crypto";
import {
  clean,
  demand,
  normal,
  Fault,
  resolveAnswer,
  validateQuestion,
} from "./rules.ts";
import { catalogue, catalogueQuestions } from "./catalogue.ts";
import type { Suggestion } from "./semantic.ts";
import type { Question } from "./rules.ts";
import type { SQLInputValue } from "node:sqlite";
import { points, metres } from "../public/scoring.js";

type Row = Record<string, any>;
const teamNameOrder = new Intl.Collator("en-GB", {
  sensitivity: "base",
  numeric: true,
});
export const digest = (v: string) =>
  createHash("sha256").update(v).digest("hex");
export const secret = () => randomBytes(24).toString("base64url");
const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
const makeCode = (n: number) =>
  Array.from({ length: n }, () => alphabet[randomInt(alphabet.length)]).join(
    "",
  );
const pointScale = (version: number) =>
  version === 1 ? [0, 1, 2, 4, 7, 10] : points;
const releasedHashes: Record<string, string> = JSON.parse(
  readFileSync(
    new URL("../data/catalogue-v1-hashes.json", import.meta.url),
    "utf8",
  ),
);
let catalogueById: Map<string, Question> | undefined;
function releasedQuestion(id: string) {
  catalogueById ??= new Map(
    catalogue
      .flatMap((pack) => catalogueQuestions(pack.id))
      .map((q) => [q.id, validateQuestion(q, q.id)]),
  );
  return (
    catalogueById.get(id) ?? catalogueById.get(id.slice(id.indexOf("-") + 1))
  );
}
const receipt = (raw: string, canonical: string, revision: number) => ({
  raw,
  revision,
  ...(raw !== canonical ? { canonical } : {}),
});

export class UnmatchedGuess extends Fault {
  question: Question;
  raw: string;
  constructor(question: Question, raw: string) {
    super(422, "That answer isn’t in this category. Try another answer.");
    this.question = question;
    this.raw = raw;
  }
}
export class Game {
  db: DatabaseSync;
  seed: Question[];
  constructor(file: string) {
    this.seed = JSON.parse(
      readFileSync(new URL("../data/questions.json", import.meta.url), "utf8"),
    ).map((q: Question) => validateQuestion(q, q.id));
    if (file !== ":memory:")
      mkdirSync(dirname(resolve(file)), { recursive: true });
    this.db = new DatabaseSync(file);
    this.db.exec(`
      PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;
      CREATE TABLE IF NOT EXISTS rooms(code TEXT PRIMARY KEY, name TEXT NOT NULL, finished INTEGER DEFAULT 0, created INTEGER NOT NULL, current INTEGER, version INTEGER DEFAULT 1);
      CREATE TABLE IF NOT EXISTS library(room TEXT REFERENCES rooms(code), id TEXT, content TEXT NOT NULL, revision INTEGER DEFAULT 1, PRIMARY KEY(room,id));
      CREATE TABLE IF NOT EXISTS rounds(id INTEGER PRIMARY KEY, room TEXT REFERENCES rooms(code), seq INTEGER NOT NULL, question TEXT NOT NULL, snapshot TEXT NOT NULL, phase TEXT NOT NULL, deadline INTEGER, UNIQUE(room,seq));
      CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY, room TEXT REFERENCES rooms(code), name TEXT NOT NULL, namekey TEXT NOT NULL, token TEXT NOT NULL, transfer TEXT, expires INTEGER, UNIQUE(room,namekey));
      CREATE TABLE IF NOT EXISTS submissions(round INTEGER REFERENCES rounds(id), team INTEGER REFERENCES teams(id), raw TEXT NOT NULL, key TEXT NOT NULL, canonical TEXT, depth INTEGER, revision INTEGER NOT NULL, PRIMARY KEY(round,team));
      CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS rulings(id INTEGER PRIMARY KEY, round INTEGER REFERENCES rounds(id), key TEXT NOT NULL, canonical TEXT NOT NULL, depth INTEGER NOT NULL, created INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS join_receipts(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS rulings_by_key ON rulings(round,key,id);
      CREATE INDEX IF NOT EXISTS rounds_room ON rounds(room,seq);
      CREATE INDEX IF NOT EXISTS round_titles ON rounds(room,phase,seq,id,json_extract(snapshot,'$.title'));
      CREATE INDEX IF NOT EXISTS question_plays ON rounds(room,question);
      CREATE INDEX IF NOT EXISTS question_summaries ON library(room,id,json_extract(content,'$.title'),json_extract(content,'$.topic'),json_extract(content,'$.prompt'),revision);
      CREATE INDEX IF NOT EXISTS teams_room ON teams(room);
    `);
    this.tx(() => {
      if (this.one("PRAGMA user_version")!.user_version < 1) {
        this.db.exec(`
          ALTER TABLE rooms ADD COLUMN score_version INTEGER NOT NULL DEFAULT 2;
          ALTER TABLE rounds ADD COLUMN last_team_id INTEGER;
          UPDATE rooms SET score_version=1 WHERE finished=1;
          UPDATE rounds SET last_team_id=COALESCE((SELECT MAX(id) FROM teams WHERE room=rounds.room),0) WHERE phase='locked';
          UPDATE rooms SET version=version+1;
          PRAGMA user_version=1;
        `);
      }
      if (this.one("PRAGMA user_version")!.user_version < 2) {
        this.db.exec(
          "ALTER TABLE rounds ADD COLUMN revealed_at INTEGER; PRAGMA user_version=2;",
        );
      }
      if (this.one("PRAGMA user_version")!.user_version < 3) {
        const allergens = this.seed.find((q) => q.id === "allergens")!;
        this.run(
          "UPDATE library SET content=?,revision=revision+1 WHERE id='allergens' AND revision=1 AND room IN (SELECT code FROM rooms WHERE finished=0)",
          JSON.stringify(allergens),
        );
        this.db.exec("PRAGMA user_version=3");
      }
      if (this.one("PRAGMA user_version")!.user_version < 4) {
        if (this.one("SELECT 1 FROM rooms LIMIT 1")) this.upgradeCatalogue();
        this.db.exec("PRAGMA user_version=4");
      }
    });
  }
  private upgradeCatalogue() {
    const old = releasedHashes;
    for (const [id, hash] of Object.entries(old)) {
      const released = releasedQuestion(id);
      if (!released) continue;
      const canonical = released.id;
      for (const row of this.all(
        "SELECT room,content,revision FROM library WHERE id=?",
        id,
      )) {
        const original = digest(row.content) === hash;
        if (id === canonical) {
          if (original)
            this.run(
              "UPDATE library SET content=?, revision=revision+1 WHERE room=? AND id=?",
              JSON.stringify(released),
              row.room,
              id,
            );
          continue;
        }
        const primary = this.one(
          "SELECT content,revision FROM library WHERE room=? AND id=?",
          row.room,
          canonical,
        );
        const mergedContent = original
          ? JSON.stringify(released)
          : JSON.stringify({ ...JSON.parse(row.content), id: canonical });
        // When both versions were edited, retain the second host-authored variant
        // and its own play history.
        if (
          !original &&
          primary &&
          digest(primary.content) !== old[canonical] &&
          primary.content !== JSON.stringify(released) &&
          primary.content !== mergedContent
        ) {
          continue;
        }
        if (!original || !primary) {
          this.run(
            "INSERT INTO library(room,id,content,revision) VALUES(?,?,?,?) ON CONFLICT(room,id) DO UPDATE SET content=excluded.content,revision=excluded.revision",
            row.room,
            canonical,
            mergedContent,
            Math.max(primary?.revision ?? 0, row.revision) + 1,
          );
        }
        this.run(
          "UPDATE rounds SET question=? WHERE room=? AND question=?",
          canonical,
          row.room,
          id,
        );
        this.run("DELETE FROM library WHERE room=? AND id=?", row.room, id);
      }
    }
    this.db.exec("UPDATE rooms SET version=version+1");
  }
  all(sql: string, ...args: SQLInputValue[]): Row[] {
    return this.db.prepare(sql).all(...args);
  }
  one(sql: string, ...args: SQLInputValue[]): Row | undefined {
    return this.db.prepare(sql).get(...args);
  }
  run(sql: string, ...args: SQLInputValue[]) {
    return this.db.prepare(sql).run(...args);
  }
  tx<T>(fn: () => T): T {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const out = fn();
      this.db.exec("COMMIT");
      return out;
    } catch (e) {
      this.db.exec("ROLLBACK");
      throw e;
    }
  }
  bump(code: string) {
    this.run("UPDATE rooms SET version=version+1 WHERE code=?", code);
  }
  create(name: unknown) {
    const title = clean(name, 70, "event name");
    let code = makeCode(6);
    while (this.one("SELECT code FROM rooms WHERE code=?", code))
      code = makeCode(6);
    return this.tx(() => {
      this.run(
        "INSERT INTO rooms(code,name,created) VALUES(?,?,?)",
        code,
        title,
        Date.now(),
      );
      for (const q of this.seed)
        this.run(
          "INSERT INTO library(room,id,content) VALUES(?,?,?)",
          code,
          q.id,
          JSON.stringify(q),
        );
      return { code };
    });
  }
  room(code: string) {
    const r = this.one("SELECT * FROM rooms WHERE code=?", code);
    demand(r, 404, "Room not found. Check the six-character code.");
    if (
      r.current &&
      this.one(
        "SELECT 1 FROM rounds WHERE id=? AND phase='open' AND deadline<=?",
        r.current,
        Date.now(),
      )
    ) {
      this.tx(() => this.lock(code, r.current));
    }
    return r;
  }
  deleteRoom(code: string, confirmation: unknown) {
    const room = this.one("SELECT name FROM rooms WHERE code=?", code);
    demand(room, 404, "Room not found.");
    demand(
      confirmation === room.name,
      400,
      "Type the room name exactly as shown.",
    );
    const rounds = "SELECT id FROM rounds WHERE room=?";
    this.run("DELETE FROM rulings WHERE round IN (" + rounds + ")", code);
    this.run("DELETE FROM submissions WHERE round IN (" + rounds + ")", code);
    this.run("DELETE FROM rounds WHERE room=?", code);
    this.run(
      "DELETE FROM join_receipts WHERE token IN (SELECT token FROM teams WHERE room=?)",
      code,
    );
    this.run("DELETE FROM teams WHERE room=?", code);
    this.run("DELETE FROM library WHERE room=?", code);
    this.run("DELETE FROM rooms WHERE code=?", code);
    for (const key of this.banks.keys())
      if (key.startsWith(code + "/")) this.banks.delete(key);
    return { ok: true };
  }
  round(code: string, id: number) {
    const r = this.one(
      "SELECT rounds.id,rounds.room,seq,question,phase,deadline,last_team_id,revealed_at,rooms.score_version FROM rounds JOIN rooms ON rooms.code=rounds.room WHERE rounds.id=? AND room=?",
      id,
      code,
    );
    demand(r, 404, "Round not found.");
    return r;
  }
  lock(code: string, id: number) {
    // Team IDs increase as teams join. Capture the eligible roster exactly once.
    if (
      this.run(
        "UPDATE rounds SET phase='locked',last_team_id=COALESCE((SELECT MAX(id) FROM teams WHERE room=?),0) WHERE id=? AND room=? AND phase='open'",
        code,
        id,
        code,
      ).changes
    )
      this.bump(code);
  }
  join(r: Row, body: any) {
    let token = secret();
    if (body.retryKey !== undefined) {
      demand(
        typeof body.retryKey === "string" &&
          /^[0-9a-f]{64}$/.test(body.retryKey),
        400,
        "Invalid join receipt.",
      );
      const intent = body.transfer
        ? ["transfer", clean(body.transfer, 14, "transfer code").toUpperCase()]
        : ["name", normal(clean(body.name, 32, "team name", 2))];
      token = digest(JSON.stringify(["join", r.code, intent, body.retryKey]));
      this.run("DELETE FROM join_receipts WHERE expires<=?", Date.now());
      if (
        this.one(
          "SELECT 1 FROM teams t JOIN join_receipts j ON j.token=t.token WHERE t.room=? AND t.token=?",
          r.code,
          digest(token),
        )
      )
        return token;
    }
    demand(!r.finished, 409, "This quiz has finished.");
    if (body.transfer) {
      const code = clean(body.transfer, 14, "transfer code").toUpperCase();
      const t = this.run(
        "UPDATE teams SET token=?,transfer=NULL,expires=NULL WHERE room=? AND transfer=? AND expires>?",
        digest(token),
        r.code,
        digest(code),
        Date.now(),
      );
      demand(t.changes, 400, "That transfer code is invalid or has expired.");
    } else {
      const name = clean(body.name, 32, "team name", 2);
      const key = normal(name);
      demand(key, 400, "Include a letter or number in your team name.");
      demand(
        !this.one(
          "SELECT id FROM teams WHERE room=? AND namekey=?",
          r.code,
          key,
        ),
        409,
        "That team name is taken. Choose another, or ask the host for a transfer code.",
      );
      this.run(
        "INSERT INTO teams(room,name,namekey,token) VALUES(?,?,?,?)",
        r.code,
        name,
        key,
        digest(token),
      );
    }
    if (body.retryKey)
      this.run(
        "INSERT OR REPLACE INTO join_receipts VALUES(?,?)",
        digest(token),
        Date.now() + 600000,
      );
    this.bump(r.code);
    return token;
  }
  transfer(code: string, id: number) {
    const now = Date.now();
    let transfer = makeCode(5);
    while (
      this.one(
        "SELECT 1 FROM teams WHERE room=? AND transfer=? AND expires>?",
        code,
        digest(transfer),
        now,
      )
    )
      transfer = makeCode(5);
    demand(
      this.run(
        "UPDATE teams SET transfer=?,expires=? WHERE room=? AND id=?",
        digest(transfer),
        now + 600000,
        code,
        id,
      ).changes,
      404,
      "Team not found.",
    );
    return { transfer };
  }
  library(code: string) {
    return this.all(
      "SELECT l.id,json_extract(content,'$.title') AS title,json_extract(content,'$.topic') AS topic,json_extract(content,'$.prompt') AS prompt,l.revision,(SELECT COUNT(*) FROM rounds r WHERE r.room=l.room AND r.question=l.id) AS plays FROM library l INDEXED BY question_summaries WHERE room=? ORDER BY plays,l.rowid",
      code,
    );
  }
  private banks = new Map<string, Question>();
  roundBank(round: Row): Question {
    const key = round.room + "/" + round.id;
    let q = this.banks.get(key);
    if (!q) {
      q = JSON.parse(
        this.one(
          "SELECT snapshot FROM rounds WHERE id=? AND room=?",
          round.id,
          round.room,
        )!.snapshot,
      ) as Question;
      this.banks.set(key, q);
      if (this.banks.size > 4)
        this.banks.delete(this.banks.keys().next().value!);
    }
    return q;
  }
  question(code: string, id: string) {
    const row = this.one(
      "SELECT * FROM library WHERE room=? AND id=?",
      code,
      id,
    );
    demand(row, 404, "Question not found.");
    const q = JSON.parse(row.content);
    q.matching ??=
      this.seed.find((seed) => seed.id === id)?.matching ?? "categories";
    return { ...q, revision: row.revision };
  }
  save(code: string, id: string | undefined, body: any) {
    const matching =
      body.matching ?? (id ? this.question(code, id).matching : "categories");
    const q = validateQuestion({ ...body, matching }, id ?? randomUUID());
    if (id)
      demand(
        this.run(
          "UPDATE library SET content=?,revision=revision+1 WHERE room=? AND id=? AND revision=?",
          JSON.stringify(q),
          code,
          id,
          Number(body.revision) || 0,
        ).changes,
        409,
        "This question changed. Reopen the editor for the latest version.",
      );
    else
      this.run(
        "INSERT INTO library(room,id,content) VALUES(?,?,?)",
        code,
        q.id,
        JSON.stringify(q),
      );
    this.bump(code);
    return { id: q.id };
  }
  importPack(code: string, body: any, keepExisting = false) {
    demand(
      Array.isArray(body.questions) &&
        body.questions.length > 0 &&
        body.questions.length <= 1000,
      400,
      "Choose a pack with 1–1,000 questions.",
    );
    let added = 0;
    for (const [i, raw] of body.questions.entries()) {
      try {
        let id =
          raw?.id ??
          "pack-" +
            digest(JSON.stringify([raw?.topic, raw?.title, raw?.prompt])).slice(
              0,
              32,
            );
        demand(
          typeof id === "string" && /^[\w-]{1,120}$/.test(id),
          400,
          "Use letters, numbers, underscores or hyphens for the question ID.",
        );
        let content = JSON.stringify(validateQuestion(raw, id));
        const oldRelease =
          Object.hasOwn(releasedHashes, id) &&
          releasedHashes[id] === digest(content);
        if (oldRelease) {
          const updated = releasedQuestion(id);
          if (updated) {
            id = updated.id;
            content = JSON.stringify(updated);
          }
        }
        const old = this.one(
          "SELECT content FROM library WHERE room=? AND id=?",
          code,
          id,
        );
        if (old) {
          if (keepExisting || oldRelease) continue;
          const prior = JSON.parse(old.content);
          const matching =
            prior.matching ??
            this.seed.find((seed) => seed.id === id)?.matching ??
            "categories";
          const saved = prior.matching
            ? old.content
            : JSON.stringify(validateQuestion({ ...prior, matching }, id));
          demand(
            saved === content,
            409,
            "This ID already belongs to a different question. Give the imported question a new ID.",
          );
        } else {
          this.run(
            "INSERT INTO library(room,id,content) VALUES(?,?,?)",
            code,
            id,
            content,
          );
          added++;
        }
      } catch (e) {
        if (e instanceof Fault)
          throw new Fault(e.status, "Question " + (i + 1) + ": " + e.message);
        throw e;
      }
    }
    if (added) this.bump(code);
    return { added, skipped: body.questions.length - added };
  }
  start(r: Row, b: any) {
    demand(
      !r.finished && r.current === b.previous,
      409,
      "The room has moved on. Refresh the host console.",
    );
    const previous = r.current ? this.round(r.code, r.current) : null;
    demand(
      !previous || previous.phase === "revealed",
      409,
      "Reveal the current round first.",
    );
    const q = this.question(r.code, clean(b.questionId, 120, "question ID"));
    demand(
      Number.isInteger(b.seconds) && b.seconds >= 0 && b.seconds <= 3600,
      400,
      "Set a timer between 0 and 3,600 seconds.",
    );
    const out = this.run(
      "INSERT INTO rounds(room,seq,question,snapshot,phase,deadline) VALUES(?,?,?,?,'open',?)",
      r.code,
      (previous?.seq ?? 0) + 1,
      q.id,
      JSON.stringify(q),
      b.seconds ? Date.now() + 1000 * b.seconds : null,
    );
    const id = Number(out.lastInsertRowid);
    this.run(
      "UPDATE rooms SET current=?, version=version+1 WHERE code=?",
      id,
      r.code,
    );
    return { id };
  }
  submit(r: Row, round: Row, t: Row, b: any, semantic?: Suggestion) {
    const raw = clean(b.answer, 140, "answer");
    const key = normal(raw);
    demand(key, 400, "Enter an answer with letters or numbers.");
    const old = this.one(
      "SELECT raw,canonical,revision FROM submissions WHERE round=? AND team=? AND depth IS NOT NULL AND key NOT LIKE 'miss:%'",
      round.id,
      t.id,
    );
    if (old?.raw === raw) return receipt(raw, old.canonical, old.revision);
    demand(!old, 409, "Your team has already locked in an answer.");
    demand(
      !r.finished &&
        r.current === round.id &&
        round.phase === "open" &&
        (!round.deadline || Date.now() < round.deadline),
      409,
      "Submissions are closed for this round.",
    );
    demand(
      b.revision === 0,
      409,
      "Refresh the round to see your team’s answer.",
    );
    const q = this.roundBank(round);
    const found =
      semantic === undefined
        ? resolveAnswer(raw, q.answers, q.matching ?? "names")
        : undefined;
    if (found === null || semantic?.ambiguous)
      throw new Fault(422, "Can you be more specific?");
    if (!found) {
      if (semantic?.busy)
        throw new Fault(
          422,
          "The answer matcher is still working. Try this wording again in a moment, or enter a name.",
        );
      if (semantic?.name && q.answers.some((a) => a.name === semantic.name))
        throw new Fault(
          422,
          "Confirm the suggested answer or try another.",
          semantic.name,
        );
      throw new UnmatchedGuess(q, raw);
    }
    if (!found.exact)
      throw new Fault(
        422,
        "Confirm the suggested answer or try another.",
        found.answer.name,
      );
    const revision = 1;
    this.run(
      `INSERT INTO submissions(round,team,raw,key,canonical,depth,revision) VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(round,team) DO UPDATE SET raw=excluded.raw,key=excluded.key,canonical=excluded.canonical,depth=excluded.depth,revision=excluded.revision WHERE submissions.depth IS NULL`,
      round.id,
      t.id,
      raw,
      key,
      found.answer.name,
      found.answer.depth,
      revision,
    );
    this.bump(r.code);
    return receipt(raw, found.answer.name, revision);
  }
  judge(r: Row, round: Row, body: any) {
    demand(round.phase !== "open", 409, "Lock answers before judging.");
    const individual = body.teamId !== undefined;
    const key = individual
      ? "miss:" + body.teamId
      : clean(body.key, 180, "answer key");
    if (individual)
      demand(
        Number.isSafeInteger(body.teamId) && body.teamId > 0,
        400,
        "Choose a team.",
      );
    if (!individual)
      demand(!key.startsWith("miss:"), 400, "Choose the team for this ruling.");
    const group = individual
      ? this.one(
          "SELECT t.name AS raw FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=? WHERE t.room=? AND t.id=? AND t.id<=? AND (s.team IS NULL OR s.key=?)",
          round.id,
          r.code,
          Number(body.teamId),
          round.last_team_id,
          key,
        )
      : this.one(
          "SELECT raw FROM submissions WHERE round=? AND key=?",
          round.id,
          key,
        );
    demand(group, 404, "Answer not found.");
    let depth = body.depth;
    let name = clean(body.name || group.raw, 140, "ruling");
    const q = this.roundBank(round);
    const known = q.answers.find((a) =>
      [a.name, ...a.aliases].some((x) => normal(x) === normal(name)),
    );
    if (depth !== 0) {
      demand(known, 422, "Choose an accepted answer from this round.");
      name = known.name;
      depth = known.depth;
    }
    demand(
      Number.isInteger(depth) && depth >= 0 && depth <= 5,
      400,
      "Select a scoring depth.",
    );
    if (individual) {
      if (depth === 0) name = "Miss";
      demand(
        (this.one(
          "SELECT MAX(id) AS id FROM rulings WHERE round=? AND key=?",
          round.id,
          key,
        )?.id ?? 0) === body.previous,
        409,
        "This ruling changed. Reopen the round.",
      );
      this.run(
        "INSERT INTO submissions(round,team,raw,key,canonical,depth,revision) VALUES(?,?,'Miss',?,?,?,1) ON CONFLICT(round,team) DO UPDATE SET canonical=excluded.canonical,depth=excluded.depth WHERE submissions.key=excluded.key",
        round.id,
        body.teamId,
        key,
        depth ? name : "Miss",
        depth,
      );
    } else
      this.run(
        "UPDATE submissions SET canonical=?,depth=? WHERE round=? AND key=?",
        name,
        depth,
        round.id,
        key,
      );
    this.run(
      "INSERT INTO rulings(round,key,canonical,depth,created) VALUES(?,?,?,?,?)",
      round.id,
      key,
      name,
      depth,
      Date.now(),
    );
    this.bump(r.code);
  }
  results(round: Row) {
    const scale = pointScale(round.score_version);
    return this.all(
      `SELECT t.id AS teamId,t.name AS team,s.raw,
       CASE WHEN COALESCE(s.depth,0)=0 THEN 'Miss' ELSE s.canonical END AS canonical,
       COALESCE(s.depth,0) AS depth FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=?
       WHERE t.room=? AND (t.id<=? OR s.team IS NOT NULL) ORDER BY depth,canonical,t.name`,
      round.id,
      round.room,
      round.last_team_id,
    ).map((s) => ({ ...s, points: scale[s.depth], metres: metres[s.depth] }));
  }
  board(code: string, current: number | null, scale: number[]) {
    const scoreSql =
      "CASE s.depth " +
      scale.map((value, tier) => "WHEN " + tier + " THEN " + value).join(" ") +
      " ELSE 0 END";
    const entries = this.all(
      `SELECT t.id,t.name,
      COALESCE(SUM(CASE WHEN r.phase='revealed' THEN ${scoreSql} ELSE 0 END),0) AS total,
      COALESCE(MAX(CASE WHEN r.phase='revealed' AND r.id=? THEN ${scoreSql} ELSE 0 END),0) AS gain
      FROM teams t LEFT JOIN submissions s ON s.team=t.id LEFT JOIN rounds r ON r.id=s.round WHERE t.room=? GROUP BY t.id ORDER BY total DESC,t.name COLLATE NOCASE,t.id`,
      current,
      code,
    );
    const prior = entries.map((x) => x.total - x.gain).sort((a, b) => b - a);
    let rank = 0;
    return entries.map((x, i) => {
      if (!i || x.total !== entries[i - 1].total) rank = i + 1;
      return {
        ...x,
        rank,
        movement: prior.indexOf(x.total - x.gain) + 1 - rank,
      };
    });
  }
  review(round: Row) {
    return this.all(
      "SELECT key,MIN(raw) AS raw,canonical,depth,COUNT(*) AS count FROM submissions WHERE round=? AND depth IS NOT NULL AND key NOT LIKE 'miss:%' GROUP BY key ORDER BY raw",
      round.id,
    );
  }
  roundRoster(round: Row) {
    return this.all(
      `SELECT t.id, t.name, (s.depth IS NOT NULL AND s.key NOT LIKE 'miss:%') AS submitted
       FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=?
       WHERE t.room=? AND (? IS NULL OR t.id<=?)`,
      round.id,
      round.room,
      round.last_team_id,
      round.last_team_id,
    )
      .map((t) => ({ id: t.id, name: t.name, submitted: Boolean(t.submitted) }))
      .sort((a, b) => teamNameOrder.compare(a.name, b.name) || a.id - b.id);
  }
  detail(round: Row, host: boolean) {
    const q = this.roundBank(round);
    return {
      id: round.id,
      seq: round.seq,
      title: q.title,
      topic: q.topic,
      prompt: q.prompt,
      phase: round.phase,
      pointValues: pointScale(round.score_version),
      deadline: round.deadline,
      ...(round.phase === "revealed"
        ? { results: this.results(round), revealedAt: round.revealed_at }
        : {}),
      ...(host
        ? {
            answerCount: q.answers.length,
            ...(q.answers.length <= 80 ? { accepted: q.answers } : {}),
          }
        : {}),
      ...(host && round.phase !== "open"
        ? {
            review: this.review(round),
            missedTeams: this.all(
              `SELECT t.id,t.name,s.canonical,COALESCE(s.depth,0) AS depth,
                COALESCE((SELECT MAX(id) FROM rulings WHERE round=? AND key='miss:'||t.id),0) AS previous
               FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=?
               WHERE t.room=? AND t.id<=? AND (s.team IS NULL OR s.key='miss:'||t.id)
               ORDER BY t.name`,
              round.id,
              round.id,
              round.room,
              round.last_team_id,
            ),
            rulings: this.all(
              `SELECT COALESCE(t.name,r.key) AS key,r.canonical,r.depth,r.created FROM rulings r
               LEFT JOIN teams t ON r.key LIKE 'miss:%' AND t.id=CAST(substr(r.key,6) AS INTEGER)
               WHERE r.round=? ORDER BY r.id DESC`,
              round.id,
            ),
          }
        : {}),
    };
  }
  state(r: Row, host: boolean, t?: Row) {
    const round = r.current ? this.round(r.code, r.current) : null;
    const mine =
      t && round
        ? this.one(
            "SELECT raw,canonical,revision FROM submissions WHERE round=? AND team=? AND depth IS NOT NULL AND key NOT LIKE 'miss:%'",
            round.id,
            t.id,
          )
        : null;
    const history = this.all(
      "SELECT id,seq,json_extract(snapshot,'$.title') AS title FROM rounds WHERE room=? AND phase='revealed' ORDER BY seq DESC",
      r.code,
    );
    const roster = host && round ? this.roundRoster(round) : null;
    return {
      room: { code: r.code, name: r.name, finished: !!r.finished },
      round: round ? this.detail(round, host) : null,
      history,
      board: this.board(r.code, round?.id ?? null, pointScale(r.score_version)),
      team: t ? { id: t.id, name: t.name } : null,
      mine: mine ? receipt(mine.raw, mine.canonical, mine.revision) : null,
      ...(t && round
        ? {
            eligibleForRound:
              round.last_team_id == null || t.id <= round.last_team_id,
          }
        : {}),
      ...(roster
        ? {
            roster,
            received: roster.filter((t) => t.submitted).length,
            eligible: roster.length,
          }
        : {}),
      serverTime: Date.now(),
    };
  }
}
