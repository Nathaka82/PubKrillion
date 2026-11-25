"""Build the element register from PubChem's complete periodic table export."""

import json
import os
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from graph import digest, ingest, norm
from sources import fetch

URL = "https://pubchem.ncbi.nlm.nih.gov/rest/pug/periodictable/JSON"
SOURCE = "general-elements"


def elements(table):
    columns = table["Table"]["Columns"]["Column"]
    required = {"AtomicNumber", "Name", "Symbol", "StandardState", "GroupBlock"}
    if not required.issubset(columns) or len(columns) != len(set(columns)):
        raise ValueError("The periodic table is missing a required column or contains duplicate columns.")
    rows, symbols, names = {}, set(), set()
    for record in table["Table"]["Row"]:
        row = dict(zip(columns, record["Cell"], strict=True))
        number = int(row["AtomicNumber"])
        symbol, name = row["Symbol"], row["Name"]
        if (number in rows or symbol in symbols or name in names or not name.strip()
                or not re.fullmatch(r"[A-Z][a-z]{0,2}", symbol)):
            raise ValueError("Invalid or duplicate entry in the periodic table.")
        rows[number] = row
        symbols.add(symbol)
        names.add(name)
    if not rows or min(rows) != 1 or max(rows) != len(rows):
        raise ValueError("The periodic table has missing atomic numbers.")
    return rows


def load(db, export, local_names, *, minimum=0):
    rows = elements(export)
    if len(rows) < minimum:
        raise ValueError("The periodic table omits previously published atomic numbers.")
    # A local spelling or symbol can enrich an official entry. Membership and
    # identity always come from the atomic number in the publisher's export.
    aliases = {}
    for answer in local_names:
        for alias in answer["aliases"]:
            if re.fullmatch(r"[A-Z][a-z]{0,2}", alias):
                aliases[alias] = answer
    records = []
    for number, row in sorted(rows.items()):
        name, symbol = row["Name"], row["Symbol"]
        record = {"id": f"pubchem:element:{number}", "name": name, "aliases": [symbol],
                  "types": ["chemical element"], "url": f"https://pubchem.ncbi.nlm.nih.gov/element/{number}",
                  "edges": [[relation, row[field]] for field, relation in
                            (("StandardState", "standard state"), ("GroupBlock", "group")) if row[field]]}
        prior = aliases.get(symbol)
        if prior and norm(name) in {norm(v) for v in (prior["name"], *prior["aliases"])}:
            record.update(name=prior["name"], aliases=sorted({name, symbol, *prior["aliases"]}), depth=prior["depth"])
        records.append(record)
    facets = {"chemical element": {"standard state": "Name a chemical element whose standard state PubChem lists as {value}",
                                    "group": "Name a chemical element that PubChem classifies as {value}"}}
    ingest(db, {"source": SOURCE, "revision": digest([records, facets]), "url": URL, "completeFacets": facets,
                "terms": {"chemical element": ["chemical elements", "element", "chemical element or give its symbol"]},
                "census": {"chemical element": len(rows)}}, records)


def refresh(db, cache: Path, local_names, offline):
    previous = json.loads(cache.read_bytes()) if cache.is_file() else None
    minimum = db.execute("SELECT count(*) FROM entity WHERE source=?", (SOURCE,)).fetchone()[0]
    if previous is not None:
        minimum = max(minimum, len(elements(previous)))
    if offline:
        if previous is not None:
            load(db, previous, local_names, minimum=minimum)
        return
    raw = fetch(URL, limit=2_000_000)
    load(db, json.loads(raw), local_names, minimum=minimum)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=cache.parent, delete=False) as staged:
        try:
            staged.write(raw)
            staged.close()
            os.replace(staged.name, cache)
        finally:
            staged.close()
            Path(staged.name).unlink(missing_ok=True)
