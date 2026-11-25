import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
from compiler import evaluate
from graph import ROOT, open_graph
from sources import wikidata
from world import Ambiguous, one, project, resolve, stored_measurements
from world_facts import live_relation, quantities


class WorldQuestions(unittest.TestCase):
    def setUp(self):
        self.world = sqlite3.connect(":memory:", uri=True)
        self.graph = open_graph(":memory:")
        self.game = sqlite3.connect(":memory:")
        for conn in (self.world, self.graph, self.game):
            self.addCleanup(conn.close)
        self.game.executescript((ROOT / "server/knowledge.sql").read_text())
        self.world.executescript("""
            CREATE TABLE triple(s,p,o);
            CREATE TABLE entity(id INTEGER PRIMARY KEY,names);
            CREATE TABLE property(id INTEGER PRIMARY KEY,names);
            CREATE TABLE entity_alias(term,id);
            CREATE TABLE property_alias(term,id);
            CREATE TABLE title(id INTEGER PRIMARY KEY,name,term);
            CREATE TABLE incoming(id INTEGER PRIMARY KEY,n);
            INSERT INTO property VALUES(800,'sound system');
            INSERT INTO property_alias VALUES('sound system',800);
            INSERT INTO entity VALUES(1,'device'),(2,'radio'),(3,'Receiver'),(4,'Other'),
                                     (5,'Stereo'),(6,'Stereo'),(7,'Engineer'),(8,'Lee');
            INSERT INTO entity_alias VALUES('device',1),('device',2),('stereo',5),('stereo',6),('engineer',7);
            INSERT INTO title VALUES(1,'Device','device'),(2,'Radio','radio'),(3,'Super Receiver','super receiver');
            INSERT INTO triple VALUES(2,279,1),(3,31,2),(4,31,1),(3,800,5),(8,106,7),(7,31,28640);
        """)

    def test_generic_properties_canonical_titles_subclasses_and_occupations(self):
        # A performer linked by occupation to an instrument cannot be accepted as the instrument.
        self.world.execute("INSERT INTO triple VALUES(8,106,2)")
        self.world.execute("INSERT INTO property VALUES(801,'different property')")
        self.world.execute("INSERT INTO property_alias VALUES('sound system',801)")
        for wording in (
            "a device with sound system Stereo",
            "a device whose sound system is Stereo",
        ):
            found = resolve(self.world, wording)
            plan = project(self.graph, self.world, found)
            bank, meta = evaluate(self.graph, self.game, wording, plan)
            self.assertEqual(meta["answerCount"], 1)
            self.assertEqual(
                self.game.execute(
                    "SELECT name FROM kb_member WHERE bank=?", (bank,)
                ).fetchone()[0],
                "Super Receiver",
            )
            self.assertEqual(
                self.game.execute(
                    "SELECT count(*) FROM kb_name WHERE bank=? AND term='receiver'",
                    (bank,),
                ).fetchone()[0],
                1,
            )
        self.assertEqual(resolve(self.world, "a device")[0], 1)
        from world import candidates
        sql, args = candidates(1, False)
        self.assertEqual(set(self.world.execute(sql + "SELECT id FROM members", args)), {(3,), (4,)})
        cls = project(self.graph, self.world, resolve(self.world, "an engineer"))
        self.assertEqual(
            evaluate(self.graph, self.game, "an engineer", cls)[1]["answerCount"], 1
        )
        with self.assertRaises(Ambiguous):
            one(self.world, [5, 6], "Stereo")
        self.assertEqual(resolve(self.world, "a device with sound system Q5")[5], 5)

    def test_a_mixed_subclass_requires_more_than_its_weak_inherited_label(self):
        from world import candidates
        for entity in range(20, 24):
            self.world.executemany("INSERT INTO triple VALUES(?,31,?)", [(entity, 2), (entity, 99)])
        sql, args = candidates(1, False)
        self.assertEqual(set(self.world.execute(sql + "SELECT id FROM members", args)), {(4,)})
        self.world.execute("INSERT INTO triple VALUES(3,31,1)")
        self.assertEqual(set(self.world.execute(sql + "SELECT id FROM members", args)), {(3,), (4,)})

    def test_live_sources_keep_evidence_cache_and_pinned_editions(self):
        resolved = resolve(self.world, "a device with sound system Stereo")
        plan = project(self.graph, self.world, resolved)
        old, _ = evaluate(self.graph, self.game, "Device question", plan)
        hits = [
            {
                "item": {"value": "https://www.wikidata.org/entity/Q999"},
                "itemLabel": {"value": "New Receiver"},
            }
        ]
        with patch("world_facts.bindings", return_value=hits) as endpoint:
            live_source = live_relation(self.graph, resolved, plan)
            live_relation(self.graph, resolved, plan)
            self.assertEqual(endpoint.call_count, 1)
        fresh, meta = evaluate(self.graph, self.game, "Device question", plan)
        self.assertNotEqual(old, fresh)
        self.assertEqual(meta["answerCount"], 2)
        latest = {**plan, "edgeSource": live_source}
        verified, filtered = evaluate(self.graph, self.game, "Current devices", latest)
        self.assertEqual(filtered["answerCount"], 1)
        self.assertEqual(
            self.game.execute(
                "SELECT name FROM kb_member WHERE bank=?", (verified,)
            ).fetchone()[0],
            "New Receiver",
        )
        self.assertEqual(
            self.game.execute(
                "SELECT count(*) FROM kb_member WHERE bank=?", (old,)
            ).fetchone()[0],
            1,
        )
        with (
            patch("world_facts.bindings", side_effect=OSError("source unavailable")),
            self.assertRaises(OSError),
        ):
            live_relation(self.graph, resolved, plan, True)
        self.assertEqual(
            evaluate(self.graph, self.game, "Device question", plan)[0], fresh
        )

    def test_ordinary_quantities_preserve_units_and_uncertainty(self):
        def result(i, name, low, high):
            return {
                k: {"value": val}
                for k, val in {
                    "item": "https://www.wikidata.org/entity/Q" + str(i),
                    "itemLabel": name,
                    "amount": str(low),
                    "unit": "https://www.wikidata.org/entity/Q11573",
                    "low": str(low),
                    "high": str(high),
                    "s": "urn:statement:" + str(i),
                }.items()
            }

        with patch(
            "world_facts.bindings",
            side_effect=[
                [],
                [result(90, "Reference", 9, 10)],
                [{"s": {"value": "urn:statement:93"}}],
                [
                    result(91, "Qualifies", 11, 12),
                    result(92, "Overlaps", 8, 12),
                    result(93, "Record only", 50, 50),
                ],
            ],
        ):
            quantities(self.graph, 90, "reference", "length", True)
            quantities(self.graph, 1, "device", "length", False)
        bank, meta = evaluate(
            self.graph, self.game, "Name a device longer than the reference"
        )
        self.assertEqual(meta["answerCount"], 1)
        self.assertEqual(meta["gaps"]["borderline"], 1)
        ref = meta["reference"][0]
        self.assertEqual((ref["low"], ref["high"]), (900, 1000))
        self.assertTrue(
            json.loads(
                self.game.execute(
                    "SELECT evidence FROM kb_member WHERE bank=?", (bank,)
                ).fetchone()[0]
            )
        )

    def test_local_measurements_join_by_entity_across_independent_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            release = Path(tmp) / "measure.sqlite"
            data = sqlite3.connect(release)
            data.executescript("""
                CREATE TABLE measure(dimension,id,low,high);
                CREATE TABLE release(dimension,pid,revision);
                INSERT INTO release VALUES('length','P2043','revision-1');
                INSERT INTO measure VALUES('length',5,10,12),('length',3,14,20),('length',4,8,16);
            """)
            data.close()
            with patch("world.MEASURE_FILE", release):
                self.assertTrue(
                    stored_measurements(
                        self.graph, self.world, 1, "device", 5, "reference", "length"
                    )
                )
            self.world.execute("DETACH DATABASE quantities")
        bank, meta = evaluate(
            self.graph, self.game, "Name a device longer than the reference"
        )
        self.assertEqual(meta["gaps"]["borderline"], 1)
        self.assertEqual(
            self.game.execute(
                "SELECT name FROM kb_member WHERE bank=?", (bank,)
            ).fetchall(),
            [("Super Receiver",)],
        )

    @patch("sources.fetch")
    def test_live_class_reads_past_old_cap_and_preserves_last_complete_release(
        self, fetch_names
    ):
        def names(url):
            ids = parse_qs(urlsplit(url).query)["ids"][0].split("|")
            return json.dumps(
                {
                    "entities": {
                        i: {
                            "id": i,
                            "labels": {
                                "en": {"value": "Item " + str(int(i[1:]) - 10000)}
                            },
                            "aliases": {
                                "en": [{"value": "Alias " + str(int(i[1:]) - 10000)}]
                            },
                        }
                        for i in ids
                    }
                }
            ).encode()

        fetch_names.side_effect = names
        rows = [
            {
                "item": {"value": f"https://www.wikidata.org/entity/Q{10000 + i}"},
                "itemLabel": {"value": f"Item {i}"},
                "names": {"value": f"Alias {i}"},
            }
            for i in range(2501)
        ]
        pages = [rows[i : i + 500] for i in range(0, len(rows), 500)]
        count = [{"n": {"value": str(len(rows))}}]
        with patch("world_facts.bindings", side_effect=(pages+[count])*2):
            wikidata(self.graph, "device", "Q42")
        self.assertEqual(
            self.graph.execute("SELECT count(*) FROM member").fetchone()[0], 2501
        )
        changed = [{"item": {"value": "https://www.wikidata.org/entity/Q13000"}}]
        with patch("world_facts.bindings", side_effect=pages+[count]+pages[:-1]+[changed,count]):
            with self.assertRaisesRegex(ValueError, "changed during pagination"):
                wikidata(self.graph, "device", "Q42")
        self.assertFalse(self.graph.execute("SELECT 1 FROM member WHERE id='wd:Q13000'").fetchone())
        self.assertEqual(
            self.graph.execute(
                "SELECT id FROM entity_name WHERE term='alias 2500'"
            ).fetchone()[0],
            "wd:Q12500",
        )
        with (
            patch(
                "world_facts.bindings",
                side_effect=[pages[0], OSError("page unavailable")],
            ),
            self.assertRaises(OSError),
        ):
            wikidata(self.graph, "device", "Q42")
        self.assertEqual(
            self.graph.execute("SELECT count(*) FROM member").fetchone()[0], 2501
        )
