"""Use a published year of multilingual Wikimedia readership as a familiarity signal."""

import gzip
import hashlib
import http.client
import json
import re
import sqlite3
import tempfile
import time
import urllib.request
import zlib
from contextlib import closing
from pathlib import Path

from sources import AGENT, fetch
from world_index import install_lock

BASE = "https://qrank.toolforge.org/download/"
UPDATE_ERRORS = (OSError, ValueError, KeyError, TypeError, sqlite3.Error, EOFError, OverflowError,
                 http.client.HTTPException, zlib.error)
SCHEMA = """
CREATE TABLE readership(id INTEGER PRIMARY KEY,views INTEGER NOT NULL);
CREATE TABLE readership_release(revision TEXT PRIMARY KEY,url TEXT NOT NULL,n INTEGER NOT NULL) WITHOUT ROWID;
"""


def location(storage):
    return storage / "knowledge-sources" / "qrank" / "readership.sqlite"


def descriptor():
    manifest = json.loads(fetch(BASE + "datapackage.json"))
    resource, = [r for r in manifest["resources"] if r["name"] == "qrank"]
    path, checksum, size = resource["path"], resource["hash"], resource["bytes"]
    if (not re.fullmatch(r"qrank-[0-9]{8}\.csv\.gz", path)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", checksum)
            or type(size) is not int or not 0 < size <= 1_000_000_000):
        raise ValueError("Invalid QRank release descriptor.")
    return BASE + path, checksum[7:], size


def import_snapshot(archive, output, url, checksum, size):
    with archive.open("rb") as raw:
        if archive.stat().st_size != size or hashlib.file_digest(raw, "sha256").hexdigest() != checksum:
            raise ValueError("The readership download failed its published size or SHA-256 check.")
    with gzip.open(archive, "rb") as lines, closing(sqlite3.connect(output)) as db:
        if next(lines, b"").rstrip(b"\r\n") != b"Entity,QRank":
            raise ValueError("Unexpected QRank columns.")
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;" + SCHEMA)
        db.execute("CREATE TEMP TABLE staging(id INTEGER,views INTEGER)")

        def rows():
            for line in lines:
                qid, views = line.rstrip(b"\r\n").split(b",")
                if (not qid.startswith(b"Q") or not qid[1:].isdigit() or qid[1:2] == b"0"
                        or not views.isdigit() or int(views) < 1):
                    raise ValueError("Invalid item or readership in the QRank release.")
                yield int(qid[1:]), int(views)

        db.executemany("INSERT INTO staging VALUES(?,?)", rows())
        db.execute("INSERT INTO readership SELECT id,views FROM staging ORDER BY id")
        n = db.execute("SELECT count(*) FROM readership").fetchone()[0]
        if not n:
            raise ValueError("The QRank release has no readership records.")
        db.execute("INSERT INTO readership_release VALUES(?,?,?)", (checksum, url, n))
        db.commit()


def release(path):
    if path.is_file():
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='readership_release'").fetchone():
                return db.execute("SELECT revision,url,n FROM readership_release").fetchone()


def refresh(storage):
    path = location(storage)
    path.parent.mkdir(parents=True, exist_ok=True)
    checked = path.parent / "checked"
    with install_lock(path.parent):
        if checked.is_file() and time.time() - checked.stat().st_mtime < (86400 if path.is_file() else 3600):
            return
        checked.touch()
        url, checksum, size = descriptor()
        previous = release(path)
        if previous and previous[:2] == (checksum, url):
            return
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            folder = Path(directory)
            archive, stage = folder / "release.gz", folder / "build.sqlite"
            request = urllib.request.Request(url, headers={"User-Agent": AGENT})
            with urllib.request.urlopen(request, timeout=90) as remote, archive.open("wb") as file:
                while chunk := remote.read(1024 * 1024):
                    if file.tell() + len(chunk) > size:
                        raise ValueError("The QRank download exceeded its published size.")
                    file.write(chunk)
            import_snapshot(archive, stage, url, checksum, size)
            stage.replace(path)


def source(storage, previous):
    for path in (location(storage), previous):
        if info := release(path):
            return path, info
    return None, None


def publish(db, path, expected):
    db.execute("ATTACH DATABASE ? AS attention_source", (path.resolve().as_uri() + "?mode=ro",))
    actual = db.execute("SELECT revision,url,n FROM attention_source.readership_release").fetchone()
    if not actual or tuple(actual) != expected:
        raise ValueError("The readership release changed during publication.")
    db.execute("INSERT INTO main.readership SELECT * FROM attention_source.readership ORDER BY id")
    db.execute("INSERT INTO main.readership_release SELECT * FROM attention_source.readership_release")
    db.commit()
    db.execute("DETACH DATABASE attention_source")


def score(graph, publication, size):
    """Measure only already-qualified answer names. An unlisted QID has unknown readership."""
    if publication is None or not publication.execute(
        "SELECT 1 FROM sqlite_master WHERE name='readership_release'"
    ).fetchone():
        return None
    info = publication.execute("SELECT revision,url FROM readership_release").fetchone()
    if not info:
        return None
    graph.executescript("""DROP TABLE IF EXISTS temp.answer_views;
        DROP TABLE IF EXISTS temp.name_views;
        CREATE TEMP TABLE answer_views(id TEXT PRIMARY KEY,views INTEGER NOT NULL) WITHOUT ROWID;""")
    items = graph.execute("""SELECT CAST(substr(id,5) AS INTEGER) FROM qualified
        WHERE id='wd:Q'||CAST(substr(id,5) AS INTEGER)""")
    while batch := items.fetchmany(500):
        matches = publication.execute("""SELECT 'wd:Q'||r.id,r.views FROM json_each(?) j
            CROSS JOIN readership r ON r.id=j.value""", (json.dumps([r[0] for r in batch]),))
        graph.executemany("INSERT INTO answer_views VALUES(?,?)", matches)
    graph.execute("""CREATE TEMP TABLE name_views AS
        SELECT q.owner,max(v.views) views,count(v.views)=count(*) complete,
            min(coalesce((SELECT max(g.incoming) FROM graph_familiarity g WHERE g.id=q.id),0) BETWEEN 1 AND 6) sparse
        FROM qualified q LEFT JOIN answer_views v USING(id) GROUP BY q.owner HAVING count(v.views)>0""")
    covered, full = graph.execute("SELECT count(*),coalesce(sum(complete),0) FROM name_views").fetchone()
    if not covered:
        return None
    if covered >= 12:
        # Unmeasured names could be anywhere in the ranking. Use the least favourable
        # position for each bonus and include every tie at its boundary.
        graph.execute("""WITH ranked AS MATERIALIZED (
            SELECT owner,views,complete,sparse,
                count(*) OVER (ORDER BY views) low,
                count(*) OVER (ORDER BY views DESC) high
            FROM name_views
        ) UPDATE result SET depth=CASE
                WHEN (high+:unknown)*100<=15*:size THEN 1
                WHEN depth=1 THEN 1
                WHEN complete AND sparse AND (low+:unknown)*100<=10*:size THEN max(depth,4)
                WHEN complete AND (low+:unknown)*100<=25*:size THEN max(depth,3)
                ELSE depth END,
            proxy=CASE WHEN (high+:unknown)*100<=75*:size THEN NULL ELSE proxy END
            FROM ranked WHERE result.id=ranked.owner AND result.upper IS NULL""",
            {"unknown": size-full, "size": size})
    return {"source": "Wikidata QRank", "url": info[1], "revision": info[0],
            "coveredAnswers": covered, "fullyCoveredAnswers": full}
