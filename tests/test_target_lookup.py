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
import target_discovery
import target_lookup
import watched_rules
from graph import ROOT
from test_catalogue import world_fixture


class CurrentTargets(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.pub = self.root / "published.sqlite"
        world_fixture(self.world)
        self.publish()

    def publish(self):
        return catalogue.build(self.world, self.pub, self.root, include_bundled=False, offline=True)

    def test_new_name_in_a_dated_question_retains_the_requested_year(self):
        with closing(catalogue.reader(self.pub)) as db:
            for prompt in (
                "Name a work published by New Studio and released in 2001",
                "Name a work released in 2001 and published by New Studio",
            ):
                with self.assertRaises(catalogue.UnverifiedRule) as unknown:
                    catalogue.match_question(db, prompt)
                req = target_lookup.Request.decode(unknown.exception.key)
                self.assertEqual(req.rule(987), "during:date-10-577-2001-2001+w-10-123-987")
                self.assertEqual(req.term, "new studio")

    def test_unknown_place_keeps_its_geographic_path_and_date(self):
        with closing(sqlite3.connect(self.world)) as world:
            world.execute(
                "INSERT INTO property VALUES(131,'located in the administrative territorial entity')"
            )
            world.commit()
        self.publish()
        with closing(catalogue.reader(self.pub)) as db:
            for prompt, date in [
                ("Name a work located in New Harbour", ""),
                ("Name a work released in 2001 and located in New Harbour", "date-10-577-2001-2001"),
            ]:
                with self.assertRaises(catalogue.UnverifiedRule) as unknown:
                    catalogue.match_question(db, prompt)
                req = target_lookup.Request.decode(unknown.exception.key)
                self.assertEqual((req.prop, req.mode, req.date, req.term), (131, "g", date, "new harbour"))

    def test_specific_relationship_takes_precedence_over_the_word_in(self):
        with closing(sqlite3.connect(self.world)) as world:
            world.executemany(
                "INSERT INTO property VALUES(?,?)",
                [(131, "administrative area"), (136, "genre"), (17, "country")],
            )
            world.execute("INSERT INTO triple VALUES(1,136,11)")
            world.commit()
        self.publish()
        with closing(catalogue.reader(self.pub)) as db:
            self.assertEqual(
                catalogue.match_question(db, "Name a work in the genre Studio")["key"], "w-10-136-11"
            )
            with self.assertRaises(catalogue.UnverifiedRule) as unknown:
                catalogue.match_question(db, "Name a work released in 2001 and in the genre New Wave")
            req = target_lookup.Request.decode(unknown.exception.key)
            self.assertEqual(
                (req.mode, req.prop, req.term, req.date), ("w", 136, "new wave", "date-10-577-2001-2001")
            )

    def test_a_new_credit_uses_the_current_target_label_and_keeps_tagged_older_wording(self):
        with closing(sqlite3.connect(self.pub)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM world_target WHERE id=7").fetchone())
            db.execute("INSERT INTO current_target VALUES(7,'Cedar Media')")
            db.execute("INSERT INTO current_target_name VALUES('cedar media',7,1)")
            db.execute("INSERT INTO live_rule VALUES('w-10-123-7','release',1,2,x'00')")
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            source = catalogue.match_question(db, "Name a work published by Cedar Media")
            self.assertEqual(
                (source["key"], source["prompt"]), ("w-10-123-7", "Name a work published by Cedar Media")
            )
            self.assertEqual(catalogue.match_question(db, "Name a work published by Work 7 (Q7)"), source)
            self.assertIsNone(catalogue.match_question(db, "Name a work published by Different name (Q7)"))

    def test_relationship_refresh_detects_a_member_substitution_with_the_same_count(self):
        key = "w-10-123-11"
        file = self.root / "checked.json.gz"
        members = [1, 2]

        def source(query):
            if "COUNT(" in query:
                return [{"n": {"value": "1"}}]
            return [{"item": {"value": f"http://www.wikidata.org/entity/Q{members.pop(0)}"}}]

        with (
            patch.object(watched_rules, "bindings", side_effect=source),
            patch.object(watched_rules, "named_items", return_value=[("Q1", "First", [], 1)]),
            self.assertRaisesRegex(ValueError, "membership changed"),
        ):
            watched_rules.refresh_cache(key, file)
        self.assertFalse(file.exists())

    def collect(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("ATTACH ? AS world", (str(self.world),))
            db.executescript(target_lookup.SCHEMA)
            db.execute("CREATE TABLE live_rule(key,revision,captured,n,release)")
            target_lookup.collect(db, self.root, False, previous=self.pub)
            watched_rules.collect(db, self.root, False)

    def test_search_nominates_targets_source_names_and_edges_decide_the_bank(self):
        prompt = "Name a work published by Élan"
        with (
            closing(catalogue.reader(self.pub)) as db,
            self.assertRaises(catalogue.UnverifiedRule) as unknown,
        ):
            catalogue.match_question(db, prompt)
        req = target_lookup.Request.decode(unknown.exception.key)
        self.assertEqual((req.cls, req.prop, req.term), (10, 123, "élan"))
        self.assertTrue(watched_rules.follow(self.root, req.key))
        self.assertTrue(watched_rules.due_requests(self.root, self.pub))
        item = lambda i: {"value": f"http://www.wikidata.org/entity/Q{i}"}
        labels = {1001: ("Élan", ["新社"]), 1002: ("Elan Press", []), 1003: ("Élan", [])}
        names = lambda page: [
            ("Q" + str(i), *labels[i], 1) for r in page if (i := int(r["item"]["value"].rsplit("Q", 1)[1]))
        ]

        def facts(query):
            if "GROUP BY" in query:
                return []
            self.assertIn("wd:Q1001", query)
            self.assertNotIn("wd:Q1002", query)
            return [{"target": item(1001)}]

        def members(query):
            self.assertIn("wdt:P123 wd:Q1001", query)
            return [{"n": {"value": "2"}}] if "COUNT(" in query else [{"item": item(i)} for i in (31, 32)]

        with (
            patch.object(target_lookup, "search_ids", return_value=([1001, 1002, 1003], False)),
            patch.object(target_lookup, "page_names", side_effect=names),
            patch.object(target_lookup, "bindings", side_effect=facts),
            patch.object(watched_rules, "bindings", side_effect=members),
            patch.object(
                watched_rules, "named_items", return_value=[("Q31", "One", [], 1), ("Q32", "Two", [], 1)]
            ),
        ):
            self.collect()
        edition = self.publish()["edition"]
        with closing(catalogue.reader(self.pub)) as db, closing(sqlite3.connect(":memory:")) as game:
            self.assertFalse(db.execute("SELECT 1 FROM item WHERE id=1001").fetchone())
            for name in ("Élan", "新社", "Q1001"):
                card = catalogue.match_question(db, "Name a work published by " + name)
                self.assertEqual((card["records"], card["key"]), (2, req.rule(1001)))
            self.assertTrue(any(c["key"] == card["key"] for c in catalogue.search(db, "新社")))
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            bank, meta = catalogue.play(db, game, card["key"], card["prompt"])
            self.assertEqual(meta["answerCount"], 2)
            self.assertEqual(
                [
                    r[0]
                    for r in game.execute("SELECT name FROM kb_member WHERE bank=? ORDER BY name", (bank,))
                ],
                ["One", "Two"],
            )
        marker = {r.key: f for r, f in target_lookup.requests(self.root)}[req.key]
        os.utime(marker, (1, 1))
        with patch.object(target_lookup, "capture", side_effect=OSError("source unavailable")):
            self.collect()
        self.assertEqual(self.publish()["edition"], edition)
        self.assertTrue(marker.with_suffix(".attempt").is_file())
        os.utime(marker.with_suffix(".attempt"), (1, 1))
        with (
            patch.object(target_lookup, "search_ids", return_value=([], False)),
            patch.object(target_lookup, "page_names", return_value=[]),
        ):
            self.collect()
        self.assertNotEqual(self.publish()["edition"], edition)
        with closing(catalogue.reader(self.pub)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM current_target_name WHERE target=1001").fetchone())
            with self.assertRaises(catalogue.RuleWithdrawn):
                catalogue.match_question(db, prompt)

    def test_search_pagination_preserves_symbols_and_explicit_ids_need_no_search(self):
        import json
        import urllib.parse

        seen = []

        def page(url):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(query["search"], ["c++"])
            cursor = int(query.get("continue", [0])[0])
            seen.append(cursor)
            return json.dumps({"search": [{"id": f"Q{cursor + 1}"}], "search-continue": cursor + 50}).encode()

        with patch.object(target_lookup, "fetch", side_effect=page):
            self.assertEqual(target_lookup.search_ids("c++"), ([1, 51, 101], True))
            self.assertEqual(target_lookup.search_ids("q41"), ([41], False))
        self.assertEqual(seen, [0, 50, 100])

    def test_crowded_search_requires_identity_and_suggestions_preserve_the_original_subject(self):
        req = target_lookup.Request(10, 123, "press")
        with closing(sqlite3.connect(self.pub)) as db:
            db.executescript(
                "INSERT INTO current_target VALUES(1001,'Press'); INSERT INTO current_target_name VALUES('press',1001,1);"
            )
            db.execute("INSERT INTO target_lookup VALUES(?,?,1)", (req.key, "[1001]"))
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaisesRegex(date_rules.DateChoices, "Q1001"):
                catalogue.match_question(db, "Name a work published by Press")
            with self.assertRaises(catalogue.UnverifiedRule) as qualified:
                catalogue.match_question(db, "Name a work published by Press (Q1001)")
            self.assertEqual(qualified.exception.key, req.rule(1001))
        with closing(sqlite3.connect(self.pub)) as db:
            db.execute("UPDATE target_lookup SET ids='[]',crowded=0")
            db.execute("INSERT INTO target_related VALUES(?,?,?)", (req.key, req.rule(1001), "w-12-123-1001"))
            db.execute(
                "INSERT INTO current_question VALUES(?,?,?)",
                ("w-12-123-1001", "Name an other published by Press", 3),
            )
            db.commit()
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaises(date_rules.DateChoices) as choices:
                catalogue.match_question(db, "Name a work published by Press")
            self.assertEqual([c["key"] for c in choices.exception.cards], ["w-12-123-1001"])
            self.assertEqual(catalogue.search(db, "works published by Press"), choices.exception.cards)

    def test_distinct_verified_publishers_with_the_same_name_remain_separate_choices(self):
        req = target_lookup.Request(10, 123, "cedar")
        watched_rules.follow(self.root, req.key)
        uri = lambda i: {"value": f"http://www.wikidata.org/entity/Q{i}"}

        def verified(query):
            if "?description" in query:
                return [
                    {"target": uri(i), "description": {"value": name}}
                    for i, name in [(1001, "art publisher"), (1002, "science publisher")]
                ]
            return [{"target": uri(i)} for i in (1001, 1002)]

        def members(query):
            if "COUNT(" in query:
                return [{"n": {"value": "1"}}]
            return [{"item": uri(41 if "wd:Q1001 " in query else 42)}]

        with (
            patch.object(target_lookup, "search_ids", return_value=([1001, 1002], False)),
            patch.object(
                target_lookup,
                "page_names",
                return_value=[("Q1001", "Cedar", [], 1), ("Q1002", "Cedar", [], 1)],
            ),
            patch.object(target_lookup, "bindings", side_effect=verified),
            patch.object(watched_rules, "bindings", side_effect=members),
            patch.object(
                watched_rules,
                "named_items",
                side_effect=lambda page: [
                    (i, "Work " + i, [], 1) for r in page if (i := r["item"]["value"].rsplit("/", 1)[1])
                ],
            ),
        ):
            self.collect()
        self.publish()
        with closing(catalogue.reader(self.pub)) as db:
            with self.assertRaises(date_rules.DateChoices) as ambiguous:
                catalogue.match_question(db, "Name a work published by Cedar")
            self.assertIn("art publisher", str(ambiguous.exception))
            self.assertIn("science publisher", str(ambiguous.exception))
            self.assertEqual({c["key"] for c in ambiguous.exception.cards}, {req.rule(1001), req.rule(1002)})
            for card in ambiguous.exception.cards:
                self.assertEqual(catalogue.match_question(db, card["prompt"]), card)

    def test_background_sample_queues_unseen_target_once_and_never_uses_sample_as_bank(self):
        with closing(sqlite3.connect(self.pub)) as db:
            db.executemany(
                "INSERT INTO scope_name VALUES(?,?,?,?,1)", [(10, str(i), i, str(i)) for i in (1, 2, 3)]
            )
            db.commit()
        counts = [
            {
                "property": {"value": "http://www.wikidata.org/entity/P123"},
                "target": {"value": f"http://www.wikidata.org/entity/Q{i}"},
                "n": {"value": "2"},
            }
            for i in (11, 1001)
        ]
        with patch.object(target_discovery, "bindings", return_value=counts) as source:
            nominated = target_discovery.tick(self.root, self.pub)
            self.assertEqual(nominated, [target_lookup.Request(10, 123, "q1001")])
            self.assertEqual(target_discovery.tick(self.root, self.pub), [])
            self.assertEqual(source.call_count, 1)
        self.assertEqual(list(watched_rules.requests(self.root)), [])
        with closing(catalogue.reader(self.pub)) as db:
            self.assertFalse(
                db.execute(
                    "SELECT 1 FROM current_question WHERE rule=?", (nominated[0].rule(1001),)
                ).fetchone()
            )
