"""Discover new question targets among the current facts of published entities."""

import re
import sqlite3
import time
from contextlib import closing

from graph import digest
from target_lookup import Request
from watched_rules import follow, requests_folder
from world_facts import bindings


def location(storage):
    return storage / "knowledge-sources" / "target-discovery.sqlite"


def is_due(storage):
    file = location(storage)
    if not file.is_file():
        return True
    with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)) as state:
        if not state.execute("SELECT 1 FROM sqlite_master WHERE name='schedule'").fetchone():
            return True
        row = state.execute("SELECT next FROM schedule").fetchone()
        return not row or row[0] <= time.time()


def nominations(db, storage, cls, ids):
    from catalogue import RELATION_PHRASES
    from topic_sources import METADATA_PROPERTIES

    values = " ".join(f"wd:Q{i}" for i in ids)
    rows = bindings(f"""SELECT ?property ?target (COUNT(DISTINCT ?item) AS ?n) WHERE {{
        VALUES ?item {{ {values} }} ?item ?edge ?target .
        ?property wikibase:directClaim ?edge; wikibase:propertyType wikibase:WikibaseItem .
        FILTER(?property NOT IN (wd:P31,wd:P106,wd:P279))
        FILTER(STRSTARTS(STR(?target),"http://www.wikidata.org/entity/Q"))
        }} GROUP BY ?property ?target HAVING(COUNT(DISTINCT ?item)>=2)
        ORDER BY DESC(?n) ?property ?target LIMIT 256""")
    candidates = []
    for row in rows:
        pid = re.fullmatch(r"https?://www\.wikidata\.org/entity/P([1-9]\d*)", row["property"]["value"])
        qid = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d*)", row["target"]["value"])
        hits = int(row["n"]["value"])
        if not pid or not qid or not 2 <= hits <= len(ids):
            raise ValueError("The current graph returned an inconsistent relationship sample.")
        prop, target = int(pid[1]), int(qid[1])
        if cls == target or prop in (31, 106, 279) or prop in METADATA_PROPERTIES:
            continue
        key = f"w-{cls}-{prop}-{target}"
        if db.execute("SELECT 1 FROM live_rule WHERE key=?", (key,)).fetchone():
            continue
        if db.execute(
            "SELECT 1 FROM world_link WHERE prop=? AND target=? LIMIT 1", (prop, target)
        ).fetchone():
            continue
        req = Request(cls, prop, "q" + str(target))
        if (requests_folder(storage) / (digest(req.key) + ".lookup")).exists():
            continue
        candidates.append((prop not in RELATION_PHRASES, hits == len(ids), -hits, prop, target, req))
    selected, chosen = [], set()
    for *_, req in sorted(candidates):
        if req.prop not in chosen:
            selected.append(req)
            chosen.add(req.prop)
            if len(selected) == 2:
                break
    return selected


def tick(storage, publication):
    """Rotate through different subjects, then through their next pages of names."""
    from catalogue import reader

    if not publication.is_file() or not is_due(storage):
        return []
    file = location(storage)
    file.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(file)) as state, closing(reader(publication)) as prior:
        state.executescript("""CREATE TABLE IF NOT EXISTS schedule(next INTEGER);
            CREATE TABLE IF NOT EXISTS progress(cls INTEGER PRIMARY KEY,after_id INTEGER,checked INTEGER);""")
        now = int(time.time())
        with state:
            state.execute("DELETE FROM schedule")
            state.execute("INSERT INTO schedule VALUES(?)", (now + 900,))
        if prior.execute("SELECT format FROM publication").fetchone()[0] < 23:
            return []
        scopes = [
            r[0]
            for r in prior.execute("""SELECT cls FROM scope_name WHERE cls>0 AND preferred=1
                GROUP BY cls HAVING count(DISTINCT id)>=3""")
        ]
        progress = {cls: (checked, last) for cls, last, checked in state.execute("SELECT * FROM progress")}
        found = []
        if scopes:
            cls = min(scopes, key=lambda c: (progress.get(c, (0, 0))[0], c))
            after = progress.get(cls, (0, 0))[1]

            def page(cursor):
                return [
                    r[0]
                    for r in prior.execute(
                        """SELECT DISTINCT id FROM scope_name
                    WHERE cls=? AND preferred=1 AND id>? ORDER BY id LIMIT 48""",
                        (cls, cursor),
                    )
                ]

            ids = page(after) or page(0)
            if len(ids) < 2 and after:
                ids = page(0)
            if ids:
                # Leave a failed page eligible for another pass, and let the
                # next class proceed if a particular source query is unavailable.
                with state:
                    state.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (cls, after, now))
                found = nominations(prior, storage, cls, ids)
                for req in found:
                    if not follow(storage, req.key):
                        raise OSError("Unable to register a discovered target for verification.")
                with state:
                    state.execute("INSERT OR REPLACE INTO progress VALUES(?,?,?)", (cls, ids[-1], now))
        with state:
            state.execute("UPDATE schedule SET next=?", (now + 3600,))
        return found
