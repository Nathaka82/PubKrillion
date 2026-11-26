"""Capture reusable item-valued statements from a single Wikidata entity revision."""

import gzip
import json
import re
import time
from urllib.parse import urlencode

from graph import digest, folded_spelling
from sources import entity_name_record, entity_name_records, fetch
from watched_rules import replace, rule_keys

KEY = re.compile(r"values:Q([1-9]\d{0,11}):P([1-9]\d{0,6})")
PID = re.compile(r"P([1-9]\d{0,6})")
QID = re.compile(r"Q([1-9]\d{0,11})")
DAY = 86400
SCHEMA = """
CREATE TABLE value_subject(id INTEGER PRIMARY KEY, name TEXT, revision INTEGER, checked INTEGER);
CREATE TABLE value_subject_name(term TEXT,id INTEGER,display TEXT,PRIMARY KEY(term,id,display)) WITHOUT ROWID;
CREATE TABLE value_property(subject INTEGER, prop INTEGER, unresolved INTEGER, named INTEGER,
    PRIMARY KEY(subject,prop)) WITHOUT ROWID;
CREATE TABLE value_claim(subject INTEGER,prop INTEGER,target INTEGER,statement TEXT NOT NULL,
    PRIMARY KEY(subject,prop,target)) WITHOUT ROWID;
CREATE TABLE value_label(id INTEGER PRIMARY KEY,name TEXT,aliases TEXT,checked INTEGER);
"""


def capture(subject):
    qid = f"Q{subject}"
    params = urlencode(dict(action="wbgetentities", ids=qid, props="info|labels|aliases|claims", format="json"))
    response = json.loads(fetch("https://www.wikidata.org/w/api.php?" + params, limit=16_000_000))
    if (not isinstance(response, dict) or not isinstance(response.get("entities"), dict)
            or set(response["entities"]) != {qid} or "warnings" in response or "error" in response):
        raise ValueError("Wikidata returned an incomplete entity response.")
    entity = response["entities"][qid]
    if not isinstance(entity, dict) or entity.get("id") != qid:
        raise ValueError("The returned statements belong to a different entity.")
    checked = int(time.time())
    if "missing" in entity:
        return dict(id=subject, name=None, revision=0, checked=checked, properties={})
    revision = entity.get("lastrevid")
    if type(revision) is not int or revision < 1 or entity.get("type") != "item":
        raise ValueError("The entity has no verifiable item revision.")
    identity = entity_name_record(qid, entity, required=True)
    claims = entity.get("claims")
    if claims == []:
        claims = {}
    if not isinstance(claims, dict):
        raise ValueError("Wikidata omitted the entity's statements.")
    properties = {}
    for prop, statements in claims.items():
        if not PID.fullmatch(prop) or not isinstance(statements, list):
            raise ValueError("Malformed property statements.")
        if any(not isinstance(s, dict) or s.get("rank") not in ("preferred", "normal", "deprecated") for s in statements):
            raise ValueError("Unknown Wikidata statement rank.")
        rank = "preferred" if any(s["rank"] == "preferred" for s in statements) else "normal"
        targets, unresolved = {}, False
        for s in statements:
            if s["rank"] != rank:
                continue
            snak = s["mainsnak"]
            if not isinstance(snak, dict) or snak.get("property") != prop:
                raise ValueError("Statement property mismatch.")
            if snak["datatype"] != "wikibase-item":
                continue
            if snak["snaktype"] in ("somevalue", "novalue"):
                unresolved |= snak["snaktype"] == "somevalue"
                continue
            value = snak["datavalue"]
            target = value["value"]
            if (snak["snaktype"] != "value" or value["type"] != "wikibase-entityid"
                    or target["entity-type"] != "item" or not QID.fullmatch(target["id"])
                    or target.get("numeric-id", int(target["id"][1:])) != int(target["id"][1:])):
                raise ValueError("Invalid item-valued statement.")
            if not re.fullmatch(re.escape(qid) + r"\$[a-zA-Z0-9-]{1,128}", s["id"], re.I):
                raise ValueError("Statement ID does not belong to this subject.")
            guid = s["id"].replace("$", "-")
            targets[target["id"]] = min(targets.get(target["id"], guid), guid)
        if targets or unresolved:
            properties[prop] = [targets, unresolved]
    return dict(id=subject, name=identity[1] if identity else None, aliases=identity[2] if identity else [], revision=revision,
                checked=checked, properties=properties)


def subscriptions(storage):
    grouped = {}
    for key, _ in rule_keys(storage):
        if parsed := KEY.fullmatch(key):
            grouped.setdefault(int(parsed[1]), set()).add("P" + parsed[2])
    return grouped


def path_for(storage, subject):
    return storage / "knowledge-sources" / "entity-values" / f"Q{subject}.json.gz"


def read(path, subject):
    if not path.is_file():
        return None
    saved = json.loads(gzip.decompress(path.read_bytes()))
    data = saved["data"]
    if saved["digest"] != digest(data) or data["id"] != subject:
        raise ValueError("The saved entity statements failed verification.")
    return data


def due(path, data, props):
    retry = path.with_suffix(".retry")
    after = retry.stat().st_mtime + 900 if retry.exists() else 0
    if data is not None and props.issubset(data["named"]):
        after = max(after, data["checked"] + DAY)
    return after


def pending(storage):
    now = time.time()
    for subject, props in subscriptions(storage).items():
        path = path_for(storage, subject)
        if due(path, read(path, subject), props) <= now:
            return True
    return False


def refresh(path, subject, props, saved):
    now = time.time()
    if saved and saved["checked"] + DAY > now:
        data = dict(saved)
    else:
        data = capture(subject)
        data.update(labels={}, named=[])
    ids = {qid for prop in props for qid in data["properties"].get(prop, [{}, False])[0]}
    missing = ids.difference(data["labels"])
    labels = dict(data["labels"])
    if missing:
        for qid, record in entity_name_records(sorted(missing), allow_missing=True):
            if qid not in missing or qid in labels:
                raise ValueError("Unexpected or duplicate target identity.")
            labels[qid] = [record[1], record[2]] if record else None
        if not ids.issubset(labels):
            raise ValueError("Wikidata omitted a requested target identity.")
    data.update(labels=labels, named=sorted(set(data["named"]) | props))
    blob = json.dumps({"data": data, "digest": digest(data)}, ensure_ascii=False, separators=(",", ":")).encode()
    replace(path, gzip.compress(blob, mtime=0))
    return data


def install(db, storage, offline):
    jobs = []
    for subject, props in subscriptions(storage).items():
        path = path_for(storage, subject)
        saved = read(path, subject)
        jobs.append((due(path, saved, props), subject, path, props, saved))
    signatures = []
    updates = 0
    for after, subject, path, props, saved in sorted(jobs):
        if not offline and updates < 4 and after <= time.time():
            updates += 1
            try:
                saved = refresh(path, subject, props, saved)
            except (OSError, ValueError, KeyError, TypeError) as error:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.with_suffix(".retry").touch()
                print(f"Entity statement update for Q{subject} deferred: {error}", flush=True)
        if saved is None:
            continue
        db.execute("INSERT INTO value_subject VALUES(?,?,?,?)",
                   (subject, saved["name"], saved["revision"], saved["checked"]))
        names = set(saved.get("aliases", []))
        if saved["name"]:
            names.add(saved["name"])
        db.executemany("INSERT OR IGNORE INTO value_subject_name VALUES(?,?,?)",
                       ((folded_spelling(name),subject,name) for name in names))
        for prop, (targets, unresolved) in saved["properties"].items():
            pid = int(prop[1:])
            db.execute("INSERT INTO value_property VALUES(?,?,?,?)", (subject, pid, unresolved, prop in saved["named"]))
            db.executemany("INSERT INTO value_claim VALUES(?,?,?,?)",
                           ((subject, pid, int(qid[1:]), guid) for qid, guid in targets.items()))
        for qid, record in saved["labels"].items():
            name, aliases = record if record else (None, [])
            db.execute("""INSERT INTO value_label VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,aliases=excluded.aliases,checked=excluded.checked
                WHERE excluded.checked>value_label.checked""", (int(qid[1:]), name, json.dumps(aliases), saved["checked"]))
        signatures.append((subject, digest(saved)))
    return sorted(signatures)
