"""Map English demonyms to country identities and propose explicit, evidenced question scopes."""

import gzip
import json
import re
import time
from functools import lru_cache

from graph import digest, folded_spelling, spelling
from sources import DATA
from watched_rules import replace
from world_facts import bindings

SOURCE = "https://www.wikidata.org/wiki/Property:P1549"
SEED = DATA / "wikidata-demonyms.json.gz"
SCOPE = """?country wdt:P297 ?iso; wdt:P1549 ?word .
    FILTER(REGEX(?iso, "^[A-Z]{2}$") && LANG(?word)="en")"""
SCHEMA = """CREATE TABLE country_word(term TEXT,country INTEGER,PRIMARY KEY(term,country)) WITHOUT ROWID;"""
COUNTRY_PROPERTIES = (495, 27, 17)
LANGUAGE_PROPERTIES = (364, 407)


def capture():
    pairs = f"SELECT DISTINCT ?country ?word WHERE {{ {SCOPE} }}"
    rows = set()
    for record in bindings(pairs):
        country = re.fullmatch(r"https?://www\.wikidata\.org/entity/Q([1-9]\d{0,11})", record["country"]["value"])
        word = record["word"]
        if not country or word.get("xml:lang") != "en" or not 1 <= len(word["value"].strip()) <= 140:
            raise ValueError("The demonym source returned an invalid identity or English name.")
        rows.add((word["value"], int(country[1])))
    count = bindings(f"SELECT (COUNT(*) AS ?n) WHERE {{ {{ {pairs} }} }}")
    if not rows or len(count) != 1 or len(rows) != int(count[0]["n"]["value"]):
        raise ValueError("The demonym source count changed during collection.")
    words = sorted(rows)
    data = {"source": SOURCE, "words": words, "revision": digest(words)}
    return gzip.compress(json.dumps(data, ensure_ascii=False).encode(), mtime=0)


def location(storage):
    return storage / "knowledge-sources" / SEED.name


def refresh(storage):
    path = location(storage)
    if not path.exists() or path.stat().st_mtime + 7 * 86400 <= time.time():
        replace(path, capture())


def read(path):
    data = json.loads(gzip.decompress(path.read_bytes()))
    if data["source"] != SOURCE or data["revision"] != digest(data["words"]):
        raise ValueError("The country-word dictionary failed verification.")
    words = {}
    for word, country in data["words"]:
        words.setdefault(spelling(word), set()).add(country)
    return words, data["revision"]


@lru_cache(maxsize=1)
def bundled():
    return read(SEED)


def install(db, storage):
    db.executescript(SCHEMA)
    path = location(storage)
    words, revision = read(path) if path.is_file() else bundled()
    db.executemany("INSERT INTO country_word VALUES(?,?)",
                   ((word, country) for word, countries in words.items() for country in countries))
    return revision


def prefix(db, subject):
    words = subject.split()
    prefixes = [" ".join(words[:end]) for end in range(1, len(words))]
    if not prefixes:
        return None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='country_word'").fetchone():
        rows = db.execute("""SELECT term,country FROM country_word
            WHERE term IN (SELECT value FROM json_each(?)) ORDER BY length(term) DESC,country""",
                          (json.dumps(prefixes),)).fetchall()
        if rows:
            return rows[0][0], [country for term, country in rows if term == rows[0][0]]
    else:
        # Published catalogues remain playable while the builder prepares the next edition.
        words_by_name, _ = bundled()
        for term in reversed(prefixes):
            if term in words_by_name:
                return term, sorted(words_by_name[term])


def relationships(db, cls, term, countries):
    """The class or its immediate source superclass must use the proposed property."""
    classes = [cls, *(row[0] for row in db.execute("SELECT parent FROM world_parent WHERE child=?", (cls,)))]
    scope = json.dumps(classes)
    props = {row[0] for row in db.execute("""SELECT DISTINCT prop FROM pattern
        WHERE cls IN (SELECT value FROM json_each(?)) AND prop IN (?,?,?)""", (scope, *COUNTRY_PROPERTIES))}
    # Published patterns start at five examples. A single documented use can justify
    # investigating a rare class. The actual answer bank still checks every member.
    examples = db.execute("""WITH examples AS (SELECT id FROM world_member WHERE cls=? LIMIT 64)
        SELECT DISTINCT l.prop,l.target FROM examples e CROSS JOIN world_link l ON l.id=e.id
        WHERE l.prop IN (?,?,?,?,?)""", (cls, *COUNTRY_PROPERTIES, *LANGUAGE_PROPERTIES)).fetchall()
    props.update(prop for prop, _ in examples if prop in COUNTRY_PROPERTIES)
    for prop in COUNTRY_PROPERTIES:
        if prop in props:
            yield from ((prop, country) for country in countries)

    # A language must itself be an attested target of a language predicate on this class.
    names = (folded_spelling(term), folded_spelling(term + " language"))
    languages = {(prop, target) for prop, target in db.execute("""SELECT DISTINCT p.prop,p.target FROM world_target_name AS t
        JOIN pattern AS p ON p.target=t.target
        WHERE t.term IN (?,?) AND p.cls IN (SELECT value FROM json_each(?)) AND p.prop IN (?,?)""",
        (*names, scope, *LANGUAGE_PROPERTIES))}
    named = {row[0] for row in db.execute("SELECT target FROM world_target_name WHERE term IN (?,?)", names)}
    languages.update((prop, target) for prop, target in examples if prop in LANGUAGE_PROPERTIES and target in named)
    yield from sorted(languages)


def choices(db, body, edition):
    from catalogue import RuleWithdrawn, UnverifiedRule, world_card
    import date_rules
    import date_subjects
    import dated_links
    from world_links import info

    body = spelling(body).removesuffix("?").strip()
    date = dated_links.DATE.search(body)
    if date and date.end() != len(body):
        return
    subject = body[:date.start()].strip() if date else body
    match = prefix(db, subject)
    if not match:
        return
    term, countries = match
    # Keep names such as "French horn" intact, including when followed by a date.
    literal = folded_spelling(subject)
    if (date_subjects.resolve(db, literal, literal=subject)
            or db.execute("SELECT 1 FROM world_name WHERE term=? LIMIT 1", (literal,)).fetchone()
            or db.execute("SELECT 1 FROM question_text WHERE term=? LIMIT 1", (literal,)).fetchone()):
        return
    base = subject[len(term) + 1:]
    classes = date_subjects.resolve(db, folded_spelling(base), literal=base)
    if len(classes) != 1:
        return
    cls = classes[0]
    dated = None
    if date:
        dated = date_rules.match(db, f"q{cls} {date['clause']}", edition[0], resolve_only=True)
        if not dated:
            return
    cards, pending, checked = [], [], False
    for prop, target in relationships(db, cls, term, countries):
        labels = info(db, cls, prop, target)
        if not labels:
            continue
        checked = True
        if dated:
            try:
                card = dated_links.Rule(dated, "w", prop, target).card(db, edition[0])
            except UnverifiedRule as error:
                pending.extend(error.requests)
                continue
            except RuleWithdrawn:
                continue
        else:
            row = dict(cls=cls, prop=prop, target=target, n=0,
                       subject=labels[0], relation=labels[1], object=labels[2])
            card = world_card(db, row, edition)
            if not card["records"]:
                if not db.execute("SELECT 1 FROM live_rule WHERE key=?", (card["key"],)).fetchone():
                    pending.append(card["key"])
                continue
        cards.append(card)
    if cards:
        raise date_rules.DateChoices(cards, "Choose the recorded relationship you want players to use.", refresh=pending)
    if pending:
        raise UnverifiedRule(pending[0], "The builder can check the recorded relationships for this question.",
                             requests=pending)
    if checked:
        raise RuleWithdrawn("The published source checks found no named answers for these relationships.")
