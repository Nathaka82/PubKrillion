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
CREATE TABLE relationship_proposal(rule TEXT PRIMARY KEY) WITHOUT ROWID;
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
    db.executemany("INSERT OR IGNORE INTO relationship_proposal VALUES(?)", ((key,) for key in proposals(db)))


def proposals(db):
    """Choose the next incomplete relationship to check for each verified roster."""
    verified = {row[0] for row in db.execute("SELECT key FROM live_rule")}
    for anchor, in db.execute("SELECT key FROM live_rule WHERE key GLOB 'w-*' AND n>=3 ORDER BY key"):
        cls, anchor_prop, _ = predicates(anchor)[0]
        # Use at most one source page to prioritise checks. Only a separate full
        # source capture of the compound question can contribute answers.
        pairs = db.execute("""WITH sample AS MATERIALIZED (
                SELECT id FROM scope_witness WHERE (cls,origin)=(?,?) ORDER BY id LIMIT 500),
            edges AS MATERIALIZED (
                SELECT l.id,l.prop,l.target FROM sample s CROSS JOIN world_link l ON l.id=s.id WHERE l.prop<>?),
            coverage AS (SELECT prop,count(DISTINCT id) AS known FROM edges GROUP BY prop),
            pairs AS (SELECT prop,target,count(*) AS hits FROM edges GROUP BY prop,target HAVING count(*)>=2)
            SELECT p.prop,p.target FROM pairs p JOIN coverage c USING(prop)
            JOIN predicate ON predicate.id=p.prop JOIN item ON item.id=p.target
            WHERE c.known<(SELECT count(*) FROM sample)
            ORDER BY p.hits*((SELECT count(*) FROM sample)-c.known) DESC,p.hits DESC,p.prop,p.target""",
            (cls,anchor,anchor_prop))
        for prop, target in pairs:
            operand = f"w-{cls}-{prop}-{target}"
            combined = "and:" + "+".join(sorted((anchor,operand)))
            if operand not in verified and combined not in verified:
                yield combined
                break


def members(db, rule, query, args):
    query, args = with_archived_edge(db, rule, query, args)
    return f"""SELECT id FROM ({query}) UNION
        SELECT w.id FROM relation_witness r JOIN scope_witness w ON w.cls=? AND w.origin=r.origin
        WHERE r.rule=?""", (*args, predicates(rule)[0][0], rule)


STRICT_CLASS = "(w.origin GLOB 'w-*' OR w.origin GLOB 'and:*+w-*')"


def classified_edges(db, rule):
    """Join an independently verified instance classification to an archived edge on the same item."""
    cls, prop, target = predicates(rule)[0]
    # Older captures can count unnamed members without recording their IDs. In
    # that case absence from the list of names cannot establish a withdrawal.
    accounted = "census.scanned=census.n"
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='current_unnamed'").fetchone():
        accounted += """ OR (NOT EXISTS(SELECT 1 FROM current_unnamed u WHERE (u.rule,u.id)=(census.rule,w.id))
            AND census.scanned-census.n=(SELECT count(*) FROM current_unnamed u WHERE u.rule=census.rule))"""
    query = DESCENDANTS + f""" SELECT w.id,w.cls,w.origin FROM scope s CROSS JOIN scope_witness w ON w.cls=s.id
        JOIN live_rule witness ON witness.key=w.origin
        JOIN world_link l ON (l.id,l.prop,l.target)=(w.id,?,?)
        JOIN item i ON i.id=w.id
        WHERE {STRICT_CLASS} AND length(i.name) BETWEEN 1 AND 140
        AND NOT EXISTS (SELECT 1 FROM world_member m WHERE m.id=w.id AND m.cls IN (SELECT id FROM scope))
        AND NOT EXISTS (SELECT 1 FROM current_class census
            WHERE census.rule IN ('type:Q'||w.cls,'type:Q'||?) AND census.captured>=witness.captured
            AND ({accounted})
            AND NOT EXISTS (SELECT 1 FROM current_member cm WHERE (cm.rule,cm.id)=(census.rule,w.id)))
        AND NOT EXISTS (SELECT 1 FROM live_rule exact
            WHERE exact.key='w-'||w.cls||'-'||l.prop||'-'||l.target
            AND NOT EXISTS (SELECT 1 FROM scope_witness h
                WHERE (h.cls,h.origin,h.id)=(w.cls,exact.key,w.id)))"""
    return query, (cls, prop, target, cls)


def with_archived_edge(db, rule, query, args):
    extra, values = classified_edges(db, rule)
    return f"SELECT id FROM ({query}) UNION SELECT id FROM ({extra})", (*args, *values)


def origins(db, rule):
    return db.execute("""SELECT l.key,l.revision,l.captured,l.release FROM relation_witness r
        JOIN live_rule l ON l.key=r.origin WHERE r.rule=? ORDER BY l.captured DESC,l.key""", (rule,))


def add_sources(db, graph, cls, *, rule=None, condition=None, missing_types=None):
    from dated_links import Rule

    if missing_types is not None:
        if not missing_types:
            return
        captures = db.execute(DESCENDANTS + f""" SELECT l.key,l.revision,l.captured,l.release,s.cls
            FROM (SELECT DISTINCT w.cls,w.origin FROM scope CROSS JOIN scope_witness w ON w.cls=scope.id
                WHERE {STRICT_CLASS} AND w.id IN (SELECT value FROM json_each(?))) s
            JOIN live_rule l ON l.key=s.origin ORDER BY l.key""", (cls, json.dumps(sorted(missing_types))))
    elif rule:
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
        rows = payload["rows"]
        if missing_types is not None:
            rows = [row for row in rows if int(row[0][1:]) in missing_types]
        ingest(graph, source, records(db, rows, supported_cls, condition, includes_roles))


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
