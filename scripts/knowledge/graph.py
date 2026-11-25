"""Source-owned assertions. Replacing a source withdraws only its own assertions."""

import hashlib
import json
import math
import re
import sqlite3
import unicodedata
from decimal import Decimal, DecimalException
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def indefinite_article(noun):
    """Choose the English article for a displayed class or reference name."""
    first = noun.strip().split(maxsplit=1)[0] if noun.strip() else ""
    word = unicodedata.normalize("NFKD", first).lower()
    # Initials such as EU, US and MRI have different onsets from ordinary words.
    letters = first.partition("-")[0].replace(".", "")
    if letters.isascii() and letters.isupper() and (
        len(letters) <= 2 or "." in first or re.match(r"^[BCDFGHJKLMNPQRSTVWXZ]{2}", letters)
    ):
        return "an" if letters[0] in "AEFHILMNORSX" else "a"
    if word.startswith(("honest", "honor", "honour", "hour", "heir", "euler")):
        return "an"
    if (word.startswith(("eu", "ewe", "uniqu", "univ", "union", "unit", "unif", "unil", "unicor",
                         "unicy", "use", "user", "usual", "util", "uten", "ubiq", "utop"))
            or re.match(r"^(?:one|once)\b", word)):
        return "a"
    return "an" if word[:1] and word[0] in "aeiou" else "a"


def question_noun(label):
    first = label.split(" ", 1)[0]
    if (first.partition("-")[0].isupper() or first in ("European", "United", "English")
            or any(c.isupper() for c in first[1:])):
        return label
    return label[:1].lower() + label[1:]


def plural_class_head(label):
    phrase = re.split(r"\s+(?:of|in|from|for|with|on|at|by|and)\s", label, maxsplit=1, flags=re.I)[0]
    head = re.search(r"\b([a-z]+s)$", phrase, re.I)
    if head and not head[1].lower().endswith(("ss", "us", "is", "ics", "news", "series", "species")):
        return head


def class_determiner(label):
    # Plural collection titles can have no singular alias in their source.
    head = plural_class_head(label)
    if head and re.match(r"\s+(?:of|in|from)\s", label[head.end():], re.I):
        return "one of the"
    return indefinite_article(label)


UNITS = {
    "mm": ("length", 0.1),
    "cm": ("length", 1),
    "m": ("length", 100),
    "km": ("length", 100000),
    "g": ("mass", 1),
    "kg": ("mass", 1000),
    "in": ("length", 2.54),
    "ft": ("length", 30.48),
    "yd": ("length", 91.44),
    "mi": ("length", 160934.4),
    "oz": ("mass", 28.349523125),
    "lb": ("mass", 453.59237),
    "year": ("year", 1),
}
SCHEMA = """
CREATE TABLE IF NOT EXISTS recipe(source TEXT PRIMARY KEY, args TEXT);
CREATE TABLE IF NOT EXISTS world_watch(id TEXT PRIMARY KEY,kind TEXT,args TEXT,attempted INTEGER);
CREATE TABLE IF NOT EXISTS calendar(source TEXT,event TEXT,year INTEGER,PRIMARY KEY(source,event,year)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS calendar_event ON calendar(event,year);
CREATE TABLE IF NOT EXISTS source(id TEXT PRIMARY KEY, revision TEXT, url TEXT, checked INTEGER, census TEXT);
CREATE TABLE IF NOT EXISTS complete_facet(source TEXT,class TEXT,relation TEXT,prompt TEXT,
    PRIMARY KEY(source,class,relation)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS term(source TEXT, key TEXT, alias TEXT, PRIMARY KEY(source,key,alias)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS term_display(source TEXT,key TEXT,alias TEXT,PRIMARY KEY(source,key,alias)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS display_key ON term_display(key,source);
CREATE INDEX IF NOT EXISTS lookup_term ON term(alias,key);
CREATE TABLE IF NOT EXISTS parent(source TEXT, child TEXT, parent TEXT, PRIMARY KEY(source,child,parent)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS parent_by_parent ON parent(parent,child,source);
CREATE INDEX IF NOT EXISTS parent_by_child ON parent(child,parent,source);
CREATE TABLE IF NOT EXISTS entity(source TEXT, id TEXT, name TEXT, aliases TEXT, depth INTEGER, url TEXT, PRIMARY KEY(source,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entity_id ON entity(id);
CREATE TABLE IF NOT EXISTS entity_name(source TEXT,term TEXT,id TEXT,PRIMARY KEY(source,term,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS entity_name_lookup ON entity_name(term,id);
CREATE TABLE IF NOT EXISTS member(source TEXT, class TEXT, id TEXT, PRIMARY KEY(source,class,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS member_class ON member(class,id);
CREATE INDEX IF NOT EXISTS member_id ON member(id,class);
CREATE TABLE IF NOT EXISTS fact(source TEXT, id TEXT, property TEXT, low REAL, high REAL, basis TEXT, url TEXT);
CREATE INDEX IF NOT EXISTS facts_by_id ON fact(id,property,basis);
CREATE INDEX IF NOT EXISTS facts_by_range ON fact(property,basis,low,high,id);
CREATE TABLE IF NOT EXISTS edge(source TEXT, id TEXT, relation TEXT, target TEXT, PRIMARY KEY(source,id,relation,target)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS reverse_edge ON edge(relation,target,id);
CREATE TABLE IF NOT EXISTS familiarity(source TEXT,id TEXT,frequency REAL,languages INTEGER,PRIMARY KEY(source,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS familiarity_by_id ON familiarity(id);
CREATE TABLE IF NOT EXISTS graph_familiarity(source TEXT,id TEXT,frequency REAL,incoming INTEGER,PRIMARY KEY(source,id)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS graph_familiarity_id ON graph_familiarity(id);
CREATE TABLE IF NOT EXISTS recall(source TEXT, id TEXT, prompt TEXT, n INTEGER, population INTEGER, PRIMARY KEY(source,id,prompt)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS recall_by_entity ON recall(id,prompt);
"""


SIMPLE_WORDS = re.compile(r"[A-Za-z0-9]+(?: [A-Za-z0-9]+)*")


def norm(s):
    if SIMPLE_WORDS.fullmatch(s):
        return s.lower()
    s = unicodedata.normalize("NFKD", s.lower())
    s = "".join(c for c in s if not unicodedata.category(c).startswith("M"))
    s = re.sub(r"(^|\s)\+(?=\d)", r"\1over ", s)
    s = re.sub(r"(^|\s)[−-](?=\d)", r"\1under ", s)
    return " ".join(
        "".join(
            c if c.isalnum() else " "
            for c in s.replace("&", " and ").replace("’", "").replace("'", "")
        ).split()
    )


def spelling(s):
    return " ".join(unicodedata.normalize("NFC", s.lower()).replace("’", "'").replace("‘", "'")
                    .replace("‑", "-").replace("‐", "-").split())


def folded_spelling(s):
    if SIMPLE_WORDS.fullmatch(s):
        return s.lower()
    value = []
    latin = False
    for char in unicodedata.normalize("NFD", spelling(s)):
        if unicodedata.category(char).startswith("M"):
            if not latin:
                value.append(char)
        else:
            latin = unicodedata.name(char, "").startswith("LATIN ")
            value.append(char)
    text = unicodedata.normalize("NFC", "".join(value)).replace("&", " and ").replace("'", "")
    text = re.sub(r"(?<!\d)[.,]|[.,](?!\d)|(?<!\d)-(?!\d)|[()\[\]]", " ", text)
    return " ".join(text.split())


def digest(obj):
    return hashlib.sha256(
        json.dumps(
            obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def open_graph(path, *, uri=False):
    db = sqlite3.connect(path, timeout=5, uri=uri)
    db.row_factory = sqlite3.Row
    db.create_function("normalize", 1, norm, deterministic=True)
    db.create_function("spelling_key", 1, folded_spelling, deterministic=True)
    db.executescript(
        "PRAGMA journal_mode=WAL; PRAGMA journal_size_limit=134217728; PRAGMA cache_size=-32768; PRAGMA temp_store=FILE;"
        + SCHEMA
    )
    db.execute("PRAGMA max_page_count=2000000")  # 8.2 GB with the default 4 KiB pages
    if not db.execute("SELECT 1 FROM entity_name LIMIT 1").fetchone():
        with db:
            db.execute(
                "INSERT OR IGNORE INTO entity_name SELECT source,normalize(name),id FROM entity"
            )
            db.execute(
                "INSERT OR IGNORE INTO entity_name SELECT e.source,normalize(j.value),e.id FROM entity e,json_each(e.aliases) j"
            )
    return db


def web_url(s):
    if not isinstance(s, str) or not re.fullmatch(r"https?://[^\s]+", s):
        raise ValueError("Each source and evidence link needs an HTTP(S) URL.")
    return s


def label(s, size=140):
    if not isinstance(s, str) or not s.strip() or len(s) > size:
        raise ValueError("Invalid knowledge label or identifier.")
    return s.strip()


def ingest(db, src, rows):
    """Publish one full source release atomically. rows can be a streaming iterator."""
    sid, revision, url = (
        label(src["source"]),
        label(src["revision"]),
        web_url(src["url"]),
    )
    old = db.execute("SELECT revision FROM source WHERE id=?", (sid,)).fetchone()
    if old and old[0] == revision:
        if src.get("terms") and not db.execute("SELECT 1 FROM term_display WHERE source=? LIMIT 1", (sid,)).fetchone():
            with db:
                db.executemany("INSERT OR IGNORE INTO term_display VALUES(?,?,?)",
                               ((sid, key, label(alias, 450)) for key, names in src["terms"].items() for alias in {key, *names}))
        return False
    census = src.get("census", {})
    with db:
        for table in (
            "complete_facet",
            "recipe",
            "calendar",
            "entity",
            "entity_name",
            "term",
            "term_display",
            "member",
            "parent",
            "fact",
            "edge",
            "recall",
            "familiarity",
            "graph_familiarity",
        ):
            db.execute(f"DELETE FROM {table} WHERE source=?", (sid,))
        if "recipe" in src:
            db.execute(
                "INSERT INTO recipe VALUES(?,?)", (sid, json.dumps(src["recipe"]))
            )
        for event, years in src.get("calendar", {}).items():
            db.executemany(
                "INSERT INTO calendar VALUES(?,?,?)", ((sid, event, y) for y in years)
            )
        for key, aliases in src.get("terms", {}).items():
            for alias in {key, *aliases}:
                db.execute(
                    "INSERT OR IGNORE INTO term VALUES(?,?,?)",
                    (sid, label(key), norm(label(alias, 450))),
                )
                db.execute("INSERT OR IGNORE INTO term_display VALUES(?,?,?)", (sid, key, label(alias, 450)))
        db.executemany(
            "INSERT INTO parent VALUES(?,?,?)",
            ((sid, label(c), label(p)) for c, p in src.get("parents", [])),
        )
        for r in rows:
            eid, name = label(r["id"], 300), label(r["name"])
            aliases = sorted(
                {label(a) for a in r.get("aliases", []) if a and a != name}
            )
            depth = r.get("depth")
            if depth is not None and (type(depth) is not int or not 1 <= depth <= 5):
                raise ValueError("Invalid rarity prior.")
            link = web_url(r.get("url", url))
            db.execute(
                "INSERT INTO entity VALUES(?,?,?,?,?,?)",
                (
                    sid,
                    eid,
                    name,
                    json.dumps(aliases),
                    min(depth, 4) if depth is not None else None,
                    link,
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO entity_name VALUES(?,?,?)",
                ((sid, norm(a), eid) for a in [name, *aliases]),
            )
            for c in set(r.get("types", [])):
                db.execute("INSERT INTO member VALUES(?,?,?)", (sid, label(c), eid))
            for f in r.get("facts", []):
                prop = label(f["property"])
                try:
                    dimension, scale = UNITS[f["unit"]]
                    lo, hi = (float(Decimal(str(f[k])) * Decimal(str(scale))) for k in ("low", "high"))
                except (KeyError, DecimalException, TypeError, OverflowError) as error:
                    raise ValueError("Invalid measurement or incompatible unit.") from error
                basis = f["basis"]
                if (
                    dimension != prop
                    or not math.isfinite(lo)
                    or not math.isfinite(hi)
                    or lo > hi
                    or (prop in ("length","mass") and lo < 0)
                    or basis not in ("typical", "typical-lower-bound", "record", "record-lower-bound", "maturity-lower-bound")
                ):
                    raise ValueError("Invalid measurement or incompatible unit.")
                db.execute(
                    "INSERT INTO fact VALUES(?,?,?,?,?,?,?)",
                    (sid, eid, prop, lo, hi, basis, web_url(f.get("url", link))),
                )
            for rel, target in r.get("edges", []):
                db.execute(
                    "INSERT OR IGNORE INTO edge VALUES(?,?,?,?)",
                    (sid, eid, label(rel), label(target, 300)),
                )
            if "recognition" in r:
                frequency, languages = r["recognition"]
                if not (
                    math.isfinite(frequency)
                    and 0 <= frequency <= 9
                    and type(languages) is int
                    and languages >= 0
                ):
                    raise ValueError("Invalid familiarity measurements.")
                db.execute(
                    "INSERT INTO familiarity VALUES(?,?,?,?)",
                    (sid, eid, frequency, languages),
                )
            if "graph_recognition" in r:
                frequency, incoming = r["graph_recognition"]
                if not 0 <= frequency <= 9 or type(incoming) is not int or incoming < 0:
                    raise ValueError("Invalid graph familiarity measurements.")
                db.execute(
                    "INSERT INTO graph_familiarity VALUES(?,?,?,?)",
                    (sid, eid, frequency, incoming),
                )
            for sample in r.get("recall", []):
                n, pop = sample["count"], sample["participants"]
                if (
                    type(n) is not int
                    or type(pop) is not int
                    or not 0 <= n <= pop
                    or pop < 200
                ):
                    raise ValueError("Invalid recall evidence.")
                db.execute(
                    "INSERT INTO recall VALUES(?,?,?,?,?)",
                    (sid, eid, label(sample["prompt"], 450), n, pop),
                )
        for event, years in src.get("calendar", {}).items():
            for year in years:
                sides = dict(
                    db.execute(
                        "SELECT relation,COUNT(*) FROM edge WHERE source=? AND target=? GROUP BY relation",
                        (sid, event + "@" + str(year)),
                    )
                )
                if (
                    sides.get("reached") != 2
                    or sides.get("won") != 1
                    or sides.get("was runner-up in") != 1
                ):
                    raise ValueError(
                        "Incomplete final for " + event + " in " + str(year)
                    )
        db.execute("""INSERT OR IGNORE INTO term SELECT DISTINCT source,class,normalize(class)
                      FROM member WHERE source=?""", (sid,))
        db.execute("INSERT OR IGNORE INTO term_display SELECT DISTINCT source,class,class FROM member WHERE source=?", (sid,))
        for c, expected in census.items():
            actual = db.execute(
                "SELECT COUNT(*) FROM member WHERE source=? AND class=?", (sid, c)
            ).fetchone()[0]
            if type(expected) is not int or expected <= 0 or expected != actual:
                raise ValueError("Incomplete enumerated source release for " + c)
        for cls, facets in src.get("completeFacets", {}).items():
            if cls not in census:
                raise ValueError("Complete properties require an enumerated source class.")
            for relation, template in facets.items():
                if not isinstance(template, str) or template.count("{value}") != 1 or len(template)>225:
                    raise ValueError("A property question needs exactly one {value} placeholder.")
                gap = db.execute("""SELECT m.id FROM member m LEFT JOIN edge e
                    ON e.source=m.source AND e.id=m.id AND e.relation=?
                    WHERE m.source=? AND m.class=? GROUP BY m.id HAVING count(e.target)<>1 LIMIT 1""",
                    (relation, sid, cls)).fetchone()
                if gap:
                    raise ValueError(f"{sid}: {gap[0]} needs exactly one source value for {relation}.")
                db.execute("INSERT INTO complete_facet VALUES(?,?,?,?)", (sid, cls, label(relation), template))
        db.execute(
            "INSERT OR REPLACE INTO source VALUES(?,?,?,COALESCE(?,unixepoch()),?)",
            (sid, revision, url, src.get("captured"), json.dumps(census)),
        )
    return True
