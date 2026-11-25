"""Collect current class memberships in resumable pages, independently of live rounds."""

import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.parse
from contextlib import closing

from graph import digest
from sources import fetch, named_record
from world_facts import bindings

KEY = re.compile(r"(type|kind):(Q[1-9]\d{0,11})")
CAPTURE_FORMAT = 3
SCHEMA = """
CREATE TABLE current_class(rule TEXT PRIMARY KEY,revision TEXT,captured INTEGER,n INTEGER,scanned INTEGER);
CREATE TABLE current_member(rule TEXT,id INTEGER,name TEXT,aliases TEXT,PRIMARY KEY(rule,id)) WITHOUT ROWID;
"""


def condition(key, item="?item"):
    kind, cls = KEY.fullmatch(key).groups()
    if kind == "kind":
        return f"{item} wdt:P279+ wd:{cls} . FILTER({item} != wd:{cls})"
    return f"{item} (wdt:P31|wdt:P106)/wdt:P279* wd:{cls} ."


def evidence(key, qid):
    return "https://query.wikidata.org/#" + urllib.parse.quote(
        "ASK { " + condition(key, "wd:" + qid) + " }", safe=""
    )


def registered(storage):
    from watched_rules import requests_folder

    folder = storage / "knowledge-sources" / "world-classes"
    for registration in requests_folder(storage).glob("*.rule"):
        if not re.fullmatch(r"[a-f0-9]{64}", registration.stem) or registration.stat().st_size > 40:
            continue
        try:
            key = registration.read_text("ascii").strip()
        except UnicodeError:
            continue
        if KEY.fullmatch(key) and registration.stem == digest(key):
            yield key, folder / (registration.stem + ".sqlite")


def due(file, capture_format=CAPTURE_FORMAT):
    partial, retry = file.with_suffix(".partial"), file.with_suffix(".retry")
    if partial.exists():
        next_page = partial.stat().st_mtime + 60
    elif file.exists():
        with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            current = db.execute("PRAGMA user_version").fetchone()[0]
        next_page = file.stat().st_mtime + 86400 if current >= capture_format else 0
    else:
        next_page = 0
    return max(next_page, retry.stat().st_mtime + 900 if retry.exists() else 0)


def fingerprint(db, key):
    checksum = hashlib.sha256(key.encode("ascii"))
    for row in db.execute("SELECT id,name,aliases FROM member ORDER BY id"):
        checksum.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
    return checksum.hexdigest()


def page_names(page):
    # Collect labels and aliases in the same graph as the class assertions.
    # Small VALUES batches keep multilingual results within the source's transport budget.
    ids = [row["item"]["value"].rsplit("/", 1)[-1] for row in page]
    for start in range(0, len(ids), 40):
        batch = ids[start : start + 40]
        if not all(re.fullmatch(r"Q[1-9]\d*", item) for item in batch):
            raise ValueError("Unexpected Wikidata entity identifier.")
        terms = " ".join("wd:" + item for item in batch)
        labels = {item: {} for item in batch}
        aliases = {item: {} for item in batch}
        rows = bindings(f"""SELECT ?item ?property ?name WHERE {{ VALUES ?item {{ {terms} }}
            VALUES ?property {{ rdfs:label skos:altLabel }} ?item ?property ?name }}""")
        for row in rows:
            item = row["item"]["value"].rsplit("/", 1)[-1]
            prop = row["property"]["value"]
            value = row["name"]["value"]
            lang = row["name"].get("xml:lang", "mul")
            if item not in labels or prop not in (
                "http://www.w3.org/2000/01/rdf-schema#label",
                "http://www.w3.org/2004/02/skos/core#altLabel",
            ):
                raise ValueError("The name query returned an unexpected source record.")
            if prop.endswith("#label"):
                labels[item][lang] = value
            else:
                aliases[item].setdefault(lang, []).append(value)
        for item in batch:
            if record := named_record(
                item, labels[item], {lang: sorted(names) for lang, names in aliases[item].items()}
            ):
                yield record


def advance(key, file, *, pages=2, page_size=200):
    """Commit each whole page. The active snapshot changes only after the last page."""
    partial = file.with_suffix(".partial")
    file.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS progress(rule TEXT PRIMARY KEY,cursor TEXT,scanned INTEGER);
            CREATE TABLE IF NOT EXISTS member(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS release(rule TEXT PRIMARY KEY,revision TEXT,captured INTEGER,n INTEGER);""")
        with db:
            if db.execute("PRAGMA user_version").fetchone()[0] != CAPTURE_FORMAT:
                db.execute("DELETE FROM member")
                db.execute("DELETE FROM progress")
                db.execute("DELETE FROM release")
                db.execute(f"PRAGMA user_version={CAPTURE_FORMAT}")
            db.execute("INSERT OR IGNORE INTO progress VALUES(?,'',0)", (key,))
        cursor, scanned = db.execute("SELECT cursor,scanned FROM progress WHERE rule=?", (key,)).fetchone()
        for _ in range(pages):
            if db.execute("SELECT 1 FROM release WHERE rule=?", (key,)).fetchone():
                break
            after = f"FILTER(STR(?item)>{json.dumps(cursor)})" if cursor else ""
            page = bindings(
                f"SELECT DISTINCT ?item WHERE {{ {condition(key)} {after} }} ORDER BY STR(?item) LIMIT {page_size}"
            )
            urls = [row["item"]["value"] for row in page]
            if (
                len(urls) > page_size
                or urls != sorted(set(urls))
                or (urls and urls[0] <= cursor)
                or any(not re.fullmatch(r"https?://www\.wikidata\.org/entity/Q[1-9]\d*", u) for u in urls)
            ):
                raise ValueError("The Wikidata class page has inconsistent identifiers or ordering.")
            # Fetch all labels before advancing the cursor. A failed label batch leaves this page retryable.
            names = [
                (int(qid[1:]), name, json.dumps(sorted(aliases), ensure_ascii=False))
                for qid, name, aliases, _ in page_names(page)
            ]
            scanned += len(page)
            cursor = urls[-1] if urls else cursor
            if len(page) < page_size:
                counts = bindings(f"SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE {{ {condition(key)} }}")
                if len(counts) != 1 or not str(counts[0]["n"]["value"]).isdecimal():
                    raise ValueError("Wikidata did not supply the class member count.")
                if int(counts[0]["n"]["value"]) != scanned:
                    with db:
                        db.execute("DELETE FROM member")
                        db.execute("UPDATE progress SET cursor='',scanned=0 WHERE rule=?", (key,))
                    raise ValueError(
                        "Membership changed during pagination. The publisher will repeat the class check."
                    )
            if not scanned:
                cls = KEY.fullmatch(key)[2]
                args = urllib.parse.urlencode(
                    {"action": "wbgetentities", "ids": cls, "props": "info", "format": "json"}
                )
                item = json.loads(fetch("https://www.wikidata.org/w/api.php?" + args))["entities"][cls]
                if item.get("id") != cls or not ("missing" in item or item.get("lastrevid")):
                    raise ValueError("The source could not confirm the empty class.")
            with db:
                db.executemany("INSERT INTO member VALUES(?,?,?)", names)
                db.execute("UPDATE progress SET cursor=?,scanned=? WHERE rule=?", (cursor, scanned, key))
                if len(page) < page_size:
                    n = db.execute("SELECT count(*) FROM member").fetchone()[0]
                    if scanned and not n:
                        raise ValueError("The source listed class members without usable names.")
                    db.execute(
                        "INSERT INTO release VALUES(?,?,?,?)",
                        (key, fingerprint(db, key), int(time.time()), n),
                    )
        finished = db.execute("SELECT 1 FROM release WHERE rule=?", (key,)).fetchone()
    if finished:
        os.replace(partial, file)
        file.with_suffix(".retry").unlink(missing_ok=True)


def refresh(db, storage, offline):
    from world_index import install_lock

    now = time.time()
    editions = []
    attempted = 0
    for key, file in sorted(registered(storage), key=lambda pair: (due(pair[1]), pair[0])):
        cls = int(KEY.fullmatch(key)[2][1:])
        if not db.execute("SELECT 1 FROM world.entity WHERE id=?", (cls,)).fetchone():
            if not offline and due(file) <= now:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".retry").touch()
            continue
        if not offline and due(file) <= now and attempted < 4:
            attempted += 1
            try:
                lock_dir = file.with_suffix(".build")
                lock_dir.mkdir(parents=True, exist_ok=True)
                with install_lock(lock_dir):
                    if due(file) <= now:
                        advance(key, file)
            except (OSError, ValueError, KeyError, sqlite3.IntegrityError) as error:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".retry").touch()
                print(f"Class update for {key} deferred: {error}", flush=True)
        if file.is_file():
            with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)) as saved:
                release = saved.execute(
                    "SELECT rule,revision,captured,n FROM release WHERE rule=?", (key,)
                ).fetchone()
                if (
                    not release
                    or release[1] != fingerprint(saved, key)
                    or release[3] != saved.execute("SELECT count(*) FROM member").fetchone()[0]
                ):
                    raise ValueError("The current Wikidata class snapshot failed verification.")
                scanned = saved.execute("SELECT scanned FROM progress WHERE rule=?",(key,)).fetchone()[0]
                if scanned<release[3]:
                    raise ValueError("The current Wikidata class has more names than source members.")
                db.execute("INSERT INTO current_class VALUES(?,?,?,?,?)", (*release,scanned))
                db.executemany(
                    "INSERT INTO current_member VALUES(?,?,?,?)",
                    ((key, *row) for row in saved.execute("SELECT id,name,aliases FROM member")),
                )
                editions.append((key, release[1], scanned))
    return sorted(editions)
