import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
from compiler import evaluate
from graph import ROOT, ingest, open_graph
from sources import competition, import_file


class KnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.db = open_graph(":memory:")
        self.game = sqlite3.connect(":memory:")
        self.game.executescript((ROOT / "server/knowledge.sql").read_text())

    def tearDown(self):
        self.db.close()
        self.game.close()

    def put(self, key, records, **fields):
        src = {
            "source": key,
            "revision": "1",
            "url": "https://example.org/" + key,
            **fields,
        }
        ingest(self.db, src, records)

    def test_interrupted_publication_rebuilds_all_answer_indexes_once(self):
        self.put("places", [
            {"id": "one", "name": "Montréal", "types": ["place"], "aliases": ["Montreal", "Ville-Marie"]},
            {"id": "two", "name": "京都", "types": ["place"], "aliases": ["Kyoto", "Kyōto"]},
        ], terms={"place": []})
        self.game.execute("""CREATE TEMP TRIGGER interrupt_import BEFORE INSERT ON kb_answer_key
            BEGIN SELECT raise(ABORT, 'interrupted'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "interrupted"):
            evaluate(self.db, self.game, "Name a place")
        self.assertEqual(self.game.execute("SELECT metadata FROM kb_bank").fetchall(), [("null",)])
        self.assertEqual(self.game.execute("SELECT count(*) FROM kb_text").fetchone()[0], 2)
        self.game.execute("DROP TRIGGER interrupt_import")
        bank, meta = evaluate(self.db, self.game, "Name a place")
        self.assertEqual(meta["answerCount"], 2)
        self.assertEqual(self.game.execute("SELECT count(*) FROM kb_text").fetchone()[0], 2)
        self.assertEqual(self.game.execute("SELECT entity FROM kb_text WHERE kb_text MATCH 'Kyoto'").fetchall(), [("two",)])
        for term, owner in [("montréal", "one"), ("montreal", "one"), ("ville-marie", "one"),
                            ("京都", "two"), ("kyoto", "two"), ("kyōto", "two")]:
            self.assertEqual(self.game.execute(
                "SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term=?", (bank, term)
            ).fetchall(), [(owner,)])

    def test_linked_sources_conflicts_unknowns_hierarchy_and_units(self):
        self.put(
            "catalog",
            [
                {
                    "id": s,
                    "name": s,
                    "types": ["ray"],
                    "aliases": ["regional"] if s in ["Large", "Variable"] else [],
                }
                for s in ("Large", "Variable", "Missing", "Record", "Small")
            ],
            terms={"fish": ["fish species"], "pear": []},
            parents=[["ray", "fish"]],
        )

        def measurement(key, lo, hi, unit="cm", basis="typical"):
            return {
                "id": key,
                "name": key,
                "facts": [
                    {
                        "property": "length",
                        "low": lo,
                        "high": hi,
                        "unit": unit,
                        "basis": basis,
                    }
                ],
            }

        self.put(
            "traits",
            [
                measurement("pear", 80, 120, "mm"),
                measurement("Large", 1, 2, "m"),
                measurement("Variable", 20, 22),
                measurement("Record", 100, 100, basis="record"),
                measurement("Small", 1, 5),
            ],
        )
        self.put("second-study", [measurement("Variable", 5, 9)])
        bank, meta = evaluate(self.db, self.game, "Name a fish bigger than a pear")
        self.assertEqual(meta["gaps"], {"missing": 2, "borderline": 1, "outside": 1})
        self.assertEqual(meta["answerCount"], 1)
        proof = json.loads(
            self.game.execute(
                "SELECT evidence FROM kb_member WHERE bank=?", (bank,)
            ).fetchone()[0]
        )
        self.assertTrue(
            any(p.get("source") == "traits" and p.get("low") == 100 for p in proof)
        )
        self.assertEqual(
            self.game.execute(
                "SELECT count(*) FROM kb_group WHERE bank=?", (bank,)
            ).fetchone()[0],
            0,
        )
        self.assertEqual(
            evaluate(self.db, self.game, "Name a fish bigger than a pear")[0], bank
        )

    def test_only_supported_rare_tail_receives_top_score_and_ties_stay_together(self):
        prompt = "Name a colour"
        rows = []
        for i in range(100):
            r = {"id": str(i), "name": "Colour " + str(i), "types": ["colour"]}
            r["depth"] = 5  # A publisher's assertion of rarity is not a measured score.
            if i < 99:
                r["recall"] = [
                    {
                        "prompt": prompt,
                        "count": 0 if i < 2 else 1 if i < 7 else 250,
                        "participants": 1000,
                    }
                ]
            rows.append(r)
        self.put("survey", rows, terms={"colour": []}, census={"colour": 100})
        bank, meta = evaluate(self.db, self.game, prompt)
        self.assertEqual(meta["counts"][5], 2)
        self.assertIn("Complete", meta["coverage"])
        self.assertEqual(
            self.game.execute(
                "SELECT depth FROM kb_member WHERE bank=? AND entity='99'", (bank,)
            ).fetchone()[0],
            2,
        )

    def test_empirical_rarity_respects_first_choice_uncertainty_and_keeps_old_scores(self):
        prompt = "Name a pigment"
        self.put("pigments", [
            {"id": str(i), "name": "Pigment " + str(i), "types": ["pigment"]}
            for i in range(100)
        ], terms={"pigment": []})
        old, meta = evaluate(self.db, self.game, prompt)
        self.game.execute("CREATE TABLE rounds(id PRIMARY KEY,phase)")
        self.game.executemany("INSERT INTO rounds VALUES(?,'locked')", [(1,), (2,), (3,)])
        cohort = meta["cohort"]
        for i in range(600):
            choice = "Pigment " + str(1 if i == 599 else 0)
            self.game.execute("INSERT INTO kb_pick(cohort,team,room,round,choice) VALUES(?,?,?,?,?)",
                              (cohort, i, str(i % 3), 1 + i % 3, choice))
        self.game.execute("INSERT INTO kb_calibration VALUES(?,600)", (cohort,))
        new, scored = evaluate(self.db, self.game, prompt)
        self.assertNotEqual(new, old)
        self.assertEqual(scored["observedTeams"], 600)
        def tier(bank, name):
            return self.game.execute("SELECT depth FROM kb_member WHERE bank=? AND name=?", (bank, name)).fetchone()[0]
        self.assertEqual(tier(old, "Pigment 0"), 2)
        self.assertEqual(tier(new, "Pigment 0"), 1)
        self.assertEqual(tier(new, "Pigment 1"), 4)
        self.assertEqual(tier(new, "Pigment 2"), 4)
        self.assertEqual(scored["counts"][5], 0, "A large tie for rarest must not promote a more common answer.")
        # In a measured cohort, zero and one selections are informative when the
        # rest of the bank really was chosen more often.
        self.game.execute("""UPDATE kb_pick SET choice=CASE WHEN team=599 THEN 'Pigment 1'
            ELSE 'Pigment ' || (2 + team%98) END""")
        ranked, scored = evaluate(self.db, self.game, prompt)
        self.assertEqual(scored["counts"][5], 2)
        self.assertEqual(tier(ranked, "Pigment 0"), 5)
        self.assertEqual(tier(ranked, "Pigment 1"), 5)
        self.assertEqual(tier(new, "Pigment 0"), 1)

    def test_incomplete_release_rolls_back_and_jsonld_links_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            src = {
                "source": "works",
                "revision": "v1",
                "url": "https://example.org/art",
                "file": "works.jsonld",
                "terms": {"painting": [], "urn:creator": ["Ada"]},
                "mapping": {
                    "label": "name",
                    "types": {"Work": "painting"},
                    "relations": {"author": "painted by"},
                },
            }
            (root / "manifest.json").write_text(json.dumps(src))
            (root / "works.jsonld").write_text(
                json.dumps(
                    {
                        "@graph": [
                            {
                                "@id": "urn:work",
                                "@type": ["Work"],
                                "name": [{"@value": "Landscape"}],
                                "author": [{"@id": "urn:creator"}],
                            }
                        ]
                    }
                )
            )
            import_file(self.db, root / "manifest.json")
            self.assertEqual(
                evaluate(self.db, self.game, "Name a painting painted by Ada")[1][
                    "answerCount"
                ],
                1,
            )
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                self.put("works", [], revision="v2", census={"painting": 1})
            self.assertEqual(
                self.db.execute(
                    "SELECT revision FROM source WHERE id='works'"
                ).fetchone()[0],
                "v1",
            )
        with self.assertRaisesRegex(ValueError, "calendar"):
            competition(
                self.db,
                {
                    "key": "cup",
                    "competition": "Cup",
                    "participant": "team",
                    "expected": [2004, 2008],
                    "editions": [[2004, "a", "b"]],
                },
            )
