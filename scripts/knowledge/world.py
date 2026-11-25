"""Resolve quiz prompts against an indexed, immutable Wikidata5M release."""

import contextlib
import os
import re
import sqlite3
from importlib.metadata import PackageNotFoundError, version

from compiler import class_term, interpret, noun
from graph import ROOT, digest, folded_spelling, ingest, norm
from sources import RELATIONS, wikidata
from world_facts import live_relation, quantities, relation_key, watch
from world_index import RELEASE, SOURCE, TITLE_RELEASE, index_path

try:
    FREQUENCY_VERSION = version("wordfreq")
except PackageNotFoundError:
    FREQUENCY_VERSION = None

# Wikidata P106 describes a person's occupation. It can establish membership only
# when the requested class is itself a profession, occupation or position.
CLASSES = """
WITH RECURSIVE classes(id) AS (
    SELECT ?1 UNION SELECT t.s FROM classes c JOIN triple t ON t.p=279 AND t.o=c.id
), ancestry(id) AS (
    SELECT o FROM triple WHERE s=?1 AND p IN (31,279)
    UNION SELECT t.o FROM ancestry a JOIN triple t ON t.s=a.id AND t.p=279
), occupation AS (
    SELECT 1 FROM ancestry WHERE id IN (28640,12737077,4164871) LIMIT 1
), mixed_types(id) AS MATERIALIZED (
    SELECT c.id FROM classes c WHERE c.id<>?1 AND 4<=(
        SELECT count(*) FROM (SELECT DISTINCT s FROM triple WHERE o=c.id AND p=31 LIMIT 24) sample
        WHERE EXISTS(SELECT 1 FROM triple alt WHERE alt.s=sample.s AND alt.p=31
                     AND alt.o NOT IN (SELECT id FROM classes))
    )
)
"""
# Some archived subclasses span several media or entity types. Four incompatible
# records among 24 indexed examples require a more specific membership proof.
CONSISTENT_TYPE = """(ty.o=?1 OR (ty.o NOT IN (SELECT id FROM mixed_types) AND NOT EXISTS (
    SELECT 1 FROM triple other WHERE other.s=ty.s AND other.p=31 AND other.o NOT IN (SELECT id FROM classes)
)))"""
MEMBERS = CLASSES + f""", members(id) AS (
    SELECT ty.s FROM classes c JOIN triple ty ON ty.p=31 AND ty.o=c.id WHERE {CONSISTENT_TYPE}
    UNION SELECT ty.s FROM classes c JOIN triple ty ON ty.p=106 AND ty.o=c.id WHERE EXISTS(SELECT 1 FROM occupation)
    UNION SELECT id FROM classes WHERE ?2 AND id<>?1
)"""
# Start with the target's incoming links to avoid materialising all films or people for a narrow relation.
FILTERED = CLASSES + f""", members(id) AS (
    SELECT DISTINCT link.s FROM triple link WHERE link.p=?3 AND link.o=?4 AND (
        EXISTS (SELECT 1 FROM triple ty WHERE ty.s=link.s AND ty.o IN (SELECT id FROM classes)
                AND ((ty.p=31 AND {CONSISTENT_TYPE}) OR (ty.p=106 AND EXISTS(SELECT 1 FROM occupation))))
        OR (?2 AND link.s<>?1 AND link.s IN (SELECT id FROM classes)))
)
"""


def prompt_body(prompt):
    return re.sub(
        r"^(?:(?:please )?(?:name|identify|suggest|give me|list) |can you name )",
        "",
        prompt.strip().rstrip(" .?"),
        flags=re.IGNORECASE,
    )


class Ambiguous(ValueError):
    pass


def lookup_ids(world, text, kind, table="entity"):
    explicit = re.fullmatch(r"(?:.*\()?([QP])(\d+)\)?", text, re.IGNORECASE)
    column = "e.id"
    if explicit:
        if explicit[1].upper() != ("P" if table == "property" else "Q"):
            return []
        source = f"{table} e WHERE e.id=?"
        param = int(explicit[2])
    else:
        column = "a.id"
        source = (
            "(SELECT id,term FROM entity_alias UNION ALL SELECT id,term FROM title) a WHERE a.term=?"
            if table == "entity"
            else "property_alias a WHERE a.term=?"
        )
        param = norm(noun(text))
    where = ""
    if kind == "class":
        where = f" AND EXISTS(SELECT 1 FROM triple t WHERE t.o={column} AND t.p IN (31,279,106))"
    found = [
        r[0]
        for r in world.execute(
            f"SELECT DISTINCT {column} FROM {source}{where}", (param,)
        )
    ]
    if explicit:
        return found
    precise = folded_spelling(noun(text))
    matches = []
    for item in found:
        names = world.execute(f"SELECT names FROM {table} WHERE id=?", (item,)).fetchone()
        labels = names[0].split("\t") if names else []
        title = world.execute("SELECT name FROM title WHERE id=?", (item,)).fetchone() if table == "entity" else None
        if title:
            labels.append(title[0])
        if any(folded_spelling(name) == precise for name in labels) or (
                precise == param and all(norm(name) != param for name in labels)):
            matches.append(item)
    return matches


def one(world, ids, phrase, table="entity"):
    if len(ids) > 1 and table == "entity":
        exact = {
            r[0]
            for r in world.execute(
                "SELECT id,name FROM title WHERE term=?", (norm(noun(phrase)),)
            ) if folded_spelling(r[1]) == folded_spelling(noun(phrase))
        }
        preferred = [qid for qid in ids if qid in exact]
        if preferred:
            ids = preferred
    if len(ids) > 1 and table == "property":
        # Prefer the canonical property label to an incidental alias of a different property.
        canonical = [pid for pid in ids if norm(world.execute(
            "SELECT names FROM property WHERE id=?", (pid,)
        ).fetchone()[0].split("\t")[0]) == norm(phrase)]
        if canonical:
            ids = canonical
    if len(ids) > 1:
        prefix = "P" if table == "property" else "Q"
        choices = []
        for i in sorted(ids)[:4]:
            title = (
                world.execute("SELECT name FROM title WHERE id=?", (i,)).fetchone()
                if table == "entity"
                else None
            )
            names = (
                title[0]
                if title
                else world.execute(
                    f"SELECT names FROM {table} WHERE id=?", (i,)
                ).fetchone()[0]
            )
            choices.append(names.split("\t")[0] + f" ({prefix}{i})")
        raise Ambiguous(
            f"“{phrase}” has several meanings in the source. Specify an ID in the prompt: "
            + ", ".join(choices)
        )
    return ids[0] if ids else None


def relation_parts(world, body):
    for label, pid in sorted(RELATIONS.items(), key=lambda item: -len(item[0])):
        m = re.fullmatch(
            r"(.+?) (?:that (?:was |is )?|which (?:was |is )?|who (?:was |is )?|was |is )?"
            + re.escape(label)
            + r" (.+)",
            body,
            re.IGNORECASE,
        )
        if m:
            return m[1], label, int(pid[1:]), m[2]
    m = re.fullmatch(
        r"(.+?) (?:whose (.+?) is |(?:with|having|that has) (.+?)(?: of| is)? )(.+)",
        body,
        re.IGNORECASE,
    )
    if not m:
        return None
    subject, explicit, first, rest = m.groups()
    # Match complete property labels, longest first, so multiword properties stay intact.
    words = (first + " " + rest).split() if first else []
    pairs = (
        [(explicit, rest)]
        if explicit
        else [
            (" ".join(words[:i]), " ".join(words[i:]))
            for i in range(len(words) - 1, 0, -1)
        ]
    )
    for label, target in pairs:
        ids = lookup_ids(world, label, "property", "property")
        if ids:
            target = re.sub(r"^(?:of|is) ", "", target, flags=re.IGNORECASE)
            return subject, norm(label), one(world, ids, label, "property"), target
    return None


def classes_for(world, subject):
    subject = noun(subject)
    ids = lookup_ids(world, subject, "class")
    if not ids:
        # Try a singular form only when the source has no exact class of that name.
        stem = (
            re.sub(r"ies$", "y", subject)
            if subject.endswith("ies")
            else subject.removesuffix("s")
        )
        if stem != subject:
            ids = lookup_ids(world, stem, "class")
    return ids


def candidates(qid, kinds, prop=None, target=None):
    if prop is None:
        return MEMBERS, (qid, kinds)
    return FILTERED, (qid, kinds, prop, target)


def resolve(world, body):
    relation = relation_parts(world, body)
    subject, label, prop, target_name = relation or (body, None, None, None)
    subject = subject.strip()
    kinds = bool(
        re.match(r"^(?:(?:a|an|the) )?(?:type|kind) of ", subject, re.IGNORECASE)
    )
    ids = classes_for(world, subject)
    if not ids:
        return None
    if relation:
        targets = lookup_ids(world, target_name, "target")
        if not targets:
            return None
        # Distinct namesakes are only plausible when the actual subject and property match.
        pairs = []
        for cls in ids:
            for target in targets:
                sql, params = candidates(cls, kinds, prop, target)
                if world.execute(
                    sql + "SELECT 1 FROM members LIMIT 1", params
                ).fetchone():
                    pairs.append((cls, target))
        if pairs:
            ids = sorted({c for c, _ in pairs})
            cls = one(world, ids, subject)
            targets = sorted({t for c, t in pairs if c == cls})
        else:
            cls = one(world, ids, subject)
        target = one(world, targets, target_name)
    else:
        cls = one(world, ids, subject)
        target = None
    return cls, kinds, subject, label, prop, target, target_name


def project(graph, world, resolved):
    cls, kinds, subject, label, prop, target, target_name = resolved
    term = norm(noun(subject))
    if (
        kinds
        and not graph.execute(
            "SELECT 1 FROM term WHERE key=? AND source NOT LIKE 'world5m-%' LIMIT 1",
            (term,),
        ).fetchone()
    ):
        term = "type of " + term
    key = "world5m-" + digest([cls, kinds, term, label, prop, target])[:24]
    revision = digest([RELEASE, TITLE_RELEASE, FREQUENCY_VERSION, 6])
    plan = {"class": term, "worldSource": key}
    terms = {term: [term]}
    if label:
        target_id = "wd:Q" + str(target)
        plan.update(relation=label, target=target_id)
        terms[target_id] = [target_name]
    if graph.execute(
        "SELECT 1 FROM source WHERE id=? AND revision=?", (key, revision)
    ).fetchone():
        return plan
    query, args = candidates(cls, kinds, prop, target)
    result = world.execute(
        query
        + """
        SELECT e.id,e.names,COALESCE(i.n,0),t.name FROM members m JOIN entity e ON e.id=m.id
        LEFT JOIN incoming i ON i.id=e.id LEFT JOIN title t ON t.id=e.id ORDER BY e.id
    """,
        args,
    )

    def records():
        try:
            from wordfreq import zipf_frequency
        except ImportError:
            zipf_frequency = None
        for entity, names, incoming, title in result:
            names = names.split("\t")
            # Keep the credited article title whenever Wikimedia supplies one.
            name = title or next((n for n in names if not n.islower()), names[0])
            depth = 2
            if incoming >= 1000:
                depth = 1
            row = {
                "id": f"wd:Q{entity}",
                "name": name,
                "aliases": names,
                "url": f"https://www.wikidata.org/wiki/Q{entity}",
                "types": [term],
                "depth": depth,
                "graph_recognition": [0, incoming],
            }
            if label:
                row["edges"] = [[label, target_id]]
            if zipf_frequency and title:
                frequency = zipf_frequency(re.sub(r" \([^()]+\)$", "", title), "en")
                row["graph_recognition"] = [frequency, incoming]
                if 0 < frequency < 2.5 and 0 < incoming < 10:
                    row["depth"] = 4
                elif 0 < frequency < 3.2 and 0 < incoming < 100:
                    row["depth"] = 3
            yield row

    ingest(
        graph,
        {"source": key, "revision": revision, "url": SOURCE, "terms": terms},
        records(),
    )
    return plan


MEASURE_FILE = ROOT / "data/knowledge/wikidata-quantities.sqlite"


def source_revision():
    quantities = []
    if MEASURE_FILE.is_file():
        uri = MEASURE_FILE.resolve().as_uri() + "?mode=ro&immutable=1"
        with contextlib.closing(sqlite3.connect(uri, uri=True)) as reader:
            quantities = list(
                reader.execute(
                    "SELECT dimension,revision FROM release ORDER BY dimension"
                )
            )
    return digest([RELEASE, TITLE_RELEASE, FREQUENCY_VERSION, quantities])


def stored_measurements(graph, world, cls, subject, ref_id, ref, dimension):
    file = MEASURE_FILE
    if not file.is_file():
        return False
    world.execute(
        "ATTACH DATABASE ? AS quantities",
        (file.resolve().as_uri() + "?mode=ro&immutable=1",),
    )
    release = world.execute(
        "SELECT pid,revision FROM quantities.release WHERE dimension=?", (dimension,)
    ).fetchone()
    if not release:
        return False
    pid, revision = release
    url = "https://www.wikidata.org/wiki/Property:" + pid
    unit = "cm" if dimension == "length" else "g"

    def record(qid, name, lo, hi, types):
        link = f"https://www.wikidata.org/wiki/Q{qid}"
        return {
            "id": f"wd:Q{qid}",
            "name": name,
            "types": types,
            "url": link,
            "facts": [
                {
                    "property": dimension,
                    "low": lo,
                    "high": hi,
                    "unit": unit,
                    "basis": "typical",
                    "url": link + "#" + pid,
                }
            ],
        }

    reference = world.execute(
        "SELECT low,high FROM quantities.measure WHERE dimension=? AND id=?",
        (dimension, ref_id),
    ).fetchone()
    if not reference:
        return False
    key = "quantity-ref-" + digest([ref_id, ref, dimension])[:24]
    ingest(
        graph,
        {
            "source": key,
            "revision": revision,
            "url": url,
            "terms": {f"wd:Q{ref_id}": [ref]},
        },
        [record(ref_id, ref, *reference, [])],
    )
    key = "quantity-class-" + digest([cls, subject, dimension])[:24]
    if graph.execute(
        "SELECT 1 FROM source WHERE id=? AND revision=?", (key, revision)
    ).fetchone():
        return True
    sql, args = candidates(cls, False)
    rows = world.execute(
        sql
        + """
        SELECT e.id,COALESCE(t.name,e.names),q.low,q.high FROM members m
        JOIN quantities.measure q ON q.id=m.id AND q.dimension=?
        JOIN entity e ON e.id=m.id LEFT JOIN title t ON t.id=m.id
    """,
        (*args, dimension),
    )
    first = rows.fetchone()
    if not first:
        return False
    from itertools import chain

    ingest(
        graph,
        {"source": key, "revision": revision, "url": url, "terms": {subject: []}},
        (
            record(qid, name.split("\t")[0], lo, hi, [subject])
            for qid, name, lo, hi in chain([first], rows)
        ),
    )
    return True


def prepare(graph, gamefile, prompt, refresh=False, snapshot_only=False):
    file = index_path(gamefile)
    if not file.is_file():
        if os.environ.get("KNOWLEDGE_WORLD_DB"):
            raise ValueError("KNOWLEDGE_WORLD_DB must point to an installed Wikidata5M index.")
        return None
    with contextlib.closing(
        sqlite3.connect(file.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    ) as world:
        body = prompt_body(prompt)
        try:
            local_class = class_term(graph, body)
        except ValueError:
            local_class = None
        finite = graph.execute("""SELECT s.id,c.key FROM source s,json_each(s.census) c WHERE c.key=?""", (local_class,)).fetchall()
        if len(finite) == 1:
            return {"class": finite[0][1], "register": finite[0][0]}
        compare = re.fullmatch(
            r"(.+?) (?:that (?:is |grows )?|which (?:is )?|is )?(longer|larger|bigger|shorter|smaller|heavier|lighter) than (.+)",
            body,
            re.IGNORECASE,
        )
        if compare:
            subject, op, ref = compare.groups()
            # An existing specialist measurement source can already cover this comparison.
            try:
                local = interpret(graph, prompt)
            except ValueError:
                local = {}
            if (
                local.get("reference")
                and not refresh
                and graph.execute(
                    "SELECT 1 FROM fact WHERE id=? AND property=? AND basis='typical' AND source NOT LIKE 'wd-quantity-%' AND source NOT LIKE 'quantity-%' LIMIT 1",
                    (local["reference"], local["property"]),
                ).fetchone()
            ):
                return local
            cls = one(world, classes_for(world, subject), subject)
            ref_id = one(world, lookup_ids(world, ref, "target"), ref)
            if cls is None or ref_id is None:
                return None
            term = norm(noun(subject))
            dimension = "mass" if op.lower() in ("heavier", "lighter") else "length"
            if refresh or not stored_measurements(
                graph, world, cls, term, ref_id, noun(ref), dimension
            ):
                if snapshot_only:
                    return None
                quantities(graph, ref_id, noun(ref), dimension, True, refresh)
                quantities(graph, cls, term, dimension, False, refresh)
            project(graph, world, (cls, False, subject, None, None, None, None))
            return {
                "class": term,
                "reference": "wd:Q" + str(ref_id),
                "property": dimension,
                "op": "lt" if op.lower() in ("shorter", "smaller", "lighter") else "gt",
            }
        resolved = resolve(world, body)
        if not resolved:
            return None
        plan = project(graph, world, resolved)
        if resolved[4] is not None:
            key = relation_key(resolved, plan)
            args = [resolved, plan]
            watch(graph, "relation", args)
            with graph:
                attempt = not snapshot_only and graph.execute(
                    "UPDATE world_watch SET attempted=unixepoch() WHERE id=? AND (attempted < unixepoch()-3600 OR ?)",
                    (digest(["relation", args]), refresh),
                ).rowcount
            if attempt:
                try:
                    live_relation(graph, resolved, plan, refresh)
                except (OSError, ValueError):
                    if (
                        refresh
                        or not graph.execute(
                            "SELECT 1 FROM edge e JOIN member m USING(source,id) WHERE m.class=? AND e.relation=? AND e.target=? LIMIT 1",
                            (plan["class"], plan["relation"], plan["target"]),
                        ).fetchone()
                    ):
                        raise
            if graph.execute("SELECT 1 FROM source WHERE id=?", (key,)).fetchone():
                plan["edgeSource"] = key
        else:
            watch(graph, "class", [plan["class"], "Q" + str(resolved[0]), resolved[1]])
            if refresh:
                wikidata(
                    graph, plan["class"], "Q" + str(resolved[0]), kinds=resolved[1]
                )
        return plan
