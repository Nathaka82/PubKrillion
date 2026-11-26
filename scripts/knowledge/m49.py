"""Publish the UN Statistics Division's multilingual country and area register."""

import gzip
import json
import os
import re
from collections import defaultdict
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile

from graph import digest, ingest
from sources import DATA, fetch

URL = "https://unstats.un.org/unsd/methodology/m49/overview/"
CLDR = "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-localenames-full/main/en/territories.json"
CODES = "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/cldr-core/supplemental/codeMappings.json"
SOURCE = "un-statistics-m49"
SUBJECT = "UN M49 country or area"
SEED = DATA / "un-m49.json.gz"
LANGUAGES = ("EN", "AR", "ES", "FR", "RU", "ZH")
LEVELS = ("Region", "Sub-region", "Intermediate Region")


class DownloadTables(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = {}
        self.language = self.row = self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            table_id = dict(attrs).get("id") or ""
            if self.language:
                raise ValueError("Nested table in the M49 download.")
            if table_id.startswith("downloadTable") and (name := table_id[len("downloadTable"):]) in LANGUAGES:
                if name in self.tables:
                    raise ValueError("Duplicate M49 language table.")
                self.language = name
                self.tables[name] = []
        elif self.language and tag == "tr":
            if self.row is not None:
                raise ValueError("Unfinished row in the M49 download.")
            self.row = []
        elif self.row is not None and tag in ("td", "th"):
            if self.cell is not None:
                raise ValueError("Unfinished cell in the M49 download.")
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if self.cell is not None and tag in ("td", "th"):
            self.row.append(" ".join("".join(self.cell).split()))
            self.cell = None
        elif self.row is not None and tag == "tr" and self.cell is None:
            self.tables[self.language].append(self.row)
            self.row = None
        elif self.language and tag == "table" and self.row is None:
            self.language = None


def download():
    parser = DownloadTables()
    parser.feed(fetch(URL, limit=8_000_000).decode("utf-8-sig"))
    parser.close()
    if parser.language or set(parser.tables) != set(LANGUAGES):
        raise ValueError("The M49 page is missing a complete language table.")
    return parser.tables


def cldr_names():
    cldr = json.loads(fetch(CLDR, limit=500_000))["main"]["en"]
    if cldr["identity"]["language"] != "en":
        raise ValueError("Unexpected CLDR language edition.")
    codes = json.loads(fetch(CODES, limit=500_000))["supplemental"]["codeMappings"]
    territories = cldr["localeDisplayNames"]["territories"]
    if (not isinstance(territories, dict) or not territories or not isinstance(codes, dict) or not codes
            or any(not isinstance(label, str) or not label.strip() for label in territories.values())
            or any(not isinstance(mapping, dict) for mapping in codes.values())):
        raise ValueError("Missing CLDR territory names or identifiers.")
    return {"territories": territories, "codes": codes}


def records(tables):
    if set(tables) != set(LANGUAGES):
        raise ValueError("Missing M49 language edition.")
    areas, regions = {}, defaultdict(dict)
    for language in LANGUAGES:
        header, *rows = tables[language]
        required = {"Country or Area", "M49 Code", "ISO-alpha2 Code", "ISO-alpha3 Code"}
        required.update(level + " " + field for level in LEVELS for field in ("Code", "Name"))
        if not rows or len(set(header)) != len(header) or not required.issubset(header):
            raise ValueError("The M49 register is empty or missing required columns.")
        seen, alpha2, alpha3 = set(), set(), set()
        for values in rows:
            row = dict(zip(header, values, strict=True))
            code, two, three, name = (row[k] for k in ("M49 Code", "ISO-alpha2 Code", "ISO-alpha3 Code", "Country or Area"))
            if (not re.fullmatch(r"[0-9]{3}", code) or not re.fullmatch(r"[A-Z]{2}", two)
                    or not re.fullmatch(r"[A-Z]{3}", three) or not name.strip()
                    or code in seen or two in alpha2 or three in alpha3):
                raise ValueError("Invalid or duplicate area identity in the M49 register.")
            seen.add(code)
            alpha2.add(two)
            alpha3.add(three)
            groups = []
            for level in LEVELS:
                key, label = row[level + " Code"], row[level + " Name"]
                if not (key or label):
                    continue
                if not re.fullmatch(r"[0-9]{3}", key) or not label or regions[key].setdefault(language, label) != label:
                    raise ValueError("Inconsistent M49 region codes or names.")
                groups.append(key)
            if language == "EN":
                areas[code] = (two, three, groups, {})
            if code not in areas or areas[code][:3] != (two, three, groups):
                raise ValueError("M49 language editions disagree on an area's identity or region.")
            areas[code][3][language] = name
        if seen != areas.keys():
            raise ValueError("M49 language editions enumerate different areas.")
    if len({labels["EN"] for labels in regions.values()}) != len(regions):
        raise ValueError("Separate M49 region codes share the same English name.")
    return areas, regions


def load(db, export):
    revision = digest(["m49-v2", export])
    if db.execute("SELECT 1 FROM source WHERE id=? AND revision=?", (SOURCE, revision)).fetchone():
        return
    areas, regions = records(export["tables"])
    display_names = export["territories"]
    if not isinstance(display_names, dict):
        raise ValueError("Missing CLDR territory names.")
    names = {SUBJECT, "UN M49 country or territory"}
    terms = {SUBJECT: names}
    suggested = {SUBJECT: ["country", "territory", "countries", "territories", "country or area", "country or territory"]}
    counts = defaultdict(int)
    rows = []
    parents = set()
    for code, (two, three, groups, labels) in sorted(areas.items()):
        classes = [SUBJECT]
        for key in groups:
            cls = SUBJECT + " in " + regions[key]["EN"]
            parents.add((cls, classes[-1]))
            classes.append(cls)
            if cls not in terms:
                terms[cls] = {f"{subject} in {region}" for subject in names for region in regions[key].values()}
                suggested[cls] = {f"{subject} in {region}" for subject in suggested[SUBJECT]
                                  for region in regions[key].values()}
        for cls in classes:
            counts[cls] += 1
        mapping = export["codes"].get(two, {})
        common = set()
        if (mapping.get("_numeric"), mapping.get("_alpha3")) == (code, three):
            common = {value for tag, value in display_names.items() if tag == two or tag.startswith(two + "-alt-")}
        extra = sorted(common.difference(labels.values()))
        rows.append({"id": "un-m49:" + code, "name": labels["EN"],
                     "aliases": sorted({*labels.values(), *common, two, three}), "types": classes,
                     "nameSources": [{"source": "Unicode CLDR", "url": CLDR, "identity": f"ISO {two} / {three}, M49 {code}",
                                      "names": extra}] if extra else []})
    ingest(db, {"source": SOURCE, "url": URL, "revision": revision, "suggestFor": suggested,
                "terms": terms, "parents": sorted(parents), "census": counts}, rows)


def refresh(db, cache, offline):
    prior = cache if cache.is_file() else SEED
    if offline:
        load(db, json.loads(gzip.decompress(prior.read_bytes())))
        return
    try:
        tables = download()
        try:
            names = cldr_names()
        except (OSError, KeyError, TypeError, ValueError) as error:
            saved = json.loads(gzip.decompress(prior.read_bytes()))
            names = {key: saved[key] for key in ("territories", "codes")}
            print(f"CLDR refresh deferred; matching prior names against the new region codes: {error}", flush=True)
        export = {"tables": tables, **names}
        load(db, export)
    except (OSError, KeyError, TypeError, ValueError) as error:
        load(db, json.loads(gzip.decompress(prior.read_bytes())))
        print(f"UN M49 refresh deferred; using the last validated register: {error}", flush=True)
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=cache.parent, delete=False) as stage:
        try:
            stage.write(gzip.compress(json.dumps(export, ensure_ascii=False, separators=(",", ":")).encode(), mtime=0))
            stage.close()
            os.replace(stage.name, cache)
        finally:
            stage.close()
            Path(stage.name).unlink(missing_ok=True)
