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
import { builtInQuestions, published } from "./catalogue.ts";
import { additionalAnswer, indexed, lookup, restoreRoundNames } from "./knowledge.ts";
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
const previousHashes: Record<string, string> = JSON.parse(
  readFileSync(
    new URL("../data/catalogue-v2-hashes.json", import.meta.url),
    "utf8",
  ),
);
function releasedQuestion(id: string) {
  return published.get(id) ?? published.get(id.slice(id.indexOf("-") + 1));
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
    super(422, indexed(question) && question.sourceJudged
      ? "The sources could not verify that answer for this question. Try a fuller name or another answer."
      : "We don’t recognise that answer. Try another spelling or ask the host to review it.");
    this.question = question;
    this.raw = raw;
  }
}
export class Game {
  db: DatabaseSync;
  constructor(file: string) {
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
      CREATE TABLE IF NOT EXISTS round_additions(round INTEGER REFERENCES rounds(id) ON DELETE CASCADE, key TEXT, name TEXT NOT NULL, depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 5), PRIMARY KEY(round,key));
      CREATE TABLE IF NOT EXISTS review_guesses(round INTEGER REFERENCES rounds(id) ON DELETE CASCADE, team INTEGER REFERENCES teams(id) ON DELETE CASCADE, raw TEXT NOT NULL, PRIMARY KEY(round,team));
      CREATE TABLE IF NOT EXISTS join_receipts(token TEXT PRIMARY KEY, expires INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS rulings_by_key ON rulings(round,key,id);
      CREATE INDEX IF NOT EXISTS rounds_room ON rounds(room,seq);
      CREATE INDEX IF NOT EXISTS round_titles ON rounds(room,phase,seq,id,json_extract(snapshot,'$.title'));
      CREATE INDEX IF NOT EXISTS question_plays ON rounds(room,question);
      CREATE INDEX IF NOT EXISTS question_picker ON library(room,id,json_extract(content,'$.title'),json_extract(content,'$.topic'),json_extract(content,'$.prompt'),json_extract(content,'$.context'),revision);
      CREATE INDEX IF NOT EXISTS teams_room ON teams(room);
    `);
    this.db.exec(readFileSync(new URL("./knowledge.sql", import.meta.url), "utf8"));
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
        const allergens = published.get("allergens")!;
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
      if (this.one("PRAGMA user_version")!.user_version < 5) {
        this.refreshReleasedQuestions(previousHashes);
        this.db.exec(`
          UPDATE rounds SET phase='locked',last_team_id=(SELECT MAX(id) FROM teams WHERE room=rounds.room)
          WHERE phase='open' AND EXISTS (SELECT 1 FROM teams WHERE room=rounds.room)
          AND NOT EXISTS (SELECT 1 FROM teams t WHERE t.room=rounds.room AND NOT EXISTS
            (SELECT 1 FROM submissions s WHERE s.team=t.id AND s.round=rounds.id AND s.depth IS NOT NULL AND s.key NOT LIKE 'miss:%'));
          UPDATE rooms SET version=version+1;
          PRAGMA user_version=5;
        `);
      }
      if (this.one("PRAGMA user_version")!.user_version < 6) {
        this.refreshReleasedQuestions(JSON.parse(readFileSync(
          new URL("../data/catalogue-v3-hashes.json", import.meta.url), "utf8",
        )));
        this.db.exec("DROP INDEX IF EXISTS question_summaries; UPDATE rooms SET version=version+1; PRAGMA user_version=6");
      }
      if (this.one("PRAGMA user_version")!.user_version < 7) {
        restoreRoundNames(this.db);
        this.db.exec("PRAGMA user_version=7");
      }
    });
  }
  private refreshReleasedQuestions(hashes: Record<string, string>) {
    for (const [id, previous] of Object.entries(hashes)) {
      const question = published.get(id);
      if (!question) continue;
      const content = JSON.stringify(question);
      if (digest(content) === previous) continue;
      for (const row of this.all("SELECT room,content FROM library WHERE id=?", id))
        if (digest(row.content) === previous)
          this.run("UPDATE library SET content=?,revision=revision+1 WHERE room=? AND id=?", content, row.room, id);
    }
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
    ) {
      this.bump(code);
      const cohort = this.one("SELECT json_extract(snapshot,'$.cohort') AS cohort FROM rounds WHERE id=?", id)?.cohort;
      if (cohort) this.calibrate(cohort);
    }
  }
  observe(round: Row, team: Row, choice: string | null) {
    const q = this.roundBank(round);
    if (indexed(q) && q.cohort)
      this.run("INSERT OR IGNORE INTO kb_pick(cohort,team,room,round,choice) VALUES(?,?,?,?,?)",
        q.cohort, team.id, round.room, round.id, choice);
  }
  private calibrate(cohort: string) {
    const sample = this.one(`SELECT count(*) n,count(DISTINCT p.room) rooms FROM kb_pick p
      JOIN rounds r ON r.id=p.round WHERE p.cohort=? AND p.choice IS NOT NULL AND r.phase<>'open'`, cohort)!;
    const prior = Number(this.one("SELECT population FROM kb_calibration WHERE cohort=?", cohort)?.population ?? 0);
    if (sample.rooms < 3 || sample.n < Math.max(200, prior * 2)) return;
    this.run("INSERT OR REPLACE INTO kb_calibration VALUES(?,?)", cohort, sample.n);
    for (const l of this.all(`SELECT l.room,l.id,l.revision,json_extract(content,'$.prompt') prompt
      FROM library l JOIN rooms r ON r.code=l.room WHERE r.finished=0 AND json_extract(content,'$.cohort')=?`, cohort)) {
      const job = randomUUID();
      this.run("INSERT INTO kb_job(id,room,prompt,research,status,created) VALUES(?,?,?,2,'queued',?)", job, l.room, l.prompt, Date.now());
      this.run("INSERT INTO kb_rebuild VALUES(?,?,?)", job, l.id, l.revision);
    }
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
    const entries = new Map<string, Row>();
    for (const { id, title, topic, prompt, context } of builtInQuestions.values())
      entries.set(id, { id, title, topic, prompt, context, revision: 1 });
    for (const row of this.all(
      "SELECT id,json_extract(content,'$.title') AS title,json_extract(content,'$.topic') AS topic,json_extract(content,'$.prompt') AS prompt,json_extract(content,'$.context') AS context,CASE WHEN id GLOB 'generated-*' THEN json_type(content,'$.knowledge') IS NOT NULL ELSE 0 END AS generated,revision FROM library INDEXED BY question_picker WHERE room=?",
      code,
    ))
      entries.set(row.id, row);
    const plays = new Map(
      this.all(
        "SELECT question,COUNT(*) AS n FROM rounds WHERE room=? GROUP BY question",
        code,
      ).map((row) => [row.question, row.n]),
    );
    const notices = new Map(this.all("SELECT entry,notice FROM kb_catalogue_entry WHERE room=?", code)
      .map(row => [row.entry, row.notice]));
    return [...entries.values()]
      .map((q) => ({ ...q, plays: plays.get(q.id) ?? 0, ...(notices.has(q.id) ? { sourceNotice: notices.get(q.id) } : {}) }))
      .sort((a, b) => a.plays - b.plays);
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
      "SELECT l.*,e.notice FROM library l LEFT JOIN kb_catalogue_entry e ON (e.room,e.entry)=(l.room,l.id) WHERE l.room=? AND l.id=?",
      code,
      id,
    );
    if (!row) {
      const builtin = builtInQuestions.get(id);
      demand(builtin, 404, "Question not found.");
      return { ...builtin, revision: 1 };
    }
    const q = JSON.parse(row.content);
    q.matching ??= published.get(id)?.matching ?? "categories";
    return { ...q, revision: row.revision, ...(row.notice ? { sourceNotice: row.notice } : {}) };
  }
  judgingBank(round: Row): Question {
    const q = this.roundBank(round);
    if (round.phase === "open") return q;
    const additions = this.all("SELECT name,depth FROM round_additions WHERE round=? ORDER BY name", round.id)
      .map(({ name, depth }) => ({ name, depth, aliases: [] }));
    return additions.length ? { ...q, answers: [...q.answers, ...additions] } : q;
  }
  save(code: string, id: string | undefined, body: any) {
    if (id) demand(!indexed(this.question(code,id)),409,"Generate a new edition to change a sourced question.");
    const matching =
      body.matching ?? (id ? this.question(code, id).matching : "categories");
    const q = validateQuestion({ ...body, matching }, id ?? randomUUID());
    if (id) {
      const revision = Number(body.revision) || 0;
      const content = JSON.stringify(q);
      const updated = this.run(
        "UPDATE library SET content=?,revision=revision+1 WHERE room=? AND id=? AND revision=?",
        content,
        code,
        id,
        revision,
      );
      // Revision one can also be the shared edition, with no room row yet.
      const firstEdit =
        !updated.changes &&
        revision === 1 &&
        builtInQuestions.has(id) &&
        this.run(
          "INSERT OR IGNORE INTO library(room,id,content,revision) VALUES(?,?,?,2)",
          code,
          id,
          content,
        ).changes;
      demand(
        updated.changes || firstEdit,
        409,
        "This question changed. Reopen the editor for the latest version.",
      );
    } else
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
        const hash = digest(content);
        const oldRelease =
          Object.hasOwn(releasedHashes, id) && releasedHashes[id] === hash;
        const previousRelease = previousHashes[id] === hash;
        if (oldRelease || previousRelease) {
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
        if (old || builtInQuestions.has(id)) {
          if (keepExisting || oldRelease || previousRelease) continue;
          const savedContent =
            old?.content ?? JSON.stringify(builtInQuestions.get(id));
          const prior = JSON.parse(savedContent);
          const matching =
            prior.matching ?? published.get(id)?.matching ?? "categories";
          const saved = prior.matching
            ? savedContent
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
    if ("sourceNotice" in q && q.sourceNotice) throw new Fault(409, q.sourceNotice);
    if (indexed(q) && b.revision !== undefined)
      demand("revision" in q && q.revision === b.revision,409,"The sources changed after preview. Reopen round setup to use the new edition.");
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
    demand(key || indexed(q), 400, "Enter an answer with letters or numbers.");
    let found =
      semantic === undefined
        ? (indexed(q) ? lookup(this.db, q.knowledge, raw) : resolveAnswer(raw, q.answers, q.matching ?? "names"))
        : undefined;
    let supplemental = false;
    if (indexed(q) && q.sourceJudged && found !== null && !found?.exact) {
      const proof = additionalAnswer(this.db, round.id, r.code, round.question, q, raw);
      if (proof) { found = proof; supplemental = true; }
    }
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
    if (indexed(q)) this.observe(round, t, supplemental || "shared" in found ? null : found.answer.name);
    this.run(
      `INSERT INTO submissions(round,team,raw,key,canonical,depth,revision) VALUES(?,?,?,?,?,?,?)
      ON CONFLICT(round,team) DO UPDATE SET raw=excluded.raw,key=excluded.key,canonical=excluded.canonical,depth=excluded.depth,revision=excluded.revision WHERE submissions.depth IS NULL`,
      round.id,
      t.id,
      raw,
      indexed(q) && q.sourceJudged && "entity" in found && found.entity
        ? "kb:" + found.entity : key || raw,
      found.answer.name,
      found.answer.depth,
      revision,
    );
    this.run("DELETE FROM review_guesses WHERE round=? AND team=?", round.id, t.id);
    this.run("DELETE FROM kb_appeal WHERE round=? AND team=?", round.id,t.id);
    this.run("DELETE FROM kb_review WHERE round=? AND team=?", round.id,t.id);
    const waiting = this.one(
      `SELECT 1 FROM teams t WHERE t.room=? AND NOT EXISTS (
        SELECT 1 FROM submissions s WHERE s.round=? AND s.team=t.id
          AND s.depth IS NOT NULL AND s.key NOT LIKE 'miss:%'
      ) LIMIT 1`,
      r.code,
      round.id,
    );
    if (waiting) this.bump(r.code);
    else this.lock(r.code, round.id);
    return receipt(raw, found.answer.name, revision);
  }
  judge(r: Row, round: Row, body: any) {
    const edition = this.roundBank(round);
    demand(!indexed(edition) || !edition.sourceJudged, 409, "The sources determine this round’s scores.");
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
    const q = this.judgingBank(round);
    const indexedMatch = indexed(q) ? lookup(this.db,q.knowledge,name) : undefined;
    const known = indexedMatch?.exact ? indexedMatch.answer : q.answers.find((a) =>
      [a.name, ...a.aliases].some((x) => normal(x) === normal(name)),
    );
    if (depth !== 0 && known) {
      name = known.name;
      depth = known.depth;
    } else if (depth !== 0) {
      if (indexed(q)) depth=2;
      demand(body.unlisted === true && body.name && normal(name), 422, "Choose a listed answer or award an unlisted answer.");
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
    if (depth > 0 && !known)
      this.run("INSERT INTO round_additions(round,key,name,depth) VALUES(?,?,?,?)", round.id, normal(name), name, depth);
    this.run(
      "INSERT INTO rulings(round,key,canonical,depth,created) VALUES(?,?,?,?,?)",
      round.id,
      key,
      name,
      depth,
      Date.now(),
    );
    if (individual) {
      const peers=this.all(`SELECT g.team,COALESCE(a.raw,g.raw) AS raw,json_extract(v.evidence,'$[0].entity') AS entity
        FROM review_guesses g LEFT JOIN kb_appeal a USING(round,team) LEFT JOIN kb_review v ON v.round=g.round AND v.team=g.team AND v.raw=COALESCE(a.raw,g.raw)
        WHERE g.round=? ORDER BY g.team`,round.id);
      const original=peers.find(peer=>peer.team===body.teamId);
      if (original) for (const peer of peers.filter(p=>p.team!==body.teamId && (normal(p.raw)===normal(original.raw) || original.entity && original.entity===p.entity))) {
        const peerKey="miss:"+peer.team;
        this.run("INSERT INTO submissions VALUES(?,?,?,?,?,?,1) ON CONFLICT(round,team) DO UPDATE SET canonical=excluded.canonical,depth=excluded.depth WHERE submissions.key=excluded.key",round.id,peer.team,peer.raw,peerKey,name,depth);
        this.run("INSERT INTO rulings(round,key,canonical,depth,created) VALUES(?,?,?,?,?)",round.id,peerKey,name,depth,Date.now());
      }
    }
    this.bump(r.code);
  }
  appeal(r: Row, round: Row, team: Row, raw: unknown) {
    const edition = this.roundBank(round);
    demand(!indexed(edition) || !edition.sourceJudged, 409, "This round verifies answers automatically.");
    const text = clean(raw,140,"submitted guess");
    const guess = this.one("SELECT raw FROM review_guesses WHERE round=? AND team=?",round.id,team.id);
    demand(r.current===round.id && !r.finished && round.phase==="open" && (!round.deadline || Date.now()<round.deadline),409,"The review window has closed.");
    demand(guess?.raw===text && !this.one("SELECT 1 FROM submissions WHERE round=? AND team=? AND depth IS NOT NULL",round.id,team.id),409,"Submit this wording first.");
    this.run("INSERT INTO kb_appeal VALUES(?,?,?,?,?) ON CONFLICT(round,team) DO UPDATE SET term=excluded.term,raw=excluded.raw,received=excluded.received",round.id,team.id,normal(text),text,Date.now());
    this.run("INSERT INTO kb_review(round,team,raw,status) VALUES(?,?,?,'queued') ON CONFLICT(round,team) DO UPDATE SET raw=excluded.raw,status='queued',evidence='[]' WHERE raw<>excluded.raw",round.id,team.id,text);
    this.bump(r.code);
    return {raw:text};
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
      `SELECT t.id, t.name, (s.depth IS NOT NULL AND s.key NOT LIKE 'miss:%') AS submitted, EXISTS(SELECT 1 FROM kb_appeal a WHERE a.round=? AND a.team=t.id) AS requested
       FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=?
       WHERE t.room=? AND (? IS NULL OR t.id<=?)`,
      round.id,
      round.id,
      round.room,
      round.last_team_id,
      round.last_team_id,
    )
      .map((t) => ({ id: t.id, name: t.name, submitted: Boolean(t.submitted), ...(t.requested ? {requested:true} : {}) }))
      .sort((a, b) => teamNameOrder.compare(a.name, b.name) || a.id - b.id);
  }
  detail(round: Row, host: boolean) {
    const q = host ? this.judgingBank(round) : this.roundBank(round);
    return {
      id: round.id,
      seq: round.seq,
      title: q.title,
      topic: q.topic,
      prompt: q.prompt,
      ...(indexed(q) ? {unlistedDepth:2} : {}),
      ...(indexed(q) && q.sourceJudged ? {sourceJudged:true} : {}),
      ...(indexed(q) && "playerRule" in q ? {playerRule:q.playerRule} : {}),
      phase: round.phase,
      pointValues: pointScale(round.score_version),
      deadline: round.deadline,
      ...(round.phase === "revealed"
        ? { results: this.results(round), revealedAt: round.revealed_at }
        : {}),
      ...(host
        ? {
            answerCount: indexed(q) ? q.answerCount + Number(this.one("SELECT count(*) n FROM round_additions WHERE round=?", round.id)!.n) : q.answers.length,
            ...(!indexed(q) && q.answers.length <= 80 ? { accepted: q.answers } : {}),
          }
        : {}),
      ...(host && round.phase !== "open" && !(indexed(q) && q.sourceJudged)
        ? {
            review: this.review(round),
            missedTeams: this.all(
              `SELECT t.id,t.name,s.canonical,(SELECT evidence FROM kb_review WHERE round=g.round AND team=g.team) AS reviewEvidence,COALESCE((SELECT raw FROM kb_appeal WHERE round=g.round AND team=g.team),g.raw) AS lastGuess,COALESCE(s.depth,0) AS depth,
                COALESCE((SELECT MAX(id) FROM rulings WHERE round=? AND key='miss:'||t.id),0) AS previous
                FROM teams t LEFT JOIN submissions s ON s.team=t.id AND s.round=?
                LEFT JOIN review_guesses g ON g.team=t.id AND g.round=?
               WHERE t.room=? AND t.id<=? AND (s.team IS NULL OR s.key='miss:'||t.id)
               ORDER BY t.name`,
              round.id,
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
      ...(t && round ? {appeal:this.one("SELECT raw FROM kb_appeal WHERE round=? AND team=?",round.id,t.id)?.raw ?? null} : {}),
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
