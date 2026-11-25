"""Reuse assertions from verified smaller scopes when compiling a broader open question."""

import gzip
import json
import urllib.parse

from graph import digest, ingest
from watched_rules import membership_pattern, predicates
from world_classes import DESCENDANTS

SCHEMA = """
CREATE TABLE scope_witness(cls INTEGER,origin TEXT,id INTEGER,PRIMARY KEY(cls,origin,id)) WITHOUT ROWID;
CREATE TABLE relation_witness(rule TEXT,origin TEXT,PRIMARY KEY(rule,origin)) WITHOUT ROWID;
"""


def publish(db):
    for origin, blob in db.execute("SELECT key,release FROM live_rule WHERE n>0"):
        parts = origin[4:].split("+") if origin.startswith("and:") else [origin]
        direct = [part for part in parts if part.startswith("w-")]
        conditions = predicates(origin)
        if not conditions:
            continue
        # All captures verify the bare class. Only direct relationship captures
        # also establish the stricter P31 membership of an undated direct rule.
        cls = conditions[0][0]
        payload = json.loads(gzip.decompress(blob))
        db.executemany("INSERT INTO scope_witness VALUES(?,?,?)",
                       ((cls, origin, int(qid[1:])) for qid, _, _ in payload["rows"]))
        if len(parts) > 1:
            db.executemany("INSERT INTO relation_witness VALUES(?,?)", ((rule, origin) for rule in direct))


def members(db, rule, query, args):
    if not db.execute("SELECT 1 FROM relation_witness WHERE rule=? LIMIT 1", (rule,)).fetchone():
        return query, args
    return f"""SELECT id FROM ({query}) UNION
        SELECT w.id FROM relation_witness r JOIN scope_witness w ON w.cls=? AND w.origin=r.origin
        WHERE r.rule=?""", (*args, predicates(rule)[0][0], rule)


def origins(db, rule):
    return db.execute("""SELECT l.key,l.revision,l.captured,l.release FROM relation_witness r
        JOIN live_rule l ON l.key=r.origin WHERE r.rule=? ORDER BY l.captured DESC,l.key""", (rule,))


def add_sources(db, graph, cls, *, rule=None, condition=None):
    from dated_links import Rule

    if rule:
        captures = ((*row, cls) for row in origins(db, rule))
    else:
        captures = db.execute(DESCENDANTS + """ SELECT l.key,l.revision,l.captured,l.release,s.cls
            FROM (SELECT DISTINCT w.cls,w.origin FROM scope CROSS JOIN scope_witness w ON w.cls=scope.id) s
            JOIN live_rule l ON l.key=s.origin ORDER BY l.key""", (cls,))
    for origin, revision, checked, blob, supported_cls in captures:
        payload = json.loads(gzip.decompress(blob))
        parts = origin[4:].split("+") if origin.startswith("and:") else [origin]
        prefix = ""
        if dated := Rule.decode(origin):
            from date_updates import PREFIX, body
            from dated_link_updates import relationship_pattern
            prefix = PREFIX
            pattern = relationship_pattern(dated) + " " + body(dated.date)
        else:
            pattern = membership_pattern(parts, predicates(origin))
        source_url = "https://query.wikidata.org/#" + urllib.parse.quote(
            prefix + f"SELECT DISTINCT ?item WHERE {{ {pattern} }}", safe="")
        source = {"source": "wikidata-query-witness-" + digest(origin), "revision": revision,
                  "captured": checked, "url": source_url}
        includes_roles = bool(dated) or all(part.startswith("g-") for part in parts)
        ingest(graph, source, records(db, payload["rows"], supported_cls, condition, includes_roles))


def records(db, rows, cls, condition, includes_roles):
    from class_updates import evidence

    for qid, name, aliases in rows:
        incoming = db.execute("SELECT incoming FROM item WHERE id=?", (int(qid[1:]),)).fetchone()
        if includes_roles:
            url = evidence(f"type:Q{cls}", qid)
        else:
            query = f"ASK {{ wd:{qid} wdt:P31/wdt:P279* wd:Q{cls} . }}"
            url = "https://query.wikidata.org/#" + urllib.parse.quote(query, safe="")
        yield {
            "id": "wd:" + qid, "name": name, "aliases": aliases, "types": [f"wd:Q{cls}"],
            "url": url,
            "edges": [(condition["relation"], condition["target"])] if condition else [],
            "graph_recognition": [0, incoming[0] if incoming else 0],
        }
