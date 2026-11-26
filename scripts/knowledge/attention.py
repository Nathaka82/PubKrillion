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


def add_registry_readership(graph, publication, register, revision, url):
    if not graph.execute("SELECT 1 FROM sqlite_master WHERE name='readership_item'").fetchone():
        return
    # Retain the register's stable answer ID. A conflicting identity or an item
    # already represented by a qualifying Wikidata entity must not supply views twice.
    links = graph.execute("""WITH linked AS (
            SELECT x.id,min(x.item) item,min(x.url) url FROM readership_item x
            CROSS JOIN qualified q ON q.id=x.id
            WHERE (:register IS NULL OR x.source=:register)
                AND q.id<>'wd:Q'||CAST(substr(q.id,5) AS INTEGER)
            GROUP BY x.id HAVING min(x.item)=max(x.item)
        ) SELECT min(id),item,min(url) FROM linked
            WHERE NOT EXISTS (SELECT 1 FROM qualified q WHERE q.id='wd:Q'||linked.item)
            GROUP BY item HAVING count(*)=1""", {"register": register})
    has_items = publication.execute("SELECT 1 FROM sqlite_master WHERE name='item'").fetchone()
    incoming = "(SELECT incoming FROM item WHERE id=r.id)" if has_items else "NULL"
    mapped_count = 0
    while batch := links.fetchmany(500):
        observations = publication.execute(f"""SELECT r.id,r.views,{incoming}
            FROM json_each(?) j CROSS JOIN readership r ON r.id=j.value""",
            (json.dumps([row[1] for row in batch]),))
        values = {item: (views, count) for item, views, count in observations}
        mapped_count += len(values)
        graph.executemany("INSERT INTO answer_views VALUES(?,?,?,?,?)",
            ((entity, values[item][0], item, values[item][1], identity_url)
             for entity, item, identity_url in batch if item in values))
    if mapped_count:
        graph.execute("""WITH proofs AS (
            SELECT owner,json_group_array(json_object('item','Q'||item,'url',identity_url,'views',views)) items
            FROM (SELECT q.owner,v.item,v.identity_url,v.views FROM answer_views v
                CROSS JOIN qualified q ON q.id=v.id WHERE v.identity_url IS NOT NULL ORDER BY q.owner,v.item)
            GROUP BY owner
        ) UPDATE result SET evidence=json_insert(evidence,'$[#]',json_object('source','Wikidata QRank',
            'url',?,'revision',?,'readershipItems',json(proofs.items))) FROM proofs
            WHERE result.id=proofs.owner AND result.upper IS NULL""", (url, revision))


# bounds has one row per measured answer name. A shared name has at least the
# count of its most familiar owner and at most the sum across all its owners.
# An unmeasured owner leaves the upper bound open. Count each possible rival
# once, including every tie, to find the worst position in either direction.
RANK_BOUNDS = """
    , coverage AS (SELECT count(*) measured,count(high_value) complete FROM bounds)
    , endpoints(value,starts,ends) AS (
        SELECT low_value,count(*),0 FROM bounds GROUP BY low_value UNION ALL
        SELECT high_value,0,count(*) FROM bounds WHERE high_value IS NOT NULL GROUP BY high_value
    ), positions AS MATERIALIZED (
        SELECT value,sum(sum(starts)) OVER (ORDER BY value) below,
               sum(sum(ends)) OVER (ORDER BY value DESC) above FROM endpoints GROUP BY value
    ), ranked AS MATERIALIZED (
        SELECT b.owner,b.high_value,c.measured,
               hi.below+(:size-c.measured) rare_rank,lo.above+(:size-c.complete) common_rank
        FROM bounds b CROSS JOIN coverage c JOIN positions lo ON lo.value=b.low_value
        LEFT JOIN positions hi ON hi.value=b.high_value
    )
"""


def rank_familiarity(graph, size):
    """Compare like measurements across the bank before awarding an estimated top score."""
    # Zipf frequency is logarithmic. Convert to linear weights before combining owners.
    graph.create_function("familiarity_weight", 2, lambda frequency, reach: (10 ** frequency) * reach,
                          deterministic=True)
    coverage = []
    for method, table, spread, offset in (
        ("language coverage", "familiarity", "languages", 0),
        ("graph links", "graph_familiarity", "incoming", 1),
    ):
        if not graph.execute("SELECT 1 FROM result WHERE proxy=? AND depth BETWEEN 2 AND 4 LIMIT 1",
                             (method,)).fetchone():
            continue
        graph.execute("DROP TABLE IF EXISTS temp.familiarity_bounds")
        graph.execute(f"""CREATE TEMP TABLE familiarity_bounds AS WITH per_item AS (
            SELECT q.owner,q.id,max(CASE WHEN f.frequency>0 AND f.{spread}>0
                THEN familiarity_weight(f.frequency,f.{spread}+{offset}) END) weight
            FROM qualified q LEFT JOIN {table} f ON f.id=q.id GROUP BY q.id
        ) SELECT owner,max(weight) low_value,CASE WHEN count(weight)=count(*) THEN sum(weight) END high_value
            FROM per_item GROUP BY owner HAVING count(weight)>0""")
        measured, full = graph.execute("SELECT count(*),count(high_value) FROM familiarity_bounds").fetchone()
        coverage.append({"method": method, "measuredAnswers": measured, "fullyMeasuredAnswers": full})
        if (size-measured+1)*100 > 2*size:
            continue
        graph.execute("WITH bounds AS (SELECT * FROM familiarity_bounds)" + RANK_BOUNDS + """
            UPDATE result SET proxy_rank=rare_rank FROM ranked
            WHERE result.id=ranked.owner AND result.proxy=:method AND result.depth BETWEEN 2 AND 4
                AND result.upper IS NULL AND result.survey_total=0 AND rare_rank*100<=2*:size""",
            {"method": method, "size": size})
    return coverage


def score(graph, publication, size, *, register=None):
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
        CREATE TEMP TABLE answer_views(id TEXT PRIMARY KEY,views INTEGER NOT NULL,item INTEGER,
            incoming INTEGER,identity_url TEXT) WITHOUT ROWID;""")
    items = graph.execute("""SELECT CAST(substr(id,5) AS INTEGER) FROM qualified
        WHERE id='wd:Q'||CAST(substr(id,5) AS INTEGER)""")
    while batch := items.fetchmany(500):
        matches = publication.execute("""SELECT 'wd:Q'||r.id,r.views FROM json_each(?) j
            CROSS JOIN readership r ON r.id=j.value""", (json.dumps([r[0] for r in batch]),))
        graph.executemany("INSERT INTO answer_views(id,views) VALUES(?,?)", matches)
    add_registry_readership(graph, publication, register, info[0], info[1])
    graph.execute("""CREATE TEMP TABLE name_views AS
        SELECT q.owner,max(v.views) views,sum(v.views) total_views,count(v.views)=count(*) complete,
            min(max(coalesce((SELECT max(g.incoming) FROM graph_familiarity g WHERE g.id=q.id),0),
                coalesce(v.incoming,0)) BETWEEN 1 AND 6) sparse
        FROM qualified q LEFT JOIN answer_views v USING(id) GROUP BY q.owner HAVING count(v.views)>0""")
    covered, full = graph.execute("SELECT count(*),coalesce(sum(complete),0) FROM name_views").fetchone()
    if not covered:
        return None
    if covered >= 12:
        graph.execute("""WITH bounds(owner,low_value,high_value) AS MATERIALIZED (
            SELECT owner,views,CASE WHEN complete THEN total_views END FROM name_views
        )""" + RANK_BOUNDS + """ UPDATE result SET depth=CASE
                WHEN common_rank*100<=15*:size THEN 1
                WHEN depth=1 THEN 1
                WHEN sparse AND rare_rank*100<=10*:size THEN max(depth,4)
                WHEN rare_rank*100<=25*:size THEN max(depth,3)
                ELSE depth END,
            proxy=CASE WHEN common_rank*100<=75*:size THEN NULL ELSE proxy END,
            rare_views=CASE WHEN sparse AND depth>1 AND survey_total=0
                AND rare_rank*100<=2*:size THEN high_value END,
            view_rank_bound=rare_rank
            FROM ranked JOIN name_views USING(owner) WHERE result.id=ranked.owner AND result.upper IS NULL""",
            {"size": size})
    return {"source": "Wikidata QRank", "url": info[1], "revision": info[0],
            "coveredAnswers": covered, "fullyCoveredAnswers": full}
