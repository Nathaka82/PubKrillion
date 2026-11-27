import type { DatabaseSync } from "node:sqlite";
import { isDeepStrictEqual } from "node:util";
import { builtInQuestions, catalogue, catalogueQuestions } from "./catalogue.ts";
import { validateQuestion } from "./rules.ts";

export function activeLegacyRounds(db: DatabaseSync): number {
  const rounds = db.prepare("SELECT r.question, r.snapshot FROM rooms JOIN rounds r ON r.id=rooms.current WHERE rooms.finished=0 AND r.phase IN ('open','locked')").all();
  return rounds.filter(row => {
    const id = String(row.question);
    const published = builtInQuestions.get(id);
    if (!published) return true;
    const snapshot = JSON.parse(String(row.snapshot));
    return snapshot.knowledge || !isDeepStrictEqual(published, validateQuestion(snapshot, id));
  }).length;
}

// Called inside the schema transaction, before any obsolete tables are dropped.
export function retireQuestionBuilder(db: DatabaseSync) {
  if (activeLegacyRounds(db)) throw new Error("Finish and reveal the current custom rounds before upgrading.");
  const hasTable = (name: string) => db.prepare("SELECT 1 FROM sqlite_schema WHERE type='table' AND name=?").get(name);
  if (!db.prepare("PRAGMA table_info(review_guesses)").all().some(c => c.name === "requested"))
    db.exec("ALTER TABLE review_guesses ADD COLUMN requested INTEGER NOT NULL DEFAULT 0");
  if (hasTable("kb_appeal")) db.exec(`
    INSERT INTO review_guesses(round,team,raw,requested)
    SELECT round,team,raw,1 FROM kb_appeal WHERE true
    ON CONFLICT(round,team) DO UPDATE SET requested=1 WHERE review_guesses.raw=excluded.raw;
  `);
  // Materialise the bank each historical round opened with. Scores and rulings
  // already belong to the round and do not need to be recomputed.
  for (const row of db.prepare("SELECT id,snapshot FROM rounds WHERE json_type(snapshot,'$.knowledge')='text'").all()) {
    const q = JSON.parse(String(row.snapshot));
    const members = db.prepare("SELECT entity,name,depth,aliases FROM kb_member WHERE bank=? ORDER BY depth,name,entity").all(q.knowledge);
    if (!members.length) throw new Error("Missing historical answer bank for round " + row.id);
    const byEntity = new Map(members.map(m => [
      m.entity, {name: String(m.name), depth: Number(m.depth), aliases: JSON.parse(String(m.aliases)) as string[]},
    ]));
    if (hasTable("kb_round_proof")) {
      const extras = db.prepare(
        "SELECT m.entity,m.name,m.aliases FROM kb_round_proof p JOIN kb_member m USING(bank,entity) WHERE p.round=? AND m.bank<>? ORDER BY m.bank,m.entity"
      ).all(row.id, q.knowledge);
      for (const extra of extras) {
        let answer = byEntity.get(extra.entity);
        if (!answer) {
          const addition = db.prepare("SELECT key,name,depth FROM round_additions WHERE round=? AND name=?").get(row.id, extra.name);
          if (!addition) continue;
          answer = {name:String(addition.name),depth:Number(addition.depth),aliases:[]};
          byEntity.set(extra.entity, answer);
          db.prepare("DELETE FROM round_additions WHERE round=? AND key=?").run(row.id, addition.key);
        }
        const names = [String(extra.name), ...JSON.parse(String(extra.aliases))];
        answer.aliases = [...new Set([...answer.aliases, ...names])].filter(name => name !== answer.name);
      }
    }
    const answers = [...byEntity.values()];
    const { id, title, topic, prompt, source, coverage, context, matching } = q;
    db.prepare("UPDATE rounds SET snapshot=? WHERE id=?").run(
      JSON.stringify({id,title,topic,prompt,source,coverage,context,matching,answers}), row.id);
  }
  // Earlier pack installs prefixed a handful of question IDs with the pack ID.
  // Reattach their play counts without touching the historical snapshots.
  if (Number(db.prepare("PRAGMA user_version").get()?.user_version) < 4) {
    const relink = db.prepare("UPDATE rounds SET question=? WHERE question=?");
    for (const pack of catalogue) {
      for (const q of catalogueQuestions(pack.id)) {
        const oldId = pack.id + "-" + q.id;
        if (!builtInQuestions.has(oldId)) relink.run(q.id, oldId);
      }
    }
  }
  for (const table of [
    "kb_text", "kb_catalogue_entry", "kb_catalogue_cache", "kb_catalogue_retry",
    "kb_catalogue_job", "kb_source_wait", "kb_rebuild", "kb_job", "kb_catalogue_sync",
    "kb_round_name", "kb_round_proof", "kb_pick", "kb_calibration", "kb_appeal",
    "kb_review", "kb_group", "kb_name", "kb_answer_key", "kb_member", "kb_sync", "kb_bank", "library",
  ]) db.exec("DROP TABLE IF EXISTS " + table);
}
