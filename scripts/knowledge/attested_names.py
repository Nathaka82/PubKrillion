"""Collect names recorded as Wikidata statements and join them by subject ID."""

import gzip
import hashlib
import json
import re
import sqlite3
import time
from contextlib import closing

from graph import folded_spelling
from world_facts import bindings

PROPERTIES = {1448: "Official name", 1705: "Native label", 1813: "Short name", 1476: "Title"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS attested_name(
    id INTEGER NOT NULL, statement TEXT NOT NULL, prop INTEGER NOT NULL, lang TEXT NOT NULL,
    display TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY(id,statement)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS attested_by_term ON attested_name(term,id);
"""
STATUS = """
CREATE TABLE IF NOT EXISTS checked(id INTEGER PRIMARY KEY,at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS progress(singleton INTEGER PRIMARY KEY,cursor INTEGER NOT NULL,next INTEGER NOT NULL);
INSERT OR IGNORE INTO progress VALUES(1,0,0);
"""
ENTITY = re.compile(r"https?://www\.wikidata\.org/entity/Q([1-9][0-9]*)")
STATEMENT = re.compile(r"https?://www\.wikidata\.org/entity/statement/([Qq]([1-9][0-9]*)-[A-Za-z0-9-]+)")


def location(storage):
    return storage / "knowledge-sources" / "attested-names.sqlite"


def is_due(storage):
    path = location(storage)
    if not path.exists():
        return True
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as cache:
        if not cache.execute("SELECT 1 FROM sqlite_master WHERE name='progress'").fetchone():
            return True
        row = cache.execute("SELECT next FROM progress").fetchone()
        return not row or row[0] <= time.time()


def capture(ids):
    """Require an independent statement count for every requested entity, including zero."""
    entities = " ".join("wd:Q" + str(i) for i in ids)
    branches = " UNION ".join(
        f"""{{ VALUES ?item {{ {entities} }}
        ?item p:P{i} ?s . ?s a wikibase:BestRank; ps:P{i} ?name . BIND(p:P{i} AS ?property) }}"""
        for i in PROPERTIES
    )
    scope = f"""{{ VALUES ?item {{ {entities} }} }} UNION {{ {{ {branches} }}
        FILTER(isLiteral(?name) && LANG(?name)!="" && STRLEN(STR(?name))<=140)
        FILTER NOT EXISTS {{ ?s <http://www.wikidata.org/prop/qualifier/P518> ?part }}
        FILTER NOT EXISTS {{ ?s <http://www.wikidata.org/prop/qualifier/P1480> ?uncertainty }}
    }}"""
    prefix = "PREFIX ps: <http://www.wikidata.org/prop/statement/> "
    seen = set()
    claims = {}
    counts = {i: 0 for i in ids}

    def owner(row):
        item = ENTITY.fullmatch(row["item"]["value"])
        if not item or int(item[1]) not in counts:
            raise ValueError("The name source returned an unexpected entity.")
        return int(item[1])

    for row in bindings(prefix + f"SELECT DISTINCT ?item ?s ?property ?name WHERE {{ {scope} }}"):
        item = owner(row)
        seen.add(item)
        if "s" not in row and "name" not in row and "property" not in row:
            continue
        guid = STATEMENT.fullmatch(row["s"]["value"])
        prop = re.fullmatch(r"https?://www\.wikidata\.org/prop/P([0-9]+)", row["property"]["value"])
        name = row["name"]["value"].strip()
        lang = row["name"].get("xml:lang")
        if (
            not guid
            or int(guid[2]) != item
            or not prop
            or int(prop[1]) not in PROPERTIES
            or not lang
            or not 0 < len(name) <= 140
            or guid[1] in claims
        ):
            raise ValueError("The source returned an inconsistent name statement.")
        claims[guid[1]] = (item, guid[1], int(prop[1]), lang, name, folded_spelling(name))
        counts[item] += 1
    totals = {}
    for row in bindings(
        prefix + f"SELECT ?item (COUNT(DISTINCT ?s) AS ?n) WHERE {{ {scope} }} GROUP BY ?item"
    ):
        item = owner(row)
        if item in totals or not row["n"]["value"].isdecimal():
            raise ValueError("The source returned an inconsistent name count.")
        totals[item] = int(row["n"]["value"])
    if seen != set(ids) or totals != counts:
        raise ValueError("The source names changed during collection or the response was incomplete.")
    return claims.values()


def refresh(db, storage, *, previous=None, batches=12, size=65):
    file = location(storage)
    file.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    with closing(sqlite3.connect(file, timeout=15)) as cache:
        cache.executescript(SCHEMA + STATUS)
        if (
            previous is not None
            and previous.is_file()
            and not cache.execute("SELECT 1 FROM checked LIMIT 1").fetchone()
        ):
            with closing(sqlite3.connect(previous.resolve().as_uri() + "?mode=ro", uri=True)) as prior:
                if prior.execute("SELECT 1 FROM sqlite_master WHERE name='attested_name'").fetchone():
                    with cache:
                        cache.executemany(
                            "INSERT OR IGNORE INTO attested_name VALUES(?,?,?,?,?,?)",
                            prior.execute("SELECT * FROM attested_name"),
                        )
        cursor, due = cache.execute("SELECT cursor,next FROM progress").fetchone()
        if now < due:
            return
        # One batch per pass walks the wider world graph, even when popular questions are busy.
        # Select the remaining batches from verified classes and relationships by oldest check.
        cache.execute("CREATE TEMP TABLE priority(id INTEGER PRIMARY KEY)")
        for table in ("date_member", "current_member"):
            cache.executemany(
                "INSERT OR IGNORE INTO priority VALUES(?)", db.execute(f"SELECT DISTINCT id FROM {table}")
            )
        for (payload,) in db.execute("SELECT release FROM live_rule"):
            rows = json.loads(gzip.decompress(payload))["rows"]
            cache.executemany(
                "INSERT OR IGNORE INTO priority VALUES(?)", ((int(qid[1:]),) for qid, _, _ in rows)
            )
        cache.commit()
        try:
            for batch in range(batches):
                scanning = batch == 0
                if not scanning:
                    ids = [
                        i
                        for (i,) in cache.execute(
                            """SELECT p.id FROM priority p LEFT JOIN checked c ON c.id=p.id
                        WHERE c.at IS NULL OR c.at<? ORDER BY coalesce(c.at,0),p.id LIMIT ?""",
                            (now - 7 * 86400, size),
                        )
                    ]
                    scanning = not ids
                if scanning:
                    ids = [
                        r[0]
                        for r in db.execute(
                            "SELECT id FROM world.entity WHERE id>? ORDER BY id LIMIT ?", (cursor, size)
                        )
                    ]
                    if not ids:
                        with cache:
                            cache.execute("UPDATE progress SET cursor=0")
                        cursor = 0
                        if batch == 0:
                            continue
                        break
                    cursor = ids[-1]
                claims = capture(ids)
                with cache:
                    cache.executemany("DELETE FROM attested_name WHERE id=?", ((i,) for i in ids))
                    cache.executemany("INSERT INTO attested_name VALUES(?,?,?,?,?,?)", claims)
                    cache.executemany("INSERT OR REPLACE INTO checked VALUES(?,?)", ((i, now) for i in ids))
                    if scanning:
                        cache.execute("UPDATE progress SET cursor=?", (cursor,))
        except (OSError, ValueError, KeyError):
            with cache:
                cache.execute("UPDATE progress SET next=?", (now + 900,))
            raise
        else:
            with cache:
                cache.execute("UPDATE progress SET next=?", (now + 3600,))


def install(db, storage, previous):
    """Only content changes create an edition. An unavailable cache can reuse a published capture."""
    path = location(storage)
    if not path.is_file():
        path = previous
    if not path.is_file():
        return None
    checksum = hashlib.sha256()
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        if not source.execute("SELECT 1 FROM sqlite_master WHERE name='attested_name'").fetchone():
            return None

        def facts():
            for row in source.execute("SELECT * FROM attested_name ORDER BY id,statement"):
                checksum.update(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
                yield row

        db.executemany("INSERT INTO attested_name VALUES(?,?,?,?,?,?)", facts())
    return checksum.hexdigest() if db.execute("SELECT 1 FROM attested_name LIMIT 1").fetchone() else None


def stage(graph, publication):
    """Look up statements only for candidate entities. No name can establish eligibility."""
    if (
        publication is None
        or not publication.execute("SELECT 1 FROM sqlite_master WHERE name='attested_name'").fetchone()
    ):
        return False
    if not publication.execute("SELECT 1 FROM attested_name LIMIT 1").fetchone():
        return False
    graph.executescript("""DROP TABLE IF EXISTS temp.answer_name_claim;
        DROP TABLE IF EXISTS temp.added_name;
        CREATE TEMP TABLE added_name(term TEXT,display TEXT, PRIMARY KEY(term,display)) WITHOUT ROWID;
        CREATE TEMP TABLE answer_name_claim(id TEXT, statement TEXT,prop INTEGER,lang TEXT,display TEXT,term TEXT,
            PRIMARY KEY(id,statement)) WITHOUT ROWID;""")
    candidates = graph.execute("""SELECT CAST(substr(id,5) AS INTEGER) FROM candidate
        WHERE id='wd:Q'||CAST(substr(id,5) AS INTEGER)""")
    while items := candidates.fetchmany(500):
        rows = publication.execute(
            """SELECT 'wd:Q'||a.id,statement,prop,lang,display,term FROM json_each(?) j
            CROSS JOIN attested_name a ON a.id=j.value""",
            (json.dumps([row[0] for row in items]),),
        )
        graph.executemany("INSERT INTO answer_name_claim VALUES(?,?,?,?,?,?)", rows)
    return bool(graph.execute("SELECT 1 FROM answer_name_claim LIMIT 1").fetchone())


def conflict_names(graph, publication):
    """Consider other known owners before scoring an added spelling, including out-of-scope owners."""
    yield from graph.execute("""SELECT c.id,c.display,0 FROM answer_name_claim c
        WHERE NOT EXISTS(SELECT 1 FROM qualified q WHERE q.id=c.id)""")
    terms = graph.execute("SELECT term,display FROM added_name")
    version = publication.execute("SELECT format FROM publication").fetchone()[0]
    scope_table = "scope_name" if version >= 23 else "date_name"
    world_names = "answer_world_name" if version >= 33 else "world_name"
    scope_names = "answer_scope_name" if version >= 33 else scope_table
    while batch := terms.fetchmany(400):
        displays = {}
        for term, name in batch:
            displays.setdefault(term, set()).add(name)
        rows = publication.execute(
            f"""WITH words AS (SELECT value term FROM json_each(?))
            SELECT w.term,a.id FROM words w CROSS JOIN attested_name a ON a.term=w.term
            UNION SELECT n.term,n.id FROM {world_names} n WHERE n.term IN (SELECT term FROM words)
            UNION SELECT w.term,n.id FROM words w CROSS JOIN {scope_names} n ON n.term=w.term""",
            (json.dumps(list(displays)),),
        )
        for term, item in rows:
            for name in displays[term]:
                yield "wd:Q" + str(item), name, False


def evidence(row):
    statement, prop, lang, name = row
    return {
        "source": "Wikidata",
        "nameProperty": PROPERTIES[prop],
        "language": lang,
        "name": name,
        "url": "https://www.wikidata.org/entity/statement/" + statement,
    }
