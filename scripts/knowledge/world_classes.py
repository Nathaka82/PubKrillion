"""Resolve named classes and occupations directly from a published Wikidata graph."""

import json
import re
import tempfile
from contextlib import closing
from pathlib import Path

from graph import class_determiner, digest, folded_spelling, ingest, open_graph, question_noun

SCHEMA = """
CREATE TABLE world_parent(parent INTEGER,child INTEGER,PRIMARY KEY(parent,child)) WITHOUT ROWID;
CREATE TABLE world_role(role INTEGER,id INTEGER,PRIMARY KEY(role,id)) WITHOUT ROWID;
CREATE TABLE world_class_name(term TEXT,cls INTEGER,preferred INTEGER,PRIMARY KEY(term,cls)) WITHOUT ROWID;
"""


def publish(db, *, current_taxonomy=False):
    if current_taxonomy:
        db.execute("INSERT INTO world_parent SELECT parent,child FROM taxonomy.edge")
    else:
        db.execute("INSERT OR IGNORE INTO world_parent SELECT o,s FROM world.triple WHERE p=279 AND s<>o")
    db.execute("INSERT OR IGNORE INTO world_role SELECT o,s FROM world.triple WHERE p=106")
    db.execute("CREATE INDEX world_role_by_item ON world_role(id,role)")
    db.execute("CREATE INDEX world_parent_by_child ON world_parent(child,parent)")
    keys = """SELECT cls FROM world_member UNION SELECT role FROM world_role UNION SELECT parent FROM world_parent
              UNION SELECT child FROM world_parent"""

    def names():
        for cls, label, aliases in db.execute(f"""SELECT i.id,i.name,i.aliases FROM ({keys}) k
                JOIN item i ON i.id=k.cls WHERE length(i.name) BETWEEN 1 AND 140"""):
            for alias in {label, *aliases.split("\t")}:
                if alias and len(alias) <= 140:
                    yield folded_spelling(alias), cls, int(alias == label)
            yield "q" + str(cls), cls, 1

    db.executemany(
        """INSERT INTO world_class_name VALUES(?,?,?) ON CONFLICT(term,cls)
                      DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
        names(),
    )


DESCENDANTS = """WITH RECURSIVE scope(id) AS (VALUES(?) UNION SELECT p.child FROM scope s JOIN world_parent p ON p.parent=s.id)"""
MEMBERS = """SELECT m.id,m.cls,31 AS property FROM scope s CROSS JOIN world_member m INDEXED BY world_members_by_class ON m.cls=s.id
             UNION ALL SELECT m.id,m.role,106 FROM scope s CROSS JOIN world_role m ON m.role=s.id"""
KINDS = """SELECT p.child AS id,p.parent AS cls,279 AS property FROM scope s CROSS JOIN world_parent p ON p.parent=s.id
          WHERE p.child<>?"""


def roster_ids(db, key, fmt, live):
    """Selectable entity IDs for one scope, including its published positive evidence."""
    import work_forms
    mode, token = key.split(":")
    kinds, cls = mode == "kind", int(token[1:])
    if live:
        query, args = "SELECT id FROM current_member WHERE rule=?", (key,)
    else:
        args = (cls, cls) if kinds else (cls,)
        witnessed = (" UNION SELECT w.id FROM scope CROSS JOIN scope_witness w ON w.cls=scope.id"
                     if fmt >= 29 and not kinds else "")
        members = KINDS if kinds else MEMBERS
        query = DESCENDANTS + f""" SELECT DISTINCT m.id FROM ({members}) m
                JOIN item i ON i.id=m.id WHERE length(i.name) BETWEEN 1 AND 140{witnessed}"""
    if fmt >= 30 and not kinds and work_forms.available(db, key):
        return work_forms.union(db, key, query, args)
    return query, args


def card(db, phrase, *, strict=False, literal=None):
    kinds = phrase.startswith("type of ")
    term = phrase[8:] if kinds else phrase
    tagged = re.fullmatch(r"(.+) q([1-9]\d{0,11})", term)
    match = "n.term=? AND n.cls=?" if tagged else "n.term=?"
    args = (tagged[1], int(tagged[2])) if tagged else (term,)
    rows = db.execute(
        f"""SELECT n.cls,i.name,n.preferred FROM world_class_name n JOIN item i ON i.id=n.cls
                         WHERE {match} ORDER BY n.preferred DESC,n.cls LIMIT 13""", args,
    ).fetchall()
    if not rows and term.endswith("s"):
        return card(db, ("type of " if kinds else "") + term[:-1], strict=strict,
                    literal=literal[:-1] if literal and literal.endswith("s") else None)
    if not rows:
        return None
    from world_links import ranked_class_ids
    raw = literal.removeprefix("type of ") if kinds and literal else literal
    matches = ranked_class_ids(db, term, raw, [(cls, preferred) for cls, _, preferred in rows])
    rows = [row for row in rows if row[0] in matches]
    if len(rows) > 1:
        if strict:
            options = ", ".join(f"{label} (Q{qid})" for qid, label, _ in rows[:3])
            raise ValueError(
                "Several source classes match this wording. Specify one of these IDs: " + options
            )
        return None
    cls, label, _ = rows[0]
    key = ("kind:" if kinds else "type:") + "Q" + str(cls)
    edition, fmt = db.execute("SELECT edition,format FROM publication").fetchone()
    live = db.execute("SELECT n FROM current_class WHERE rule=?", (key,)).fetchone() if fmt >= 15 else None
    import work_forms
    forms = fmt >= 30 and not kinds and work_forms.available(db,key)
    if live and not forms:
        count = live[0]
    else:
        query, args = roster_ids(db, key, fmt, live)
        count = db.execute(f"SELECT count(*) FROM ({query})",args).fetchone()[0]
    if not count:
        if strict and not kinds:
            from topic_sources import classified_choices
            classified_choices(db, cls, edition)
        if strict and live:
            from catalogue import RuleWithdrawn, UnverifiedRule
            if fmt >= 31 and work_forms.pending(db,key):
                raise UnverifiedRule(key,"The builder can check works recorded with this form.")
            if (not kinds and db.execute("SELECT 1 FROM sqlite_master WHERE name='discovered_subclass_subject'").fetchone()
                    and db.execute("SELECT 1 FROM discovered_subclass_subject WHERE cls=?", (cls,)).fetchone()):
                from date_rules import DateChoices
                alternate = db.execute("SELECT n FROM current_class WHERE rule=?", (f"kind:Q{cls}",)).fetchone()
                if alternate is None:
                    raise UnverifiedRule(f"kind:Q{cls}", "The source records types of this subject. The builder can check them.")
                if alternate[0]:
                    option = card(db, f"type of q{cls}", strict=True)
                    raise DateChoices([option], "The source lists types of this subject. Select the revised question to play.")
            raise RuleWithdrawn("The current source has no named members for this class question.")
        if strict and not live and (fmt >= 31 or fmt >= 27 and db.execute(
            "SELECT 1 FROM source_vocabulary WHERE token=? AND name IS NOT NULL", (f"Q{cls}",)
        ).fetchone()):
            from catalogue import UnverifiedRule
            raise UnverifiedRule(key, "The builder has learned this class and can verify its full answer list.")
        return None
    if fmt >= 25:
        from world_links import display_subject
        label = display_subject(db, cls, label)
    name = question_noun(label)
    if tagged or term == f"q{cls}":
        from world_links import identifiers
        if not name.endswith(f" (Q{cls})") and identifiers(db, folded_spelling(label), "class") != [cls]:
            name += f" (Q{cls})"
    prefix = "a type of " if kinds else class_determiner(name) + " "
    return {
        "key": key,
        "prompt": "Name " + prefix + name,
        "edition": edition,
        "records": count,
        "complete": False,
    }


NAMED_PREFIX = "individually named "
BOTH_SUFFIX = " or a specific named example"


def scope_phrase(phrase):
    if phrase.startswith(NAMED_PREFIX):
        return "named", phrase[len(NAMED_PREFIX):]
    if phrase.startswith(("type of ", "kind of ")):
        if phrase.endswith(BOTH_SUFFIX):
            return "both", phrase[8:-len(BOTH_SUFFIX)]
        return "kind", phrase[8:]
    return "", phrase


def named_card(member):
    prompt = member["prompt"]
    if prompt.startswith("Name one of the "):
        prompt = prompt.replace("Name one of the ", "Name one of the individually named ", 1)
    else:
        prompt = re.sub(r"^Name (?:a|an) ", "Name an individually named ", prompt)
    return {**member, "prompt": prompt}


def scope_choices(db, member, *, explicit=False):
    """Offer distinct playable interpretations when the source has kinds and individual examples."""
    if not member or not member["key"].startswith("type:"):
        return []
    token = member["key"].split(":")[1]
    kind = card(db, "type of " + token.lower())
    if not kind or (not explicit and kind["records"] < 2):
        return []
    fmt = db.execute("SELECT format FROM publication").fetchone()[0]
    rosters = []
    for key in (member["key"], kind["key"]):
        live = fmt >= 15 and db.execute("SELECT 1 FROM current_class WHERE rule=?", (key,)).fetchone()
        rosters.append(roster_ids(db, key, fmt, live))
    (members, ma), (kinds, ka) = rosters
    overlap = db.execute(f"SELECT count(*) FROM (SELECT id FROM ({kinds}) INTERSECT SELECT id FROM ({members}))",
                         (*ka, *ma)).fetchone()[0]
    if not explicit and overlap == kind["records"]:
        return []
    combined = {**kind, "key": "examples:" + token, "prompt": kind["prompt"] + BOTH_SUFFIX,
                "records": kind["records"] + member["records"] - overlap}
    options = [combined, kind]
    if explicit or member["records"] >= 2:
        options.append(named_card(member))
    return options


def question(db, phrase, literal):
    """Resolve explicit scopes directly and let the host choose before an unqualified round."""
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    mode, phrase = scope_phrase(phrase)
    _, literal = scope_phrase(literal)
    if mode == "kind":
        return card(db, "type of " + phrase, strict=True, literal="type of " + literal)
    if mode == "named":
        member = card(db, phrase, strict=True, literal=literal)
        return named_card(member) if member else None
    deferred = None
    try:
        member = card(db, phrase, strict=True, literal=literal)
    except (RuleWithdrawn, UnverifiedRule) as error:
        if mode or isinstance(error, UnverifiedRule) and not error.key.startswith("type:"):
            raise
        deferred, member = error, None
    if not member and not mode:
        alternate = card(db, "type of " + phrase, literal="type of " + literal)
        if alternate:
            raise DateChoices([alternate], "This subject has a published list of types. Select that question to play.",
                              refresh=getattr(deferred, "requests", ()))
        if deferred:
            raise deferred
    options = scope_choices(db, member, explicit=mode == "both")
    if mode == "both" and member:
        if not options:
            key = "kind:" + member["key"].split(":")[1]
            if db.execute("SELECT format FROM publication").fetchone()[0] >= 15 and not db.execute(
                    "SELECT 1 FROM current_class WHERE rule=?", (key,)).fetchone():
                raise UnverifiedRule(key, "The builder can check which types of this subject have source records.")
            raise RuleWithdrawn("This edition has no separate lists of types and individually named examples for this subject.")
        return options[0]
    if options:
        raise DateChoices(options, "This subject has both types and individually named examples. Choose what players can submit.")
    return member


def play(db, game, key, prompt):
    from catalogue import RuleWithdrawn
    from compiler import evaluate

    match = re.fullmatch(r"(kind|type|examples):Q([1-9]\d{0,11})", key)
    if not match:
        raise RuleWithdrawn("This source class is unavailable.")
    cls = int(match[2])
    label = db.execute("SELECT name FROM item WHERE id=?", (cls,)).fetchone()
    if not label:
        raise RuleWithdrawn("This source class is no longer in the publication.")
    plan = {"class": "wd:Q" + str(cls), "classLabel": label[0], "catalogueKey": key}
    combined = match[1] == "examples"
    scopes = ("type", "kind") if combined else (match[1],)
    with tempfile.TemporaryDirectory() as folder, closing(open_graph(Path(folder) / "class.sqlite")) as graph:
        unnamed = {}
        for scope in scopes:
            unnamed[scope] = add_scope(db, graph, cls, scope, plan, combined)
        if combined and any(unnamed.values()):
            plan["unnamedByScope"] = unnamed
        return evaluate(graph, game, prompt, plan, unnamed_members=0 if combined else unnamed[match[1]], name_catalogue=db)


def add_scope(db, graph, cls, scope, plan, combined):
    from catalogue import RuleWithdrawn
    key, kinds = f"{scope}:Q{cls}", scope == "kind"
    file_format = db.execute("SELECT format FROM publication").fetchone()[0]
    import work_forms
    forms = file_format >= 30 and not kinds and work_forms.available(db,key)
    if file_format >= 31 and not kinds:
        roster = db.execute("SELECT scanned-n FROM current_class WHERE rule=?", (f"form:Q{cls}",)).fetchone()
        if roster and roster[0] and cls in work_forms.work_classes(db, [cls]):
            plan["unnamedFormRecords"] = roster[0]
    if file_format >= 15:
        live = db.execute("SELECT * FROM current_class WHERE rule=?", (key,)).fetchone()
        if live:
            if not live["n"] and not forms:
                raise RuleWithdrawn("The current source has no named members for this class question.")
            from class_updates import evidence

            def current_records():
                for item, name, aliases, incoming in db.execute(
                    """SELECT m.id,m.name,m.aliases,coalesce(i.incoming,0)
                        FROM current_member m LEFT JOIN item i ON i.id=m.id WHERE m.rule=?""",
                    (key,),
                ):
                    qid = "Q" + str(item)
                    yield {
                        "id": "wd:" + qid,
                        "name": name,
                        "aliases": json.loads(aliases),
                        "types": [plan["class"]],
                        "url": evidence(key, qid),
                        "graph_recognition": [0, incoming],
                        "depth": 1 if incoming >= 1000 else 2,
                    }

            history = db.execute(
                "SELECT revision FROM source WHERE id='wikidata5m-catalogue-input'"
            ).fetchone()[0]
            ingest(
                graph,
                {
                    "source": ("wikidata-query-kinds-" if combined and kinds else "wikidata-query-class-") + str(cls),
                    "revision": digest([live["revision"], history]),
                    "captured": live["captured"],
                    "url": "https://www.wikidata.org/wiki/Q" + str(cls),
                },
                current_records(), index_names=False,
            )
            if forms:
                work_forms.add_sources(db,graph,key,plan["class"])
            unnamed = live["scanned"]-live["n"] if file_format>=16 else 0
            return unnamed
    if combined:
        sql, params = roster_ids(db, key, file_format, False)
        if not db.execute(f"SELECT 1 FROM ({sql}) LIMIT 1", params).fetchone():
            raise RuleWithdrawn("This edition has no named records for one of the two question scopes.")
    db.executescript("DROP TABLE IF EXISTS temp.class_match;")
    args = (cls, cls) if kinds else (cls,)
    db.execute(
        "CREATE TEMP TABLE class_match AS " + DESCENDANTS + " " + (KINDS if kinds else MEMBERS), args
    )
    db.execute("CREATE INDEX temp.class_match_by_property ON class_match(property,id,cls)")
    revision, url = db.execute(
        "SELECT revision,url FROM source WHERE id='wikidata5m-catalogue-input'"
    ).fetchone()

    def records(prop):
        names = "answer_item" if file_format >= 33 else "item"
        results = db.execute(
            f"""SELECT m.id,i.name,i.aliases,i.incoming,group_concat(m.cls) FROM class_match m
            CROSS JOIN {names} i ON i.id=m.id WHERE m.property=? AND length(i.name) BETWEEN 1 AND 140 GROUP BY m.id""",
            (prop,),
        )
        for item, name, aliases, incoming, types in results:
            yield {
                "id": "wd:Q" + str(item),
                "name": name,
                "aliases": [a for a in aliases.split("\t") if 0 < len(a) <= 140],
                "url": f"https://www.wikidata.org/wiki/Q{item}#P{prop}",
                "types": ["wd:Q" + t for t in types.split(",")],
                "graph_recognition": [0, incoming],
                "depth": 1 if incoming >= 1000 else 2,
            }

    for (prop,) in db.execute("SELECT DISTINCT property FROM class_match"):
        ingest(
            graph,
            {"source": "world5m-membership-" + str(prop), "revision": revision, "url": url},
            records(prop), index_names=False,
        )
    if file_format >= 29 and not kinds:
        from scope_evidence import add_sources
        add_sources(db,graph,cls)
    if forms:
        work_forms.add_sources(db,graph,key,plan["class"])
    if not graph.execute("SELECT 1 FROM entity LIMIT 1").fetchone():
        raise RuleWithdrawn("This edition has no named members of the source class.")
    # Preserve the actual path connecting each source-reported type to the requested class.
    taxonomy = "world5m-class-taxonomy"
    hierarchy = db.execute("SELECT revision,url,checked FROM source WHERE id='wikidata-current-taxonomy'").fetchone()
    if hierarchy:
        taxonomy = "wikidata-current-taxonomy"
    if graph.execute("SELECT 1 FROM source WHERE id=?", (taxonomy,)).fetchone():
        return 0
    checked = db.execute("SELECT checked FROM source WHERE id='wikidata5m-catalogue-input'").fetchone()[0]
    graph.execute("INSERT INTO source VALUES(?,?,?,?,'{}')", (taxonomy, *(hierarchy or (revision,url,checked))))
    edges = db.execute(
        DESCENDANTS + """ SELECT p.child,p.parent FROM scope s JOIN world_parent p ON p.parent=s.id""",
        (cls,),
    )
    graph.executemany(
        "INSERT OR IGNORE INTO parent VALUES(?,?,?)",
        ((taxonomy, "wd:Q" + str(c), "wd:Q" + str(p)) for c, p in edges),
    )
    graph.commit()
    return 0
