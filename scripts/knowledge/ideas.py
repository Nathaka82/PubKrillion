"""Find playable question shapes in the local Wikidata snapshot, using indexed lookups."""

import json
import mmap
import re
import sqlite3
import sys
from collections import Counter
from contextlib import closing
from pathlib import Path

from graph import ROOT, folded_spelling, indefinite_article, norm
from world_index import index_path

STOP = set("a an the of and or for from in on with about something some give me make create question questions round rounds quiz pub theme topic table mixed age ages fun accessible easy hard please".split())


def article(noun):
    return indefinite_article(noun) + " "


def nouns(head):
    """Expand a topic heading using the WordNet noun index already shipped with the quiz."""
    yield head
    folder = ROOT / "node_modules/wordnet-db/dict"
    if not (folder / "index.noun").is_file():
        return
    token = head.lower().replace(" ", "_").encode()
    with (folder / "index.noun").open("rb") as index, mmap.mmap(index.fileno(), 0, access=mmap.ACCESS_READ) as view:
        pos = view.find(b"\n" + token + b" ")
        if pos < 0:
            return
        fields = view[pos+1:view.find(b"\n", pos+1)].split()
        offsets = fields[-int(fields[2]):][:3]
    with (folder / "data.noun").open("rb") as data:
        for offset in offsets:
            data.seek(int(offset))
            row = data.readline().split()
            for name in row[4:4 + 2 * int(row[3], 16):2]:
                yield name.decode().replace("_", " ")


def discover(world, idea):
    from world import Ambiguous, lookup_ids, one
    words = folded_spelling(idea).split()[:45]
    phrases = dict.fromkeys(
        " ".join(words[start:start + size])
        for size in (5, 4, 3, 2, 1) for start in range(len(words) - size + 1)
        if words[start] not in STOP and words[start + size - 1] not in STOP
    )
    # Prefer a whole named entity to an unrelated namesake of one of its words.
    anchors = {}
    covered = set()
    topic_words = set(words) - STOP
    for phrase in phrases:
        if phrase in covered:
            continue
        if topic_words.difference(phrase.split()):
            continue
        try:
            target = one(world, lookup_ids(world, phrase, "target"), phrase)
        except Ambiguous:
            continue
        if not target:
            continue
        anchors.setdefault(target, phrase)
        parts = phrase.split()
        covered.update(" ".join(parts[i:j]) for i in range(len(parts)) for j in range(i+1, len(parts)+1))
        if len(anchors) == 5:
            break

    def label(qid):
        row = world.execute("SELECT name FROM title WHERE id=?", (qid,)).fetchone()
        if row:
            return row[0]
        row = world.execute("SELECT names FROM entity WHERE id=?", (qid,)).fetchone()
        return row[0].split("\t")[0] if row else ""

    candidates = {}
    def offer(text, score):
        if len(text) <= 220:
            candidates[text] = max(score, candidates.get(text, 0))

    for target, phrase in anchors.items():
        title = label(target)
        before = len(candidates)
        weight = len(phrase.split()) * 10
        for predicate, kinds in ((31, False), (106, False), (279, True)):
            sample = world.execute("SELECT s FROM triple WHERE o=? AND p=? LIMIT 8", (target, predicate)).fetchall()
            if len(sample) >= 3:
                singular = phrase[:-1] if phrase.endswith("s") else phrase
                base = singular if norm(title) == singular else phrase
                if base.split()[0].endswith("s") and " of " in base:
                    if not kinds:
                        offer("Name one of the " + base, weight + len(sample))
                    continue
                subject = "type of " + base if kinds else base
                offer("Name " + article(subject) + subject, weight + len(sample) + (5 if kinds else 0))
        # Seek from property to property. A city or country may have millions of incoming links.
        prop = 0
        for _ in range(48):
            row = world.execute("SELECT min(p) FROM triple WHERE o=? AND p>?", (target, prop)).fetchone()
            prop = row[0]
            if prop is None:
                break
            if prop in (31, 106, 279) or not title:
                continue
            row = world.execute("SELECT names FROM property WHERE id=?", (prop,)).fetchone()
            if not row:
                continue
            relation = row[0].split("\t")[0]
            if not re.fullmatch(r"[\w -]{2,45}", relation) or relation.endswith(" ID"):
                continue
            types = Counter()
            for (entity,) in world.execute("SELECT DISTINCT s FROM triple WHERE o=? AND p=? LIMIT 32", (target, prop)):
                classes = {r[0] for r in world.execute("SELECT o FROM triple WHERE s=? AND p=31 LIMIT 8", (entity,))}
                if 5 in classes:  # For people, prefer a relevant occupation when the source supplies one.
                    jobs = {r[0] for r in world.execute("SELECT o FROM triple WHERE s=? AND p=106 LIMIT 12", (entity,))}
                    if jobs:
                        classes = (classes - {5}) | jobs
                types.update(classes)
            for cls, count in types.most_common(2):
                if count < 3:
                    continue
                subject = label(cls)
                if not subject or "(" in subject or len(subject) > 45:
                    continue
                if not subject.isupper():
                    subject = subject[:1].lower() + subject[1:]
                suffix = {57: "directed by ", 50: "written by ", 86: "composed by "}.get(prop)
                wording = (suffix or "whose " + relation + " is ") + title
                if not suffix and relation.endswith(" by"):
                    continue
                offer("Name " + article(subject) + subject + " " + wording, weight + min(count, 18) + 2)
        # Topical articles such as "Cinema of Japan" can have very few incoming facts.
        # Recover the subject and place from the article and require actual records of their relationship.
        heading = re.fullmatch(r"(.+?) of (?:the )?(.+)", title, re.I)
        if len(candidates) == before and heading:
            where = world.execute("SELECT id,name FROM title WHERE term=? LIMIT 2", (norm(heading[2]),)).fetchall()
            if len(where) != 1:
                continue
            place, place_name = where[0]
            for subject in dict.fromkeys(nouns(heading[1].lower())):
                classes = [r[0] for r in world.execute(
                    "SELECT id FROM title WHERE term=? UNION SELECT id FROM entity_alias WHERE term=? LIMIT 6",
                    (norm(subject), norm(subject)))]
                for prop in (495, 17):
                    if not classes:
                        continue
                    marks = ",".join("?" for _ in classes)
                    hits = world.execute(f"""SELECT DISTINCT link.s FROM
                        (SELECT s FROM triple WHERE o=? AND p=? LIMIT 2000) link
                        JOIN triple ty ON ty.s=link.s AND ty.p=31 AND ty.o IN ({marks}) LIMIT 3""",
                        (place, prop, *classes)).fetchall()
                    if len(hits) == 3:
                        relation = " whose country of origin is " if prop == 495 else " in "
                        offer("Name " + article(subject) + subject + relation + place_name, weight + 10)
    return sorted(candidates, key=lambda p: (-candidates[p], len(p), p))[:6]


def published_suggestions(db, request):
    from catalogue import UnverifiedRule, match_question, relationship_choices, search, surprise
    from date_rules import DateChoices
    complete_only = request["completeOnly"]
    if request.get("surprise"):
        return surprise(db, complete_only)
    try:
        direct = match_question(db, request["idea"])
        if not direct and not complete_only and not request.get("direct"):
            edition, version = db.execute("SELECT edition,format FROM publication").fetchone()
            if version >= 26 and db.execute("SELECT 1 FROM world_class_name LIMIT 1").fetchone():
                import topic_sources
                topic = topic_sources.parse(db, request["idea"], bare=True)
                if topic and (topic.cls or db.execute("SELECT 1 FROM source_topic WHERE key=?",(topic.key,)).fetchone()):
                    topic_sources.choices(db,topic,edition)
                local = search(db,request["idea"],False)
                if not local and topic:
                    topic_sources.choices(db,topic,edition)
                return local
    except DateChoices as choices:
        if choices.cards or complete_only:
            request["sourceNote"] = str(choices)
            return [] if complete_only else choices.cards
        raise
    except UnverifiedRule:
        if complete_only:
            return []
        raise
    if not direct:
        return [] if request.get("direct") else search(db, request["idea"], complete_only)
    rules = [direct] if direct["complete"] or not complete_only else []
    if direct["key"].startswith("compare:"):
        from comparisons import alternatives
        rules.extend(alternatives(db, direct, complete_only))
    elif direct["key"].startswith("date-") and not complete_only:
        from dated_links import alternatives
        rules.extend(alternatives(db,direct))
    elif direct["key"].startswith(("w-", "g-", "v-", "f-")) and db.execute("SELECT format FROM publication").fetchone()[0] >= 17:
        return relationship_choices(db, direct, complete_only)
    return rules


if __name__ == "__main__":
    from catalogue import UnverifiedRule, catalogue_path, reader
    from watched_rules import follow
    game = Path(sys.argv[1])
    request = json.load(sys.stdin)
    idea = request["idea"]
    published = catalogue_path(game)
    rules = []
    if published.is_file():
        with closing(reader(published)) as catalogue:
            try:
                rules = published_suggestions(catalogue, request)
            except ValueError as error:
                message = str(error)
                waiting = None
                if isinstance(error, UnverifiedRule) and not request["completeOnly"]:
                    registered = [follow(game.parent,key) for key in error.requests]
                    if all(registered):
                        waiting = catalogue.execute("SELECT edition FROM publication").fetchone()[0]
                        message += " The knowledge builder has been asked to check the source. Try again after its next publication."
                print(json.dumps({"error": message, "awaitingEdition": waiting}))
                sys.exit(0)
    file = index_path(game)
    options = []
    if not rules and not request.get("direct") and not request.get("surprise") and not request["completeOnly"] and file.is_file():
        with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
            options = discover(db, idea)
    print(json.dumps({"local": options, "rules": rules, "sourceNote": request.get("sourceNote","")}))
