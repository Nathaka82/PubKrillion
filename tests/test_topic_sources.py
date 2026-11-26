import gzip
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import catalogue
import class_updates
import date_rules
import ideas
import topic_sources as topics
import source_vocabulary as vocabulary
import target_lookup
import watched_rules
from graph import ROOT
from test_catalogue import world_fixture


def binding(kind, number):
    return {"value": f"http://www.wikidata.org/entity/{kind}{number}"}


class SourceTopics(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.pub = self.root / "published.sqlite"
        world_fixture(self.world)
        with closing(sqlite3.connect(self.world)) as db:
            db.executemany("INSERT INTO property VALUES(?,?)", [(50, "author"), (1889, "different from")])
            db.commit()
        self.publish()

    def publish(self):
        return catalogue.build(self.world, self.pub, self.root, include_bundled=False, offline=True)

    def test_unfamiliar_subject_verifies_its_class_then_automatically_builds_its_bank(self):
        prompt = "Name a prism device"
        topic = topics.Topic("prism device", subject_lookup="type")
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as waiting:
                catalogue.match_question(db, prompt)
            self.assertEqual(waiting.exception.requests, [topic.key])
            self.assertTrue(watched_rules.follow(self.root, topic.key))
            self.assertIsNone(catalogue.match_question(db, prompt + " that weighs less than a pear"))
            self.assertEqual(ideas.published_suggestions(db, {"idea": prompt, "completeOnly": True}), [])
            with self.assertRaises(catalogue.UnverifiedRule) as waiting:
                catalogue.match_question(db, "Name a type of prism device")
            self.assertEqual(waiting.exception.key, topics.Topic("prism device", subject_lookup="kind").key)
        forms = {i: [i, "Prism device", []] for i in (901, 902)}
        with (closing(sqlite3.connect(":memory:")) as stage,
              patch.object(topics, "named_candidates", return_value=(forms, False)),
              patch.object(topics, "bindings", return_value=[{"subject": binding("Q", i),
                  "members": {"value": str(i == 902).lower()}, "children": {"value": "false"},
                  "classification": {"value": "false"}} for i in forms]),
              patch.object(vocabulary, "capture", side_effect=AssertionError("already verified subject names"))):
            stage.execute("ATTACH DATABASE ? AS world", (str(self.world),))
            stage.executescript(topics.SCHEMA)
            result, = topics.collect(stage, self.root, False)
            self.assertTrue(result["scopedIdentity"])
            self.assertEqual(result["rows"], [forms[902]])
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule):
            catalogue.match_question(db, prompt)
        (key, file), = class_updates.registered(self.root)
        self.assertEqual(key, "type:Q902")
        rows = [{"item": binding("Q", i)} for i in (911, 912)]
        with (patch.object(class_updates, "bindings", side_effect=[rows, [{"n": {"value": "2"}}]] * 2),
              patch.object(class_updates, "named_items", return_value=[("Q911", "Another Prism", [], 0),
                         ("Q912", "Small Prism", [], 0)])):
            class_updates.advance(key, file)
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            card = catalogue.match_question(db, prompt)
            self.assertEqual((card["key"], card["records"], card["complete"]), (key, 2, False))
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            bank, _ = catalogue.play(db, game, key, card["prompt"])
            self.assertEqual({r[0] for r in game.execute("SELECT entity FROM kb_member WHERE bank=?", (bank,))},
                             {"wd:Q911", "wd:Q912"})

    def test_unfamiliar_credit_subject_preserves_creator_and_truncated_search_cannot_select_a_class(self):
        topic = topics.Topic("prism devices", subject_lookup="subject")
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule) as waiting:
            ideas.published_suggestions(db, {"idea": "prism devices by Cedar", "completeOnly": False})
        self.assertEqual(waiting.exception.requests, [topic.key])
        with patch.object(topics, "named_candidates", return_value=({}, True)), patch.object(topics, "bindings") as source:
            saved = topics.capture(topic)
            self.assertTrue(saved["crowded"])
            source.assert_not_called()
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO source_topic VALUES(?,?,0,0)", (topic.key, "[901]"))
            db.commit()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule) as waiting:
            ideas.published_suggestions(db, {"idea": "prism devices by Cedar", "completeOnly": False})
        self.assertEqual(waiting.exception.requests, [topics.Topic("cedar", 901).key])

    def test_genre_only_subject_rechecks_old_misses_and_publishes_a_verified_work_question(self):
        topic = topics.Topic("prism genre", subject_lookup="type")
        prompt, rule = "Name a prism genre", "w-10-136-902"
        watched_rules.follow(self.root, topic.key)
        file = topics.cache_path(self.root, topic.key)
        with patch.object(topics, "named_candidates", return_value=({}, False)):
            old = topics.capture(topic)
        old.pop("classificationOnly")
        old["discoveryVersion"] = 4
        old["revision"] = topics.digest([old[k] for k in
            ("key", "rows", "crowded", "proposals", "descriptions", "scopedIdentity", "subclassOnly")])
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(gzip.compress(json.dumps(old).encode()))
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule) as waiting:
            catalogue.match_question(db, prompt)
        self.assertEqual(waiting.exception.key, topic.key)
        forms = {i: [i, "Prism genre", []] for i in (901, 902)}
        usages = [{"subject": binding("Q", i), "members": {"value": "false"}, "children": {"value": "false"},
                   "classification": {"value": str(i == 902).lower()}} for i in forms]
        proposal = {"property": binding("P", 136), "class": binding("Q", 10), "n": {"value": "7"}}
        with patch.object(topics, "named_candidates", return_value=(forms, False)):
            with patch.object(topics, "bindings", return_value=usages):
                self.assertEqual(topics.capture(topics.Topic(topic.term, subject_lookup="kind"))["rows"], [])
            with patch.object(topics, "bindings", side_effect=[usages, [proposal]]):
                credit_subject = topics.capture(topics.Topic(topic.term, subject_lookup="subject"))
                self.assertEqual(credit_subject["rows"], [forms[902]])
                self.assertEqual(credit_subject["proposals"], [[10, 136, 902, 7]])
            with (closing(sqlite3.connect(":memory:")) as stage,
                  patch.object(topics, "bindings", side_effect=[usages, [proposal]]),
                  patch.object(vocabulary, "capture", return_value={"P136": ["genre", []]})):
                stage.execute("ATTACH ? AS world", (str(self.world),))
                stage.executescript(topics.SCHEMA + target_lookup.SCHEMA)
                stage.execute("CREATE TABLE live_rule(key,revision,captured,n,release)")
                result, = topics.collect(stage, self.root, False)
                self.assertEqual(result["classificationOnly"], [902])
                self.assertEqual(list(class_updates.registered(self.root)), [])
                target_lookup.collect(stage, self.root, False, topic_captures=[result])
                self.publish()
                with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule) as pending:
                    catalogue.match_question(db, prompt)
                self.assertEqual(pending.exception.key, rule)
                members = [{"item": binding("Q", i)} for i in (31, 32)]
                with (patch.object(watched_rules, "bindings", side_effect=lambda q:
                        [{"n": {"value": "2"}}] if "COUNT(" in q else members),
                      patch.object(watched_rules, "named_items", return_value=[
                          ("Q31", "First prism", [], 0), ("Q32", "Second prism", [], 0)])):
                    watched_rules.collect(stage, self.root, False)
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            card, = ideas.published_suggestions(db, {"idea": prompt, "completeOnly": False})
            self.assertEqual((card["key"], card["records"]), (rule, 2))
            self.assertIn("in the genre", card["prompt"])
            self.assertEqual(catalogue.match_question(db, card["prompt"])["key"], rule)
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            bank, _ = catalogue.play(db, game, rule, card["prompt"])
            self.assertEqual({r[0] for r in game.execute("SELECT entity FROM kb_member WHERE bank=?", (bank,))},
                             {"wd:Q31", "wd:Q32"})
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("UPDATE live_rule SET n=0 WHERE key=?", (rule,))
            db.commit()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.RuleWithdrawn):
            catalogue.match_question(db, prompt)

    def test_subclass_only_source_offers_an_explicit_question_after_verification(self):
        topic = topics.Topic("work", subject_lookup="type")
        names = {10: [10, "Work", []]}
        child = {"subject": binding("Q", 10), "members": {"value": "false"}, "children": {"value": "true"},
                 "classification": {"value": "false"}}
        with (patch.object(topics, "named_candidates", return_value=(names, False)),
              patch.object(topics, "bindings", return_value=[child])):
            self.assertEqual(topics.capture(topic)["subclassOnly"], [10])
        with (patch.object(topics, "named_candidates", return_value=(names, False)),
              patch.object(topics, "bindings", return_value=[]), self.assertRaisesRegex(ValueError, "did not finish")):
            topics.capture(topic)
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO discovered_subclass_subject VALUES(10)")
            db.execute("INSERT INTO current_class VALUES('type:Q10','checked',100,0,0)")
            db.commit()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule) as pending:
            catalogue.match_question(db, "Name a work")
        self.assertEqual(pending.exception.key, "kind:Q10")
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO current_class VALUES('kind:Q10','checked',101,2,2)")
            db.executemany("INSERT INTO current_member VALUES('kind:Q10',?,?,'[]')", [(81, "Folio"), (82, "Volume")])
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            revised, = ideas.published_suggestions(db, {"idea": "Name a work", "completeOnly": False})
            self.assertEqual((revised["key"], revised["records"]), ("kind:Q10", 2))
            self.assertIn("type of", revised["prompt"])
            self.assertEqual(catalogue.match_question(db, revised["prompt"])["key"], "kind:Q10")

    def test_plural_subjects_use_credited_works_even_with_many_source_meanings(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("INSERT INTO property VALUES(86,'composer'),(272,'production company'),(162,'producer')")
            db.execute("INSERT INTO entity VALUES(80,'Symphony'),(81,'Symphonie'),(90,'Cedar House')")
            db.executemany("INSERT INTO entity VALUES(?,?)",
                           [(i, f"Composition ({i})") for i in range(100, 108)])
            db.executemany("INSERT INTO triple VALUES(?,?,?)",
                           [(1,31,i) for i in range(100,108)] +
                           [(1,31,80),(2,31,80),(3,31,81),(2,31,107),(1,86,90),(2,86,90),
                            (1,272,90),(2,272,90),(3,162,90),(4,162,90)])
            db.commit()
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            def suggest(text):
                return ideas.published_suggestions(db, {"idea": text, "completeOnly": False})
            for subject, cls in [("symphonies", 80), ("compositions", 107)]:
                cards = suggest(subject + " by Cedar House")
                self.assertEqual({c["key"] for c in cards}, {f"w-{cls}-86-90", f"w-{cls}-272-90"})
                for card in cards:
                    self.assertEqual(catalogue.match_question(db, card["prompt"])["key"], card["key"])
            with self.assertRaisesRegex(ValueError, r"more specific subject such as Composition \("):
                suggest("compositions by Another Name")
            companies = {c["key"]: c for c in suggest("works by Cedar House")}
            legacy = 'Name a work with "production company" recorded as Cedar House'
            self.assertEqual(catalogue.match_question(db, legacy)["key"], "w-10-272-90")
            for role, wording, expected in [(272, "from the production company", {1, 2}),
                                           (162, "produced by", {3, 4})]:
                card = companies[f"w-10-{role}-90"]
                self.assertIn(wording, card["prompt"])
                bank, _ = catalogue.play(db, game, card["key"], card["prompt"])
                rows = game.execute("SELECT entity,evidence FROM kb_member WHERE bank=?", (bank,)).fetchall()
                self.assertEqual({eid for eid, _ in rows}, {f"wd:Q{i}" for i in expected})
                self.assertTrue(all(f"#P{role}" in evidence for _, evidence in rows))

    def test_ambiguous_subjects_queue_missing_meanings_and_preserve_qualified_names(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("INSERT INTO entity VALUES(80,'Print (art)'),(81,'Print (publishing)'),(90,'Cedar House')")
            db.executemany("INSERT INTO triple VALUES(?,?,?)",
                           [(1,31,80),(2,31,80),(1,50,90),(2,50,90),(3,31,81),(4,31,81)])
            db.commit()
        self.publish()
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO source_vocabulary VALUES('Q80','Art print',?,0,1)",
                       (json.dumps(["Impression (printmaking)"]),))
            db.execute("INSERT INTO world_class_name VALUES('impression printmaking',80,0)")
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            def queued(key):
                followed.append(key)
                return True
            followed = []
            query = {"idea": "prints by Cedar House", "completeOnly": False}
            self.assertEqual(topics.credit_subjects(db, "impressions"), [80])
            cards = ideas.published_suggestions(db, query, follow_topic=queued)
            self.assertEqual([c["key"] for c in cards], ["w-80-50-90"])
            self.assertEqual(set(followed), {topics.Topic("cedar house", i).key for i in (80, 81)})
            self.assertIn("will check", query["sourceNote"])
            with self.assertRaises(catalogue.UnverifiedRule) as missing:
                ideas.published_suggestions(db, {"idea": "prints by Someone Else", "completeOnly": False})
            self.assertEqual(set(missing.exception.requests), {topics.Topic("someone else", i).key for i in (80, 81)})
            with self.assertRaises(catalogue.UnverifiedRule) as qualified:
                ideas.published_suggestions(db, {"idea": "Print (publishing) by Cedar House", "completeOnly": False})
            self.assertEqual(qualified.exception.requests, [topics.Topic("cedar house", 81).key])

    def test_uncached_credits_preserve_literal_class_names_roles_and_individual_evidence(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("UPDATE entity SET names=names||char(9)||'folio' WHERE id=10")
            db.execute("UPDATE entity SET names=names||char(9)||'.folio' WHERE id=12")
            db.execute("INSERT INTO property VALUES(86,'composer'),(136,'genre')")
            db.execute("INSERT INTO entity VALUES(90,'Copper House'),(95,'story'),(96,'Alternate Writer')")
            db.executemany("INSERT INTO triple VALUES(?,?,?)",
                [(90,31,95),(1,50,90),(2,50,90),(3,50,90),(4,86,90),(5,86,90),
                 (6,50,90),(1,136,95),(2,136,95),(7,136,95),(7,50,96)])
            db.commit()
        self.publish()
        followed = []
        def suggest(db, idea):
            return ideas.published_suggestions(db, {"idea": idea, "completeOnly": False}, follow_topic=followed.append)
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            self.assertEqual(catalogue.match_question(db, "Name a folio")["key"], "type:Q10")
            self.assertEqual(catalogue.match_question(db, "Name a .folio")["key"], "type:Q12")
            self.assertEqual(catalogue.match_question(db, "Name a .folio written by Copper House")["key"], "w-12-50-90")
            cards = suggest(db, "Name a folio by Copper House")
            self.assertEqual({c["key"] for c in cards}, {"w-10-50-90", "w-10-86-90"})
            self.assertEqual(followed, [topics.Topic("copper house", 10).key])
            form, = suggest(db, "Name a story by Copper House")
            self.assertEqual(form["key"], "and:w-10-136-95+w-10-50-90")
            self.assertEqual({c["complete"] for c in [*cards, form]}, {False})
            for card in [*cards, form]:
                self.assertEqual(catalogue.match_question(db, card["prompt"])["key"], card["key"])
                bank, _ = catalogue.play(db, game, card["key"], card["prompt"])
                seen = list(game.execute("SELECT entity,evidence FROM kb_member WHERE bank=?", (bank,)))
                expected = {"w-10-50-90": {1, 2, 3}, "w-10-86-90": {4, 5}, form["key"]: {1, 2}}
                self.assertEqual({eid for eid, _ in seen}, {f"wd:Q{i}" for i in expected[card["key"]]})
                for _, evidence in seen:
                    for _, prop, _ in watched_rules.predicates(card["key"]):
                        self.assertIn(f"#P{prop}", evidence)
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO live_rule VALUES('w-10-50-90','withdrawal',100,0,?)",
                       (gzip.compress(b'{"rows":[]}'),))
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            self.assertEqual([c["key"] for c in suggest(db, "folios by Copper House")], ["w-10-86-90"])
            with self.assertRaises(catalogue.UnverifiedRule):
                suggest(db, "Name a story by Copper House")

    def test_topic_to_two_identities_to_verified_playable_answers(self):
        topic = topics.Topic("cedar")
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as request:
                ideas.published_suggestions(db, {"idea": "Cedar", "completeOnly": False})
            self.assertEqual(request.exception.requests, [topic.key, topics.Topic("cedar", subject_lookup="type").key])
            self.assertEqual(ideas.published_suggestions(db, {"idea": "Cedar", "completeOnly": True}), [])
        self.assertTrue(watched_rules.follow(self.root, topic.key))
        self.assertTrue(watched_rules.due_requests(self.root, self.pub))
        forms = {801: [801, "Cedar", ["Cèdre"]], 802: [802, "Cedar", []]}

        def facts(query):
            if "schema.org/description" in query:
                return [{"target": binding("Q", i), "description": {"value": hint}}
                        for i, hint in [(801, "writer"), (802, "press")]]
            p = 50 if "wd:Q801 " in query else 123
            return [{"property": binding("P", pid), "class": binding("Q", 10), "n": {"value": "4"}}
                    for pid in (p, 1889)]

        def roster(query):
            return ([{"n": {"value": "3"}}] if "COUNT(" in query else
                    [{"item": binding("Q", i)} for i in (31, 32, 33)])

        with (closing(sqlite3.connect(":memory:")) as stage,
              patch.object(topics, "named_candidates", return_value=(forms, False)),
              patch.object(topics, "bindings", side_effect=facts),
              patch.object(watched_rules, "bindings", side_effect=roster),
              patch.object(watched_rules, "named_items", return_value=[
                  ("Q31", "First", [], 1), ("Q32", "Second", [], 1), ("Q33", "Third", [], 1)])):
            stage.execute("ATTACH ? AS world", (str(self.world),))
            stage.executescript(topics.SCHEMA + target_lookup.SCHEMA)
            stage.execute("CREATE TABLE live_rule(key,revision,captured,n,release)")
            captured = topics.collect(stage, self.root, False, self.pub)
            target_lookup.collect(stage, self.root, False, topic_captures=captured)
            watched_rules.collect(stage, self.root, False)
        self.assertEqual(list(watched_rules.rule_keys(self.root)), [])
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            request = {"idea": "Cedar", "completeOnly": False}
            cards = ideas.published_suggestions(db, request)
            self.assertIn("Q801 (writer)", request["sourceNote"])
            self.assertIn("Q802 (press)", request["sourceNote"])
            self.assertEqual({c["key"] for c in cards}, {"w-10-50-801", "w-10-123-802"})
            for text in ("works by Cedar", "Name a work by Cedar", "works by Q801"):
                abbreviated = {"idea": text, "completeOnly": False}
                chosen = ideas.published_suggestions(db, abbreviated)
                self.assertEqual([c["key"] for c in chosen], ["w-10-50-801"])
                if "Cedar" in text:
                    self.assertIn("Q802 (press)", abbreviated["sourceNote"])
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            for card in cards:
                self.assertEqual(card["records"], 3)
                self.assertEqual(catalogue.match_question(db, card["prompt"])["key"], card["key"])
                bank, _ = catalogue.play(db, game, card["key"], card["prompt"], card["edition"])
                self.assertEqual(game.execute("SELECT count(*) FROM kb_member WHERE bank=?", (bank,)).fetchone()[0], 3)

    def test_many_namesakes_are_checked_per_credit_without_reusing_scoped_identities(self):
        forms = {i: [i, "Cedar", []] for i in range(801, 809)}
        scopes = [(topics.Topic("cedar", 10), 801), (topics.Topic("cedar", 12), 802)]
        def facts(query):
            if "GROUP BY ?target HAVING" in query:
                self.assertIn("VALUES ?credit", query)
                self.assertIn("wd:Q808", query)
                owner = 801 if "wd:Q10 " in query else 802
                return [{"target": binding("Q", owner), "n": {"value": "5"}}]
            cls = 10 if "wd:Q801 " in query else 12
            return [{"property": binding("P", 50), "class": binding("Q", cls), "n": {"value": "5"}}]
        for topic, _ in scopes:
            self.assertTrue(watched_rules.follow(self.root, topic.key))
        with (closing(sqlite3.connect(":memory:")) as db,
              patch.object(topics, "named_candidates", return_value=(forms, False)) as names,
              patch.object(topics, "bindings", side_effect=facts)):
            db.execute("ATTACH DATABASE ? AS world", (str(self.world),))
            db.executescript(topics.SCHEMA)
            topics.collect(db, self.root, offline=False)
            self.assertEqual(names.call_count, 2)
            for topic, expected in scopes:
                saved = topics.read_capture(topics.cache_path(self.root, topic.key), topic.key)
                self.assertTrue(saved["scopedIdentity"])
                self.assertEqual(saved["rows"], [forms[expected]])
                self.assertEqual(saved["proposals"], [[topic.cls, 50, expected, 5]])
        with (patch.object(topics, "named_candidates", return_value=(forms, True)),
              patch.object(topics, "bindings", side_effect=AssertionError("incomplete name search"))):
            self.assertTrue(topics.capture(scopes[0][0])["crowded"])

    def test_explicit_credit_reuses_only_the_matching_role_and_equivalent_form_roster(self):
        topic = topics.Topic("cedar", 10)
        simple = "w-10-50-801"
        compound = "and:w-12-50-801+w-12-7937-10"
        complex_prompt = "Name an other written by Cedar and in the form of Work"
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO source_topic VALUES(?,?,0,0)", (topic.key, "[801,802]"))
            db.executemany("INSERT INTO source_topic_rule VALUES(?,?,?)",
                           [(topic.key, compound, 0), (topic.key, "w-10-57-802", 1)])
            db.executemany("INSERT INTO live_rule VALUES(?,'test',1,2,?)",
                           [(compound, b"fixture"), ("w-10-57-802", b"fixture")])
            db.executemany("INSERT INTO current_question VALUES(?,?,2)",
                           [(simple, "Name a work written by Cedar"), (compound, complex_prompt),
                            ("w-10-57-802", "Name a work directed by Cedar")])
            db.executemany("INSERT INTO work_form_match VALUES(?,?,?)",
                           [(simple, 1, compound), (simple, 2, compound)])
            def keys():
                return [c["key"] for c in topics.verified_credit_choices(db, topic, 50, "edition")]
            self.assertEqual(keys(), [simple])
            db.execute("UPDATE current_question SET n=3 WHERE rule=?", (simple,))
            self.assertEqual(keys(), [compound])
            db.execute("UPDATE current_question SET n=2 WHERE rule=?", (simple,))
            db.execute("DELETE FROM work_form_match WHERE id=1")
            self.assertEqual(keys(), [compound])
            db.execute("UPDATE live_rule SET n=0 WHERE key=?", (compound,))
            self.assertEqual(keys(), [])

    def test_credit_keeps_the_requested_subject_or_offers_a_revised_question(self):
        with closing(catalogue.reader(self.pub)) as db:
            for text in ("Works by Cedar", "Name a work by Cedar"):
                with self.assertRaises(catalogue.UnverifiedRule) as result:
                    ideas.published_suggestions(db, {"idea": text, "completeOnly": False})
                self.assertEqual(result.exception.key, topics.Topic("cedar", 10).key)
            self.assertIsNone(topics.parse(db, "Name a work by Cedar except one", bare=True))
            self.assertIsNone(topics.parse(db, "Name a work published by Cedar", bare=True))
        topic = topics.Topic("cedar", 10)
        broader = {"property": binding("P", 50), "class": binding("Q", 12), "n": {"value": "4"}}
        with (patch.object(topics, "named_candidates", return_value=({801: [801, "Cedar", []]}, False)),
              patch.object(topics, "bindings", side_effect=[[], [], [broader]]) as source):
            saved = topics.capture(topic)
        self.assertIn("wdt:P31/wdt:P279* wd:Q10", source.call_args_list[0].args[0])
        self.assertIn("VALUES ?property", source.call_args_list[1].args[0])
        self.assertEqual(saved["proposals"], [[12, 50, 801, 4]])
        watched_rules.follow(self.root, topic.key)
        _, file = next(topics.requests(self.root))
        watched_rules.replace(file, gzip.compress(json.dumps(saved).encode()))
        self.publish()
        with closing(catalogue.reader(self.pub)) as db, self.assertRaises(catalogue.UnverifiedRule):
            topics.choices(db, topic, "edition")
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("INSERT INTO current_question VALUES('w-12-50-801','Name an other written by Cedar',3)")
            db.execute("INSERT INTO live_rule VALUES('w-12-50-801','r',1,3,?)", (b"example",))
            with self.assertRaises(date_rules.DateChoices) as choose:
                topics.choices(db, topic, "edition")
            self.assertIn("revised question", str(choose.exception))
            self.assertEqual(choose.exception.cards[0]["key"], "w-12-50-801")
            db.execute("UPDATE live_rule SET n=0")
            with self.assertRaises(catalogue.RuleWithdrawn):
                topics.choices(db, topic, "edition")

    def test_search_bounds_and_significant_spelling(self):
        for term in ("c++", "新社", "l'été", "q123"):
            topic = topics.Topic(term)
            self.assertEqual(topics.Topic.decode(topic.key), topic)
        for key in ('topic:["x",0]', 'topic:["new name",-1]', 'topic:["a@b.com",0]', 'topic:["cedar",true]'):
            self.assertFalse(watched_rules.follow(self.root, key))
        with (patch.object(topics, "named_candidates", return_value=({i: [i, "Cedar", []] for i in range(1, 9)}, False)),
              patch.object(topics, "bindings") as source):
            data = topics.capture(topics.Topic("cedar"))
            source.assert_not_called()
            self.assertTrue(data["crowded"])
            self.assertEqual(data["proposals"], [])

    def test_new_class_and_property_check_both_conditions_and_retire_cleanly(self):
        topic = topics.Topic("cedar", 10)
        self.assertTrue(watched_rules.follow(self.root, topic.key))
        request = {"idea": "works by Cedar", "completeOnly": False}
        classified = dict(
            property=binding("P", 50),
            basis=binding("P", 7937),
            **{"class": binding("Q", 9100)},
            n={"value": "9"},
        )

        def names(query):
            self.assertIn("wikibase:WikibaseItem", query)
            self.assertIn("wd:P7937", query)
            self.assertIn("wd:Q9100", query)
            return [
                dict(
                    term=binding(kind, i),
                    field={"value": "http://www.w3.org/2000/01/rdf-schema#label"},
                    text={"value": word, "xml:lang": lang},
                )
                for kind, i, lang, word in (
                    ("P", 7937, "en", "form of creative work"),
                    ("Q", 9100, "en", "interactive work"),
                    ("Q", 9100, "fr", "œuvre interactive"),
                )
            ]

        def verify(query):
            self.assertIn("wdt:P50 wd:Q801", query)
            self.assertIn("wdt:P7937 wd:Q10", query)
            self.assertIn("wd:Q9100", query)
            return (
                [{"n": {"value": "2"}}]
                if "COUNT(" in query
                else [{"item": binding("Q", i)} for i in (61, 62)]
            )

        with (
            closing(sqlite3.connect(":memory:")) as stage,
            patch.object(topics, "named_candidates", return_value=({801: [801, "Cedar", []]}, False)),
            patch.object(topics, "bindings", side_effect=[[], [classified]]),
            patch.object(vocabulary, "bindings", side_effect=names),
            patch.object(watched_rules, "bindings", side_effect=verify),
            patch.object(
                watched_rules, "named_items", return_value=[("Q61", "One", [], 1), ("Q62", "Two", [], 1)]
            ),
        ):
            stage.execute("ATTACH ? AS world", (str(self.world),))
            stage.executescript(topics.SCHEMA + target_lookup.SCHEMA)
            stage.execute("CREATE TABLE live_rule(key,revision,captured,n,release)")
            captured = topics.collect(stage, self.root, False, self.pub)
            target_lookup.collect(stage, self.root, True, topic_captures=captured)
            watched_rules.collect(stage, self.root, False)
        edition = self.publish()["edition"]
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            cards = ideas.published_suggestions(db, request)
            key = "and:w-9100-50-801+w-9100-7937-10"
            self.assertEqual([(c["key"], c["records"]) for c in cards], [(key, 2)])
            self.assertIn("form or genre", request["sourceNote"])
            prompt = cards[0]["prompt"]
            self.assertIn("interactive work", prompt)
            self.assertEqual(catalogue.match_question(db, prompt)["key"], key)
            for question in ("Name an œuvre interactive","Name an interactive work (Q9100)"):
                general = catalogue.match_question(db,question)
                self.assertEqual((general["key"],general["records"],general["complete"]),("type:Q9100",2,False))
            self.assertIsNone(catalogue.match_question(db,"Name a different subject (Q9100)"))
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            bank, _ = catalogue.play(db, game, key, prompt, edition)
            members = game.execute("SELECT entity,evidence FROM kb_member WHERE bank=?", (bank,)).fetchall()
            self.assertEqual({entity for entity, _ in members}, {"wd:Q61", "wd:Q62"})
            for entity, evidence in members:
                urls = {entry.get("url") for entry in json.loads(evidence)}
                self.assertTrue(
                    {f"https://www.wikidata.org/wiki/{entity[3:]}#P{prop}" for prop in (50, 7937)} <= urls
                )
        self.assertEqual(self.publish()["edition"], edition)
        # A dropped property must disappear from reused base tables and spellings too.
        file = vocabulary.cache_file(self.root, "P7937")
        os.utime(file, (1, 1))
        self.assertTrue(watched_rules.due_requests(self.root, self.pub))
        row = ["P7937", None, []]
        from graph import digest

        watched_rules.replace(file, json.dumps({"row": row, "revision": digest(row)}).encode())
        self.assertNotEqual(self.publish()["edition"], edition)
        with closing(catalogue.reader(self.pub)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM predicate WHERE id=7937").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM scope_witness WHERE cls=9100").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM world_property_name WHERE prop=7937").fetchone())
            with self.assertRaises(catalogue.RuleWithdrawn):
                ideas.published_suggestions(db, request)
        _, cache = next(topics.requests(self.root))
        cache.unlink()
        self.publish()
        with closing(catalogue.reader(self.pub)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM item WHERE id=9100").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM world_class_name WHERE cls=9100").fetchone())

    def test_cached_names_keep_their_age_and_a_partial_vocabulary_is_never_published(self):
        t = topics.Topic("cedar", 10)
        old = {"rows": [[801, "Cedar", []]], "crowded": False, "namedAt": 12, "checked": 90}
        with (
            patch.object(topics, "named_candidates") as search,
            patch.object(topics, "bindings", return_value=[]),
        ):
            updated = topics.capture(t, identity=old)
            search.assert_not_called()
        self.assertEqual(updated["namedAt"], 12)
        with patch.object(vocabulary, "bindings", return_value=[{"term": binding("Q", 999)}]):
            with self.assertRaisesRegex(ValueError, "unrequested"):
                vocabulary.capture(["Q10"])
        from sources import fetch, _retry_after
        from urllib.error import HTTPError

        url = "https://example.org/test"
        with (
            patch.dict(_retry_after, {}, clear=True),
            patch(
                "urllib.request.urlopen",
                side_effect=HTTPError(url, 429, "busy", {"Retry-After": "120"}, None),
            ) as send,
        ):
            with self.assertRaises(HTTPError) as reply:
                fetch(url)
            reply.exception.close()
            with self.assertRaisesRegex(OSError, "requested a pause"):
                fetch("https://example.org/other")
            self.assertEqual(send.call_count, 1)
