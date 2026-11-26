"""Join a recorded date and an entity relationship on the same Wikidata subject."""

import gzip
import json
import re
from dataclasses import dataclass

import date_rules
import date_subjects
import places
from graph import digest, folded_spelling, spelling

YEAR = r"[1-9]\d{0,3}"
PERIOD = rf"(?:(?:in|during) (?:the )?[1-9]\d{{2}}0s|(?:from|between) {YEAR} (?:to|through|and) {YEAR}|(?:from |in )?{YEAR}\s*[-–—]\s*{YEAR}|(?:in|during|after|before|since|through) {YEAR})"
DATE = re.compile(
    rf"(?<!\S)(?P<clause>(?:{date_rules.VERB}|whose (?:(?!(?:and|whose)\b)[\w-]+ )+?(?:was|is|falls)) (?:{PERIOD}))(?=$|[\s,?])"
)
LINK = re.compile(r"([wg])-([1-9]\d{0,11})-([1-9]\d{0,6})-([1-9]\d{0,11})")
SCHEMA = """CREATE TABLE dated_alternative(operand TEXT,rule TEXT,PRIMARY KEY(operand,rule)) WITHOUT ROWID;"""


@dataclass(frozen=True)
class Rule:
    date: date_rules.Rule
    mode: str
    prop: int
    target: int

    @property
    def link(self):
        return f"{self.mode}-{self.date.cls}-{self.prop}-{self.target}"

    @property
    def key(self):
        return f"during:{self.date.key}+{self.link}"

    @classmethod
    def decode(cls, key):
        if not isinstance(key, str) or not key.startswith("during:"):
            return None
        dated, sep, link = key[7:].partition("+")
        rule, parts = date_rules.Rule.decode(dated), LINK.fullmatch(link)
        if (
            sep
            and rule
            and parts
            and rule.cls == int(parts[2])
            and (parts[1] == "w" or int(parts[3]) in places.PHRASES)
        ):
            return cls(rule, parts[1], int(parts[3]), int(parts[4]))

    def condition(self, db):
        from world_links import info

        if not db.execute("SELECT 1 FROM date_property WHERE prop=?", (self.date.prop,)).fetchone():
            return None
        if self.mode == "g":
            return places.condition(db, self.date.cls, self.prop, self.target)
        labels = info(db, self.date.cls, self.prop, self.target)
        if labels:
            return {
                "classId": self.date.cls,
                "class": labels[0],
                "prop": self.prop,
                "relation": labels[1],
                "target": f"wd:Q{self.target}",
                "label": labels[2],
            }

    def prompt(self, db, condition):
        from catalogue import world_clause
        from world_links import identifiers

        name = condition["label"]
        if identifiers(db, folded_spelling(name), "target") != [self.target]:
            name = f"{name} (Q{self.target})"
        if self.mode == "g":
            phrase = "located in" if self.prop == 131 else places.PHRASES[self.prop][0]
            relation = f"{phrase} {name}"
        else:
            relation = world_clause(self.prop, condition["relation"], name)
            if relation.startswith("with “") and re.fullmatch(r"[\w -]{1,44}", condition["relation"]):
                relation = f"whose {condition['relation']} is {name}"
        return self.date.prompt(db) + " and " + relation

    def members(self, db):
        """A captured date already establishes the class of every candidate."""
        from world_links import current_members

        current = current_members(db, self.link)
        if current:
            return (
                "SELECT value FROM json_each(?)",
                (json.dumps([int(qid[1:]) for qid, _, _ in current[2]]),),
                current,
            )
        if self.mode == "g":
            query, args = places.members(self.date.cls, self.prop, self.target, check_class=False)
        else:
            query, args = "SELECT id FROM world_link WHERE prop=? AND target=?", (self.prop, self.target)
        return query, args, None

    def card(self, db, edition):
        from catalogue import RuleWithdrawn, UnverifiedRule

        condition = self.condition(db)
        if not condition:
            raise RuleWithdrawn("The named relationship is unavailable in this edition.")
        live = db.execute("SELECT n FROM live_rule WHERE key=?", (self.key,)).fetchone()
        if live:
            n = live[0]
            if not n:
                raise RuleWithdrawn("The current source has no named answers meeting both conditions.")
        else:
            chosen = date_rules.segments(db, self.date)
            if not chosen:
                raise UnverifiedRule(
                    self.key, "The catalogue has not yet checked these two conditions together."
                )
            query, args, _ = self.members(db)
            n = db.execute(
                date_rules.MATCHES
                + """SELECT count(DISTINCT f.id) FROM matches f JOIN date_member m
                ON (m.rule,m.id)=(f.rule,f.id) WHERE m.name IS NOT NULL AND f.id IN ("""
                + query
                + ")",
                (json.dumps(chosen), *args),
            ).fetchone()[0]
            if not n:
                raise UnverifiedRule(
                    self.key, "The local records have no verified overlap for these conditions."
                )
        return {
            "key": self.key,
            "edition": edition,
            "prompt": self.prompt(db, condition),
            "records": n,
            "complete": False,
        }


def match(db, body, edition):
    from catalogue import UnverifiedRule, match_relation, match_single

    body = spelling(body).removesuffix("?").strip()
    dates = list(DATE.finditer(body))
    if len(dates) != 1:
        return None
    date = dates[0]
    before, after = body[: date.start()].strip(" ,"), body[date.end() :].strip(" ,")
    left = re.sub(r"\s+(?:and(?: (?:was|were))?|that (?:was|were)|which (?:was|were))$", "", before)
    right = after.removeprefix("and ").strip()
    if not left or (not right and left == before and not re.search(r"\s", left)):
        return None
    remaining = (left + " " + right).strip()
    publication = db.execute("SELECT edition,format FROM publication").fetchone()

    def resolve(phrase, dated=None):
        return match_relation(
            db,
            folded_spelling(phrase),
            spelling(phrase),
            publication,
            resolve_only=True,
            request_target=dated is not None,
            date_key=dated.key if dated else "",
        )

    # An explicit ID gives the relationship resolver the current date class even if
    # its historic label has changed. Keep the entire remaining clause as entered.
    # A real target name may contain date wording of its own.
    if resolve(body):
        whole = match_single(db, "Name a " + body, include_empty=True)
        if whole and LINK.fullmatch(whole["key"]):
            if not whole["records"]:
                raise UnverifiedRule(whole["key"])
            return whole
    words = remaining.split()
    for end in range(1, len(words)):
        subject = " ".join(words[:end])
        lookup = "human" if subject in ("person", "people", "someone") else folded_spelling(subject)
        if len(classes := date_subjects.resolve(db, lookup, literal=subject)) != 1:
            continue
        rest = " ".join(words[end:])
        if rest.split()[0] in ("and", "or", "not", "never", "first", "originally"):
            continue
        dated = date_rules.match(db, f"q{classes[0]} {date['clause']}", edition, resolve_only=True)
        if not dated:
            continue
        linked = resolve(f"Q{classes[0]} {rest}", dated)
        parts = LINK.fullmatch(linked["key"]) if linked else None
        if not parts or int(parts[2]) != classes[0]:
            continue
        rule = Rule(dated, parts[1], int(parts[3]), int(parts[4]))
        card = rule.card(db, edition)
        historical = db.execute("SELECT name FROM item WHERE id=?", (dated.cls,)).fetchone()[0]
        if folded_spelling(subject) == folded_spelling(historical) and folded_spelling(
            subject
        ) != folded_spelling(date_subjects.label(db, dated.cls)):
            raise date_rules.DateChoices([card], "Select the wording that uses the current source class.")
        if rule.mode == "g" and right.startswith("in "):
            raise date_rules.DateChoices(
                [card],
                "This wording can refer to the place of the event. The catalogue can check the subject’s recorded location separately.",
            )
        return card


def play(db, game, rule):
    from catalogue import RuleWithdrawn, play_world

    condition = rule.condition(db)
    if not condition:
        raise RuleWithdrawn("The relationship is no longer available.")
    live = db.execute("SELECT release FROM live_rule WHERE key=?", (rule.key,)).fetchone()
    if live:
        captured = json.loads(gzip.decompress(live[0]))
        capture = {**captured, "n": len(captured["rows"])}
        dates = captured["dates"]
        rel_source = "wikidata-query-link-" + digest(rule.key)
        revision, checked = captured["revision"], captured["captured"]
        paths = places.proof_reader(db, [rule.link], captured["proofs"]) if rule.mode == "g" else None
    else:
        chosen = date_rules.segments(db, rule.date)
        if not chosen:
            raise RuleWithdrawn("The sources for this dated relationship are unavailable.")
        query, args, current = rule.members(db)
        capture, dates = date_rules.read_capture(db, chosen, query, args)
        if current:
            by_id = {qid: [name, *aliases] for qid, name, aliases in current[2]}
            for row in capture["rows"]:
                row[2] = sorted((set(row[2]) | set(by_id[row[0]])) - {row[1]})
            rel_source = "wikidata-query-" + digest(rule.link)
            revision, checked = current[:2]
        else:
            rel_source = "world5m-" + rule.link
            revision, checked = db.execute(
                "SELECT revision,checked FROM source WHERE id='wikidata5m-catalogue-input'"
            ).fetchone()
        paths = places.proof_reader(db, [rule.link] if current else []) if rule.mode == "g" else None
    if not capture["rows"]:
        raise RuleWithdrawn("No named answers in this edition meet both conditions.")
    from world_index import SOURCE

    facts = {
        "source": rel_source,
        "revision": revision,
        "captured": checked,
        "url": SOURCE
        if rel_source.startswith("world5m-")
        else f"https://www.wikidata.org/wiki/Property:P{rule.prop}",
    }

    def proof(eid, c):
        if c.get("dated"):
            recorded = dates[eid]
            return {"dates": recorded, "url": recorded[0]["url"]}
        if paths:
            detail = paths(eid, c)
        elif live:
            guid = captured["statements"][eid][0]
            detail = {"url": "https://www.wikidata.org/entity/statement/" + guid}
        else:
            detail = {"url": f"https://www.wikidata.org/wiki/{eid[3:]}#P{rule.prop}"}
        return {**detail, "relationshipLabel": c["relation"], "targetLabel": c["label"]}

    return play_world(
        db,
        game,
        rule.key,
        rule.prompt(db, condition),
        None,
        (),
        [date_rules.condition(db, rule.date), {**condition, "statementProof": True}],
        db.execute("SELECT format FROM publication").fetchone()[0],
        date_capture=capture,
        date_proof=proof,
        related_source=facts,
    )


def alternatives(db, card):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='dated_alternative'").fetchone():
        return []
    return [
        dict(card, key=key, prompt=prompt, records=n, complete=False)
        for key, prompt, n in db.execute(
            """SELECT q.rule,q.prompt,q.n FROM dated_alternative a JOIN current_question q USING(rule)
            WHERE a.operand=? ORDER BY q.n BETWEEN 5 AND 80 DESC,q.n DESC,q.prompt LIMIT 2""",
            (card["key"],),
        )
    ]


def discover(db):
    """Choose varied relationships actually shared by named members of each published date range."""
    from catalogue import RELATION_PHRASES, RuleWithdrawn, UnverifiedRule
    from world_links import identifiers

    def index(rule, card):
        if len(card["prompt"]) > 240:
            return
        db.execute(
            "INSERT OR IGNORE INTO current_question VALUES(?,?,?)",
            (rule.key, card["prompt"], card["records"]),
        )
        if rule.date.lo == rule.date.hi:
            db.execute("INSERT OR IGNORE INTO question_year VALUES(?,?)", (rule.date.lo, rule.key))
        db.executemany(
            "INSERT OR IGNORE INTO current_question_topic VALUES(?,?)",
            ((i, rule.key) for i in (rule.date.cls, rule.target)),
        )
        db.executemany(
            "INSERT OR IGNORE INTO dated_alternative VALUES(?,?)",
            ((k, rule.key) for k in (rule.date.key, rule.link)),
        )

    for key, n in db.execute("SELECT key,n FROM live_rule WHERE n>0 AND key GLOB 'during:*'"):
        rule = Rule.decode(key)
        if rule and (condition := rule.condition(db)):
            index(rule, {"prompt": rule.prompt(db, condition), "records": n})
    for key, n in db.execute(
        "SELECT rule,n FROM current_question WHERE rule GLOB 'date-*' AND n>=8"
    ).fetchall():
        dated = date_rules.Rule.decode(key)
        spans = date_rules.segments(db, dated)
        candidates = db.execute(
            date_rules.MATCHES
            + """, named AS MATERIALIZED (
            SELECT DISTINCT f.id FROM matches f JOIN date_member m ON (m.rule,m.id)=(f.rule,f.id) WHERE m.name IS NOT NULL),
            groups AS (SELECT l.prop,l.target,count(*) hits FROM named m CROSS JOIN world_link l
                ON l.id=m.id GROUP BY l.prop,l.target HAVING count(*) BETWEEN 3 AND ?)
            SELECT g.prop,g.target,i.name FROM groups g JOIN item i ON i.id=g.target JOIN predicate p ON p.id=g.prop
            WHERE i.name<>'' ORDER BY g.prop IN (SELECT value FROM json_each(?)) DESC,
            g.hits BETWEEN 5 AND 80 DESC,abs(g.hits-?),i.incoming DESC,g.prop,g.target LIMIT 160""",
            (
                json.dumps(spans),
                min(200, 2 * n // 3),
                json.dumps(list(RELATION_PHRASES)),
                max(4, min(30, int(n**0.5))),
            ),
        )
        used = set()
        subject = " " + folded_spelling(date_subjects.label(db, dated.cls)) + " "
        for prop, target, label in candidates:
            if (
                prop in used
                or target == dated.cls
                or " " + folded_spelling(label) + " " in subject
                or identifiers(db, folded_spelling(label), "target") != [target]
            ):
                continue
            rule = Rule(dated, "w", prop, target)
            try:
                card = rule.card(db, "")
            except (RuleWithdrawn, UnverifiedRule):
                continue
            if 3 <= card["records"] <= 2 * n // 3 and len(card["prompt"]) <= 240:
                index(rule, card)
                used.add(prop)
                if len(used) == 3:
                    break
