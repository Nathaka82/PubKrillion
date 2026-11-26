"""Read entity-owned names from indexed ranges of a dated Wikidata XML dump."""

import argparse
import bz2
import hashlib
import http.client
import io
import itertools
import json
import re
import shutil
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import entity_labels
import sources
from world_index import install_lock

ROOT = "https://dumps.wikimedia.org"
BASE = ROOT + "/wikidatawiki/"
GROUP_BYTES = 8_000_000
UPDATE_ERRORS = (
    OSError,
    EOFError,
    ValueError,
    ET.ParseError,
    KeyError,
    sqlite3.Error,
    http.client.HTTPException,
)
SCHEMA = (
    entity_labels.RECORDS
    + """
CREATE TABLE IF NOT EXISTS scan(id INTEGER PRIMARY KEY, release TEXT, parts TEXT, part INTEGER,
    indexed INTEGER, world TEXT, next INTEGER);
INSERT OR IGNORE INTO scan VALUES(1,'','[]',0,0,'',0);
CREATE TABLE IF NOT EXISTS task(start INTEGER PRIMARY KEY,stop INTEGER,blocks TEXT,density INTEGER);
CREATE INDEX IF NOT EXISTS task_order ON task(density,start);
CREATE TABLE IF NOT EXISTS deferred(part INTEGER,start INTEGER,stop INTEGER,blocks TEXT,
    attempts INTEGER,due INTEGER,error TEXT,PRIMARY KEY(part,start)) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS deferred_by_due ON deferred(due,part,start);
CREATE TABLE IF NOT EXISTS release_check(id INTEGER PRIMARY KEY,next INTEGER);
INSERT OR IGNORE INTO release_check VALUES(1,0);
"""
)


class InvalidBlock(ValueError):
    """The indexed range was served, but its entity data failed validation."""


def path(storage):
    return storage / "knowledge-sources" / "wikimedia-names" / "observations.sqlite"


def discover(after):
    listing = sources.fetch(BASE).decode("utf8")
    dates = sorted(set(re.findall(r'href="([0-9]{8})/"', listing)), reverse=True)
    if not dates:
        raise ValueError("Wikimedia returned no dated Wikidata dump directories.")
    for date in dates:
        if date <= after:
            break
        try:
            status = json.loads(sources.fetch(BASE + date + "/dumpstatus.json"))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                continue
            raise
        job = status["jobs"].get("articlesmultistreamdump", {})
        if job.get("status") != "done":
            continue
        files = job["files"]
        shards = []
        part_numbers = []
        prefix = f"wikidatawiki-{date}-pages-articles-multistream"
        for name, data in files.items():
            match = re.fullmatch(
                prefix + r"([1-9][0-9]*)\.xml-(p[0-9]+p[0-9]+)\.bz2", name
            )
            if not match:
                continue
            index = f"{prefix}-index{match[1]}.txt-{match[2]}.bz2"
            for filename in (name, index):
                meta = files[filename]
                if (
                    meta["url"] != f"/wikidatawiki/{date}/{filename}"
                    or not isinstance(meta["size"], int)
                    or meta["size"] <= 0
                    or not re.fullmatch("[a-f0-9]{40}", meta["sha1"])
                ):
                    raise ValueError("Invalid Wikidata dump file descriptor.")
            first, last = map(int, match[2][1:].split("p"))
            shards.append((first, last, data, files[index]))
            part_numbers.append(int(match[1]))
        shards.sort(key=lambda s: s[0])
        if (
            not shards
            or len(shards) * 2 != len(files)
            or sorted(part_numbers) != list(range(1, len(shards) + 1))
            or any(
                end < begin or begin <= prior
                for (begin, end, *_), prior in zip(shards, [0] + [s[1] for s in shards])
            )
        ):
            raise ValueError(
                "Wikimedia returned an incomplete or overlapping shard inventory."
            )
        return date, shards


def index_tasks(db, shard):
    first, last, xml, index = shard
    if index["size"] > 64_000_000:
        raise ValueError("Wikidata shard index exceeds the supported download size.")
    data = sources.fetch(ROOT + index["url"], index["size"], timeout=60)
    if len(data) != index["size"] or hashlib.sha1(data).hexdigest() != index["sha1"]:
        raise ValueError(
            "The downloaded Wikidata shard index failed its release checksum."
        )

    def entries():
        previous = (-1, 0)
        with bz2.open(io.BytesIO(data), "rb") as stream:
            for line in stream:
                start, page, title = line.rstrip(b"\r\n").split(b":", 2)
                offset, page = int(start), int(page)
                if (
                    not 0 <= offset < xml["size"]
                    or not first <= page <= last
                    or offset < previous[0]
                    or page <= previous[1]
                ):
                    raise ValueError(
                        "Wikidata shard offsets or page identifiers are out of order."
                    )
                previous = offset, page
                yield (
                    offset,
                    page,
                    int(title[1:]) if re.fullmatch(rb"Q[1-9][0-9]*", title) else None,
                )

    def blocks():
        batch = []
        for offset, pages in itertools.groupby(entries(), key=lambda row: row[0]):
            batch.append((offset, [(p, q) for _, p, q in pages if q is not None]))
            if len(batch) == 64:
                yield from matching(batch)
                batch.clear()
        yield from matching(batch)

    def matching(batch):
        ids = [q for _, pages in batch for _, q in pages]
        known = {
            r[0]
            for r in db.execute(
                "SELECT id FROM world.entity WHERE id IN (SELECT value FROM json_each(?))",
                (json.dumps(ids),),
            )
        }
        for start, pages in batch:
            yield start, [(page, qid) for page, qid in pages if qid in known]

    task = []
    count = 0
    pending = iter(blocks())
    current = next(pending, None)
    if current is None:
        raise ValueError("Wikidata shard index has no pages.")
    for stop, following in itertools.chain(pending, [(xml["size"], None)]):
        start, pages = current
        if task and (not pages or stop - task[0][0] > GROUP_BYTES or len(task) >= 32):
            yield task[0][0], start, json.dumps(task), (start - task[0][0]) // count
            task, count = [], 0
        if pages:
            task.append((start, pages))
            count += len(pages)
        current = stop, following
    if task:
        yield task[0][0], stop, json.dumps(task), (stop - task[0][0]) // count


def page_record(page, expected, release, captured):
    title = page.findtext("title", "")
    if not re.fullmatch(r"Q[1-9][0-9]*", title) or int(title[1:]) not in expected:
        return
    qid = int(title[1:])
    if page.findtext("ns") != "0" or page.findtext("id") != str(expected.pop(qid)):
        raise ValueError("Wikidata page belongs to a different indexed entity.")
    rev = page.find("revision")
    if rev is None or not re.fullmatch("[1-9][0-9]*", rev.findtext("id", "")):
        raise ValueError("Wikidata page has no revision identifier.")
    stamp = datetime.strptime(
        rev.findtext("timestamp", ""), "%Y-%m-%dT%H:%M:%SZ"
    ).replace(tzinfo=UTC)
    content = rev.findtext("text")
    checksum = rev.findtext("sha1", "")
    if (
        content is None
        or not re.fullmatch("[0-9a-z]{1,31}", checksum)
        or int(checksum, 36)
        != int.from_bytes(hashlib.sha1(content.encode("utf8")).digest(), "big")
    ):
        raise ValueError("Wikidata revision text failed its published checksum.")
    redirect = page.find("redirect")
    if redirect is not None:
        if not re.fullmatch("Q[1-9][0-9]*", redirect.get("title", "")):
            raise ValueError("Unrecognized Wikidata entity redirect.")
        names = ""
    else:
        if rev.findtext("model") != "wikibase-item":
            raise ValueError("Indexed Wikidata page is missing its item content.")
        item = json.loads(content)
        if (
            not isinstance(item, dict)
            or item.get("type") != "item"
            or item.get("id") != title
        ):
            raise ValueError("Wikidata revision belongs to a different item.")
        record = sources.entity_name_record(title, item, required=True)
        names = entity_labels.forms(record[1], record[2]) if record else ""
    return (
        qid,
        names,
        max(captured, int(stamp.timestamp())),
        f"dump:{release}:{rev.findtext('id')}",
    )


def read_block(response, length, pages, release, last):
    expected = {qid: page for page, qid in pages}
    if len(expected) != len(pages):
        raise ValueError("Duplicate Wikidata entity in a dump block.")
    captured = int(datetime.strptime(release, "%Y%m%d").replace(tzinfo=UTC).timestamp())
    xml = ET.XMLPullParser(events=("start", "end"))
    xml.feed("<root>")
    root = next(xml.read_events())[1]
    decoder = bz2.BZ2Decompressor()
    footer = bytearray()
    while length:
        raw = response.read(min(65536, length))
        if not raw:
            raise OSError("Wikidata dump range ended early.")
        length -= len(raw)
        if decoder.eof:
            footer.extend(raw)
        else:
            while True:
                try:
                    chunk = decoder.decompress(raw, max_length=262144)
                except OSError as error:
                    raise InvalidBlock(
                        "Wikidata bzip2 member failed validation."
                    ) from error
                xml.feed(chunk)
                for event, page in xml.read_events():
                    if event == "end" and page.tag == "page":
                        record = page_record(page, expected, release, captured)
                        if record:
                            yield record
                        root.remove(page)
                if decoder.eof:
                    footer.extend(decoder.unused_data)
                    break
                if decoder.needs_input:
                    break
                raw = b""
        if len(footer) > 1024:
            raise ValueError("Unexpected content beyond an indexed bzip2 member.")
    if not decoder.eof or expected:
        raise ValueError("Wikidata dump block is incomplete.")
    if footer:
        try:
            valid = last and bz2.decompress(footer).strip() == b"</mediawiki>"
        except OSError as error:
            raise InvalidBlock("Wikidata dump footer failed validation.") from error
        if not valid:
            raise ValueError("Unexpected trailing Wikidata dump member.")
    xml.feed("</root>")
    xml.close()


def capture(db, release, xml, task):
    start, stop, blocks = task
    request = urllib.request.Request(
        ROOT + xml["url"],
        headers={
            "User-Agent": sources.AGENT,
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{stop - 1}",
        },
    )
    with sources.open_source(request, 60) as response:
        if (
            response.status != 206
            or response.headers.get("Content-Range")
            != f"bytes {start}-{stop - 1}/{xml['size']}"
            or response.headers.get("Content-Encoding", "identity") != "identity"
        ):
            raise ValueError(
                "Wikimedia did not serve the indexed range of the dated file."
            )
        groups = json.loads(blocks)
        for (offset, pages), end in zip(groups, [g[0] for g in groups[1:]] + [stop]):
            try:
                entity_labels.put(
                    db,
                    read_block(
                        response, end - offset, pages, release, end == xml["size"]
                    ),
                )
            except (ValueError, ET.ParseError, EOFError) as error:
                raise InvalidBlock(str(error)) from error
        if response.read(1):
            raise ValueError("Wikidata dump range exceeds its declared boundary.")


def process(db, release, parts, part, task, *, retry=False):
    start, stop, blocks = task
    try:
        with db:
            capture(db, release, parts[part][2], task)
            if retry:
                db.execute(
                    "DELETE FROM deferred WHERE part=? AND start=?", (part, start)
                )
            else:
                db.execute("DELETE FROM task WHERE start=?", (start,))
        return stop - start
    except InvalidBlock as error:
        # Retry a bad group independently. Other shards can still contribute
        # checked names, but this gap must prevent any inference of absence.
        groups = json.loads(blocks)
        if not retry and len(groups) > 1:
            # The release index gives the exact boundary of each independently
            # compressed member. Keep healthy siblings eligible for this pass.
            middle = len(groups) // 2
            with db:
                db.execute("DELETE FROM task WHERE start=?", (start,))
                for half, end in (
                    (groups[:middle], groups[middle][0]),
                    (groups[middle:], stop),
                ):
                    begin = half[0][0]
                    owners = sum(len(pages) for _, pages in half)
                    db.execute(
                        "INSERT INTO task VALUES(?,?,?,?)",
                        (begin, end, json.dumps(half), (end - begin) // owners),
                    )
            return 0
        with db:
            db.execute(
                """INSERT INTO deferred VALUES(?,?,?,?,1,unixepoch()+900,?)
                ON CONFLICT(part,start) DO UPDATE SET attempts=attempts+1,
                due=unixepoch()+min(86400,900*(1<<min(attempts,7))),error=excluded.error""",
                (part, start, stop, blocks, str(error)),
            )
            if not retry:
                db.execute("DELETE FROM task WHERE start=?", (start,))
        print(
            f"Wikidata {release}, shard {part + 1}, bytes {start}-{stop - 1} deferred: {error}",
            flush=True,
        )
        return 0


def retire_absent(db, release):
    if db.execute("SELECT 1 FROM deferred LIMIT 1").fetchone():
        return
    captured = int(datetime.strptime(release, "%Y%m%d").replace(tzinfo=UTC).timestamp())
    db.execute(
        """UPDATE label_record SET forms='',origin=?,checked=? WHERE checked<=?
        AND origin NOT LIKE ? AND id IN (SELECT id FROM world.entity)""",
        (f"dump:{release}:absent", captured, captured, f"dump:{release}:%"),
    )


def reset(db, release, parts, world):
    with db:
        db.execute("DELETE FROM task")
        db.execute("DELETE FROM deferred")
        db.execute("UPDATE release_check SET next=0")
        db.execute(
            "UPDATE scan SET release=?,parts=?,part=0,indexed=0,world=?,next=0",
            (release, json.dumps(parts), world),
        )


def advance(db, world_key, budget):
    release, parts, part, indexed, world = db.execute(
        "SELECT release,parts,part,indexed,world FROM scan"
    ).fetchone()
    parts = json.loads(parts)
    verified = spent = 0
    if parts and world == world_key:
        # Reserve at most one retry per pass while fresh shards are available.
        retry = db.execute("""SELECT part,start,stop,blocks FROM deferred WHERE due<=unixepoch()
            ORDER BY due,part,start LIMIT 1""").fetchone()
        if retry:
            verified = process(db, release, parts, retry[0], retry[1:], retry=True)
            spent = retry[2] - retry[1]
            if verified and part == len(parts):
                with db:
                    retire_absent(db, release)
    if world_key != world or part >= len(parts):
        now = int(time.time())
        check = db.execute("SELECT next FROM release_check").fetchone()[0]
        new = None
        if world_key != world or now >= check:
            new = discover(release if world_key == world else "")
            with db:
                check = now + 86400
                db.execute("UPDATE release_check SET next=?", (check,))
        if not new:
            due = db.execute("SELECT min(due) FROM deferred").fetchone()[0]
            with db:
                db.execute(
                    "UPDATE scan SET next=?",
                    (min(due, check) if due is not None else check,),
                )
            return verified
        release, parts = new
        reset(db, release, parts, world_key)
        part, indexed = 0, False
    shard = parts[part]
    if not indexed:
        with db:
            db.executemany("INSERT INTO task VALUES(?,?,?,?)", index_tasks(db, shard))
            db.execute("UPDATE scan SET indexed=1")
    while spent < budget:
        task = db.execute(
            "SELECT start,stop,blocks FROM task ORDER BY density,start LIMIT 1"
        ).fetchone()
        if task is None:
            with db:
                db.execute("UPDATE scan SET part=part+1,indexed=0")
                if part + 1 == len(parts):
                    retire_absent(db, release)
            break
        verified += process(db, release, parts, part, task)
        spent += task[1] - task[0]
    return verified


def tick(world, storage, budget=32_000_000):
    cache = path(storage)
    cache.parent.mkdir(parents=True, exist_ok=True)
    stat = world.stat()
    world_key = f"{world.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
    with install_lock(cache.parent), closing(sqlite3.connect(cache, uri=True)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        db.execute(
            "ATTACH DATABASE ? AS world",
            (world.resolve().as_uri() + "?mode=ro&immutable=1",),
        )
        if db.execute("SELECT next FROM scan").fetchone()[0] > time.time():
            return 0
        try:
            if shutil.disk_usage(storage).free < 2_000_000_000:
                raise ValueError(
                    "Wikimedia name capture needs 2 GB free for publication."
                )
            return advance(db, world_key, budget)
        except UPDATE_ERRORS as error:
            with db:
                wait = max(900, sources.retry_wait("dumps.wikimedia.org"))
                db.execute("UPDATE scan SET next=?", (int(time.time()) + wait,))
            if isinstance(error, urllib.error.HTTPError) and error.code == 404:
                release = db.execute("SELECT release FROM scan").fetchone()[0]
                if newer := discover(release):
                    reset(db, *newer, world_key)
                    return 0
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world", type=Path, required=True)
    parser.add_argument("--storage", type=Path, default=Path("storage"))
    parser.add_argument(
        "--megabytes",
        type=int,
        default=32,
        help="Compressed bytes per pass, rounded up to a group",
    )
    args = parser.parse_args()
    if args.megabytes <= 0:
        parser.error("--megabytes must be positive")
    used = tick(args.world, args.storage, args.megabytes * 1_000_000)
    with closing(sqlite3.connect(path(args.storage))) as db:
        release, part = db.execute("SELECT release,part FROM scan").fetchone()
        count = db.execute("SELECT count(*) FROM label_record").fetchone()[0]
        deferred = db.execute("SELECT count(*) FROM deferred").fetchone()[0]
        incomplete = db.execute(
            "SELECT count(DISTINCT part) FROM deferred WHERE part<?", (part,)
        ).fetchone()[0]
        print(
            json.dumps(
                {
                    "release": release,
                    "completedShards": part - incomplete,
                    "observedEntities": count,
                    "deferredRanges": deferred,
                    "compressedBytes": used,
                }
            )
        )
