"""Index explicit creative forms and the conditions verified on those same entities."""

import gzip
import json
import urllib.parse

from graph import digest, ingest
from watched_rules import membership_pattern, predicates
from world_classes import DESCENDANTS


SCHEMA = """
CREATE TABLE work_form_scope(form INTEGER,base INTEGER,PRIMARY KEY(form,base)) WITHOUT ROWID;
CREATE TABLE work_form_item(origin TEXT,id INTEGER,name TEXT,aliases TEXT,PRIMARY KEY(origin,id)) WITHOUT ROWID;
CREATE TABLE work_form_match(rule TEXT,id INTEGER,origin TEXT,PRIMARY KEY(rule,id,origin)) WITHOUT ROWID;
"""


def publish(db):
    # A form such as "portrait" can also denote a format. Require the form to be
    # a subclass of the work class that the source actually verified.
    exact = {}

    def permitted(rule, item):
        if rule not in exact:
            release = db.execute("SELECT release FROM live_rule WHERE key=?", (rule,)).fetchone()
            exact[rule] = ({int(qid[1:]) for qid, _, _ in json.loads(gzip.decompress(release[0]))["rows"]}
                           if release else None)
        return exact[rule] is None or item in exact[rule]

    for origin, blob in db.execute("SELECT key,release FROM live_rule WHERE key LIKE '%-7937-%'"):
        direct = [(part, *condition) for part, condition in zip(operands(origin), predicates(origin))
                  if part.startswith("w-")]
        for form_rule, base, prop, form in direct:
            if prop != 7937 or base == form or not db.execute(
                DESCENDANTS + " SELECT 1 FROM scope WHERE id=?", (base, form)
            ).fetchone():
                continue
            db.execute("INSERT OR IGNORE INTO work_form_scope VALUES(?,?)", (form, base))
            related = [(part, f"w-{form}-{p}-{target}") for part, _, p, target in direct if part != form_rule]
            for qid, name, aliases in json.loads(gzip.decompress(blob))["rows"]:
                item = int(qid[1:])
                if origin != form_rule and not permitted(form_rule, item):
                    continue
                db.execute("INSERT OR IGNORE INTO work_form_item VALUES(?,?,?,?)",
                           (origin, item, name, json.dumps(aliases, ensure_ascii=False)))
                rules = [f"type:Q{form}", *(key for part, key in related if permitted(part, item))]
                db.executemany("INSERT OR IGNORE INTO work_form_match VALUES(?,?,?)",
                               ((rule, item, origin) for rule in rules))


def operands(key):
    return key[4:].split("+") if key.startswith("and:") else [key]


def available(db, key):
    return all(db.execute("SELECT 1 FROM work_form_match WHERE rule=? LIMIT 1", (part,)).fetchone()
               for part in operands(key))


def ids(key):
    parts = operands(key)
    return " INTERSECT ".join("SELECT id FROM work_form_match WHERE rule=?" for _ in parts), tuple(parts)


def union(db, key, query, args):
    if not available(db, key):
        return query, args
    extra, params = ids(key)
    return f"SELECT id FROM ({query}) UNION SELECT id FROM ({extra})", (*args, *params)


def captured(db, key):
    """Yield the actual source, version, and positively witnessed names for one operand."""
    from itertools import groupby
    rows = db.execute("""SELECT m.origin,l.revision,l.captured,i.id,i.name,i.aliases
        FROM work_form_match m JOIN work_form_item i ON (i.origin,i.id)=(m.origin,m.id)
        JOIN live_rule l ON l.key=m.origin WHERE m.rule=? ORDER BY m.origin,i.id""", (key,))
    for source, group in groupby(rows, key=lambda row: (row[0], row[1], row[2])):
        yield (*source, [("Q"+str(r[3]), r[4], json.loads(r[5])) for r in group])


def add_sources(db, graph, key, root, conditions=(), included=None):
    parts = operands(key)
    if len(parts) > 1 and included is None:
        query, args = ids(key)
        included = {row[0] for row in db.execute(query, args)}
    for index, part in enumerate(parts):
        for origin, revision, checked, rows in captured(db, part):
            pattern = membership_pattern(operands(origin), predicates(origin))
            def url(query):
                return "https://query.wikidata.org/#" + urllib.parse.quote(query, safe="")
            src = {"source": "wikidata-work-form-" + digest([part, origin]), "revision": revision,
                   "captured": checked, "url": url("SELECT DISTINCT ?item WHERE { " + pattern + " }")}
            def records():
                for qid, name, aliases in rows:
                    item = int(qid[1:])
                    if included is not None and item not in included:
                        continue
                    known = db.execute("SELECT incoming FROM item WHERE id=?", (item,)).fetchone()
                    yield {"id": "wd:"+qid, "name": name, "aliases": aliases, "types": [root],
                           "url": url("ASK { " + pattern.replace("?item", "wd:"+qid) + " }"),
                           "edges": [(conditions[index]["relation"], conditions[index]["target"])] if conditions else [],
                           "graph_recognition": [0, known[0] if known else 0]}
            ingest(graph, src, records())


def subscriptions(db, requested):
    """Keep the independent form branch refreshed alongside a host's nominal question."""
    for key in requested:
        for part in operands(key):
            if part.startswith("type:Q"):
                form = int(part[6:])
                relation = None
            elif part.startswith("w-") and predicates(part):
                form, prop, target = predicates(part)[0]
                relation = (prop, target)
            else:
                continue
            for base, in db.execute("SELECT base FROM work_form_scope WHERE form=?", (form,)):
                form_rule = f"w-{base}-7937-{form}"
                if relation:
                    other = f"w-{base}-{relation[0]}-{relation[1]}"
                    if other != form_rule:
                        yield "and:" + "+".join(sorted((other, form_rule)))
                        continue
                yield form_rule
