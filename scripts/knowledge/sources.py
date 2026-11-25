"""Bounded, free structured-data readers used by the local research worker."""

import csv
import email.utils
import gzip
import hashlib
import io
import json
import re
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path

from graph import ROOT, digest, ingest, norm

BUCKET = "https://s3.us-west-2.amazonaws.com/us-west-2.opendata.source.coop"
FISH_PREFIX = "cboettig/fishbase/fb/"
PEAR = "https://plantdatabase.kpu.ca/PlantPrint/pycm"
DATA = ROOT / "data/knowledge"
AGENT = "KrillClubQuiz/1.0 (source-backed quiz research; single worker)"
_retry_after = {}


class DownloadTooLarge(ValueError):
    pass


def retry_wait(origin):
    return max(0, int(_retry_after.get(origin, 0) - time.monotonic() + 1))


def fetch(url, limit=8_000_000, timeout=20):
    origin = urllib.parse.urlsplit(url).netloc.lower()
    if _retry_after.get(origin, 0) > time.monotonic():
        raise OSError(f"{origin} requested a pause before the next download.")
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as error:
        header = (error.headers or {}).get("Retry-After", "")
        if error.code == 429 or error.code == 503 and header:
            try:
                delay = int(header) if header.isascii() and header.isdecimal() else (
                    email.utils.parsedate_to_datetime(header).timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                delay = 900
            _retry_after[origin] = time.monotonic() + max(1, delay)
        raise
    with response:
        if int(response.headers.get("Content-Length") or 0) > limit:
            raise DownloadTooLarge("Source exceeds download budget.")
        data = response.read(limit + 1)
        if len(data) > limit:
            raise DownloadTooLarge("Source exceeds download budget.")
        return data


def familiarity(frequency, languages, has_name):
    if frequency >= 4 and languages >= 10:
        return 1
    if frequency >= 3.2 or languages >= 10:
        return 2
    if frequency >= 2 or languages >= 5:
        return 3
    return 4 if has_name and languages else 2


def fish_release():
    query = {"list-type": "2", "prefix": FISH_PREFIX, "delimiter": "/"}
    releases = set()
    seen = set()
    while True:
        listing = ET.fromstring(fetch(BUCKET + "?" + urllib.parse.urlencode(query)))
        for prefix in listing.findall(".//{*}CommonPrefixes/{*}Prefix"):
            releases.update(re.findall(r"/v(\d{2}\.\d{2})/", prefix.text or ""))
        if listing.findtext("{*}IsTruncated", "false").lower() != "true":
            break
        token = listing.findtext("{*}NextContinuationToken")
        if not token or token in seen:
            raise ValueError("The FishBase release directory has an incomplete page.")
        seen.add(token)
        query["continuation-token"] = token
    if not releases:
        raise ValueError("The FishBase source returned no release identifiers.")
    return max(releases)


def fish_projection(release, output, local=None, names=None, maturity=None):
    import duckdb
    from wordfreq import zipf_frequency

    url = f"{BUCKET}/{FISH_PREFIX}v{release}/parquet/species.parquet"
    with tempfile.TemporaryDirectory(dir=output.parent) as tmp:
        path = Path(local) if local else Path(tmp) / "species.parquet"
        if not local:
            path.write_bytes(fetch(url, 120_000_000))
        alias_file = Path(names) if names else Path(tmp) / "comnames.parquet"
        if not names:
            alias_file.write_bytes(
                fetch(url.replace("/species.parquet", "/comnames.parquet"), 120_000_000)
            )
        maturity_file = Path(maturity) if maturity else Path(tmp) / "maturity.parquet"
        if not maturity:
            maturity_file.write_bytes(
                fetch(url.replace("/species.parquet", "/maturity.parquet"), 40_000_000)
            )
        conn = duckdb.connect(config={"memory_limit": "384MB", "threads": "1"})
        try:
            expected, distinct = conn.execute("""SELECT count(*),count(DISTINCT SpecCode) FROM read_parquet(?)
                WHERE Genus IS NOT NULL AND Species IS NOT NULL""", [str(path)]).fetchone()
            if not expected or expected != distinct:
                raise ValueError("The FishBase species export has missing or duplicate identifiers.")
            conn.execute(
                """CREATE TEMP TABLE maturity AS SELECT SpecCode,min(coalesce(nullif(LCL_Lm,0),Lm)) mature
                FROM read_parquet(?) WHERE Lm>0 AND Type1 IN ('TL','SL','FL') GROUP BY SpecCode""",
                [str(maturity_file)],
            )
            conn.execute(
                """CREATE TEMP TABLE regional AS SELECT SpecCode,
                list(DISTINCT coalesce(nullif(UnicodeText,''),decode(encode(ComName),'replace'))) names,
                count(DISTINCT Language) langs
                FROM read_parquet(?) WHERE coalesce(Misspelling,0)=0 GROUP BY SpecCode""",
                [str(alias_file)],
            )
            cursor = conn.execute(
                """SELECT s.SpecCode, Genus || ' ' || Species, FBname,
                CommonLength, LTypeComM, CommonLengthF, LTypeComF, Length, LTypeMaxM
                , LengthFemale, LTypeMaxF, names, langs, mature FROM read_parquet(?) s LEFT JOIN regional USING(SpecCode) LEFT JOIN maturity USING(SpecCode) WHERE Genus IS NOT NULL AND Species IS NOT NULL ORDER BY s.SpecCode""",
                [str(path)],
            )
            with output.open("wb") as raw, gzip.GzipFile(filename="",fileobj=raw,mode="wb",mtime=0) as packed, io.TextIOWrapper(packed,encoding="utf8",newline="") as file:
                writer = csv.writer(file)
                while rows := cursor.fetchmany(2000):
                    for *record, variants, languages, mature in rows:
                        aliases = sorted(
                            {
                                v.strip()
                                for v in (variants or [])
                                if v
                                and len(v.strip()) <= 140
                                and "�" not in v
                                and v.strip()
                            }
                        )
                        frequency = zipf_frequency(record[2] or "", "en")
                        depth = familiarity(frequency, languages or 0, record[2])
                        writer.writerow(
                            [
                                *record[:9],
                                json.dumps(aliases, ensure_ascii=False),
                                depth,
                                mature,
                                frequency,
                                languages or 0,
                                *record[9:],
                            ]
                        )
        except duckdb.Error as error:
            raise ValueError("FishBase could not project the source tables: "+str(error)) from error
        finally:
            conn.close()
    return expected


def fish(db, path, release, *, expected=None, fingerprint=None):
    source = {
        "source": "fishbase",
        "revision": release + "-traits5" + ("-" + fingerprint if fingerprint else ""),
        "terms": {"fish": ["fish species", "fishes"]},
        "url": f"{BUCKET}/{FISH_PREFIX}v{release}/parquet/species.parquet",
    }

    def rows():
        with gzip.open(path, "rt", encoding="utf8", newline="") as file:
            count = 0
            for (
                code,
                latin,
                common,
                male,
                mt,
                female,
                ft,
                maximum,
                xt,
                names,
                depth,
                mature,
                frequency,
                languages,
                *female_maximum,
            ) in csv.reader(file):
                if not code.isascii() or not code.isdecimal() or not latin.strip() or len(female_maximum) not in (0,2):
                    raise ValueError("Invalid FishBase species record.")
                count += 1
                typical = [
                    float(x)
                    for x, t in ((male, mt), (female, ft))
                    if x and t == "TL" and float(x) > 0
                ]
                facts = []
                if typical:
                    facts.append(
                        {
                            "property": "length",
                            "unit": "cm",
                            "low": min(typical),
                            "high": max(typical),
                            "basis": "typical",
                        }
                    )
                lower_bounds = [
                    float(x)
                    for x, t in ((male, mt), (female, ft))
                    if x and t in ("SL", "FL") and float(x) > 0
                ]
                if lower_bounds:
                    facts.append(
                        {
                            "property": "length",
                            "unit": "cm",
                            "low": min(lower_bounds),
                            "high": max(lower_bounds),
                            "basis": "typical-lower-bound",
                        }
                    )
                if not typical and not lower_bounds and mature and float(mature) > 0:
                    facts.append(
                        {
                            "property": "length",
                            "unit": "cm",
                            "low": float(mature),
                            "high": float(mature),
                            "basis": "maturity-lower-bound",
                            "url": "https://www.fishbase.se/Reproduction/MaturityList.php?ID="
                            + code,
                        }
                    )
                maxima = [(float(x), unit) for x, unit in [(maximum, xt), *([female_maximum] if female_maximum else [])]
                          if x and float(x) > 0]
                for value, unit in maxima:
                    if unit not in ("TL", "SL", "FL"):
                        continue
                    facts.append(
                        {
                            "property": "length",
                            "unit": "cm",
                            "low": value,
                            "high": value,
                            # Older projections omit the female maximum. They can still prove that
                            # a species has attained this length. Partial body lengths also lack an upper bound.
                            "basis": "record" if female_maximum and all(t == "TL" for _, t in maxima) else "record-lower-bound",
                        }
                    )
                yield {
                    "id": "fishbase:" + code,
                    "name": common or latin,
                    "aliases": [latin, *json.loads(names)],
                    "depth": int(depth),
                    "recognition": [float(frequency), int(languages)],
                    "types": ["fish"],
                    "facts": facts,
                    "url": "https://www.fishbase.se/summary/SpeciesSummary.php?ID="
                    + code,
                }
            if not count or (expected is not None and count != expected):
                raise ValueError("The projected fish rows do not match the FishBase species export.")

    ingest(db, source, rows())


def refresh_fish(db, cache, offline):
    """Replay the last complete capture, then check the upstream release directory once per day."""
    if offline and not cache.is_file():
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(cache)) as store, tempfile.TemporaryDirectory(dir=cache.parent) as tmp:
        store.execute("""CREATE TABLE IF NOT EXISTS capture(id INTEGER PRIMARY KEY CHECK(id=1), checked INTEGER,
                      release TEXT, expected INTEGER, checksum TEXT, csv BLOB)""")
        prior = store.execute("SELECT checked,release,expected,checksum,csv FROM capture WHERE id=1").fetchone()
        current = db.execute("SELECT revision FROM source WHERE id='fishbase'").fetchone()[0].split("-")[0]
        projection = Path(tmp) / "species.csv.gz"
        if prior and prior[4] is not None and prior[1] >= current:
            if hashlib.sha256(prior[4]).hexdigest() != prior[3]:
                raise ValueError("The cached FishBase projection failed its checksum.")
            projection.write_bytes(prior[4])
            fish(db, projection, prior[1], expected=prior[2], fingerprint=prior[3])
            current = prior[1]
        now = int(time.time())
        if offline or (prior and 0 <= now - prior[0] < 86400):
            return
        try:
            release = fish_release()
            if release > current:
                expected = fish_projection(release, projection)
                raw = projection.read_bytes()
                checksum = hashlib.sha256(raw).hexdigest()
                fish(db, projection, release, expected=expected, fingerprint=checksum)
                with store:
                    store.execute("INSERT OR REPLACE INTO capture VALUES(1,?,?,?,?,?)",
                                  (now, release, expected, checksum, raw))
            else:
                with store:
                    store.execute("""INSERT INTO capture(id,checked,release) VALUES(1,?,?)
                        ON CONFLICT(id) DO UPDATE SET checked=excluded.checked""", (now,current))
        except (OSError, EOFError, ValueError, ET.ParseError, ImportError, sqlite3.IntegrityError) as error:
            print(f"FishBase refresh deferred; using release {current}: {error}", flush=True)


def competition(db, src):
    event, cls = src["competition"], src["participant"]
    expected = src["expected"]
    years = [x[0] for x in src["editions"]]
    if not expected or sorted(set(expected)) != years or len(years) != len(expected):
        raise ValueError("Finals do not match the completed edition calendar.")
    rows = {}
    for year, winner, runner in src["editions"]:
        if type(year) is not int or norm(winner) == norm(runner):
            raise ValueError("A completed final needs two distinct sides.")
        for name, role in ((winner, "won"), (runner, "was runner-up in")):
            record = rows.setdefault(
                name,
                {
                    "id": cls + ":" + norm(name),
                    "name": name,
                    "types": [cls],
                    "aliases": src.get("aliases", {}).get(name, []),
                    "edges": [],
                },
            )
            for relation in ("reached", role):
                record["edges"].extend(
                    [[relation, event], [relation, event + "@" + str(year)]]
                )
    meta = {
        "source": "results-" + src["key"],
        "revision": digest(src),
        "url": src["url"],
        "terms": {event: src["event_aliases"], cls: src["participant_aliases"]},
        "calendar": {event: years},
    }
    ingest(db, meta, rows.values())


def bootstrap(db):
    participants = {}
    finalists = {}
    element_names = []
    for series in json.loads((DATA / "finals.json").read_text("utf8")):
        competition(db, series)
        participants["general-" + series["key"]] = series["participant"]
        finalists["general-" + series["key"]] = {norm(name) for _, winner, runner in series["editions"] for name in (winner, runner)}
    ingest(
        db,
        {
            "source": "kpu-pear",
            "revision": "typical-fruit-length-v1",
            "url": PEAR,
            "terms": {"pear": ["common pear", "European pear"]},
        },
        [
            {
                "id": "pear",
                "name": "Common pear",
                "facts": [
                    {
                        "property": "length",
                        "low": 8,
                        "high": 12,
                        "unit": "cm",
                        "basis": "typical",
                    }
                ],
            }
        ],
    )
    release = (DATA / "fishbase-release.txt").read_text().strip()
    old = db.execute("SELECT revision FROM source WHERE id='fishbase'").fetchone()
    projection = DATA / "fishbase.csv.gz"
    fingerprint = hashlib.sha256(projection.read_bytes()).hexdigest()
    if not old or (old[0].split("-")[0] <= release and old[0] != release+"-traits5-"+fingerprint):
        fish(db, projection, release, fingerprint=fingerprint)
    elif "-traits5" not in old[0]:
        # A worker may already hold a newer source release in the previous projection format.
        with db:
            db.execute("""UPDATE fact SET basis='maturity-lower-bound' WHERE source='fishbase'
                AND basis='typical-lower-bound' AND url LIKE '%/MaturityList.php?%'""")
            db.execute("UPDATE fact SET basis='record-lower-bound' WHERE source='fishbase' AND basis='record'")
            db.execute("UPDATE source SET revision=? WHERE id='fishbase'", (old[0].split("-")[0]+"-traits5",))
    for path in sorted(DATA.glob("*.source.json.gz")):
        with gzip.open(path, "rt", encoding="utf8") as f:
            payload = json.load(f)
        prior = db.execute(
            "SELECT checked FROM source WHERE id=?", (payload["source"]["source"],)
        ).fetchone()
        if not prior or prior[0] < payload["source"].get("captured", 0):
            ingest(db, payload["source"], payload["rows"])
    packs = ROOT / "data/packs"
    general = (
        q
        for p in sorted(packs.glob("*.json"))
        if p.stem not in ("index", "sources")
        for q in json.loads(p.read_text("utf8"))["questions"]
    )
    # Curated releases can seed subjects offline. Their stable IDs can also be supplied by future feeds.
    scopes = {
        "general-cheese": ("cheese", ["cheeses", "cheese variety", "type of cheese"]),
        "general-pasta": ("pasta", ["pasta shape", "type of pasta"]),
        "general-elements": ("chemical element", ["element", "chemical elements"]),
        "general-women-world-final": (
            "women world cup finalist 1991 2023",
            [
                "national side that reached the fifa women's world cup final between 1991 and 2023, including winners and runners-up"
            ],
        ),
        "general-women-wimbledon": (
            "women wimbledon finalist 2012 2024",
            [
                "player who reached the wimbledon women's singles final between 2012 and 2024, including winners and runners-up"
            ],
        ),
    }
    for q in general:
        if q["id"] == "general-elements":
            element_names = q["answers"]
            continue
        wording = re.sub(
            r"^name (?:an? )?", "", q["prompt"].rstrip(".?!"), flags=re.IGNORECASE
        )
        cls, terms = scopes.get(q["id"], (q["id"], []))
        if len(wording) <= 450:
            terms.append(wording)
        if not all(a.get("name") and len(a["name"]) <= 140 for a in q["answers"]):
            continue
        census = {}
        if q["id"] in finalists:
            expected = finalists[q["id"]]
            if {norm(a["name"]) for a in q["answers"]} != expected:
                raise ValueError("Finalist entries disagree with the dated competition results.")
            census[cls] = len(expected)
        src = {
            "source": q["id"],
            "revision": digest([q, participants.get(q["id"]), census]),
            "terms": {cls: terms},
            "url": q["source"],
            "census": census,
        }
        ingest(
            db,
            src,
            (
                {
                    "id": participants.get(q["id"], cls) + ":" + norm(a["name"]),
                    "name": a["name"],
                    "types": [cls],
                    "aliases": a["aliases"],
                    "depth": a["depth"],
                }
                for a in q["answers"]
            ),
        )


    from pubchem_register import load
    load(db, json.loads((DATA / "pubchem-periodic-table.json").read_bytes()), element_names)
    return element_names


def discover_class(term):
    labels = " ".join(
        json.dumps(s, ensure_ascii=False) + "@en"
        for s in sorted({term, term.lower(), term.capitalize()})
    )
    query = f"""SELECT DISTINCT ?item WHERE {{ VALUES ?label {{ {labels} }} ?item rdfs:label ?label .
        FILTER EXISTS {{ {{ ?member wdt:P31 ?item }} UNION {{ ?member wdt:P279 ?item }} }}
    }} LIMIT 3"""
    args = urllib.parse.urlencode({"query": query, "format": "json"})
    bindings = json.loads(
        fetch("https://query.wikidata.org/sparql?" + args, timeout=45)
    )["results"]["bindings"]
    exact = {b["item"]["value"].rsplit("/", 1)[-1] for b in bindings}
    if len(exact) != 1:
        raise ValueError(
            "Wikidata could not identify one class with that name. Use a more specific class or import a source."
        )
    return exact.pop()


RELATIONS = {
    "from": "P495",
    "in": "P17",
    "directed by": "P57",
    "written by": "P50",
    "born in": "P19",
    "part of": "P361",
    "composed by": "P86",
}


def relation_request(subject):
    for relation in sorted(RELATIONS, key=len, reverse=True):
        pattern = re.fullmatch(
            r"(.+?) (?:that (?:was |is )?|which (?:was |is )?|who (?:was |is )?|was |is )?"
            + relation
            + r" (.+)",
            subject,
            re.IGNORECASE,
        )
        if pattern and not re.search(
            r"\b(and|or|than|between|after|before)\b", pattern[2], re.IGNORECASE
        ):
            return pattern[1], relation, pattern[2].strip(" .?!")
    return None


def relation_source(db, term, qid, relation, target, export, property_id=None):
    property_id = property_id or RELATIONS.get(relation)
    if not isinstance(property_id, str) or not re.fullmatch(r"P[1-9]\d*", property_id):
        raise ValueError("A Wikidata relationship needs a valid property ID.")
    source = "wd:" + qid + ":" + term
    if not re.fullmatch(r"Q[1-9]\d*", qid):
        raise ValueError("Invalid Wikidata class identifier.")
    if not db.execute("SELECT 1 FROM source WHERE id=?", (source,)).fetchone():
        wikidata(db, term, qid)
    items = {
        r["id"][3:]: r
        for r in db.execute("SELECT * FROM entity WHERE source=?", (source,))
    }
    ids = sorted(items)
    entities = {}
    terms = {term: [term]}
    for start in range(0, len(ids), 250):
        values = " ".join(
            "wd:" + i for i in ids[start : start + 250] if re.fullmatch(r"Q[1-9]\d*", i)
        )
        from world_facts import bindings
        query = f"""SELECT DISTINCT ?item ?place ?name WHERE {{ VALUES ?item {{ {values} }}
            ?item wdt:{property_id} ?place . OPTIONAL {{ ?place rdfs:label ?name . FILTER(LANG(?name)="en") }}
            FILTER(isIRI(?place)) }}"""
        page = bindings(query)
        unnamed = {b["place"]["value"] for b in page if "name" not in b
                   and re.fullmatch(r"Q[1-9]\d*", b["place"]["value"].rsplit("/", 1)[-1])}
        localized = {item: label for item, label, _, _ in
                     named_items([{"item": {"value": uri}} for uri in sorted(unnamed)])}
        for b in page:
            eid = b["item"]["value"].rsplit("/", 1)[-1]
            target_id = b["place"]["value"].rsplit("/", 1)[-1]
            if not re.fullmatch(r"Q[1-9]\d*", target_id):
                continue
            pid = "wd:" + target_id
            name = b.get("name", {}).get("value") or localized.get(target_id)
            if not name or len(name) > 140 or not name.strip():
                continue
            terms[pid] = [name]
            e = items[eid]
            record = entities.setdefault(
                eid,
                {
                    "id": e["id"],
                    "name": e["name"],
                    "types": [term],
                    "url": e["url"],
                    "edges": [],
                },
            )
            record["edges"].append([relation, pid])
    rows = sorted(entities.values(), key=lambda r: r["id"])
    for row in rows:
        row["edges"].sort()
    src = {
        "source": "wd-rel:" + qid + ":" + property_id,
        "url": "https://www.wikidata.org/wiki/Property:" + property_id,
        "revision": digest([terms, rows]),
        "terms": terms,
        "baseRevision": db.execute("SELECT revision FROM source WHERE id=?", (source,)).fetchone()[0],
        "recipe": [term, qid, relation, target, property_id],
    }
    src["captured"] = int(time.time())
    ingest(db, src, rows)
    if export:
        with gzip.open(export, "wt", encoding="utf8") as f:
            json.dump(
                {"source": src, "rows": rows},
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )


def named_record(item, labels, aliases):
    languages = set(labels) | set(aliases)
    # Preserve the English display preference while indexing every source language and script.
    order = dict.fromkeys(["en","mul","de","fr","es","it","pt","nl","no","ar","zh","ja",*sorted(languages)])
    forms = [labels[lang] for lang in order if lang in labels]
    forms.extend(value for lang in order for value in aliases.get(lang,()))
    names = dict.fromkeys(value.strip() for value in forms
                          if isinstance(value,str) and 0<len(value.strip())<=140 and value.strip()!=item)
    if names:
        primary = next(iter(names))
        return item,primary,sorted(names.keys()-{primary}),len(languages-{"mul"})


def named_items(page):
    ids = [r["item"]["value"].rsplit("/", 1)[-1] for r in page]
    for _, record in entity_name_records(ids):
        if record:
            yield record


def language_map(entity, field, required):
    value = entity.get(field, None if required else {})
    if value == []:  # MediaWiki formatversion 1 can encode an empty map as an array.
        return {}
    if not isinstance(value,dict):
        raise ValueError("Wikidata omitted the requested names.")
    return value


def name_text(value, lang):
    if (not isinstance(value,dict) or value.get("language",lang)!=lang
            or not isinstance(value.get("value"),str)):
        raise ValueError("Wikidata returned a malformed name.")
    return value["value"]


def entity_name_records(ids, *, allow_missing=False):
    if not all(re.fullmatch(r"Q[1-9]\d*", item) for item in ids):
        raise ValueError("Unexpected Wikidata entity identifier.")
    def batches(batch):
        args = urllib.parse.urlencode(
            {
                "action": "wbgetentities",
                "ids": "|".join(batch),
                "props": "labels|aliases",
                "redirects": "no",
                "format": "json",
            }
        )
        try:
            result = json.loads(fetch("https://www.wikidata.org/w/api.php?" + args))
            entities = result.get("entities") if isinstance(result,dict) else None
        except DownloadTooLarge:
            if len(batch)==1:
                raise
            middle = len(batch)//2
            yield from batches(batch[:middle])
            yield from batches(batch[middle:])
            return
        if not isinstance(entities,dict) or set(entities) != set(batch) or "warnings" in result or "error" in result:
            raise ValueError("Wikidata returned an incomplete name batch.")
        for item in batch:
            entity = entities[item]
            if not isinstance(entity,dict):
                raise ValueError("Wikidata returned a malformed entity.")
            if allow_missing and entity.get("id")==item and "missing" in entity:
                yield item, None
                continue
            if entity.get("id") != item or "missing" in entity:
                raise ValueError("The Wikidata names no longer belong to the requested entity.")
            labels = {lang:name_text(v,lang) for lang,v in language_map(entity,"labels",allow_missing).items()}
            aliases = {}
            for lang,values in language_map(entity,"aliases",allow_missing).items():
                if not isinstance(values,list):
                    raise ValueError("Wikidata returned malformed aliases.")
                aliases[lang] = [name_text(v,lang) for v in values]
            yield item, named_record(item,labels,aliases)
    for start in range(0, len(ids), 50):
        yield from batches(ids[start:start+50])


def wikidata(db, term, qid=None, export=None, relation=None, target=None, kinds=True, allow_empty=False):
    known = db.execute(
        "SELECT id FROM source WHERE id LIKE ? LIMIT 1", ("wd:%:" + term,)
    ).fetchone()
    qid = qid or (known[0].split(":")[1] if known else discover_class(term))
    if not re.fullmatch(r"Q[1-9]\d*", qid):
        raise ValueError("Invalid Wikidata class identifier.")
    # Direct instances and subclasses are candidates; web result snippets never establish membership.
    if relation:
        return relation_source(db, term, qid, relation, target, export)
    from world_facts import bindings as query_page, membership_scope, verified_member_pages

    try:
        from wordfreq import zipf_frequency
    except ImportError:
        zipf_frequency = None
    import hashlib

    scope = membership_scope(qid, kinds)
    fingerprint = hashlib.sha256()
    count = 0
    seen_members = False
    with tempfile.TemporaryFile(mode="w+t", encoding="utf8") as staged:
        for page in verified_member_pages(query_page, scope, 500):
            for row in page:
                item = row["item"]["value"].rsplit("/", 1)[-1]
                seen_members |= item != qid
                fingerprint.update(b"\x00" + item.encode("ascii") + b"\n")
            for eid, name, aliases, langs in named_items(page):
                if eid == qid:
                    continue
                record = {
                    "id": "wd:" + eid,
                    "name": name,
                    "aliases": aliases,
                    "types": [term],
                    "url": "https://www.wikidata.org/wiki/" + eid,
                }
                if zipf_frequency:
                    freq = zipf_frequency(name, "en")
                    record.update(
                        recognition=[freq, langs], depth=familiarity(freq, langs, True)
                    )
                line = (
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )
                staged.write(line)
                fingerprint.update(line.encode())
                count += 1
        if not count and allow_empty and not seen_members:
            # An empty query can represent a withdrawn scope. Cross-check that
            # the entity service can account for this class before replacing it.
            args = urllib.parse.urlencode({"action": "wbgetentities", "ids": qid, "props": "info", "format": "json"})
            info = json.loads(fetch("https://www.wikidata.org/w/api.php?" + args))["entities"][qid]
            if info.get("id") != qid or not ("missing" in info or info.get("lastrevid")):
                raise ValueError("Wikidata could not verify the empty class.")
        elif not count:
            raise ValueError(
                "Wikidata returned no named members. The previous release is retained."
            )
        src = {
            "source": "wd:" + qid + ":" + term,
            "revision": fingerprint.hexdigest(),
            "url": "https://www.wikidata.org/wiki/" + qid,
            "terms": {term: [term]},
            "captured": int(time.time()),
        }
        staged.seek(0)
        ingest(db, src, (json.loads(line) for line in staged))
        with db:
            db.execute(
                "UPDATE source SET checked=unixepoch() WHERE id=?", (src["source"],)
            )
        if export:
            staged.seek(0)
            with gzip.open(export, "wt", encoding="utf8") as file:
                file.write('{"source":' + json.dumps(src) + ',"rows":[')
                for number, line in enumerate(staged):
                    file.write(("," if number else "") + line.rstrip("\n"))
                file.write("]}")


def linked_records(document, mapping):
    """Read expanded JSON-LD identifiers and source-mapped predicates. Context URLs stay offline."""
    nodes = (
        document.get("@graph", [document]) if isinstance(document, dict) else document
    )

    def values(node, predicate):
        items = node.get(predicate, [])
        return items if isinstance(items, list) else [items]

    for node in nodes:
        if "@id" not in node:
            continue
        labels = [
            x.get("@value") if isinstance(x, dict) else x
            for x in values(node, mapping["label"])
        ]
        if not labels:
            continue
        types = mapping.get("types", {})
        record = {
            "id": node["@id"],
            "name": labels[0],
            "aliases": labels[1:],
            "types": [types.get(t, t) for t in values(node, "@type")],
            "edges": [],
            "facts": [],
        }
        for predicate, relation in mapping.get("relations", {}).items():
            record["edges"].extend(
                [relation, x["@id"]]
                for x in values(node, predicate)
                if isinstance(x, dict) and "@id" in x
            )
        for predicate, definition in mapping.get("measures", {}).items():
            for x in values(node, predicate):
                amount = x.get("@value") if isinstance(x, dict) else x
                record["facts"].append(
                    {**definition, "low": float(amount), "high": float(amount)}
                )
        yield record


def mapped_records(rows, mapping):
    """Project native export columns onto stable IDs, memberships and sourced facts."""
    def values(row, column):
        if column not in row:
            raise ValueError(f"The source export is missing the mapped column {column}.")
        items = row[column] if isinstance(row[column], list) else [row[column]]
        return [value for value in items if value is not None and value != ""]

    for row in rows:
        ids, names = values(row, mapping["id"]), values(row, mapping["name"])
        if len(ids) != 1 or len(names) != 1 or isinstance(ids[0], (list, dict, bool)):
            raise ValueError("Each export row must identify one entity and one name.")
        record = {"id": mapping.get("prefix", "") + str(ids[0]), "name": names[0],
                  "types": [mapping["class"]], "aliases": [], "edges": [], "facts": []}
        for col in mapping.get("aliases", []):
            record["aliases"].extend(values(row, col))
        for col, relation in mapping.get("relations", {}).items():
            for target in values(row, col):
                if not isinstance(target, (str, int)) or isinstance(target, bool):
                    raise ValueError(f"Invalid relationship target in {col}.")
                record["edges"].append((relation, str(target)))
        for col, definition in mapping.get("measures", {}).items():
            lows = values(row, col)
            high_column = definition.get("highColumn")
            highs = values(row, high_column) if high_column else lows
            if len(lows) != len(highs):
                raise ValueError(f"Measurement bounds have different counts in {col} and {high_column}.")
            fact = {key: value for key, value in definition.items() if key != "highColumn"}
            record["facts"].extend({**fact, "low": low, "high": high} for low, high in zip(lows, highs))
        if "url" in mapping:
            urls = values(row, mapping["url"])
            if len(urls) > 1:
                raise ValueError("A source record can supply one evidence URL.")
            if urls:
                record["url"] = urls[0]
        yield record


def import_file(db, manifest, *, data_path=None, revision=None, definition=None):
    src = dict(definition) if definition is not None else json.loads(manifest.read_text("utf8"))
    if "wikidata" in src:
        return False  # Live scope snapshots are acquired by the catalogue publisher.
    if "competition" in src:
        return competition(db, src["competition"])
    if "download" in src and data_path is None:
        return False  # The independent publisher fetches and validates remote feeds.
    if revision is not None:
        src["revision"] = revision
    if db.execute(
        "SELECT 1 FROM source WHERE id=? AND revision=?",
        (src["source"], src["revision"]),
    ).fetchone():
        return False
    path = data_path if data_path is not None else manifest.parent / src["file"]
    if path.stat().st_size > (
        32_000_000 if path.suffix in (".json", ".jsonld") else 512_000_000
    ):
        raise ValueError("Split this source into bounded JSONL or Parquet releases.")
    mapping = src.get("rowMapping")
    def publish(rows):
        return ingest(db, src, mapped_records(rows, mapping) if mapping is not None else rows)
    if (src.get("rowsPath") or src.get("countPath")) and path.suffix != ".json":
        raise ValueError("A nested rows path or reported count path requires a JSON document.")
    # An operator-provided manifest describes a completed source snapshot.
    if path.suffix == ".parquet":
        import duckdb

        conn = duckdb.connect(config={"memory_limit": "384MB", "threads": "1"})
        try:
            cursor = conn.execute("SELECT * FROM read_parquet(?)", [str(path)])
            keys = [c[0] for c in cursor.description]

            def records():
                while chunk := cursor.fetchmany(1000):
                    yield from (dict(zip(keys, row)) for row in chunk)

            return publish(records())
        finally:
            conn.close()
    with path.open(encoding="utf-8-sig", newline="") as f:
        if path.suffix == ".csv":
            if mapping is not None:
                return publish(csv.DictReader(f))

            def rows():
                for row in csv.DictReader(f):
                    for key in ("types", "aliases", "facts", "edges", "recall"):
                        if key in row:
                            row[key] = json.loads(row[key] or "[]")
                    if "depth" in row:
                        row["depth"] = int(row["depth"] or 2)
                    yield row

            return publish(rows())
        if path.suffix == ".jsonld":
            return ingest(db, src, linked_records(json.load(f), src["mapping"]))
        if path.suffix == ".jsonl":
            return publish(json.loads(line) for line in f if line.strip())
        document = json.load(f)
        if "countPath" in src:
            if not mapping or not src["countPath"]:
                raise ValueError("A source-reported count needs a mapped class and a field path.")
            count = document
            for field in src["countPath"]:
                count = count[field]
            src["census"] = {**src.get("census", {}), mapping["class"]: count}
        for field in src.get("rowsPath", []):
            document = document[field]
        if not isinstance(document, list):
            raise ValueError("The source export needs a list of records or a rowsPath pointing to one.")
        return publish(document)
