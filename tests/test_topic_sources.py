import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import catalogue
import date_rules
import ideas
import topic_sources as topics
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

    def test_topic_to_two_identities_to_verified_playable_answers(self):
        topic = topics.Topic("cedar")
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as request:
                ideas.published_suggestions(db, {"idea": "Cedar", "completeOnly": False})
            self.assertEqual(request.exception.requests, [topic.key])
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
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            for card in cards:
                self.assertEqual(card["records"], 3)
                self.assertEqual(catalogue.match_question(db, card["prompt"])["key"], card["key"])
                bank, _ = catalogue.play(db, game, card["key"], card["prompt"], card["edition"])
                self.assertEqual(game.execute("SELECT count(*) FROM kb_member WHERE bank=?", (bank,)).fetchone()[0], 3)

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
              patch.object(topics, "bindings", side_effect=[[], [broader]]) as source):
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
