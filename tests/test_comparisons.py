import csv
import gzip
import json
import sqlite3
import sys
import tempfile
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import catalogue
import comparisons
import sources
from graph import ROOT, open_graph
from test_catalogue import world_fixture


def length(low, high=None, unit="cm", basis="typical"):
    return {
        "property": "length",
        "low": low,
        "high": low if high is None else high,
        "unit": unit,
        "basis": basis,
    }


class ComparisonTests(unittest.TestCase):
    def test_live_names_include_languages_outside_the_display_preferences(self):
        page = [{"item": {"value": "http://www.wikidata.org/entity/" + qid}} for qid in ("Q1", "Q2")]
        queried = []

        def endpoint(url):
            args = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertNotIn("languages", args)
            queried.append(args["ids"][0])
            if len(queried) == 1:
                raise sources.DownloadTooLarge("split the batch")
            catalog = {
                "Q1": {
                    "labels": {"en": {"value": "First"}, "iu": {"value": "ᐃᓄᒃ"}},
                    "aliases": {"bn": [{"value": "নাম"}]},
                },
                "Q2": {"labels": {"uk": {"value": "Друга назва"}}, "aliases": {"uk": [{"value": "Назва"}]}},
            }
            qid = args["ids"][0]
            return json.dumps({"entities": {qid: catalog[qid]}}).encode()

        with patch.object(sources, "fetch", side_effect=endpoint):
            rows = list(sources.named_items(page))
        self.assertEqual(queried, ["Q1|Q2", "Q1", "Q2"])
        self.assertEqual((rows[0][1], set(rows[0][2]), rows[0][3]), ("First", {"ᐃᓄᒃ", "নাম"}, 3))
        self.assertEqual(rows[1], ("Q2", "Друга назва", ["Назва"], 1))

    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.file = self.root / "catalogue.sqlite"
        self.game = sqlite3.connect(":memory:")
        self.addCleanup(self.game.close)
        self.game.executescript((ROOT / "server/knowledge.sql").read_text())

    def feed(self, key, rows, **meta):
        data = key + ".json"
        (self.root / data).write_text(json.dumps(rows))
        (self.root / (key + ".manifest.json")).write_text(
            json.dumps(
                {"source": key, "revision": "1", "url": "https://example.org/" + key, "file": data, **meta}
            )
        )

    def publish(self, **opts):
        return catalogue.build(
            opts.pop("world", None), self.file, self.root, include_bundled=False, offline=True, **opts
        )

    def names(self, bank):
        return {r[0] for r in self.game.execute("SELECT name FROM kb_member WHERE bank=?", (bank,))}

    def test_geographic_paths_use_the_same_place_class_and_measurement(self):
        world=self.root/"world.sqlite"
        world_fixture(world)
        with closing(sqlite3.connect(world)) as raw:
            raw.executescript("""INSERT INTO entity VALUES(70,'Lake City'),(71,'Old Ward'),(72,'Township'),
                (73,'Country A'),(74,'Elsewhere'),(75,'Carrier'),(76,'Untyped');
                INSERT INTO property VALUES(131,'administrative area'),(17,'country'),(276,'location'),
                    (19,'place of birth'),(915,'filming location'),(840,'narrative location');
                INSERT INTO triple VALUES(71,131,72),(72,131,71),(72,131,70),(70,131,73),
                    (74,17,73),(1,131,71),(2,276,72),(3,131,70),(4,131,74),(5,17,73),(6,131,71),
                    (76,276,71),(1,915,71),(2,840,71),(3,19,71),(6,106,75),(6,19,71);
                UPDATE entity SET names=names||char(9)||'Inscription' WHERE id IN (1,4);""")
            raw.commit()
        self.feed("ruler",[{"id":"wd:Q"+str(i),"name":"Work "+str(i),"types":["wd:Q10"],"facts":[length(n)]}
                           for i,n in [(1,30),(2,12),(3,10),(4,65),(5,35)]],terms={"wd:Q10":["work"]})
        self.publish(world=world)
        world.unlink()
        with closing(catalogue.reader(self.file)) as db:
            def play(prompt):
                c=catalogue.match_question(db,prompt)
                bank,meta=catalogue.play(db,self.game,c["key"],prompt)
                return c,bank,meta
            card,bank,meta=play("Name a work in Lake City")
            self.assertEqual((card["records"],self.names(bank)),(3,{"Work 1","Work 2","Work 3"}))
            proof=json.loads(self.game.execute("SELECT evidence FROM kb_member WHERE bank=? AND name='Work 1'",(bank,)).fetchone()[0])
            steps=next(e["steps"] for e in proof if "steps" in e)
            self.assertEqual([(s["entity"],s["property"],s["target"]) for s in steps],
                [("wd:Q1","P131","wd:Q71"),("wd:Q71","P131","wd:Q72"),("wd:Q72","P131","wd:Q70")])
            self.assertIsNone(self.game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND term='inscription' AND form=0",(bank,)).fetchone()[0])
            picks=catalogue.search(db,"works in Lake City")
            self.assertIn(("Name a work in Country A",5),[(c["prompt"],c["records"]) for c in picks])
            self.assertEqual(self.names(play("Name a work whose country is Country A")[1]),{"Work 5"})
            self.assertEqual(self.names(play("Name a Carrier born in Lake City")[1]),{"Work 6"})
            self.assertEqual(self.names(play("Name a work filmed in Lake City")[1]),{"Work 1"})
            self.assertEqual(self.names(play("Name a work set in Lake City")[1]),{"Work 2"})
            mixed,filtered,_=play("Name a work in Lake City longer than 10 cm")
            self.assertEqual(self.names(filtered),{"Work 1","Work 2"})
            self.assertEqual(catalogue.match_question(db,"Name a work longer than 10 cm in Lake City")["key"],mixed["key"])
            compound,filtered,meta=play("Name a work published by Studio and set in Lake City")
            self.assertEqual(self.names(filtered),{"Work 2"})
            self.assertIn("Studio",meta["playerRule"])
            self.assertIn("Lake City",meta["playerRule"])
            self.assertIsNone(catalogue.match_question(db,"Name a work in Lake City before 1970"))

    def test_relationship_and_measurement_join_independent_feeds_by_id(self):
        records = [
            {"id": code, "name": "Piece "+code, "types": ["exhibit"],
             "aliases": ["Shared engraving"] if code in "AE" else [],
             "edges": [["material","bronze" if code in "ABCD" else "silver"]]}
            for code in "ABCDEF"
        ]
        self.feed("collection",records,census={"exhibit":6},
                  completeFacets={"exhibit":{"material":"Name an exhibit made of {value}"}})
        def measures(values):
            self.feed("observatory",[{"id":c,"name":"Piece "+c,"facts":[length(n)]}
                                     for c,n in values.items()])
        measures(dict(A=35,B=25,C=15,D=10,E=70))
        self.publish()
        prompt = "Name an exhibit made of bronze longer than 10 cm"
        with closing(catalogue.reader(self.file)) as db:
            card = catalogue.match_question(db,prompt)
            self.assertEqual((card["records"],card["complete"]),(3,True))
            reverse = catalogue.match_question(db,"Name an exhibit longer than 10 cm and made of bronze")
            self.assertEqual(reverse["key"],card["key"])
            self.assertIsNone(catalogue.match_question(db,prompt+" made before 1880"))
            bank,meta = catalogue.play(db,self.game,card["key"],prompt)
            self.assertEqual(self.names(bank),{"Piece A","Piece B","Piece C"})
            self.assertIsNone(meta["openRule"])
            self.assertIsNone(self.game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND term='shared engraving' AND form=0",(bank,)).fetchone()[0])
            proof = json.loads(self.game.execute("SELECT evidence FROM kb_member WHERE bank=? AND entity='A'",(bank,)).fetchone()[0])
            self.assertIn(("material","https://example.org/collection"),[(p.get("relation"),p["url"]) for p in proof])
            self.assertIn(("length","https://example.org/observatory"),[(p.get("property"),p["url"]) for p in proof])
            picks = catalogue.search(db,"exhibit made of bronze",complete_only=True)
            self.assertTrue(any(p["key"].startswith("mixed:") and p["records"]==2 for p in picks))
        measures(dict(A=5,B=25,C=15,E=70))
        self.publish()
        with closing(catalogue.reader(self.file)) as db:
            now = catalogue.match_question(db,prompt)
            self.assertEqual((now["key"],now["complete"],now["records"]),(card["key"],False,2))
            new,_ = catalogue.play(db,self.game,now["key"],prompt)
            self.assertEqual(self.names(new),{"Piece B","Piece C"})
            self.assertEqual(self.names(bank),{"Piece A","Piece B","Piece C"})

    def test_small_world_groups_subclasses_and_significant_target_names(self):
        world = self.root / "world.sqlite"
        world_fixture(world)
        with closing(sqlite3.connect(world)) as raw:
            raw.executescript("""INSERT INTO triple VALUES(12,279,10),(6,123,81),(1,123,82),
                    (1,123,83),(2,123,83);
                INSERT INTO entity VALUES(81,'A+ Press'),(82,'A Press'),(83,'Copper Press');
                UPDATE property SET names=names||char(9)||'imprint' WHERE id=123;""")
            raw.commit()
        self.feed("ruler",[{"id":"wd:Q"+str(i),"name":"Work "+str(i),"types":["wd:Q10"],"facts":[length(n)]}
                           for i,n in [(1,60),(2,70),(6,55),(7,10)]],
                  terms={"wd:Q10":["work"]})
        self.publish(world=world)
        world.unlink()
        with closing(catalogue.reader(self.file)) as db:
            small = catalogue.match_question(db,"Name a work whose imprint is A+ Press")
            self.assertEqual((small["key"],small["records"]),("w-10-123-81",1))
            self.assertEqual(catalogue.match_question(db,"Name a Q10 whose P123 is Q82")["records"],1)
            picks = catalogue.search(db,"Copper Press")
            self.assertIn(("w-10-123-83",2),[(p["key"],p["records"]) for p in picks])
            bank,_ = catalogue.play(db,self.game,small["key"],small["prompt"])
            self.assertEqual(self.names(bank),{"Work 6"})
            evidence = self.game.execute("SELECT evidence FROM kb_member WHERE bank=?",(bank,)).fetchone()[0]
            self.assertTrue(all(p in evidence for p in ("Q6#P31","Q12#P279","Q6#P123")))
            self.assertEqual(catalogue.match_question(db,"Name a work published by Studio")["records"],6)
            prompt = "Name a work published by A+ Press longer than 50 cm"
            mixed = catalogue.match_question(db,prompt)
            self.assertEqual(mixed["records"],1)
            self.assertEqual(self.names(catalogue.play(db,self.game,mixed["key"],prompt)[0]),{"Work 6"})
            with self.assertRaises(catalogue.EmptyIntersection):
                catalogue.match_question(db,"Name a work published by A+ Press longer than 60 cm")

    def test_new_regional_alias_cannot_steal_a_previously_unique_excluded_name(self):
        world=self.root/"world.sqlite"
        world_fixture(world)
        self.feed("ruler",[{"id":"wd:Q"+str(i),"name":"Work "+str(i),"types":["wd:Q10"],"facts":[length(n)]}
                           for i,n in [(1,55),(2,5)]],terms={"wd:Q10":["work"]})
        self.publish(world=world)
        rows=[["Q1","Work 1",["Alias 7","New regional name","Live overlap"]],
              ["Q2","Work 2",["Live overlap"]]]
        with closing(sqlite3.connect(self.file)) as db:
            db.execute("INSERT INTO live_rule VALUES(?,?,1,?,?)",("w-10-123-11","new",len(rows),gzip.compress(json.dumps({"rows":rows}).encode())))
            db.commit()
        with closing(catalogue.reader(self.file)) as db:
            prompt="Name a work published by Studio longer than 10 cm"
            card=catalogue.match_question(db,prompt)
            bank,_=catalogue.play(db,self.game,card["key"],prompt)
            self.assertEqual(self.names(bank),{"Work 1"})
            for term,expected in [("alias 7",None),("live overlap",None),("new regional name","wd:Q1")]:
                self.assertEqual(self.game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND term=? AND form=0",(bank,term)).fetchone()[0],expected)

    def test_measured_basis_changes_which_answers_qualify(self):
        records = {
            "A": [length(8), length(40, basis="record")],
            "B": [length(20, basis="maturity-lower-bound"), length(35, basis="record")],
            "C": [length(50, basis="record")],
            "D": [length(25), length(60, basis="record")],
            "E": [length(9, basis="record-lower-bound")],
            "F": [length(7), length(8, basis="record")],
        }
        self.feed(
            "registry",
            [
                {"id": key, "name": "Sample " + key, "types": ["sample"], "facts": facts}
                for key, facts in records.items()
            ],
            census={"sample": 6},
        )
        self.publish()
        with closing(catalogue.reader(self.file)) as db:
            cases = [
                ("longer than .1 m", "BD", "size at maturity"),
                ("that is usually longer than 10 cm", "D", "typical"),
                ("that can grow longer than 10 cm", "ABCD", "recorded maximum"),
                ("with a recorded maximum length under 10 cm", "F", "recorded maximum"),
                ("with a recorded maximum length over Sample A", "ABCDE", "recorded maximum"),
            ]
            for clause, expected, wording in cases:
                with self.subTest(clause=clause):
                    prompt = "Name a sample " + clause
                    card = catalogue.match_question(db, prompt)
                    bank, report = catalogue.play(db, self.game, card["key"], prompt)
                    self.assertEqual(self.names(bank), {"Sample " + s for s in expected})
                    self.assertEqual(card["records"], len(expected))
                    self.assertIn(wording, report["playerRule"])
                    self.assertEqual(card["complete"], clause.endswith("Sample A"))
            options = comparisons.alternatives(
                db, catalogue.match_question(db, "Name a sample longer than 10 cm"), False
            )
            self.assertEqual([row["records"] for row in options], [1, 4])
            self.assertEqual(comparisons.alternatives(db, options[0], False), [])
            self.assertEqual(
                comparisons.alternatives(
                    db, catalogue.match_question(db, "Name a sample longer than 10 cm"), True
                ),
                [],
            )

    def test_fish_refresh_validates_replacements_and_replays_offline(self):
        def projection(release, path):
            with gzip.open(path, "wt", encoding="utf8", newline="") as stream:
                out = csv.writer(stream)
                for code in ("2", "3"):
                    out.writerow(
                        [
                            code,
                            "Example " + code,
                            "Fish " + code,
                            "",
                            "",
                            "",
                            "",
                            12,
                            "TL",
                            "[]",
                            2,
                            9,
                            2,
                            3,
                            24,
                            "TL",
                        ]
                    )
            return 2

        bundled = self.root / "old.csv.gz"
        with gzip.open(bundled, "wt", encoding="utf8", newline="") as stream:
            csv.writer(stream).writerow(
                ["1", "Sample one", "First", "", "", "", "", 15, "TL", "[]", 2, 0, 1, 2]
            )
        cache = self.root / "fish.sqlite"
        with closing(open_graph(":memory:")) as db:
            sources.fish(db, bundled, "25.01")
            self.assertEqual(db.execute("SELECT basis FROM fact").fetchone()[0], "record-lower-bound")
            with (
                patch.object(sources, "fish_release", return_value="26.01"),
                patch.object(sources, "fish_projection", side_effect=projection),
            ):
                sources.refresh_fish(db, cache, False)
            self.assertEqual(
                [r[0] for r in db.execute("SELECT name FROM entity ORDER BY id")], ["Fish 2", "Fish 3"]
            )
            self.assertEqual(
                db.execute("SELECT max(high) FROM fact WHERE basis='record'").fetchone()[0], 24
            )
            with patch.object(
                sources, "fish_release", side_effect=AssertionError("unexpected source call")
            ):
                sources.refresh_fish(db, cache, False)
        with closing(open_graph(":memory:")) as db:
            sources.fish(db, bundled, "25.01")
            sources.refresh_fish(db, cache, True)
            with closing(sqlite3.connect(cache)) as store:
                store.execute("UPDATE capture SET checked=0")
                store.commit()

            def truncated(release, path):
                projection(release, path)
                return 3

            with (
                patch.object(sources, "fish_release", return_value="26.02"),
                patch.object(sources, "fish_projection", side_effect=truncated),
            ):
                sources.refresh_fish(db, cache, False)
            self.assertTrue(db.execute("SELECT revision FROM source").fetchone()[0].startswith("26.01-"))
            self.assertEqual(
                [r[0] for r in db.execute("SELECT name FROM entity ORDER BY id")], ["Fish 2", "Fish 3"]
            )

    def test_native_export_intervals_preserve_bounds_and_reject_incomplete_rows(self):
        self.feed(
            "measure-feed",
            [
                {"code": "a", "label": "Bay A", "min": "1.5", "max": "1.7"},
                {"code": "b", "label": "Bay B", "min": "2", "max": "2.4"},
            ],
            rowMapping={
                "id": "code",
                "name": "label",
                "class": "bay",
                "measures": {
                    "min": {"property": "length", "unit": "km", "basis": "typical", "highColumn": "max"}
                },
            },
        )
        edition = self.publish()["edition"]
        with closing(catalogue.reader(self.file)) as db:
            c = catalogue.match_question(db, "Name a bay longer than 1.6 km")
            bank, report = catalogue.play(db, self.game, c["key"], c["prompt"])
            self.assertEqual(self.names(bank), {"Bay B"})
            self.assertEqual(report["gaps"]["borderline"], 1)
        (self.root / "measure-feed.json").write_text(
            json.dumps([{"code": "a", "label": "Bay A", "min": 1.5, "max": None}])
        )
        with self.assertRaisesRegex(ValueError, "bounds have different counts"):
            self.publish()
        with closing(catalogue.reader(self.file)) as db:
            self.assertEqual(edition, db.execute("SELECT edition FROM publication").fetchone()[0])

    def test_cross_source_ranges_units_ambiguous_names_and_complete_editions(self):
        ids = list("ABCDEFGH")
        self.feed(
            "register",
            [
                {
                    "id": i,
                    "name": "Gauge " + i,
                    "types": ["gauge"],
                    "aliases": ["Shared handle"] if i in "BE" else [],
                }
                for i in ids
            ],
            census={"gauge": len(ids)},
        )
        measures = [
            length(0.29, unit="m"),
            length(50),
            length(8, 12),
            length(20, basis="typical-lower-bound"),
            length(300, basis="record"),
            length(8),
            length(24, 32),
            length(10),
        ]

        def describe(values):
            self.feed(
                "instrument-data",
                [{"id": i, "name": "Gauge " + i, "facts": [f]} for i, f in zip(ids, values)],
            )

        describe(measures)
        self.feed(
            "calibration",
            [{"id": "bar-1", "name": "Reference bar", "aliases": ["Test bar"], "facts": [length(9, 11)]}],
        )
        self.publish()
        prompt = "Name a gauge longer than the Test bar"
        with closing(catalogue.reader(self.file)) as db:
            rule = catalogue.match_question(db, prompt)
            self.assertEqual((rule["records"], rule["complete"]), (4, False))
            bank, report = catalogue.play(db, self.game, rule["key"], prompt)
            self.assertEqual(self.names(bank), {"Gauge A", "Gauge B", "Gauge D", "Gauge G"})
            self.assertEqual(report["gaps"], {"missing": 1, "outside": 1, "borderline": 2})
            self.assertEqual(report["candidates"], 8)
            self.assertIn("11 cm", report["playerRule"])
            self.assertIsNone(
                self.game.execute(
                    "SELECT entity FROM kb_answer_key WHERE bank=? AND term='shared handle' AND form=0",
                    (bank,),
                ).fetchone()[0]
            )
            strict = catalogue.match_question(db, "Name a gauge longer than 290 mm")
            inclusive = catalogue.match_question(db, "Name a gauge at least 0.29 metres long")
            self.assertEqual((strict["records"], inclusive["records"]), (1, 2))
            self.assertEqual(strict["key"], catalogue.match_question(db, "Name a gauge over 29 cm")["key"])
            self.assertEqual(catalogue.match_question(db, "Name a gauge longer than 1 foot")["records"], 1)
            with self.assertRaisesRegex(ValueError, "different quantities"):
                catalogue.match_question(db, "Name a gauge heavier than 2 metres")
            self.assertEqual(
                self.names(catalogue.play(db, self.game, inclusive["key"], inclusive["prompt"])[0]),
                {"Gauge A", "Gauge B"},
            )
        describe([length(n) for n in (29, 50, 8, 20, 60, 8, 30, 10)])
        self.publish()
        with closing(catalogue.reader(self.file)) as db:
            latest = catalogue.match_question(db, strict["prompt"])
            self.assertEqual(
                (latest["key"], latest["records"], latest["complete"]), (strict["key"], 3, True)
            )
            new, report = catalogue.play(db, self.game, latest["key"], latest["prompt"])
            self.assertEqual(self.names(new), {"Gauge B", "Gauge E", "Gauge G"})
            self.assertIsNone(report["openRule"])
            suggestions = catalogue.search(db, "gauge", complete_only=True)
            self.assertTrue(any(c["key"].startswith("compare:") and c["complete"] for c in suggestions))
            ideas = catalogue.search(db, "Gauge G", complete_only=True)
            self.assertTrue(
                any(c["key"].startswith("compare:") and "Gauge G" in c["prompt"] for c in ideas)
            )
            self.assertEqual(self.names(bank), {"Gauge A", "Gauge B", "Gauge D", "Gauge G"})
        self.feed("calibration", [{"id": "other", "name": "Unmeasured bar"}])
        self.publish()
        with closing(catalogue.reader(self.file)) as db, self.assertRaises(catalogue.RuleWithdrawn):
            catalogue.play(db, self.game, rule["key"], prompt)

    def test_world_members_and_references_remain_playable_without_input_files(self):
        world, measures = self.root / "world.sqlite", self.root / "values.sqlite"
        world_fixture(world)
        with closing(sqlite3.connect(world)) as db:
            db.execute("INSERT INTO entity VALUES(96,'Measuring stick')")
            db.execute("INSERT INTO triple VALUES(12,279,10)")
            db.commit()
        with closing(sqlite3.connect(measures)) as db:
            db.executescript("""CREATE TABLE release(dimension,pid,revision); INSERT INTO release VALUES('length','P2043','r1');
                CREATE TABLE measure(dimension,id,low,high); INSERT INTO measure VALUES
                ('length',1,25,25),('length',2,8,8),('length',4,10,12),('length',6,20,20),('length',96,9,11);""")
            db.commit()
        self.publish(world=world, measurements=measures)
        world.unlink()
        measures.unlink()
        with closing(catalogue.reader(self.file)) as db:
            prompt = "Name a work longer than the Measuring stick"
            rule = catalogue.match_question(db, prompt)
            self.assertEqual((rule["records"], rule["complete"]), (2, False))
            bank, report = catalogue.play(db, self.game, rule["key"], prompt)
            self.assertEqual(self.names(bank), {"Work 1", "Work 6"})
            self.assertEqual(report["gaps"], {"missing": 3, "outside": 1, "borderline": 1})
            self.assertEqual(report["candidates"], 7)
            self.assertIsNone(
                self.game.execute(
                    "SELECT entity FROM kb_answer_key WHERE bank=? AND term='shared title' AND form=0",
                    (bank,),
                ).fetchone()[0]
            )
            evidence = self.game.execute(
                "SELECT evidence FROM kb_member WHERE entity='wd:Q6' AND bank=?", (bank,)
            ).fetchone()[0]
            self.assertIn("Q6#P2043", evidence)
            self.assertIn("Q6#P31", evidence)
            self.assertIn("Q12#P279", evidence)
            self.assertEqual(report["reference"][0]["url"], "https://www.wikidata.org/wiki/Q96#P2043")
