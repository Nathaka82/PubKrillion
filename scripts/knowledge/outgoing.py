"""Compile the named objects of a property on one identified subject."""

import json
import re
from dataclasses import dataclass

from graph import digest, folded_spelling, indefinite_article, ingest, open_graph, spelling
from item_values import KEY
from topic_sources import noun_forms
from world_index import SOURCE


def properties(db, text):
    text = folded_spelling(text)
    if tag := re.fullmatch(r"(?:(.+) )?p([1-9]\d{0,6})", text):
        pid = int(tag[2])
        return {pid} if ((not tag[1] or pid in properties(db,tag[1]))
                         and db.execute("SELECT 1 FROM predicate WHERE id=?", (pid,)).fetchone()) else set()
    forms = noun_forms(text)
    rows = db.execute("""SELECT p.id,p.name FROM world_property_name n JOIN predicate p ON p.id=n.prop
                          WHERE n.term IN (SELECT value FROM json_each(?))""", (json.dumps(sorted(forms)),))
    labels = dict(rows)
    preferred = {pid for pid, name in labels.items() if folded_spelling(name) in forms}
    return preferred or set(labels)


def subjects(db, text):
    text = text.strip(' "“”')
    term = folded_spelling(text)
    fresh = db.execute("SELECT 1 FROM sqlite_master WHERE name='value_subject_name'").fetchone()
    if tag := re.fullmatch(r"(?:(.+) )?q([1-9]\d{0,11})", term):
        item = int(tag[2])
        if tag[1]:
            matched = db.execute("SELECT 1 FROM world_name WHERE term=? AND id=?", (tag[1],item)).fetchone()
            if not matched and fresh:
                matched = db.execute("SELECT 1 FROM value_subject_name WHERE term=? AND id=?", (tag[1],item)).fetchone()
            if not matched:
                return []
        return [item]
    rows = db.execute("""SELECT n.id,n.preferred,i.name,i.aliases FROM world_name n JOIN item i ON i.id=n.id
                         WHERE n.term=? ORDER BY n.preferred DESC,n.id LIMIT 13""", (term,)).fetchall()
    exact = [(i, int(spelling(name) == spelling(text))) for i, _, name, aliases in rows
             if any(spelling(n) == spelling(text) for n in (name,*aliases.split("\t")))]
    current = db.execute("SELECT DISTINCT id FROM value_subject_name WHERE term=? LIMIT 13",(term,)).fetchall() if fresh else []
    if len(rows) == 13 or len(current) == 13:
        raise ValueError("Several subjects share this name. Include the subject’s Wikidata Q number.")
    ranked = exact or [(i, preferred) for i, preferred, _, _ in rows]
    found = sorted({i for i,_ in ranked} | {r[0] for r in current})
    if not found and term.startswith("the "):
        return subjects(db, text[4:])
    return found


def fields(db, text):
    """Resolve only complete noun or verb phrases supported by the property dictionary."""
    raw = spelling(text).rstrip("?").strip()
    if canonical := re.fullmatch(r'for (.+), name a value wikidata lists under “([^”]+)”', raw):
        yield canonical[1], properties(db, canonical[2])
        return
    if interrogative := re.fullmatch(r"who ([\w-]+) (.+)", raw):
        verb, subject = interrogative.groups()
        if verb.endswith(("ed","en")):
            yield subject, properties(db, verb + " by")
    if action := re.fullmatch(r"(?:what|which) (.+?) (?:did|does|do) (.+) ([\w-]+)", raw):
        verb = properties(db,action[3]) | properties(db,action[3]+"s")
        yield action[2], properties(db,action[1]) & verb
        return
    body = re.sub(r"^(?:(?:please |can you )?(?:name|list|identify|give me|tell me)|(?:who|what|which) (?:is|are|was|were)) ", "", raw)
    body = re.sub(r"^(?:all (?:of )?)?(?:one of )?(?:the |a |an )?", "", body)
    for split in re.finditer(r" (wikidata lists for|of|for) ", body):
        field, subject = body[:split.start()], body[split.end():]
        if split[1] == "of" and field in ("type","kind","form","example"):
            continue
        yield subject, properties(db, field)
    if possessive := re.fullmatch(r"(.+)'s (.+)", body):
        yield possessive[1], properties(db, possessive[2])


@dataclass(frozen=True)
class Rule:
    subject: int
    prop: int

    @property
    def key(self):
        return f"values:Q{self.subject}:P{self.prop}"

    @classmethod
    def decode(cls, key):
        if parsed := KEY.fullmatch(key):
            return cls(int(parsed[1]), int(parsed[2]))

    def snapshot(self, db):
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='value_subject'").fetchone():
            return db.execute("""SELECT s.name,s.revision,s.checked,p.unresolved,p.named FROM value_subject s
                LEFT JOIN value_property p ON p.subject=s.id AND p.prop=? WHERE s.id=?""", (self.prop,self.subject)).fetchone()

    def row_query(self, snapshot):
        if snapshot:
            return """SELECT c.target,CASE WHEN n.id IS NULL THEN i.name ELSE n.name END AS name,
                    CASE WHEN n.id IS NULL THEN i.aliases ELSE n.aliases END AS aliases,coalesce(i.incoming,0),c.statement,n.id
                FROM value_claim c LEFT JOIN value_label n ON n.id=c.target LEFT JOIN item i ON i.id=c.target
                WHERE (c.subject,c.prop)=(?,?)""", (self.subject,self.prop)
        edges = {31:"SELECT cls AS target FROM world_member WHERE id=?",
                 106:"SELECT role AS target FROM world_role WHERE id=?",
                 279:"SELECT parent AS target FROM world_parent WHERE child=?"}
        query = edges.get(self.prop, "SELECT target FROM world_link WHERE id=? AND prop=?")
        args = (self.subject,) if self.prop in edges else (self.subject,self.prop)
        return f"""SELECT c.target,i.name,i.aliases,coalesce(i.incoming,0),NULL,NULL
            FROM ({query}) c LEFT JOIN item i ON i.id=c.target""", args

    def card(self, db, edition):
        from catalogue import RuleWithdrawn, UnverifiedRule
        snapshot = self.snapshot(db)
        label = db.execute("SELECT name FROM predicate WHERE id=?", (self.prop,)).fetchone()
        if not label or (snapshot and not snapshot[1]):
            raise RuleWithdrawn("The subject or property is unavailable in this edition.")
        prop = label[0]
        historical = db.execute("SELECT name FROM item WHERE id=?", (self.subject,)).fetchone()
        name = (snapshot[0] if snapshot else None) or (historical[0] if historical else f"Q{self.subject}")
        if (snapshot and (not historical or name != historical[0] or db.execute(
                "SELECT 1 FROM value_subject_name WHERE term=? AND id<>? LIMIT 1",
                (folded_spelling(name),self.subject)).fetchone())) or db.execute(
                "SELECT id FROM world_name WHERE term=? AND id<>? LIMIT 1", (folded_spelling(name),self.subject)).fetchone():
            name += f" (Q{self.subject})"
        if properties(db, prop) != {self.prop}:
            prop += f" (P{self.prop})"
        prompt = (f"For {name}, name a value Wikidata lists under “{prop}”" if
                  prop.endswith((" by"," of"," in"," on"," at"," with")) or prop.startswith(("has ","is "))
                  else f"Name {indefinite_article(prop)} {prop} Wikidata lists for {name}")
        query, args = self.row_query(snapshot)
        total, named = db.execute(f"SELECT count(*),count(nullif(name,'')) FROM ({query})", args).fetchone()
        if not named:
            if snapshot and not snapshot[3] and (not total or snapshot[4]):
                raise RuleWithdrawn("This Wikidata revision has no named item values for that property.")
            raise UnverifiedRule(self.key, "The builder can check this subject’s recorded property values.")
        complete = bool(snapshot and snapshot[4] and not snapshot[3] and named == total)
        return {"edition": edition, "key": self.key, "records": named, "prompt": prompt, "complete": complete}

    def play(self, db, game, edition):
        from compiler import evaluate
        from contextlib import closing
        card = self.card(db, edition)
        snapshot = self.snapshot(db)
        query, args = self.row_query(snapshot)
        rows = db.execute(query + " ORDER BY c.target", args)
        if snapshot:
            revision, checked = snapshot[1:3]
            url = f"https://www.wikidata.org/w/index.php?title=Q{self.subject}&oldid={revision}#P{self.prop}"
        else:
            revision, checked = db.execute("SELECT revision,checked FROM source WHERE id='wikidata5m-catalogue-input'").fetchone()
            url = SOURCE
        sid = ("wikidata-query-values-" if snapshot else "world5m-values-") + self.key[7:]
        source = dict(source=sid, revision=digest([revision,edition]), url=url, captured=checked)
        plan = {"class": self.key, "catalogueKey": self.key, "classLabel": card["prompt"]}
        if card["complete"]:
            source["census"] = {self.key: card["records"]}
            plan.update(register=sid, expectedMatches=card["records"])
        def records():
            for target, name, forms, incoming, guid, named_id in rows:
                if name:
                    yield {"id": f"wd:Q{target}", "name": name,
                           "aliases": json.loads(forms) if named_id else (forms or "").split("\t"),
                           "types":[self.key], "graph_recognition":[0,incoming], "depth":1 if incoming>=1000 else 2,
                           "url": ("https://www.wikidata.org/entity/statement/"+guid if guid else
                                   f"https://www.wikidata.org/wiki/Q{self.subject}#P{self.prop}")}
        with closing(open_graph(":memory:")) as graph:
            ingest(graph, source, records(), index_names=False)
            total = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
            return evaluate(graph, game, card["prompt"], plan, name_catalogue=db, name_edition=edition,
                            unnamed_members=total-card["records"])


def suggestions(db, subject_name, edition, complete_only):
    """Offer recorded properties of a known subject when a host supplies just its name."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='value_subject_name'").fetchone():
        return []
    # Only inspect a subject whose statements we actually captured. Its name may
    # still refer to a different archived item, in which case require a qualifier.
    if not db.execute("SELECT 1 FROM value_subject_name WHERE term=?", (folded_spelling(subject_name),)).fetchone():
        return []
    ids = subjects(db, subject_name)
    if len(ids) != 1:
        return []
    subject = ids[0]
    available = db.execute("""SELECT c.prop FROM value_claim c JOIN predicate p ON p.id=c.prop
        JOIN value_property v ON (v.subject,v.prop)=(c.subject,c.prop)
        WHERE c.subject=? GROUP BY c.prop HAVING count(*)>=2
        ORDER BY v.named DESC,v.unresolved,count(*) DESC,c.prop""", (subject,))
    from catalogue import RuleWithdrawn, UnverifiedRule
    cards = []
    for (prop,) in available:
        try:
            card = Rule(subject, prop).card(db, edition)
        except (UnverifiedRule, RuleWithdrawn):
            continue
        if card["records"] >= 2 and (card["complete"] or not complete_only):
            cards.append(card)
            if len(cards) == 3:
                break
    return cards


def match(db, text, edition):
    from catalogue import RuleWithdrawn, UnverifiedRule
    from date_rules import DateChoices
    rules = {Rule(subject,prop) for name, props in fields(db,text) if props
             for subject in subjects(db,name) for prop in props}
    if len(rules) == 1:
        return rules.pop().card(db,edition)
    if rules:
        cards, pending = [], []
        for rule in sorted(rules,key=lambda r:r.key):
            try:
                cards.append(rule.card(db,edition))
            except UnverifiedRule:
                pending.append(rule.key)
            except RuleWithdrawn:
                continue
        if cards:
            raise DateChoices(cards,"Choose the subject and recorded property to use for this question.",refresh=pending)
        if pending:
            raise UnverifiedRule(pending[0], "The builder can check the possible subjects and properties.", requests=pending)
        raise RuleWithdrawn("The recorded properties have no named values in this edition.")
