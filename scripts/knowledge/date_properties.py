"""Read Wikidata's time-property definitions and names independently of game prompts."""

import json
import re
import time

from date_rules import PROPERTIES
from graph import digest, folded_spelling
from watched_rules import replace
from world_facts import bindings


def location(storage):
    return storage / "knowledge-sources" / "wikidata-time-properties.json"


def refresh(storage):
    cache = location(storage)
    if cache.exists() and cache.stat().st_mtime + 7 * 86400 > time.time():
        return
    scope = '?property wikibase:propertyType wikibase:Time; rdfs:label ?name . FILTER(LANG(?name)="en")'
    rows = bindings(f"""SELECT DISTINCT ?property ?name ?alias WHERE {{ {scope}
        OPTIONAL {{ ?property skos:altLabel ?alias . FILTER(LANG(?alias)="en") }} }}""")
    properties = {}
    for row in rows:
        pid = re.fullmatch(r"https?://www\.wikidata\.org/entity/P([1-9]\d*)", row["property"]["value"])
        if not pid:
            raise ValueError("The date dictionary contains an invalid property identifier.")
        label = row["name"]["value"]
        primary, aliases = properties.setdefault(int(pid[1]), (label, set()))
        if not label or label != primary:
            raise ValueError("The date dictionary contains inconsistent property labels.")
        if alias := row.get("alias", {}).get("value"):
            aliases.add(alias)
    total = bindings(f"SELECT (COUNT(DISTINCT ?property) AS ?n) WHERE {{ {scope} }}")
    if not properties or len(total) != 1 or int(total[0]["n"]["value"]) != len(properties):
        raise ValueError("The date-property count changed during collection.")
    values = [[prop, name, sorted(aliases)] for prop, (name, aliases) in sorted(properties.items())]
    replace(
        cache, json.dumps({"properties": values, "revision": digest(values)}, ensure_ascii=False).encode()
    )


def install(db, storage):
    cache = location(storage)
    if cache.exists():
        data = json.loads(cache.read_bytes())
        rows = data["properties"]
        if data["revision"] != digest(rows):
            raise ValueError("The date-property dictionary failed verification.")
    else:
        rows = [[p, forms[0], []] for p, forms in sorted(PROPERTIES.items())]
    for prop, name, aliases in rows:
        db.execute("INSERT INTO date_property VALUES(?,?)", (prop, name))
        db.executemany(
            "INSERT OR IGNORE INTO date_property_name VALUES(?,?,?)",
            ((folded_spelling(label), prop, int(label == name)) for label in [name, *aliases]),
        )
    return digest(rows)
