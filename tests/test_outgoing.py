import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from test_catalogue import catalogue, watched_rules, world_fixture
from date_rules import DateChoices
from graph import ROOT
from ideas import published_suggestions
import item_values


def statement(prop, target, rank="normal"):
    snak = {"property": prop, "datatype": "wikibase-item", "snaktype": "somevalue"}
    if target:
        snak.update(snaktype="value", datavalue={"type": "wikibase-entityid",
            "value": {"id": target, "entity-type": "item", "numeric-id": int(target[1:])}})
    return {"rank": rank, "mainsnak": snak, "id": "Q1$" + prop + "-" + (target or "unknown")}


class OutgoingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.published = self.root / "catalogue.sqlite"
        world_fixture(self.world)
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("INSERT INTO property VALUES(98,'editor')")
            db.commit()
        self.entity = {"id": "Q1", "type": "item", "lastrevid": 123, "labels":
            {"en": {"language": "en", "value": "Work 1"}}, "aliases": {}, "claims": {
                "P123": [statement("P123", "Q11"), statement("P123", "Q21", "preferred"),
                         statement("P123", "Q22", "preferred"), statement("P123", "Q23", "deprecated")],
                "P98": [statement("P98", "Q22"), statement("P98", None)]}}

    def build(self):
        catalogue.build(self.world, self.published, self.root, offline=True, include_bundled=False)
        return catalogue.reader(self.published)

    def capture(self):
        payload = json.dumps({"entities": {"Q1": self.entity}}).encode()
        def names(ids, *, allow_missing):
            self.assertTrue(allow_missing)
            for qid in ids:
                yield qid, (qid, {"Q21": "Éditions A", "Q22": "Studio B"}[qid], ["Alternate " + qid])
        with patch.object(item_values, "fetch", return_value=payload) as fetch, \
                patch.object(item_values, "entity_name_records", side_effect=names) as labels:
            for prop in ("123", "98"):
                self.assertTrue(watched_rules.follow(self.root, "values:Q1:P" + prop))
                with closing(sqlite3.connect(":memory:")) as db:
                    db.executescript(item_values.SCHEMA)
                    self.assertTrue(item_values.install(db, self.root, False))
            fetch.assert_called_once()
            labels.assert_called_once()

    def test_named_subject_uses_outgoing_values_and_current_best_rank(self):
        request = {"idea": "Name a publisher of Work 1", "completeOnly": False}
        with closing(self.build()) as db:
            queue = []
            card, = published_suggestions(db, request, follow_topic=lambda key: queue.append(key) or True)
            self.assertEqual((card["key"], card["records"], card["complete"]), ("values:Q1:P123", 1, False))
            self.assertEqual(queue, [card["key"]])
            self.assertEqual(published_suggestions(db, dict(request, completeOnly=True)), [])
            with self.assertRaises(DateChoices):
                catalogue.match_question(db, "Name a publisher of Shared title")
        self.capture()
        with closing(self.build()) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            card, = published_suggestions(db, dict(request, completeOnly=True))
            self.assertEqual(catalogue.match_question(db, card["prompt"]), card)
            self.assertEqual((card["records"], card["complete"]), (2, True))
            self.assertIn(card, catalogue.search(db, "Work 1", complete_only=True))
            bank, _ = catalogue.play(db, game, card["key"], card["prompt"])
            records = {r[0]: r[1:] for r in game.execute(
                "SELECT entity,name,evidence FROM kb_member WHERE bank=?", (bank,))}
            self.assertEqual(set(records), {"wd:Q21", "wd:Q22"})
            self.assertEqual(records["wd:Q21"][0], "Éditions A")
            self.assertIn("Q1-P123-Q21", records["wd:Q21"][1])
            self.assertTrue(game.execute("SELECT 1 FROM kb_answer_key WHERE bank=? AND term='alternate q21'", (bank,)).fetchone())
            editor = catalogue.match_question(db, "Name an editor of Work 1")
            self.assertEqual((editor["records"], editor["complete"]), (1, False))

    def test_invalid_refresh_preserves_previous_capture_and_withdrawal_supersedes_archive(self):
        self.capture()
        path = item_values.path_for(self.root, 1)
        previous = path.read_bytes()
        saved = item_values.read(path, 1)
        saved["checked"] = 0
        with patch.object(item_values, "fetch", return_value=b'{"entities":{"Q99":{}}}'):
            with self.assertRaises(ValueError):
                item_values.refresh(path, 1, {"P123"}, saved)
        self.assertEqual(path.read_bytes(), previous)
        self.entity.update(lastrevid=124, claims={"P123": [statement("P123", "Q21", "deprecated")]})
        with patch.object(item_values, "fetch", return_value=json.dumps({"entities":{"Q1":self.entity}}).encode()):
            item_values.refresh(path, 1, {"P123"}, saved)
        with closing(self.build()) as db, self.assertRaises(catalogue.RuleWithdrawn):
            catalogue.match_question(db, "Name a publisher of Work 1")
