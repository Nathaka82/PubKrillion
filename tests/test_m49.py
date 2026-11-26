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
import m49
from graph import ROOT, open_graph
from ideas import published_suggestions


class M49Tests(unittest.TestCase):
    def setUp(self):
        self.export = json.loads(gzip.decompress(m49.SEED.read_bytes()))

    def test_new_area_and_withdrawal_across_languages_without_a_cldr_entry(self):
        with closing(open_graph(":memory:")) as db:
            m49.load(db, self.export)
            initial = db.execute("SELECT count(*) FROM entity WHERE source=?", (m49.SOURCE,)).fetchone()[0]
            for lang, table in self.export["tables"].items():
                header = table[0]
                entry = next(row.copy() for row in table[1:] if row[header.index("ISO-alpha2 Code")] == "CL")
                for field, value in [("M49 Code", "999"), ("ISO-alpha2 Code", "XZ"), ("ISO-alpha3 Code", "XZZ"),
                                     ("Country or Area", "New place " + lang)]:
                    entry[header.index(field)] = value
                table.append(entry)
            m49.load(db, self.export)
            info = db.execute("SELECT aliases FROM entity WHERE id='un-m49:999'").fetchone()
            self.assertTrue({"New place AR", "XZ", "XZZ"}.issubset(json.loads(info[0])))
            self.assertEqual(db.execute("SELECT count(*) FROM member WHERE class=?", (m49.SUBJECT,)).fetchone()[0], initial + 1)
            self.export["tables"]["EN"].pop()
            with self.assertRaisesRegex(ValueError, "disagree|different"):
                m49.load(db, self.export)
            self.assertTrue(db.execute("SELECT 1 FROM entity WHERE id='un-m49:999'").fetchone())
            for lang in m49.LANGUAGES[1:]:
                self.export["tables"][lang].pop()
            m49.load(db, self.export)
            self.assertIsNone(db.execute("SELECT 1 FROM entity WHERE id='un-m49:999'").fetchone())

    def test_scoped_regional_bank_and_names_with_corroborating_identifiers(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            publication = root / "catalogue.sqlite"
            with patch.object(catalogue, "bootstrap", side_effect=lambda db: m49.load(db, self.export)):
                catalogue.build(None, publication, root, offline=True)
            with closing(catalogue.reader(publication)) as db, closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                alternatives = published_suggestions(db, {"idea": "Name a country in South America", "completeOnly": True})
                self.assertEqual(len(alternatives), 1)
                card = alternatives[0]
                self.assertIn("UN M49", card["prompt"])
                bank, meta = catalogue.play(db, game, card["key"], card["prompt"])
                self.assertEqual(meta["answerCount"], card["records"])
                for raw, owner in [("bolivia", "068"), ("chilE", "152"), ("guf", "254")]:
                    self.assertEqual(game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term=?",
                                                  (bank, raw.lower())).fetchone(), ("un-m49:" + owner,))
                self.assertIsNone(game.execute("SELECT 1 FROM kb_answer_key WHERE bank=? AND term='kenya'", (bank,)).fetchone())
                evidence = json.loads(game.execute("SELECT evidence FROM kb_member WHERE bank=? AND entity='un-m49:068'", (bank,)).fetchone()[0])
                self.assertTrue(any(p.get("url") == m49.CLDR and "Bolivia" in p["names"] for p in evidence))
        self.export["codes"]["BO"]["_numeric"] = "999"
        with closing(open_graph(":memory:")) as db:
            m49.load(db, self.export)
            self.assertIsNone(db.execute("SELECT 1 FROM entity_name_proof WHERE id='un-m49:068'").fetchone())

    def test_interrupted_source_refresh_retains_verified_edition(self):
        with tempfile.TemporaryDirectory() as folder, closing(open_graph(":memory:")) as db:
            cache = Path(folder) / "cache.gz"
            with patch.object(m49, "fetch", return_value=b'<table id="downloadTableEN"><tr><td>partial'):
                m49.refresh(db, cache, offline=False)
            self.assertFalse(cache.exists())
            self.assertTrue(db.execute("SELECT 1 FROM entity WHERE id='un-m49:152'").fetchone())
            with patch.object(m49, "download", return_value=self.export["tables"]), patch.object(
                m49, "cldr_names", side_effect=OSError("temporarily unavailable")):
                m49.refresh(db, cache, offline=False)
            self.assertEqual(json.loads(gzip.decompress(cache.read_bytes())), self.export)
