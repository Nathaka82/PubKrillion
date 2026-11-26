"""Refresh the Wikidata relationships requested by hosts, outside live gameplay."""

import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from tempfile import NamedTemporaryFile

from graph import digest
from class_updates import page_names as named_items
from world_facts import bindings


def predicates(key):
    if key.startswith("during:"):
        from dated_links import Rule
        rule=Rule.decode(key)
        return [(rule.date.cls,rule.prop,rule.target)] if rule else []
    keys = key[4:].split("+") if key.startswith("and:") else [key]
    if len(keys) not in (1, 2) or keys != sorted(set(keys)):
        return []
    result = []
    for part in keys:
        match = re.fullmatch(r"[wg]-([1-9]\d{0,11})-([1-9]\d{0,11})-([1-9]\d{0,11})", part)
        if not match:
            return []
        if part.startswith("g-"):
            from places import PHRASES
            if int(match[2]) not in PHRASES:
                return []
        result.append(tuple(map(int, match.groups())))
    return result if result[0][0] == result[-1][0] else []


def requests_folder(storage):
    return Path(os.environ.get("KNOWLEDGE_REQUESTS") or storage / "knowledge-requests")


def replace(file, data):
    file.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=file.parent, delete=False) as staged:
        try:
            staged.write(data)
            staged.close()
            os.replace(staged.name, file)
        finally:
            staged.close()
            Path(staged.name).unlink(missing_ok=True)


def follow(storage, key):
    from class_updates import KEY
    if combined := re.fullmatch(r"examples:(Q[1-9]\d{0,11})", key):
        members = follow(storage, "type:" + combined[1])
        kinds = follow(storage, "kind:" + combined[1])
        return members and kinds
    from date_rules import Rule
    if key.startswith(("lookup:", "topic:")):
        from target_lookup import Request
        from topic_sources import Topic
        lookup = key.startswith("lookup:")
        if not (Request if lookup else Topic).decode(key):
            return False
        path = requests_folder(storage) / (digest(key) + (".lookup" if lookup else ".topic"))
        try:
            if not path.is_file():
                replace(path, key.encode("ascii"))
            return True
        except OSError as err:
            print(f"The target request could not be registered: {err}", file=sys.stderr)
            return False
    if key.startswith("mixed:"):
        # Refresh the relationship independently. Its entity IDs are joined to the
        # measurements already present in the next publication.
        parts = json.loads(key[6:])
        if isinstance(parts,list) and len(parts)==2 and isinstance(parts[0],str) and predicates(parts[0]):
            key = parts[0]
    if not predicates(key) and not KEY.fullmatch(key) and not Rule.decode(key):
        return False
    path = requests_folder(storage) / (digest(key) + ".rule")
    try:
        if not path.exists():
            replace(path, key.encode("ascii"))
        return True
    except OSError as err:
        print(f"The publisher request folder could not be updated: {err}", file=sys.stderr)
        return False


def membership_pattern(keys, conditions):
    cls = conditions[0][0]
    from places import expression
    constraints = " ".join(
        f"?item {expression(prop) if k.startswith('g-') else 'wdt:P'+str(prop)} wd:Q{target} ."
        + (f" FILTER(?item != wd:Q{target})" if k.startswith("g-") and prop==131 else "")
        for k,(_,prop,target) in zip(keys,conditions))
    member="(wdt:P31|wdt:P106)" if all(k.startswith("g-") for k in keys) else "wdt:P31"
    return f"{constraints} ?item {member}/wdt:P279* wd:Q{cls} ."


def capture(key):
    if key.startswith("during:"):
        from dated_links import Rule
        from dated_link_updates import capture as capture_dated
        if rule:=Rule.decode(key):
            return capture_dated(rule)
        raise ValueError("Invalid dated relationship.")
    conditions = predicates(key)
    if not conditions:
        raise ValueError("Invalid source identifiers for a watched question.")
    keys=key[4:].split("+") if key.startswith("and:") else [key]
    body=membership_pattern(keys,conditions)
    def pages(checksum):
        after = ""
        while True:
            cursor = f"FILTER(STR(?item)>{json.dumps(after)})" if after else ""
            page = bindings(f"SELECT DISTINCT ?item WHERE {{ {body} {cursor} }} ORDER BY STR(?item) LIMIT 500")
            urls=[row["item"]["value"] for row in page]
            if len(page)>500 or urls!=sorted(set(urls)) or (urls and urls[0]<=after):
                raise ValueError("The source returned an inconsistent page of relationship matches.")
            ids=set()
            for url in urls:
                owner=re.fullmatch(r"https?://www\.wikidata\.org/entity/(Q[1-9]\d*)",url)
                if not owner:
                    raise ValueError("The source returned an invalid relationship member.")
                ids.add(owner[1])
                checksum.update((owner[1]+"\n").encode("ascii"))
            yield page,ids
            if len(page)<500:
                return
            after=urls[-1]

    names, scanned, first = {}, 0, hashlib.sha256()
    for page,ids in pages(first):
        scanned+=len(page)
        for item, name, aliases, _ in named_items(page):
            if item not in ids or item in names:
                raise ValueError("A source label did not match its requested entity.")
            names[item] = [item, name, aliases]
    rows = sorted(names.values())
    if scanned and not rows:
        raise ValueError("Wikidata returned members without usable names.")
    count=bindings(f"SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE {{ {body} }}")
    if len(count)!=1 or int(count[0]["n"]["value"])!=scanned:
        raise ValueError("The source count changed while collecting the relationship. The previous edition is retained.")
    from places import collect_paths
    areas,proofs={},{}
    for k,(_,prop,target) in zip(keys,conditions):
        if k.startswith("g-"):
            proofs[k]=collect_paths([int(row[0][1:]) for row in rows],prop,target,bindings)
        if k.startswith("g-") and str(target) not in areas:
            parents=bindings(f"SELECT DISTINCT ?p WHERE {{ wd:Q{target} wdt:P131 ?p . FILTER(isIRI(?p)) }}")
            areas[str(target)]=sorted({int(qid[1:]) for row in parents
                if re.fullmatch(r"Q[1-9]\d*",qid:=row["p"]["value"].rsplit("/",1)[-1])})
    replay=hashlib.sha256()
    confirmed=sum(len(page) for page,_ in pages(replay))
    if confirmed!=scanned or replay.digest()!=first.digest():
        raise ValueError("The source membership changed while checking the relationship.")
    return {"key":key,"captured":int(time.time()),"revision":digest([key,rows,scanned,areas,proofs]),
            "rows":rows,"scanned":scanned,"areas":areas,"proofs":proofs}


def due_at(cached):
    failed = cached.with_suffix(".attempt")
    return max(cached.stat().st_mtime + 86400 if cached.exists() else 0,
               failed.stat().st_mtime + 900 if failed.exists() else 0)


def refresh_cache(key,cached):
    data=capture(key)
    replace(cached,gzip.compress(json.dumps(data,ensure_ascii=False,separators=(",",":")).encode(),mtime=0))


def rule_keys(storage):
    for path in requests_folder(storage).glob("*.rule"):
        if not re.fullmatch(r"[0-9a-f]{64}", path.stem) or path.stat().st_size > 160:
            continue
        try:
            key = path.read_text("ascii").strip()
        except UnicodeError:
            continue
        if path.stem == digest(key):
            yield key,path.stem


def requests(storage):
    cache = storage / "knowledge-sources" / "world-rules"
    return ((key,cache / (stem + ".json.gz")) for key,stem in rule_keys(storage) if predicates(key))


def discovered_requests(storage, publication, *, include_topics=False, retained_forms=False):
    """Use the published question index as the builder's own subscription list."""
    if publication is None or not publication.is_file():
        return
    cache = storage / "knowledge-sources" / "world-rules"
    with closing(sqlite3.connect(publication.resolve().as_uri() + "?mode=ro", uri=True)) as prior:
        if not prior.execute("SELECT 1 FROM sqlite_master WHERE name='dated_alternative'").fetchone():
            return
        # Keep checking withdrawn captures. A subsequent edition may restore them.
        if retained_forms:
            keys = {row[0] for row in prior.execute("SELECT key FROM live_rule WHERE key GLOB 'w-*-7937-*'")}
        else:
            sql = "SELECT rule FROM dated_alternative UNION SELECT key FROM live_rule WHERE key GLOB 'during:*'"
            if include_topics and prior.execute("SELECT 1 FROM sqlite_master WHERE name='source_topic_rule'").fetchone():
                sql += " UNION SELECT rule FROM source_topic_rule"
            keys = {row[0] for row in prior.execute(sql)}
            if prior.execute("SELECT 1 FROM sqlite_master WHERE name='scope_witness'").fetchone():
                # Retain the source evidence while exploring the next overlap.
                keys.update(key for key, in prior.execute("SELECT key FROM live_rule WHERE key GLOB 'and:*'"))
                if prior.execute("SELECT 1 FROM sqlite_master WHERE name='relationship_proposal'").fetchone():
                    keys.update(key for key, in prior.execute("SELECT rule FROM relationship_proposal"))
                else:
                    from scope_evidence import proposals
                    keys.update(proposals(prior))
            if prior.execute("SELECT 1 FROM sqlite_master WHERE name='work_form_scope'").fetchone():
                from work_forms import subscriptions
                keys.update(subscriptions(prior,(key for key,_ in rule_keys(storage))))
    for key in sorted(keys):
        if predicates(key):
            yield key, cache / (digest(key) + ".json.gz")


def due_requests(storage, publication=None):
    from class_updates import registered,due
    import date_updates
    import target_lookup, topic_sources, source_vocabulary
    return (any(time.time() >= due_at(cached) for _, cached in topic_sources.requests(storage)) or
            source_vocabulary.due(storage, publication) or
            any(time.time() >= due_at(cached) for _, cached in target_lookup.requests(storage)) or
            any(time.time() >= due_at(cached) for _, cached in requests(storage)) or
            any(time.time() >= due_at(cached) for _, cached in discovered_requests(storage, publication, include_topics=True)) or
            any(time.time() >= due(file) for _,file in registered(storage,publication)) or
            any(time.time() >= date_updates.due(file) for _,file in date_updates.registered(storage)))


def collect(db, storage, offline, publication=None):
    from source_vocabulary import known
    now = int(time.time())
    requested = dict(requests(storage))
    discovered = dict(discovered_requests(storage, publication))
    # Prior versions fetched whole forms in a single job. Keep their evidence
    # while the resumable collector builds the new roster.
    retained = dict(discovered_requests(storage, publication, retained_forms=True))
    topics = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='source_topic_rule'").fetchone():
        topics = {key: storage / "knowledge-sources" / "world-rules" / (digest(key) + ".json.gz")
                  for key, in db.execute("SELECT DISTINCT rule FROM source_topic_rule")}
    pending = []
    have_targets = db.execute("SELECT 1 FROM sqlite_master WHERE name='current_target'").fetchone()
    jobs = {}
    for queue,group in (("retained",retained), ("builder",discovered), ("topic",topics), ("host",requested)):
        jobs.update((key,(cached,queue)) for key,cached in group.items())
    for key,(cached,queue) in jobs.items():
        if any(not (known(db, f"Q{cls}") and known(db, f"P{prop}") and
                    (known(db, f"Q{target}") or have_targets and
                     db.execute("SELECT 1 FROM current_target WHERE id=?", (target,)).fetchone()))
               for cls, prop, target in predicates(key)):
            if queue != "retained" and not offline and now >= due_at(cached):
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.with_suffix(".attempt").touch()
            continue
        pending.append((queue, due_at(cached), key, cached))
    versions = []
    remaining = {"host": 12, "builder": 2, "topic": 6, "retained": 0}
    for queue, due, key, cached in sorted(pending):
        if not offline and now >= due and remaining[queue]:
            remaining[queue] -= 1
            try:
                refresh_cache(key,cached)
            except (OSError, ValueError, KeyError) as error:
                cached.parent.mkdir(parents=True, exist_ok=True)
                cached.with_suffix(".attempt").touch()
                print(f"Current source check for {key} deferred: {error}", flush=True)
        if not cached.is_file():
            continue
        blob = cached.read_bytes()
        data = json.loads(gzip.decompress(blob))
        covered=[data["scanned"],data["areas"]] if "scanned" in data else []
        if "proofs" in data:
            covered.append(data["proofs"])
        if key.startswith("during:"):
            covered.extend([data["dates"],data["statements"]])
        if data["key"] != key or digest([key, data["rows"],*covered]) != data["revision"]:
            raise ValueError("A watched rule failed its source fingerprint check.")
        db.execute("INSERT INTO live_rule VALUES(?,?,?,?,?)", (key, data["revision"], data["captured"], len(data["rows"]), blob))
        versions.append((key, data["revision"]))
    return sorted(versions)
