"""Learn class and item-property vocabulary independently of the historical graph."""

import json
import re
import time
from contextlib import closing

from graph import digest, folded_spelling
from sources import named_record
from watched_rules import due_at, replace
from world_facts import bindings

SCHEMA = """
CREATE TABLE source_vocabulary(token TEXT PRIMARY KEY,name TEXT,aliases TEXT,added INTEGER,checked INTEGER) WITHOUT ROWID;
CREATE TABLE topic_dependency(topic TEXT,token TEXT,PRIMARY KEY(topic,token)) WITHOUT ROWID;
"""


def cache_file(storage, token):
    return storage / "knowledge-sources" / "vocabulary" / (token + ".json")


def capture(tokens):
    branches = []
    for prefix in ("Q", "P"):
        terms = [t for t in tokens if t.startswith(prefix)]
        if terms:
            values = " ".join("wd:" + t for t in terms)
            kind = "?term wikibase:propertyType wikibase:WikibaseItem ." if prefix == "P" else ""
            branches.append(f"{{ VALUES ?term {{ {values} }} {kind} }}")
    rows = bindings(
        """SELECT ?term ?field ?text WHERE { { """
        + " UNION ".join(branches)
        + """ } VALUES ?field { rdfs:label skos:altLabel } ?term ?field ?text . }"""
    )
    forms = {t: ({}, {}) for t in tokens}
    for row in rows:
        identifier = re.fullmatch(r"https?://www\.wikidata\.org/entity/([QP][1-9]\d*)", row["term"]["value"])
        if not identifier or identifier[1] not in forms:
            raise ValueError("The source returned a name for an unrequested vocabulary term.")
        labels, aliases = forms[identifier[1]]
        lang = row["text"].get("xml:lang", "mul")
        field, value = row["field"]["value"], row["text"]["value"]
        if field == "http://www.w3.org/2000/01/rdf-schema#label":
            if labels.setdefault(lang, value) != value:
                raise ValueError("The source returned conflicting primary labels for a language.")
        elif field == "http://www.w3.org/2004/02/skos/core#altLabel":
            aliases.setdefault(lang, set()).add(value)
        else:
            raise ValueError("Unexpected vocabulary name property.")
    result = {}
    for token, (labels, aliases) in forms.items():
        record = named_record(token, labels, {lang: sorted(v) for lang, v in aliases.items()})
        result[token] = [record[1], record[2]] if record else [None, []]
    return result


def dependencies(db, requests):
    states = {}
    for key, tokens in requests:
        for token in sorted(tokens):
            if token not in states:
                i = int(token[1:])
                if token[0] == "P":
                    exists = db.execute("SELECT 1 FROM world.property WHERE id=?", (i,)).fetchone()
                    states[token] = None if exists else 1
                else:
                    exists = db.execute("SELECT 1 FROM world.entity WHERE id=?", (i,)).fetchone()
                    is_class = (
                        exists
                        and db.execute(
                            "SELECT 1 FROM world.triple WHERE p=31 AND o=? LIMIT 1", (i,)
                        ).fetchone()
                    )
                    states[token] = None if is_class else int(not exists)
            if states[token] is not None:
                db.execute("INSERT INTO topic_dependency VALUES(?,?)", (key, token))
    return {t: added for t, added in states.items() if added is not None}


def collect(db, storage, requests, offline):
    missing = dependencies(db, requests)
    missing = {
        token: added
        for token, added in missing.items()
        if not db.execute("SELECT 1 FROM source_vocabulary WHERE token=?", (token,)).fetchone()
    }
    files = {t: cache_file(storage, t) for t in missing}
    if not offline:
        now = time.time()
        pending = sorted((due_at(file), t) for t, file in files.items())
        due = [t for when, t in pending if when <= now][:64]
        for start in range(0, len(due), 16):
            batch = due[start : start + 16]
            try:
                records = capture(batch)
                for token in batch:
                    row = [token, *records[token]]
                    replace(
                        files[token],
                        json.dumps({"row": row, "revision": digest(row)}, ensure_ascii=False).encode(),
                    )
            except (OSError, KeyError, ValueError) as error:
                for token in batch:
                    file = files[token]
                    file.parent.mkdir(parents=True, exist_ok=True)
                    file.with_suffix(".attempt").touch()
                print(f"Vocabulary update deferred: {error}", flush=True)
    for token, file in sorted(files.items()):
        label, aliases = None, []
        checked = file.is_file()
        if checked:
            saved = json.loads(file.read_bytes())
            identity, label, aliases = saved["row"]
            if identity != token or digest(saved["row"]) != saved["revision"]:
                raise ValueError("The source vocabulary failed its fingerprint check.")
        db.execute(
            "INSERT INTO source_vocabulary VALUES(?,?,?,?,?)",
            (token, label, json.dumps(aliases, ensure_ascii=False), missing[token], checked),
        )


def due(storage, publication):
    from catalogue import reader

    if not publication or not publication.is_file():
        return False
    with closing(reader(publication)) as db:
        if db.execute("SELECT format FROM publication").fetchone()[0] < 27:
            return False
        from topic_sources import cache_path

        for (key,) in db.execute("SELECT key FROM source_topic WHERE upgrade=1"):
            attempt = cache_path(storage, key).with_suffix(".attempt")
            if not attempt.is_file() or attempt.stat().st_mtime + 900 <= time.time():
                return True
        return any(
            time.time() >= due_at(cache_file(storage, token))
            for (token,) in db.execute("SELECT token FROM source_vocabulary")
        )


def known(db, token, *, direct_class=False):
    table = "world.entity" if token[0] == "Q" else "world.property"
    i = int(token[1:])
    if db.execute(f"SELECT 1 FROM {table} WHERE id=?", (i,)).fetchone() and (
        not direct_class
        or db.execute("SELECT 1 FROM world.triple WHERE p=31 AND o=? LIMIT 1", (i,)).fetchone()
    ):
        return True
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE name='source_vocabulary'").fetchone()
        and db.execute(
            "SELECT 1 FROM source_vocabulary WHERE token=? AND name IS NOT NULL", (token,)
        ).fetchone()
    )


def pending_names(db, key):
    return bool(
        db.execute("SELECT 1 FROM sqlite_master WHERE name='topic_dependency'").fetchone()
        and db.execute(
            """SELECT 1 FROM topic_dependency d JOIN source_vocabulary v USING(token)
                    WHERE d.topic=? AND v.checked=0 LIMIT 1""",
            (key,),
        ).fetchone()
    )


def publish(db):
    for token, name, aliases in db.execute(
        "SELECT token,name,aliases FROM source_vocabulary WHERE name IS NOT NULL ORDER BY token"
    ):
        i = int(token[1:])
        aliases = json.loads(aliases)
        if token[0] == "Q":
            db.execute("INSERT OR IGNORE INTO item VALUES(?,?,?,0)", (i, name, "\t".join(aliases)))
            (historical,) = db.execute("SELECT name FROM item WHERE id=?", (i,)).fetchone()
            for form in {name, historical, *aliases, token}:
                db.execute(
                    """INSERT INTO world_class_name VALUES(?,?,?) ON CONFLICT(term,cls) DO UPDATE
                              SET preferred=max(preferred,excluded.preferred)""",
                    (folded_spelling(form), i, int(form in (name, historical, token))),
                )
        else:
            db.execute("INSERT INTO predicate VALUES(?,?)", (i, name))
            db.executemany(
                "INSERT OR IGNORE INTO world_property_name VALUES(?,?)",
                ((folded_spelling(form), i) for form in [name, *aliases]),
            )
    from topic_sources import CLASSIFICATIONS
    from watched_rules import predicates

    objects = {
        value
        for (key,) in db.execute("SELECT DISTINCT rule FROM source_topic_rule WHERE rule GLOB 'and:*'")
        for _, prop, value in predicates(key)
        if prop in CLASSIFICATIONS
    }
    for value in objects:
        row = db.execute("SELECT name,aliases FROM item WHERE id=?", (value,)).fetchone()
        if row:
            name, aliases = row
            db.executemany(
                """INSERT INTO current_target_name VALUES(?,?,?) ON CONFLICT(term,target)
                           DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
                (
                    (folded_spelling(form), value, int(form == name))
                    for form in {name, *aliases.split("\t")}
                    if form
                ),
            )


def remove_prior_additions(db):
    for (token,) in db.execute("SELECT token FROM prior.source_vocabulary WHERE added AND name IS NOT NULL"):
        table = "item" if token[0] == "Q" else "predicate"
        db.execute(f"DELETE FROM {table} WHERE id=?", (int(token[1:]),))
