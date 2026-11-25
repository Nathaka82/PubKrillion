"""Acquire current, source-attributed Wikidata facts for a resolved question."""

import json
import re
import time
import urllib.parse

from graph import digest, ingest
from sources import fetch

UNITS = {
    "Q11573": "m",
    "Q828224": "km",
    "Q174728": "cm",
    "Q174789": "mm",
    "Q11570": "kg",
    "Q41803": "g",
}
DIMENSIONS = {"length": "P2043", "mass": "P2067"}


def watch(db, kind, args):
    payload = json.dumps(args, ensure_ascii=False)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO world_watch VALUES(?,?,?,0)",
            (digest([kind, args]), kind, payload),
        )


def refresh_watched(db):
    failures = []
    for key, kind, payload in db.execute(
        "SELECT id,kind,args FROM world_watch WHERE attempted < unixepoch()-86400 ORDER BY attempted,id LIMIT 2"
    ).fetchall():
        with db:
            db.execute(
                "UPDATE world_watch SET attempted=unixepoch() WHERE id=?", (key,)
            )
        try:
            args = json.loads(payload)
            if kind == "quantity":
                quantities(db, *args)
            elif kind == "relation":
                live_relation(db, *args)
            else:
                from sources import wikidata

                wikidata(db, args[0], args[1], kinds=args[2] if len(args) > 2 else True)
        except (OSError, ValueError) as err:
            failures.append(str(err))
    return failures


def bindings(query):
    prefixes = """
        PREFIX wd: <http://www.wikidata.org/entity/>
        PREFIX wdt: <http://www.wikidata.org/prop/direct/>
        PREFIX p: <http://www.wikidata.org/prop/>
        PREFIX psn: <http://www.wikidata.org/prop/statement/value-normalized/>
        PREFIX wikibase: <http://wikiba.se/ontology#>
        PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
        PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
        PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>
    """
    errors = []
    for endpoint in (
        "https://qlever.dev/api/wikidata?",
        "https://query.wikidata.org/sparql?",
    ):
        try:
            engine_query = (
                query
                if "qlever.dev" in endpoint
                else query.replace(
                    "ql:has-predicate ?qualifier", "?qualifier ?qualifierValue"
                )
            )
            args = urllib.parse.urlencode(
                {"query": prefixes + engine_query, "format": "json"}
            )
            data = fetch(
                endpoint + args + "&action=sparql_json_export",
                limit=12_000_000,
                timeout=65,
            )
            return json.loads(data)["results"]["bindings"]
        except (OSError, ValueError, KeyError) as err:
            failure = err
            errors.append(f"{urllib.parse.urlsplit(endpoint).hostname}: {err}")
    raise ValueError(
        "The Wikidata query services could not return these facts: " + " / ".join(errors)
    ) from failure


def identity(value):
    item = value.rsplit("/", 1)[-1]
    if not re.fullmatch(r"Q[1-9]\d*", item):
        raise ValueError("Unexpected Wikidata item.")
    return item


def current(db, source, force):
    row = db.execute("SELECT checked FROM source WHERE id=?", (source,)).fetchone()
    return row and not force and time.time() - row[0] < 7 * 86400


def publish(db, key, url, terms, records, allow_empty=False):
    rows = sorted(records.values(), key=lambda r: r["id"])
    if not rows and not allow_empty:
        raise ValueError("Wikidata has no matching named statements for this request.")
    ingest(
        db,
        {"source": key, "url": url, "terms": terms, "revision": digest([terms, rows])},
        rows,
    )
    with db:
        db.execute("UPDATE source SET checked=unixepoch() WHERE id=?", (key,))


def membership_scope(qid, kinds):
    branches = [
        f"{{ ?item wdt:P31 wd:{qid} }}",
        f"""{{ ?item wdt:P31/wdt:P279+ wd:{qid} . FILTER NOT EXISTS {{
            ?item wdt:P31 ?other . FILTER NOT EXISTS {{ ?other wdt:P279* wd:{qid} }}
        }} }}""",
        f"""{{ VALUES ?profession {{ wd:Q28640 wd:Q12737077 wd:Q4164871 }}
            wd:{qid} (wdt:P31|wdt:P279)/wdt:P279* ?profession .
            ?item wdt:P106/wdt:P279* wd:{qid} }}""",
    ]
    if kinds:
        branches.append(f"{{ ?item wdt:P279+ wd:{qid} }}")
    return "{ " + " UNION ".join(branches) + " }"


def live_relation(db, resolved, plan, force=False):
    cls, kinds, _, label, prop, target, target_name = resolved
    key = relation_key(resolved, plan)
    watch(db, "relation", [resolved, plan])
    if current(db, key, force):
        return key
    # All identifiers are integers resolved from the local graph.
    scope = f"?item wdt:P{prop} wd:Q{target} . " + membership_scope("Q" + str(cls), kinds)
    records = {}
    after = ""
    while True:
        cursor_filter = f"FILTER(STR(?item)>{json.dumps(after)})" if after else ""
        query = f"""SELECT ?item ?itemLabel WHERE {{
          {{ SELECT DISTINCT ?item WHERE {{ {scope} {cursor_filter} }}
             ORDER BY STR(?item) LIMIT 1000 }}
          OPTIONAL {{ ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en") }}
        }} ORDER BY STR(?item)"""
        hits = bindings(query)
        for row in hits:
            item = identity(row["item"]["value"])
            name = row.get("itemLabel", {}).get("value", item)
            if name != item and 0 < len(name) <= 140:
                records[item] = {
                    "id": "wd:" + item,
                    "name": name,
                    "types": [plan["class"]],
                    "edges": [[label, plan["target"]]],
                    "url": "https://www.wikidata.org/wiki/" + item,
                }
        if len(hits) < 1000:
            break
        cursor = hits[-1]["item"]["value"]
        if cursor <= after:
            raise ValueError("Wikidata did not advance its results page.")
        after = cursor
    publish(
        db,
        key,
        f"https://www.wikidata.org/wiki/Property:P{prop}",
        {plan["class"]: [], plan["target"]: [target_name]},
        records,
        allow_empty=True,
    )
    return key


def relation_key(resolved, plan):
    cls, kinds, _, _, prop, target, _ = resolved
    return "wd-linked-" + digest([cls, kinds, plan, prop, target])[:24]


def quantities(db, qid, term, dimension, is_reference, force=False):
    pid = DIMENSIONS[dimension]
    key = "wd-quantity-" + digest([qid, term, pid, is_reference])[:24]
    watch(db, "quantity", [qid, term, dimension, is_reference])
    if current(db, key, force):
        return
    scope = (
        f"VALUES ?item {{ wd:Q{qid} }}"
        if is_reference
        else f"?item wdt:P31/wdt:P279* wd:Q{qid} ."
    )
    records = {}
    after = ""
    excluded = {
        row["s"]["value"]
        for row in bindings(f"""SELECT DISTINCT ?s WHERE {{
          {scope} ?item p:{pid} ?s . ?s ql:has-predicate ?qualifier .
          FILTER(STRSTARTS(STR(?qualifier),"http://www.wikidata.org/prop/qualifier/"))
        }}""")
    }
    while True:
        cursor_filter = f"FILTER(STR(?s)>{json.dumps(after)})" if after else ""
        query = f"""SELECT ?item ?itemLabel ?s ?amount ?unit ?low ?high WHERE {{
          {{ SELECT DISTINCT ?s ?item ?amount ?unit ?low ?high WHERE {{
            {scope} ?item p:{pid} ?s . ?s a wikibase:BestRank; psn:{pid} ?v .
            ?v wikibase:quantityAmount ?amount; wikibase:quantityUnit ?unit .
            OPTIONAL {{ ?v wikibase:quantityLowerBound ?low }}
            OPTIONAL {{ ?v wikibase:quantityUpperBound ?high }}
            {cursor_filter}
          }} ORDER BY STR(?s) LIMIT 4000 }}
          OPTIONAL {{ ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel)="en") }}
        }} ORDER BY STR(?s)"""
        hits = bindings(query)
        for row in hits:
            if row["s"]["value"] in excluded:
                continue
            unit = UNITS.get(row["unit"]["value"].rsplit("/", 1)[-1])
            item = identity(row["item"]["value"])
            name = row.get("itemLabel", {}).get("value", item)
            if not unit or name == item or not 0 < len(name) <= 140:
                continue
            record = records.setdefault(
                item,
                {
                    "id": "wd:" + item,
                    "name": name,
                    "types": [] if is_reference else [term],
                    "facts": [],
                    "url": "https://www.wikidata.org/wiki/" + item,
                },
            )
            record["facts"].append(
                {
                    "property": dimension,
                    "unit": unit,
                    "basis": "typical",
                    "low": float(row.get("low", row["amount"])["value"]),
                    "high": float(row.get("high", row["amount"])["value"]),
                    "url": record["url"] + "#" + pid,
                }
            )
        if len(hits) < 4000:
            break
        cursor = hits[-1]["s"]["value"]
        if cursor <= after:
            raise ValueError("Wikidata did not advance its measurements page.")
        after = cursor
    publish(
        db,
        key,
        "https://www.wikidata.org/wiki/Property:" + pid,
        {"wd:Q" + str(qid): [term]} if is_reference else {term: []},
        records,
    )
