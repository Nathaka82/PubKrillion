import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.parse
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import catalogue
import class_updates
import nobel_register
import pubchem_register
import watched_rules
import taxonomy
from graph import ROOT, open_graph


def world_fixture(file):
    with closing(sqlite3.connect(file)) as db:
        db.executescript("""
            CREATE TABLE triple(s INTEGER,p INTEGER,o INTEGER);
            CREATE TABLE title(id INTEGER PRIMARY KEY,name TEXT,term TEXT);
            CREATE TABLE entity(id INTEGER PRIMARY KEY,names TEXT);
            CREATE TABLE entity_alias(term TEXT,id INTEGER);
            CREATE TABLE incoming(id INTEGER PRIMARY KEY,n INTEGER);
            CREATE TABLE property(id INTEGER PRIMARY KEY,names TEXT);
            INSERT INTO property VALUES(123,'publisher');
            INSERT INTO entity VALUES(10,'work'),(11,'Studio'),(12,'other');
            INSERT INTO entity_alias VALUES('studio',11),('studio alias',11);
            INSERT INTO entity_alias VALUES('work',10);
            INSERT INTO triple VALUES(6,31,12),(6,123,11),(7,31,10),(1,123,11);
        """)
        db.executemany("INSERT INTO entity VALUES(?,?)",
                       [(i, f"Work {i}\tAlias {i}") for i in range(1, 8)])
        db.execute("UPDATE entity SET names=names||char(9)||'Shared title' WHERE id IN (1,7)")
        db.executemany("INSERT INTO entity_alias VALUES('shared title',?)", [(1,), (7,)])
        for i in range(1, 6):
            db.executemany("INSERT INTO triple VALUES(?,?,?)", [(i, 31, 10), (i, 123, 11)])
        db.commit()


class CatalogueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.published = self.root / "catalogue.sqlite"
        world_fixture(self.world)
        self.manifest = {"source": "local", "revision": "1", "url": "https://example.org",
                         "terms": {"entry": ["entries"]}, "census": {"entry": 2}, "file": "rows.json"}
        self.rows = [{"id": f"entry:{i}", "name": f"Entry {i}", "types": ["entry"]} for i in range(2)]
        (self.root / "local.manifest.json").write_text(json.dumps(self.manifest))
        (self.root / "rows.json").write_text(json.dumps(self.rows))
        # Another source uses the same class. Its entities are outside the register.
        outside = dict(self.manifest, source="other", file="other.json", census={})
        (self.root / "other.manifest.json").write_text(json.dumps(outside))
        (self.root / "other.json").write_text(json.dumps([{"id": "outside", "name": "Unlisted", "types": ["entry"]}]))

    def publish(self):
        with patch.object(catalogue, "bootstrap"):
            return catalogue.build(self.world, self.published, self.root, offline=True)

    def test_taxonomy_refresh_resumes_follows_new_parents_and_withdraws_old_paths(self):
        import re
        snapshot=taxonomy.location(self.root)
        parents={12:[80],80:[10]}
        def provider(query):
            rows=[]
            for child in map(int,re.findall(r"wd:Q(\d+)",query)):
                for parent in parents.get(child,[]) or [None]:
                    row={"child":{"value":f"http://www.wikidata.org/entity/Q{child}"}}
                    if parent is not None:
                        row["parent"]={"value":f"http://www.wikidata.org/entity/Q{parent}"}
                    rows.append(row)
            return rows
        with patch.object(taxonomy,"bindings",side_effect=provider):
            self.assertFalse(taxonomy.capture(self.world,snapshot,pages=1,batch_size=1))
        self.assertFalse(snapshot.exists())
        with patch.object(taxonomy,"bindings",return_value=[]),self.assertRaisesRegex(ValueError,"every requested class"):
            taxonomy.capture(self.world,snapshot)
        with patch.object(taxonomy,"bindings",side_effect=provider):
            self.assertTrue(taxonomy.capture(self.world,snapshot))
        first=self.publish()["edition"]
        with closing(catalogue.reader(self.published)) as db:
            prompt="Name a work published by Studio"
            self.assertEqual(catalogue.match_question(db,prompt)["records"],6)
            self.assertEqual(db.execute("SELECT count(*) FROM world_parent WHERE child=80 AND parent=10").fetchone()[0],1)
        parents[12]=[]
        with patch.object(taxonomy,"bindings",side_effect=provider):
            self.assertFalse(taxonomy.capture(self.world,snapshot,pages=1,batch_size=1))
            self.assertEqual(self.publish()["edition"],first)
            self.assertTrue(taxonomy.capture(self.world,snapshot))
        self.assertNotEqual(self.publish()["edition"],first)
        with closing(catalogue.reader(self.published)) as db:
            self.assertEqual(catalogue.match_question(db,prompt)["records"],5)
        self.assertGreater(taxonomy.due(self.world,snapshot),0)
        with closing(sqlite3.connect(self.world)) as world:
            world.execute("INSERT INTO entity VALUES(99,'New source item')")
            world.commit()
        self.assertEqual(taxonomy.due(self.world,snapshot),0)

    def test_plain_world_questions_include_subclasses_and_occupations_offline(self):
        with closing(sqlite3.connect(self.world)) as world:
            world.executescript("""
                INSERT INTO entity VALUES(88,'illustrator'),(89,'scientific illustrator'),(90,'thing'),
                    (91,'occupation A'),(92,'occupation B');
                UPDATE entity SET names=names||char(9)||'unresolved occupation' WHERE id IN (91,92);
                INSERT INTO triple VALUES(12,279,10),(89,279,88),(88,279,90),(89,279,90),
                    (2,106,88),(3,106,89),(3,106,88),(4,106,91),(4,106,92);
            """)
            world.commit()
        self.publish()
        self.world.unlink()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT/"server/knowledge.sql").read_text())
            for topic,expected in (("work",7),("illustrator",2),("type of thing",2)):
                prompt = "Name a "+topic
                card = catalogue.match_question(db,prompt)
                self.assertEqual(card["records"],expected)
                self.assertFalse(card["complete"])
                self.assertEqual(catalogue.search(db,topic)[0]["key"],card["key"])
                bank,report = catalogue.play(db,game,card["key"],prompt)
                self.assertEqual(report["answerCount"],expected)
                if topic=="work":
                    proof = game.execute("SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q6'",(bank,)).fetchone()[0]
                    self.assertIn("Q6#P31",proof)
                    self.assertIn("Q12#P279",proof)
                elif topic=="illustrator":
                    proof = game.execute("SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q3'",(bank,)).fetchone()[0]
                    self.assertIn("Q3#P106",proof)
                    self.assertIn("Q89#P279",proof)
            with self.assertRaisesRegex(ValueError,"Several source classes"):
                catalogue.match_question(db,"Name an unresolved occupation")
            self.assertEqual(catalogue.match_question(db,"Name a Q91")["records"],1)
            self.assertIsNone(catalogue.match_question(db,"Name an illustrator who won an unrecorded prize"))

    def test_watched_class_pages_are_atomic_and_can_resume_after_an_outage(self):
        key = "type:Q10"
        watched_rules.follow(self.root,key)
        registered,cache = next(class_updates.registered(self.root))
        self.assertEqual(registered,key)
        def page(*ids):
            return [{"item":{"value":"http://www.wikidata.org/entity/Q"+str(i)}} for i in ids]
        def count(n):
            return [{"n":{"value":str(n)}}]
        def names(rows):
            for row in rows:
                qid = row["item"]["value"].split("/")[-1]
                if qid!="Q106":
                    yield qid,"New "+qid,["Alias "+qid],1
        def run(pages):
            with patch.object(class_updates,"bindings",side_effect=pages),patch.object(class_updates,"page_names",side_effect=names):
                class_updates.advance(key,cache,pages=1,page_size=2)
        run([page(101,102)])
        self.assertFalse(cache.exists())
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            self.assertEqual(catalogue.match_question(db,"Name a work")["records"],6)
        with self.assertRaisesRegex(OSError,"offline"):
            run([OSError("offline")])
        run([page(103),count(3)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT/"server/knowledge.sql").read_text())
            current = catalogue.match_question(db,"Name a work")
            self.assertEqual(current["records"],3)
            bank,report = catalogue.play(db,game,key,current["prompt"])
            self.assertEqual(report["answerCount"],3)
            evidence = game.execute("SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q101'",(bank,)).fetchone()[0]
            self.assertIn("query.wikidata.org",evidence)
            self.assertIn("Q101",evidence)
        run([page(102,104)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            self.assertEqual(catalogue.match_question(db,"Name a work"),current)
        with self.assertRaisesRegex(ValueError,"changed during pagination"):
            run([page(),count(3)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            self.assertEqual(catalogue.match_question(db,"Name a work"),current)
        run([page(102,104)])
        run([page(),count(2)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            new = catalogue.match_question(db,"Name a work")
            self.assertEqual((new["records"],new["key"]),(2,key))
            self.assertNotEqual(new["edition"],current["edition"])
        with patch.object(class_updates,"fetch",return_value=b'{"entities":{"Q10":{"id":"Q10","lastrevid":1}}}'):
            run([page(),count(0)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db,self.assertRaises(catalogue.RuleWithdrawn):
            catalogue.match_question(db,"Name a work")
        run([page(101,106)])
        run([page(),count(2)])
        self.publish()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT/"server/knowledge.sql").read_text())
            card = catalogue.match_question(db,"Name a work")
            _,q = catalogue.play(db,game,key,card["prompt"])
            self.assertEqual((card["records"],q["candidates"],q["unnamedMembers"]),(1,2,1))

    def test_independent_wikidata_refresh_replaces_members_and_replays_a_consistent_release(self):
        folder = self.root / "linked"
        folder.mkdir()
        (folder / "scope.manifest.json").write_text(json.dumps({"wikidata": {
            "classId": "Q100", "label": "instrument", "propertyId": "P87654", "relation": "exhibited at"}}))
        members = {"Q101": "Amber bell", "Q102": "Tin harp", "Q103": "Oak drum"}
        aliases = {"Q101": ["Old bell"]}
        linked = {"Q101", "Q102"}
        def item(qid):
            return {"value": "http://www.wikidata.org/entity/" + qid}
        def query(sparql):
            if "P87654" in sparql:
                return [{"item": item(qid), "place": item("Q990"), "name": {"value": "Gallery"}}
                        for qid in sorted(linked) if "wd:" + qid in sparql]
            return [{"item": item(qid)} for qid in sorted(members)]
        def api(url):
            args = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            ids = args["ids"][0].split("|")
            return json.dumps({"entities": {qid: {"id": qid, "lastrevid": 10,
                "labels": {"en": {"value": members.get(qid, "Instrument")}},
                "aliases": {"en": [{"value": name} for name in aliases.get(qid, [])]}} for qid in ids}}).encode()
        def publish(offline=False):
            return catalogue.build(None, self.published, folder, offline=offline, include_bundled=False)
        def inspect():
            with closing(catalogue.reader(self.published)) as db:
                rule, = catalogue.search(db, "gallery")
                rows = db.execute("SELECT name,aliases FROM entity WHERE source='wd:Q100:instrument' ORDER BY name")
                return rule, {name: json.loads(forms) for name, forms in rows}
        now = int(time.time())
        with patch("sources.fetch", side_effect=api), patch("world_facts.bindings", side_effect=query):
            with patch("wikidata_registers.time.time", return_value=now-200000):
                first = publish()["edition"]
            rule, names = inspect()
            self.assertEqual((rule["records"], rule["complete"], names["Amber bell"]), (2, False, ["Old bell"]))
            self.assertEqual(rule["prompt"], "Name an instrument exhibited at Gallery")
            del members["Q102"]
            members["Q104"] = "Silver chime"
            aliases["Q101"] = ["Evening bell"]
            linked.remove("Q102")
            linked.add("Q104")
            with patch("wikidata_registers.relation_source", side_effect=OSError("incomplete response")):
                with self.assertRaisesRegex(ValueError, "incomplete response"):
                    publish()
                self.assertEqual(publish(offline=True)["edition"], first)
                self.assertEqual(inspect(), (rule, names))
            next_release = publish()["edition"]
            updated, names = inspect()
            self.assertNotEqual(first, next_release)
            self.assertEqual((rule["key"], updated["records"]), (updated["key"], 2))
            self.assertEqual((set(names), names["Amber bell"]), (set(members.values()), ["Evening bell"]))
            with patch("world_facts.bindings", side_effect=AssertionError("unexpected network request")):
                self.assertEqual(publish()["edition"], next_release)
                self.assertEqual(publish(offline=True)["edition"], next_release)
            linked.clear()
            with patch("wikidata_registers.time.time", return_value=now+200000):
                self.assertNotEqual(publish()["edition"], next_release)
            with closing(catalogue.reader(self.published)) as db:
                self.assertFalse(catalogue.search(db, "gallery"))
                self.assertEqual(catalogue.search(db, "instrument")[0]["records"], 3)

    def test_host_topics_preserve_meaningful_symbols_and_writing_system_marks(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("UPDATE entity SET names='C++' WHERE id=11")
            db.execute("DELETE FROM entity_alias WHERE id=11")
            db.execute("INSERT INTO entity_alias VALUES('c',11)")
            db.commit()
        self.manifest["terms"]["entry"] = ["語", "किताब", "F#", "!!!"]
        (self.root / "other.manifest.json").unlink()
        (self.root / "local.manifest.json").write_text(json.dumps(self.manifest))
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            for word in ("किताब", "F#", "!!!", "語"):
                rule, = catalogue.search(db, word, complete_only=True)
                self.assertEqual(catalogue.match_question(db, f"Name a {word}?")["key"], rule["key"])
            for word in ("F", "कतब", "C"):
                self.assertEqual(catalogue.search(db, word), [])
                with self.assertRaises(catalogue.UnverifiedRule) as unknown:
                    catalogue.match_question(db, f"Name a work published by {word}?")
                from target_lookup import Request
                self.assertEqual(Request.decode(unknown.exception.key).term, word.lower())
            rule, = catalogue.search(db, "C++")
            self.assertTrue(rule["prompt"].endswith("C++"))
            self.assertEqual(catalogue.match_question(db, rule["prompt"] + "?")["key"], rule["key"])

    def test_two_world_predicates_compile_on_demand_and_require_both_facts(self):
        with closing(sqlite3.connect(self.world)) as db:
            db.execute("INSERT INTO property VALUES(50,'author')")
            db.execute("INSERT INTO property VALUES(1050,'coauthor'||char(9)||'author')")
            db.execute("INSERT INTO entity VALUES(13,'Writer')")
            db.execute("INSERT INTO triple VALUES(5,1050,13)")
            db.executemany("INSERT INTO triple VALUES(?,50,13)", [(i,) for i in (1, 2, 3, 4, 7)])
            db.commit()
        self.publish()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            prompt = "Name a work published by Studio alias and written by Writer"
            card = catalogue.match_question(db, prompt)
            self.assertEqual((card["records"], card["complete"]), (4, False))
            reverse = catalogue.match_question(db, "Name a work written by Writer and published by Studio")
            self.assertEqual(card["key"], reverse["key"])
            explicit = catalogue.match_question(db, "Name a work whose author is Writer and whose publisher is Studio")
            self.assertEqual((explicit["key"], explicit["records"]), (card["key"], 4))
            self.assertIsNone(catalogue.match_question(db, prompt + " before 1990"))
            bank, meta = catalogue.play(db, game, card["key"], prompt, card["edition"])
            rows = game.execute("SELECT name,evidence FROM kb_member WHERE bank=?", (bank,)).fetchall()
            self.assertEqual({r[0] for r in rows}, {"Work 1", "Work 2", "Work 3", "Work 4"})
            self.assertEqual({f["relation"] for f in json.loads(rows[0][1]) if "relation" in f}, {"author", "publisher"})
            self.assertIsNotNone(meta["openRule"])
            self.assertIsNone(game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND term='shared title' LIMIT 1",
                                            (bank,)).fetchone()[0])

    def test_two_properties_of_a_complete_source_register(self):
        source = {"source": "museum", "url": "https://example.org", "revision": "1", "file": "exhibits.json",
                  "census": {"exhibit": 4}, "completeFacets": {"exhibit": {
                      "material": "Name an exhibit made of {value}", "venue": "Name an exhibit shown at {value}"}}}
        rows = [{"id": str(i), "types": ["exhibit"], "name": f"Exhibit {i}",
                 "edges": [["material", "bronze" if i < 3 else "silver"], ["venue", "Louvre" if i in (0, 1, 3) else "Orsay"]]}
                for i in range(4)]
        (self.root / "exhibits.json").write_text(json.dumps(rows))
        (self.root / "museum.manifest.json").write_text(json.dumps(source))
        self.publish()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            prompt = "Name an exhibit made of bronze and shown at Louvre"
            card = catalogue.match_question(db, prompt)
            self.assertEqual((card["records"], card["complete"]), (2, True))
            bank, meta = catalogue.play(db, game, card["key"], prompt, card["edition"])
            self.assertEqual((meta["answerCount"], meta["gaps"]["outside"], meta["openRule"]), (2, 2, None))
            self.assertEqual([r[0] for r in game.execute("SELECT name FROM kb_member WHERE bank=? ORDER BY name", (bank,))],
                             ["Exhibit 0", "Exhibit 1"])

    def test_current_geographic_capture_proves_paths_and_preserves_the_previous_release_on_failure(self):
        import gzip
        import re
        key="g-10-131-80"
        with closing(sqlite3.connect(self.world)) as raw:
            raw.executescript("""INSERT INTO property VALUES(131,'administrative area');
                INSERT INTO entity VALUES(80,'Port City'),(81,'Region');
                INSERT INTO triple VALUES(6,131,80),(80,131,81);""")
            raw.commit()
        self.publish()
        def drafts(idea, complete_only=False):
            env = {**os.environ, "KNOWLEDGE_CATALOGUE": str(self.published),
                   "KNOWLEDGE_REQUESTS": str(self.root / "knowledge-requests")}
            params = {"idea": idea, "direct": False, "completeOnly": complete_only}
            run = subprocess.run([sys.executable, str(ROOT / "scripts/knowledge/ideas.py"), str(self.root / "game.sqlite")],
                                 input=json.dumps(params), text=True, capture_output=True, check=True, env=env)
            return json.loads(run.stdout)
        self.assertEqual(drafts("works in Port City", complete_only=True)["rules"], [])
        self.assertEqual(drafts("works in Port City before 1880")["rules"], [])
        self.assertFalse(list(watched_rules.requests(self.root)))
        self.assertIn("asked to check", drafts("works in Port City")["error"])
        self.assertIn("asked to check", drafts("Name a work in Port City")["error"])
        self.assertEqual([k for k,_ in watched_rules.requests(self.root)], [key])
        with closing(catalogue.reader(self.published)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as failed:
                catalogue.match_question(db,"Name a work published by Port City")
            self.assertEqual(failed.exception.key, "w-10-123-80")
        _,cache=next(watched_rules.requests(self.root))
        path={(999,276,87),(87,131,88),(88,131,87),(88,131,80)}
        count=2
        def entity(i):
            return {"value":f"http://www.wikidata.org/entity/Q{i}"}
        def provider(query):
            if "?u ?p ?v" in query:
                ids={int(i) for i in re.findall(r"wd:Q(\d+)",query)}
                props={int(i) for i in re.findall(r"wdt:P(\d+)",query)}
                return [{"u":entity(u),"p":{"value":f"http://www.wikidata.org/prop/direct/P{p}"},"v":entity(v)}
                        for u,p,v in path if u in ids and p in props]
            if "DISTINCT ?p WHERE" in query:
                return [{"p":entity(81)}]
            self.assertIn("(wdt:P131|wdt:P276)/wdt:P131*/wdt:P17?",query)
            self.assertIn("(wdt:P31|wdt:P106)/wdt:P279*",query)
            if "COUNT(DISTINCT" in query:
                return [{"n":{"value":str(count)}}]
            return [{"item":entity(i)} for i in (998,999)]
        with patch.object(watched_rules,"bindings",side_effect=provider),patch.object(
                watched_rules,"named_items",return_value=[("Q999","New building",["Regional name"],1)]):
            watched_rules.refresh_cache(key,cache)
            original=cache.read_bytes()
            captured=json.loads(gzip.decompress(original))
            self.assertEqual(captured["areas"],{"80":[81]})
            self.assertEqual(captured["scanned"],2)
            count=3
            with self.assertRaisesRegex(ValueError,"count changed"):
                watched_rules.refresh_cache(key,cache)
            count=2
            path.remove((88,131,80))
            with self.assertRaisesRegex(ValueError,"every step"):
                watched_rules.refresh_cache(key,cache)
            self.assertEqual(cache.read_bytes(),original)
        self.publish()
        self.assertEqual(drafts("works in Port City")["rules"][0]["records"], 1)
        for topic in ("Port City", "works"):
            found = next(card for card in drafts(topic)["rules"] if card["key"] == key)
            self.assertEqual((found["prompt"], found["records"]), ("Name a work in Port City", 1))
        with closing(catalogue.reader(self.published)) as db,closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT/"server/knowledge.sql").read_text())
            card=catalogue.match_question(db,"Name a work in Port City")
            self.assertEqual((card["key"],card["records"]),(key,1))
            bank,meta=catalogue.play(db,game,key,card["prompt"])
            self.assertIn("1 additional",meta["coverage"])
            names=[r[0] for r in game.execute("SELECT name FROM kb_member WHERE bank=?",(bank,))]
            self.assertEqual(names,["New building"])
            proof=json.loads(game.execute("SELECT evidence FROM kb_member WHERE bank=?",(bank,)).fetchone()[0])
            self.assertEqual([s["property"] for s in next(p["steps"] for p in proof if "steps" in p)],["P276","P131","P131"])

    def test_same_named_places_can_be_selected_by_containing_region(self):
        with closing(sqlite3.connect(self.world)) as raw:
            raw.executescript("""INSERT INTO entity VALUES(81,'Marsh'),(82,'Marsh'),(83,'River Province'),(84,'Coast Province');
                INSERT INTO property VALUES(131,'administrative area');
                INSERT INTO triple VALUES(81,131,83),(82,131,84),(1,131,81),(2,131,82);""")
            raw.commit()
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            with self.assertRaisesRegex(ValueError,"Several places"):
                catalogue.match_question(db,"Name a work in Marsh")
            for place,key in [("Marsh, River Province",81),("Marsh within Coast Province",82),("Q82",82)]:
                card=catalogue.match_question(db,"Name a work in "+place)
                self.assertEqual((card["key"],card["records"]),(f"g-10-131-{key}",1))
                self.assertEqual(catalogue.match_question(db,card["prompt"])["key"],card["key"])
            self.assertIsNone(catalogue.match_question(db,"Name a work in Marsh (Q84)"))

    def test_combining_separately_refreshed_relationships_uses_the_current_members(self):
        import gzip
        with closing(sqlite3.connect(self.world)) as world:
            world.executescript("""INSERT INTO property VALUES(50,'author');
                INSERT INTO entity VALUES(15,'Writer');
                INSERT INTO triple VALUES(1,50,15),(2,50,15),(3,50,15);""")
            world.commit()
        self.publish()
        def update(key,ids):
            rows=[["Q"+str(i),"Current "+str(i),["Alias "+str(i)]] for i in ids]
            with closing(sqlite3.connect(self.published)) as db:
                db.execute("INSERT INTO live_rule VALUES(?,?,1,?,?)",(key,key,len(ids),gzip.compress(json.dumps({"rows":rows}).encode())))
                db.commit()
        update("w-10-123-11",[2,4,44001])
        prompt="Name a work published by Studio and written by Writer"
        with closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            with closing(catalogue.reader(self.published)) as db:
                card=catalogue.match_question(db,prompt)
                self.assertEqual(card["records"],1)
                old,_=catalogue.play(db,game,card["key"],prompt)
            update("w-10-50-15",[3,44001])
            with closing(catalogue.reader(self.published)) as db:
                card=catalogue.match_question(db,prompt)
                self.assertEqual(card["records"],1)
                bank,_=catalogue.play(db,game,card["key"],prompt)
                self.assertEqual(game.execute("SELECT name FROM kb_member WHERE bank=?",(bank,)).fetchone()[0],"Current 44001")
                self.assertEqual(game.execute("SELECT name FROM kb_member WHERE bank=?",(old,)).fetchone()[0],"Current 2")

    def test_question_wording_uses_attested_singulars_and_understands_earlier_phrases(self):
        with closing(sqlite3.connect(self.world)) as world:
            world.executescript("""
                UPDATE entity SET names='Municipalities of Aster'||char(9)||'municipality of Aster' WHERE id=10;
                INSERT INTO title VALUES(10,'Municipalities of Aster','municipalities of aster');
                INSERT INTO property VALUES(47,'shares border with');
                INSERT INTO triple SELECT s,47,o FROM triple WHERE p=123;
            """)
            world.commit()
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            earlier = 'Name a municipalities of Aster with “shares border with” recorded as Studio'
            natural = 'Name a municipality of Aster that has shared a border with Studio'
            for prompt in (earlier, natural, 'Name a municipality of Aster bordering Studio'):
                card = catalogue.match_question(db,prompt)
                self.assertEqual((card["key"],card["records"]),("w-10-47-11",5))
            self.assertEqual(catalogue.match_question(db,natural)["prompt"],natural)
            self.assertEqual(catalogue.match_question(db,"Name a municipalities of Aster")["prompt"],
                             "Name a municipality of Aster")
            with closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                _, meta = catalogue.play(db,game,card["key"],natural)
                self.assertIn("historical borders",meta["playerRule"])
        from graph import indefinite_article
        for subject in ("university","European city","United Kingdom constituency","one-act play","NASA mission","UK port"):
            self.assertEqual(indefinite_article(subject),"a")
        for subject in ("unidentified object","hour","honorary title","MRI scanner","F.B.I. office","X-ray","EU member"):
            self.assertEqual(indefinite_article(subject),"an")
        self.assertEqual(catalogue.world_question("FBI agent",19,"place of birth","Leeds"),
                         "Name an FBI agent whose place of birth is Leeds")
        self.assertEqual(catalogue.named_question("X-ray source","in this catalogue"),
                         "Name an X-ray source in this catalogue")
        self.assertEqual(catalogue.world_question("Municipalities of Spain",47,"shares border with","Aster"),
                         "Name one of the municipalities of Spain that has shared a border with Aster")

    def test_narrower_evidence_extends_its_class_and_operands_and_can_be_joined_by_id(self):
        import gzip
        from graph import digest
        with closing(sqlite3.connect(self.world)) as world:
            world.executescript("""
                INSERT INTO property VALUES(50,'author'),(136,'genre');
                INSERT INTO entity VALUES(13,'Creator'),(14,'Genre');
                INSERT INTO triple VALUES(1,50,13),(2,136,14);
            """)
            world.commit()
        publisher, author, genre = "w-10-123-11","w-10-50-13","w-10-136-14"
        def both(a,b):
            return "and:"+"+".join(sorted((a,b)))
        def capture(rule, items):
            watched_rules.follow(self.root,rule)
            rows = [[f"Q{i}",f"Work {i}",[f"Current name {i}"]] for i in items]
            payload = dict(key=rule,rows=rows,revision=digest([rule,rows]),captured=1000)
            path = dict(watched_rules.requests(self.root))[rule]
            watched_rules.replace(path,gzip.compress(json.dumps(payload).encode()))
        def play(db, game, rule, prompt):
            bank,meta = catalogue.play(db,game,rule,prompt)
            return bank, {r[0]:json.loads(r[1]) for r in game.execute(
                "SELECT entity,evidence FROM kb_member WHERE bank=?",(bank,))},meta
        capture(both(publisher,author),[7,55])
        capture(both(genre,author),[55,65])
        self.publish()
        with closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            with closing(catalogue.reader(self.published)) as db:
                card = catalogue.match_question(db,"Name a work published by Studio")
                self.assertEqual(card["records"],7)
                old,answers,_ = play(db,game,card["key"],card["prompt"])
                self.assertEqual(set(answers), {f"wd:Q{i}" for i in [1,2,3,4,5,7,55]})
                self.assertTrue(any(p.get("source","").startswith("wikidata-query-witness-") for p in answers["wd:Q55"]))
                self.assertTrue(any(p.get("source","").startswith("world5m-") for p in answers["wd:Q2"]))
                card = catalogue.match_question(db,"Name a work")
                _,all_works,_ = play(db,game,card["key"],card["prompt"])
                self.assertEqual(set(all_works),{f"wd:Q{i}" for i in [1,2,3,4,5,7,55,65]})
                self.assertEqual(card["records"],len(all_works))
                _,joined,meta = play(db,game,both(publisher,genre),"Name a work in the genre Genre published by Studio")
                self.assertEqual(set(joined),{"wd:Q2","wd:Q55"})
                self.assertTrue(meta["plan"]["historicalOperands"])
                self.assertEqual({p["verifiedBy"]["rule"] for p in joined["wd:Q55"] if "verifiedBy" in p},
                                 {both(publisher,author),both(genre,author)})
                self.assertTrue(any(p.get("verifiedBy",{}).get("source")=="Wikidata5M" for p in joined["wd:Q2"]))
            capture(publisher,[])
            self.publish()
            with closing(catalogue.reader(self.published)) as db:
                with self.assertRaises(catalogue.RuleWithdrawn):
                    play(db,game,publisher,"Name a work published by Studio")
                with self.assertRaises(catalogue.RuleWithdrawn):
                    play(db,game,both(publisher,genre),"Name a work in the genre Genre published by Studio")
                self.assertEqual(game.execute("SELECT count(*) FROM kb_member WHERE bank=?",(old,)).fetchone()[0],7)

    def test_form_and_primary_class_answers_share_a_bank_with_independent_withdrawals(self):
        import gzip
        import work_forms
        from graph import digest
        with closing(sqlite3.connect(self.world)) as world:
            world.executescript("""
                INSERT INTO property VALUES(50,'author'),(7937,'form of creative work'),(136,'genre');
                INSERT INTO entity VALUES(13,'Creator'),(14,'sonata'),(15,'landscape');
                INSERT INTO triple VALUES(14,279,10),(1,31,14),(1,50,13),(4,7937,15),(7,136,14);
            """)
            world.commit()
        def capture(rule, ids):
            watched_rules.follow(self.root,rule)
            rows = [[f"Q{i}",f"Work {i}",[f"Alternative {i}"]] for i in ids]
            payload = dict(key=rule,rows=rows,revision=digest([rule,rows]),captured=1000)
            path = dict(watched_rules.requests(self.root))[rule]
            watched_rules.replace(path,gzip.compress(json.dumps(payload).encode()))
        def both(a,b):
            return "and:" + "+".join(sorted((a,b)))
        author, publisher, form = "w-10-50-13","w-10-123-11","w-10-7937-14"
        credit = both(author,form)
        capture(credit,[1,55,56])
        capture(both(publisher,form),[55,65])
        capture(both(author,"w-10-7937-15"),[77])  # format with no subclass path to work
        capture(both(author,"w-10-136-14"),[78])   # genre alone does not classify the work
        self.publish()
        prompt = "Name a sonata written by Creator"
        paired = "Name a sonata written by Creator and published by Studio"
        def check(db,game,wording,expected):
            card = catalogue.match_question(db,wording)
            self.assertEqual(card["records"],len(expected))
            bank, _ = catalogue.play(db,game,card["key"],wording)
            members = {int(qid[4:]):json.loads(proof) for qid,proof in game.execute(
                "SELECT entity,evidence FROM kb_member WHERE bank=?",(bank,))}
            self.assertEqual(set(members),expected)
            return members
        with closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            with closing(catalogue.reader(self.published)) as db:
                check(db,game,"Name a sonata",{1,55,56,65})
                card = db.execute("SELECT prompt,n FROM current_question WHERE rule='w-14-50-13'").fetchone()
                self.assertEqual(tuple(card),(prompt,3))
                members = check(db,game,prompt,{1,55,56})
                evidence = [urllib.parse.unquote(e["url"]) for e in members[55] if "url" in e]
                self.assertTrue(any("wd:Q55 wdt:P7937 wd:Q14" in url and "wd:Q55 wdt:P50 wd:Q13" in url for url in evidence))
                check(db,game,paired,{1,55})
                self.assertEqual(list(work_forms.subscriptions(db,["type:Q14","w-14-50-13"])),[form,credit])
            # A P31-only capture can withdraw the nominal branch without invalidating a verified form.
            capture("w-14-50-13",[])
            capture(both("w-14-123-11","w-14-50-13"),[])
            self.publish()
            with closing(catalogue.reader(self.published)) as db:
                check(db,game,prompt,{1,55,56})
                check(db,game,paired,{55})
            # The exact form and author enumerations also constrain older compound evidence.
            capture(form,[55,56,65])
            capture(author,[55])
            self.publish()
            with closing(catalogue.reader(self.published)) as db:
                check(db,game,prompt,{55})
                check(db,game,"Name a sonata",{1,55,56,65})
                check(db,game,paired,{55})
            with closing(sqlite3.connect(self.published)) as stage:
                stage.execute("INSERT INTO current_class VALUES('type:Q14','nominal-v2',1001,0,0)")
                stage.commit()
            with closing(catalogue.reader(self.published)) as db:
                check(db,game,"Name a sonata",{55,56,65})
            capture(form,[])
            self.publish()
            with closing(catalogue.reader(self.published)) as db:
                check(db,game,"Name a sonata",{1})
                with self.assertRaises(catalogue.RuleWithdrawn):
                    catalogue.play(db,game,"w-14-50-13",prompt)

    def test_watched_world_question_republishes_current_members_and_withdrawals(self):
        key = "w-10-123-11"
        watched_rules.follow(self.root, key)
        watched_rules.follow(self.root, key)
        self.assertEqual(len(list((self.root / "knowledge-requests").glob("*.rule"))), 1)
        self.publish()
        with closing(catalogue.reader(self.published)) as db:
            self.assertEqual(catalogue.search(db, "Studio")[0]["records"], 5)
        members = ["Q1", "Q8", "Q9"]
        def hits(query):
            self.assertIn("?item wdt:P123 wd:Q11", query)
            self.assertIn("wdt:P31/wdt:P279* wd:Q10", query)
            if "COUNT(DISTINCT" in query:
                return [{"n":{"value":str(len(members))}}]
            return [{"item": {"value": "http://www.wikidata.org/entity/" + i}} for i in members]
        def publish():
            with patch.object(catalogue, "bootstrap"), patch.object(nobel_register, "import_snapshot"):
                return catalogue.build(self.world, self.published, self.root)
        with patch("watched_rules.bindings", side_effect=hits), patch("watched_rules.named_items",
                side_effect=lambda page: ((i, "Live " + i, ["Alias " + i], 2) for i in members)):
            updated = publish()
            self.assertEqual(updated["watchedQuestions"], 1)
            with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                card, = catalogue.search(db, "Studio")
                self.assertEqual(card["records"], 3)
                self.assertEqual(catalogue.match_question(db, card["prompt"]), card)
                bank, q = catalogue.play(db, game, key, card["prompt"])
                self.assertEqual((q["answerCount"], q["plan"]["liveClass"]), (3, 10))
                self.assertEqual({r[0] for r in game.execute("SELECT entity FROM kb_member WHERE bank=?", (bank,))},
                                 {"wd:Q1", "wd:Q8", "wd:Q9"})
                self.assertIn("Current Wikidata", q["sources"][0]["label"])
            cache, = (self.root / "knowledge-sources" / "world-rules").glob("*.gz")
            os.utime(cache, (10, 10))
            with patch("watched_rules.bindings", side_effect=OSError("provider unavailable")):
                self.assertEqual(publish()["edition"], updated["edition"])
                self.assertEqual(self.publish()["edition"], updated["edition"])
            members.clear()
            with patch("watched_rules.time.time", return_value=time.time()+2000):
                self.assertNotEqual(publish()["edition"], updated["edition"])
            with closing(catalogue.reader(self.published)) as db:
                self.assertFalse(catalogue.search(db, "Studio"))
                with self.assertRaises(catalogue.RuleWithdrawn):
                    catalogue.play(db, None, key, "Name a work published by Studio")

    def test_standalone_search_play_and_scope(self):
        stats = self.publish()
        self.assertEqual((stats["patterns"], stats["registers"]), (1, 1))
        self.world.unlink()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            rule, = catalogue.search(db, "Studio alias")
            self.assertEqual((rule["complete"], rule["records"]), (False, 5))
            self.assertEqual(catalogue.search(db, "Studio", complete_only=True), [])
            self.assertEqual(catalogue.search(db, "Studio in Spain"), [])
            self.assertEqual(catalogue.search(db, "entry from", complete_only=True), [])
            self.assertEqual(catalogue.match_question(db, "Name a work published by Studio alias")["key"], rule["key"])
            self.assertIsNone(catalogue.match_question(db, "Name a work published by Studio before 1995"))
            bank, preview = catalogue.play(db, game, rule["key"], rule["prompt"], rule["edition"])
            self.assertEqual((preview["answerCount"], preview["plan"]["catalogueKey"]), (5, rule["key"]))
            answers = game.execute("SELECT entity,evidence,aliases FROM kb_member WHERE bank=?", (bank,)).fetchall()
            self.assertEqual({r[0] for r in answers}, {f"wd:Q{i}" for i in range(1, 6)})
            self.assertTrue(all("#P123" in r[1] and "Alias" in r[2] for r in answers))
            self.assertIsNone(game.execute("SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term='shared title'",
                                           (bank,)).fetchone()[0])
            register, = catalogue.search(db, "Name an entry", complete_only=True)
            bank, preview = catalogue.play(db, game, register["key"], register["prompt"], register["edition"])
            self.assertEqual((preview["answerCount"], preview["openRule"]), (2, None))
            self.assertEqual(game.execute("SELECT count(*) FROM kb_member WHERE bank=? AND name='Unlisted'", (bank,)).fetchone()[0], 0)
            self.assertEqual(catalogue.surprise(db, complete_only=True), [register])
            self.assertEqual({r["key"] for r in catalogue.surprise(db)}, {rule["key"], register["key"]})
            with self.assertRaisesRegex(ValueError, "publication changed"):
                catalogue.play(db, game, rule["key"], rule["prompt"], "previous")

    def test_atomic_publish_and_unchanged_or_faulty_sources(self):
        first = self.publish()
        stamp = self.published.stat().st_mtime_ns
        self.assertEqual(self.publish(), first)
        self.assertEqual(self.published.stat().st_mtime_ns, stamp)
        self.rows[0]["name"] = "Corrected name"
        (self.root / "rows.json").write_text(json.dumps(self.rows))
        with patch.object(catalogue, "compile_world", side_effect=AssertionError("Unchanged world graph was recompiled")):
            new = self.publish()
        self.assertNotEqual(new["edition"], first["edition"])
        working = self.published.read_bytes()
        (self.root / "rows.json").write_text(json.dumps(self.rows[:1]))
        with self.assertRaisesRegex(ValueError, "Incomplete enumerated source release"):
            self.publish()
        self.assertEqual(self.published.read_bytes(), working)

    def test_new_open_feed_links_sources_by_entity_and_subclass(self):
        second = {"source": "other", "url": "https://example.org/other", "revision": "1",
                  "file": "other.json", "parents": [["print", "artwork"]], "terms": {"artwork": ["art works"]}}
        (self.root / "other.manifest.json").write_text(json.dumps(second))
        (self.root / "other.json").write_text(json.dumps([
            {"id": "entry:0", "name": "Entry 0", "types": ["print"]},
            {"id": "new", "name": "New work", "types": ["artwork"]}]))
        self.publish()
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            rule, = catalogue.search(db, "art works")
            self.assertEqual((rule["complete"], rule["records"]), (False, 2))
            self.assertEqual(catalogue.search(db, "art works", complete_only=True), [])
            self.assertEqual(catalogue.match_question(db, "Name an artwork")["key"], rule["key"])
            _, preview = catalogue.play(db, game, rule["key"], rule["prompt"], rule["edition"])
            self.assertEqual(preview["answerCount"], 2)
            self.assertIsNotNone(preview["openRule"])

    def test_native_export_drives_complete_filtered_questions_and_rejects_missing_properties(self):
        source = {"source": "collection", "revision": "1", "url": "https://example.org/collection",
                  "file": "native.json", "rowsPath": ["catalogue", "pieces"], "countPath": ["total"],
                  "rowMapping": {"id": "ref", "prefix": "museum:", "name": "title", "class": "exhibit",
                                 "relations": {"material": "made from"}, "aliases": ["local title"]},
                  "completeFacets": {"exhibit": {"made from": "Name an exhibit made from {value}"}}}
        rows = [{"ref": i, "title": f"Exhibit {i}", "local title": f"Piece {i}",
                 "material": "bronze" if i < 2 else "glass"} for i in range(3)]
        folder = self.root / "museum"
        folder.mkdir()
        (folder / "native.manifest.json").write_text(json.dumps(source))
        data = folder / "native.json"
        def publish(total):
            data.write_text(json.dumps({"total": total, "catalogue": {"pieces": rows}}))
            return catalogue.build(None, self.published, folder, offline=True, include_bundled=False)
        self.assertEqual(publish(3)["completeProperties"], 1)
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            rule, = catalogue.search(db, "bronze", complete_only=True)
            self.assertTrue(rule["complete"])
            self.assertEqual(catalogue.search(db, "exhibit", complete_only=True)[0]["records"], 3)
            self.assertEqual(catalogue.match_question(db, rule["prompt"]), rule)
            _, q = catalogue.play(db, game, rule["key"], rule["prompt"], rule["edition"])
            self.assertEqual((q["answerCount"], q["gaps"]["missing"], q["gaps"]["outside"], q["openRule"]), (2, 0, 1, None))
        working = self.published.read_bytes()
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            publish(4)
        rows[0]["material"] = ""
        with self.assertRaisesRegex(ValueError, "exactly one"):
            publish(3)
        self.assertEqual(self.published.read_bytes(), working)

    def test_remote_feed_replaces_only_after_validation_and_replays_offline(self):
        self.manifest["download"] = "https://example.org/registry/latest.json"
        manifest = self.root / "local.manifest.json"
        manifest.write_text(json.dumps(self.manifest))
        cache = self.root / "remote-cache"
        def pull(db, rows):
            with patch("catalogue.urllib.request.urlopen", return_value=io.BytesIO(json.dumps(rows).encode())):
                return catalogue.import_feed(db, manifest, cache, False)
        with closing(open_graph(":memory:")) as db:
            first = pull(db, self.rows)
            self.rows[0]["name"] = "New catalogue name"
            latest = pull(db, self.rows)
            self.assertNotEqual(first, latest)
            with self.assertRaises(ValueError):
                pull(db, self.rows[:1])
        with closing(open_graph(":memory:")) as db:
            self.assertEqual(catalogue.import_feed(db, manifest, cache, True), latest)
            self.assertEqual(db.execute("SELECT name FROM entity WHERE id='entry:0'").fetchone()[0], "New catalogue name")

    def test_ingest_reads_one_release_during_a_concurrent_feed_update(self):
        manifest = self.root / "local.manifest.json"
        original = catalogue.import_file
        def update_then_import(db, path, **kwargs):
            self.rows[0]["name"] = "Next release"
            (self.root / "rows.json").write_text(json.dumps(self.rows))
            manifest.write_text(json.dumps(dict(self.manifest, source="different-owner")))
            return original(db, path, **kwargs)
        with closing(open_graph(":memory:")) as db, patch.object(catalogue, "import_file", side_effect=update_then_import):
            catalogue.import_feed(db, manifest, self.root / "cache", offline=True)
            self.assertEqual(tuple(db.execute("SELECT source,name FROM entity WHERE id='entry:0'").fetchone()), ("local", "Entry 0"))

    def test_external_relationships_link_membership_and_evidence_across_publishers(self):
        edges = {"source": "credits", "revision": "1", "url": "https://example.org/credits", "file": "credits.json"}
        (self.root / "credits.manifest.json").write_text(json.dumps(edges))
        rows = [{"id": e["id"], "name": e["name"], "edges": [["commissioned by", "org:kiln"]]} for e in self.rows]
        rows.append({"id": "org:kiln", "name": "Copper Kiln"})
        (self.root / "credits.json").write_text(json.dumps(rows))
        stats = catalogue.build(None, self.published, self.root, offline=True, include_bundled=False)
        self.assertEqual((stats["entities"], stats["feedPatterns"]), (0, 1))
        with closing(catalogue.reader(self.published)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            rule, = catalogue.search(db, "Copper Kiln")
            self.assertIn(rule, catalogue.surprise(db))
            self.assertEqual(rule["prompt"], "Name an entry commissioned by Copper Kiln")
            self.assertEqual(catalogue.match_question(db, rule["prompt"]), rule)
            self.assertEqual(catalogue.search(db, "Copper Kiln", complete_only=True), [])
            _, q = catalogue.play(db, game, rule["key"], rule["prompt"], rule["edition"])
            self.assertEqual(q["answerCount"], 2)
            self.assertEqual({s["id"] for s in q["sources"]}, {"local", "credits"})


class PeriodicTableTests(unittest.TestCase):
    def test_complete_membership_and_aliases_follow_the_official_rows(self):
        source = json.loads((ROOT / "data/knowledge/pubchem-periodic-table.json").read_bytes())
        aliases = [{"name": "Aluminium", "aliases": ["Al", "Aluminum"], "depth": 2},
                   {"name": "Invented", "aliases": ["Xx"], "depth": 4}]
        with closing(open_graph(":memory:")) as db:
            pubchem_register.load(db, source, aliases)
            n = len(source["Table"]["Row"])
            self.assertEqual(db.execute("SELECT count(*) FROM entity").fetchone()[0], n)
            self.assertEqual(db.execute("SELECT name FROM entity WHERE id='pubchem:element:13'").fetchone()[0], "Aluminium")
            columns = source["Table"]["Columns"]["Column"]
            def row(number, symbol, name):
                fields = {"AtomicNumber": str(number), "Symbol": symbol, "Name": name,
                          "StandardState": "test state", "GroupBlock": "test group"}
                return {"Cell": [fields.get(key, "") for key in columns]}
            source["Table"]["Row"].append(row(n+1, "Xy", "Synthetic fixture"))
            pubchem_register.load(db, source, aliases, minimum=n)
            self.assertEqual(db.execute("SELECT count(*) FROM entity").fetchone()[0], n+1)
            source["Table"]["Row"].pop()
            with self.assertRaisesRegex(ValueError, "omits previously"):
                pubchem_register.load(db, source, aliases, minimum=n+1)
            source["Table"]["Row"].append(row(n+2, "Xy", "Synthetic fixture"))
            with self.assertRaisesRegex(ValueError, "missing atomic"):
                pubchem_register.load(db, source, aliases)
            self.assertEqual(db.execute("SELECT count(*) FROM entity").fetchone()[0], n+1)


class NobelTests(unittest.TestCase):
    def test_incomplete_awards_do_not_publish(self):
        prize = {"awardYear": "1992", "category": {"en": "Physics"}, "categoryFullName": {"en": "The Nobel Prize in Physics"}}
        with closing(open_graph(":memory:")) as db:
            for export in ({"count": 2, "prizes": [prize]}, {"count": 1, "prizes": [prize]}):
                with self.assertRaises(ValueError):
                    nobel_register.load(db, export)
            winner = {"id": "1", "knownName": {"en": "Name One"}}
            for share in ("1/2", "1/0", "err"):
                with self.assertRaises(ValueError):
                    nobel_register.load(db, {"count": 1, "prizes": [dict(prize, laureates=[dict(winner, portion=share)])]})
            awarded = dict(prize, laureates=[dict(winner, portion="1")])
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                nobel_register.load(db, {"count": 2, "prizes": [awarded, awarded]})
            unawarded = dict(prize, awardYear="1993", topMotivation={"en": "No Nobel Prize was awarded."})
            nobel_register.load(db, {"count": 2, "prizes": [awarded, unawarded]})
            self.assertEqual(db.execute("SELECT count(*) FROM entity").fetchone()[0], 1)
