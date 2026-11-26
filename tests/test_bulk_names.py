import bz2
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
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts/knowledge"))
import bulk_names as bulk
import catalogue
import entity_labels
from graph import ROOT
from test_catalogue import world_fixture


def page(page_id, qid, labels, aliases=(), redirect=None):
    element = ET.Element("page")
    for tag, value in (("title", qid), ("ns", 0), ("id", page_id)):
        ET.SubElement(element, tag).text = str(value)
    if redirect:
        ET.SubElement(element, "redirect", title=redirect)
    revision = ET.SubElement(element, "revision")
    text = json.dumps(
        {
            "type": "item",
            "id": qid,
            "labels": {lang: {"language": lang, "value": v} for lang, v in labels},
            "aliases": {"en": [{"language": "en", "value": v} for v in aliases]},
        },
        ensure_ascii=False,
    )
    number = int(hashlib.sha1(text.encode()).hexdigest(), 16)
    checksum = ""
    while number:
        number, digit = divmod(number, 36)
        checksum = "0123456789abcdefghijklmnopqrstuvwxyz"[digit] + checksum
    for tag, value in (
        ("id", str(900 + page_id)),
        ("timestamp", "2026-09-23T01:00:00Z"),
        ("model", "wikibase-item"),
        ("text", text),
        ("sha1", checksum),
    ):
        ET.SubElement(revision, tag).text = value
    return ET.tostring(element, encoding="utf-8")


class Response(io.BytesIO):
    def __init__(self, data, bounds, status=206):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Range": bounds}


class BulkNames(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.world = self.root / "world.sqlite"
        world_fixture(self.world)
        self.output = self.root / "catalogue.sqlite"
        # A page outside the graph between two relevant blocks exercises range skipping.
        groups = [
            [
                (
                    111,
                    "Q1",
                    page(
                        111, "Q1", [("en", "Work 1"), ("ja", "作品一")], ["Shared name"]
                    ),
                )
            ],
            [(115, "Q999", page(115, "Q999", [("en", "Outside the index")]))],
            [
                (233, "Q7", page(233, "Q7", [("en", "Work 7")], ["Shared name"])),
                (240, "Q6", page(240, "Q6", [], redirect="Q7")),
            ],
        ]
        self.xml = bz2.compress(b"<mediawiki>")
        lines = []
        for group in groups:
            lines.extend(f"{len(self.xml)}:{p}:{q}\n" for p, q, _ in group)
            self.xml += bz2.compress(b"".join(content for _, _, content in group))
        self.xml += bz2.compress(b"</mediawiki>")
        self.index = bz2.compress("".join(lines).encode())
        self.shard = [
            1,
            300,
            {"url": "/xml", "size": len(self.xml)},
            {
                "url": "/index",
                "size": len(self.index),
                "sha1": hashlib.sha1(self.index).hexdigest(),
            },
        ]
        self.requests = []

    def source(self, request, timeout):
        start, stop = map(int, request.get_header("Range")[6:].split("-"))
        self.requests.append((start, stop))
        return Response(
            self.xml[start : stop + 1], f"bytes {start}-{stop}/{len(self.xml)}"
        )

    def tick(self, budget=10_000):
        with (
            patch.object(bulk, "discover", return_value=("20261101", [self.shard])),
            patch.object(bulk.sources, "fetch", return_value=self.index),
            patch.object(bulk.sources, "open_source", side_effect=self.source),
        ):
            return bulk.tick(self.world, self.root, budget)

    def publish(self):
        return catalogue.build(
            self.world,
            self.output,
            self.root / "feeds",
            offline=True,
            include_bundled=False,
        )

    def test_restart_publication_and_answer_proof(self):
        self.assertGreater(self.tick(1), 0)
        self.assertGreater(self.tick(), 0)
        self.assertEqual(len(set(self.requests)), 2)
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            self.assertEqual(
                [r[0] for r in db.execute("SELECT id FROM label_record ORDER BY id")],
                [1, 6, 7],
            )
            self.assertEqual(
                db.execute("SELECT forms FROM label_record WHERE id=6").fetchone()[0],
                "",
            )
            self.assertEqual(db.execute("SELECT part FROM scan").fetchone()[0], 1)
        stats = self.publish()
        self.assertEqual(stats["dumpNameEntities"], 3)
        with (
            closing(catalogue.reader(self.output)) as db,
            closing(sqlite3.connect(":memory:")) as game,
        ):
            game.executescript((ROOT / "server/knowledge.sql").read_text())
            prompt = "Name a work published by Studio"
            bank, _ = catalogue.play(
                db, game, catalogue.match_question(db, prompt)["key"], prompt
            )
            keys = dict(
                game.execute(
                    "SELECT term,entity FROM kb_answer_key WHERE bank=? AND form=0",
                    (bank,),
                )
            )
            self.assertEqual(keys["作品一"], "wd:Q1")
            self.assertIsNone(keys["shared name"])
            self.assertNotIn("alias 1", keys)
            proof = json.loads(
                game.execute(
                    "SELECT evidence FROM kb_member WHERE bank=? AND entity='wd:Q1'",
                    (bank,),
                ).fetchone()[0]
            )
            self.assertTrue(
                any(p.get("url", "").endswith("title=Q1&oldid=1011") for p in proof)
            )
        # A portable edition retains its observations even when the source cache is absent.
        bulk.path(self.root).unlink()
        self.assertEqual(self.publish()["edition"], stats["edition"])
        direct = entity_labels.file(self.root)
        with closing(sqlite3.connect(direct)) as db:
            db.executescript(entity_labels.RECORDS)
            entity_labels.put(db, [(1, "Work 1\tRenamed", 2_000_000_000, "~wikidata")])
            db.commit()
        self.assertEqual(self.publish()["dumpNameEntities"], 2)
        with closing(catalogue.reader(self.output)) as db:
            self.assertIsNone(
                db.execute("SELECT id FROM label_term WHERE term='作品一'").fetchone()
            )

    def test_bad_download_rolls_back_and_resumes(self):
        self.tick(1)
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            prior = list(db.execute("SELECT * FROM label_record"))
            pending = list(db.execute("SELECT * FROM task"))
        normal = self.source
        for damage in ("ignored range", "truncated", "wrong content range", "crc"):

            def corrupt(request, timeout, damage=damage):
                source = normal(request, timeout)
                data = source.read()
                if damage == "crc":
                    data = data[:9] + bytes([data[9] ^ 32]) + data[10:]
                if damage == "truncated":
                    data = data[: len(data) // 2]
                return Response(
                    data,
                    source.headers["Content-Range"]
                    if damage != "wrong content range"
                    else "bytes 0-1/2",
                    200 if damage == "ignored range" else 206,
                )

            with (
                patch.object(bulk.sources, "open_source", side_effect=corrupt),
                self.assertRaises(bulk.UPDATE_ERRORS),
            ):
                bulk.tick(self.world, self.root)
            with closing(sqlite3.connect(bulk.path(self.root))) as db:
                self.assertEqual(list(db.execute("SELECT * FROM label_record")), prior)
                self.assertEqual(list(db.execute("SELECT * FROM task")), pending)
                self.assertGreater(db.execute("SELECT next FROM scan").fetchone()[0], 0)
                db.execute("UPDATE scan SET next=0")
                db.commit()
        self.assertGreater(self.tick(), 0)

    def test_index_and_entity_identity_are_verified(self):
        self.shard[3]["sha1"] = "0" * 40
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.tick()
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            self.assertEqual(db.execute("SELECT indexed FROM scan").fetchone()[0], 0)
            self.assertFalse(list(db.execute("SELECT * FROM label_record")))
        actual = page(111, "Q1", [("fr", "Un")])
        for fragment, pages in [
            (actual, [(112, 1)]),
            (actual, [(111, 9)]),
            (actual.replace(b'"Un"', b'"XX"'), [(111, 1)]),
        ]:
            raw = bz2.compress(fragment)
            with self.assertRaises(ValueError):
                list(
                    bulk.read_block(io.BytesIO(raw), len(raw), pages, "20261101", False)
                )

    def test_discover_pins_a_complete_release_with_paired_files(self):
        date = "20261101"
        prefix = f"wikidatawiki-{date}-pages-articles-multistream"
        names = [prefix + "1.xml-p1p300.bz2", prefix + "-index1.txt-p1p300.bz2"]
        files = {
            name: {"url": f"/wikidatawiki/{date}/{name}", "size": 200, "sha1": "f" * 40}
            for name in names
        }

        def get(url):
            if url == bulk.BASE:
                return b'<a href="20261201/"></a><a href="20261101/"></a>'
            job = (
                {"status": "waiting"}
                if "20261201" in url
                else {"status": "done", "files": files}
            )
            return json.dumps({"jobs": {"articlesmultistreamdump": job}}).encode()

        with patch.object(bulk.sources, "fetch", side_effect=get):
            release, shards = bulk.discover("")
            self.assertEqual(release, date)
            self.assertEqual(shards, [(1, 300, files[names[0]], files[names[1]])])
            self.assertIsNone(bulk.discover(release))
            files[names[1]]["url"] = "https://example.org/foreign"
            with self.assertRaisesRegex(ValueError, "descriptor"):
                bulk.discover("")
            del files[names[1]]
            with self.assertRaises(KeyError):
                bulk.discover("")

    def test_missing_from_incomplete_new_release_preserves_last_observation(self):
        self.tick()
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            entity_labels.put(db, [(2, "Former", 1, "dump:20261001:111")])
            db.execute("UPDATE scan SET part=0,indexed=0")
            db.commit()
        self.tick(1)
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            self.assertEqual(
                db.execute("SELECT forms FROM label_record WHERE id=2").fetchone()[0],
                "Former",
            )
        self.tick()
        with closing(sqlite3.connect(bulk.path(self.root))) as db:
            self.assertEqual(
                db.execute(
                    "SELECT forms,origin FROM label_record WHERE id=2"
                ).fetchone(),
                ("", "dump:20261101:absent"),
            )
