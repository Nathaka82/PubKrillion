import json
import math
import os
import re
import time
import urllib.request

from graph import digest, folded_spelling, norm, spelling

BANK_FORMAT = 36


def publication_batches(game, cursor, size):
    # SQLite's data_version changes only for commits made by other connections.
    # Give a burst of game requests time to drain before competing for the writer again.
    version = game.execute("PRAGMA data_version").fetchone()[0]
    next_check = time.monotonic()
    while rows := cursor.fetchmany(size):
        yield rows
        if time.monotonic() >= next_check:
            latest = game.execute("PRAGMA data_version").fetchone()[0]
            if latest != version:
                time.sleep(0.050)
                version = latest
            next_check = time.monotonic() + 0.100


def noun(text):
    return re.sub(
        r"^(?:one of the |a |an |the |type of |kind of )+",
        "",
        text.strip().rstrip(" .?"),
        flags=re.IGNORECASE,
    )


def class_term(db, value):
    name = noun(value)
    precise, plain = folded_spelling(name), norm(name)
    rows = db.execute(
        """SELECT DISTINCT t.key FROM term t WHERE t.alias=? AND (
            EXISTS(SELECT 1 FROM term_display d WHERE (d.source,d.key)=(t.source,t.key) AND spelling_key(d.alias)=?)
            OR (?=? AND NOT EXISTS(SELECT 1 FROM term_display d WHERE (d.source,d.key)=(t.source,t.key))))""",
        (plain, precise, precise, plain)
    ).fetchall()
    if len(rows) != 1:
        raise ValueError(
            "The index needs an unambiguous subject for “"
            + value
            + "”. Search sources or import a source for this subject."
        )
    return rows[0][0]


def interpret(db, prompt):
    body = re.sub(
        r"^(?:(?:please )?(?:name|give me|identify|suggest) |can you name )",
        "",
        prompt.strip(),
        flags=re.IGNORECASE,
    )
    body = noun(body)
    from scoped_topics import require_explicit_subject
    require_explicit_subject(db, body)
    try:
        return {"class": class_term(db, body)}
    except ValueError:
        pass
    comp = re.fullmatch(
        r"(.+?) (?:that (?:is |grows )?|which (?:is )?|whose length is |is )?(bigger|larger|longer|smaller|shorter|heavier|lighter) than (.+)",
        body,
        re.IGNORECASE,
    )
    if comp:
        subject, op, ref = comp.groups()
        op = op.lower()
        try:
            head = {"class": class_term(db, subject)}
        except ValueError:
            head = interpret(db, subject)
        return {
            **head,
            "property": "mass" if op in ("heavier", "lighter") else "length",
            "op": "lt" if op in ("smaller", "shorter", "lighter") else "gt",
            "reference": class_term(db, ref),
        }
    years = re.search(
        r" (?:between|from) (\d{4}) (?:and|to) (\d{4})$", body, re.IGNORECASE
    )
    period = {}
    if years:
        if int(years[1]) > int(years[2]):
            raise ValueError("The start year must come before the end year.")
        period = {"years": [int(years[1]), int(years[2])]}
        body = body[: years.start()]
    rel = re.fullmatch(r"(.+?) (?:that |which |who )?(.+?) (.+)", body)
    if rel:
        for row in db.execute("SELECT DISTINCT relation FROM edge"):
            pattern = re.fullmatch(
                r"(.+?) (?:that |which |who )?(?:has |have )?"
                + re.escape(row[0])
                + r" (.+)",
                body,
                re.IGNORECASE,
            )
            if pattern:
                return {
                    "class": class_term(db, pattern[1]),
                    "relation": row[0],
                    "target": class_term(db, pattern[2]),
                    **period,
                }
    if os.environ.get("KNOWLEDGE_MODEL_URL"):
        words = set(norm(prompt).split()) - {
            "a",
            "an",
            "the",
            "of",
            "which",
            "what",
            "has",
            "have",
            "would",
            "is",
            "can",
            "name",
            "give",
            "that",
            "who",
            "with",
            "me",
            "to",
            "in",
            "does",
            "and",
            "or",
        }
        vocabulary = {}
        for key, alias in db.execute(
            "SELECT DISTINCT key,alias FROM term ORDER BY key"
        ):
            vocabulary.setdefault(key, []).append(alias)
        best = sorted(
            vocabulary,
            key=lambda k: -max(len(words & set(a.split())) for a in vocabulary[k]),
        )
        terms = best[:18]
        descriptions = {
            k: sorted(vocabulary[k], key=lambda a: -len(words & set(a.split())))[:2]
            for k in terms
        }
        edges = [
            x[0]
            for x in db.execute(
                "SELECT DISTINCT relation FROM edge ORDER BY relation LIMIT 100"
            )
        ]
        system = (
            "Translate every constraint into one JSON object. Allowed forms are {class}, "
            "{class,property,op,reference} or {class,relation,target}. property=length|mass|year, op=lt|gt. "
            "Keys class, reference and target must use known terms. If any constraint cannot be represented return {}. "
            "Terms and example wordings="
            + json.dumps(descriptions)
            + " Relations="
            + json.dumps(edges)
        )
        system += (
            " Translate physical size or growth into length unless a different dimension is explicit. "
            'For "Which bridge stretches farther than an avenue?" the plan is '
            '{"class":"bridge","property":"length","op":"gt","reference":"avenue"}. Use only available terms.'
        )
        vocab = {"type": "string", "enum": terms}
        props = {
            "class": vocab,
            "reference": vocab,
            "target": vocab,
            "op": {"enum": ["lt", "gt"]},
            "property": {"enum": ["length", "mass", "year"]},
            "relation": {"enum": edges or ["unknown"]},
        }
        schema = {
            "anyOf": [
                {
                    "type": "object",
                    "properties": {k: props[k] for k in keys},
                    "required": keys,
                    "additionalProperties": False,
                }
                for keys in (
                    ["class", "property", "op", "reference"],
                    ["class", "relation", "target"],
                    ["class"],
                    [],
                )
            ]
        }
        req = urllib.request.Request(
            os.environ["KNOWLEDGE_MODEL_URL"].rstrip("/") + "/api/chat",
            data=json.dumps(
                {
                    "model": os.environ.get("KNOWLEDGE_MODEL", "qwen2.5:1.5b"),
                    "stream": False,
                    "format": schema,
                    "options": {
                        "temperature": 0,
                        "num_ctx": 4096,
                        "num_predict": 140,
                        "num_thread": 2,
                    },
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                }
            ).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            plan = json.loads(json.loads(r.read(100000))["message"]["content"])
        if not isinstance(plan, dict) or any(
            not isinstance(v, str) for v in plan.values()
        ):
            raise ValueError("The model could not ground this prompt in the index.")
        keys = set(plan)
        if keys not in (
            {"class"},
            {"class", "property", "op", "reference"},
            {"class", "relation", "target"},
            {"class", "property", "op", "reference", "relation", "target"},
        ):
            raise ValueError(
                "The model could not express every condition in this prompt."
            )
        if (
            all(plan[k] in terms for k in keys & {"class", "reference", "target"})
            and (
                "property" not in keys
                or plan["property"] in ("length", "mass", "year")
                and plan["op"] in ("lt", "gt")
            )
            and ("relation" not in keys or plan["relation"] in edges)
        ):
            scope = " " + norm(prompt) + " "
            for k in ("class", "reference", "target"):
                if k in plan:
                    for alias in sorted(vocabulary[plan[k]], key=len, reverse=True):
                        scope = scope.replace(" " + alias + " ", " ")
            if "relation" in plan:
                scope = scope.replace(" " + plan["relation"] + " ", " ")
            if re.search(
                r"\b(?:except|excluding|without|between|since|before|after|from|in|with|and|or|most|least|best|worst|famous|interesting)\b|\d",
                scope,
            ):
                raise ValueError(
                    "The index cannot verify every condition in that wording."
                )
            if keys == {"class"}:
                filler = {
                    "which",
                    "what",
                    "are",
                    "is",
                    "could",
                    "you",
                    "would",
                    "can",
                    "please",
                    "name",
                    "give",
                    "me",
                    "identify",
                    "suggest",
                    "some",
                    "one",
                    "any",
                    "an",
                    "a",
                    "the",
                    "kind",
                    "type",
                    "example",
                    "examples",
                    "of",
                    "variety",
                    "varieties",
                    "belongs",
                    "to",
                    "this",
                    "group",
                }
                if set(scope.split()) - filler:
                    raise ValueError(
                        "The index cannot verify every condition in that wording."
                    )
            return plan
    raise ValueError(
        "This wording has no verified interpretation in the index yet. Try a named subject or a supported comparison."
    )


def upper_recall(count, population):
    fraction = count / population
    z = 1.96
    return (fraction + z*z/(2*population) + z*math.sqrt(
        fraction*(1-fraction)/population + z*z/(4*population*population)
    )) / (1 + z*z/population)


def recall_depth(upper):
    if upper >= .35:
        return 1
    if upper >= .15:
        return 2
    if upper >= .05:
        return 3
    return 4


def class_paths(db, cls, sources, include_measured):
    """Find a shortest recorded subclass path from each contributing type."""
    from collections import defaultdict, deque
    wanted = {r[0] for r in db.execute("SELECT DISTINCT class FROM membership WHERE class<>?", (cls,))}
    if not wanted:
        return {}
    children = defaultdict(list)
    for parent, child, source in db.execute("""WITH RECURSIVE needed(k) AS (
            SELECT DISTINCT class FROM membership UNION SELECT p.parent FROM needed n JOIN parent p ON p.child=n.k
            WHERE ? OR p.source<>'world5m-measure-members')
            SELECT p.parent,p.child,p.source FROM needed n CROSS JOIN parent p ON p.child=n.k
            WHERE ? OR p.source<>'world5m-measure-members' ORDER BY p.parent,p.child,p.source""",
            (include_measured,include_measured)):
        children[parent].append((child,source))
    paths, queue = {cls:[]}, deque([cls])
    while queue and wanted:
        parent = queue.popleft()
        for child, source in children[parent]:
            if child in paths:
                continue
            url = sources[source]["url"]
            if source in ("world5m-measure-members","world5m-class-taxonomy","wikidata5m-catalogue-input","wikidata-current-taxonomy"):
                url = "https://www.wikidata.org/wiki/"+child[3:]+"#P279"
            edge = {"source":source,"subclass":child,"parent":parent,"url":url}
            paths[child] = [edge,*paths[parent]]
            wanted.discard(child)
            queue.append(child)
    return paths


def observed_choices(game, cohort):
    cutoff = game.execute("SELECT population FROM kb_calibration WHERE cohort=?", (cohort,)).fetchone()
    if not cutoff:
        return {}
    # Only closed rounds count. Each team contributes once per rule and edition.
    observed, rooms = {}, set()
    for name, room, count in game.execute("""SELECT choice,room,count(*) FROM (
        SELECT p.choice,p.room FROM kb_pick p JOIN rounds r ON r.id=p.round
        WHERE p.cohort=? AND p.choice IS NOT NULL AND r.phase<>'open' ORDER BY p.id LIMIT ?
    ) GROUP BY choice,room""", (cohort, cutoff[0])):
        observed[name] = observed.get(name, 0) + count
        rooms.add(room)
    return observed if len(rooms) >= 3 and sum(observed.values()) >= 200 else {}


def evaluate(db, game, prompt, plan=None, *, outside_names=(), name_edition=None, missing_members=0, measured_decisions=False, unnamed_members=0,relationship_proof=None,name_catalogue=None):
    import attested_names
    plan = plan or interpret(db, prompt)
    # Keep a consistent read snapshot across membership, source revisions and measurements.
    sources = {r["id"]: dict(r) for r in db.execute("SELECT * FROM source ORDER BY id")}
    cls = plan["class"]
    comparing = "reference" in plan or "threshold" in plan
    db.executescript("DROP TABLE IF EXISTS temp.candidate; DROP TABLE IF EXISTS temp.membership;")
    db.execute(
        """CREATE TEMP TABLE membership AS WITH RECURSIVE classes(c) AS
        (VALUES(?) UNION SELECT p.child FROM parent p JOIN classes ON p.parent=classes.c
         WHERE ? OR p.source<>'world5m-measure-members')
        SELECT source,class,id FROM member JOIN classes ON member.class=c
        WHERE (? IS NULL OR source NOT LIKE 'world5m-%' OR source=?)
        AND (? IS NULL OR (source=? AND member.class=?)) AND (? OR source<>'world5m-measure-members')""",
        (cls, comparing, plan.get("worldSource"), plan.get("worldSource"), plan.get("register"), plan.get("register"), cls, comparing),
    )
    db.executescript("""CREATE INDEX temp.membership_id ON membership(id);
        CREATE TEMP TABLE candidate AS SELECT DISTINCT id FROM membership;
        CREATE UNIQUE INDEX temp.candidate_id ON candidate(id);""")
    total = db.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] + missing_members + unnamed_members
    mode = plan.get("measureMode","typical")
    if comparing and "measureMode" not in plan and not re.search(r"\b(usually|typically|common length)\b",prompt,re.I):
        if db.execute("""SELECT 1 FROM fact f JOIN candidate c USING(id) WHERE f.property=? AND
                f.basis='maturity-lower-bound' LIMIT 1""",(plan["property"],)).fetchone():
            mode = "common-or-maturity"
            plan = {**plan,"measureMode":mode}
    selected = set()
    reference = []
    if "reference" in plan:
        reference = [
            dict(r)
            for r in db.execute(
                "SELECT * FROM fact WHERE id=? AND property=? AND basis IN ('typical','typical-lower-bound')",
                (plan["reference"], plan["property"]),
            )
        ]
        if not reference:
            raise ValueError(
                "The reference lacks a sourced typical " + plan["property"] + " range."
            )
        selected.update(r["source"] for r in reference)
        ref_low = min(r["low"] for r in reference)
        ref_high = (math.inf if any(r["basis"]=="typical-lower-bound" for r in reference) else max(r["high"] for r in reference))
    elif comparing:
        ref_low = ref_high = float(plan["threshold"])
    dependencies = selected | {r[0] for r in db.execute("SELECT DISTINCT source FROM membership")}
    if comparing:
        dependencies.update(r[0] for r in db.execute("""SELECT DISTINCT f.source FROM candidate c
            CROSS JOIN fact f ON f.id=c.id AND f.property=?""", (plan["property"],)))
    if plan.get("edgeSource"):
        dependencies.add(plan["edgeSource"])
    for condition in plan.get("conditions",()):
        dependencies.update(row[0] for row in db.execute("""SELECT DISTINCT e.source FROM edge e
            JOIN candidate c ON c.id=e.id WHERE e.relation=? AND e.target=? AND (? IS NULL OR e.source=?)""",
            (condition["relation"],condition["target"],condition.get("source"),condition.get("source"))))
    if not plan.get("register"):
        dependencies.update(
            r[0]
            for r in db.execute(
                "SELECT DISTINCT e.source FROM candidate c CROSS JOIN entity e ON e.id=c.id"
            )
        )
        dependencies.update(
            r[0]
            for r in db.execute(
                """WITH RECURSIVE cl(k) AS (VALUES(?) UNION
            SELECT child FROM parent JOIN cl ON parent=cl.k) SELECT DISTINCT source FROM parent JOIN cl ON parent=cl.k""",
                (cls,),
            )
        )
    else:
        for table in ("recall", "familiarity", "graph_familiarity"):
            dependencies.update(r[0] for r in db.execute(
                f"SELECT DISTINCT s.source FROM candidate c CROSS JOIN {table} s ON s.id=c.id"))
    dependencies.update(
        r[0]
        for r in db.execute(
            "SELECT DISTINCT source FROM calendar WHERE event=?",
            (plan.get("target", ""),),
        )
    )
    cohort = digest(["source-name-decisions-v2", prompt, plan, [(s, sources[s]["revision"]) for s in sorted(dependencies)]])
    observed = observed_choices(game, cohort)
    population = sum(observed.values())
    name_picks = {}
    for name, count in observed.items():
        term = spelling(name)
        name_picks[term] = name_picks.get(term, 0) + count
    # Another group in the publication may introduce a conflicting alias while
    # this question's own qualifying records and recall cohort stay the same.
    if name_catalogue is None and plan.get("catalogueKey"):
        name_catalogue = db
    if name_edition is None and name_catalogue is not None and name_catalogue.execute(
        "SELECT 1 FROM sqlite_master WHERE name='attested_name'").fetchone():
        name_edition = name_catalogue.execute("SELECT edition FROM publication").fetchone()[0]
    version = "kb-" + digest([BANK_FORMAT, cohort, observed, name_edition,missing_members])[:36]
    if unnamed_members:
        version = "kb-" + digest([version,unnamed_members])[:36]
    cached = game.execute(
        "SELECT metadata FROM kb_bank WHERE id=?", (version,)
    ).fetchone()
    if cached and cached[0] != "null":
        meta = json.loads(cached[0])
        if observed and "choiceSignature" not in meta:
            # The immutable bank ID already includes these exact observations.
            meta["choiceSignature"] = digest(observed)
            with game:
                game.execute("UPDATE kb_bank SET metadata=? WHERE id=?", (json.dumps(meta), version))
        return version, meta
    paths = class_paths(db,cls,sources,comparing)
    from itertools import groupby

    counts = [0] * 6
    gaps = {"missing": missing_members, "borderline": 0, "outside": 0}
    if measured_decisions:
        for state, n in db.execute("SELECT outcome,count(*) FROM measure_decision WHERE outcome<>'accepted' GROUP BY outcome"):
            gaps[state] += n
    db.executescript("""DROP TABLE IF EXISTS temp.result; CREATE TEMP TABLE result(
        id TEXT PRIMARY KEY, name TEXT, depth INTEGER, aliases TEXT, evidence TEXT, upper REAL, proxy REAL,
        term TEXT UNIQUE, links INTEGER, survey_total REAL NOT NULL, rare_views INTEGER, view_rank_bound INTEGER);""")
    db.executescript("""DROP TABLE IF EXISTS temp.qualified;
        CREATE TEMP TABLE qualified(id TEXT PRIMARY KEY, owner TEXT NOT NULL) WITHOUT ROWID;
        DROP TABLE IF EXISTS temp.spellings;
        CREATE TEMP TABLE spellings(id TEXT,exact TEXT,folded TEXT,display TEXT,preferred INTEGER NOT NULL,legacy TEXT,
            PRIMARY KEY(id,exact)) WITHOUT ROWID;""")
    candidates = "(SELECT id FROM measure_decision WHERE outcome='accepted')" if measured_decisions else "candidate"
    has_name_claims = attested_names.stage(db,name_catalogue)
    import entity_labels
    from world_index import SOURCE as world_archive
    checked_labels = entity_labels.stage(db,name_catalogue)
    label_fields = ("l.forms,l.checked AS names_checked,l.variants,l.origin" if checked_labels
                    else "NULL AS forms,NULL AS names_checked,NULL AS variants,NULL AS origin")
    label_join = "LEFT JOIN candidate_labels l ON l.id=e.id" if checked_labels else ""
    surveyed = db.execute("SELECT 1 FROM recall WHERE prompt=? LIMIT 1", (prompt,)).fetchone()
    has_familiarity = db.execute("SELECT 1 FROM familiarity LIMIT 1").fetchone()
    joined = db.execute(f"""SELECT e.*, {label_fields}, (SELECT json_group_array(m.class) FROM membership m
            WHERE m.id=e.id AND m.source=e.source) AS classes FROM {candidates} c CROSS JOIN entity e ON e.id=c.id
        {label_join}
        WHERE ? IS NULL OR e.source=? ORDER BY c.id,e.source""", (plan.get("register"), plan.get("register")))
    for eid, records in groupby(joined, key=lambda r: r["id"]):
        rows = list(records)
        names = {r["name"] for r in rows}
        names_checked = None
        name_origin = ""
        variants = None
        for row in rows:
            source = row["source"]
            if row["forms"] is not None and (source.startswith("world5m-") or
                    source.startswith(("wikidata-query-", "wikidata-joined-"))
                    and row["names_checked"] >= sources[source]["checked"]):
                names.update(filter(None,row["forms"].split("\t")))
                names_checked = row["names_checked"]
                name_origin = row["origin"]
                variants = row["variants"]
            else:
                names.update(json.loads(row["aliases"]))
        names = sorted(names)
        preferred = {spelling(r["name"]) for r in rows}
        db.executemany("INSERT OR IGNORE INTO spellings VALUES(?,?,?,?,?,?)",
                       ((eid, key, folded_spelling(name), name, key in preferred, norm(name))
                        for name in names for key in (spelling(name),)))
        evidence = [{"source": r["source"], "entity": eid, "url": r["url"], "classes": json.loads(r["classes"])} for r in rows]
        derived = {c for fact in evidence for c in fact["classes"] if c != cls}
        seen_edges = set()
        for child in sorted(derived):
            for edge in paths.get(child, ()):
                token = edge["source"],edge["subclass"],edge["parent"]
                if token not in seen_edges:
                    evidence.append(edge)
                    seen_edges.add(token)
        if comparing:
            facts = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM fact WHERE id=? AND property=?",
                    (eid, plan["property"]),
                )
            ]
            if mode=="record":
                facts = [f for f in facts if f["basis"] in ("record","record-lower-bound")]
            else:
                common = [f for f in facts if f["basis"] in ("typical","typical-lower-bound")]
                facts = common or ([f for f in facts if f["basis"]=="maturity-lower-bound"] if mode=="common-or-maturity" else [])
            if not facts:
                gaps["missing"] += 1
                continue
            lo = (max if mode=="record" else min)(r["low"] for r in facts)
            hi = max(r["high"] for r in facts)
            if any(r["basis"].endswith("-lower-bound") for r in facts):
                hi = math.inf
            op = plan["op"]
            if eid == plan.get("reference") and mode!="record":
                yes = op in ("ge","le")
                outside = not yes
            elif op == "gt":
                yes, outside = lo > ref_high, hi <= ref_low
            elif op == "lt":
                yes, outside = hi < ref_low, lo >= ref_high
            elif op == "ge":
                yes, outside = lo >= ref_high, hi < ref_low
            else:
                yes, outside = hi <= ref_low, lo > ref_high
            if not yes:
                gaps["outside" if outside else "borderline"] += 1
                continue
            evidence.extend(facts)
        if "conditions" in plan:
            for condition in plan["conditions"]:
                edges = [dict(r) for r in db.execute("""SELECT * FROM edge WHERE id=? AND relation=? AND target=?
                    AND (? IS NULL OR source=?)""", (eid, condition["relation"], condition["target"],
                                                      condition.get("source"), condition.get("source")))]
                if not edges:
                    break
                if condition.get("geo") or condition.get("dated") or condition.get("statementProof"):
                    detail = relationship_proof(eid,condition)
                    for edge in edges:
                        edge.update(detail)
                elif "prop" in condition and eid.startswith("wd:Q"):
                    for edge in edges:
                        edge["url"] = "https://www.wikidata.org/wiki/" + eid[3:] + "#P" + str(condition["prop"])
                else:
                    for edge in edges:
                        edge["url"] = next((r["url"] for r in rows if r["source"]==edge["source"]),
                                           sources[edge["source"]]["url"])
                evidence.extend(edges)
            if not edges:
                gaps["outside" if "expectedMatches" in plan else "missing"] += 1
                continue
        if "relation" in plan:
            edges = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM edge WHERE id=? AND relation=? AND target=? AND (? IS NULL OR source=?)",
                    (
                        eid,
                        plan["relation"],
                        plan["target"],
                        plan.get("edgeSource"),
                        plan.get("edgeSource"),
                    ),
                )
            ]
            if edges and "years" in plan:
                low, high = plan["years"]
                edges = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM edge WHERE id=? AND relation=? AND target BETWEEN ? AND ? AND (? IS NULL OR source=?)",
                        (
                            eid,
                            plan["relation"],
                            plan["target"] + "@" + str(low),
                            plan["target"] + "@" + str(high),
                            plan.get("edgeSource"), plan.get("edgeSource"),
                        ),
                    )
                ]
            if not edges:
                gaps["outside" if "expectedMatches" in plan else "missing"] += 1
                continue
            evidence.extend(edges)
        selected.update(r["source"] for r in evidence)
        if names_checked is not None:
            url = "https://www.wikidata.org/wiki/"+eid[3:]
            if name_origin.startswith("dump:") and (revision := name_origin.rsplit(":",1)[1]).isdecimal():
                url = "https://www.wikidata.org/w/index.php?title="+eid[3:]+"&oldid="+revision
            evidence.append({"source":"Wikidata","namesChecked":names_checked,
                             "url":url})
            if variants:
                evidence.append({"source":"Wikidata5M","qualifiedNames":variants.split("\t"),
                                 "url":world_archive})
        if has_name_claims:
            claims = list(db.execute("SELECT statement,prop,lang,display FROM answer_name_claim WHERE id=?", (eid,)))
            extra = {row[3] for row in claims}.difference(names)
            if extra:
                names = sorted(set(names) | extra)
                db.executemany("INSERT OR IGNORE INTO spellings VALUES(?,?,?,?,0,?)",
                    ((eid,spelling(name),folded_spelling(name),name,norm(name)) for name in extra))
                db.executemany("INSERT OR IGNORE INTO added_name VALUES(?,?)",((folded_spelling(name),name) for name in extra))
            evidence.extend(attested_names.evidence(row) for row in claims)
        primary = rows[0]["name"]
        aliases = [name for name in names if name != primary]
        depth = min(4, min((r["depth"] for r in rows if r["depth"] is not None), default=2))
        term = spelling(primary)
        survey_total = 0
        if surveyed:
            for count, participants, source in db.execute(
                "SELECT n,population,source FROM recall WHERE id=? AND prompt=?", (eid, prompt)
            ):
                survey_total = max(survey_total, upper_recall(count, participants))
                selected.add(source)
        # An eligible answer selected zero times in a measured cohort has the
        # same denominator as every other eligible answer in that edition.
        choice_bound = upper_recall(name_picks.get(term, 0), population) if population else 0
        upper = max(survey_total, choice_bound) or None
        if upper is not None:
            depth = recall_depth(upper)
        signal = db.execute(
            "SELECT frequency,languages FROM familiarity WHERE id=? ORDER BY frequency DESC LIMIT 1",
            (eid,),
        ).fetchone() if has_familiarity else None
        if signal and not any(signal):
            signal = None
        proxy = None
        if (
            upper is None
            and signal
            and 0 < signal[0] < 2.5
            and 2 <= signal[1] <= 6
            and depth >= 3
        ):
            proxy = round(signal[0] + math.log10(signal[1]), 3)
        links = None
        world_only = all(r["source"].startswith(("world5m-", "wikidata-query-", "wikidata-joined-")) for r in rows)
        if world_only or (upper is None and not signal and depth >= 3):
            frequency, incoming = db.execute(
                "SELECT MAX(frequency),MAX(incoming) FROM graph_familiarity WHERE id=?",
                (eid,),
            ).fetchone()
            if world_only:
                links = incoming
            if upper is None and not signal and depth >= 3 and frequency and 0 < frequency < 2.5 and 1 <= incoming <= 6:
                proxy = round(frequency + math.log10(1 + incoming), 3)
            if upper is None and not signal and not (frequency or incoming):
                depth = min(depth, 2)
        existing = db.execute("SELECT * FROM result WHERE term=?", (term,)).fetchone()
        db.execute("INSERT INTO qualified VALUES(?,?)", (eid, existing["id"] if existing else eid))
        if existing:
            # Distinct verified entities can share one playable answer. Keep both proofs and the conservative tier.
            aliases = sorted(
                (set(aliases) | set(json.loads(existing["aliases"])) | {primary})
                - {existing["name"]}
            )
            evidence += json.loads(existing["evidence"])
            # Entity surveys may describe different respondents. Bound the chance
            # of recalling any owner. In-game choices already count the shared name once.
            survey_total = min(1.0, survey_total + existing["survey_total"])
            upper = (
                max(survey_total, choice_bound)
                if upper is not None and existing["upper"] is not None
                else None
            )
            if upper is not None:
                depth = min(depth, recall_depth(upper))
            proxy = (
                max(proxy, existing["proxy"])
                if proxy is not None and existing["proxy"] is not None
                else None
            )
            db.execute(
                """UPDATE result SET depth=?,aliases=?,evidence=?,upper=?,proxy=?,
                    links=max(coalesce(links,0),coalesce(?,0)),survey_total=? WHERE term=?""",
                (
                    min(depth, existing["depth"]),
                    json.dumps(aliases),
                    json.dumps(evidence),
                    upper,
                    proxy,
                    links,
                    survey_total,
                    term,
                ),
            )
        else:
            db.execute(
                "INSERT INTO result(id,name,depth,aliases,evidence,upper,proxy,term,links,survey_total) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    eid,
                    primary,
                    depth,
                    json.dumps(aliases),
                    json.dumps(evidence),
                    upper,
                    proxy,
                    term,
                    links,
                    survey_total,
                ),
            )
    size = db.execute("SELECT COUNT(*) FROM result").fetchone()[0]
    # A single graph signal can separate the middle tiers. Ties stay together and zero links
    # convey missing evidence. A top score still requires recall or corroborating signals.
    db.execute("""WITH ranks AS MATERIALIZED (
        SELECT id, count(*) OVER () n, cume_dist() OVER (ORDER BY links) low,
            cume_dist() OVER (ORDER BY links DESC) high FROM result WHERE links>0
    ) UPDATE result SET depth=CASE WHEN ranks.high<=0.15 AND links>=10 THEN 1
        WHEN ranks.low<=0.25 THEN 3 ELSE 2 END FROM ranks
        WHERE result.id=ranks.id AND ranks.n>=12 AND result.depth=2 AND result.upper IS NULL""")
    import attention
    readership = attention.score(db,name_catalogue,size)
    # A partial survey can cap the bonus that indirect signals give a shared name.
    partial = db.execute("SELECT id,survey_total FROM result WHERE upper IS NULL AND survey_total>0")
    db.executemany("UPDATE result SET depth=min(depth,?) WHERE id=?",
                   ((recall_depth(total), item) for item,total in partial))
    budget = size // 20
    for bound, num in db.execute(
        "SELECT upper,COUNT(*) FROM result WHERE upper<.02 GROUP BY upper ORDER BY upper"
    ).fetchall():
        if num > budget:
            budget = 0
            break
        db.execute("UPDATE result SET depth=5 WHERE upper=?", (bound,))
        budget -= num
    # Spend at most one percent on proxy-only estimates, preserving ties at the boundary.
    budget = min(budget, size // 100)
    if readership:
        promoted = 0
        for views, num in db.execute("""SELECT rare_views,count(*) FROM result
                WHERE rare_views IS NOT NULL GROUP BY rare_views ORDER BY rare_views""").fetchall():
            if num > budget:
                budget = 0
                break
            db.execute("""UPDATE result SET depth=5,evidence=json_insert(evidence,'$[#]',json_object(
                'source','Wikidata QRank','readershipViews',rare_views,'rankAtMost',view_rank_bound,
                'answerNames',?,'incomingLinksPerOwnerBetween',json_array(1,6),'url',?,'revision',?)) WHERE rare_views=?""",
                (size,readership["url"],readership["revision"],views))
            promoted += num
            budget -= num
        readership["topTierEstimates"] = promoted
    for value, num in db.execute(
        """SELECT proxy,COUNT(*) FROM result WHERE proxy IS NOT NULL AND depth BETWEEN 2 AND 4
            AND upper IS NULL AND survey_total=0 GROUP BY proxy ORDER BY proxy"""
    ).fetchall():
        if num > budget:
            break
        db.execute("""UPDATE result SET depth=5 WHERE proxy=? AND depth BETWEEN 2 AND 4
            AND upper IS NULL AND survey_total=0""", (value,))
        budget -= num
    if not size:
        raise ValueError(
            "No answers met every condition. "
            + str(gaps["missing"])
            + " candidates lack evidence and "
            + str(gaps["borderline"])
            + " overlap the reference."
        )
    closed = len(plan) == 1 and any(
        json.loads(s["census"]).get(cls) == total for s in sources.values()
    )
    if "register" in plan:
        closed = json.loads(sources[plan["register"]]["census"]).get(cls) == total
        if "expectedMatches" in plan:
            closed = closed and db.execute("SELECT count(*) FROM qualified").fetchone()[0] == plan["expectedMatches"]
        if not closed:
            raise ValueError("This register failed its published membership check.")
    if "measuredCensus" in plan:
        closed = (not gaps["missing"] and not gaps["borderline"] and
                  json.loads(sources[plan["measuredCensus"]]["census"]).get(cls) == total)
        if not closed:
            raise ValueError("The comparison failed its register coverage check.")
    editions = []
    if "relation" in plan and plan["relation"] in (
        "reached",
        "won",
        "was runner-up in",
    ):
        editions = [
            r[0]
            for r in db.execute(
                "SELECT DISTINCT year FROM calendar WHERE event=? ORDER BY year",
                (plan["target"],),
            )
        ]
        if editions:
            low, high = plan.get("years", [editions[0], editions[-1]])
            unbounded = db.execute(
                "SELECT 1 FROM edge e WHERE e.target=? AND e.relation=? AND NOT EXISTS(SELECT 1 FROM calendar c WHERE c.source=e.source AND c.event=e.target) LIMIT 1",
                (plan["target"], plan["relation"]),
            ).fetchone()
            closed = not unbounded and editions[0] <= low <= high <= editions[-1]
            editions = [y for y in editions if low <= y <= high]
    for depth, num in db.execute("SELECT depth,COUNT(*) FROM result GROUP BY depth"):
        counts[depth] = num
    refs = [
        {k: sources[s][k] for k in ("id", "revision", "url", "checked")}
        for s in sorted(selected)
    ]
    for entry in refs:
        sid = entry["id"]
        if sid.startswith("world5m-"):
            entry["label"] = "Wikidata5M with English Wikipedia titles"
        elif sid == "wikidata-current-taxonomy":
            entry["label"] = "Wikidata subclass statements"
        elif sid.startswith("wikidata-joined-"):
            entry["label"] = "Published Wikidata relationship records"
        elif sid.startswith("wikidata-query-"):
            entry["label"] = "Current Wikidata class and relationship statements"
        elif sid.startswith(("quantity-class-", "quantity-ref-", "wd-quantity-", "wikidata-measure-")):
            entry["label"] = (
                "Wikidata " + plan.get("property", "measurement") + " statements"
            )
        elif sid.startswith("wd-linked-"):
            entry["label"] = "Wikidata relationship · " + plan.get(
                "relation", "linked items"
            )
    reference_label = plan.get("reference", "")
    if reference:
        label = db.execute(
            "SELECT name FROM entity WHERE id=? ORDER BY source LIMIT 1",
            (reference_label,),
        ).fetchone()
        if label:
            reference_label = label[0]
    interpretation = ("Types of " if plan.get("catalogueKey","").startswith("kind:") else "Members of ") + plan.get("classLabel",cls)
    player_comparison = ""
    if comparing:
        bound = ref_high if plan["op"] in ("gt","ge") else ref_low
        if plan["property"] == "length":
            scale, unit = (100000,"km") if bound>=100000 else (100,"m") if bound>=100 else (1,"cm")
        elif plan["property"] == "mass":
            scale, unit = (1000,"kg") if bound>=1000 else (1,"g")
        else:
            scale, unit = 1,"years"
        def formatted(n):
            return format(n/scale,",.12g")
        relation = {"gt":"over","lt":"under","ge":"at least","le":"at most"}[plan["op"]]
        quantity = plan["property"]
        if mode=="common-or-maturity":
            player_comparison = (f"The reported common {quantity} must be {relation} {formatted(bound)} {unit}. "
                                 f"If common {quantity} is unavailable, the reported size at maturity must meet the same threshold.")
        else:
            basis = "a recorded maximum" if mode=="record" else "typical"
            player_comparison = f"Use {basis} {quantity} {relation} {formatted(bound)} {unit}."
        if reference:
            span = (f"at least {formatted(ref_low)}" if ref_high==math.inf else
                    formatted(ref_low) if ref_low==ref_high else f"{formatted(ref_low)}–{formatted(ref_high)}")
            interpretation = f"{player_comparison} The source range for {reference_label} is {span} {unit}."
        else:
            interpretation = player_comparison
    if "relation" in plan:
        target = plan["target"]
        for (alias,) in db.execute(
            "SELECT DISTINCT alias FROM term WHERE key=? ORDER BY length(alias) DESC",
            (target,),
        ):
            match = re.search(r"\b" + re.escape(alias) + r"\b", prompt, re.IGNORECASE)
            if match:
                target = match[0]
                break
        interpretation = cls + " " + plan["relation"] + " " + target
        if "years" in plan:
            interpretation += (
                " from " + str(plan["years"][0]) + " to " + str(plan["years"][1])
            )
    if "qualifierLabel" in plan:
        qualifier = plan["qualifierLabel"]
        interpretation += " "+qualifier
        player_comparison += " "+qualifier
    elif "conditions" in plan:
        interpretation = plan.get("classLabel",cls) + " with " + " and ".join(
            c["relation"] + " " + c.get("label", c["target"]) for c in plan["conditions"])
    coverage = (
        "Complete for the enumerated source edition."
        if closed
        else "Open source coverage. Other valid answers may exist."
    )
    verified=db.execute("SELECT count(*) FROM qualified").fetchone()[0]
    if verified>size:
        coverage+=f" {verified:,} verified entities have {size:,} distinct answer names. Shared names receive one score."
    if unnamed_members:
        noun = "member" if unnamed_members == 1 else "members"
        coverage += f" The source lists {unnamed_members:,} additional {noun} without a usable name."
    if unnamed_forms := plan.get("unnamedFormRecords"):
        noun = "record" if unnamed_forms == 1 else "records"
        coverage += f" The creative-form check found {unnamed_forms:,} {noun} without a usable name."
    if editions:
        coverage += f" Includes {len(editions)} completed finals from {editions[0]} to {editions[-1]}."
    if plan.get("historicalOperands") or any(s.startswith("world5m-") for s in selected):
        coverage += (
            " Includes the Wikidata5M snapshot. Some published facts may have changed."
        )
    open_rule = None
    if not closed:
        identity = [prompt, plan]
        if not plan.get("catalogueKey", "").startswith("during:"):
            identity.extend([
                sorted({(r["property"], r["low"], r["high"], r["basis"]) for r in reference}),
                sorted(s for s in dependencies if s.startswith("world5m-")),
            ])
        open_rule = digest(identity)
    meta = {
        "id": version,
        "title": prompt,
        "topic": "Generated questions",
        "prompt": prompt,
        "source": refs[0]["url"],
        "matching": "full",
        "coverage": coverage,
        "knowledge": version,
        "answerCount": size,
        "cohort": cohort,
        "openRule": open_rule,
        "sourceJudged": True,
        "nameIndex": 2,
        "observedTeams": population,
        "nameAuditSet": entity_labels.eligibility_key(db),
        **({"choiceSignature": digest(observed)} if observed else {}),
        "examples": [row[0] for row in db.execute(
            """SELECT name FROM (SELECT name,depth,row_number() OVER
                (PARTITION BY depth ORDER BY links DESC,name) pos FROM result) ORDER BY pos,depth LIMIT 3"""
        )],
        "counts": counts,
        "gaps": gaps,
        "candidates": total,
        "unnamedMembers": unnamed_members,
        "interpretation": interpretation,
        "playerRule": (
            player_comparison if comparing else plan["dateScope"] if "dateScope" in plan else
            plan["geographicScope"] if "geographicScope" in plan else
            "The source must list the answer as a subclass of the named subject, directly or through another class." if plan.get("catalogueKey","").startswith("kind:") else
            "The source must list the answer’s type or occupation in this class or one of its subclasses." if plan.get("catalogueKey","").startswith("type:") else
            "Answers must be listed in the Wikidata class or its subclasses and meet every stated condition." if "liveClass" in plan else
            "Each answer must belong to the source class or a subclass and meet every stated condition." if "conditions" in plan else
            "Answers come from the published source register for this edition." if "register" in plan else
            "Answers come from source records for this class and its subclasses." if plan.get("catalogueKey", "").startswith("c-") else
            "Answers need a source record for the class and the stated relationship." if plan.get("catalogueKey", "").startswith("f-") else
            "Answers must have a directly recorded class and relationship in this Wikidata5M edition."
            if "catalogueKey" in plan else ""
        ) + (" " + plan["borderScope"] if plan.get("borderScope") else ""),
        "scoring": (f"Published Wikimedia readership over 52 weeks covers {readership['coveredAnswers']:,} of {size:,} answer names. "
                    f"{readership['topTierEstimates']:,} earned the top tier through bottom-2% readership and sparse graph links. "
                    if readership else "") + "Graph links help estimate relative familiarity. First choices from at least 200 teams across three rooms and exact-prompt surveys use an upper confidence bound. At most 5% qualify for the top tier, with at most 1% from two supporting familiarity signals. Missing data earns no rarity bonus.",
        "readership": readership,
        "plan": plan,
        "sources": refs,
        "reference": reference,
        "answers": [],
    }
    # An unfinished edition has no host/job reference. Short write batches keep live submissions moving.
    with game:
        if cached:
            for table in ("kb_text", "kb_answer_key", "kb_name", "kb_group", "kb_member"):
                game.execute(f"DELETE FROM {table} WHERE bank=?", (version,))
        game.execute(
            "INSERT OR IGNORE INTO kb_bank VALUES(?,?,'null')", (version, prompt)
        )
    cur = db.execute("SELECT * FROM result ORDER BY id")
    for chunk in publication_batches(game, cur, 1000):
        search_rows = [
            (version, row["id"], " ".join([row["name"], *json.loads(row["aliases"])]))
            for row in chunk
        ]
        with game:
            game.executemany(
                "INSERT OR REPLACE INTO kb_member VALUES(?,?,?,?,?,?)",
                (
                    (
                        version,
                        r["id"],
                        r["name"],
                        r["depth"],
                        r["aliases"],
                        r["evidence"],
                    )
                    for r in chunk
                ),
            )
            game.executemany(
                "INSERT INTO kb_text(bank,entity,terms) VALUES(?,?,?)",
                search_rows,
            )
    # Deduplicate and sort on the private graph so the shared name index receives
    # consecutive keys. The upper bound leaves the writer available between batches.
    name_rows = db.execute("""WITH names AS (
            SELECT q.owner,s.exact,s.legacy,coalesce(nullif(s.folded,''),s.exact) folded
            FROM qualified q JOIN spellings s USING(id))
        SELECT coalesce(nullif(legacy,''),exact) term,owner FROM names WHERE exact<>''
        UNION SELECT char(31)||folded,owner FROM names WHERE folded IS NOT legacy
        ORDER BY 1,2""")
    for batch in publication_batches(game, name_rows, 2000):
        with game:
            game.executemany("INSERT OR IGNORE INTO kb_name VALUES(?,?,?)",
                ((version, term, owner) for term, owner in batch))
    # Keep conflicting names from excluded candidates when they could otherwise
    # identify a qualifying item. Losing those owners would turn uncertainty into points.
    if has_name_claims:
        from itertools import chain
        outside_names = chain(outside_names,attested_names.conflict_names(db,name_catalogue))
    db.executemany("INSERT OR IGNORE INTO spellings VALUES(?,?,?,?,?,?)",
                   ((eid, spelling(name), folded_spelling(name), name, preferred, None)
                    for eid, name, preferred in outside_names if name))
    db.executescript("""DROP TABLE IF EXISTS temp.playable_terms;
        CREATE TEMP TABLE playable_terms(term TEXT PRIMARY KEY) WITHOUT ROWID;
        INSERT OR IGNORE INTO playable_terms SELECT s.folded FROM spellings s JOIN qualified q USING(id);""")
    for form, column in enumerate(("exact", "folded")):
        decisions = db.execute(f"""WITH decisions AS (SELECT s.{column} term,
            CASE WHEN count(q.owner)=count(*) AND count(DISTINCT q.owner)=1 THEN min(q.owner)
                 WHEN {form}=0 AND count(q.owner)=count(*) THEN ''
                 WHEN {form}=0 AND count(DISTINCT CASE WHEN s.preferred THEN q.owner END)=1
                      AND max(s.preferred AND q.owner IS NULL)=0 THEN min(CASE WHEN s.preferred THEN q.owner END)
                 WHEN count(q.owner)<count(*) OR
                      ({form}=1 AND count(DISTINCT s.exact)>1 AND count(DISTINCT q.owner)>1) THEN NULL
                 ELSE '' END owner,
            coalesce(min(CASE WHEN s.preferred AND r.term=s.exact THEN r.name END),min(s.display)) display,
            min(2,min(r.depth)) shared_depth
            FROM spellings s JOIN playable_terms t ON t.term=s.folded
            LEFT JOIN qualified q ON q.id=s.id LEFT JOIN result r ON r.id=q.owner
            WHERE s.{column}<>'' GROUP BY s.{column})
            SELECT d.term,d.owner,coalesce(r.name,d.display),coalesce(r.depth,d.shared_depth)
            FROM decisions d LEFT JOIN result r ON r.id=d.owner""")
        for batch in publication_batches(game, decisions, 4000):
            with game:
                game.executemany("INSERT INTO kb_answer_key VALUES(?,?,?,?,?,?)",
                    ((version, form, term, entity, name or term, depth or 0) for term, entity, name, depth in batch))
    with game:
        game.execute(
            "UPDATE kb_bank SET metadata=? WHERE id=?", (json.dumps(meta), version)
        )
    return version, meta
