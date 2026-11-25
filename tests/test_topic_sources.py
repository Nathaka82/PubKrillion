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
            with self.assertRaises(catalogue.UnverifiedRule) as missing:
                catalogue.match_question(db, "Name an œuvre interactive")
            self.assertEqual(missing.exception.key, "type:Q9100")
            with self.assertRaises(catalogue.UnverifiedRule) as tagged:
                catalogue.match_question(db,"Name an interactive work (Q9100)")
            self.assertEqual(tagged.exception.key,"type:Q9100")
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
