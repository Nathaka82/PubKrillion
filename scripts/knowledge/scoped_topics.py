"""Suggest explicit questions when an event uses a broad word for its participants."""

import re

from graph import folded_spelling, norm


def participant_aliases(db, subject):
    terms = db.execute("""SELECT t.source,t.key,EXISTS(SELECT 1 FROM calendar c WHERE c.source=t.source)
        FROM term t WHERE t.alias=? AND EXISTS(SELECT 1 FROM member m WHERE (m.source,m.class)=(t.source,t.key))
        AND EXISTS(SELECT 1 FROM term_display d
            WHERE (d.source,d.key)=(t.source,t.key) AND spelling_key(d.alias)=?)""",
        (norm(subject), subject)).fetchall()
    if any(not is_event or folded_spelling(cls) == subject for _, cls, is_event in terms):
        return []
    return [(source, cls) for source, cls, _ in terms]


def require_explicit_subject(db, body):
    subject = folded_spelling(body).rstrip("?").strip()
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='suggested_scope'").fetchone():
        row = db.execute("SELECT class FROM suggested_scope WHERE term=? ORDER BY class LIMIT 1", (subject,)).fetchone()
        if row:
            from graph import indefinite_article
            raise ValueError(f"Choose a published scope, such as “Name {indefinite_article(row[0])} {row[0]}”.")
    if participant_aliases(db, subject):
        raise ValueError("The event source uses that word for its participants. Specify the group or name the event.")


def scoped_choices(db, body, edition):
    from catalogue import prompt_term, register_card
    from date_rules import DateChoices

    # Retain the entire qualifier when trying a more precise subject.
    parts = re.split(r" (?=(?:in|from|at|with|by|that|which|who|whose|bordering) )", body, maxsplit=1)
    subject = parts[0]
    qualifier = " " + parts[1] if len(parts) == 2 else ""
    terms = participant_aliases(db, subject)
    declared = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='suggested_scope'").fetchone():
        for row in db.execute("""SELECT r.* FROM suggested_scope s JOIN register_rule r
            ON (r.source,r.class)=(s.source,s.class) WHERE s.term=? ORDER BY r.n DESC,r.key LIMIT 3""", (body,)):
            declared[row["key"]] = register_card(db, row, edition)
    if not (terms or declared) or db.execute("SELECT 1 FROM question_text WHERE term=? LIMIT 1", (body,)).fetchone():
        return
    if qualifier:
        for source, _ in terms:
            events = db.execute("""SELECT DISTINCT d.alias FROM calendar c JOIN term_display d
                ON (d.source,d.key)=(c.source,c.event) WHERE c.source=?""", (source,))
            if any(" " + folded_spelling(row[0]) + " " in qualifier + " " for row in events):
                return  # The named event provides context for its own participant terms and dates.

    choices = {}
    for source, cls in terms:
        # Shared names can suggest another published question. They never grant
        # membership or points. Each selected rule still compiles its own evidence.
        related = db.execute("""WITH sample(term) AS MATERIALIZED (
                SELECT DISTINCT normalize(e.name) FROM member m JOIN entity e USING(source,id)
                WHERE m.source=? AND m.class=? ORDER BY 1 LIMIT 40),
            overlap AS (SELECT m.class,count(DISTINCT s.term) shared FROM sample s
                CROSS JOIN entity_name n ON n.term=s.term CROSS JOIN member m ON (m.source,m.id)=(n.source,n.id)
                WHERE length(s.term)>=3 AND m.class<>? GROUP BY m.class
                HAVING count(DISTINCT s.term)>=max(2,(SELECT count(*) FROM sample)*0.6)),
            rules AS (SELECT key,class,n,source,1 complete FROM register_rule
                UNION ALL SELECT key,class,n,NULL,0 FROM class_rule)
            SELECT r.* FROM overlap o JOIN rules r USING(class)
            ORDER BY r.n>(SELECT count(*) FROM sample) DESC,o.shared DESC,r.n DESC,r.complete DESC,r.key
            LIMIT 8""", (source, cls, cls))
        cards = [{**register_card(db, row, edition), "complete": bool(row["complete"])}
                 for row in related if row["key"] not in declared]
        if qualifier:
            subjects = {cls}
            for card in cards:
                subjects.add(prompt_term(card["prompt"]))
                # Source questions sometimes shorten a proper name, e.g. United Nations to UN.
                abbreviated = re.sub(r"\b[A-Z][a-z]+(?: [A-Z][a-z]+)+\b",
                                     lambda m: "".join(word[0] for word in m[0].split()), card["prompt"])
                subjects.add(prompt_term(abbreviated))
            for name in sorted(subjects):
                for key, prompt, count, complete in db.execute(
                    "SELECT rule,prompt,n,complete FROM question_text WHERE term=? AND n>0 ORDER BY rule",
                    (folded_spelling(name) + qualifier,),
                ):
                    choices[key] = dict(key=key, prompt=prompt, records=count, complete=bool(complete), edition=edition)
        else:
            event = db.execute("""SELECT f.key,f.prompt,f.n,c.event FROM calendar c JOIN feed_pattern f ON f.target=c.event
                WHERE c.source=? AND f.class=? ORDER BY f.n DESC,f.prompt LIMIT 1""", (source, cls)).fetchone()
            if event:
                title = folded_spelling(event[3])
                same_event = [card for card in cards if title in folded_spelling(card["prompt"])]
                event_card = next((card for card in same_event if card["complete"]), None)
                cards = [card for card in cards if card not in same_event][:1]
                cards.append(event_card or dict(key=event[0], prompt=event[1], records=event[2],
                                                complete=False, edition=edition))
            for card in cards:
                choices.setdefault(card["key"], card)
    if choices or declared:
        offered = list(choices.values())
        offered[1:1] = [card for key, card in declared.items() if key not in choices]
        raise DateChoices(offered[:3],
                          "This wording can describe different groups. Choose the question you want to play.")
    if qualifier and db.execute("SELECT 1 FROM sqlite_master WHERE name='world_class_name'").fetchone():
        if db.execute("SELECT 1 FROM world_class_name WHERE term=? AND preferred=1", (subject,)).fetchone():
            return
    raise DateChoices([], "The event source uses that word for its participants. Specify the group you mean.")
