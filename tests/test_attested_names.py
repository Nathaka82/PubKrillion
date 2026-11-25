import json
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import attested_names
import catalogue
import class_updates
import watched_rules
from graph import ROOT
from test_catalogue import world_fixture


class RecordedNames(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.output = self.root / "catalogue.sqlite"
        world_fixture(self.world)
        self.claims = {1: ["Native title", "Shared abbreviation", "Alias 7"], 7: ["Shared abbreviation"]}
        self.game = sqlite3.connect(":memory:")
        self.addCleanup(self.game.close)
        self.game.executescript((ROOT / "server/knowledge.sql").read_text())

    def provider(self, query):
        ids = sorted(set(map(int, re.findall(r"wd:Q(\d+)", query))))
        rows = []
        for item in ids:
            identity = {"item": {"value": f"http://www.wikidata.org/entity/Q{item}"}}
            values = self.claims.get(item, [])
            if "COUNT(DISTINCT" in query:
                rows.append({**identity, "n": {"value": str(len(values))}})
            else:
                rows.append(identity)
                for idx, name in enumerate(values):
                    rows.append(
                        {
                            **identity,
                            "property": {"value": "http://www.wikidata.org/prop/P1476"},
                            "s": {"value": f"http://www.wikidata.org/entity/statement/q{item}-a-{idx}"},
                            "name": {"value": name, "xml:lang": "nl"},
                        }
                    )
        return rows

    def publish(self):
        return catalogue.build(self.world, self.output, self.root, offline=True, include_bundled=False)

    def collect(self):
        with closing(catalogue.reader(self.output)) as pub:
            pub.execute("ATTACH DATABASE ? AS world", (str(self.world),))
            attested_names.refresh(pub, self.root, previous=self.output, batches=1)

    def bank(self):
        with closing(catalogue.reader(self.output)) as pub:
            card = catalogue.match_question(pub, "Name a work published by Studio")
            return catalogue.play(pub, self.game, card["key"], card["prompt"])

    def answer(self, bank, name):
        return self.game.execute(
            "SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term=?", (bank, name.lower())
        ).fetchone()

    def test_recorded_name_identifies_only_a_proven_entity_and_old_round_keeps_its_evidence(self):
        self.publish()
        before, _ = self.bank()
        with patch.object(attested_names, "bindings", side_effect=self.provider):
            self.collect()
        edition = self.publish()
        self.assertEqual(edition["recordedNames"], 4)
        self.assertEqual(self.publish()["edition"], edition["edition"])
        enriched, meta = self.bank()
        self.assertEqual(meta["answerCount"], 5)
        self.assertEqual(self.answer(enriched, "Native title"), ("wd:Q1",))
        self.assertIsNone(self.answer(before, "Native title"))
        for name in ("Shared abbreviation", "Alias 7"):
            self.assertEqual(self.answer(enriched, name), (None,))
        proof = json.loads(
            self.game.execute(
                "SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q1'", (enriched,)
            ).fetchone()[0]
        )
        self.assertTrue(
            any(
                row.get("name") == "Native title"
                and row["nameProperty"] == "Title"
                and row["language"] == "nl"
                and row["url"].endswith("/q1-a-0")
                for row in proof
            )
        )
        cache = attested_names.location(self.root)
        cache.unlink()
        self.assertEqual(self.publish()["edition"], edition["edition"])
        with (
            patch.object(attested_names, "bindings", side_effect=ValueError("incomplete response")),
            self.assertRaises(ValueError),
        ):
            self.collect()
        self.assertEqual(self.publish()["edition"], edition["edition"])
        with closing(sqlite3.connect(cache)) as db, db:
            db.execute("UPDATE progress SET next=0,cursor=0")
        self.claims = {}
        with patch.object(attested_names, "bindings", side_effect=self.provider):
            self.collect()
        self.assertEqual(self.publish()["recordedNames"], 0)
        latest, _ = self.bank()
        self.assertIsNone(self.answer(latest, "Native title"))
        self.assertEqual(self.answer(enriched, "Native title"), ("wd:Q1",))

    def test_collision_with_newly_observed_class_member_is_never_scored_as_a_specific_answer(self):
        self.claims = {1: ["Local gallery name"]}
        self.publish()
        with patch.object(attested_names, "bindings", side_effect=self.provider):
            self.collect()
        self.publish()
        first, _ = self.bank()
        self.assertEqual(self.answer(first, "Local gallery name"), ("wd:Q1",))
        watched_rules.follow(self.root, "type:Q10")
        key, file = next(class_updates.registered(self.root))
        page = [{"item": {"value": "http://www.wikidata.org/entity/Q900"}}]
        with (
            patch.object(class_updates, "bindings", side_effect=[page, [{"n": {"value": "1"}}]] * 2),
            patch.object(
                class_updates,
                "named_items",
                return_value=[("Q900", "Another work", ["Local gallery name"], 1)],
            ),
        ):
            class_updates.advance(key, file, pages=2)
        self.publish()
        second, _ = self.bank()
        self.assertEqual(self.answer(second, "Local gallery name"), (None,))
        self.assertEqual(self.answer(first, "Local gallery name"), ("wd:Q1",))

    def test_only_excluded_work_acquires_a_playable_alias(self):
        self.publish()
        first, _ = self.bank()
        self.assertEqual(self.answer(first, "Alias 1"), ("wd:Q1",))
        self.claims = {7: ["Alias 1"]}
        with patch.object(attested_names, "bindings", side_effect=self.provider):
            self.collect()
        self.publish()
        revised, _ = self.bank()
        self.assertEqual(self.answer(revised, "Alias 1"), (None,))
        self.assertEqual(self.answer(revised, "Work 1"), ("wd:Q1",))

    def test_malformed_or_incomplete_results_cannot_remove_an_existing_name(self):
        def changed(query):
            rows = self.provider(query)
            if "COUNT(DISTINCT" in query:
                rows[0]["n"]["value"] = "0"
            return rows

        with (
            patch.object(attested_names, "bindings", side_effect=changed),
            self.assertRaisesRegex(ValueError, "changed"),
        ):
            attested_names.capture([1])

        def unexpected(query):
            rows = self.provider(query)
            rows[1]["s"]["value"] = "http://www.wikidata.org/entity/statement/q7-wrong"
            return rows

        with (
            patch.object(attested_names, "bindings", side_effect=unexpected),
            self.assertRaisesRegex(ValueError, "inconsistent"),
        ):
            attested_names.capture([1])
        with (
            patch.object(attested_names, "bindings", return_value=[]),
            self.assertRaisesRegex(ValueError, "incomplete"),
        ):
            attested_names.capture([1])
