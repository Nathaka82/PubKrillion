"""Discover source relationships from a host's topic, before constructing answer banks."""

import gzip
import json
import re
import time
import unicodedata
from dataclasses import dataclass

from graph import digest, folded_spelling, spelling
from target_lookup import QID, named_candidates, prior_candidates
from world_facts import bindings

SCHEMA = """
CREATE TABLE source_topic(key TEXT PRIMARY KEY,ids TEXT NOT NULL,crowded INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE source_topic_rule(topic TEXT,rule TEXT,position INTEGER,PRIMARY KEY(topic,rule)) WITHOUT ROWID;
"""

# Authorship and artistic/technical credits. The selected question always names the exact credit.
CREDITS = (50, 57, 58, 84, 86, 87, 110, 162, 170, 175, 176, 178, 287, 676, 943)
MAX_NAMES = 6
# Equivalence, disambiguation and Wikimedia maintenance links describe records themselves.
METADATA_PROPERTIES = {301, 360, 387, 460, 910, 1151, 1204, 1423, 1424, 1753, 1889, 2354, 3733, 5008}


def name_phrase(term):
    return (2 <= len(term) <= 120 and len(term.split()) <= 9 and term.isprintable()
            and any(c.isalpha() for c in term)
            and all(c.isalnum() or unicodedata.category(c).startswith("M")
                    or c in " .'-&(),+!·" for c in term)
            and not re.search(r"\b(?:except|excluding|without|whose|named|bigger than|smaller than|"
                              r"before|after|between|released in|published in|directed by|performed by)\b", term))


@dataclass(frozen=True)
class Topic:
    term: str
    cls: int = 0

    @property
    def key(self):
        return "topic:" + json.dumps([self.term, self.cls], ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def decode(cls, key):
        if not isinstance(key, str) or not key.startswith("topic:") or len(key) > 1600:
            return None
        try:
            name, subject = json.loads(key[6:])
        except (ValueError, TypeError):
            return None
        if (not isinstance(name, str) or not name_phrase(name) or spelling(name) != name
                or type(subject) is not int or not 0 <= subject < 10**12):
            return None
        topic = cls(name, subject)
        return topic if topic.key == key else None


def parse(db, text, *, bare):
    from catalogue import COMMAND
    from date_subjects import resolve

    term = spelling(text).removesuffix("?").strip()
    body = COMMAND.sub("", term, count=1)
    if match := re.fullmatch(r"(.+?) by (.+)", body):
        if name_phrase(match[2]):
            classes = resolve(db, folded_spelling(match[1]))
            if len(classes) == 1:
                return Topic(match[2], classes[0])
        return None
    if bare and body == term:
        term = re.sub(r"^(?:an? )?(?:quiz|round|questions?|topic) (?:about|on) ", "", term)
        if name_phrase(term):
            return Topic(term)
    return None


def source_id(row, field, prefix):
    match = re.fullmatch(r"https?://www\.wikidata\.org/entity/" + prefix + r"([1-9]\d{0,11})",
                         row[field]["value"])
    if not match:
        raise ValueError("The topic source returned an invalid identifier.")
    return int(match[1])


def capture(topic, seeds=()):
    forms, crowded = named_candidates(topic.term, seeds)
    crowded = crowded or len(forms) > MAX_NAMES
    proposals, descriptions = [], {}
    # If search was truncated, require a specific identity before proposing any rule.
    if forms and not crowded:
        if len(forms) > 1:
            values = " ".join(f"wd:Q{i}" for i in forms)
            hints = bindings(f"""SELECT ?target ?description WHERE {{ VALUES ?target {{ {values} }}
                ?target <http://schema.org/description> ?description . FILTER(LANG(?description)="en") }}""")
            for row in hints:
                target, hint = source_id(row, "target", "Q"), row["description"]["value"]
                if target not in forms:
                    raise ValueError("The topic description belongs to a different entity.")
                if hint.isprintable() and len(hint) <= 240:
                    descriptions[str(target)] = hint
        for target in sorted(forms):
            if topic.cls:
                values = " ".join(f"wd:P{p}" for p in CREDITS)
                scope = (f"VALUES ?property {{ {values} }} ?item wdt:P31/wdt:P279* wd:Q{topic.cls} . "
                         f"BIND(wd:Q{topic.cls} AS ?class)")
            else:
                scope = "?item wdt:P31 ?class ."
            def counts(scope):
                return bindings(f"""SELECT ?property ?class (COUNT(DISTINCT ?item) AS ?n) WHERE {{
                ?item ?edge wd:Q{target} . ?property wikibase:directClaim ?edge;
                wikibase:propertyType wikibase:WikibaseItem . {scope}
                FILTER(?property NOT IN (wd:P31,wd:P106,wd:P279))
                }} GROUP BY ?property ?class HAVING(COUNT(DISTINCT ?item)>=2)
                ORDER BY DESC(?n) ?property ?class LIMIT 80""")
            rows = counts(scope)
            alternative = topic.cls and not rows
            if alternative:
                rows = counts(f"VALUES ?property {{ {values} }} ?item wdt:P31 ?class .")
            for row in rows:
                prop, cls, n = source_id(row, "property", "P"), source_id(row, "class", "Q"), int(row["n"]["value"])
                if n < 2 or prop in (31, 106, 279) or topic.cls and (prop not in CREDITS or not alternative and cls != topic.cls):
                    raise ValueError("The source returned a relationship outside the topic query.")
                proposals.append([cls, prop, target, n])
    rows = [] if crowded else [forms[i] for i in sorted(forms)]
    identity = [topic.key, rows, crowded, proposals, descriptions]
    return dict(key=topic.key, rows=rows, crowded=crowded, proposals=proposals, descriptions=descriptions,
                revision=digest(identity), checked=int(time.time()))


def requests(storage):
    from watched_rules import requests_folder
    for file in requests_folder(storage).glob("*.topic"):
        if file.stat().st_size > 1600 or not re.fullmatch(r"[0-9a-f]{64}", file.stem):
            continue
        try:
            key = file.read_text("ascii")
        except UnicodeError:
            continue
        if file.stem == digest(key) and (topic := Topic.decode(key)):
            yield topic, storage / "knowledge-sources" / "topics" / (file.stem + ".v2.json.gz")


def pick(db, proposals):
    from catalogue import RELATION_PHRASES
    choices = []
    for cls, prop, target, n in proposals:
        if prop in METADATA_PROPERTIES:
            continue
        if not db.execute("""SELECT 1 FROM world.entity c,world.property p WHERE c.id=? AND p.id=?
                AND EXISTS(SELECT 1 FROM world.triple WHERE p=31 AND o=?)""", (cls, prop, cls)).fetchone():
            continue
        choices.append((prop not in RELATION_PHRASES and prop not in CREDITS,
                        not 5 <= n <= 250, n > 250, -min(n, 250), target, prop, cls))
    selected, properties, entities = [], set(), set()
    # Give each identity a slot, then each distinct property before repeating a credit.
    for pass_no in range(3):
        for *_, target, prop, cls in sorted(choices):
            key = f"w-{cls}-{prop}-{target}"
            if key in selected or pass_no == 0 and target in entities or pass_no == 1 and prop in properties:
                continue
            selected.append(key)
            entities.add(target)
            properties.add(prop)
            if len(selected) == MAX_NAMES:
                return selected
    return selected


def collect(db, storage, offline, previous=None):
    from watched_rules import due_at, replace
    captures = []
    budget = 3
    for due, key, topic, file in sorted((due_at(f), t.key, t, f) for t, f in requests(storage)):
        if not offline and due <= time.time() and budget:
            budget -= 1
            try:
                data = capture(topic, prior_candidates(previous, topic.term))
                replace(file, gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0))
            except (OSError, KeyError, ValueError) as error:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".attempt").touch()
                print(f"Topic discovery deferred: {error}", flush=True)
        if not file.is_file():
            continue
        data = json.loads(gzip.decompress(file.read_bytes()))
        identity = [data[k] for k in ("key", "rows", "crowded", "proposals", "descriptions")]
        if data["key"] != key or digest(identity) != data["revision"]:
            raise ValueError("The source topic failed its fingerprint check.")
        db.execute("INSERT INTO source_topic VALUES(?,?,?)",
                   (key, json.dumps([i for i, _, _ in data["rows"]]), data["crowded"]))
        for pos, rule in enumerate(pick(db, data["proposals"])):
            db.execute("INSERT INTO source_topic_rule VALUES(?,?,?)", (key, rule, pos))
        captures.append(data)
    return captures


def published_credits(db, topic):
    """Use a checked name and already playable rules when the host abbreviates a credit."""
    if not topic.cls:
        return [], []
    identity = db.execute("SELECT ids,crowded FROM source_topic WHERE key=?", (Topic(topic.term).key,)).fetchone()
    if identity:
        if identity[1]:
            return [], []
        ids = json.loads(identity[0])
    elif qid := QID.fullmatch(topic.term):
        ids = [int(qid[1])]
    else:
        # A folded alias alone cannot identify names with significant punctuation.
        ids = [i for i, label in db.execute("""SELECT t.id,t.name FROM current_target_name n
               JOIN current_target t ON t.id=n.target WHERE n.term=? AND n.preferred=1""",
               (folded_spelling(topic.term),)) if spelling(label) == topic.term]
    if not 1 <= len(ids) <= MAX_NAMES:
        return [], []
    keys = [f"w-{topic.cls}-{prop}-{target}" for target in ids for prop in CREDITS]
    matches = db.execute("""SELECT q.rule,l.n,q.prompt FROM json_each(?) wanted
        CROSS JOIN current_question q ON q.rule=wanted.value CROSS JOIN live_rule l ON l.key=q.rule
        WHERE l.n>=2 ORDER BY l.n DESC,q.prompt,q.rule LIMIT ?""", (json.dumps(keys), MAX_NAMES)).fetchall()
    return ids, matches


def choices(db, topic, edition):
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    row = db.execute("SELECT ids,crowded FROM source_topic WHERE key=?", (topic.key,)).fetchone()
    if row and row[1]:
        raise RuleWithdrawn("Several entities could match that name. Enter a specific Wikidata Q number.")
    if row:
        ids = json.loads(row[0])
        rows = db.execute("""SELECT t.rule,l.n,q.prompt FROM source_topic_rule t
            LEFT JOIN live_rule l ON l.key=t.rule LEFT JOIN current_question q ON q.rule=t.rule
            WHERE t.topic=? ORDER BY t.position""", (topic.key,)).fetchall()
    else:
        ids, rows = published_credits(db, topic)
        if not rows:
            raise UnverifiedRule(topic.key, "This edition has not searched for question subjects and credits for this topic.")
    cards = [dict(key=key, prompt=prompt, edition=edition, records=n, complete=False)
             for key, n, prompt in rows if prompt and n is not None and n >= 2]
    note = "Choose a question to play with its documented answers."
    if topic.cls and any(int(card["key"].split("-")[1]) != topic.cls for card in cards):
        exact = [card for card in cards if int(card["key"].split("-")[1]) == topic.cls]
        if exact:
            cards = exact
        else:
            note = ("The source records these credits under different subjects. "
                    "Select a revised question to use the subject shown.")
    if len(ids) > 1:
        details = []
        for i in ids:
            hint = db.execute("SELECT description FROM target_description WHERE id=?", (i,)).fetchone()
            details.append(f"Q{i} ({hint[0]})" if hint else f"Q{i}")
        note = "Several entities share this name. " + ", ".join(details) + ". " + note
    if cards:
        raise DateChoices(cards, note)
    pending = [key for key, n, _ in rows if n is None]
    if pending:
        raise UnverifiedRule(topic.key, "The builder is checking the answer lists for this topic.")
    if not ids:
        raise RuleWithdrawn("The source search found no entity with that name. Try another spelling or its Wikidata Q number.")
    raise RuleWithdrawn("The builder found no playable question with two verified answers for this topic. "
                        + (note if len(ids) > 1 else "Try a broader subject or another topic."))
