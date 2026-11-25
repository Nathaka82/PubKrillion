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
import entity_labels
import sources
from graph import ROOT
from test_catalogue import world_fixture


class EntityLabels(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.edition = self.root / "catalogue.sqlite"
        world_fixture(self.world)

    def publish(self):
        return catalogue.build(
            self.world, self.edition, self.root / "feeds", offline=True, include_bundled=False
        )

    def test_names_change_the_checker_search_and_alias_conflicts_without_changing_membership(self):
        with closing(sqlite3.connect(self.world)) as world:
            world.execute("""UPDATE entity SET names=names||char(9)||'Work 1 (1999 film)'||
                char(9)||'Work 1 (2020 film)'||char(9)||'Character (1999 film)' WHERE id=1""")
            world.commit()
        self.publish()
        with closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            prompt = "Name a work published by Studio"

            def bank_and_keys(db, prompt):
                card = catalogue.match_question(db, prompt)
                bank, meta = catalogue.play(db, game, card["key"], prompt)
                keys = dict(
                    game.execute("SELECT term,entity FROM kb_answer_key WHERE bank=? AND form=0", (bank,))
                )
                return bank, keys, meta["answerCount"]

            with closing(catalogue.reader(self.edition)) as db:
                old, original, count = bank_and_keys(db, prompt)
                self.assertEqual(original["alias 1"], "wd:Q1")
                entity_labels.request(self.root, db, game, old)
                entity_labels.request(self.root, db, game, old)
            with closing(sqlite3.connect(self.root / "knowledge-requests" / "entity-labels.sqlite")) as queue:
                self.assertEqual(queue.execute("SELECT count(*) FROM wanted").fetchone()[0], 5)
                self.assertEqual(queue.execute("SELECT count(*) FROM requested").fetchone()[0], 1)
            cache = entity_labels.file(self.root)
            cache.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(cache)) as labels:
                labels.executescript(entity_labels.RECORDS + entity_labels.SCHEDULE)
                entity_labels.put(
                    labels,
                    [
                        (1, "Work 1\tAlba\t作品\tAlba (1999)\tWork 1 (film)", 100, "~wikidata"),
                        (7, "Work 7\tAlba", 100, "~wikidata"),
                    ],
                )
                labels.commit()
            fresh = self.publish()
            self.assertEqual(fresh["checkedNameEntities"], 2)
            self.assertEqual(self.publish()["edition"], fresh["edition"])
            with closing(catalogue.reader(self.edition)) as db:
                bank, terms, n = bank_and_keys(db, prompt)
                self.assertEqual(n, count)
                self.assertNotEqual(bank, old)
                self.assertNotIn("alias 1", terms)
                self.assertIsNone(terms["alba"])  # another work, outside this publisher
                self.assertEqual(terms["作品"], "wd:Q1")
                self.assertEqual(terms["work 1 (1999 film)"], "wd:Q1")
                self.assertNotIn("work 1 (2020 film)", terms)
                self.assertNotIn("character (1999 film)", terms)
                self.assertFalse(
                    game.execute("SELECT 1 FROM kb_name WHERE bank=? AND term='alias 1'", (bank,)).fetchone()
                )
                proof = json.loads(
                    game.execute(
                        "SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q1'", (bank,)
                    ).fetchone()[0]
                )
                self.assertTrue(any(p.get("namesChecked") == 100 for p in proof))
                self.assertIn(["Work 1 (1999 film)"], [p.get("qualifiedNames") for p in proof])
                broad, all_terms, _ = bank_and_keys(db, "Name a work")
                self.assertEqual(all_terms["alba"], "")  # both named owners qualify
                self.assertNotIn("alias 1", all_terms)
                self.assertEqual(
                    game.execute(
                        "SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term='alias 1'", (old,)
                    ).fetchone()[0],
                    "wd:Q1",
                )
            with closing(sqlite3.connect(cache)) as labels:
                # A builder starting with a partial cache must carry forward other direct audits.
                labels.execute("DELETE FROM label_record WHERE id=7")
                entity_labels.put(labels, [(1, "Work 1\tNuovo", 200, "~wikidata")])
                labels.commit()
            self.publish()
            with closing(catalogue.reader(self.edition)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM label_record").fetchone()[0], 2)
                _, terms, _ = bank_and_keys(db, prompt)
                self.assertNotIn("作品", terms)
                self.assertNotIn("work 1 (1999 film)", terms)
                self.assertEqual(terms["nuovo"], "wd:Q1")

    def test_new_queue_wakes_the_builder_and_respects_source_backoff(self):
        with closing(sqlite3.connect(":memory:")) as world:
            world.execute("ATTACH DATABASE ? AS world", (str(self.world),))

            def capture(ids, **kwargs):
                return [(item, (item, item + " title", [], 1)) for item in ids]

            with patch.object(sources, "entity_name_records", side_effect=capture):
                entity_labels.refresh(world, self.root, batches=1, size=1)
            self.assertFalse(entity_labels.due(self.root))
            queue = entity_labels.queue_path(self.root)
            queue.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(queue)) as want:
                want.executescript(entity_labels.QUEUE)
                want.execute("INSERT INTO wanted VALUES(7,100)")
                want.commit()
            with closing(sqlite3.connect(entity_labels.file(self.root))) as cache:
                self.assertFalse(entity_labels.due(self.root))
                cache.execute("UPDATE pacing SET earliest=0")
                cache.commit()
                self.assertTrue(entity_labels.due(self.root))
                with patch.object(sources, "entity_name_records", side_effect=capture) as lookup:
                    entity_labels.refresh(world, self.root, batches=2, size=1)
                    self.assertEqual([c.args[0] for c in lookup.call_args_list], [["Q2"], ["Q7"]])
                cache.execute("UPDATE schedule SET next=0")
                cache.commit()
                with (
                    patch.object(sources, "entity_name_records", side_effect=OSError("retry")),
                    patch.object(sources, "retry_wait", return_value=7200),
                    self.assertRaises(OSError),
                ):
                    entity_labels.refresh(world, self.root)
                import time

                earliest = cache.execute("SELECT earliest FROM pacing").fetchone()[0]
                self.assertGreater(earliest, time.time() + 7100)
                queue.touch()
                self.assertFalse(entity_labels.due(self.root))

    def test_incomplete_fields_cannot_retire_observed_names(self):
        for fields in (
            {"labels": []},
            {"labels": {"en": {"language": "es", "value": "Title"}}, "aliases": {}},
            {"labels": {}, "aliases": {"en": {"value": "Title"}}},
        ):
            reply = json.dumps({"entities": {"Q1": {"id": "Q1", **fields}}}).encode()
            with (
                self.subTest(fields=fields),
                patch.object(sources, "fetch", return_value=reply),
                self.assertRaises(ValueError),
            ):
                list(sources.entity_name_records(["Q1"], allow_missing=True))
        reply = b'{"entities":{"Q1":{"id":"Q1","labels":[],"aliases":[]}}}'
        with patch.object(sources, "fetch", return_value=reply):
            self.assertEqual(list(sources.entity_name_records(["Q1"], allow_missing=True)), [("Q1", None)])

    def test_incomplete_name_lookup_retries_the_batch_and_retains_previous_names(self):
        with closing(sqlite3.connect(":memory:")) as db:
            db.execute("ATTACH DATABASE ? AS world", (str(self.world),))

            def response(url):
                from urllib.parse import parse_qs, urlsplit

                query = parse_qs(urlsplit(url).query)
                self.assertEqual(query["props"], ["labels|aliases"])
                return json.dumps(
                    {
                        "entities": {
                            qid: {"id": qid, "labels": {"en": {"value": qid + " name"}}, "aliases": {}}
                            for qid in query["ids"][0].split("|")
                        }
                    }
                ).encode()

            with patch.object(sources, "fetch", side_effect=response):
                entity_labels.refresh(db, self.root, batches=1, size=2)
            with closing(sqlite3.connect(entity_labels.file(self.root))) as cache:
                self.assertEqual(cache.execute("SELECT cursor FROM schedule").fetchone()[0], 2)
                cache.execute("UPDATE schedule SET next=0")
                cache.commit()
                with (
                    patch.object(sources, "fetch", return_value=b'{"entities":{}}'),
                    self.assertRaises(ValueError),
                ):
                    entity_labels.refresh(db, self.root, batches=1, size=2)
                self.assertEqual(list(cache.execute("SELECT id FROM label_record ORDER BY id")), [(1,), (2,)])
                self.assertEqual(cache.execute("SELECT cursor FROM schedule").fetchone()[0], 2)
                self.assertFalse(entity_labels.due(self.root))
        with patch.object(sources, "fetch", return_value=b'{"entities":{"Q1":{"id":"Q1","missing":""}}}'):
            self.assertEqual(list(sources.entity_name_records(["Q1"], allow_missing=True)), [("Q1", None)])
            with self.assertRaises(ValueError):
                list(sources.entity_name_records(["Q1"]))
