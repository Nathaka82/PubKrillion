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
import country_words


class CountryWords(unittest.TestCase):
    def test_refresh_keeps_shared_names_and_retains_last_verified_release_on_truncation(self):
        def row(country):
            return {"country": {"value": f"http://www.wikidata.org/entity/Q{country}"},
                    "word": {"value": "New Example", "xml:lang": "en"}}
        rows = [row(123), row(456)]
        with tempfile.TemporaryDirectory() as directory, closing(sqlite3.connect(":memory:")) as db:
            root = Path(directory)
            path = country_words.location(root)
            with patch.object(country_words, "bindings", side_effect=[rows, [{"n": {"value": "2"}}]]):
                country_words.refresh(root)
            before = path.read_bytes()
            with patch.object(country_words.time, "time", return_value=path.stat().st_mtime + 8 * 86400), \
                    patch.object(country_words, "bindings", side_effect=[rows[:1], [{"n": {"value": "2"}}]]):
                with self.assertRaisesRegex(ValueError, "count"):
                    country_words.refresh(root)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(country_words.install(db, root), json.loads(gzip.decompress(before))["revision"])
            self.assertEqual(country_words.prefix(db, "new example films"), ("new example", [123, 456]))
            self.assertIsNone(country_words.prefix(db, "new examples films"))
