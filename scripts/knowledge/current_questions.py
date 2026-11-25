"""Index playable questions from source refreshes by the entities they are about."""

import gzip
import json

import places
from graph import folded_spelling
from watched_rules import predicates
from world_links import identifiers, info

SCHEMA = """
CREATE TABLE current_question(rule TEXT PRIMARY KEY,prompt TEXT NOT NULL,n INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE current_question_topic(entity INTEGER,rule TEXT,PRIMARY KEY(entity,rule)) WITHOUT ROWID;
CREATE TABLE scope_name(cls INTEGER,term TEXT,id INTEGER,display TEXT,preferred INTEGER,
    PRIMARY KEY(cls,term,id,display)) WITHOUT ROWID;
CREATE INDEX scope_names_by_term ON scope_name(term);
"""


def recorded_spellings(db):
    for key,item,name,aliases in db.execute("""SELECT m.rule,i.id,i.name,i.aliases FROM work_form_match m
            JOIN work_form_item i ON (i.origin,i.id)=(m.origin,m.id) WHERE m.rule GLOB 'type:Q*'"""):
        yield int(key[6:]), item, name, json.loads(aliases)
    for cls, item, name, aliases in db.execute("""SELECT s.cls,m.id,m.name,m.aliases FROM date_scope s
            JOIN date_member m USING(rule) WHERE m.name IS NOT NULL
            UNION ALL SELECT CASE WHEN rule LIKE 'type:%' THEN CAST(substr(rule,7) AS INTEGER) ELSE 0 END,
            id,name,aliases FROM current_member WHERE name IS NOT NULL"""):
        yield cls, item, name, json.loads(aliases)
    for key, payload in db.execute("SELECT key,release FROM live_rule"):
        conditions = predicates(key)
        if conditions:
            for qid, name, aliases in json.loads(gzip.decompress(payload))["rows"]:
                yield conditions[0][0], int(qid[1:]), name, aliases


def publish(db):
    from catalogue import FORMAT, current_count, named_question, world_clause
    from world_links import display_subject

    db.executemany(
        """INSERT INTO scope_name VALUES(?,?,?,?,?) ON CONFLICT(cls,term,id,display)
        DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
        (
            (cls, folded_spelling(form), item, form, int(form == name))
            for cls, item, name, aliases in recorded_spellings(db)
            for form in (name, *aliases)
        ),
    )
    # Index name statements under observed classes so that a newly recorded name
    # on an excluded entity can disambiguate a bank that never contained that entity.
    db.executescript("""CREATE TEMP TABLE named_scope(cls INTEGER,id INTEGER,PRIMARY KEY(id,cls)) WITHOUT ROWID;
        INSERT OR IGNORE INTO named_scope SELECT cls,id FROM scope_name;
        INSERT OR IGNORE INTO named_scope SELECT m.cls,m.id FROM (SELECT DISTINCT id FROM attested_name) a
            CROSS JOIN world_member m ON m.id=a.id;
        INSERT OR IGNORE INTO named_scope SELECT r.role,r.id FROM world_role r
            WHERE EXISTS(SELECT 1 FROM attested_name a WHERE a.id=r.id);
        INSERT OR IGNORE INTO scope_name SELECT s.cls,a.term,a.id,a.display,0 FROM attested_name a
            CROSS JOIN named_scope s ON s.id=a.id;
        DROP TABLE named_scope;""")

    # Publish natural questions for qualified forms even before a host asks for one.
    rules = dict(db.execute("SELECT key,n FROM live_rule WHERE n>0 AND key NOT GLOB 'during:*'"))
    import work_forms
    for key, in db.execute("SELECT DISTINCT rule FROM work_form_match WHERE rule GLOB 'w-*'"):
        rules.setdefault(key, 0)
    for key,count in rules.items():
        if work_forms.available(db,key):
            count = current_count(db,key,count,FORMAT)
        parts = key[4:].split("+") if key.startswith("and:") else [key]
        clauses = []
        property_clauses = []
        topics = set()
        for part, (cls, prop, target) in zip(parts, predicates(key)):
            labels = info(db, cls, prop, target)
            if not labels:
                break
            subject, relation, target_name = labels
            subject = display_subject(db,cls,subject)
            unique_target = identifiers(db, folded_spelling(target_name), "target") == [target]
            if part.startswith("g-"):
                label = target_name if unique_target else f"{target_name} (Q{target})"
                clauses.append(f"{places.PHRASES[prop][0]} {label}")
            else:
                label = target_name if unique_target else f"{target_name} (Q{target})"
                clause = world_clause(prop, relation, label)
                clauses.append(clause)
                property_clauses.append(
                    clause if clause.startswith("whose ") else f"whose {relation} is {label}"
                )
            topics.update((cls, target))
        if len(clauses) != len(parts):
            continue
        if len(property_clauses) == 2 and any(c.startswith("whose ") for c in clauses):
            clauses = property_clauses
        prompt = named_question(subject, " and ".join(clauses))
        if len(prompt) <= 240:
            db.execute("INSERT INTO current_question VALUES(?,?,?)", (key, prompt, count))
            db.executemany(
                "INSERT INTO current_question_topic VALUES(?,?)", ((entity, key) for entity in topics)
            )


def cards(db, edition, topic=None):
    if topic is None:
        rows = db.execute("SELECT * FROM current_question WHERE n>=5 ORDER BY random() LIMIT 1")
    elif (
        topic.isascii()
        and topic.isdecimal()
        and len(topic) <= 4
        and db.execute("SELECT format FROM publication").fetchone()[0] >= 20
    ):
        rows = db.execute(
            """SELECT rule,prompt,n FROM question_year JOIN current_question USING(rule)
            WHERE year=? ORDER BY rule GLOB 'date-*' DESC,n DESC,prompt LIMIT 3""",
            (int(topic),),
        )
    else:
        entities = set()
        cls = None
        for role in ("class", "target"):
            if role == "class":
                from date_subjects import resolve

                matches = resolve(db, topic)
            else:
                matches = identifiers(db, topic, role)
            if len(matches) == 1:
                entities.add(matches[0])
                if role == "class":
                    cls = matches[0]
        if not entities:
            return []
        rows = db.execute(
            """WITH RECURSIVE related(id,hops) AS
                (SELECT value,0 FROM json_each(?) UNION SELECT p.child,r.hops+1 FROM related r
                 JOIN world_parent p ON p.parent=r.id WHERE r.hops<2 AND (r.id=? OR r.hops>0))
                SELECT q.rule,q.prompt,q.n FROM related r JOIN current_question_topic t ON t.entity=r.id
                JOIN current_question q USING(rule) GROUP BY q.rule
                ORDER BY min(r.hops),q.n BETWEEN 5 AND 250 DESC,q.n DESC,q.prompt LIMIT 3""",
            (json.dumps(sorted(entities)), cls),
        )
    return [
        {"key": key, "prompt": prompt, "edition": edition, "records": count, "complete": False}
        for key, prompt, count in rows
    ]
