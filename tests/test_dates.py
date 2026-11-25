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
import date_discovery
import date_properties
import date_rules
import date_subjects
import date_updates
import dated_link_updates
import dated_links
import watched_rules
from graph import ROOT
from test_catalogue import world_fixture


class DatedQuestions(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.world = self.root / "world.sqlite"
        self.edition = self.root / "published.sqlite"
        world_fixture(self.world)
        self.key = "date-10-577-1998-2000"
        self.assertTrue(watched_rules.follow(self.root, self.key))
        self.file = dict(date_updates.registered(self.root))[self.key]
        with patch.object(
            date_subjects,
            "bindings",
            return_value=[
                {
                    "item": {"value": "http://www.wikidata.org/entity/Q10"},
                    "field": {"value": "http://www.w3.org/2000/01/rdf-schema#label"},
                    "text": {"value": "work"},
                }
            ],
        ):
            date_subjects.refresh(self.root, {10})
        self.facts = [
            (1, "a", "1998-07-19", 11),
            (1, "b", "1999-06-01", 10),
            (2, "c", "1999-01-01", 9),
            (3, "d", "2000-12-31", 11),
            (8, "e", "1999-01-01", 9),
        ]
        self.names = {1: ("First", ["Local title"]), 2: ("Second", []), 3: ("Third", [])}
        self.change_count = False

    def provider(self, query):
        self.assertIn("wikibase:BestRank", query)
        self.assertIn("wikibase:timeCalendarModel wd:Q1985727", query)
        self.assertIn("wdt:P577", query)
        self.assertIn("(wdt:P31|wdt:P106)/wdt:P279*", query)
        if "COUNT(DISTINCT" in query:
            return [
                {
                    "n": {"value": str(len(self.facts) + int(self.change_count))},
                    "members": {"value": str(len({x[0] for x in self.facts}))},
                }
            ]
        import re

        cursor = re.search(r'FILTER\(STR\(\?s\)>"([^"]+)"\)', query)
        rows = [
            {
                "s": {"value": f"http://www.wikidata.org/entity/statement/Q{i}-{guid}"},
                "item": {"value": f"http://www.wikidata.org/entity/Q{i}"},
                "date": {"value": value + "T00:00:00Z"},
                "precision": {"value": str(p)},
            }
            for i, guid, value, p in self.facts
        ]
        rows.sort(key=lambda r: r["s"]["value"])
        if self.change_count and query.endswith("LIMIT " + str(len(self.facts) + 1)):
            rows[-1]["date"]["value"] = "1999-04-01T00:00:00Z"
        return [r for r in rows if not cursor or r["s"]["value"] > cursor[1]][
            : int(query.rsplit("LIMIT ", 1)[1])
        ]

    def labels(self, page):
        for row in page:
            qid = row["item"]["value"].split("/")[-1]
            if pair := self.names.get(int(qid[1:])):
                yield qid, *pair, 0

    def advance(self, key=None, size=2, pages=1):
        key = key or self.key
        where = dict(date_updates.registered(self.root))[key]
        with (
            patch.object(date_updates, "bindings", side_effect=self.provider),
            patch.object(date_updates, "page_names", side_effect=self.labels),
        ):
            return date_updates.advance(key, where, page_size=size, pages=pages)

    def publish(self):
        return catalogue.build(self.world, self.edition, self.root, offline=True, include_bundled=False)

    def test_date_and_relationship_join_by_entity_in_both_orders_and_keep_separate_evidence(self):
        self.facts.append((7, "outside", "1999-01-01", 9))
        self.names[7] = ("Seventh", ["Local title"])
        self.advance(size=20)
        self.publish()
        with closing(catalogue.reader(self.edition)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            questions = [
                "Name a work published by Studio and released in 1999",
                "Name a work released in 1999 and published by Studio",
                "Name a work whose publication date was in 1999 published by Studio",
            ]
            cards = [catalogue.match_question(db, prompt) for prompt in questions]
            self.assertTrue(all(card == cards[0] for card in cards))
            card = cards[0]
            self.assertEqual(card["records"], 2)
            self.assertEqual(catalogue.search(db, "works released in 1999 and published by Studio"), [card])
            self.assertIsNone(
                catalogue.match_question(db, "Name a work released in 1999 or published by Studio")
            )
            self.assertIsNone(
                catalogue.match_question(
                    db, "Name a work released in 1999 and published by Studio and starring Nobody"
                )
            )
            bank, meta = catalogue.play(db, game, card["key"], card["prompt"])
            rows = dict(game.execute("SELECT name,evidence FROM kb_member WHERE bank=?", (bank,)))
            self.assertEqual(set(rows), {"First", "Second"})
            evidence = json.loads(rows["First"])
            self.assertTrue(any(row.get("dates", [{}])[0].get("value") == "1999-06" for row in evidence))
            self.assertTrue(
                any(
                    row.get("relation") == "publisher"
                    and row["source"].startswith("world5m-")
                    and row["url"].endswith("Q1#P123")
                    for row in evidence
                )
            )
            self.assertEqual(
                game.execute(
                    "SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term='local title'", (bank,)
                ).fetchone(),
                (None,),
            )
            self.assertIn("Studio", meta["playerRule"])
            self.assertIn("Wikidata5M", meta["coverage"])
            with self.assertRaises(catalogue.UnverifiedRule) as missing:
                catalogue.match_question(db, "Name a work published by Studio and released in 2001")
            self.assertEqual(missing.exception.requests, ["during:date-10-577-2001-2001+w-10-123-11"])

    def test_current_join_without_a_broad_year_capture_is_atomic_and_withdrawals_are_respected(self):
        key = "during:date-10-577-2001-2001+w-10-123-11"
        watched_rules.follow(self.root, key)
        file = dict(watched_rules.requests(self.root))[key]
        members = [101, 102, 103]
        changed = False
        calls = 0

        def source(query):
            nonlocal calls
            self.assertIn("wdt:P123 wd:Q11", query)
            self.assertIn("wdt:P577", query)
            if "COUNT(DISTINCT" in query:
                return [{"n": {"value": str(len(members))}}]
            calls += 1
            rows = [
                {
                    "s": {"value": f"http://www.wikidata.org/entity/statement/Q{i}-a"},
                    "edge": {"value": f"http://www.wikidata.org/entity/statement/q{i}-b"},
                    "item": {"value": f"http://www.wikidata.org/entity/Q{i}"},
                    "precision": {"value": "9"},
                    "date": {"value": "2001-01-01T00:00:00Z"},
                }
                for i in members
            ]
            if changed and calls % 2 == 0:
                rows[0]["s"]["value"] = rows[0]["s"]["value"] + "2"
            if "OFFSET" in query:
                start = int(query.split("OFFSET ")[1])
                return rows[start : start + 1]
            return rows

        def names(page):
            return [
                (qid, "New " + qid, ["In print " + qid], 1)
                for row in page
                if (qid := row["item"]["value"].rsplit("/", 1)[1]) != "Q103"
            ]

        with (
            patch.object(dated_link_updates, "bindings", side_effect=source),
            patch.object(dated_link_updates, "page_names", side_effect=names),
        ):
            self.assertEqual(dated_link_updates.capture(dated_links.Rule.decode(key), size=1)["scanned"], 3)
            watched_rules.refresh_cache(key, file)
            prior = file.read_bytes()
            changed, calls = True, 0
            with self.assertRaisesRegex(ValueError, "changed"):
                watched_rules.refresh_cache(key, file)
            self.assertEqual(file.read_bytes(), prior)
            # The broader publisher search can be older and empty. The dated
            # capture independently established both facts for its own members.
            parent = "w-10-123-11"
            watched_rules.follow(self.root, parent)
            with patch.object(
                watched_rules,
                "bindings",
                side_effect=lambda q: [{"n": {"value": "0"}}] if "COUNT(" in q else [],
            ):
                watched_rules.refresh_cache(parent, dict(watched_rules.requests(self.root))[parent])
            self.publish()
            with closing(catalogue.reader(self.edition)) as db, closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                prompt = "Name a work published by Studio and released in 2001"
                card = catalogue.match_question(db, prompt)
                self.assertEqual((card["key"], card["records"]), (key, 2))
                with self.assertRaises(catalogue.RuleWithdrawn):
                    catalogue.match_question(db, "Name a work published by Studio")
                self.assertIn(key, [q["key"] for q in catalogue.search(db, "2001")])
                self.assertFalse(db.execute("SELECT 1 FROM date_scope").fetchone())
                bank, meta = catalogue.play(db, game, key, prompt)
                self.assertEqual(meta["unnamedMembers"], 1)
                proof = json.loads(
                    game.execute(
                        "SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q101'", (bank,)
                    ).fetchone()[0]
                )
                self.assertTrue(
                    any(
                        p.get("url", "").endswith("/q101-b") and p.get("targetLabel") == "Studio"
                        for p in proof
                    )
                )
                self.assertTrue(
                    any(p.get("dates", [{}])[0].get("url", "").endswith("/Q101-a") for p in proof)
                )
            members.clear()
            changed = False
            watched_rules.refresh_cache(key, file)
            self.publish()
            with closing(catalogue.reader(self.edition)) as db, self.assertRaises(catalogue.RuleWithdrawn):
                catalogue.match_question(db, prompt)

    def test_geographic_date_capture_only_keeps_subjects_with_a_verified_path(self):
        rule = dated_links.Rule.decode("during:date-10-577-1998-2000+g-10-131-99")
        edges = [(1, 276, 31), (31, 131, 32), (32, 17, 99), (2, 17, 100), (3, 17, 99)]
        wrong_count = False

        def source(query):
            if (
                "SELECT DISTINCT ?s ?item ?date ?precision" in query
                and "{ SELECT DISTINCT ?item ?date" not in query
            ):
                raise ValueError("The source timed out on the broad class join.")
            if "COUNT(DISTINCT" in query:
                return [
                    {
                        "n": {"value": str(len(self.facts) + int(wrong_count))},
                        "members": {"value": str(len({f[0] for f in self.facts}))},
                    }
                ]
            if "VALUES ?u" in query:
                subjects = query.split("VALUES ?u {", 1)[1].split("}", 1)[0].split()
                properties = query.split("VALUES ?p {", 1)[1].split("}", 1)[0].split()
                return [
                    {
                        "u": {"value": f"http://www.wikidata.org/entity/Q{u}"},
                        "v": {"value": f"http://www.wikidata.org/entity/Q{v}"},
                        "p": {"value": f"http://www.wikidata.org/prop/direct/P{p}"},
                    }
                    for u, p, v in edges
                    if f"wd:Q{u}" in subjects and f"wdt:P{p}" in properties
                ]
            return self.provider(query)

        with (
            patch.object(dated_link_updates, "bindings", side_effect=source),
            patch.object(dated_link_updates, "page_names", side_effect=self.labels),
        ):
            data = dated_link_updates.capture(rule)
            self.assertEqual([r[0] for r in data["rows"]], ["Q1", "Q3"])
            self.assertEqual(data["proofs"][rule.link], sorted([edges[i] for i in (0, 1, 2, 4)]))
            self.assertEqual(set(data["dates"]), {"wd:Q1", "wd:Q3"})
            wrong_count = True
            with self.assertRaisesRegex(ValueError, "complete date group"):
                dated_link_updates.capture(rule)

    def test_builder_generates_combined_prompts_from_observed_target_groups(self):
        self.facts = [(i, f"f{i}", "1999-05-01", 10) for i in range(1, 15)]
        self.names = {i: (f"Named {i}", []) for i in range(1, 15)}
        self.advance(size=20)
        self.publish()
        with closing(catalogue.reader(self.edition)) as db:
            dated = catalogue.match_question(db, "Name a work released in 1999")
            choices = dated_links.alternatives(db, dated)
            self.assertTrue(choices)
            card = next(c for c in choices if c["key"].endswith("+w-10-123-11"))
            self.assertEqual(card["records"], 6)
            self.assertEqual(catalogue.match_question(db, card["prompt"]), card)
            self.assertIn(
                card,
                dated_links.alternatives(db, catalogue.match_question(db, "Name a work published by Studio")),
            )
        requested = {key for key, _ in watched_rules.requests(self.root)}
        found = dict(watched_rules.discovered_requests(self.root, self.edition))
        self.assertIn(card["key"], found)
        self.assertNotIn(card["key"], requested)
        checked = []

        def withdrawn(key):
            checked.append(key)
            return {
                "key": key,
                "rows": [],
                "scanned": 0,
                "areas": {},
                "proofs": {},
                "dates": {},
                "statements": {},
                "captured": 1,
                "revision": watched_rules.digest([key, [], 0, {}, {}, {}, {}]),
            }

        with (
            closing(sqlite3.connect(":memory:")) as builder,
            patch.object(watched_rules, "capture", side_effect=withdrawn),
        ):
            builder.execute("ATTACH ? AS world", (str(self.world),))
            builder.execute("CREATE TABLE live_rule(key,revision,captured,n,release)")
            watched_rules.collect(builder, self.root, False, publication=self.edition)
        self.assertEqual(len(checked), min(2, len(found)))
        self.assertTrue(set(checked) <= set(found))
        self.publish()
        self.assertTrue(
            set(checked) <= dict(watched_rules.discovered_requests(self.root, self.edition)).keys()
        )
        with closing(catalogue.reader(self.edition)) as db:
            self.assertFalse(
                db.execute("SELECT 1 FROM current_question WHERE rule=?", (checked[0],)).fetchone()
            )

    def test_calendar_ranges_provenance_unnamed_members_aliases_and_discovery(self):
        self.publish()
        prompt = "Name a work released in 1999"
        with closing(catalogue.reader(self.edition)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as missing:
                catalogue.match_question(db, prompt)
            self.assertEqual(missing.exception.key, "date-10-577-1999-1999")
            self.assertIsNone(catalogue.match_question(db, prompt + " in Paris"))
            with self.assertRaisesRegex(ValueError, "earliest release"):
                catalogue.match_question(db, "Name a work first released in 1999")
        self.assertFalse(self.advance())
        self.assertFalse(self.file.exists())
        self.assertTrue(self.advance(pages=3))
        self.assertEqual(self.publish()["dateStatements"], 5)
        with closing(catalogue.reader(self.edition)) as db, closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            card = catalogue.match_question(db, prompt)
            self.assertEqual(card["records"], 2)
            self.assertIn(card, catalogue.search(db, "1999"))
            self.assertEqual(catalogue.search(db, "works from 1999"), [card])
            with self.assertRaises(date_rules.DateChoices):
                catalogue.match_question(db, "Name a work in 1999")
            self.assertEqual(catalogue.search(db, "works published in 1999"), [card])
            self.assertEqual(catalogue.search(db, "1999", complete_only=True), [])
            larger = catalogue.match_question(db, "Name a work released between 1998 and 2000")
            self.assertEqual(larger["records"], 3)
            bank, meta = catalogue.play(db, game, card["key"], prompt)
            self.assertIn("1 additional", meta["coverage"])
            self.assertIn("Any matching recorded date", meta["playerRule"])
            rows = game.execute(
                "SELECT name,aliases,evidence FROM kb_member WHERE bank=? ORDER BY name", (bank,)
            ).fetchall()
            self.assertEqual([r[0] for r in rows], ["First", "Second"])
            self.assertIn("Local title", json.loads(rows[0][1]))
            dates = [next(p["dates"] for p in json.loads(r[2]) if "dates" in p)[0] for r in rows]
            self.assertEqual([(d["value"], d["precision"]) for d in dates], [("1999-06", 10), ("1999", 9)])
            self.assertTrue(dates[0]["url"].endswith("Q1-b"))
            self.assertIsNone(
                game.execute("SELECT 1 FROM kb_member WHERE bank=? AND name='Third'", (bank,)).fetchone()
            )

    def test_refreshes_resume_keep_previous_release_and_resolve_overlaps(self):
        self.assertTrue(self.advance(pages=4))
        old = self.file.read_bytes()
        self.change_count = True
        with self.assertRaisesRegex(ValueError, "changed during collection"):
            self.advance(pages=4)
        self.assertEqual(self.file.read_bytes(), old)
        self.change_count = False
        self.assertTrue(self.advance(pages=4))
        narrower = "date-10-577-1999-1999"
        watched_rules.follow(self.root, narrower)
        self.facts = []
        self.assertTrue(self.advance(narrower))
        self.publish()
        with closing(catalogue.reader(self.edition)) as db:
            with self.assertRaises(catalogue.RuleWithdrawn):
                catalogue.match_question(db, "Name a work released in 1999")
            self.assertEqual(
                catalogue.match_question(db, "Name a work released from 1998 to 2000")["records"], 2
            )
            self.assertEqual(catalogue.search(db, "1999"), [])
            with self.assertRaises(catalogue.UnverifiedRule):
                catalogue.match_question(db, "Name a work released before 2000")

    def test_exclusive_and_inclusive_years_and_statement_validation(self):
        self.assertEqual(date_rules.period("after 1980"), (1981, 9999))
        self.assertEqual(date_rules.period("before 1980"), (1, 1979))
        self.assertEqual(date_rules.period("since 1980"), (1980, 9999))
        self.assertEqual(date_rules.period("in the 1980s"), (1980, 1989))
        self.assertIsNone(date_rules.period("before about 1980"))
        self.facts[0] = (1, "a", "1980-01-01", 8)
        with self.assertRaisesRegex(ValueError, "precision or years"):
            self.advance()
        self.assertFalse(self.file.exists())

    def test_new_date_properties_and_their_aliases_are_discovered_from_the_provider(self):
        rows = [
            {
                "property": {"value": "http://www.wikidata.org/entity/P10135"},
                "name": {"value": "recording date"},
                "alias": {"value": "date recorded"},
            }
        ]
        with patch.object(date_properties, "bindings", side_effect=[rows, [{"n": {"value": "1"}}]]):
            date_properties.refresh(self.root)
        self.publish()
        with closing(catalogue.reader(self.edition)) as db:
            for prop in ("recording date", "date recorded", "P10135"):
                with self.assertRaises(catalogue.UnverifiedRule) as requested:
                    catalogue.match_question(db, f"Name a work whose {prop} was in 1970")
                self.assertEqual(requested.exception.key, "date-10-10135-1970-1970")
            self.assertIsNone(catalogue.match_question(db, "Name a work whose P999 was in 1970"))

    def test_large_captures_resume_statement_pages_before_naming(self):
        with (
            patch.object(date_updates, "bindings", side_effect=self.provider),
            patch.object(date_updates, "page_names", side_effect=self.labels),
        ):
            self.assertFalse(date_updates.advance(self.key, self.file, pages=1, page_size=2, bulk_limit=2))
            with closing(sqlite3.connect(self.file.with_suffix(".partial"))) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM fact").fetchone()[0], 2)
                self.assertEqual(db.execute("SELECT count(*) FROM member").fetchone()[0], 0)
            self.assertTrue(date_updates.advance(self.key, self.file, pages=6, page_size=2, bulk_limit=2))

    def test_adjacent_releases_fill_only_the_missing_period(self):
        all_facts = self.facts

        def capture(year):
            key = f"date-10-577-{year}-{year}"
            self.facts = [row for row in all_facts if int(row[2][:4]) == year]
            watched_rules.follow(self.root, key)
            self.assertTrue(self.advance(key, size=10))

        capture(1998)
        capture(2000)
        self.publish()
        wording = "Name a work published from 1998 through 2000"
        with closing(catalogue.reader(self.edition)) as db:
            with self.assertRaises(catalogue.UnverifiedRule) as missed:
                catalogue.match_question(db, wording)
            self.assertEqual(missed.exception.requests, ["date-10-577-1999-1999"])
        capture(1999)
        self.publish()
        with closing(catalogue.reader(self.edition)) as db:
            self.assertEqual(catalogue.match_question(db, wording)["records"], 3)

    def test_new_outside_alias_changes_future_banks_and_preserves_previous_scores(self):
        year = "date-10-577-1999-1999"
        watched_rules.follow(self.root, year)
        self.facts = [(1, "a", "1999-01-01", 9)]
        self.assertTrue(self.advance(year, size=10))
        self.publish()
        with closing(sqlite3.connect(":memory:")) as game:
            game.executescript((ROOT / "server/knowledge.sql").read_text())

            def compile_year():
                with closing(catalogue.reader(self.edition)) as db:
                    return catalogue.play(db, game, year, "Name a work released in 1999")[0]

            def alias(bank):
                return game.execute(
                    "SELECT entity FROM kb_answer_key WHERE bank=? AND form=0 AND term='local title'", (bank,)
                ).fetchone()

            first = compile_year()
            self.assertEqual(alias(first), ("wd:Q1",))
            later = "date-10-577-2000-2000"
            watched_rules.follow(self.root, later)
            self.facts = [(400, "new", "2000-04-12", 11)]
            self.names[400] = ("Different work", ["Local title"])
            self.assertTrue(self.advance(later, size=10))
            self.publish()
            second = compile_year()
            self.assertNotEqual(first, second)
            self.assertEqual(alias(second), (None,))
            self.assertEqual(alias(first), ("wd:Q1",))

    def test_background_builder_discovers_new_years_without_host_requests(self):
        discovered = "date-10-577-1982-1982"

        def source(query):
            if "VALUES (?item ?class)" in query:
                return [
                    {
                        "item": {"value": "http://www.wikidata.org/entity/Q1"},
                        "class": {"value": "http://www.wikidata.org/entity/Q10"},
                        "property": {"value": "http://www.wikidata.org/entity/P577"},
                        "date": {"value": "1982-01-01T00:00:00Z"},
                    }
                ]
            self.assertIn("LIMIT 251", query)
            return [{"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}} for i in range(1, 7)]

        storage = self.root / "automatic"
        storage.mkdir()
        publication = storage / "catalogue.sqlite"
        self.facts = [(1, "a", "1982-01-01", 9), (2, "b", "1982-01-01", 9)]
        with (
            patch.object(date_discovery, "bindings", side_effect=source) as fetch,
            patch.object(date_updates, "bindings", side_effect=self.provider),
            patch.object(date_updates, "page_names", side_effect=self.labels),
            patch.object(
                date_subjects,
                "bindings",
                return_value=[
                    {
                        "item": {"value": "http://www.wikidata.org/entity/Q10"},
                        "field": {"value": "http://www.w3.org/2000/01/rdf-schema#label"},
                        "text": {"value": "artistic creation"},
                    }
                ],
            ),
        ):
            catalogue.build(self.world, publication, storage, include_bundled=False, discover_dates=True)
            self.assertEqual(fetch.call_count, 2)
        self.assertNotIn(discovered, dict(watched_rules.rule_keys(storage)))
        self.assertIn(discovered, dict(date_updates.registered(storage)))
        with closing(catalogue.reader(publication)) as db:
            self.assertEqual(date_discovery.tick(db, storage), [])
            q = catalogue.search(db, "1982")[0]
            self.assertEqual((q["key"], q["records"]), (discovered, 2))
            self.assertEqual(q["prompt"], "Name an artistic creation with a recorded release in 1982")
            self.assertIn(q, catalogue.search(db, "artistic creations"))
            legacy = "Name a work published in 1982"
            with self.assertRaisesRegex(date_rules.DateChoices, "artistic creation") as changed:
                catalogue.match_question(db, legacy)
            self.assertEqual(changed.exception.cards, [q])
            with closing(sqlite3.connect(":memory:")) as game:
                game.executescript((ROOT / "server/knowledge.sql").read_text())
                self.assertEqual(catalogue.play(db, game, q["key"], legacy)[1]["prompt"], q["prompt"])
