"""Build-time source readers. The live quiz reads only the released JSON."""

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from wordfreq import zipf_frequency

ROOT = Path(__file__).resolve().parents[2]
CACHE = Path(
    os.environ.get(
        "KRILL_PACK_CACHE", str(Path(tempfile.gettempdir()) / "krill-pack-sources")
    )
)
CACHE.mkdir(parents=True, exist_ok=True)
sources = {}
packs = {}


def fetch(url):
    path = CACHE / hashlib.sha256(url.encode()).hexdigest()
    if not path.exists():
        for attempt in range(3):
            try:
                request = Request(
                    url,
                    headers={
                        "User-Agent": "KrillClubQuiz/1.0 (educational reference data)",
                        "Accept": "*/*",
                    },
                )
                with urlopen(request, timeout=45) as response:
                    data = response.read()
                tmp = path.with_suffix("." + str(os.getpid()))
                tmp.write_bytes(data)
                tmp.replace(path)
                break
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(attempt + 1)
    data = path.read_bytes()
    sources[url] = hashlib.sha256(data).hexdigest()
    return data


def js(url):
    return json.loads(fetch(url))


def csvrows(url):
    return list(csv.DictReader(io.StringIO(fetch(url).decode("utf-8-sig"))))


def soup(url):
    return BeautifulSoup(fetch(url), "html.parser")


def norm(s):
    s = re.sub(
        r"(?<!\S)([+−-])(?=\d)", lambda m: "over " if m[1] == "+" else "under ", s
    )
    s = unicodedata.normalize(
        "NFKD", s.lower().replace("&", " and ").replace("’", "").replace("'", "")
    )
    return " ".join(
        re.findall(
            r"[^\W_]+", "".join(c for c in s if not unicodedata.combining(c)), re.U
        )
    )


def clean(s):
    s = re.sub(r"\s+", " ", s or "").strip()
    s = re.sub(r"\s+([,;:!?%)])", r"\1", s)
    return re.sub(r"([(])\s+", r"\1", s)


def record(name, *aliases):
    return (name, list(aliases))


def split(s):
    return [x.strip() for x in s.split("|") if x.strip()]


def add(
    pack,
    key,
    title,
    prompt,
    source,
    rows,
    coverage,
    common="",
    rare="",
    matching="names",
    groups=None,
):
    """Merge repeated source identities (one scoring answer per display name).
    Exact canonical names take precedence; ambiguous alternative names are excluded."""
    by_name = {}
    senses = {}
    for row in rows:
        label, aliases, *sense = (row, []) if isinstance(row, str) else row
        label = clean(label)
        if not label:
            raise ValueError(f"{key}: empty source identity")
        namekey = norm(label)
        if not namekey:
            raise ValueError(f"{key}: untypeable answer {label}")
        if sense:
            senses.setdefault(namekey, set()).update(sense)
        name, variants = by_name.setdefault(namekey, (label, set()))
        variants.update(
            clean(a) for a in aliases if a and norm(a) and norm(a) != namekey
        )
    ownership = {}
    for k, (_, variants) in by_name.items():
        for a in variants:
            ownership.setdefault(norm(a), set()).add(k)
    fav, deep = map(lambda s: set(map(norm, split(s))), [common, rare])
    from rarity import COMMON, DEEP

    fav.update(map(norm, split(COMMON.get(pack, ""))))
    deep.update(map(norm, split(DEEP.get(pack, ""))))
    # Familiarity in English is a starting point; editors can override individual tiers.
    ordered = sorted(by_name, key=lambda k: (-zipf_frequency(k, "en"), k))
    tiers = {k: 1 + min(4, 5 * i // len(ordered)) for i, k in enumerate(ordered)}
    for k in fav & tiers.keys():
        tiers[k] = 1
    for k in deep & tiers.keys():
        tiers[k] = 5
    for tier, labels in (groups or {}).items():
        for name in split(labels):
            if norm(name) not in tiers:
                raise ValueError(f"{key}: unknown rarity choice {name}")
            tiers[norm(name)] = tier
    answers = []
    for k, (name, variants) in sorted(by_name.items()):
        variants = sorted(
            a
            for a in variants
            if norm(a) not in by_name and len(ownership[norm(a)]) == 1
        )
        answer = dict(name=name, aliases=variants, depth=tiers[k])
        if k in senses:
            answer["wordnetSenses"] = sorted(senses[k])
        answers.append(answer)
    if not answers:
        raise ValueError("No source records for " + key)
    q = dict(
        id=pack + "-" + key,
        title=title,
        topic=PACKS[pack],
        prompt=prompt,
        source=source,
        coverage=coverage,
        matching="categories" if senses else matching,
        answers=answers,
    )
    packs.setdefault(pack, []).append(q)
    return q


PACKS = {
    "general": "General Knowledge",
    "screen": "Film & TV",
    "music": "Music",
    "geography": "Geography",
    "animals": "Animals",
    "ultimate": "Ultimate Frisbee",
}


def wiki(title):
    from urllib.parse import quote

    return "https://en.wikipedia.org/wiki/" + quote(
        title.replace(" ", "_"), safe="()_,-"
    )


def textcell(cell):
    node = BeautifulSoup(str(cell), "html.parser")
    for s in node.select("sup, .sortkey, .reference, style, .noprint"):
        s.decompose()
    return clean(node.get_text(" ", strip=True))


def grid(table):
    """Expand HTML row/column spans before selecting a named field."""
    pending, result = {}, []
    for tr in table.find_all("tr"):
        if tr.find_parent("table") is not table:
            continue
        row = {}
        for col, (left, value) in list(pending.items()):
            row[col] = value
            if left <= 1:
                del pending[col]
            else:
                pending[col] = (left - 1, value)
        col = 0
        for cell in tr.find_all(["td", "th"], recursive=False):
            val = textcell(cell)
            while col in row:
                col += 1
            for _ in range(int(cell.get("colspan") or 1)):
                row[col] = val
                span = int(cell.get("rowspan") or 1)
                if span > 1:
                    pending[col] = (span - 1, val)
                col += 1
        if row:
            result.append([row.get(i, "") for i in range(max(row) + 1)])
    return result


def tables(title):
    return soup(wiki(title)).select("table.wikitable")
