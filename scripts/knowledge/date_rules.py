"""Calendar-year questions over dated source statements in the published catalogue."""

import json
import re
from dataclasses import dataclass
from itertools import pairwise

import date_subjects
from graph import digest, folded_spelling, spelling

PROPERTIES = {
    569: ("date of birth", "born", "who was born"),
    570: ("date of death", "who died", "that died", "who passed away"),
    571: ("inception", "founded", "established"),
    577: (
        "publication date",
        "with a recorded release",
        "released",
        "published",
        "with a recorded publication date",
    ),
    580: ("start time", "started", "that began"),
    582: ("end time", "that ended"),
    1619: ("date of official opening", "officially opened", "opened"),
}
VERBS = {
    word: prop for prop, (label, *forms) in PROPERTIES.items() for word in (*forms, "whose " + label + " was")
}
VERB = "|".join(map(re.escape, sorted(VERBS, key=len, reverse=True)))
RANGE = re.compile(rf"(.+?) ({VERB}) (.+)")
GENERIC = re.compile(r"(.+?) whose (.+?) (?:was|is|falls) (.+)")
KEY = re.compile(r"date-([1-9]\d{0,11})-([1-9]\d{0,6})-([1-9]\d{0,3})-([1-9]\d{0,3})")
YEAR = r"([1-9]\d{0,3})"
SCHEMA = """
CREATE TABLE date_subject(cls INTEGER PRIMARY KEY,name TEXT NOT NULL,description TEXT NOT NULL);
CREATE TABLE date_subject_name(term TEXT,cls INTEGER,preferred INTEGER,PRIMARY KEY(term,cls)) WITHOUT ROWID;
CREATE TABLE date_property(prop INTEGER PRIMARY KEY,name TEXT NOT NULL);
CREATE TABLE date_property_name(term TEXT,prop INTEGER,preferred INTEGER,PRIMARY KEY(term,prop)) WITHOUT ROWID;
CREATE TABLE date_scope(rule TEXT PRIMARY KEY,cls INTEGER,prop INTEGER,lo INTEGER,hi INTEGER,revision TEXT,captured INTEGER) WITHOUT ROWID;
CREATE INDEX date_scopes_by_subject ON date_scope(cls,prop,lo,hi);
CREATE TABLE date_member(rule TEXT,id INTEGER,name TEXT,aliases TEXT,PRIMARY KEY(rule,id)) WITHOUT ROWID;
CREATE TABLE date_fact(rule TEXT,statement TEXT,id INTEGER,year INTEGER,value TEXT,precision INTEGER,PRIMARY KEY(rule,statement)) WITHOUT ROWID;
CREATE INDEX date_facts_by_year ON date_fact(rule,year,id);
CREATE TABLE question_year(year INTEGER,rule TEXT,PRIMARY KEY(year,rule)) WITHOUT ROWID;
"""


@dataclass(frozen=True)
class Rule:
    cls: int
    prop: int
    lo: int
    hi: int

    @property
    def key(self):
        return f"date-{self.cls}-{self.prop}-{self.lo}-{self.hi}"

    @classmethod
    def decode(cls, key):
        parts = KEY.fullmatch(key)
        if parts:
            rule = cls(*map(int, parts.groups()))
            if rule.lo <= rule.hi:
                return rule

    def period(self):
        if self.lo == self.hi:
            return f"in {self.lo}"
        if self.lo == 1:
            return f"before {self.hi + 1}"
        if self.hi == 9999:
            return f"after {self.lo - 1}"
        if self.lo % 10 == 0 and self.hi == self.lo + 9:
            return f"in the {self.lo}s"
        return f"from {self.lo} through {self.hi}"

    def prompt(self, db):
        from catalogue import named_question

        name = date_subjects.label(db, self.cls)
        label = db.execute("SELECT name FROM date_property WHERE prop=?", (self.prop,)).fetchone()[0]
        verb = PROPERTIES[self.prop][1] if self.prop in PROPERTIES else "whose " + label + " was"
        return named_question(name, verb + " " + self.period())


def period(text):
    if part := re.fullmatch(r"(?:in |during )(?:the )?([1-9]\d{2}0)s", text):
        return int(part[1]), int(part[1]) + 9
    if part := re.fullmatch(r"(?:from |between )" + YEAR + r" (?:through|to|and) " + YEAR, text):
        return int(part[1]), int(part[2])
    if part := re.fullmatch(r"(in|during|before|after|since|through) " + YEAR, text):
        year = int(part[2])
        return {
            "in": (year, year),
            "during": (year, year),
            "before": (1, year - 1),
            "after": (year + 1, 9999),
            "since": (year, 9999),
            "through": (1, year),
        }[part[1]]


def coverage(db, rule):
    """Choose the newest complete capture independently for each overlapping interval."""
    rows = db.execute(
        """SELECT rule,lo,hi,revision,captured FROM date_scope
        WHERE cls=? AND prop=? AND lo<=? AND hi>=?
        ORDER BY captured DESC,(hi-lo),rule""",
        (rule.cls, rule.prop, rule.hi, rule.lo),
    ).fetchall()
    bounds = sorted(
        {rule.lo, rule.hi + 1, *(max(rule.lo, r[1]) for r in rows), *(min(rule.hi, r[2]) + 1 for r in rows)}
    )
    chosen = []
    for low, end in pairwise(bounds):
        row = next((r for r in rows if r[1] <= low and end - 1 <= r[2]), None)
        key, revision, captured = (row[0], row[3], row[4]) if row else (None, None, 0)
        if chosen and chosen[-1][0] == key:
            chosen[-1][2] = end - 1
        else:
            chosen.append([key, low, end - 1, revision, captured])
    return chosen


def segments(db, rule):
    spans = coverage(db, rule)
    return spans if all(row[0] for row in spans) else []


MATCHES = """WITH periods(rule,lo,hi) AS (SELECT value->>0,value->>1,value->>2 FROM json_each(?)),
    matches AS (SELECT f.* FROM periods p CROSS JOIN date_fact f
        ON f.rule=p.rule AND f.year BETWEEN p.lo AND p.hi)
    """


def count(db, selected):
    return db.execute(
        MATCHES
        + """SELECT count(DISTINCT f.id) FROM matches f
        JOIN date_member m ON (m.rule,m.id)=(f.rule,f.id) WHERE m.name IS NOT NULL""",
        (json.dumps(selected),),
    ).fetchone()[0]


class DateChoices(ValueError):
    def __init__(
        self,
        cards,
        message="Specify which date the question uses, such as a release, founding or birth year.",
        *,
        refresh=(),
    ):
        self.cards = cards
        self.refresh = refresh
        super().__init__(message)


def unspecified_date(db, text, edition, *, literal=None):
    if parts := re.fullmatch(r"(.+?) (?:in|from) ([1-9]\d{3})", text):
        subject, year = parts.groups()
        subject_group = 1
    elif parts := re.fullmatch(r"([1-9]\d{3}) (.+)", text):
        year, subject = parts.groups()
        subject_group = 2
    else:
        return
    if db.execute("SELECT 1 FROM question_text WHERE term=? AND complete=1 LIMIT 1", (text,)).fetchone():
        return
    original = parts.re.fullmatch(literal) if literal else None
    classes = date_subjects.resolve(db, subject, literal=original[subject_group] if original else None)
    if len(classes) == 1:
        rows = db.execute(
            """SELECT q.rule,q.prompt,q.n FROM question_year y
            JOIN current_question q USING(rule) JOIN current_question_topic t USING(rule)
            WHERE y.year=? AND t.entity=? ORDER BY q.rule GLOB 'date-*' DESC,n DESC,prompt LIMIT 3""",
            (int(year), classes[0]),
        )
        raise DateChoices(
            [
                {"key": key, "prompt": prompt, "records": n, "edition": edition, "complete": False}
                for key, prompt, n in rows
            ]
        )


def match(db, body, edition, *, resolve_only=False):
    from catalogue import RuleWithdrawn, UnverifiedRule

    body = re.sub(
        r"\b(?:from |in )?([1-9]\d{3})\s*[-–—]\s*([1-9]\d{3})(?=\s*[?.]?\s*$)", r"from \1 through \2", body
    )
    text = folded_spelling(body).removesuffix("?").strip()
    part = RANGE.fullmatch(text) or GENERIC.fullmatch(text)
    if not part:
        unspecified_date(db, text, edition, literal=spelling(body).removesuffix("?").strip())
        return None
    if not (bounds := period(part[3])):
        return None
    if re.search(
        r"\b(in|during|from|between|through|to|and|before|after|since)\s+[-−]\s*\d", body, re.IGNORECASE
    ):
        raise ValueError("This date index covers Gregorian years 1 through 9999 CE.")
    prop = VERBS.get(part[2])
    if prop is None:
        if by_id := re.fullmatch(r"p([1-9]\d{0,6})", part[2]):
            prop = int(by_id[1])
        else:
            options = db.execute(
                "SELECT prop,preferred FROM date_property_name WHERE term=? ORDER BY preferred DESC",
                (part[2],),
            ).fetchall()
            if not options:
                return None
            if len(options) > 1 and options[0][1] == options[1][1]:
                raise ValueError("Several date properties match. Use the full property name or its P number.")
            prop = options[0][0]
    if not db.execute("SELECT 1 FROM date_property WHERE prop=?", (prop,)).fetchone():
        return None
    if not 1 <= bounds[0] <= bounds[1] <= 9999:
        raise ValueError("Choose a year range with the earlier year first, between 1 and 9999.")
    subject = "human" if part[1] in ("people", "person", "someone") else part[1]
    if prop == 577 and subject.endswith((" first", " originally")):
        raise ValueError(
            "An original release needs evidence of the earliest release. Use “with a recorded release” to include regional releases and re-releases."
        )
    original = part.re.fullmatch(spelling(body).removesuffix("?").strip())
    classes = date_subjects.resolve(db, subject, literal=original[1] if original else None)
    if len(classes) > 1:
        choices = ", ".join(f"Q{i}" for i in classes[:5])
        raise ValueError("Several source classes match this subject. Use its class ID: " + choices + ".")
    if not classes or not db.execute("SELECT 1 FROM item WHERE id=?", (classes[0],)).fetchone():
        return None
    rule = Rule(classes[0], prop, *bounds)
    if resolve_only:
        return rule
    selected = coverage(db, rule)
    missing = [Rule(rule.cls, rule.prop, lo, hi).key for key, lo, hi, _, _ in selected if not key]
    if missing:
        raise UnverifiedRule(
            rule.key, "The catalogue has not yet checked this class and date range.", requests=missing
        )
    n = count(db, selected)
    if not n:
        raise RuleWithdrawn("The published date records contain no named matches for this question.")
    card = {"key": rule.key, "edition": edition, "prompt": rule.prompt(db), "records": n, "complete": False}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='date_subject'").fetchone():
        name = db.execute(
            """SELECT i.name,s.name FROM date_subject s JOIN item i ON i.id=s.cls
            WHERE s.cls=?""",
            (rule.cls,),
        ).fetchone()
        if (
            name
            and subject == folded_spelling(name[0])
            and subject != folded_spelling(date_subjects.label(db, rule.cls))
        ):
            raise DateChoices(
                [card],
                f"The current source calls this class “{name[1]}”. Select the question using that wording.",
            )
    return card


def date_detail(statement, value, precision):
    return {
        "value": value[: {9: 4, 10: 7}.get(precision, 10)],
        "precision": precision,
        "calendar": "Gregorian",
        "url": "https://www.wikidata.org/entity/statement/" + statement,
    }


def read_capture(db, chosen, member_query=None, args=()):
    names, claims, scanned = {}, {}, set()
    membership = " WHERE f.id IN (" + member_query + ")" if member_query else ""
    rows = db.execute(
        MATCHES
        + """SELECT f.id,f.statement,f.value,f.precision,m.name,m.aliases,s.captured
        FROM matches f JOIN date_member m ON (m.rule,m.id)=(f.rule,f.id)
        JOIN date_scope s ON s.rule=f.rule"""
        + membership
        + " ORDER BY s.captured DESC,f.statement",
        (json.dumps(chosen), *args),
    )
    for entity, statement, value, precision, name, aliases, _ in rows:
        scanned.add(entity)
        if name:
            _, forms = names.setdefault(entity, (name, set()))
            forms.update([name, *json.loads(aliases)])
        claims.setdefault(entity, {})[statement] = date_detail(statement, value, precision)
    capture = {
        "n": len(names),
        "rows": [[f"Q{i}", n, sorted(forms - {n})] for i, (n, forms) in sorted(names.items())],
        "scanned": len(scanned),
        "captured": max(c[4] for c in chosen),
        "revision": digest([c[:4] for c in chosen]),
    }
    return capture, {f"wd:Q{i}": list(rows.values()) for i, rows in claims.items()}


def condition(db, rule):
    label = db.execute("SELECT name FROM date_property WHERE prop=?", (rule.prop,)).fetchone()[0]
    subject = date_subjects.label(db, rule.cls)
    return {
        "classId": rule.cls,
        "class": subject,
        "prop": rule.prop,
        "relation": label,
        "label": rule.period(),
        "target": f"years:{rule.lo}-{rule.hi}",
        "dated": True,
    }


def play(db, game, rule, prompt, file_format):
    from catalogue import RuleWithdrawn, play_world

    chosen = segments(db, rule)
    if not chosen:
        raise RuleWithdrawn("The recorded date range is unavailable in this edition.")
    capture, claims = read_capture(db, chosen)
    if not capture["rows"]:
        raise RuleWithdrawn("The published date records contain no named matches for this question.")

    def proof(eid, _condition):
        dates = claims[eid]
        return {"dates": dates, "url": dates[0]["url"]}

    return play_world(
        db,
        game,
        rule.key,
        rule.prompt(db),
        None,
        (),
        [condition(db, rule)],
        file_format,
        date_capture=capture,
        date_proof=proof,
    )


def index_questions(db):
    candidates = {
        Rule(cls, prop, lo, hi) for cls, prop, lo, hi in db.execute("SELECT cls,prop,lo,hi FROM date_scope")
    }
    for cls, prop, year in db.execute("""SELECT DISTINCT s.cls,s.prop,f.year FROM date_scope s
            JOIN date_fact f ON f.rule=s.rule"""):
        candidates.add(Rule(cls, prop, year, year))
    for rule in sorted(candidates, key=lambda r: r.key):
        n = count(db, segments(db, rule))
        if n:
            db.execute("INSERT INTO current_question VALUES(?,?,?)", (rule.key, rule.prompt(db), n))
            db.execute("INSERT INTO current_question_topic VALUES(?,?)", (rule.cls, rule.key))
            if rule.lo == rule.hi:
                db.execute("INSERT INTO question_year VALUES(?,?)", (rule.lo, rule.key))
