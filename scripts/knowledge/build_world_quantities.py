"""Build a class-independent index of unqualified Wikidata lengths and masses."""

import csv
import hashlib
import io
import math
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from graph import ROOT, UNITS
from world_facts import DIMENSIONS
from world_facts import UNITS as WIKIDATA_UNITS

OUTPUT = ROOT / "data/knowledge/wikidata-quantities.sqlite"
ENDPOINT = "https://qlever.dev/api/wikidata"
PREFIX = """
PREFIX p: <http://www.wikidata.org/prop/>
PREFIX psn: <http://www.wikidata.org/prop/statement/value-normalized/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX ql: <http://qlever.cs.uni-freiburg.de/builtin-functions/>
"""


def rows(query, expected):
    args = urllib.parse.urlencode({"query": PREFIX + query, "action": "tsv_export"})
    request = urllib.request.Request(
        ENDPOINT + "?" + args, headers={"User-Agent": "KrillClubQuiz/1.0"}
    )
    for delay in (1, 4, None):
        try:
            response = urllib.request.urlopen(request, timeout=240)
            break
        except urllib.error.HTTPError as err:
            if delay is None or err.code not in (429, 500, 502, 503, 504):
                raise RuntimeError(err.read(600).decode(errors="replace")) from err
            time.sleep(delay)
    with response:
        reader = csv.reader(
            io.TextIOWrapper(response, encoding="utf8"),
            delimiter="\t",
            quoting=csv.QUOTE_NONE,
        )
        if next(reader) != expected:
            raise ValueError("Unexpected SPARQL export columns")
        for row in reader:
            if len(row) != len(expected):
                raise ValueError("Incomplete SPARQL export row: " + repr(row)[:250])
            yield row


def get_dimension(db, name, pid, fallback=False):
    # QLever indexes statement predicates. Enumerating the qualifier set separately
    # avoids an expensive correlated NOT EXISTS over every measured item.
    query = f"""SELECT DISTINCT ?s WHERE {{ ?s psn:{pid} ?v; ql:has-predicate ?pred .
        FILTER(STRSTARTS(STR(?pred),"http://www.wikidata.org/prop/qualifier/")) }}"""
    excluded = {statement for (statement,) in rows(query, ["?s"])}
    print(name, "qualified statements excluded:", len(excluded), flush=True)
    bound_query = f"""SELECT DISTINCT ?v ?predicate ?bound WHERE {{
        ?s psn:{pid} ?v . VALUES ?predicate {{ wikibase:quantityLowerBound wikibase:quantityUpperBound }}
        ?v ?predicate ?bound }}"""
    bounds = {}
    for value, predicate, bound in rows(bound_query, ["?v", "?predicate", "?bound"]):
        bounds.setdefault(value, {})["low" if "LowerBound" in predicate else "high"] = (
            float(bound)
        )
    print(name, "quantity nodes with uncertainty:", len(bounds), flush=True)
    rank = "a wikibase:BestRank"
    scope = ""
    if fallback:
        # A preferred statement can describe a particular part, date or record.
        # Use normal-ranked whole-entity statements only if no preferred value qualified.
        scope = f"{{ SELECT DISTINCT ?item WHERE {{ ?item p:{pid} ?best . ?best wikibase:rank wikibase:PreferredRank }} }}"
        rank = "wikibase:rank wikibase:NormalRank"
    query = f"""SELECT ?item ?s ?v ?amount ?unit WHERE {{
        {scope} ?item p:{pid} ?s . ?s {rank}; psn:{pid} ?v .
        ?v wikibase:quantityAmount ?amount; wikibase:quantityUnit ?unit .
    }}"""
    ranges = {}

    def values():
        offset = 0
        while True:
            count = 0
            for row in rows(
                query + f" ORDER BY ?s LIMIT 20000 OFFSET {offset}",
                ["?item", "?s", "?v", "?amount", "?unit"],
            ):
                count += 1
                yield row
            if count < 20000:
                break
            offset += count

    for item, stmt, value, amount, unit in values():
        if stmt in excluded:
            continue
        unit_id = unit.removesuffix(">").rsplit("/", 1)[-1]
        if unit_id not in WIKIDATA_UNITS:
            continue
        dimension, scale = UNITS[WIKIDATA_UNITS[unit_id]]
        if dimension != name:
            continue
        qid = int(item.removesuffix(">").rsplit("/Q", 1)[-1])
        bound = bounds.get(value, {})
        low, high = (
            bound.get("low", float(amount)) * scale,
            bound.get("high", float(amount)) * scale,
        )
        if not (math.isfinite(low) and math.isfinite(high) and 0 <= low <= high):
            continue
        if qid in ranges:
            low, high = min(low, ranges[qid][0]), max(high, ranges[qid][1])
        ranges[qid] = (low, high)
    if not ranges and not fallback:
        raise ValueError("Missing measurement export for " + name)
    with db:
        db.executemany(
            "INSERT OR IGNORE INTO measure VALUES(?,?,?,?)",
            ((name, qid, low, high) for qid, (low, high) in sorted(ranges.items())),
        )
        revision = hashlib.sha256()
        for qid, low, high in db.execute(
            "SELECT id,low,high FROM measure WHERE dimension=? ORDER BY id", (name,)
        ):
            revision.update(f"{qid}\t{low}\t{high}\n".encode())
        db.execute(
            "INSERT OR REPLACE INTO release VALUES(?,?,?)",
            (name, pid, revision.hexdigest()),
        )
    print(
        name,
        db.execute(
            "SELECT count(*) FROM measure WHERE dimension=?", (name,)
        ).fetchone()[0],
        "measured entities",
        flush=True,
    )


def main():
    with tempfile.TemporaryDirectory(dir=OUTPUT.parent) as folder:
        path = Path(folder) / OUTPUT.name
        db = sqlite3.connect(path)
        try:
            db.executescript("""CREATE TABLE measure(dimension TEXT,id INTEGER,low REAL,high REAL,
                PRIMARY KEY(dimension,id)) WITHOUT ROWID; CREATE TABLE release(dimension TEXT PRIMARY KEY,pid TEXT,revision TEXT);""")
            with db:
                for dim, pid in DIMENSIONS.items():
                    get_dimension(db, dim, pid)
                    get_dimension(db, dim, pid, fallback=True)
        finally:
            db.close()
        os.replace(path, OUTPUT)


if __name__ == "__main__":
    main()
