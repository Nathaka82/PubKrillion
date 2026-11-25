"""Index explicit creative forms and the conditions verified on those same entities."""

import gzip
import json
import urllib.parse
from contextlib import closing

from graph import digest, ingest
from watched_rules import membership_pattern, predicates
from world_classes import DESCENDANTS

# Wikidata lists "work" as a subject class for its form-of-creative-work property.
WORK = 386724


SCHEMA = """
CREATE TABLE work_form_scope(form INTEGER,base INTEGER,PRIMARY KEY(form,base)) WITHOUT ROWID;
CREATE TABLE work_form_item(origin TEXT,id INTEGER,name TEXT,aliases TEXT,PRIMARY KEY(origin,id)) WITHOUT ROWID;
CREATE TABLE work_form_match(rule TEXT,id INTEGER,origin TEXT,PRIMARY KEY(rule,id,origin)) WITHOUT ROWID;
"""


def publish(db):
    # A form such as "portrait" can also denote a format. Require the form to be
    # a subclass of the work class that the source actually verified.
    exact = {}
    rosters = {int(rule[6:]): (rule, checked, revision) for rule,checked,revision in db.execute(
        """SELECT rule,captured,revision
           FROM current_class c WHERE rule GLOB 'form:Q*'""")}
    reconcile(db, rosters)
    for form in work_classes(db, rosters):
        db.execute("INSERT OR IGNORE INTO work_form_scope VALUES(?,?)", (form, WORK))
        db.execute("""INSERT INTO work_form_match SELECT ?,id,rule FROM current_member WHERE rule=?""",
                   (f"type:Q{form}", rosters[form][0]))

    def permitted(rule, item, checked):
        if rule not in exact:
            release = db.execute("SELECT captured,release FROM live_rule WHERE key=?", (rule,)).fetchone()
            exact[rule] = ((release[0], {int(qid[1:]) for qid, _, _ in json.loads(gzip.decompress(release[1]))["rows"]})
                           if release else None)
        return exact[rule] is None or checked > exact[rule][0] or item in exact[rule][1]

    for origin, checked, blob in db.execute("SELECT key,captured,release FROM live_rule WHERE key LIKE '%-7937-%'"):
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
                if origin != form_rule and not permitted(form_rule, item, checked):
                    continue
                db.execute("INSERT OR IGNORE INTO work_form_item VALUES(?,?,?,?)",
                           (origin, item, name, json.dumps(aliases, ensure_ascii=False)))
                rules = [f"type:Q{form}", *(key for part, key in related if permitted(part, item, checked))]
                db.executemany("INSERT OR IGNORE INTO work_form_match VALUES(?,?,?)",
                               ((rule, item, origin) for rule in rules))


def reconcile(db, rosters):
    """Apply a newer verified form withdrawal to every wording that explicitly requires it."""
    if not rosters:
        return
    complete = {r for r, in db.execute("""SELECT rule FROM current_class c WHERE rule GLOB 'form:Q*'
        AND scanned-n=(SELECT count(*) FROM current_unnamed u WHERE u.rule=c.rule)""")}
    keys = [k for k, in db.execute("SELECT key FROM live_rule WHERE n>0 AND key LIKE '%-7937-%'")]
    for key in keys:
        revision,checked,n,blob = db.execute("SELECT revision,captured,n,release FROM live_rule WHERE key=?", (key,)).fetchone()
        required = [rosters[form] for part,(base,prop,form) in zip(operands(key),predicates(key))
                    if part.startswith("w-") and prop == 7937 and form in rosters and rosters[form][1] >= checked
                    and rosters[form][0] in complete
                    and (base == WORK or base in work_classes(db, [base]))]
        if not required:
            continue
        data = json.loads(gzip.decompress(blob))
        rows = data["rows"]
        for roster, _, _ in required:
            members = json.dumps([int(qid[1:]) for qid,_,_ in rows])
            keep = {i for i, in db.execute("""SELECT j.value FROM json_each(?1) j WHERE
                EXISTS(SELECT 1 FROM current_member WHERE (rule,id)=(?2,j.value)) OR
                EXISTS(SELECT 1 FROM current_unnamed WHERE (rule,id)=(?2,j.value))""", (members,roster))}
            rows = [r for r in rows if int(r[0][1:]) in keep]
        if len(rows) != n:
            data["rows"] = rows
            data["sourceRevision"] = revision
            data["verifiedForms"] = sorted(required)
            data["revision"] = digest([revision,sorted((rule,ver) for rule,_,ver in required)])
            if data.pop("scanned", None) == n:
                data["scanned"] = len(rows)
            blob = gzip.compress(json.dumps(data,ensure_ascii=False,separators=(",",":")).encode(),mtime=0)
            db.execute("UPDATE live_rule SET n=?,revision=?,release=? WHERE key=?",
                       (len(rows),data["revision"],blob,key))


def operands(key):
    return key[4:].split("+") if key.startswith("and:") else [key]


def precedence(db):
    """Only a change in which overlapping census was checked last changes the edition."""
    checks = dict(db.execute("SELECT key,captured FROM live_rule UNION ALL "
                             "SELECT rule,captured FROM current_class WHERE rule GLOB 'form:Q*'"))
    pairs = set()
    for origin, checked in checks.items():
        parts = operands(origin)
        forms = {f"form:Q{target}" for part,(_,prop,target) in zip(parts,predicates(origin))
                 if part.startswith("w-") and prop == 7937}
        if forms:
            for other in (forms | {p for p in parts if p.startswith("w-")}) - {origin}:
                if other in checks:
                    pairs.add((origin,other,checks[other] >= checked))
    return sorted(pairs)


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
    if key.startswith("type:Q"):
        roster = "form:" + key[5:]
        release = db.execute("SELECT revision,captured FROM current_class WHERE rule=? AND n>0", (roster,)).fetchone()
        if release:
            rows = db.execute("""SELECT m.id,m.name,m.aliases FROM current_member m JOIN work_form_match w
                ON (w.rule,w.id,w.origin)=(?,m.id,m.rule) WHERE m.rule=?""", (key, roster))
            yield roster, *release, (("Q"+str(i), name, json.loads(aliases)) for i,name,aliases in rows)
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
            if origin.startswith("form:"):
                from class_updates import condition
                pattern = condition(origin)
            else:
                pattern = membership_pattern(operands(origin), predicates(origin))
            def url(query):
                return "https://query.wikidata.org/#" + urllib.parse.quote(query, safe="")
            src = {"source": "wikidata-work-form-" + digest([part, origin]), "revision": revision,
                   "captured": checked, "url": url("SELECT DISTINCT ?item WHERE { " + pattern + " }")}
            if not origin.startswith("form:"):
                src["revision"] = digest([revision, [qid for qid,_,_ in rows
                    if included is None or int(qid[1:]) in included]])
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


def work_classes(db, candidates):
    candidates = sorted(set(candidates) - {WORK})
    if not candidates:
        return set()
    return {i for i, in db.execute(DESCENDANTS + """ SELECT id FROM scope
        WHERE id IN (SELECT value FROM json_each(?))""", (WORK, json.dumps(candidates)))}


def prior_work_classes(publication, candidates):
    from catalogue import reader
    if publication and publication.is_file():
        with closing(reader(publication)) as previous:
            if previous.execute("SELECT format FROM publication").fetchone()[0] >= 14:
                return work_classes(previous, candidates)
    return set()


def pending(db, key):
    """An empty nominal check can precede the first check of the creative form."""
    if key.startswith("type:Q"):
        form = int(key[6:])
        return bool(work_classes(db,[form]) and not db.execute(
            "SELECT 1 FROM current_class WHERE rule=?", (f"form:Q{form}",)).fetchone())
    if key.startswith("w-"):
        for rule in subscriptions(db,[key]):
            if not db.execute("SELECT 1 FROM live_rule WHERE key=?", (rule,)).fetchone():
                return True
    return False


def prepare_vocabulary(db, storage, publication, offline):
    from source_vocabulary import collect
    from watched_rules import rule_keys
    classes = (cls for key,_ in rule_keys(storage) for part in operands(key) if part.startswith("w-")
               for cls,_,_ in predicates(part))
    if prior_work_classes(publication, classes):
        collect(db, storage, [("creative-form-relationships", {f"Q{WORK}", "P7937"})], offline)


def subscriptions(db, requested):
    """Keep the independent form branch refreshed alongside a host's nominal question."""
    requested = list(requested)
    classes = {int(key[6:]) for key in requested if key.startswith("type:Q")}
    classes.update(cls for key in requested for part in operands(key) for cls,_,_ in predicates(part))
    eligible = work_classes(db, classes)
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
            bases = {base for base, in db.execute("SELECT base FROM work_form_scope WHERE form=?", (form,))}
            if relation and form in eligible:
                bases.add(WORK)
            for base in sorted(bases):
                form_rule = f"w-{base}-7937-{form}"
                if not relation and form in eligible and (base == WORK or base in work_classes(db,[base])):
                    continue  # The resumable class collector owns this full form roster.
                if relation:
                    other = f"w-{base}-{relation[0]}-{relation[1]}"
                    if other != form_rule:
                        yield "and:" + "+".join(sorted((other, form_rule)))
                        continue
                yield form_rule
