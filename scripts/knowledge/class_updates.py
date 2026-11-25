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
from sources import fetch, named_items, named_record
from world_facts import bindings, member_count, member_page, roster_hash

KEY = re.compile(r"(type|kind):(Q[1-9]\d{0,11})")
SOURCE_KEY = re.compile(r"(type|kind|form):(Q[1-9]\d{0,11})")
CAPTURE_FORMAT = 5
SCHEMA = """
CREATE TABLE current_class(rule TEXT PRIMARY KEY,revision TEXT,captured INTEGER,n INTEGER,scanned INTEGER);
CREATE TABLE current_member(rule TEXT,id INTEGER,name TEXT,aliases TEXT,PRIMARY KEY(rule,id)) WITHOUT ROWID;
CREATE TABLE current_unnamed(rule TEXT,id INTEGER,PRIMARY KEY(rule,id)) WITHOUT ROWID;
"""


def condition(key, item="?item"):
    kind, cls = SOURCE_KEY.fullmatch(key).groups()
    if kind == "form":
        from work_forms import WORK
        return (f"{item} wdt:P7937 wd:{cls} . {item} wdt:P31/wdt:P279* wd:Q{WORK} . "
                f"wd:{cls} wdt:P279+ wd:Q{WORK} .")
    if kind == "kind":
        return f"{item} wdt:P279+ wd:{cls} . FILTER({item} != wd:{cls})"
    return f"{item} (wdt:P31|wdt:P106)/wdt:P279* wd:{cls} ."


def evidence(key, qid):
    return "https://query.wikidata.org/#" + urllib.parse.quote(
        "ASK { " + condition(key, "wd:" + qid) + " }", safe=""
    )


def registered(storage, publication=None):
    from watched_rules import requests_folder

    folder = storage / "knowledge-sources" / "world-classes"
    types = []
    for registration in requests_folder(storage).glob("*.rule"):
        if not re.fullmatch(r"[a-f0-9]{64}", registration.stem) or registration.stat().st_size > 40:
            continue
        try:
            key = registration.read_text("ascii").strip()
        except UnicodeError:
            continue
        if KEY.fullmatch(key) and registration.stem == digest(key):
            yield key, folder / (registration.stem + ".sqlite")
            if key.startswith("type:"):
                types.append(int(key[6:]))
    from work_forms import prior_work_classes
    for form in sorted(prior_work_classes(publication, types)):
        key = f"form:Q{form}"
        yield key, folder / (digest(key) + ".sqlite")


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
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version >= 4:
        scans = db.execute("SELECT scanned,checksum FROM scan WHERE finished=1 ORDER BY pass").fetchall()
        if len(scans) != 2 or scans[0] != scans[1] or len(scans[0][1]) != 32:
            raise ValueError("The class capture has no matching membership verification.")
        checksum.update(b"\x00roster-v1\x00" + str(scans[0][0]).encode("ascii") + scans[0][1])
    if version >= 5:
        for item, in db.execute("SELECT id FROM unnamed ORDER BY id"):
            checksum.update(b"\x00unnamed:" + str(item).encode("ascii"))
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


def restart_scan(db):
    db.execute("DELETE FROM member")
    db.execute("DELETE FROM unnamed")
    db.execute("DELETE FROM scan")
    db.executemany("INSERT INTO scan VALUES(?,'',0,?,0)", [(n, bytes(32)) for n in (1, 2)])


def advance(key, file, *, pages=2, page_size=200):
    """Persist two ordered scans. Only the first one downloads names."""
    partial = file.with_suffix(".partial")
    file.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(partial)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS scan(pass INTEGER PRIMARY KEY,cursor TEXT,
                scanned INTEGER,checksum BLOB,finished INTEGER);
            CREATE TABLE IF NOT EXISTS member(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS unnamed(id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS release(rule TEXT PRIMARY KEY,revision TEXT,captured INTEGER,n INTEGER);""")
        with db:
            if db.execute("PRAGMA user_version").fetchone()[0] != CAPTURE_FORMAT:
                db.execute("DROP TABLE IF EXISTS progress")
                db.execute("DELETE FROM release")
                restart_scan(db)
                db.execute(f"PRAGMA user_version={CAPTURE_FORMAT}")
        for _ in range(pages):
            state = db.execute("SELECT pass,cursor,scanned,checksum FROM scan WHERE finished=0 ORDER BY pass LIMIT 1").fetchone()
            if not state:
                break
            scan, cursor, scanned, checksum = state
            scope = condition(key)
            page, urls = member_page(bindings, scope, cursor, page_size)
            checksum = roster_hash(checksum, urls)
            scanned += len(urls)
            cursor = urls[-1] if urls else cursor
            finished = len(page) < page_size
            if finished:
                first = db.execute("SELECT scanned,checksum FROM scan WHERE pass=1").fetchone()
                if member_count(bindings, scope) != scanned or (scan == 2 and first != (scanned,checksum)):
                    with db:
                        restart_scan(db)
                    raise ValueError("Membership changed during pagination. The publisher will repeat the class check.")
            if scan == 2 and finished and not scanned:
                cls = SOURCE_KEY.fullmatch(key)[2]
                args = urllib.parse.urlencode(
                    {"action": "wbgetentities", "ids": cls, "props": "info", "format": "json"}
                )
                item = json.loads(fetch("https://www.wikidata.org/w/api.php?" + args))["entities"][cls]
                if item.get("id") != cls or not ("missing" in item or item.get("lastrevid")):
                    raise ValueError("The source could not confirm the empty class.")
            names = [(int(qid[1:]), name, json.dumps(sorted(aliases), ensure_ascii=False))
                     for qid, name, aliases, _ in named_items(page)] if scan == 1 else []
            if scan == 1 and finished and scanned and not key.startswith("form:") and not names and not db.execute(
                    "SELECT 1 FROM member LIMIT 1").fetchone():
                with db:
                    restart_scan(db)
                raise ValueError("The source listed class members without usable names.")
            with db:
                db.executemany("INSERT INTO member VALUES(?,?,?)", names)
                if scan == 1:
                    named = {row[0] for row in names}
                    page_ids = {int(url.rsplit("/Q",1)[1]) for url in urls}
                    if not named <= page_ids:
                        raise ValueError("A name belongs to an entity outside the class page.")
                    db.executemany("INSERT INTO unnamed VALUES(?)", ((item,) for item in page_ids - named))
                db.execute("UPDATE scan SET cursor=?,scanned=?,checksum=?,finished=? WHERE pass=?",
                           (cursor,scanned,checksum,finished,scan))
                if scan == 2 and finished:
                    n = db.execute("SELECT count(*) FROM member").fetchone()[0]
                    db.execute(
                        "INSERT INTO release VALUES(?,?,?,?)",
                        (key, fingerprint(db, key), int(time.time()), n),
                    )
        finished = db.execute("SELECT 1 FROM release WHERE rule=?", (key,)).fetchone()
    if finished:
        os.replace(partial, file)
        file.with_suffix(".retry").unlink(missing_ok=True)


def refresh(db, storage, offline, publication=None):
    from world_index import install_lock
    from source_vocabulary import known

    now = time.time()
    editions = []
    attempted = 0
    for key, file in sorted(registered(storage,publication), key=lambda pair: (due(pair[1]), pair[0])):
        cls = int(SOURCE_KEY.fullmatch(key)[2][1:])
        if not known(db, f"Q{cls}"):
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
                if saved.execute("PRAGMA user_version").fetchone()[0] >= 4:
                    scanned = saved.execute("SELECT scanned FROM scan WHERE pass=1").fetchone()[0]
                else:
                    scanned = saved.execute("SELECT scanned FROM progress WHERE rule=?",(key,)).fetchone()[0]
                if scanned<release[3]:
                    raise ValueError("The current Wikidata class has more names than source members.")
                if saved.execute("PRAGMA user_version").fetchone()[0] >= 5:
                    if scanned-release[3] != saved.execute("SELECT count(*) FROM unnamed").fetchone()[0]:
                        raise ValueError("The class capture is missing membership identities.")
                    db.executemany("INSERT INTO current_unnamed VALUES(?,?)",
                                   ((key,i) for i, in saved.execute("SELECT id FROM unnamed")))
                db.execute("INSERT INTO current_class VALUES(?,?,?,?,?)", (*release,scanned))
                db.executemany(
                    "INSERT INTO current_member VALUES(?,?,?,?)",
                    ((key, *row) for row in saved.execute("SELECT id,name,aliases FROM member")),
                )
                editions.append((key, release[1], scanned))
    return sorted(editions)
