"""Publish entity-owned names independently of the questions which discovered them."""

import gzip
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from graph import folded_spelling, spelling


RECORDS = """CREATE TABLE IF NOT EXISTS label_record(
    id INTEGER PRIMARY KEY, forms TEXT NOT NULL, checked INTEGER NOT NULL, origin TEXT NOT NULL);"""
SCHEMA = (
    RECORDS
    + """
CREATE TABLE label_term(term TEXT,id INTEGER,PRIMARY KEY(term,id)) WITHOUT ROWID;
CREATE TABLE label_variant(id INTEGER PRIMARY KEY,forms TEXT NOT NULL);
CREATE VIEW audited_label AS SELECT l.id,l.checked,l.forms||coalesce(char(9)||v.forms,'') forms,
    v.forms variants FROM label_record l LEFT JOIN label_variant v ON v.id=l.id;
CREATE VIEW answer_item AS SELECT i.id,i.name,
    coalesce(l.forms||coalesce(char(9)||v.forms,''),i.aliases) aliases,i.incoming
    FROM item i LEFT JOIN label_record l ON l.id=i.id LEFT JOIN label_variant v ON v.id=i.id;
CREATE VIEW answer_world_name AS
    SELECT n.term,n.id FROM world_name n WHERE n.preferred=1 OR NOT EXISTS
        (SELECT 1 FROM label_record l WHERE l.id=n.id)
    UNION ALL SELECT term,id FROM label_term;
CREATE VIEW answer_scope_name AS SELECT n.* FROM scope_name n WHERE n.preferred=1
    OR NOT EXISTS(SELECT 1 FROM label_record l WHERE l.id=n.id)
    OR EXISTS(SELECT 1 FROM label_term t WHERE (t.term,t.id)=(n.term,n.id))
    OR EXISTS(SELECT 1 FROM attested_name a WHERE (a.term,a.id)=(n.term,n.id));
"""
)
SCHEDULE = """CREATE TABLE IF NOT EXISTS schedule(id INTEGER PRIMARY KEY,cursor INTEGER,next INTEGER);
INSERT OR IGNORE INTO schedule VALUES(1,0,0);
CREATE TABLE IF NOT EXISTS pacing(id INTEGER PRIMARY KEY,queue_stamp INTEGER,earliest INTEGER);
INSERT OR IGNORE INTO pacing VALUES(1,0,0);"""
QUEUE = """CREATE TABLE IF NOT EXISTS wanted(id INTEGER PRIMARY KEY,priority INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS wanted_by_priority ON wanted(priority DESC,id);
CREATE TABLE IF NOT EXISTS requested(bank TEXT PRIMARY KEY) WITHOUT ROWID;"""


def file(storage):
    return storage / "knowledge-sources" / "entity-labels.sqlite"


def forms(name, aliases):
    return "\t".join(
        sorted(
            {
                clean
                for text in (name, *aliases)
                if isinstance(text, str) and 0 < len(clean := " ".join(text.split())) <= 140
            }
        )
    )


def eligibility_key(graph):
    """Identify the public subjects of a bank independently of its wording, scores and aliases."""
    key = hashlib.sha256()
    found = False
    for (item,) in graph.execute("""SELECT id FROM qualified WHERE id GLOB 'wd:Q[1-9]*'
            AND id='wd:Q'||CAST(substr(id,5) AS INTEGER) ORDER BY id"""):
        key.update(item.encode("ascii") + b"\n")
        found = True
    return key.hexdigest() if found else ""


def put(db, rows):
    db.executemany(
        """INSERT INTO label_record VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE
        SET forms=excluded.forms,checked=excluded.checked,origin=excluded.origin
        WHERE (excluded.checked,excluded.origin)>(label_record.checked,label_record.origin)""",
        rows,
    )


def qualified_forms(names, archive):
    """Keep an archived disambiguation only when this entity supplies the title and qualifiers."""
    if not archive or "(" not in archive or "(" not in names:
        return []
    verified = {spelling(name) for name in names.split("\t") if name}
    pattern = re.compile(r"(.+) \(([^()]+)\)")
    parts = [m.groups() for name in verified if (m := pattern.fullmatch(name))]
    stems = verified | {stem for stem, _ in parts}
    qualifiers = {qualifier for _, qualifier in parts}
    years = {q for q in qualifiers if re.fullmatch(r"[0-9]{4}", q)}
    words = {q for q in qualifiers if q.isalpha()}
    combined = {f"{y} {w}" for y in years for w in words} | {f"{w} {y}" for y in years for w in words}
    supported = qualifiers | combined
    result = set()
    for original in archive.split("\t"):
        form = spelling(original)
        if form not in verified and len(original) <= 140 and (m := pattern.fullmatch(form)):
            if m[1] in stems and m[2] in supported:
                result.add(original)
    return sorted(result)


def stage(graph, publication):
    if (
        publication is None
        or not publication.execute("SELECT 1 FROM sqlite_master WHERE name='label_record'").fetchone()
        or not publication.execute("SELECT 1 FROM label_record LIMIT 1").fetchone()
    ):
        return False
    graph.execute("DROP TABLE IF EXISTS temp.candidate_labels")
    graph.execute(
        "CREATE TEMP TABLE candidate_labels(id TEXT PRIMARY KEY,forms TEXT,checked INTEGER,variants TEXT,origin TEXT) WITHOUT ROWID"
    )
    candidates = graph.execute("""SELECT CAST(substr(id,5) AS INTEGER) FROM candidate
        WHERE id='wd:Q'||CAST(substr(id,5) AS INTEGER)""")
    while batch := candidates.fetchmany(1000):
        labels = publication.execute(
            """SELECT 'wd:Q'||a.id,a.forms,a.checked,a.variants,l.origin FROM audited_label a
            JOIN label_record l USING(id) WHERE a.id IN (SELECT value FROM json_each(?))""",
            (json.dumps([r[0] for r in batch]),),
        )
        graph.executemany("INSERT INTO candidate_labels VALUES(?,?,?,?,?)", labels)
    return True


def collect(db, storage, previous):
    import bulk_names
    # Every row in these captures came from a name lookup on the matching Wikidata ID.
    for table, scope in (("date_member", "date_scope"), ("current_member", "current_class")):
        records = db.execute(f"""SELECT m.id,m.name,m.aliases,s.captured,m.rule FROM {table} m
            JOIN {scope} s USING(rule) WHERE m.name IS NOT NULL""")
        put(
            db,
            (
                (item, forms(name, json.loads(aliases)), checked, rule)
                for item, name, aliases, checked, rule in records
            ),
        )
    for key, checked, blob in db.execute("SELECT key,captured,release FROM live_rule"):
        rows = json.loads(gzip.decompress(blob))["rows"]
        put(db, ((int(qid[1:]), forms(name, aliases), checked, key) for qid, name, aliases in rows))
    for path in (previous, file(storage), bulk_names.path(storage)):
        if not path or not path.is_file():
            continue
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
            if source.execute("SELECT 1 FROM sqlite_master WHERE name='label_record'").fetchone():
                # Scoped observations come solely from the installed source releases.
                put(db, source.execute("SELECT * FROM label_record WHERE origin='~wikidata' OR origin GLOB 'dump:*'"))
    signature = hashlib.sha256()
    for record in db.execute("SELECT * FROM label_record ORDER BY id"):
        signature.update(json.dumps(tuple(record),ensure_ascii=False).encode() + b"\n")
    return signature.hexdigest()


def index_names(db, reuse):
    if reuse:
        db.execute("""CREATE TEMP TABLE reusable_labels AS SELECT c.id FROM label_record c JOIN prior.label_record p
            ON p.id=c.id AND p.forms=c.forms""")
        db.execute("CREATE UNIQUE INDEX reusable_id ON reusable_labels(id)")
        db.execute("""INSERT INTO label_variant SELECT v.* FROM prior.label_variant v
            JOIN reusable_labels r ON r.id=v.id""")
        db.execute("""INSERT INTO label_term SELECT t.* FROM prior.label_term t
            CROSS JOIN reusable_labels r ON r.id=t.id""")
    pending = "WHERE NOT EXISTS(SELECT 1 FROM reusable_labels r WHERE r.id=l.id)" if reuse else ""
    records = db.execute(f"""SELECT l.id,l.forms,e.names FROM label_record l LEFT JOIN world.entity e ON e.id=l.id
        {pending} ORDER BY l.id""")
    for item, names, archive in records:
        variants = qualified_forms(names, archive)
        if variants:
            db.execute("INSERT INTO label_variant VALUES(?,?)", (item, "\t".join(variants)))
        db.executemany(
            "INSERT OR IGNORE INTO label_term VALUES(?,?)",
            ((folded_spelling(name), item) for name in [*names.split("\t"), *variants] if name),
        )


def request(storage, publication, game, bank):
    """Queue distinct public IDs. Rooms, guesses and prompt text stay in the game."""
    from watched_rules import requests_folder

    membership = game.execute(
        "SELECT json_extract(metadata,'$.nameAuditSet') FROM kb_bank WHERE id=?", (bank,)
    ).fetchone()
    if not membership or membership[0] == "":
        return
    # Banks from an earlier compiler still have the membership evidence needed below.
    fingerprint = membership[0]
    key = "owners-v2:" + (
        fingerprint if isinstance(fingerprint, str) and re.fullmatch("[0-9a-f]{64}", fingerprint) else bank
    )
    folder = requests_folder(storage)
    folder.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(folder / "entity-labels.sqlite", timeout=2)) as queue:
        queue.executescript(QUEUE)
        if queue.execute("SELECT 1 FROM requested WHERE bank=?", (key,)).fetchone():
            return
        with queue:
            rows = game.execute(
                """WITH owners(entity) AS (
                    SELECT entity FROM kb_member WHERE bank=?
                    UNION ALL
                    SELECT DISTINCT json_extract(p.value,'$.entity') AS other FROM kb_member m,
                        json_each(m.evidence) p WHERE m.bank=? AND p.type='object' AND json_type(p.value,'$.classes')='array'
                        AND json_extract(p.value,'$.entity')<>m.entity)
                SELECT CAST(substr(entity,5) AS INTEGER) FROM owners WHERE entity GLOB 'wd:Q[1-9]*'
                AND entity='wd:Q'||CAST(substr(entity,5) AS INTEGER)""",
                (bank, bank),
            )
            ranked = bool(
                publication.execute("SELECT 1 FROM sqlite_master WHERE name='readership'").fetchone()
            )
            while batch := rows.fetchmany(1000):
                ids = [row[0] for row in batch]
                ranks = (
                    publication.execute(
                        """SELECT j.value,coalesce(r.views,0) FROM json_each(?) j
                    LEFT JOIN readership r ON r.id=j.value""",
                        (json.dumps(ids),),
                    )
                    if ranked
                    else ((i, 0) for i in ids)
                )
                queue.executemany(
                    """INSERT INTO wanted VALUES(?,?) ON CONFLICT(id) DO UPDATE
                    SET priority=excluded.priority WHERE excluded.priority>wanted.priority""",
                    ranks,
                )
            queue.execute("INSERT OR IGNORE INTO requested VALUES(?)", (key,))


def queue_path(storage):
    from watched_rules import requests_folder

    return requests_folder(storage) / "entity-labels.sqlite"


def ready(cache, queue, now):
    next_run, stamp, earliest = cache.execute(
        "SELECT s.next,p.queue_stamp,p.earliest FROM schedule s JOIN pacing p USING(id) WHERE id=1"
    ).fetchone()
    current = queue.stat().st_mtime_ns if queue.is_file() else 0
    return next_run <= now or (earliest <= now and current != stamp), current


def due(storage):
    path = file(storage)
    if not path.is_file():
        return True
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='pacing'").fetchone():
            return True
        return ready(db, queue_path(storage), time.time())[0]


def refresh(world, storage, *, batches=6, size=40):
    from sources import entity_name_records, retry_wait

    path = file(storage)
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, uri=True)) as cache:
        cache.executescript(RECORDS + SCHEDULE)
        now = int(time.time())
        queue = queue_path(storage)
        scheduled, stamp = ready(cache, queue, now)
        if not scheduled:
            return
        cursor = cache.execute("SELECT cursor FROM schedule WHERE id=1").fetchone()[0]
        queued = queue.is_file()
        if queued:
            cache.execute("ATTACH DATABASE ? AS requests", (queue.resolve().as_uri() + "?mode=ro",))
        try:
            for batch in range(batches):
                ids = []
                if queued and batch:
                    # Reserve a batch for renewing old records. Popular answers must not
                    # consume the new-name allowance every time they become due again.
                    if batch == batches - 1:
                        ids = [
                            r[0]
                            for r in cache.execute(
                                """SELECT c.id FROM label_record c
                            JOIN requests.wanted w ON w.id=c.id WHERE w.id>0 AND c.checked<?
                            ORDER BY c.checked,w.priority DESC,w.id LIMIT ?""",
                                (now - 7 * 86400, size),
                            )
                        ]
                    if not ids:
                        ids = [
                            r[0]
                            for r in cache.execute(
                                """SELECT w.id FROM requests.wanted w
                            WHERE w.id>0 AND NOT EXISTS(SELECT 1 FROM label_record c WHERE c.id=w.id)
                            ORDER BY w.priority DESC,w.id LIMIT ?""",
                                (size,),
                            )
                        ]
                scanning = not ids
                if scanning:
                    ids = [
                        r[0]
                        for r in world.execute(
                            "SELECT id FROM world.entity WHERE id>? ORDER BY id LIMIT ?", (cursor, size)
                        )
                    ]
                    if not ids:
                        cursor = 0
                        with cache:
                            cache.execute("UPDATE schedule SET cursor=0 WHERE id=1")
                        continue
                observations = list(entity_name_records(["Q" + str(i) for i in ids], allow_missing=True))
                with cache:
                    put(
                        cache,
                        (
                            (int(qid[1:]), forms(record[1], record[2]) if record else "", now, "~wikidata")
                            for qid, record in observations
                        ),
                    )
                    if scanning:
                        cursor = ids[-1]
                        cache.execute("UPDATE schedule SET cursor=? WHERE id=1", (cursor,))
        except (OSError, ValueError, KeyError):
            with cache:
                retry = int(time.time()) + max(900, retry_wait("www.wikidata.org"))
                cache.execute("UPDATE schedule SET next=? WHERE id=1", (retry,))
                cache.execute("UPDATE pacing SET earliest=?,queue_stamp=? WHERE id=1", (retry, stamp))
            raise
        pending = (
            queued
            and cache.execute("""SELECT 1 FROM requests.wanted w
            WHERE w.id>0 AND NOT EXISTS(SELECT 1 FROM label_record c WHERE c.id=w.id) LIMIT 1""").fetchone()
        )
        finished = int(time.time())
        with cache:
            cache.execute("UPDATE schedule SET next=? WHERE id=1", (finished + (900 if pending else 3600),))
            cache.execute("UPDATE pacing SET earliest=?,queue_stamp=? WHERE id=1", (finished + 60, stamp))
