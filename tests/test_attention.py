import gzip
import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import attention
import catalogue
from compiler import evaluate
from graph import ROOT, ingest, open_graph


class ReadershipTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def download(self, rows, *, wrong_digest=False):
        data = gzip.compress(("Entity,QRank\n" + rows).encode())
        checksum = "0"*64 if wrong_digest else hashlib.sha256(data).hexdigest()
        url = attention.BASE + "qrank-20261122.csv.gz"
        path = attention.location(self.root)
        path.with_name("checked").unlink(missing_ok=True)
        with patch.object(attention, "descriptor", return_value=(url,checksum,len(data))), \
             patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            attention.refresh(self.root)
        return checksum

    def test_publication_replaces_a_release_and_preserves_a_verified_edition_after_failed_downloads(self):
        names = [{"id":f"wd:Q{i}","name":f"Work {i}","types":["work"]} for i in range(1,9)]
        (self.root / "names.json").write_text(json.dumps(names))
        (self.root / "source.manifest.json").write_text(json.dumps({
            "source":"works", "revision":"a", "url":"https://example.org", "file":"names.json",
            "terms":{"work":[]}, "census":{"work":len(names)}
        }))
        output = self.root / "published.sqlite"
        def build():
            return catalogue.build(None, output, self.root, offline=True, include_bundled=False)
        first = self.download("Q1,200\nQ2,100\n")
        original = build()
        self.assertEqual(original["readershipEntities"], 2)
        for content, wrong, expected in [("Q3,1\n",True,ValueError), ("Q1,4\nQ1,2\n",False,sqlite3.IntegrityError),
                                        ("Q0,2\n",False,ValueError), ("Q2,0\n",False,ValueError)]:
            with self.subTest(content=content), self.assertRaises(expected):
                self.download(content, wrong_digest=wrong)
            self.assertEqual(attention.release(attention.location(self.root))[0],first)
        self.assertEqual(build()["edition"],original["edition"])
        self.download("Q2,300\n")
        updated = build()
        self.assertNotEqual(updated["edition"],original["edition"])
        with closing(catalogue.reader(output)) as db:
            self.assertEqual(list(map(tuple,db.execute("SELECT * FROM readership"))),[(2,300)])
        attention.location(self.root).unlink()
        names[1]["name"] = "Second"
        (self.root / "names.json").write_text(json.dumps(names))
        manifest = self.root / "source.manifest.json"
        manifest.write_text(manifest.read_text().replace('"revision": "a"','"revision": "b"'))
        restored = build()
        self.assertNotEqual(restored["edition"],updated["edition"])
        self.assertEqual(restored["readershipEntities"],1)

    def test_uses_eligible_entity_ids_keeps_ties_and_never_grants_top_score_from_traffic(self):
        prompt = "Name a work"
        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            def item(i, name):
                return {"id":f"wd:Q{i}","name":name,"types":["work"],"graph_recognition":[0,1]}
            items = [item(i, f"Title {i}") for i in range(1,101)]
            items[0]["recall"] = [{"prompt":prompt,"count":750,"participants":1000}]
            items[98].update(depth=4, recognition=[2.1,3])
            items.extend([item(201,"Title 2"), item(202,"Title 3"),
                          {**item(203,"Title 5"),"graph_recognition":[0,0]}])
            ingest(graph,{"source":"world5m-works","revision":"1","url":"https://example.org",
                          "terms":{"work":[]}},items)
            graph.executescript(attention.SCHEMA)
            graph.execute("INSERT INTO readership_release VALUES('one','https://example.org/rank',100)")
            graph.executemany("INSERT INTO readership VALUES(?,?)",
                [(i,i if i != 10 else 9) for i in range(1,100)] + [(201,500),(203,5),(9999,900000)])
            def compile(edition):
                bank, meta = evaluate(graph,game,prompt,name_catalogue=graph,name_edition=edition)
                return dict(game.execute("SELECT name,depth FROM kb_member WHERE bank=?",(bank,))), meta
            tiers, meta = compile("a")
            self.assertEqual(meta["answerCount"],100)
            self.assertEqual((meta["readership"]["coveredAnswers"],meta["readership"]["fullyCoveredAnswers"]),(99,98))
            self.assertEqual(tiers["Title 1"],1)    # Measured first choices take precedence.
            self.assertEqual(tiers["Title 2"],1)    # The shared name has a popular qualifying owner.
            self.assertEqual(tiers["Title 3"],2)    # One of its owners has no readership observation.
            self.assertEqual(tiers["Title 4"],4)
            self.assertEqual(tiers["Title 5"],3)    # Both owners have pageviews, only one has a graph signal.
            self.assertEqual(tiers["Title 9"],tiers["Title 10"])
            self.assertEqual(tiers["Title 20"],3)
            self.assertEqual(tiers["Title 100"],2)
            self.assertEqual(tiers["Title 99"],1)   # High readership contradicts the two older rarity proxies.
            self.assertEqual(meta["counts"][5],0)
            graph.execute("DELETE FROM readership WHERE id BETWEEN 25 AND 99")
            tiers, _ = compile("b")
            self.assertEqual(tiers["Title 4"],2)    # Too many missing records to rank it in the rare tail.
