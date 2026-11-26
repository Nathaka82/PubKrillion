CREATE TABLE IF NOT EXISTS kb_bank(
 id TEXT PRIMARY KEY, prompt TEXT NOT NULL, metadata TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_catalogue_cache(
 edition TEXT NOT NULL, file_revision TEXT NOT NULL, rule TEXT NOT NULL, prompt TEXT NOT NULL, compiler INTEGER NOT NULL,
 bank TEXT NOT NULL REFERENCES kb_bank(id) ON DELETE CASCADE,
 PRIMARY KEY(edition,file_revision,rule,prompt,compiler)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS kb_catalogue_cache_bank ON kb_catalogue_cache(bank);
CREATE TABLE IF NOT EXISTS kb_member(
 bank TEXT NOT NULL REFERENCES kb_bank(id), entity TEXT NOT NULL,
 name TEXT NOT NULL, depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 5),
 aliases TEXT NOT NULL, evidence TEXT NOT NULL, PRIMARY KEY(bank,entity)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS kb_member_page ON kb_member(bank,depth,name,entity);
CREATE TABLE IF NOT EXISTS kb_answer_key(
 bank TEXT NOT NULL REFERENCES kb_bank(id) ON DELETE CASCADE, form INTEGER NOT NULL, term TEXT NOT NULL,
 entity TEXT, name TEXT NOT NULL, depth INTEGER NOT NULL,
 PRIMARY KEY(bank,form,term)
) WITHOUT ROWID;
CREATE VIRTUAL TABLE IF NOT EXISTS kb_text USING fts5(bank, entity UNINDEXED, terms, tokenize='unicode61 remove_diacritics 2');
CREATE TABLE IF NOT EXISTS kb_group(bank TEXT,term TEXT,name TEXT,depth INTEGER,PRIMARY KEY(bank,term)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kb_name(
 bank TEXT NOT NULL, term TEXT NOT NULL, entity TEXT NOT NULL,
 PRIMARY KEY(bank,term,entity)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS kb_name_ending ON kb_name(bank,substr(term,-3),length(term));
CREATE INDEX IF NOT EXISTS kb_short_spelling_suffix ON kb_answer_key(bank,substr(term,-2),length(term),term)
 WHERE form=1 AND length(term) BETWEEN 4 AND 7;
CREATE TABLE IF NOT EXISTS kb_appeal(
 round INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
 team INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
 term TEXT NOT NULL, raw TEXT NOT NULL, received INTEGER NOT NULL,
 PRIMARY KEY(round,team)
);
CREATE TABLE IF NOT EXISTS kb_review(round INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,team INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,raw TEXT NOT NULL,status TEXT NOT NULL,evidence TEXT NOT NULL DEFAULT '[]',PRIMARY KEY(round,team));
CREATE INDEX IF NOT EXISTS kb_appeal_term ON kb_appeal(round,term);
CREATE TABLE IF NOT EXISTS kb_job(
 id TEXT PRIMARY KEY, room TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE,
 prompt TEXT NOT NULL, research INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL,
 message TEXT NOT NULL DEFAULT '', bank TEXT REFERENCES kb_bank(id), created INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_rebuild(job TEXT PRIMARY KEY REFERENCES kb_job(id) ON DELETE CASCADE, entry TEXT, revision INTEGER);
CREATE TABLE IF NOT EXISTS kb_source_wait(job TEXT PRIMARY KEY REFERENCES kb_job(id) ON DELETE CASCADE, edition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_catalogue_job(job TEXT PRIMARY KEY REFERENCES kb_job(id) ON DELETE CASCADE, rule TEXT NOT NULL, edition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_catalogue_sync(id INTEGER PRIMARY KEY CHECK(id=1), edition TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS kb_catalogue_entry(room TEXT NOT NULL,entry TEXT NOT NULL,job TEXT NOT NULL,
    notice TEXT NOT NULL,PRIMARY KEY(room,entry),FOREIGN KEY(room,entry) REFERENCES library(room,id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS kb_catalogue_retry(job TEXT PRIMARY KEY REFERENCES kb_job(id) ON DELETE CASCADE,
    attempts INTEGER NOT NULL,next INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_sync(id INTEGER PRIMARY KEY CHECK(id=1), sources TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS kb_job_lookup ON kb_job(room,prompt,created);
CREATE TABLE IF NOT EXISTS kb_pick(
 id INTEGER PRIMARY KEY, cohort TEXT NOT NULL, team INTEGER NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
 room TEXT NOT NULL REFERENCES rooms(code) ON DELETE CASCADE, round INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
 choice TEXT, UNIQUE(cohort,team)
);
CREATE INDEX IF NOT EXISTS kb_pick_cohort ON kb_pick(cohort,id) WHERE choice IS NOT NULL;
CREATE TABLE IF NOT EXISTS kb_calibration(cohort TEXT PRIMARY KEY, population INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS kb_round_proof(
 round INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
 bank TEXT NOT NULL REFERENCES kb_bank(id), entity TEXT NOT NULL, PRIMARY KEY(round,bank,entity)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS kb_round_name(
 round INTEGER NOT NULL, spelling TEXT NOT NULL, bank TEXT NOT NULL, entity TEXT NOT NULL,
 name TEXT NOT NULL, depth INTEGER NOT NULL CHECK(depth BETWEEN 1 AND 5), PRIMARY KEY(round,spelling),
 FOREIGN KEY(round,bank,entity) REFERENCES kb_round_proof(round,bank,entity) ON DELETE CASCADE
) WITHOUT ROWID;
