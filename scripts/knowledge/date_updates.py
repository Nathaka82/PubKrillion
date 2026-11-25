"""Collect resumable, source-attributed Wikidata date statements for the publisher."""

import hashlib
import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import date

from class_updates import due as cache_due
from class_updates import page_names
from date_rules import Rule
from world_facts import bindings

FORMAT = 1
PREFIX = """PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>
    PREFIX psv: <http://www.wikidata.org/prop/statement/value/> """
SCHEMA = """
CREATE TABLE IF NOT EXISTS progress(rule TEXT PRIMARY KEY,cursor TEXT);
CREATE TABLE IF NOT EXISTS paged(active INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS member(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fact(statement TEXT PRIMARY KEY,id INTEGER,year INTEGER,value TEXT,precision INTEGER) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS facts_by_entity ON fact(id);
CREATE TABLE IF NOT EXISTS release(rule TEXT PRIMARY KEY,revision TEXT,captured INTEGER);
"""


def registered(storage):
    from date_discovery import subscriptions
    from graph import digest
    from watched_rules import rule_keys

    folder = storage / "knowledge-sources" / "world-dates"
    explicit = dict(rule_keys(storage))
    for key, stem in explicit.items():
        if Rule.decode(key):
            yield key, folder / (stem + ".sqlite")
    for key in subscriptions(storage):
        if key not in explicit and Rule.decode(key):
            yield key, folder / (digest(key) + ".sqlite")


def due(file):
    return cache_due(file, FORMAT)


def body(rule, *, narrow_first=False):
    lower = f'"{rule.lo:04}-01-01T00:00:00Z"^^xsd:dateTime'
    upper = (
        f'?date < "{rule.hi + 1:04}-01-01T00:00:00Z"^^xsd:dateTime'
        if rule.hi < 9999
        else '?date <= "9999-12-31T23:59:59Z"^^xsd:dateTime'
    )
    dates = f"?item wdt:P{rule.prop} ?date . FILTER(?date >= {lower} && {upper})"
    if narrow_first:
        dates = "{ SELECT DISTINCT ?item ?date WHERE { " + dates + " } }"
    return f"""{dates}
        ?item (wdt:P31|wdt:P106)/wdt:P279* wd:Q{rule.cls}; p:P{rule.prop} ?s .
        ?s a wikibase:BestRank; psv:P{rule.prop} ?v .
        ?v wikibase:timeValue ?date; wikibase:timePrecision ?precision; wikibase:timeCalendarModel wd:Q1985727 .
        FILTER(?precision >= 9 && ?precision <= 14)"""


def fingerprint(db, key):
    checksum = hashlib.sha256(key.encode("ascii"))
    for query in ("SELECT * FROM member ORDER BY id", "SELECT * FROM fact ORDER BY statement"):
        for row in db.execute(query):
            checksum.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    return checksum.hexdigest()


def parse_row(row, rule):
    item, statement, raw = (row[k]["value"] for k in ("item", "s", "date"))
    owner = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d*)", item)
    claim = re.fullmatch(
        r"https?://www\.wikidata\.org/entity/statement/([Qq][1-9]\d*-[a-zA-Z0-9-]+)", statement
    )
    when = re.fullmatch(r"\+?(\d{4})-(\d\d)-(\d\d)T\d\d:\d\d:\d\d(?:\.\d+)?Z", raw)
    if not owner or not claim or claim[1].split("-", 1)[0].upper() != "Q" + owner[1] or not when:
        raise ValueError("The source returned an inconsistent date statement or owner.")
    precision = int(row["precision"]["value"])
    year, month, day = map(int, when.groups())
    if not rule.lo <= year <= rule.hi or not 9 <= precision <= 14:
        raise ValueError("The source returned a date outside the requested precision or years.")
    date(year, month if precision >= 10 else 1, day if precision >= 11 else 1)
    return claim[1], int(owner[1]), year, raw.removeprefix("+"), precision


def collect_facts(db, rule, page_size, limit):
    """Small ranges need just one request. Large ranges advance a durable statement cursor."""
    cursor = db.execute("SELECT cursor FROM progress WHERE rule=?", (rule.key,)).fetchone()[0]
    if cursor == "labels":
        return
    fields = "SELECT DISTINCT ?s ?item ?date ?precision"
    if not db.execute("SELECT 1 FROM paged").fetchone():
        try:
            page = bindings(PREFIX + f"{fields} WHERE {{ {body(rule)} }} LIMIT {limit}")
        except (OSError, ValueError):
            page = None
        if page is not None and len(page) < limit:
            facts = [parse_row(row, rule) for row in page]
            with db:
                db.execute("DELETE FROM fact")
                db.executemany("INSERT INTO fact VALUES(?,?,?,?,?)", facts)
                # Reuse completed name batches when recovering a previously paged snapshot.
                db.execute("DELETE FROM member WHERE id NOT IN (SELECT id FROM fact)")
                db.execute("UPDATE progress SET cursor='labels' WHERE rule=?", (rule.key,))
            return
        with db:
            db.execute("INSERT INTO paged VALUES(1)")
    after = f"FILTER(STR(?s)>{json.dumps(cursor)})" if cursor else ""
    page = bindings(
        PREFIX
        + f"{fields} WHERE {{ {body(rule, narrow_first=True)} {after} }} ORDER BY STR(?s) LIMIT {page_size}"
    )
    urls = [row["s"]["value"] for row in page]
    if len(page) > page_size or urls != sorted(set(urls)) or (urls and urls[0] <= cursor):
        raise ValueError("The source returned an inconsistent page of date statements.")
    with db:
        db.executemany("INSERT INTO fact VALUES(?,?,?,?,?)", (parse_row(row, rule) for row in page))
        next_cursor = urls[-1] if len(page) == page_size else "labels"
        db.execute("UPDATE progress SET cursor=? WHERE rule=?", (next_cursor, rule.key))


def collect_names(db, page_size):
    ids = [
        r[0]
        for r in db.execute(
            """SELECT DISTINCT f.id FROM fact f WHERE NOT EXISTS
        (SELECT 1 FROM member m WHERE m.id=f.id) ORDER BY f.id LIMIT ?""",
            (page_size,),
        )
    ]
    names = {i: (None, "[]") for i in ids}
    items = [{"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}} for i in ids]
    for qid, name, aliases, _ in page_names(items):
        i = int(qid[1:])
        if i not in names or names[i][0] is not None:
            raise ValueError("A date record received an unexpected source name.")
        names[i] = name, json.dumps(sorted(aliases), ensure_ascii=False)
    with db:
        db.executemany("INSERT INTO member VALUES(?,?,?)", ((i, *name) for i, name in names.items()))
    return len(ids) < page_size


def finish(db, rule):
    totals = db.execute("SELECT (SELECT count(*) FROM fact),(SELECT count(*) FROM member)").fetchone()
    if db.execute("SELECT 1 FROM paged").fetchone():
        counts = bindings(
            PREFIX
            + f"SELECT (COUNT(DISTINCT ?s) AS ?n) (COUNT(DISTINCT ?item) AS ?members) WHERE {{ {body(rule, narrow_first=True)} }}"
        )
        expected = tuple(int(counts[0][k]["value"]) for k in ("n", "members")) if len(counts) == 1 else ()
        verified = expected == totals
    else:
        # Re-read the whole bounded range after collecting names. The extra row
        # detects growth, and comparing values catches same-size substitutions.
        page = bindings(
            PREFIX
            + f"SELECT DISTINCT ?s ?item ?date ?precision WHERE {{ {body(rule)} }} LIMIT {totals[0] + 1}"
        )
        current = {parse_row(row, rule) for row in page}
        verified = (
            len(page) == len(current) == totals[0]
            and len({row[1] for row in current}) == totals[1]
            and current == set(db.execute("SELECT * FROM fact"))
        )
    if not verified:
        with db:
            db.execute("DELETE FROM fact")
            db.execute("DELETE FROM member")
            db.execute("DELETE FROM paged")
            db.execute("UPDATE progress SET cursor=''")
        raise ValueError("The source dates changed during collection. The publisher will repeat the check.")
    if totals[1] and not db.execute("SELECT 1 FROM member WHERE name IS NOT NULL LIMIT 1").fetchone():
        with db:
            db.execute("DELETE FROM member")
        raise ValueError("The source returned dated entities without usable names.")
    with db:
        db.execute(
            "INSERT INTO release VALUES(?,?,?)", (rule.key, fingerprint(db, rule.key), int(time.time()))
        )


def advance(key, file, *, pages=2, page_size=320, bulk_limit=8000):
    rule = Rule.decode(key)
    if not rule:
        raise ValueError("Unknown date rule.")
    partial = file.with_suffix(".partial")
    file.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.executescript(SCHEMA)
        with db:
            db.execute(f"PRAGMA user_version={FORMAT}")
            db.execute("INSERT OR IGNORE INTO progress VALUES(?,'')", (key,))
        for _ in range(pages):
            if db.execute("SELECT 1 FROM release WHERE rule=?", (key,)).fetchone():
                break
            collect_facts(db, rule, page_size, bulk_limit)
            cursor = db.execute("SELECT cursor FROM progress WHERE rule=?", (key,)).fetchone()[0]
            if cursor == "labels" and collect_names(db, page_size):
                finish(db, rule)
                break
        finished = db.execute("SELECT 1 FROM release WHERE rule=?", (key,)).fetchone()
    if finished:
        os.replace(partial, file)
        file.with_suffix(".retry").unlink(missing_ok=True)
    return bool(finished)


def collect(db, storage, offline, previous=None):
    import date_subjects
    from watched_rules import rule_keys
    from world_index import install_lock

    revisions = []
    attempted = 0
    requested = {key for key, _ in rule_keys(storage)}
    source_files = list(registered(storage))
    classes = {Rule.decode(key).cls for key, _ in source_files}
    from dated_links import Rule as DatedLink
    classes.update(rule.date.cls for key in requested if (rule:=DatedLink.decode(key)))
    if not offline:
        try:
            date_subjects.refresh(storage, classes)
        except (OSError, ValueError, KeyError) as error:
            print(f"Date-subject update deferred: {error}", flush=True)
    wording_revision = date_subjects.install(db, storage, classes, previous)
    for key, file in sorted(source_files, key=lambda pair: (pair[0] not in requested, due(pair[1]), pair[0])):
        rule = Rule.decode(key)
        known = (
            db.execute("SELECT 1 FROM date_property WHERE prop=?", (rule.prop,)).fetchone()
            and db.execute("SELECT 1 FROM world.entity WHERE id=?", (rule.cls,)).fetchone()
            and db.execute("SELECT 1 FROM date_subject WHERE cls=?", (rule.cls,)).fetchone()
        )
        if not known:
            if not offline and due(file) <= time.time():
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".retry").touch()
            continue
        if not offline and attempted < 4 and due(file) <= time.time():
            attempted += 1
            try:
                lock = file.with_suffix(".build")
                lock.mkdir(parents=True, exist_ok=True)
                with install_lock(lock):
                    if due(file) <= time.time():
                        advance(key, file)
            except (OSError, ValueError, KeyError, sqlite3.IntegrityError) as error:
                file.with_suffix(".retry").touch()
                print(f"Date update for {key} deferred: {error}", flush=True)
        if not file.is_file():
            continue
        with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)) as src:
            release = src.execute("SELECT revision,captured FROM release WHERE rule=?", (key,)).fetchone()
            if not release or release[0] != fingerprint(src, key):
                raise ValueError("The dated source snapshot failed verification.")
            db.execute(
                "INSERT INTO date_scope VALUES(?,?,?,?,?,?,?)",
                (key, rule.cls, rule.prop, rule.lo, rule.hi, *release),
            )
            db.executemany(
                "INSERT INTO date_member VALUES(?,?,?,?)",
                ((key, *r) for r in src.execute("SELECT * FROM member")),
            )
            db.executemany(
                "INSERT INTO date_fact VALUES(?,?,?,?,?,?)",
                ((key, *r) for r in src.execute("SELECT * FROM fact")),
            )
            revisions.append((rule, release[0], release[1]))
    # An unchanged recapture can supersede a conflicting overlapping slice.
    # Preserve their relative precedence in the edition fingerprint.
    revisions.sort(key=lambda row: (row[0].cls, row[0].prop, -row[2], row[0].hi - row[0].lo, row[0].key))
    return [
        ("date-subject-labels", wording_revision),
        *((rule.key, revision) for rule, revision, _ in revisions),
    ]
