"""Import the Nobel Prize API's enumerated award and laureate records."""

import json
import os
import re
from collections import defaultdict
from fractions import Fraction
from tempfile import NamedTemporaryFile

from graph import digest, ingest
from sources import fetch

URL = "https://api.nobelprize.org/2.1/nobelPrizes?limit=1000"


def download():
    rows = []
    total = None
    while total is None or len(rows) < total:
        page = json.loads(fetch(URL + (f"&offset={len(rows)}" if rows else "")))
        meta, batch = page["meta"], page["nobelPrizes"]
        if (meta["offset"] != len(rows) or type(meta["count"]) is not int or
                total is not None and total != meta["count"] or not batch):
            raise ValueError("The Nobel Prize API returned an incomplete or changing page.")
        total = meta["count"]
        rows.extend(batch)
    return {"count": total, "prizes": rows}


def load(db, export):
    prizes = export["prizes"]
    if not prizes or len(prizes) != export["count"]:
        raise ValueError("The Nobel Prize award count does not match its export.")
    last_year = max(int(p["awardYear"]) for p in prizes)
    first_year = min(int(p["awardYear"]) for p in prizes)
    records = {}
    groups, aliases = defaultdict(set), defaultdict(set)
    seen = set()
    for prize in prizes:
        year, category = int(prize["awardYear"]), prize["category"]["en"]
        if (year, category) in seen:
            raise ValueError("Duplicate year and category in the Nobel Prize export.")
        seen.add((year, category))
        recipients = prize.get("laureates", [])
        if not recipients:
            if not re.search(r"\bno\b.+\bwas awarded\b", prize.get("topMotivation", {}).get("en", ""), re.I):
                raise ValueError("An award is missing its recipients and an explicit non-award record.")
            continue
        try:
            portions = [Fraction(r["portion"]) for r in recipients]
        except (ValueError, ZeroDivisionError, TypeError, KeyError) as error:
            raise ValueError("Invalid prize share in the Nobel Prize export.") from error
        if (len({r["id"] for r in recipients}) != len(recipients) or
                any(part <= 0 or part > 1 for part in portions) or sum(portions) != 1):
            raise ValueError("The listed recipients do not account for the whole prize.")
        full = prize["categoryFullName"]["en"]
        full = full[0].lower() + full[1:] if full.startswith("The ") else full
        decade = year // 10 * 10
        span = f"from {max(first_year, decade)} through {min(last_year, decade+9)}"
        scopes = {
            "laureate listed by NobelPrize.org": ["Nobel", "Nobel prizes", "Nobel laureates"],
            f"recipient of {full}": [category, f"Nobel {category}", f"Nobel Prize in {category}", "Nobel prizes"],
            f"recipient of {full} {span}": [f"{category} {decade}s", f"Nobel {category} {decade}s"],
            f"laureate listed by NobelPrize.org for {year}": [f"Nobel {year}", f"Nobel prizes {year}"],
        }
        for winner in recipients:
            key = winner["id"]
            if not re.fullmatch(r"[1-9]\d*", key):
                raise ValueError("Invalid laureate ID.")
            names = [winner[k]["en"] for k in ("knownName", "orgName", "fullName") if k in winner]
            if not names:
                raise ValueError("A laureate is missing a name.")
            item = records.setdefault(key, {"id": "nobel:" + key, "name": names[0], "aliases": set(),
                                             "types": set(), "url": "https://api.nobelprize.org/2.1/laureate/" + key})
            item["aliases"].update(names)
            for cls, terms in scopes.items():
                groups[cls].add(key)
                aliases[cls].update(terms)
                item["types"].add(cls)
    ingest(db, {"source": "nobel-prize-api", "revision": digest(export), "url": URL,
                "terms": aliases, "census": {cls: len(ids) for cls, ids in groups.items()}}, records.values())


def import_snapshot(db, cache, offline):
    if offline:
        if cache.is_file():
            load(db, json.loads(cache.read_bytes()))
        return
    export = download()
    load(db, export)
    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=cache.parent, mode="w", encoding="utf8", delete=False) as staged:
        try:
            json.dump(export, staged, ensure_ascii=False, separators=(",", ":"))
            staged.close()
            os.replace(staged.name, cache)
        finally:
            staged.close()
            if os.path.exists(staged.name):
                os.unlink(staged.name)
