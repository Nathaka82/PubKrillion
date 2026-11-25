"""Resolve new relationship targets on the builder, independently of the historical graph."""

import gzip
import json
import re
import time
import urllib.parse
from dataclasses import dataclass

from class_updates import page_names
from graph import digest, folded_spelling, spelling
from sources import fetch
from world_facts import bindings

SCHEMA = """
CREATE TABLE current_target(id INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE target_description(id INTEGER PRIMARY KEY,description TEXT NOT NULL);
CREATE TABLE current_target_name(term TEXT,target INTEGER,preferred INTEGER,
    PRIMARY KEY(term,target)) WITHOUT ROWID;
CREATE TABLE target_lookup(key TEXT PRIMARY KEY,ids TEXT NOT NULL,crowded INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE target_related(lookup TEXT,original TEXT,rule TEXT,PRIMARY KEY(lookup,rule)) WITHOUT ROWID;
CREATE INDEX target_related_by_rule ON target_related(original);
"""
QID = re.compile(r"q([1-9]\d{0,11})", re.IGNORECASE)


@dataclass(frozen=True)
class Request:
    cls: int
    prop: int
    term: str
    date: str = ""
    mode: str = "w"

    @property
    def key(self):
        return "lookup:" + json.dumps(
            [self.cls, self.prop, self.term, self.date] + ([self.mode] if self.mode != "w" else []),
            ensure_ascii=True,
            separators=(",", ":"),
        )

    def rule(self, target):
        link = f"{self.mode}-{self.cls}-{self.prop}-{target}"
        return f"during:{self.date}+{link}" if self.date else link

    @classmethod
    def decode(cls, key):
        from date_rules import Rule

        if not isinstance(key, str) or not key.startswith("lookup:") or len(key) > 1600:
            return None
        try:
            a, p, term, date, *suffix = json.loads(key[7:])
        except (ValueError, TypeError):
            return None
        if (
            type(a) is not int
            or not 0 < a < 10**12
            or type(p) is not int
            or not 0 < p < 10**7
            or not isinstance(term, str)
            or not 1 <= len(term) <= 140
            or not term.isprintable()
            or spelling(term) != term
            or not isinstance(date, str)
        ):
            return None
        if date and (not (r := Rule.decode(date)) or r.cls != a):
            return None
        from places import PHRASES

        if suffix and (suffix != ["g"] or p not in PHRASES):
            return None
        request = cls(a, p, term, date, "g" if suffix else "w")
        return request if request.key == key else None


def search_ids(term):
    if qid := QID.fullmatch(term):
        return [int(qid[1])], False
    query = {
        "action": "wbsearchentities",
        "language": "en",
        "format": "json",
        "type": "item",
        "search": term,
        "limit": 50,
    }
    candidates, seen = set(), set()
    for _ in range(3):
        data = json.loads(fetch("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(query)))
        for result in data["search"]:
            qid = QID.fullmatch(result["id"])
            if not qid:
                raise ValueError("Wikidata returned an invalid search identifier.")
            candidates.add(int(qid[1]))
        cursor = data.get("search-continue")
        if cursor is None:
            return sorted(candidates), False
        if type(cursor) is not int or cursor in seen:
            raise ValueError("Wikidata returned an inconsistent search continuation.")
        seen.add(cursor)
        query["continue"] = cursor
    return sorted(candidates), True


def can_request(db, request):
    # A lookup consumes one complete target name. Preserve unmatched conditions
    # and negation for the rest of the question parser.
    if (
        not Request.decode(request.key)
        or re.search(
            r"\b(?:except|excluding|without)\s|\b(?:before|after|between|since)\s+\d|\b(?:and|or) (?:with|without|in|from|released|directed|published|performed|starring)\b",
            request.term,
        )
        or re.search(r"\(\s*q[1-9]\d*\s*\)$", request.term)
    ):
        return False
    return (
        db.execute(
            "SELECT 1 FROM item c,predicate p WHERE c.id=? AND p.id=?", (request.cls, request.prop)
        ).fetchone()
        is not None
    )


def named_candidates(term, seeds=()):
    candidates, crowded = search_ids(term)
    crowded = crowded or len(seeds) >= 151
    candidates = sorted(set(candidates) | set(seeds))
    forms = {}
    for start in range(0, len(candidates), 40):
        batch = candidates[start : start + 40]
        entities = [{"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}} for i in batch]
        for qid, name, aliases, _ in page_names(entities):
            i = int(qid[1:])
            if i not in batch or i in forms:
                raise ValueError("A verified target name belongs to an unexpected entity.")
            if QID.fullmatch(term) or any(spelling(n) == term for n in (name, *aliases)):
                forms[i] = [i, name, sorted(set(aliases))]
    return forms, crowded


def prior_candidates(previous, term):
    if not previous or not previous.is_file() or QID.fullmatch(term):
        return []
    from contextlib import closing
    from catalogue import reader

    with closing(reader(previous)) as prior:
        if prior.execute("SELECT format FROM publication").fetchone()[0] < 23:
            return []
        normalized = folded_spelling(term)
        return [r[0] for r in prior.execute(
            """SELECT id FROM scope_name WHERE term=?
               UNION SELECT target FROM world_target_name WHERE term=? LIMIT 151""",
            (normalized, normalized))]


def capture(request, seeds=()):
    forms, crowded = named_candidates(request.term, seeds)
    matched, related = [], []
    if forms:
        values = " ".join(f"wd:Q{i}" for i in forms)
        from places import expression

        geographic = request.mode == "g"
        edge = expression(request.prop) if geographic else f"wdt:P{request.prop}"
        member = "(wdt:P31|wdt:P106)" if geographic else "wdt:P31"
        condition = f"VALUES ?target {{ {values} }} ?item {edge} ?target . "
        if geographic and request.prop == 131:
            condition += "FILTER(?item!=?target) "
        scope = condition + f"?item {member}/wdt:P279* wd:Q{request.cls} ."
        if request.date:
            from date_rules import Rule
            from date_updates import PREFIX, body

            scope = condition + body(Rule.decode(request.date))
        else:
            PREFIX = ""
        rows = bindings(PREFIX + "SELECT DISTINCT ?target WHERE { " + scope + " }")
        for row in rows:
            target = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d*)", row["target"]["value"])
            if not target or (i := int(target[1])) not in forms or i in matched:
                raise ValueError("The source returned a relationship outside the target search.")
            matched.append(i)
        if not request.date and not geographic and not crowded and len(forms) == 1:
            # Counts nominate alternative question subjects. Each bank is
            # collected and verified independently before the game can offer it.
            target = next(iter(forms))
            counts = bindings(f"""SELECT ?class (COUNT(DISTINCT ?item) AS ?n) WHERE {{
                ?item wdt:P{request.prop} wd:Q{target}; wdt:P31 ?class .
                FILTER(STRSTARTS(STR(?class),"http://www.wikidata.org/entity/Q")) }}
                GROUP BY ?class HAVING(COUNT(DISTINCT ?item)>=2) ORDER BY DESC(?n) ?class LIMIT 24""")
            for row in counts:
                cls = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d*)", row["class"]["value"])
                n = int(row["n"]["value"])
                if not cls or n < 2:
                    raise ValueError("Invalid subject in the current relationship counts.")
                if int(cls[1]) != request.cls:
                    related.append([int(cls[1]), target, n])
    matched.sort()
    rows = [forms[i] for i in sorted(set(matched) | {item for _, item, _ in related})]
    descriptions = {}
    if len(matched) > 1 or crowded and matched:
        values = " ".join(f"wd:Q{i}" for i in matched[:8])
        hints = bindings(f"""SELECT ?target ?description WHERE {{ VALUES ?target {{ {values} }}
            ?target <http://schema.org/description> ?description . FILTER(LANG(?description)="en") }}""")
        for hint in hints:
            qid = QID.fullmatch(hint["target"]["value"].rsplit("/", 1)[-1])
            description = hint["description"]["value"]
            if not qid or int(qid[1]) not in matched[:8]:
                raise ValueError("A disambiguation description belongs to a different target.")
            if description.isprintable() and len(description) <= 240:
                descriptions[qid[1]] = description
    return {
        "key": request.key,
        "rows": rows,
        "crowded": crowded,
        "matched": matched,
        "related": related,
        "descriptions": descriptions,
        "revision": digest([request.key, rows, crowded, matched, related, descriptions]),
        "checked": int(time.time()),
    }


def requests(storage):
    from watched_rules import requests_folder

    for file in requests_folder(storage).glob("*.lookup"):
        if file.stat().st_size > 1600 or not re.fullmatch(r"[0-9a-f]{64}", file.stem):
            continue
        try:
            key = file.read_text("ascii")
        except UnicodeError:
            continue
        if file.stem == digest(key) and (request := Request.decode(key)):
            yield request, storage / "knowledge-sources" / "target-lookups" / (file.stem + ".v2.json.gz")


def collect(db, storage, offline, previous=None, topic_captures=()):
    from watched_rules import due_at, follow, replace

    pending = sorted((due_at(file), req.key, file, req) for req, file in requests(storage))
    versions, targets, descriptions = [], {}, {}
    for data in topic_captures:
        version = (data["checked"], data["key"])
        for item, name, aliases in data["rows"]:
            if item not in targets or version > targets[item][0]:
                targets[item] = (version, name, aliases)
            hint = data["descriptions"].get(str(item))
            if hint and (item not in descriptions or version > descriptions[item][0]):
                descriptions[item] = (version, hint)
    now, budget = int(time.time()), 4
    for due, key, file, req in pending:
        if not offline and now >= due and budget:
            budget -= 1
            try:
                data = capture(req, prior_candidates(previous, req.term))
                replace(file, gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0))
            except (ValueError, OSError, KeyError) as error:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".attempt").touch()
                print(f"Target lookup for Q{req.cls} / P{req.prop} deferred: {error}", flush=True)
        if not file.is_file():
            continue
        data = json.loads(gzip.decompress(file.read_bytes()))
        rows, crowded = data["rows"], data["crowded"]
        ids, related = data["matched"], data["related"]
        identity = [key, rows, crowded, ids, related]
        if "descriptions" in data:
            identity.append(data["descriptions"])
        if data["key"] != key or digest(identity) != data["revision"]:
            raise ValueError("The verified target lookup failed its fingerprint check.")
        db.execute("INSERT INTO target_lookup VALUES(?,?,?)", (key, json.dumps(ids), crowded))
        for item, name, aliases in rows:
            version = (data["checked"], key)
            if item not in targets or version > targets[item][0]:
                targets[item] = (version, name, aliases)
            if (hint := data.get("descriptions", {}).get(str(item))) and (
                item not in descriptions or version > descriptions[item][0]
            ):
                descriptions[item] = (version, hint)
            if not offline and item in ids and not crowded and len(ids) <= 8:
                follow(storage, req.rule(item))
        offered = 0
        for cls, item, _ in related:
            if not db.execute("SELECT 1 FROM world.triple WHERE p=31 AND o=? LIMIT 1", (cls,)).fetchone():
                continue
            rule = f"w-{cls}-{req.prop}-{item}"
            db.execute("INSERT INTO target_related VALUES(?,?,?)", (key, req.rule(item), rule))
            if not offline:
                follow(storage, rule)
            offered += 1
            if offered == 2:
                break
        versions.append((key, data["revision"]))
    for item, (_, name, aliases) in sorted(targets.items()):
        db.execute("INSERT INTO current_target VALUES(?,?)", (item, name))
        db.executemany(
            """INSERT INTO current_target_name VALUES(?,?,?) ON CONFLICT(term,target)
            DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
            ((folded_spelling(n), item, int(n == name)) for n in [name, *aliases]),
        )
    if targets:
        versions.append(("current-target-names", digest([(i, *targets[i][1:]) for i in sorted(targets)])))
    if descriptions:
        hints = [(i, descriptions[i][1]) for i in sorted(descriptions)]
        db.executemany("INSERT INTO target_description VALUES(?,?)", hints)
        versions.append(("target-descriptions", digest(hints)))
    return sorted(versions)


def alternatives(db, edition, *, key=None, original=None):
    column, value = ("lookup", key) if key is not None else ("original", original)
    return [
        {"key": rule, "edition": edition, "prompt": prompt, "records": n, "complete": False}
        for rule, prompt, n in db.execute(
            f"""SELECT DISTINCT q.rule,q.prompt,q.n FROM target_related t
            JOIN current_question q USING(rule) WHERE t.{column}=? ORDER BY q.n DESC,q.prompt LIMIT 2""",
            (value,),
        )
    ]


def class_choices(db, subject, prop, target, edition):
    """Resolve an abbreviated class name against verified questions for this target."""
    cards = []
    for key, prompt, n in db.execute(
        """SELECT q.rule,q.prompt,q.n FROM current_question_topic t
            JOIN current_question q USING(rule) WHERE t.entity=? AND q.rule GLOB 'w-*'""",
        (target,),
    ):
        _, cls, pid, tid = key.split("-")
        if (int(pid), int(tid)) != (prop, target):
            continue
        name = db.execute("SELECT name FROM item WHERE id=?", (int(cls),)).fetchone()[0]
        if qualifier := re.fullmatch(r"(.+) \([^()]+\)", name):
            base = folded_spelling(qualifier[1])
            if subject in (base, base + "s"):
                cards.append(
                    {"key": key, "prompt": prompt, "records": n, "edition": edition, "complete": False}
                )
    return cards


def require(db, request, edition):
    """Preserve uncertainty when search returns several possible source identities."""
    from catalogue import RuleWithdrawn, UnverifiedRule, named_question, world_clause
    from date_rules import DateChoices
    from world_links import info

    row = db.execute("SELECT ids,crowded FROM target_lookup WHERE key=?", (request.key,)).fetchone()
    if not row:
        raise UnverifiedRule(
            request.key, "This edition has not checked that name against the requested relationship."
        )
    ids, crowded = json.loads(row[0]), row[1]
    if not ids:
        if crowded:
            raise ValueError("This name has too many search results. Use the intended Wikidata Q number.")
        if choices := alternatives(db, edition, key=request.key):
            raise DateChoices(
                choices,
                "The source records this name under different question subjects. Select a question to see its documented answers.",
            )
        pending = [
            r[0]
            for r in db.execute(
                """SELECT t.rule FROM target_related t LEFT JOIN live_rule l
            ON l.key=t.rule WHERE t.lookup=? AND l.key IS NULL""",
                (request.key,),
            )
        ]
        if pending:
            raise UnverifiedRule(
                request.key,
                "The builder is verifying related question subjects for this name.",
                requests=pending,
            )
        raise RuleWithdrawn("The source check found no named target with that spelling and relationship.")
    if crowded or len(ids) > 1:
        choices = []
        for i in ids[:8]:
            key = request.rule(i)
            labels = info(db, request.cls, request.prop, i)
            n = db.execute("SELECT n FROM live_rule WHERE key=?", (key,)).fetchone()
            if labels and n and n[0]:
                target = f"{labels[2]} (Q{i})"
                if request.mode == "g":
                    from places import PHRASES

                    phrase = "located in" if request.prop == 131 else PHRASES[request.prop][0]
                    clause = f"{phrase} {target}"
                else:
                    clause = world_clause(request.prop, labels[1], target)
                if request.date:
                    from date_rules import Rule

                    prompt = Rule.decode(request.date).prompt(db) + " and " + clause
                else:
                    prompt = named_question(labels[0], clause)
                choices.append(
                    {"key": key, "prompt": prompt, "edition": edition, "records": n[0], "complete": False}
                )
        if not choices and not crowded and len(ids) <= 8:
            pending = [
                request.rule(i)
                for i in ids
                if not db.execute("SELECT 1 FROM live_rule WHERE key=?", (request.rule(i),)).fetchone()
            ]
            if pending:
                raise UnverifiedRule(
                    request.key,
                    "The builder is checking which source identities have playable answers.",
                    requests=pending,
                )
        options = []
        available = db.execute("SELECT 1 FROM sqlite_master WHERE name='target_description'").fetchone()
        for i in ids[:8]:
            hint = (
                db.execute("SELECT description FROM target_description WHERE id=?", (i,)).fetchone()
                if available
                else None
            )
            options.append(f"Q{i} ({hint[0]})" if hint else f"Q{i}")
        raise DateChoices(
            choices,
            "Choose the intended Wikidata entity, or enter its Q number. Matches include "
            + ", ".join(options)
            + ".",
        )
    return ids[0]
