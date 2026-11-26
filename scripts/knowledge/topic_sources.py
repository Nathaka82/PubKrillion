"""Discover source relationships from a host's topic, before constructing answer banks."""

import gzip
import json
import re
import time
import unicodedata
from dataclasses import dataclass
import source_vocabulary

from graph import digest, folded_spelling, spelling
from target_lookup import QID, named_candidates, prior_candidates
from world_facts import bindings

SCHEMA = source_vocabulary.SCHEMA + """
CREATE TABLE source_topic(key TEXT PRIMARY KEY,ids TEXT NOT NULL,crowded INTEGER NOT NULL,upgrade INTEGER NOT NULL) WITHOUT ROWID;
CREATE INDEX topic_upgrade ON source_topic(key) WHERE upgrade=1;
CREATE TABLE discovered_subclass_subject(cls INTEGER PRIMARY KEY);
CREATE TABLE classified_subject(cls INTEGER,topic TEXT,classification_only INTEGER,PRIMARY KEY(cls,topic)) WITHOUT ROWID;
CREATE TABLE source_topic_rule(topic TEXT,rule TEXT,position INTEGER,PRIMARY KEY(topic,rule)) WITHOUT ROWID;
"""

# Authorship and artistic/technical credits. The selected question always names the exact credit.
CREDITS = (50, 57, 58, 84, 86, 87, 110, 162, 170, 175, 176, 178, 272, 287, 676, 943)
DISCOVERY_VERSION = 4
SUBJECT_VERSION = 5
# Some source records describe a work's form or genre as a separate statement.
CLASSIFICATIONS = (7937, 136)
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
    subject_lookup: str = ""

    @property
    def key(self):
        parts = [self.term, self.cls] + ([self.subject_lookup] if self.subject_lookup else [])
        return "topic:" + json.dumps(parts, ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def decode(cls, key):
        if not isinstance(key, str) or not key.startswith("topic:") or len(key) > 1600:
            return None
        try:
            name, subject, *mode = json.loads(key[6:])
        except (ValueError, TypeError):
            return None
        if (not isinstance(name, str) or not name_phrase(name) or spelling(name) != name
                or type(subject) is not int or not 0 <= subject < 10**12
                or mode and (len(mode) != 1 or mode[0] not in ("subject", "type", "kind") or subject or not subject_phrase(name))):
            return None
        topic = cls(name, subject, mode[0] if mode else "")
        return topic if topic.key == key else None


def subject_phrase(name):
    return bool(QID.fullmatch(name)) or (name_phrase(name) and not re.search(
        r"\b(?:who|which|that|whose|by|with|in|from|than|not|published|directed|performed|written|composed|created|produced|q\d+)\b", name))


def noun_forms(literal):
    forms = {literal}
    if literal.endswith("s") and not literal.endswith(("ss", "us", "is", "series", "species")):
        forms.add(literal[:-1])
        if literal.endswith("ies"):
            forms.add(literal[:-3] + "y")
        if literal.endswith(("ches", "shes", "xes", "zes", "sses", "oes")):
            forms.add(literal[:-2])
    return forms


def credit_subjects(db, subject):
    """Keep the source's alternate meanings of a noun until the creator can disambiguate it."""
    from date_subjects import resolve
    literal = spelling(subject)
    term = folded_spelling(literal)
    classes = set(resolve(db, term, literal=literal))
    if not re.fullmatch(r"[\w]+(?:[ -][\w]+)*", literal) or QID.fullmatch(literal):
        return sorted(classes)
    current_names = db.execute("SELECT 1 FROM sqlite_master WHERE name='source_vocabulary'").fetchone()
    for form in sorted(noun_forms(literal)):
        base = folded_spelling(form)
        if form != literal:
            classes.update(resolve(db, base, literal=form))
        # A source label such as "Play (theatre)" has an indexed spelling beginning
        # with "play ". Verify the original punctuation before treating it as a class name.
        source = "SELECT DISTINCT cls FROM world_class_name WHERE term>=? AND term<?"
        names = """SELECT c.cls,i.name,i.aliases FROM candidates c CROSS JOIN item i ON i.id=c.cls"""
        if current_names:
            names += """ UNION ALL SELECT c.cls,v.name,
                (SELECT group_concat(value,char(9)) FROM json_each(v.aliases))
                FROM candidates c CROSS JOIN source_vocabulary v ON v.token='Q'||c.cls WHERE v.name IS NOT NULL"""
        for cls, name, aliases in db.execute(f"WITH candidates AS ({source}) {names}", (base + " ", base + "!")):
            if any(spelling(label[:label.rfind(" (")]) == form for label in (name, *(aliases or "").split("\t"))
                   if label.endswith(")") and " (" in label):
                classes.add(cls)
    return sorted(classes)


def choose_credit_subject(db, name, classes, edition):
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    from watched_rules import predicates
    exact, revised, pending, refresh = {}, {}, set(), set()
    for cls in classes:
        try:
            choices(db, Topic(name, cls), edition)
        except DateChoices as options:
            refresh.update(options.refresh)
            for card in options.cards:
                matching = any(c == cls or (p in CLASSIFICATIONS and target == cls)
                               for c, p, target in predicates(card["key"]))
                (exact if matching else revised)[card["key"]] = card
        except UnverifiedRule as waiting:
            pending.update(waiting.requests)
        except RuleWithdrawn:
            continue
    cards = sorted((exact or revised).values(), key=lambda c: (-c["records"], c["prompt"], c["key"]))
    if cards:
        if len(pending | refresh) <= MAX_NAMES:
            refresh.update(pending)
        message = ("Choose the subject and credit for your question." if exact else
                   "The source records these credits under different subjects. Choose a revised question.")
        raise DateChoices(cards[:MAX_NAMES], message, refresh=sorted(refresh)[:MAX_NAMES])
    if pending:
        if len(pending) > MAX_NAMES:
            examples = [row[0] for row in db.execute("""SELECT i.name FROM json_each(?) c
                CROSS JOIN item i ON i.id=c.value ORDER BY length(i.name),i.name LIMIT 3""", (json.dumps(classes),))]
            hint = " Try a more specific subject such as " + ", ".join(examples) + "." if examples else ""
            raise ValueError("Several source subjects use that name." + hint)
        keys = sorted(pending)
        raise UnverifiedRule(keys[0], "The builder can check which subjects have works credited to this name.", requests=keys)
    raise RuleWithdrawn("The published sources have no playable credit for these subjects and this name.")


def discovered_subjects(db, subject, mode="subject"):
    from catalogue import RuleWithdrawn, UnverifiedRule
    if (not subject_phrase(subject) or not db.execute("SELECT 1 FROM world_class_name LIMIT 1").fetchone()
            or db.execute("SELECT format FROM publication").fetchone()[0] < 27):
        return []
    topic = Topic(subject, subject_lookup=mode)
    row = db.execute("SELECT ids,crowded,upgrade FROM source_topic WHERE key=?", (topic.key,)).fetchone()
    if row is None:
        raise UnverifiedRule(topic.key, "The builder can look up this subject and check how its members are recorded.")
    if row[1]:
        raise RuleWithdrawn("Several subjects share this spelling. Enter a more specific name or its Wikidata Q number.")
    ids = json.loads(row[0])
    if not ids:
        if row[2]:
            raise UnverifiedRule(topic.key, "The builder is rechecking how this subject is recorded.")
        raise RuleWithdrawn("The source found no eligible members for a subject with this spelling.")
    if source_vocabulary.pending_names(db, topic.key):
        raise UnverifiedRule(topic.key, "The builder is checking names for the subjects it found.")
    return ids


def discover_class_question(db, body):
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    from world_classes import BOTH_SUFFIX, NAMED_PREFIX, question, scope_phrase
    mode, subject = scope_phrase(body)
    kinds = mode == "kind"
    ids = discovered_subjects(db, subject, "kind" if kinds else "type")
    cards, revised, pending = [], [], []
    for cls in ids:
        key = ("kind:" if kinds else "type:") + f"Q{cls}"
        try:
            phrase = f"q{cls}"
            if mode in ("kind", "both"):
                phrase = "type of " + phrase + (BOTH_SUFFIX if mode == "both" else "")
            elif mode == "named":
                phrase = NAMED_PREFIX + phrase
            candidate = question(db, phrase, phrase)
        except UnverifiedRule as error:
            pending.extend(error.requests)
        except DateChoices as options:
            revised.extend(options.cards)
            pending.extend(options.refresh)
        except RuleWithdrawn:
            continue
        else:
            if candidate:
                cards.append(candidate)
            elif not db.execute("SELECT 1 FROM current_class WHERE rule=?", (key,)).fetchone():
                pending.append(key)
    if len(cards) == 1 and len(ids) == 1:
        return cards[0]
    if cards:
        raise DateChoices(cards + revised, "Choose the source subject meant by this name.", refresh=pending)
    if revised:
        raise DateChoices(revised, "Select a revised question to play with its documented answers.", refresh=pending)
    if pending:
        raise UnverifiedRule(pending[0], "The builder can verify the members of this subject.", requests=pending)
    if ids:
        raise RuleWithdrawn("The source has no playable answer list for this subject in this edition.")


def bare_choices(db, topic, edition):
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    try:
        choices(db, topic, edition)
    except (RuleWithdrawn, UnverifiedRule, DateChoices) as error:
        original = error
    if not subject_phrase(topic.term):
        raise original
    if isinstance(original, DateChoices) and not db.execute(
            "SELECT 1 FROM source_topic WHERE key=?", (Topic(topic.term, subject_lookup="type").key,)).fetchone():
        raise original
    try:
        card = discover_class_question(db, topic.term)
    except DateChoices as alternatives:
        if isinstance(original, DateChoices):
            combined = {c["key"]: c for c in [*alternatives.cards, *original.cards]}
            raise DateChoices(list(combined.values()), "Choose the subject for this question.",
                              refresh=(*alternatives.refresh, *original.refresh))
        raise
    except RuleWithdrawn:
        raise original
    except UnverifiedRule as waiting:
        if isinstance(original, DateChoices):
            original.refresh = (*original.refresh, *waiting.requests)
            raise original
        pending = list(dict.fromkeys([*getattr(original, "requests", []), *waiting.requests]))
        raise UnverifiedRule(pending[0], "The builder can check this topic for classes and related questions.", requests=pending)
    if not card:
        raise original
    if isinstance(original, DateChoices):
        raise DateChoices([card, *(c for c in original.cards if c["key"] != card["key"])],
                          str(original), refresh=original.refresh)
    return [card]


def parse(db, text, *, bare, edition=None):
    from catalogue import COMMAND

    term = spelling(text).removesuffix("?").strip()
    body = COMMAND.sub("", term, count=1)
    if match := re.fullmatch(r"(.+?) by (.+)", body):
        if name_phrase(match[2]):
            classes = credit_subjects(db, match[1])
            if not classes and edition is not None:
                classes = discovered_subjects(db, match[1])
            if len(classes) == 1:
                return Topic(match[2], classes[0])
            if classes and edition is not None:
                choose_credit_subject(db, match[2], classes, edition)
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


def credited_names(topic, forms):
    """Disambiguate a fully enumerated name search using claims on works credited to each target."""
    names = " ".join(f"wd:Q{i}" for i in sorted(forms))
    credits = " ".join(f"wdt:P{prop}" for prop in CREDITS)
    base = f"VALUES ?target {{ {names} }} VALUES ?credit {{ {credits} }} ?work ?credit ?target ."
    classified = " ".join(f"wdt:P{prop}" for prop in CLASSIFICATIONS)
    scope = (f"{{ ?work wdt:P31/wdt:P279* wd:Q{topic.cls} }} UNION "
             f"{{ VALUES ?form {{ {classified} }} ?work ?form wd:Q{topic.cls} }}")
    # A source can use a different subject class. Offer that precise wording if
    # none of these named entities has two credited works with the requested class or form.
    for condition in (scope, ""):
        rows = bindings(f"""SELECT ?target (COUNT(DISTINCT ?work) AS ?n) WHERE {{ {base} {condition} }}
            GROUP BY ?target HAVING(COUNT(DISTINCT ?work)>=2) ORDER BY ?target""")
        selected = {}
        for row in rows:
            target = source_id(row, "target", "Q")
            if target not in forms or target in selected or int(row["n"]["value"]) < 2:
                raise ValueError("The source returned an unexpected identity for this credit.")
            selected[target] = forms[target]
        if selected:
            return selected
    return {}


def subject_names(topic, seeds):
    """Keep exact identities that classify individual answers or their types."""
    found, subclass_only, classified = {}, set(), set()
    for term in sorted(noun_forms(topic.term)):
        forms, crowded = named_candidates(term, seeds if term == topic.term else ())
        if crowded:
            return {}, True, [], set()
        if not forms:
            continue
        values = " ".join(f"wd:Q{i}" for i in sorted(forms))
        rows = bindings(f"""SELECT ?subject ((EXISTS {{ ?item wdt:P31 ?subject }} ||
            EXISTS {{ ?item wdt:P106 ?subject }}) AS ?members)
            (EXISTS {{ ?child wdt:P279 ?subject FILTER(?child != ?subject) }} AS ?children)
            (EXISTS {{ ?item (wdt:P136|wdt:P7937) ?subject }} AS ?classification)
            WHERE {{ VALUES ?subject {{ {values} }} }}""")
        checked = set()
        for row in rows:
            cls = source_id(row, "subject", "Q")
            if cls not in forms or cls in checked:
                raise ValueError("The source returned an unexpected class identity.")
            checked.add(cls)
            flags = [row[k]["value"] for k in ("members", "children", "classification")]
            if any(v not in ("true", "false", "1", "0") for v in flags):
                raise ValueError("The class check returned an unknown membership result.")
            members, children, classification = [v in ("true", "1") for v in flags]
            if classification and not members and topic.subject_lookup != "kind":
                classified.add(cls)
            if members or children or cls in classified:
                found[cls] = forms[cls]
            if children and not members:
                subclass_only.add(cls)
        if checked != forms.keys():
            raise ValueError("The source did not finish checking the named class candidates.")
    return found, len(found) > MAX_NAMES, sorted(subclass_only), classified


def classification_proposals(target):
    properties = " ".join(f"wd:P{p}" for p in CLASSIFICATIONS)
    rows = bindings(f"""SELECT ?property ?class (COUNT(DISTINCT ?item) AS ?n) WHERE {{
        VALUES ?property {{ {properties} }} ?property wikibase:directClaim ?edge .
        ?item ?edge wd:Q{target}; wdt:P31 ?class .
        }} GROUP BY ?property ?class HAVING(COUNT(DISTINCT ?item)>=2)
        ORDER BY DESC(?n) ?property ?class LIMIT 80""")
    for row in rows:
        cls, prop = source_id(row, "class", "Q"), source_id(row, "property", "P")
        n = int(row["n"]["value"])
        if prop not in CLASSIFICATIONS or n < 2:
            raise ValueError("The source returned an unexpected classification.")
        yield [cls, prop, target, n]


def capture(topic, seeds=(), *, identity=None):
    if topic.subject_lookup:
        forms, crowded, subclass_only, classified = subject_names(topic, seeds)
        named_at = int(time.time())
    elif identity is None:
        forms, crowded = named_candidates(topic.term, seeds)
        named_at = int(time.time())
    else:
        forms = {row[0]: row for row in identity["rows"]}
        crowded, named_at = identity["crowded"], identity.get("namedAt", identity["checked"])
    scoped_identity = bool(topic.cls and not crowded and len(forms) > MAX_NAMES)
    if scoped_identity:
        forms = credited_names(topic, forms)
    crowded = crowded or len(forms) > MAX_NAMES
    proposals, descriptions = [], {}
    if topic.subject_lookup in ("type", "subject") and not crowded:
        for target in sorted(classified):
            proposals.extend(classification_proposals(target))
    # If search was truncated, require a specific identity before proposing any rule.
    if forms and not crowded and not topic.subject_lookup:
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
                classifications = " ".join(f"wd:P{p}" for p in CLASSIFICATIONS)
                classified = bindings(f"""SELECT ?property ?class ?basis (COUNT(DISTINCT ?item) AS ?n) WHERE {{
                    VALUES ?property {{ {values} }} VALUES ?basis {{ {classifications} }}
                    ?property wikibase:directClaim ?edge . ?basis wikibase:directClaim ?form .
                    ?item ?edge wd:Q{target}; ?form wd:Q{topic.cls}; wdt:P31 ?class .
                    }} GROUP BY ?property ?class ?basis HAVING(COUNT(DISTINCT ?item)>=2)
                    ORDER BY DESC(?n) ?property ?class ?basis LIMIT 80""")
                for row in classified:
                    cls, prop = source_id(row, "class", "Q"), source_id(row, "property", "P")
                    basis, n = source_id(row, "basis", "P"), int(row["n"]["value"])
                    if basis not in CLASSIFICATIONS or prop not in CREDITS or n < 2:
                        raise ValueError("The source returned a different credit or classification.")
                    proposals.append([cls, prop, target, n, basis, topic.cls])
                if classified:
                    continue
                rows = counts(f"VALUES ?property {{ {values} }} ?item wdt:P31 ?class .")
            for row in rows:
                prop, cls, n = source_id(row, "property", "P"), source_id(row, "class", "Q"), int(row["n"]["value"])
                if n < 2 or prop in (31, 106, 279) or topic.cls and (prop not in CREDITS or not alternative and cls != topic.cls):
                    raise ValueError("The source returned a relationship outside the topic query.")
                proposals.append([cls, prop, target, n])
    scoped_identity = scoped_identity or topic.subject_lookup
    rows = [] if crowded else [forms[i] for i in sorted(forms)]
    content = [topic.key, rows, crowded, proposals, descriptions, scoped_identity]
    extra = {}
    if topic.subject_lookup:
        extra["subclassOnly"] = [] if crowded else subclass_only
        content.append(extra["subclassOnly"])
        if topic.subject_lookup in ("type", "subject"):
            extra["classificationOnly"] = [] if crowded else sorted(classified.difference(subclass_only))
            content.append(extra["classificationOnly"])
    return dict(key=topic.key, rows=rows, crowded=crowded, proposals=proposals, descriptions=descriptions,
                revision=digest(content), checked=int(time.time()), namedAt=named_at,
                discoveryVersion=SUBJECT_VERSION if topic.subject_lookup else DISCOVERY_VERSION,
                scopedIdentity=scoped_identity, **extra)


def cache_path(storage, key):
    return storage / "knowledge-sources" / "topics" / (digest(key) + ".v2.json.gz")


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
            yield topic, cache_path(storage, key)


def vocabulary_terms(proposals):
    tokens = set()
    for cls, prop, _, _n, *classification in proposals:
        if prop in METADATA_PROPERTIES:
            continue
        tokens.update((f"Q{cls}", f"P{prop}"))
        if classification:
            basis, value = classification
            tokens.update((f"P{basis}", f"Q{value}"))
    return tokens


def read_capture(file, key):
    saved = json.loads(gzip.decompress(file.read_bytes()))
    identity = [saved[k] for k in ("key", "rows", "crowded", "proposals", "descriptions")]
    if "scopedIdentity" in saved:
        identity.append(saved["scopedIdentity"])
    if "subclassOnly" in saved:
        identity.append(saved["subclassOnly"])
    if "classificationOnly" in saved:
        identity.append(saved["classificationOnly"])
    if saved["key"] != key or digest(identity) != saved["revision"]:
        raise ValueError("The source topic failed its fingerprint check.")
    return saved


def pick(db, proposals):
    from catalogue import RELATION_PHRASES
    choices = []
    for cls, prop, target, n, *classification in proposals:
        if prop in METADATA_PROPERTIES:
            continue
        if not (source_vocabulary.known(db, f"Q{cls}", direct_class=True)
                and source_vocabulary.known(db, f"P{prop}")):
            continue
        rule = f"w-{cls}-{prop}-{target}"
        if classification:
            basis, value = classification
            if not source_vocabulary.known(db, f"P{basis}") or not source_vocabulary.known(db, f"Q{value}"):
                continue
            rule = "and:" + "+".join(sorted([rule, f"w-{cls}-{basis}-{value}"]))
        choices.append((prop not in RELATION_PHRASES and prop not in CREDITS,
                        not 5 <= n <= 250, n > 250, -min(n, 250), target, prop, rule))
    selected, properties, entities = [], set(), set()
    # Give each identity a slot, then each distinct property before repeating a credit.
    for pass_no in range(3):
        for *_, target, prop, key in sorted(choices):
            if key in selected or pass_no == 0 and target in entities or pass_no == 1 and prop in properties:
                continue
            selected.append(key)
            entities.add(target)
            properties.add(prop)
            if len(selected) == MAX_NAMES:
                return selected
    return selected


def collect(db, storage, offline, previous=None):
    from watched_rules import due_at, follow, replace
    captures = []
    budget = 3
    pending = []
    identities = {}
    for topic, file in requests(storage):
        saved = read_capture(file, topic.key) if file.is_file() else None
        due = due_at(file)
        if saved:
            named_at = saved.get("namedAt", saved["checked"])
            if (not saved["crowded"] and not saved.get("scopedIdentity")
                    and time.time() - 86400 < named_at and named_at >= identities.get(topic.term, (0,))[0]):
                identities[topic.term] = (named_at, saved)
            # A current name search can also be used to discover newly supported credit rules.
            if needs_upgrade(topic, saved):
                attempt = file.with_suffix(".attempt")
                due = attempt.stat().st_mtime + 900 if attempt.exists() else 0
        pending.append((due, topic.key, topic, file, saved))
    for due, key, topic, file, data in sorted(pending):
        if not offline and due <= time.time() and budget:
            budget -= 1
            try:
                identity = None if topic.subject_lookup else identities.get(topic.term)
                seeds = [] if identity else prior_candidates(previous, topic.term)
                updated = capture(topic, seeds, identity=identity[1] if identity else None)
                replace(file, gzip.compress(json.dumps(updated, ensure_ascii=False).encode(), mtime=0))
                data = updated
                if not data["crowded"] and not data.get("scopedIdentity"):
                    identities[topic.term] = (data["namedAt"], data)
            except (OSError, KeyError, ValueError) as error:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.with_suffix(".attempt").touch()
                print(f"Topic discovery deferred: {error}", flush=True)
        if data is None:
            continue
        db.execute("INSERT INTO source_topic VALUES(?,?,?,?)",
                   (key, json.dumps([i for i, _, _ in data["rows"]]), data["crowded"],
                    needs_upgrade(topic, data)))
        captures.append(data)
        classification_only = set(data.get("classificationOnly", []))
        if topic.subject_lookup in ("type", "subject"):
            db.executemany("INSERT INTO classified_subject VALUES(?,?,?)",
                           ((cls, key, cls in classification_only)
                            for cls in classification_only.union(row[2] for row in data["proposals"])))
        if topic.subject_lookup == "type":
            db.executemany("INSERT OR IGNORE INTO discovered_subclass_subject VALUES(?)",
                           ((cls,) for cls in data.get("subclassOnly", [])))
        if not offline and topic.subject_lookup:
            for cls, name, aliases in data["rows"]:
                token = f"Q{cls}"
                # The name service already supplied these labels while verifying the class identity.
                cache = source_vocabulary.cache_file(storage, token)
                if not cache.exists() or cache.stat().st_mtime < data["namedAt"]:
                    row = [token, name, aliases]
                    replace(cache, json.dumps({"row": row, "revision": digest(row)}, ensure_ascii=False).encode())
                if topic.subject_lookup in ("type", "kind") and cls not in classification_only:
                    follow(storage, f"{topic.subject_lookup}:Q{cls}")
                    if topic.subject_lookup == "type" and cls in data.get("subclassOnly", []):
                        follow(storage, f"kind:Q{cls}")
    source_vocabulary.collect(db, storage, (
        (d["key"], vocabulary_terms(d["proposals"]) |
         ({f"Q{i}" for i, _, _ in d["rows"]} if Topic.decode(d["key"]).subject_lookup else set()))
        for d in captures), offline)
    for data in captures:
        for pos, rule in enumerate(pick(db, data["proposals"])):
            db.execute("INSERT INTO source_topic_rule VALUES(?,?,?)", (data["key"], rule, pos))
    return captures


def needs_upgrade(topic, data):
    required = SUBJECT_VERSION if topic.subject_lookup else DISCOVERY_VERSION if topic.cls else 1
    return data.get("discoveryVersion", 1) < required


def classified_choices(db, cls, edition):
    """Offer only verified work lists for this exact genre or form."""
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='classified_subject'").fetchone():
        return
    cards, pending = {}, set()
    topics = db.execute("SELECT topic,classification_only FROM classified_subject WHERE cls=?", (cls,)).fetchall()
    for topic, _ in topics:
        if source_vocabulary.pending_names(db, topic):
            pending.add(topic)
        rows = db.execute("""SELECT t.rule,l.n,q.prompt FROM source_topic_rule t
            LEFT JOIN live_rule l ON l.key=t.rule LEFT JOIN current_question q ON q.rule=t.rule
            WHERE t.topic=? ORDER BY t.position""", (topic,))
        for rule, count, prompt in rows:
            if rule.rsplit("-", 1)[-1] != str(cls):
                continue
            if count is None:
                pending.add(rule)
            elif count >= 2 and prompt:
                cards[rule] = dict(key=rule, prompt=prompt, records=count, edition=edition, complete=False)
    if cards:
        raise DateChoices(list(cards.values()), "The source classifies these works by genre or form. Choose a question.",
                          refresh=sorted(pending)[:MAX_NAMES])
    if pending:
        keys = sorted(pending)[:MAX_NAMES]
        raise UnverifiedRule(keys[0], "The builder is checking works recorded with this genre or form.", requests=keys)
    if topics and all(only for _, only in topics):
        raise RuleWithdrawn("The source lists no playable group of named works for this genre or form.")


def local_credit_rules(db, topic, target):
    """Find creator credits and creative forms recorded on the same works."""
    from catalogue import current_count, intersection
    from current_questions import relationship_wording

    version = db.execute("SELECT format FROM publication").fetchone()[0]
    credits = json.dumps(CREDITS)
    properties = [prop for prop, in db.execute("""SELECT value FROM json_each(?) p
        WHERE EXISTS(SELECT 1 FROM world_link WHERE prop=p.value AND target=?)""", (credits, target))]
    rules = [f"w-{topic.cls}-{prop}-{target}" for prop in properties]
    # A work may be catalogued as a book, say, with "novel" recorded on a
    # separate genre or form statement. Both edges must belong to that work.
    classified = db.execute("""SELECT m.cls,c.prop,f.prop,count(DISTINCT c.id) AS n
        FROM json_each(?) p CROSS JOIN world_link c ON (c.prop,c.target)=(p.value,?)
        CROSS JOIN world_link f ON f.id=c.id AND f.target=? AND f.prop IN (SELECT value FROM json_each(?))
        CROSS JOIN world_member m ON m.id=c.id
        GROUP BY m.cls,c.prop,f.prop HAVING n>=2 ORDER BY n DESC,m.cls,c.prop,f.prop LIMIT ?""",
        (credits, target, topic.cls, json.dumps(CLASSIFICATIONS), MAX_NAMES))
    for cls, credit, basis, _ in classified:
        rules.append("and:" + "+".join(sorted([f"w-{cls}-{credit}-{target}", f"w-{cls}-{basis}-{topic.cls}"])))
    for key in rules:
        wording = relationship_wording(db, key)
        if not wording or len(wording[0]) > 240:
            continue
        count = 0
        if key.startswith("and:"):
            query, args, _ = intersection(db, key[4:].split("+"))
            count = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        # Recheck membership against the published edition, including any later withdrawals.
        count = current_count(db, key, count, version)
        if count >= 2:
            yield key, count, wording[0]


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
        # Keep every matching identity, including an archived namesake of a recently checked target.
        rows = db.execute("""SELECT DISTINCT n.target,coalesce(t.name,i.name) FROM world_target_name n
            LEFT JOIN current_target t ON t.id=n.target LEFT JOIN item i ON i.id=n.target
            WHERE n.term=? AND n.preferred=1""", (folded_spelling(topic.term),))
        ids = sorted({i for i, name in rows if name and spelling(name) == topic.term})
    if not 1 <= len(ids) <= MAX_NAMES:
        return [], []
    keys = [f"w-{topic.cls}-{prop}-{target}" for target in ids for prop in CREDITS]
    matches = db.execute("""SELECT q.rule,l.n,q.prompt FROM json_each(?) wanted
        CROSS JOIN current_question q ON q.rule=wanted.value CROSS JOIN live_rule l ON l.key=q.rule
        WHERE l.n>=2 ORDER BY l.n DESC,q.prompt,q.rule LIMIT ?""", (json.dumps(keys), MAX_NAMES)).fetchall()
    known = {key: (key, n, prompt) for key, n, prompt in matches}
    for target in ids:
        for key, n, prompt in local_credit_rules(db, topic, target):
            known.setdefault(key, (key, n, prompt))
    return ids, sorted(known.values(), key=lambda row: (-row[1], row[2], row[0]))[:MAX_NAMES]


def credit_card(db, topic, key, prompt, count, edition):
    """Use shorter wording when the publication proves that both rules have the same named members."""
    from watched_rules import predicates
    if key.startswith("and:") and db.execute(
        "SELECT 1 FROM sqlite_master WHERE name='work_form_match'"
    ).fetchone():
        conditions = predicates(key)
        if any(pid == 7937 and value == topic.cls for _, pid, value in conditions):
            for _, pid, target in conditions:
                if pid in CREDITS:
                    simple = f"w-{topic.cls}-{pid}-{target}"
                    # Each matching row below came from this specific compound rule.
                    # Its full roster must be present and the simpler rule may add no other members.
                    equal = db.execute("""SELECT prompt FROM current_question WHERE rule=? AND n=?
                        AND n=(SELECT count(*) FROM work_form_match WHERE rule=? AND origin=?)""",
                        (simple, count, simple, key)).fetchone()
                    if equal:
                        key, prompt = simple, equal[0]
    return dict(key=key, prompt=prompt, records=count, edition=edition, complete=False)


def verified_credit_choices(db, topic, prop, edition):
    """Reuse the source's identified creators when an explicit credit has several namesakes."""
    from watched_rules import predicates
    if prop not in CREDITS:
        return []
    row = db.execute("SELECT ids FROM source_topic WHERE key=? AND crowded=0", (topic.key,)).fetchone()
    if not row:
        return []
    targets = set(json.loads(row[0]))
    result = []
    for key, prompt, count in db.execute("""SELECT q.rule,q.prompt,l.n FROM source_topic_rule t
            JOIN current_question q USING(rule) JOIN live_rule l ON l.key=q.rule
            WHERE t.topic=? AND l.n>0 ORDER BY t.position""", (topic.key,)):
        conditions = predicates(key)
        if (any(pid == prop and target in targets for _, pid, target in conditions)
                and any(cls == topic.cls or (pid in CLASSIFICATIONS and target == topic.cls)
                        for cls, pid, target in conditions)):
            result.append(credit_card(db, topic, key, prompt, count, edition))
    return result


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
    by_rule = {}
    for key, n, prompt in rows:
        if prompt and n is not None and n >= 2:
            card = credit_card(db, topic, key, prompt, n, edition)
            by_rule.setdefault(card["key"], card)
    cards = list(by_rule.values())
    note = "Choose a question to play with its documented answers."
    if topic.cls and cards:
        from watched_rules import predicates
        exact = [card for card in cards if any(cls == topic.cls or (prop in CLASSIFICATIONS and target == topic.cls)
                 for cls,prop,target in predicates(card["key"]))]
        if exact:
            cards = exact
            if any(c["key"].startswith("and:") for c in cards):
                note = "These questions use a recorded form or genre and a separate creator credit."
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
        raise DateChoices(cards, note, refresh=(topic.key,) if row is None else ())
    pending = [key for key, n, _ in rows if n is None]
    if pending:
        raise UnverifiedRule(topic.key, "The builder is checking the answer lists for this topic.")
    if source_vocabulary.pending_names(db,topic.key):
        raise UnverifiedRule(topic.key, "The builder is checking source names for newly recorded classes or properties.")
    if not ids:
        raise RuleWithdrawn("The source search found no entity with that name. Try another spelling or its Wikidata Q number.")
    raise RuleWithdrawn("The builder found no playable question with two verified answers for this topic. "
                        + (note if len(ids) > 1 else "Try a broader subject or another topic."))
