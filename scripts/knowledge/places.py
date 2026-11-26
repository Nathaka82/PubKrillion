"""Geographic question rules with traceable paths through administrative areas."""

import gzip
import json
import re
import urllib.parse
from collections import defaultdict, deque
from functools import lru_cache

from graph import folded_spelling, spelling
from world_classes import MEMBERS
from world_links import identifiers

PHRASES = {
    131: ("in", "located in", "situated in"),
    19: ("born in", "who was born in"),
    20: ("who died in", "that died in"),
    915: ("filmed in", "shot in"),
    840: ("set in",),
}
PATTERN = re.compile(
    " ("
    + "|".join(
        re.escape(p)
        for p in sorted((p for variants in PHRASES.values() for p in variants), key=len, reverse=True)
    )
    + ") "
)


def expression(prop):
    if prop not in PHRASES:
        raise ValueError("Unknown geographic relationship.")
    within = "wdt:P131*/wdt:P17?"
    return f"(wdt:P17|(wdt:P131|wdt:P276)/{within})" if prop == 131 else f"wdt:P{prop}/{within}"


def members(cls, prop, target, *, check_class=True):
    # UNION bounds the traversal even if a source contains a geographic cycle.
    expression(prop)
    first = "131,276" if prop == 131 else str(prop)
    direct = (
        " UNION SELECT id FROM world_link WHERE prop=17 AND target=(SELECT dest FROM requested)"
        if prop == 131
        else ""
    )
    distinct = "AND i.id<>(SELECT dest FROM requested)" if prop == 131 else ""
    sql = f"""WITH RECURSIVE requested(root,dest) AS (VALUES(?,?)),
        zone(id) AS (SELECT dest FROM requested UNION SELECT id FROM world_link WHERE prop=17 AND target=(SELECT dest FROM requested)
        UNION SELECT l.id FROM zone z CROSS JOIN world_link l ON (l.prop,l.target)=(131,z.id)),
        scope(id) AS (SELECT root FROM requested UNION SELECT p.child FROM scope s JOIN world_parent p ON p.parent=s.id),
        candidate(id) AS (SELECT l.id FROM zone z CROSS JOIN world_link l ON l.target=z.id AND l.prop IN ({first}){direct})"""
    if not check_class:
        return sql + " SELECT id FROM candidate WHERE 1 " + distinct.replace("i.id", "id"), (cls, target)
    sql += f""",typed(id) AS (SELECT id FROM ({MEMBERS}))
        SELECT m.id FROM (SELECT id FROM candidate INTERSECT SELECT id FROM typed) m CROSS JOIN item i ON i.id=m.id
        WHERE length(i.name) BETWEEN 1 AND 140 {distinct}"""
    return sql, (cls, target)


def condition(db, cls, prop, target):
    from world_links import target_label

    subject = db.execute("SELECT name FROM item WHERE id=?", (cls,)).fetchone()
    label = target_label(db, target)
    if not subject or not label or prop not in PHRASES:
        return None
    return {
        "classId": cls,
        "class": subject[0],
        "prop": prop,
        "relation": PHRASES[prop][0],
        "target": "wd:Q" + str(target),
        "label": label,
        "geo": True,
    }


def wording(c):
    from catalogue import named_question

    return named_question(c["class"], f"{c['relation']} {c['label']}")


def scope(conditions):
    rules = []
    for c in conditions:
        if not c.get("geo"):
            rules.append(f"The source must list {c['label']} as the {c['relation']}.")
            continue
        head = {
            131: "location",
            19: "birthplace",
            20: "place of death",
            915: "filming location",
            840: "story setting",
        }[c["prop"]]
        rules.append(f"The recorded {head} must be {c['label']} or a place the source lists within it.")
    return " ".join(rules)


def alternatives(db, card):
    """Offer the containing administrative area as a separately worded question."""
    cls, prop, target = map(int, card["key"][2:].split("-"))
    row = db.execute("SELECT release FROM live_rule WHERE key=?", (card["key"],)).fetchone()
    areas = json.loads(gzip.decompress(row[0])).get("areas") if row else None
    if areas is None:
        parents = [
            r[0] for r in db.execute("SELECT target FROM world_link WHERE (id,prop)=(?,131)", (target,))
        ]
    else:
        parents = areas.get(str(target), [])
    options = []
    for parent in parents:
        if parent == target or not (c := condition(db, cls, prop, parent)):
            continue
        key = f"g-{cls}-{prop}-{parent}"
        current = db.execute("SELECT n FROM live_rule WHERE key=?", (key,)).fetchone()
        if current:
            count = current[0]
        else:
            query, args = members(cls, prop, parent)
            count = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        if count > card["records"]:
            options.append(dict(card, key=key, prompt=wording(c), records=count))
    return sorted(options, key=lambda c: (c["records"], c["prompt"]))[:2]


def match(db, body, edition, *, include_empty=False, resolve_only=False, request_target=False, date_key=""):
    from world_links import target_label

    matches = {}
    for split in PATTERN.finditer(body.rstrip("?").strip()):
        subject = folded_spelling(body[: split.start()])
        place = body[split.end() :].rstrip("?").strip()
        if split[1] == "in":
            from catalogue import RELATION_PHRASES

            if any(
                phrase.startswith("in ") and place.startswith(phrase[3:] + " ")
                for phrase in (*RELATION_PHRASES.values(), "in the country of")
            ):
                continue
        classes = identifiers(
            db, "human" if subject in ("person", "people", "someone", "somebody") else subject, "class",
            literal=body[:split.start()]
        )
        if len(classes) != 1:
            continue
        prop = next(p for p, words in PHRASES.items() if split[1] in words)
        targets = identifiers(db, folded_spelling(place), "target")
        tagged = re.fullmatch(r"(.+)\s+\(q([1-9]\d{0,11})\)", place)
        if tagged and targets != [int(tagged[2])]:
            continue
        region_label = None
        parts = re.fullmatch(r"(.+?)(?:,\s*| within )(.+)", place)
        if len(targets) != 1 and parts:
            parents = identifiers(db, folded_spelling(parts[2]), "target")
            if len(parents) == 1:
                targets = [
                    r[0]
                    for r in db.execute(
                        """WITH RECURSIVE area(id) AS (VALUES(?) UNION
                    SELECT id FROM world_link WHERE prop=17 AND target=? UNION SELECT l.id FROM area a
                    CROSS JOIN world_link l ON (l.prop,l.target)=(131,a.id))
                    SELECT n.target FROM world_target_name n WHERE n.term=? AND n.target IN (SELECT id FROM area)
                    AND n.target<>? ORDER BY n.preferred DESC""",
                        (parents[0], parents[0], folded_spelling(parts[1]), parents[0]),
                    )
                ]
                if targets:
                    region_label = target_label(db, parents[0])
        if (
            not tagged
            and not parts
            and db.execute("SELECT 1 FROM sqlite_master WHERE name='target_lookup'").fetchone()
        ):
            from target_lookup import Request, can_request, require

            req = Request(classes[0], prop, spelling(place), date_key, "g")
            known = db.execute("SELECT 1 FROM target_lookup WHERE key=?", (req.key,)).fetchone()
            if known or (
                request_target
                and (not targets or len(targets) == 1 and not target_label(db, targets[0]))
                and can_request(db, req)
            ):
                targets = [require(db, req, edition)]
        if len(targets) > 1:
            exact = [qid for qid in targets if spelling(target_label(db, qid) or "") == spelling(place)]
            targets = exact or targets
        if len(targets) > 1:
            targets = [
                qid
                for qid in targets
                if db.execute(
                    "SELECT 1 FROM world_link WHERE target=? AND prop IN (131,276,17,?) LIMIT 1",
                    (qid, prop),
                ).fetchone()
            ]
            if len(targets) > 1:
                choices = []
                for qid in targets[:5]:
                    label = target_label(db, qid)
                    countries = [
                        r[0]
                        for r in db.execute(
                            """SELECT i.name FROM world_link l JOIN item i ON i.id=l.target
                                WHERE l.id=? AND l.prop IN (131,17) ORDER BY l.prop DESC,i.name LIMIT 3""",
                            (qid,),
                        )
                    ]
                    choices.append(", ".join([label, *countries]) + f" (Q{qid})")
                raise ValueError(
                    "Several places match that name. Include its region after a comma or use its place ID. "
                    + ", ".join(choices)
                )
        if len(targets) != 1:
            continue
        cls, target = classes[0], targets[0]
        c = condition(db, cls, prop, target)
        if c is None:
            continue
        if region_label:
            c = {**c, "label": c["label"] + ", " + region_label}
        elif tagged or (
            re.fullmatch(r"q[1-9]\d*", place)
            and len(identifiers(db, folded_spelling(c["label"]), "target")) != 1
        ):
            c = {**c, "label": f"{c['label']} (Q{target})"}
        key = f"g-{cls}-{prop}-{target}"
        if resolve_only:
            matches[key] = {"key": key}
            continue
        live = db.execute("SELECT n FROM live_rule WHERE key=?", (key,)).fetchone()
        if live:
            if not live[0]:
                from catalogue import RuleWithdrawn

                raise RuleWithdrawn(
                    "The latest source check has no named matches for this geographic question."
                )
            count = live[0]
        else:
            query, args = members(cls, prop, target)
            count = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        if count or include_empty:
            matches[key] = {
                "key": key,
                "prompt": wording(c),
                "records": count,
                "edition": edition,
                "complete": False,
            }
    return next(iter(matches.values())) if len(matches) == 1 else None


def first_properties(prop):
    return (131, 276, 17) if prop == 131 else (prop,)


def shortest_path(item, prop, target, outgoing, *, required=True):
    queue = deque([(item, ())])
    seen = set()
    while queue:
        current, path = queue.popleft()
        for pid, end in outgoing(current, (131, 17) if path else first_properties(prop)):
            if pid == 17 and end != target:
                continue
            steps = (*path, (current, pid, end))
            if end == target:
                return steps
            if end not in seen:
                seen.add(end)
                queue.append((end, steps))
    if required:
        raise ValueError("The source could not verify every step of a geographic match.")
    return ()


def collect_paths(items, prop, target, query):
    return matching_paths(items, prop, target, query, required=True)[1]


def matching_paths(items, prop, target, query, *, required=False):
    """Fetch shared place hierarchies in batches and keep a shortest witness for each member."""
    from world_facts import identity

    edges = defaultdict(set)

    def fetch(ids, props):
        reached = set()
        ordered = sorted(ids)
        for start in range(0, len(ordered), 200):
            batch = ordered[start : start + 200]
            subjects = " ".join(f"wd:Q{i}" for i in batch)
            predicates = " ".join(f"wdt:P{p}" for p in props)
            rows = query(f"""SELECT DISTINCT ?u ?p ?v WHERE {{ VALUES ?u {{ {subjects} }}
                VALUES ?p {{ {predicates} }} ?u ?p ?v .
                FILTER(STRSTARTS(STR(?v),"http://www.wikidata.org/entity/Q")) }}""")
            for row in rows:
                u = int(identity(row["u"]["value"])[1:])
                v = int(identity(row["v"]["value"])[1:])
                p = row["p"]["value"].rsplit("/", 1)[-1]
                if u not in batch or not re.fullmatch(r"P[1-9]\d*", p) or int(p[1:]) not in props:
                    raise ValueError("The source returned a geographic edge outside the requested batch.")
                pid = int(p[1:])
                if pid != 17 or v == target:
                    edges[u].add((pid, v))
                    reached.add(v)
        return reached

    frontier = fetch(items, first_properties(prop)) - {target}
    visited = set()
    while frontier:
        visited.update(frontier)
        frontier = fetch(frontier, (131, 17)) - visited - {target}

    def outgoing(item, props):
        return sorted((p, v) for p, v in edges[item] if p in props)

    found, steps = set(), set()
    for item in items:
        if prop == 131 and item == target:
            if required:
                raise ValueError("A place cannot qualify as located inside itself.")
            continue
        path = shortest_path(item, prop, target, outgoing, required=required)
        if path:
            found.add(item)
            steps.update(path)
    return found, sorted(steps)


def proof_reader(db, live_keys=(), captures=None):
    graphs = {}
    for key in live_keys:
        if captures is not None:
            triples = captures.get(key)
        else:
            row = db.execute("SELECT release FROM live_rule WHERE key=?", (key,)).fetchone()
            triples = json.loads(gzip.decompress(row[0])).get("proofs", {}).get(key) if row else None
        if triples is not None:
            adjacency = defaultdict(list)
            for subject, pid, obj in triples:
                adjacency[subject].append((pid, obj))
            graphs[key] = adjacency

    @lru_cache(maxsize=16384)
    def outgoing(item, props):
        marks = ",".join("?" for _ in props)
        return tuple(
            db.execute(
                f"SELECT prop,target FROM world_link WHERE id=? AND prop IN ({marks}) ORDER BY prop,target",
                (item, *props),
            )
        )

    def proof(entity, c):
        item, target, prop = int(entity[4:]), int(c["target"][4:]), c["prop"]
        key = f"g-{c['classId']}-{prop}-{target}"
        if key in live_keys and key not in graphs:
            ask = f"ASK {{ wd:Q{item} {expression(prop)} wd:Q{target} }}"
            return {
                "url": "https://query.wikidata.org/#" + urllib.parse.quote(ask, safe=""),
                "propertyPath": expression(prop),
            }

        def captured(subject, props):
            return [(p, v) for p, v in graphs[key][subject] if p in props]

        path = shortest_path(item, prop, target, captured if key in graphs else outgoing)
        steps = [
            {
                "entity": f"wd:Q{s}",
                "property": f"P{p}",
                "target": f"wd:Q{o}",
                "url": f"https://www.wikidata.org/wiki/Q{s}#P{p}",
            }
            for s, p, o in path
        ]
        return {"url": steps[0]["url"], "steps": steps}

    return proof
