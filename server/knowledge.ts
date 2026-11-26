import { spawn, type ChildProcess } from "node:child_process";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { existsSync, readFileSync, statSync } from "node:fs";
import { dirname, join } from "node:path";
import type { CatalogueRule } from "./question-drafts.ts";
import { DatabaseSync, type SQLInputValue, type SQLOutputValue } from "node:sqlite";
import {
  clean,
  demand,
  normal,
  resolveAnswer,
  type Answer,
  type Question,
} from "./rules.ts";

const catalogueFormat = Number(readFileSync(new URL("../scripts/knowledge/catalogue-format", import.meta.url), "utf8").trim());

export type IndexedQuestion = Question & {
  knowledge: string;
  answerCount: number;
  counts: number[];
  sourceJudged?: boolean;
  cohort?: string;
  openRule?: string;
  nameIndex?: number;
};
export function indexed(q: Question): q is IndexedQuestion {
  return "knowledge" in q && typeof q.knowledge === "string";
}

function spelling(value: string) {
  return value.toLowerCase().normalize("NFC").replace(/[‘’]/gu, "'").replace(/[‑‐]/gu, "-")
    .trim().replace(/\s+/gu, " ");
}

function foldedSpelling(value: string) {
  return spelling(value).normalize("NFD").replace(/(\p{Script=Latin})\p{M}+/gu, "$1")
    .normalize("NFC").replace(/&/gu, " and ").replace(/'/gu, "")
    .replace(/(?<!\p{Nd})[.,]|[.,](?!\p{Nd})|(?<!\p{Nd})-(?!\p{Nd})|[()[\]]/gu, " ")
    .trim().replace(/\s+/gu, " ");
}

// A typo can change a letter while still preserving the source's numbers, symbols and vowel marks.
function spellingDetails(value: string) {
  const parts = foldedSpelling(value).normalize("NFD").match(/[+-]?\p{Nd}+(?:[.,]\p{Nd}+)*|[^\p{L}\p{N}\s]/gu);
  return (parts ?? []).join("\u001f");
}

type Suggestion = { answer: Answer; exact: false; entity: string };
type Ruling = Record<string, SQLOutputValue>;

// Enumerate every single edit on a short spelling. Two outcomes at the same distance require more detail.
function shortSpellingSuggestion(db: DatabaseSync, bank: string, input: string,
  decisionFor: (name: string) => Ruling | undefined, suggestion?: Suggestion) {
  const letters = Array.from(input);
  const details = spellingDetails(input);
  const words = input.split(" ").length;
  const select = "SELECT term,entity,name,depth FROM kb_answer_key WHERE bank=? AND form=1 ";
  const exact = db.prepare(select + "AND term=?");
  const byStart = db.prepare(select + "AND term>=? AND term<? AND length(term)=? AND substr(term,?)=? LIMIT 81");
  const byEnd = db.prepare(select + "AND length(term) BETWEEN 4 AND 7 AND substr(term,-2)=? " +
    "AND substr(term,1,?)=? AND substr(term,?)=? LIMIT 81");
  const seen = new Set<string>();
  const consider = (row: Ruling) => {
    const term = String(row.term);
    if (seen.has(term) || spellingDetails(term) !== details || term.split(" ").length !== words) return true;
    seen.add(term);
    if (row.entity === null) return false;
    const entity = String(row.entity), depth = Number(row.depth), name = String(row.name);
    const same = (r?: Ruling) => r?.entity === entity && r.depth === depth && (entity !== "" || r.name === name);
    if (suggestion) return same(decisionFor(suggestion.answer.name));
    let display = name;
    if (!same(decisionFor(name))) {
      if (!same(decisionFor(term))) return false;
      display = term;
    }
    suggestion = { answer: { name: display, depth, aliases: [] }, entity, exact: false };
    return true;
  };
  for (let i = 0; i <= letters.length; i++) {
    const before = letters.slice(0,i).join("");
    const after = letters.slice(i+1).join("");
    if (i < letters.length) {
      const removed = exact.get(bank, before + after);
      if (removed && !consider(removed)) return;
      if (i + 1 < letters.length && letters[i] !== letters[i+1]) {
        const swapped = before + letters[i+1] + letters[i] + letters.slice(i+2).join("");
        const transposition = exact.get(bank, swapped);
        if (transposition && !consider(transposition)) return;
      }
    }
    // One unknown character can be inserted or replace a letter. Match its two known sides literally.
    for (const skip of i < letters.length ? [0, 1] : [0]) {
      const right = letters.slice(i+skip).join("");
      const rows = letters.length - i - skip >= 2
        ? byEnd.all(bank, letters.slice(-2).join(""), i, before, i+2, right)
        : byStart.all(bank, before, before+"\u{10ffff}", letters.length+1-skip, i+2, right);
      if (rows.length > 80 || !rows.every(consider)) return;
    }
  }
  return suggestion;
}

export function lookup(db: DatabaseSync, bank: string, raw: string, { exactOnly = false } = {}) {
  const key = normal(raw) || spelling(raw);
  const checked = db.prepare("SELECT form FROM kb_answer_key WHERE bank=? ORDER BY form DESC LIMIT 1").get(bank);
  const select = checked && db.prepare("SELECT entity,name,depth FROM kb_answer_key WHERE bank=? AND form=? AND term=?");
  const decisionFor = (name: string) => select?.get(bank, 0, spelling(name)) ?? select?.get(bank, 1, foldedSpelling(name));
  const folded = foldedSpelling(raw);
  if (checked) {
    const decision = decisionFor(raw);
    if (decision) {
      if (decision.entity === null) return null;
      return { answer: { name: String(decision.name), depth: Number(decision.depth), aliases: [] },
        exact: true, entity: String(decision.entity), ...(decision.entity === "" ? { shared: true } : {}) };
    }
  }
  const byTerm = (prefix: boolean) =>
    db
      .prepare(
        "SELECT DISTINCT m.entity,m.name,m.depth FROM kb_name n CROSS JOIN kb_member m ON m.bank=n.bank AND m.entity=n.entity WHERE n.bank=? AND " +
          (prefix ? "n.term>=? AND n.term<?" : "n.term=?") +
          " LIMIT 2",
      )
      .all(bank, key, ...(prefix ? [key + "\u{10ffff}"] : []));
  const exact = checked ? [] : byTerm(false);
  if (exactOnly && !exact.length) return;
  if (exact.length > 1) {
    const group = db
      .prepare("SELECT name,depth FROM kb_group WHERE bank=? AND term=?")
      .get(bank, key);
    return group
      ? {
          answer: {
            name: String(group.name),
            depth: Number(group.depth),
            aliases: [],
          },
          shared: true,
          exact: true,
        }
      : null;
  }
  let list = exact;
  if (!list.length && Array.from(checked ? folded : key).length >= 4) {
    if (checked) {
      // Include shared and blocked spellings. One eligible title can still have an ineligible namesake.
      const prefixes = db.prepare("SELECT DISTINCT entity,name,depth FROM kb_answer_key " +
        "WHERE bank=? AND form=? AND term>=? AND term<? LIMIT 2");
      list = prefixes.all(bank, 1, folded, folded + "\u{10ffff}");
      if (!list.length) {
        const literal = spelling(raw);
        list = prefixes.all(bank, 0, literal, literal + "\u{10ffff}");
      }
      if (list.length > 1 || list[0]?.entity === null) return undefined;
    } else {
      list = byTerm(true);
    }
  }
  const member = db.prepare("SELECT name,aliases,depth FROM kb_member WHERE bank=? AND entity=?");
  const owned = (name: string, entity: string) => decisionFor(name)?.entity === entity;
  let prefixSuggestion: Suggestion | undefined;
  if (list.length === 1) {
    const name = String(list[0].name);
    const entity = String(list[0].entity);
    const depth = Number(list[0].depth);
    if (!checked) return { answer: { name, depth, aliases: [] }, exact: exact.length === 1, entity };
    const canonical = decisionFor(name);
    if (canonical?.entity === entity && canonical.depth === depth) {
      prefixSuggestion = { answer: { name, depth, aliases: [] }, exact: false, entity };
    } else if (entity) {
      const aliases: string[] = JSON.parse(String(member.get(bank, entity)!.aliases));
      const matching = aliases.find(form => foldedSpelling(form).startsWith(folded) && owned(form, entity));
      if (matching) prefixSuggestion = { answer: { name: matching, depth, aliases: [] }, exact: false, entity };
    }
  }
  const size = Array.from(folded).length;
  if (checked?.form === 1 && size >= 4 && size <= 6)
    return shortSpellingSuggestion(db, bank, folded, decisionFor, prefixSuggestion);
  if (prefixSuggestion) return prefixSuggestion;
  // Limit typo correction to a small indexed neighbourhood. Always ask for confirmation.
  if (key.length >= 5) {
    const chars = Array.from(key);
    const head = chars.slice(0, 3).join("");
    const leading = db
      .prepare(
        "SELECT term,entity FROM kb_name WHERE bank=? AND term>=? AND term<? AND length(term) BETWEEN ? AND ? LIMIT 81",
      )
      .all(bank, head, head + "\u{10ffff}", chars.length - 2, chars.length + 2);
    const ending = db.prepare("SELECT term,entity FROM kb_name WHERE bank=? AND substr(term,-3)=? AND length(term) BETWEEN ? AND ? LIMIT 81")
      .all(bank, chars.slice(-3).join(""), chars.length - 2, chars.length + 2);
    const nearby = [leading, ending].filter(rows => rows.length <= 80).flat();
    if (nearby.length) {
      const terms = new Map<string, Set<string>>();
      for (const r of nearby) {
        const entity = String(r.entity);
        if (!terms.has(entity)) terms.set(entity, new Set());
        terms.get(entity)!.add(String(r.term));
      }
      const shortlist = new Map<Answer, string>();
      const details = spellingDetails(raw);
      for (const [entity, neighbours] of terms) {
        const row = member.get(bank, entity)!;
        const name = String(row.name), depth = Number(row.depth);
        if (!checked) {
          shortlist.set({ name, depth, aliases: [...neighbours] }, name);
          continue;
        }
        const aliases: string[] = JSON.parse(String(row.aliases));
        const forms = [name, ...aliases].filter(form => {
          const broad = normal(form) || spelling(form);
          if (!neighbours.has(broad) || spellingDetails(form) !== details) return false;
          if (broad.startsWith(key) && !foldedSpelling(form).startsWith(folded)) return false;
          return owned(form, entity);
        });
        if (forms.length) {
          // A shared display title could submit a different score. Offer a spelling owned by this entry.
          shortlist.set({ name: forms[0], depth, aliases: forms.slice(1) }, owned(name, entity) ? name : forms[0]);
        }
      }
      const match = resolveAnswer(raw, [...shortlist.keys()], "full");
      if (match) return { answer: { ...match.answer, name: shortlist.get(match.answer)! }, exact: false };
    }
  }
  return undefined;
}
export function restoreRoundNames(db: DatabaseSync) {
  const insert = db.prepare("INSERT OR IGNORE INTO kb_round_name VALUES(?,?,?,?,?,?)");
  const rows = db.prepare(`SELECT p.round,p.bank,p.entity,s.raw,s.canonical,s.depth FROM kb_round_proof p
    JOIN rounds r ON r.id=p.round JOIN submissions s ON s.round=p.round AND s.key=('kb:'||p.entity)
    WHERE r.phase='open' AND s.depth IS NOT NULL AND s.canonical IS NOT NULL ORDER BY p.round,s.team,p.bank`).all();
  for (const row of rows) {
    const match = lookup(db,String(row.bank),String(row.raw), { exactOnly: true });
    if (match?.exact && "entity" in match && match.entity===row.entity)
      insert.run(row.round,spelling(String(row.raw)),row.bank,row.entity,row.canonical,row.depth);
  }
}

// A later edition can supply a missing exact name only under the same open membership rule.
// Retain the proof used for a ruling so another team gets the same answer after a source refresh.
export function additionalAnswer(db: DatabaseSync, round: number, room: string, entry: string, q: IndexedQuestion, raw: string, remember = true) {
  if (!q.openRule) return;
  const accepted = db.prepare("SELECT entity,name,depth FROM kb_round_name WHERE round=? AND spelling=?")
    .get(round, spelling(raw));
  if (accepted) return { exact: true, entity: String(accepted.entity),
    answer: { name: String(accepted.name), depth: Number(accepted.depth), aliases: [] } };
  const editions = db.prepare(`
    WITH editions(bank,current) AS (
      SELECT DISTINCT bank,0 FROM kb_round_proof WHERE round=?
      UNION ALL
      SELECT json_extract(l.content,'$.knowledge'),1 FROM library l
        WHERE l.room=? AND l.id=? AND json_extract(l.content,'$.openRule')=?
        AND NOT EXISTS(SELECT 1 FROM kb_catalogue_entry e WHERE e.room=l.room AND e.entry=l.id)
    )
    SELECT bank,max(current) current FROM editions WHERE bank<>? GROUP BY bank ORDER BY bank
  `).all(round, room, entry, q.openRule, q.knowledge);
  const member = db.prepare(`SELECT bank,entity,name,depth FROM kb_member m WHERE bank=? AND entity=?
    AND (?=1 OR EXISTS(SELECT 1 FROM kb_round_proof p WHERE p.round=? AND p.bank=m.bank AND p.entity=m.entity))`);
  const rows: Ruling[] = [];
  for (const edition of editions) {
    const match = lookup(db, String(edition.bank), raw, { exactOnly: true });
    if (!match?.exact || !("entity" in match) || !match.entity) continue;
    const row = member.get(edition.bank, match.entity, edition.current, round);
    if (row) rows.push(row);
  }
  if (!rows.length || rows.some(r => r.entity !== rows[0].entity)) return;
  const m = rows[0];
  const pinned = db.prepare("SELECT name,depth FROM kb_member WHERE bank=? AND entity=?").get(q.knowledge, m.entity);
  const name = String(pinned?.name ?? m.name);
  const previous = db.prepare(`SELECT a.name,a.depth FROM kb_round_proof p JOIN kb_member m USING(bank,entity)
    JOIN round_additions a ON a.round=p.round AND a.name=m.name WHERE p.round=? AND p.entity=? LIMIT 1`).get(round, m.entity);
  const additionKey = (normal(name) || name) + "\u001f" + spelling(name);
  const original = lookup(db, q.knowledge, name, { exactOnly: true });
  if (!pinned && !previous && (original === null || original?.exact ||
      db.prepare("SELECT 1 FROM round_additions WHERE round=? AND key IN (?,?)").get(round, normal(name), additionKey))) return;
  const depth = Number(pinned?.depth ?? previous?.depth ?? Math.min(2, Number(m.depth)));
  const canonical = String(previous?.name ?? name);
  if (remember) {
    db.prepare("INSERT OR IGNORE INTO kb_round_proof VALUES(?,?,?)").run(round, m.bank, m.entity);
    if (!pinned && !previous) db.prepare("INSERT INTO round_additions VALUES(?,?,?,?)").run(round, additionKey, name, depth);
    db.prepare("INSERT INTO kb_round_name(round,spelling,bank,entity,name,depth) VALUES(?,?,?,?,?,?)")
      .run(round, spelling(raw), m.bank, m.entity, canonical, depth);
  }
  return { exact: true, entity: String(m.entity), answer: { name: canonical, depth, aliases: [] } };
}

export function indexedPage(
  db: DatabaseSync,
  q: IndexedQuestion,
  params: URLSearchParams,
  round?: number,
) {
  const tier = Number(params.get("depth")),
    offset = Number(params.get("offset"));
  demand(
    Number.isInteger(tier) &&
      tier >= 0 &&
      tier <= 5 &&
      Number.isSafeInteger(offset) &&
      offset >= 0,
    400,
    "Invalid answer page.",
  );
  const input = clean(params.get("q") ?? "", 140, "search", 0);
  const search = normal(input);
  const folded = foldedSpelling(input) || spelling(input);
  const precise = q.nameIndex === 2 && folded !== search ? "\u001f" + folded : "";
  const binds: SQLInputValue[] = [q.knowledge];
  const words = search.split(" ").map(t => '"' + t + '"*').join(" AND ");
  const proofMatches = precise
    ? "SELECT p.entity FROM kb_round_proof p CROSS JOIN kb_name n ON n.bank=p.bank AND n.entity=p.entity WHERE p.round=? AND n.term>=? AND n.term<?"
    : `SELECT p.entity FROM kb_round_proof p CROSS JOIN kb_text t WHERE p.round=?
       AND t.entity=p.entity AND kb_text MATCH ('bank:"'||p.bank||'" AND terms:('||?||')')`;
  const proofBinds: SQLInputValue[] = precise ? [precise,precise+"\u{10ffff}"] : [words];
  let base = "SELECT entity,name,depth,aliases,evidence FROM kb_member m WHERE bank=?";
  if (precise || search) {
    if (precise) {
      base += " AND (entity IN (SELECT entity FROM kb_name WHERE bank=? AND term>=? AND term<?)";
      binds.push(q.knowledge, ...proofBinds);
    } else {
      base += " AND (entity IN (SELECT entity FROM kb_text WHERE kb_text MATCH ?)";
      binds.push('bank:"' + q.knowledge + '" AND terms:(' + words + ")");
    }
    if (round) {
      base += " OR entity IN (" + proofMatches + ")";
      binds.push(round,...proofBinds);
    }
    base += ")";
  }
  if (round) {
    base += ` UNION ALL SELECT m.entity,a.name,a.depth,COALESCE(m.aliases,'[]'),COALESCE(m.evidence,'[]')
      FROM round_additions a LEFT JOIN kb_member m ON (m.bank,m.entity)=(
        SELECT p.bank,p.entity FROM kb_round_proof p JOIN kb_member v USING(bank,entity)
        WHERE p.round=a.round AND v.name=a.name ORDER BY p.bank LIMIT 1) WHERE a.round=?`;
    binds.push(round);
    if (precise || search) {
      base += " AND (m.entity IN (" + proofMatches + ")";
      binds.push(round,...proofBinds);
      if (!precise) {
        base += " OR (key>=? AND key<?)";
        binds.push(search, search+"\u{10ffff}");
      }
      base += ")";
    }
  }
  const query = " FROM (" + base + ")" + (tier ? " WHERE depth=?" : "");
  if (tier) binds.push(tier);
  const filtered = Number(
    db.prepare("SELECT COUNT(*) n" + query).get(...binds)!.n,
  );
  const start = Math.min(
    offset,
    Math.max(0, Math.floor((filtered - 1) / 40) * 40),
  );
  const extraProof = round && db.prepare(`SELECT m.name,m.aliases,m.evidence FROM kb_round_proof p
    JOIN kb_member m USING(bank,entity) WHERE p.round=? AND p.entity=?`);
  const roundContext = round && db.prepare("SELECT room,question FROM rounds WHERE id=?").get(round);
  const decisions = db.prepare("SELECT entity,depth FROM kb_answer_key WHERE bank=? AND form=? AND term=?");
  const answers = db
    .prepare("SELECT *" + query + " ORDER BY depth,name LIMIT 40 OFFSET ?")
    .all(...binds, start)
    .map((r) => {
      const answer = { name:r.name, depth:r.depth, aliases:JSON.parse(String(r.aliases)) as string[], evidence:JSON.parse(String(r.evidence)) as unknown[] };
      const proofs = extraProof && r.entity ? extraProof.all(round!, r.entity) : [];
      if (proofs.length) {
        const aliases = new Set(answer.aliases);
        const evidence = new Set(answer.evidence.map(fact => JSON.stringify(fact)));
        for (const proof of proofs) {
          if (proof.name !== r.name) aliases.add(String(proof.name));
          for (const alias of JSON.parse(String(proof.aliases))) aliases.add(alias);
          for (const fact of JSON.parse(String(proof.evidence))) evidence.add(JSON.stringify(fact));
        }
        answer.aliases = [...aliases];
        answer.evidence = [...evidence].map(fact=>JSON.parse(fact));
      }
      if (q.nameIndex !== 2 || !r.entity) return answer;
      const needsDetail: string[] = [], sharedNames: {name:string;depth:number}[] = [];
      const accepts = (name: string) => {
        const decision = decisions.get(q.knowledge,0,spelling(name)) ?? decisions.get(q.knowledge,1,foldedSpelling(name));
        if (decision?.entity === r.entity) return true;
        if (!decision && proofs.length && roundContext && additionalAnswer(db,round!,String(roundContext.room),
          String(roundContext.question),q,name,false)?.entity === r.entity) return true;
        if (decision?.entity === "") sharedNames.push({name,depth:Number(decision.depth)});
        else needsDetail.push(name);
        return false;
      };
      accepts(String(r.name));
      answer.aliases = answer.aliases.filter(accepts);
      return { ...answer, ...(needsDetail.length && {needsDetail}), ...(sharedNames.length && {sharedNames}) };
    });
  const counts = [...q.counts];
  if (round)
    for (const r of db
      .prepare(
        "SELECT depth,COUNT(*) n FROM round_additions WHERE round=? GROUP BY depth",
      )
      .all(round))
      counts[Number(r.depth)] += Number(r.n);
  return {
    prompt: q.prompt,
    source: q.source,
    coverage: q.coverage,
    counts,
    total: counts.reduce((a, b) => a + b, 0),
    filtered,
    offset: start,
    answers,
  };
}
const worker = fileURLToPath(
  new URL("../scripts/knowledge/worker.py", import.meta.url),
);
export class Research {
  db: DatabaseSync;
  file: string;
  active?: ChildProcess;
  installer?: ChildProcess;
  installRetry?: ReturnType<typeof setTimeout>;
  worldNotice = "";
  stopped = false;
  timer: ReturnType<typeof setInterval>;
  nextRefresh = Date.now() + 3600000;
  catalogueFile: string;
  private nextCatalogueCheck = 0;
  private catalogueStamp = "";
  private catalogueEdition?: string;
  constructor(db: DatabaseSync, file: string) {
    this.db = db;
    this.file = file;
    this.catalogueFile = process.env.KNOWLEDGE_CATALOGUE || join(dirname(file), "knowledge-catalogue.sqlite");
    db.exec(
      "UPDATE kb_job SET status='queued' WHERE status='working'; UPDATE kb_review SET status='queued' WHERE status='working'",
    );
    this.timer = setInterval(() => this.pump(), 2000);
    this.timer.unref();
    if (process.env.NODE_ENV !== "test" && !process.env.KNOWLEDGE_WORLD_DB && !existsSync(this.catalogueFile))
      this.installWorld();
  }
  syncCatalogue(force = false) {
    if (!force && Date.now() < this.nextCatalogueCheck) return this.catalogueEdition;
    this.nextCatalogueCheck = Date.now() + 15000;
    const stat = statSync(this.catalogueFile, { throwIfNoEntry: false });
    if (!stat) { this.catalogueStamp = ""; this.catalogueEdition = undefined; return; }
    const stamp = [stat.ino, stat.size, stat.mtimeMs].join("/");
    if (stamp === this.catalogueStamp) return this.catalogueEdition;
    const publication = new DatabaseSync(this.catalogueFile, { readOnly: true });
    let edition;
    try { edition = publication.prepare("SELECT edition FROM publication WHERE format BETWEEN 4 AND ?").get(catalogueFormat)?.edition; }
    finally { publication.close(); }
    if (typeof edition !== "string") return;
    const checkpoint = edition + ":answer-scores-v31";
    this.db.exec("SAVEPOINT catalogue_sync");
    try {
      if (!this.db.prepare("SELECT 1 FROM kb_catalogue_sync WHERE edition=?").get(checkpoint)) {
        const entries = this.db.prepare(`SELECT l.room,l.id,l.revision,json_extract(l.content,'$.prompt') prompt,
            json_extract(l.content,'$.plan.catalogueKey') rule FROM library l JOIN rooms r ON r.code=l.room
            WHERE r.finished=0 AND json_extract(l.content,'$.plan.catalogueKey') IS NOT NULL`).all();
        for (const entry of entries) {
          const job = randomUUID();
          this.db.prepare("INSERT INTO kb_job(id,room,prompt,research,status,created) VALUES(?,?,?,2,'queued',?)")
            .run(job, entry.room, entry.prompt, Date.now());
          this.db.prepare("INSERT INTO kb_rebuild VALUES(?,?,?)").run(job, entry.id, entry.revision);
          this.db.prepare("INSERT INTO kb_catalogue_job VALUES(?,?,?)").run(job, entry.rule, edition);
          this.db.prepare("INSERT OR REPLACE INTO kb_catalogue_entry VALUES(?,?,?,?)")
            .run(entry.room, entry.id, job, "Checking this question against the latest source edition. Reopen the question list shortly.");
        }
        this.db.prepare("INSERT OR REPLACE INTO kb_catalogue_sync VALUES(1,?)").run(checkpoint);
      }
      this.db.exec("RELEASE catalogue_sync");
      this.catalogueStamp = stamp;
      this.catalogueEdition = edition;
      return edition;
    } catch (error) {
      this.db.exec("ROLLBACK TO catalogue_sync; RELEASE catalogue_sync");
      throw error;
    }
  }
  private installWorld() {
    if (this.stopped) return;
    const script = fileURLToPath(new URL("../scripts/knowledge/world_index.py", import.meta.url));
    const child = spawn(process.env.KNOWLEDGE_PYTHON || (process.platform === "win32" ? "python" : "python3"),
      [script, "--db", this.file], { windowsHide: true, stdio: ["ignore", "inherit", "inherit"] });
    this.installer = child;
    child.on("error", error => console.error("Wikidata5M setup:", error.message));
    child.on("close", code => {
      this.installer = undefined;
      if (this.stopped) return;
      this.worldNotice = code === 0 ? "" : "The general knowledge index is unavailable. This preview uses the other connected sources. Installation will retry automatically.";
      setImmediate(() => this.pump());
      if (code === 0) return;
      this.installRetry = setTimeout(() => this.installWorld(), 3600000);
      this.installRetry.unref();
    });
  }
  checkCapacity() {
    const active = this.db.prepare("SELECT COUNT(*) n FROM kb_job WHERE research<>2 AND status IN ('queued','working')").get()!;
    demand(Number(active.n) < 16, 429, "Research queue is full. Try again shortly.");
  }
  prepare(room: string, id: string) {
    const job = this.db.prepare("SELECT status FROM kb_job WHERE room=? AND id=?").get(room, id);
    demand(job, 404, "Research job not found.");
    if (job.status === "suggested") {
      this.checkCapacity();
      this.db.prepare("UPDATE kb_job SET status='queued',created=? WHERE id=? AND status='suggested'").run(Date.now(), id);
      this.pump();
    }
    return { id };
  }
  request(room: string, body: any, snapshotOnly = false, rule?: CatalogueRule, prepareOnSelect = false) {
    const prompt = clean(body.prompt, 450, "prompt");
    const mode = snapshotOnly ? 3 : Number(body.research === true);
    const deferred = Boolean(rule && prepareOnSelect);
    if (deferred) this.db.prepare("DELETE FROM kb_job WHERE room=? AND status='suggested' AND created<?")
      .run(room, Date.now() - 7 * 86400000);
    const old = this.db
      .prepare(
        "SELECT j.id,j.status FROM kb_job j LEFT JOIN kb_catalogue_job c ON c.job=j.id WHERE room=? AND prompt=? AND research=? AND coalesce(c.rule,'')=? AND coalesce(c.edition,'')=? AND (status IN ('queued','working','waiting_source') OR (research=3 AND status IN ('ready','suggested') AND created>?)) ORDER BY created DESC LIMIT 1",
      )
      .get(room, prompt, mode, rule?.key ?? "", rule?.edition ?? "", Date.now() - 300000);
    if (old) {
      if (old.status === "suggested" && !deferred) return this.prepare(room, String(old.id));
      return { id: old.id, deferred: old.status === "suggested" };
    }
    if (!deferred) this.checkCapacity();
    const id = randomUUID();
    this.db.exec("SAVEPOINT enqueue_question");
    try {
      this.db.prepare("INSERT INTO kb_job(id,room,prompt,research,status,created) VALUES(?,?,?,?,?,?)")
        .run(id, room, prompt, mode, deferred ? "suggested" : "queued", Date.now());
      if (rule) this.db.prepare("INSERT INTO kb_catalogue_job VALUES(?,?,?)").run(id, rule.key, rule.edition);
      this.db.exec("RELEASE enqueue_question");
    } catch (error) {
      this.db.exec("ROLLBACK TO enqueue_question; RELEASE enqueue_question");
      throw error;
    }
    if (!deferred) this.pump();
    return { id, deferred };
  }
  status(room: string, id: string) {
    const row = this.db
      .prepare("SELECT j.status,j.message,j.prompt,j.bank,c.edition FROM kb_job j LEFT JOIN kb_catalogue_job c ON c.job=j.id WHERE j.id=? AND j.room=?")
      .get(id, room);
    demand(row, 404, "Research job not found.");
    return {
      ...row,
      edition: typeof row.edition === "string" ? row.edition : undefined,
      installingWorld: Boolean(this.installer),
      worldNotice: this.worldNotice,
      ...(row.bank
        ? {
            preview: JSON.parse(
              String(
                this.db
                  .prepare("SELECT metadata FROM kb_bank WHERE id=?")
                  .get(row.bank)!.metadata,
              ),
            ),
          }
        : {}),
    };
  }
  pump() {
    if (this.active || this.stopped) return;
    try { this.syncCatalogue(); }
    catch (error) { console.error("Knowledge publication:", error instanceof Error ? error.message : error); }
    if (this.catalogueEdition) this.db.prepare(`UPDATE kb_job SET status='queued',message=''
      WHERE status='waiting_source' AND id IN (SELECT job FROM kb_source_wait WHERE edition<>?)
      AND room IN (SELECT code FROM rooms WHERE finished=0)`).run(this.catalogueEdition);
    this.db.prepare(`UPDATE kb_job SET status='queued' WHERE status='failed' AND id IN
        (SELECT r.job FROM kb_catalogue_retry r JOIN kb_catalogue_entry e USING(job) WHERE r.next<=?)`).run(Date.now());
    const job = this.db
      .prepare(
        `SELECT j.*,COALESCE(c.rule,json_extract(l.content,'$.plan.catalogueKey')) AS catalogueKey,c.edition
         FROM kb_job j LEFT JOIN kb_catalogue_job c ON c.job=j.id LEFT JOIN kb_rebuild b ON b.job=j.id
         LEFT JOIN library l ON l.room=j.room AND l.id=b.entry AND l.revision=b.revision
         WHERE j.status='queued' AND (?=0 OR c.rule IS NOT NULL) ORDER BY (research=2),created LIMIT 1`,
      )
      .get(Number(Boolean(this.installer)));
    if (this.installer && !job) return;
    const review = job
      ? undefined
      : this.db
          .prepare(
            "SELECT round,team,raw FROM kb_review WHERE status='queued' ORDER BY round,team LIMIT 1",
          )
          .get();
    if (!review && !job) {
      if (Date.now() < this.nextRefresh) return;
      // Published banks receive new editions from the independent builder.
      if (!this.db.prepare("SELECT 1 FROM kb_bank WHERE metadata<>'null' AND json_extract(metadata,'$.plan.catalogueKey') IS NULL LIMIT 1").get()) {
        this.nextRefresh = Date.now() + 3600000;
        return;
      }
    }
    if (review)
      this.db
        .prepare(
          "UPDATE kb_review SET status='working' WHERE round=? AND team=?",
        )
        .run(review.round, review.team);
    else if (!job) this.nextRefresh = Date.now() + 86400000;
    else
      this.db
        .prepare("UPDATE kb_job SET status='working' WHERE id=?")
        .run(job.id);
    const child = spawn(
      process.env.KNOWLEDGE_PYTHON ||
        (process.platform === "win32" ? "python" : "python3"),
      [
        worker,
        job ? "build" : review ? "review" : "refresh",
        "--db",
        this.file,
      ],
      {
        windowsHide: true,
        stdio: "pipe",
        env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      },
    );
    this.active = child;
    let err = "",
      out = "";
    child.stdout.on("data", (c) => {
      out = (out + c).slice(-4096);
    });
    child.stderr.on("data", (c) => {
      err = (err + c).slice(-1500);
    });
    child.on("error", () => {
      err = "Could not start the local Python knowledge worker.";
    });
    child.stdin.on("error", () => {});
    child.stdin.end(
      job
        ? JSON.stringify({ prompt: job.prompt, research: job.research === 1, snapshotOnly: job.research === 3,
            catalogueKey: job.catalogueKey, edition: job.edition })
        : review
          ? JSON.stringify(review)
          : undefined,
    );
    const timer = setTimeout(() => {
      err = "Source research timed out. You can retry.";
      child.kill();
    }, job ? 1800000 : 180000);
    child.on("close", (code) => {
      clearTimeout(timer);
      this.active = undefined;
      if (this.stopped) return;
      if (job) {
        let publicationError;
        try { if (job.research === 2 && job.edition) this.syncCatalogue(true); }
        catch (error) { publicationError = error; }
        this.db.exec("SAVEPOINT finish_research");
        try {
          if (publicationError) throw publicationError;
          if (code !== 0)
            throw new Error(err.trim() || "Research stopped. Try again.");
          const { bank, rule, edition, awaitingEdition, chooseTopic, message } = JSON.parse(out);
          if (chooseTopic === true && !bank) {
            this.db.prepare("UPDATE kb_job SET status='choose_topic',message=? WHERE id=?").run(message,job.id);
            this.db.prepare("DELETE FROM kb_source_wait WHERE job=?").run(job.id);
            this.db.exec("RELEASE finish_research");
            setImmediate(() => this.pump());
            return;
          }
          if (typeof awaitingEdition === "string" && !bank) {
            this.syncCatalogue(true);
            this.db.prepare("INSERT OR REPLACE INTO kb_source_wait VALUES(?,?)").run(job.id,awaitingEdition);
            this.db.prepare("UPDATE kb_job SET status='waiting_source',message=? WHERE id=?")
              .run("The builder is checking the source. This preview will update when it publishes the result.",job.id);
            this.db.exec("RELEASE finish_research");
            setImmediate(() => this.pump());
            return;
          }
          if (rule && edition)
            this.db.prepare("INSERT OR IGNORE INTO kb_catalogue_job VALUES(?,?,?)").run(job.id, rule, edition);
          if (job.research === 2) {
            const rebuild = this.db
              .prepare("SELECT entry,revision FROM kb_rebuild WHERE job=?")
              .get(job.id);
            const current = this.db
              .prepare("SELECT metadata FROM kb_bank WHERE id=?")
              .get(bank);
            if (rebuild && current && (!job.edition || job.edition === this.catalogueEdition)) {
              const q = {
                ...JSON.parse(String(current.metadata)),
                id: rebuild.entry,
              };
              const updated = this.db
                .prepare(
                  `UPDATE library SET content=?,revision=revision+1 WHERE room=? AND id=? AND revision=? AND json_extract(content,'$.knowledge')<>?
                   AND NOT EXISTS(SELECT 1 FROM kb_catalogue_entry e WHERE e.room=library.room AND e.entry=library.id AND e.job<>?)`,
                )
                .run(
                  JSON.stringify(q),
                  job.room,
                  rebuild.entry,
                  rebuild.revision,
                  bank,
                  job.id,
                );
              const cleared = this.db.prepare("DELETE FROM kb_catalogue_entry WHERE room=? AND entry=? AND job=?")
                .run(job.room, rebuild.entry, job.id);
              if (updated.changes || cleared.changes)
                this.db
                  .prepare("UPDATE rooms SET version=version+1 WHERE code=?")
                  .run(job.room);
            }
          }
          demand(
            this.db.prepare("SELECT 1 FROM kb_bank WHERE id=?").get(bank),
            500,
            "Missing bank.",
          );
          this.db
            .prepare("UPDATE kb_job SET bank=?,status='ready' WHERE id=?")
            .run(bank, job.id);
          this.db.prepare("DELETE FROM kb_catalogue_retry WHERE job=?").run(job.id);
          this.db.prepare("DELETE FROM kb_source_wait WHERE job=?").run(job.id);
          this.db.exec("RELEASE finish_research");
        } catch (e) {
          this.db.exec("ROLLBACK TO finish_research; RELEASE finish_research");
          const message = e instanceof Error ? e.message : "Research failed.";
          this.db
            .prepare("UPDATE kb_job SET status='failed',message=? WHERE id=?")
            .run(message, job.id);
          const affected = this.db.prepare("UPDATE kb_catalogue_entry SET notice=? WHERE job=?")
            .run(code === 3 ? message : "Verification will retry automatically. " + message, job.id);
          if (affected.changes && code !== 3) {
            const attempts = Number(this.db.prepare("SELECT attempts FROM kb_catalogue_retry WHERE job=?").get(job.id)?.attempts ?? 0);
            this.db.prepare("INSERT OR REPLACE INTO kb_catalogue_retry VALUES(?,?,?)")
              .run(job.id, attempts+1, Date.now()+Math.min(1800000, 10000 * 2 ** Math.min(attempts, 8)));
          }
        }
      } else if (review) {
        let evidence = [];
        try {
          if (code === 0) evidence = JSON.parse(out).evidence;
        } catch {}
        this.db
          .prepare(
            "UPDATE kb_review SET status=?,evidence=? WHERE round=? AND team=? AND raw=?",
          )
          .run(
            code === 0 ? "ready" : "unavailable",
            JSON.stringify(evidence),
            review.round,
            review.team,
            review.raw,
          );
        this.db
          .prepare(
            "UPDATE rooms SET version=version+1 WHERE code=(SELECT room FROM rounds WHERE id=?)",
          )
          .run(review.round);
      } else if (code !== 0) console.error("Knowledge refresh:", err.trim());
      setImmediate(() => this.pump());
    });
  }
  close() {
    this.stopped = true;
    clearInterval(this.timer);
    clearTimeout(this.installRetry);
    this.installer?.kill();
    this.active?.kill();
  }
}
