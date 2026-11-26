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

    def test_graph_and_readership_rank_shared_names_against_the_whole_bank(self):
        def work(item, title, links):
            return {"id": f"wd:Q{item}", "name": f"Title {title}", "types": ["work"],
                    "graph_recognition": [0, links]}

        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            source = {"source": "world5m-test", "url": "https://example.org", "terms": {"work": []}}

            def tiers(**kwargs):
                bank, _ = evaluate(graph, game, "Name a work", **kwargs)
                return dict(game.execute("SELECT name,depth FROM kb_member WHERE bank=?", (bank,)))

            # Twelve measured names are too few to put any of these twenty in either tail.
            ingest(graph, dict(source, revision="partial"), [work(i, i, i if i <= 12 else 0) for i in range(1, 21)])
            self.assertEqual(set(tiers().values()), {2})

            rows = [work(i, i, i) for i in range(1, 21)] + [work(21, 4, 4), work(22, 10, 10), work(23, 20, 0)]
            ingest(graph, dict(source, revision="shared"), rows)
            graph_tiers = tiers()
            graph.executescript(attention.SCHEMA)
            graph.execute("INSERT INTO readership_release VALUES('fixture','https://example.org',22)")
            graph.executemany("INSERT INTO readership VALUES(?,?)", [
                (int(row["id"][4:]), row["graph_recognition"][1]) for row in rows if row["graph_recognition"][1]])
            # Reading and link counts have the same ordering. Isolate the readership ranking.
            graph.execute("UPDATE graph_familiarity SET incoming=0")
            readership_tiers = tiers(name_catalogue=graph, name_edition="readership")
            for scores in (graph_tiers, readership_tiers):
                self.assertEqual(scores["Title 4"], 2)   # The two owners may have eight counts together.
                self.assertEqual(scores["Title 5"], 3)   # The missing owner of Title 20 cannot make it rarer.
                self.assertEqual(scores["Title 18"], 2)  # Title 10's combined twenty can outrank it.
                self.assertEqual(scores["Title 19"], 1)
                self.assertEqual(scores["Title 20"], 1)

    def test_estimated_top_score_requires_comparable_evidence_across_the_bank(self):
        prompt = "Name a work"
        for field, signal in (("recognition", "language coverage"), ("graph_recognition", "graph links")):
            with self.subTest(field=field), closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                source = {"source": "works", "url": "https://example.org", "terms": {"work": []}}
                rows = [{"id": str(i), "name": f"Work {i}", "types": ["work"], "depth": 4} for i in range(100)]
                rows[0][field] = [1, 2]
                rows[1]["recall"] = [{"prompt": prompt, "count": 0, "participants": 2000}]
                ingest(graph, dict(source, revision="sparse"), rows)
                old, meta = evaluate(graph, game, prompt)
                self.assertEqual(meta["counts"][5], 0)

                for row in rows[1:]:
                    row[field] = [4, 10]
                ingest(graph, dict(source, revision="measured"), rows)
                bank, meta = evaluate(graph, game, prompt)
                self.assertEqual(meta["counts"][5], 1)
                name, proof = game.execute("SELECT name,evidence FROM kb_member WHERE bank=? AND depth=5", (bank,)).fetchone()
                self.assertEqual(name, "Work 0")
                claim = next(item for item in json.loads(proof) if "familiaritySignals" in item)
                self.assertEqual((claim["rankAtMost"], claim["answerNames"], claim["familiaritySignals"]),
                                 (1, 100, ["word frequency", signal]))
                self.assertFalse(game.execute("SELECT 1 FROM kb_member WHERE bank=? AND depth=5", (old,)).fetchone())
                rows[2][field] = rows[0][field]
                ingest(graph, dict(source, revision="tie"), rows)
                self.assertEqual(evaluate(graph, game, prompt)[1]["counts"][5], 0)

    def test_cross_register_conflicts_and_native_ids_do_not_double_count_readership(self):
        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            graph.executescript(attention.SCHEMA)
            graph.execute("INSERT INTO readership_release VALUES('release','https://example.org/rank',5)")
            graph.executemany("INSERT INTO readership VALUES(?,?)", [(i, i * 100) for i in range(1, 6)])
            for source, pairs in [("one", [("aa", 1), ("bb", 2), ("cc", 3), ("dd", 4), ("wd:Q2", None)]),
                                  ("two", [("aa", 5), ("ee", 3)])]:
                rows = [{"id": eid, "name": eid, "types": ["work"],
                         **({"readershipItem": {"item": f"Q{item}", "url": "https://example.org/id"}} if item else {})}
                        for eid, item in pairs]
                ingest(graph, {"source": source, "revision": "1", "url": "https://example.org",
                               "terms": {"work": []}, "census": {"work": len(rows)}}, rows)
            for scope, expected in [(None, {"dd", "wd:Q2"}), ("one", {"aa", "cc", "dd", "wd:Q2"})]:
                plan = {"class": "work", **({"register": scope} if scope else {})}
                _, meta = evaluate(graph, game, "Name a work", plan, name_catalogue=graph)
                self.assertEqual(meta["readership"]["coveredAnswers"], len(expected))
                self.assertEqual({r[0] for r in graph.execute("SELECT id FROM answer_views")}, expected)

    def test_exact_title_shared_with_another_valid_alias_earns_the_common_score(self):
        prompt = "Name a work"
        source = {"source":"works","revision":"1","url":"https://example.org","terms":{"work":[]}}
        works = [{"id":"rare","name":"Signal","aliases":["Signal (radio play)"],"types":["work"],
                  "recall":[{"prompt":prompt,"count":3,"participants":1000}]},
                 {"id":"common","name":"The Signal Album","aliases":["Signal"],"types":["work"],
                  "recall":[{"prompt":prompt,"count":550,"participants":1000}]}]
        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            ingest(graph,source,works)
            bank,_ = evaluate(graph,game,prompt)
            decisions = {term:(entity,depth) for term,entity,depth in game.execute(
                "SELECT term,entity,depth FROM kb_answer_key WHERE bank=? AND form=0",(bank,))}
            self.assertEqual(decisions["signal"],("",1))
            self.assertEqual(decisions["signal (radio play)"],("rare",4))
            self.assertEqual(decisions["the signal album"],("common",1))

    def test_readership_cannot_promote_a_shared_title_past_a_survey_of_one_owner(self):
        prompt = "Name a film"
        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            shared = {1:"Shared A", 101:"Shared A", 102:"Shared A", 2:"Shared B", 103:"Shared B"}
            films = [{"id":f"wd:Q{i}", "name":shared.get(i,f"Film {i}"), "types":["film"],
                      "graph_recognition":[0,3]} for i in range(1,104)]
            for i in (1,103):  # Exercise both orders of measured and unmeasured owners.
                films[i-1]["recall"] = [{"prompt":prompt,"count":200,"participants":1000}]
            ingest(graph,{"source":"world5m-films","revision":"1","url":"https://example.org",
                          "terms":{"film":[]}}, films)
            graph.executescript(attention.SCHEMA)
            graph.execute("INSERT INTO readership_release VALUES('one','https://example.org',103)")
            graph.executemany("INSERT INTO readership VALUES(?,?)",
                              ((i,1 if i in shared else i) for i in range(1,104)))
            bank, _ = evaluate(graph,game,prompt,name_catalogue=graph)
            tiers = dict(game.execute("SELECT name,depth FROM kb_member WHERE bank=?", (bank,)))
            self.assertEqual(tiers["Shared A"], 2)
            self.assertEqual(tiers["Shared B"], 2)
            self.assertEqual(tiers["Film 3"], 4)

    def test_top_readership_estimates_respect_missing_owners_ties_surveys_and_the_combined_cap(
        self,
    ):
        import json

        prompt = "Name a film"
        films = [
            {
                "id": f"wd:Q{i}",
                "name": f"Film {i}",
                "types": ["film"],
                "graph_recognition": [0, 3],
            }
            for i in range(1, 201)
        ]
        films[0]["recall"] = [{"prompt": prompt, "count": 150, "participants": 300}]
        # Another name qualifies through the existing word-frequency estimator.
        films[4].update(recognition=[1.3, 2], depth=4)
        with (
            closing(open_graph(":memory:")) as graph,
            closing(sqlite3.connect(":memory:")) as game,
        ):
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            ingest(
                graph,
                {
                    "source": "films",
                    "revision": "1",
                    "url": "https://example.org",
                    "terms": {"film": []},
                },
                films,
            )
            graph.executescript(attention.SCHEMA)
            graph.execute(
                "INSERT INTO readership_release VALUES('week','https://example.org/qrank',200)"
            )
            graph.executemany(
                "INSERT INTO readership VALUES(?,?)",
                ((i, 2 if i in (2, 3) else i) for i in range(1, 201)),
            )

            def compile(edition):
                bank, meta = evaluate(
                    graph, game, prompt, name_catalogue=graph, name_edition=edition
                )
                rare = {
                    name: json.loads(proof)
                    for name, proof in game.execute(
                        "SELECT name,evidence FROM kb_member WHERE bank=? AND depth=5",
                        (bank,),
                    )
                }
                self.assertLessEqual(len(rare), 2)
                return rare, meta["readership"]["topTierEstimates"]

            rare, count = compile("complete")
            self.assertEqual(count, 2)
            self.assertEqual(set(rare), {"Film 2", "Film 3"})
            for entries in rare.values():
                row = next(p for p in entries if "readershipViews" in p)
                self.assertEqual(
                    (
                        row["readershipViews"],
                        row["rankAtMost"],
                        row["answerNames"],
                        row["revision"],
                    ),
                    (2, 3, 200, "week"),
                )
            graph.execute("UPDATE readership SET views=2 WHERE id=4")
            self.assertEqual(compile("tie-exceeds-budget"), ({}, 0))
            graph.execute("DELETE FROM readership WHERE id>=198")
            rare, count = compile("incomplete")
            self.assertEqual(count, 0)
            self.assertNotIn("Film 2", rare)
            self.assertNotIn("Film 1", rare)

    def test_shared_readership_is_combined_before_awarding_a_rarity_bonus(self):
        films = [
            {
                "id": f"wd:Q{i}",
                "name": "Shared" if i <= 3 else f"Film {i}",
                "types": ["film"],
                "graph_recognition": [0, 3],
            }
            for i in range(1, 203)
        ]
        with (
            closing(open_graph(":memory:")) as graph,
            closing(sqlite3.connect(":memory:")) as game,
        ):
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            ingest(
                graph,
                {
                    "source": "films",
                    "revision": "1",
                    "url": "https://example.org",
                    "terms": {"film": []},
                },
                films,
            )
            graph.executescript(attention.SCHEMA)
            graph.execute(
                "INSERT INTO readership_release VALUES('week','https://example.org/qrank',202)"
            )
            # Each shared owner looks rarer than any individual rival. Together
            # they have 30 views and fall outside the top two percent.
            graph.executemany(
                "INSERT INTO readership VALUES(?,?)",
                ((i, 10 if i <= 3 else 10 + i) for i in range(1, 203)),
            )
            bank, meta = evaluate(graph, game, "Name a film", name_catalogue=graph)
            tiers = dict(
                game.execute("SELECT name,depth FROM kb_member WHERE bank=?", (bank,))
            )
            self.assertLess(tiers["Shared"], 5)
            self.assertEqual(
                (
                    tiers["Film 4"],
                    tiers["Film 5"],
                    meta["readership"]["topTierEstimates"],
                ),
                (5, 5, 2),
            )

    def test_counts_entity_surveys_separately_and_team_choices_once_for_shared_titles(self):
        prompt = "Name a film"
        with closing(open_graph(":memory:")) as graph, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            films = [{"id":f"wd:Q{i}", "name":f"Film {i}", "types":["film"]} for i in range(1,101)]
            films.append({"id":"wd:Q101","name":"FILM 1","types":["film"]})
            survey = [{"prompt":prompt,"count":6,"participants":1000}]
            films[0]["recall"] = films[-1]["recall"] = survey
            source = {"source":"films","revision":"1","url":"https://example.org","terms":{"film":[]}}
            ingest(graph,source,films)
            def compile():
                bank,meta = evaluate(graph,game,prompt)
                tier, = game.execute("SELECT depth FROM kb_member WHERE bank=? AND lower(name)='film 1'",(bank,)).fetchone()
                return tier,meta
            # Each film clears the two-percent threshold individually. Either film can
            # prompt the same playable answer, so those estimates cannot certify tier five.
            self.assertEqual(compile()[0],4)
            del films[0]["recall"], films[-1]["recall"]
            ingest(graph,{**source,"revision":"2"},films)
            _, meta = compile()
            game.execute("CREATE TABLE rounds(id INTEGER PRIMARY KEY,phase TEXT)")
            game.executemany("INSERT INTO rounds VALUES(?,'locked')",[(1,),(2,),(3,)])
            cohort = meta["cohort"]
            choices = ["Film 1"]*3 + ["FILM 1"]*3 + [f"Film {2+n%99}" for n in range(994)]
            game.executemany("INSERT INTO kb_pick(cohort,team,room,round,choice) VALUES(?,?,?,?,?)",
                             ((cohort,n,str(n%3),1+n%3,choice) for n,choice in enumerate(choices)))
            game.execute("INSERT INTO kb_calibration VALUES(?,?)",(cohort,len(choices)))
            tier,meta = compile()
            self.assertEqual(meta["observedTeams"],1000)
            self.assertEqual(tier,5)
            # Twelve distinct teams used the name. Splitting the casing into two
            # six-pick counts would incorrectly keep it in the top tier.
            game.executemany("UPDATE kb_pick SET choice=? WHERE cohort=? AND team=?",
                             ((name,cohort,n) for n,name in enumerate(["Film 1","FILM 1"]*3, start=6)))
            self.assertEqual(compile()[0],4)

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
