"""Find playable date questions from rotating samples of the source graph."""

import math
import re
import sqlite3
import time
from collections import defaultdict
from contextlib import closing

from date_rules import Rule
from graph import digest
from world_facts import bindings

SAMPLE_VERSION = 1


def state_file(storage):
    return storage / "knowledge-sources" / "date-discovery.sqlite"


def subscriptions(storage):
    path = state_file(storage)
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as state:
            if state.execute("SELECT 1 FROM sqlite_master WHERE name='subscription'").fetchone():
                yield from (row[0] for row in state.execute("SELECT rule FROM subscription ORDER BY rule"))


def is_due(storage):
    path = state_file(storage)
    if not path.is_file():
        return True
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as state:
        if state.execute("PRAGMA user_version").fetchone()[0] != SAMPLE_VERSION:
            return True
        return state.execute("SELECT next_check FROM progress").fetchone()[0] <= time.time()


def sample(db, cursor, cycle):
    classes = [
        r[0]
        for r in db.execute(
            """WITH population AS (SELECT o cls,count(*) n FROM world.triple
                WHERE p=31 GROUP BY o HAVING count(*)>=5), strata AS (
                SELECT cls,CASE WHEN n>=1000 THEN 0 WHEN n>=100 THEN 1 ELSE 2 END band FROM population),
                ranked AS (SELECT cls,band,row_number() OVER(PARTITION BY band
                    ORDER BY (cls*2654435761)%4294967296,cls) pos FROM strata)
                SELECT cls FROM ranked ORDER BY pos,band LIMIT 24 OFFSET ?""",
            (cursor,),
        )
    ]
    members = set()
    for cls in classes:
        rows = db.execute(
            """SELECT s FROM world.triple WHERE p=31 AND o=? ORDER BY s
            LIMIT 5 OFFSET ?""",
            (cls, 5 * (cycle % 8)),
        ).fetchall()
        if len(rows) >= 3:
            members.update((r[0], cls) for r in rows)
    return members, cursor + len(classes) if classes else 0, cycle if classes else cycle + 1


def candidates(db, members, used):
    if not members:
        return []
    terms = " ".join(f"(wd:Q{i} wd:Q{cls})" for i, cls in sorted(members))
    rows = bindings(f"""SELECT DISTINCT ?item ?class ?property ?date WHERE {{
        VALUES (?item ?class) {{ {terms} }} ?item wdt:P31 ?class; ?predicate ?date .
        ?property wikibase:propertyType wikibase:Time; wikibase:directClaim ?predicate . }}""")
    known = {r[0] for r in db.execute("SELECT prop FROM date_property")}
    titles = {
        i: row[0]
        for i, _ in members
        if (row := db.execute("SELECT name FROM world.title WHERE id=?", (i,)).fetchone())
    }
    witnesses = defaultdict(set)
    for row in rows:
        ids = [
            re.fullmatch(r"https?://www\.wikidata\.org/entity/" + prefix + r"([1-9]\d*)", row[field]["value"])
            for field, prefix in (("item", "Q"), ("class", "Q"), ("property", "P"))
        ]
        year = re.match(r"\+?(\d{4})-", row["date"]["value"])
        if not all(ids) or not year:
            continue
        item, cls, prop = (int(part[1]) for part in ids)
        if (item, cls) in members and prop in known and int(year[1]) > 0:
            if re.search(r"\b" + year[1] + r"\b", titles.get(item, "")):
                continue
            rule = Rule(cls, prop, int(year[1]), int(year[1]))
            if not any(r.cls == cls and r.prop == prop and r.lo <= rule.lo <= r.hi for r in used):
                witnesses[rule].add(item)
    pairs = {(r.cls, r.prop) for r in used}
    used_classes = {r.cls for r in used}
    sizes = {
        cls: db.execute("SELECT count(*) FROM world.triple WHERE p=31 AND o=?", (cls,)).fetchone()[0]
        for cls in {r.cls for r in witnesses}
    }
    spans = defaultdict(list)
    for r in witnesses:
        spans[r.cls, r.prop].append(r.lo)

    def rank(r):
        years = spans[r.cls, r.prop]
        estimate = sizes[r.cls] / max(10, max(years) - min(years) + 1)
        return (
            (r.cls, r.prop) in pairs,
            r.cls in used_classes,
            abs(math.log(max(1, estimate) / 40)),
            -len(witnesses[r]),
            digest(r.key),
        )

    return sorted(witnesses, key=rank)


def tick(db, storage):
    from date_updates import PREFIX, body
    from watched_rules import rule_keys

    file = state_file(storage)
    file.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(file)) as state:
        state.executescript("""BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS progress(cursor INTEGER,cycle INTEGER,next_check INTEGER);
            INSERT INTO progress SELECT 0,0,0 WHERE NOT EXISTS(SELECT 1 FROM progress);
            CREATE TABLE IF NOT EXISTS subscription(rule TEXT PRIMARY KEY); COMMIT;""")
        if state.execute("PRAGMA user_version").fetchone()[0] != SAMPLE_VERSION:
            with state:
                state.execute("UPDATE progress SET cursor=0,cycle=0,next_check=0")
                state.execute(f"PRAGMA user_version={SAMPLE_VERSION}")
        cursor, cycle, due = state.execute("SELECT * FROM progress").fetchone()
        now = int(time.time())
        if due > now:
            return []
        with state:
            state.execute("UPDATE progress SET next_check=?", (now + 900,))
        subscribed = {r[0] for r in state.execute("SELECT rule FROM subscription")}
        cache = storage / "knowledge-sources" / "world-dates"
        pending = sum(not (cache / (digest(k) + ".sqlite")).is_file() for k in subscribed)
        if pending >= 2:
            return []
        members, next_cursor, next_cycle = sample(db, cursor, cycle)
        keys = subscribed | {key for key, _ in rule_keys(storage)}
        used = [r for key in keys if (r := Rule.decode(key))]
        choices = candidates(db, members, used)
        selected, large, tried_classes = [], [], set()
        for rule in choices:
            if rule.cls in tried_classes:
                continue
            tried_classes.add(rule.cls)
            try:
                page = bindings(PREFIX + f"SELECT DISTINCT ?item WHERE {{ {body(rule)} }} LIMIT 251")
            except (OSError, ValueError, KeyError) as error:
                print(f"Date sample for {rule.key} deferred: {error}", flush=True)
                break
            if 5 <= len(page) < 251:
                selected.append(rule)
            elif len(page) == 251:
                large.append(rule)
            if len(selected) >= 2 - pending or len(tried_classes) >= 4:
                break
        # Keep room for new topics even while a broad subscription is collecting.
        if not selected and large:
            selected.append(large[0])
        next_check = now + (3600 if selected else 900)
        with state:
            state.executemany("INSERT OR IGNORE INTO subscription VALUES(?)", ((r.key,) for r in selected))
            state.execute(
                "UPDATE progress SET cursor=?,cycle=?,next_check=?", (next_cursor, next_cycle, next_check)
            )
        return [r.key for r in selected]
