"""Keep the wording of a live date question aligned with the class used to verify it."""

import json
import re
import sqlite3
import time
from contextlib import closing

from class_updates import page_names
from graph import digest, folded_spelling
from watched_rules import replace
from world_facts import bindings
from world_links import identifiers


def location(storage):
    return storage / "knowledge-sources" / "date-subjects.json"


def read(storage):
    path = location(storage)
    if not path.exists():
        return {}
    data = json.loads(path.read_bytes())
    if digest(data["subjects"]) != data["revision"]:
        raise ValueError("The date-subject dictionary failed verification.")
    return data["subjects"]


def refresh(storage, classes):
    saved = read(storage)
    now = int(time.time())
    pending = sorted(i for i in classes if str(i) not in saved or saved[str(i)]["checked"] + 7 * 86400 <= now)
    for start in range(0, len(pending), 50):
        batch = pending[start : start + 50]
        terms = " ".join(f"wd:Q{i}" for i in batch)
        result = bindings(f"""PREFIX schema: <http://schema.org/> SELECT ?item ?field ?text WHERE {{
            VALUES ?item {{ {terms} }} VALUES ?field {{ rdfs:label skos:altLabel schema:description }}
            ?item ?field ?text . FILTER(lang(?text)="en") }}""")
        subjects = {str(i): {"name": "", "aliases": [], "description": "", "checked": now} for i in batch}
        for row in result:
            identifier = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d*)", row["item"]["value"])
            if not identifier or identifier[1] not in subjects:
                raise ValueError("The date-subject source returned an unexpected class.")
            item = subjects[identifier[1]]
            field, text = row["field"]["value"], row["text"]["value"]
            if field.endswith("#altLabel"):
                item["aliases"].append(text)
            elif field.endswith("#label"):
                item["name"] = text
            elif field.endswith("/description"):
                item["description"] = text
        missing = [
            {"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}}
            for i, v in subjects.items()
            if not v["name"]
        ]
        for qid, label, aliases, _ in page_names(missing):
            if qid[1:] not in subjects:
                raise ValueError("The source returned a label for a different class.")
            subjects[qid[1:]]["name"] = label
            subjects[qid[1:]]["aliases"].extend(aliases)
        subjects = {i: v for i, v in subjects.items() if v["name"]}
        if not subjects:
            raise ValueError("The source did not supply names for these date-question classes.")
        for item in subjects.values():
            item["aliases"] = sorted(set(item["aliases"]))
        saved.update(subjects)
        replace(
            location(storage),
            json.dumps({"subjects": saved, "revision": digest(saved)}, ensure_ascii=False).encode(),
        )


def install(db, storage, classes, previous=None):
    subjects = read(storage)
    missing = {cls for cls in classes if str(cls) not in subjects}
    if missing and previous and previous.is_file():
        with closing(sqlite3.connect(previous.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as old:
            if old.execute("SELECT 1 FROM sqlite_master WHERE name='date_subject'").fetchone():
                for cls in missing:
                    row = old.execute(
                        "SELECT name,description FROM date_subject WHERE cls=?", (cls,)
                    ).fetchone()
                    if row:
                        aliases = [
                            v[0]
                            for v in old.execute(
                                "SELECT term FROM date_subject_name WHERE cls=? AND preferred=0", (cls,)
                            )
                        ]
                        subjects[str(cls)] = {"name": row[0], "description": row[1], "aliases": aliases}
    used = []
    for cls in sorted(classes):
        item = subjects.get(str(cls))
        if not item:
            continue
        name, description, aliases = item["name"], item["description"], item["aliases"]
        db.execute("INSERT INTO date_subject VALUES(?,?,?)", (cls, name, description))
        db.executemany(
            "INSERT OR IGNORE INTO date_subject_name VALUES(?,?,?)",
            ((folded_spelling(s), cls, int(s == name)) for s in (name, *aliases)),
        )
        used.append([cls, name, description, aliases])
    return digest(used)


def resolve(db, subject, *, literal=None):
    historical = identifiers(db, subject, "class", literal=literal)
    current_schema = db.execute("SELECT 1 FROM sqlite_master WHERE name='date_subject_name'").fetchone()
    if tagged := re.fullmatch(r"(.+) q([1-9]\d{0,11})", subject):
        cls = int(tagged[2])
        known = db.execute(
            "SELECT 1 FROM world_class_name WHERE term=? AND cls=?", (tagged[1], cls)
        ).fetchone()
        if current_schema and not known:
            known = db.execute(
                "SELECT 1 FROM date_subject_name WHERE term=? AND cls=?", (tagged[1], cls)
            ).fetchone()
        return [cls] if known else []
    if not current_schema:
        return historical

    def lookup(term):
        return db.execute(
            "SELECT cls,preferred FROM date_subject_name WHERE term=? ORDER BY preferred DESC", (term,)
        ).fetchall()

    rows = lookup(subject)
    if not rows and subject.endswith("s"):
        rows = lookup(subject[:-1])
    if rows and rows[0][1]:
        current = {r[0] for r in rows if r[1]}
        # Preserve a competing historical canonical class as an explicit ambiguity.
        current.update(
            r[0]
            for r in db.execute(
                """SELECT cls FROM world_class_name WHERE term=? AND preferred=1""", (subject,)
            )
        )
        return sorted(current)
    return sorted(set(historical) | {r[0] for r in rows})


def label(db, cls):
    old = db.execute("SELECT name FROM item WHERE id=?", (cls,)).fetchone()[0]
    current = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='date_subject'").fetchone():
        current = db.execute("SELECT name FROM date_subject WHERE cls=?", (cls,)).fetchone()
    name = current[0] if current else old
    # Retain a useful parenthetical disambiguator when the actual name is the same.
    if current and old.casefold().startswith(name.casefold() + " (") and old.endswith(")"):
        name = old
    if resolve(db, folded_spelling(name)) != [cls]:
        name = f"{name} (Q{cls})"
    return name
