"""Refresh the parent classes of the entire local graph from Wikidata in resumable batches."""

import argparse
import hashlib
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path

from world_facts import bindings, identity
from world_index import index_path, install_lock

SOURCE = "wikidata-current-taxonomy"
URL = "https://www.wikidata.org/wiki/Property:P279"


def location(storage):
    return storage / "knowledge-sources" / "taxonomy" / "parents.sqlite"


def input_tag(world):
    stamp = world.stat()
    return str(world.resolve()), stamp.st_size, stamp.st_mtime_ns


def due(world, file):
    refresh_at = 0
    if file.exists():
        with closing(sqlite3.connect(file.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            if db.execute(
                "SELECT 1 FROM input WHERE (path,size,modified)=(?,?,?)", input_tag(world)
            ).fetchone():
                refresh_at = file.stat().st_mtime + 7 * 86400
    retry = file.with_suffix(".retry")
    return max(refresh_at, retry.stat().st_mtime + 900 if retry.exists() else 0)


def signature(db, schema="main"):
    result = hashlib.sha256()
    for child, parent in db.execute(f"""SELECT n.id,e.parent FROM {schema}.node n LEFT JOIN {schema}.edge e
            ON e.child=n.id ORDER BY n.id,e.parent"""):
        result.update(f"{child}\t{parent}\n".encode())
    return result.hexdigest()


def capture(world, file, *, pages=None, batch_size=350):
    file.parent.mkdir(parents=True, exist_ok=True)
    partial = file.with_suffix(".partial")
    with install_lock(file.parent), closing(sqlite3.connect(partial.resolve().as_uri(), uri=True)) as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS input(path,size,modified,revision);
            CREATE TABLE IF NOT EXISTS node(id INTEGER PRIMARY KEY,done INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS pending ON node(id) WHERE done=0;
            CREATE TABLE IF NOT EXISTS edge(child INTEGER,parent INTEGER,PRIMARY KEY(child,parent)) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS release(world TEXT,revision TEXT,captured INTEGER);""")
        tag = input_tag(world)
        if not db.execute("SELECT 1 FROM input WHERE (path,size,modified)=(?,?,?)", tag).fetchone():
            with world.open("rb") as stream:
                revision = hashlib.file_digest(stream, "sha256").hexdigest()
            db.execute("ATTACH DATABASE ? AS world", (world.resolve().as_uri() + "?mode=ro",))
            with db:
                for table in ("edge", "node", "release", "input"):
                    db.execute(f"DELETE FROM {table}")
                db.execute("INSERT INTO input VALUES(?,?,?,?)", (*tag, revision))
                db.execute("""INSERT OR IGNORE INTO node(id) SELECT o FROM world.triple WHERE p IN (31,106,279)
                           UNION SELECT s FROM world.triple WHERE p=279""")
        completed = 0
        while pages is None or completed < pages:
            batch = {
                r[0]
                for r in db.execute("SELECT id FROM node WHERE done=0 ORDER BY id LIMIT ?", (batch_size,))
            }
            if not batch:
                break
            terms = " ".join("wd:Q" + str(item) for item in sorted(batch))
            rows = bindings(f"""SELECT DISTINCT ?child ?parent WHERE {{ VALUES ?child {{ {terms} }}
                OPTIONAL {{ ?child wdt:P279 ?parent .
                FILTER(STRSTARTS(STR(?parent),"http://www.wikidata.org/entity/Q")) }} }}""")
            seen, edges = set(), set()
            for row in rows:
                child = int(identity(row["child"]["value"])[1:])
                seen.add(child)
                if "parent" in row:
                    parent = int(identity(row["parent"]["value"])[1:])
                    if parent != child:
                        edges.add((child, parent))
            if seen != batch:
                raise ValueError("The taxonomy provider did not account for every requested class.")
            with db:
                db.executemany("INSERT OR IGNORE INTO edge VALUES(?,?)", sorted(edges))
                db.executemany("INSERT OR IGNORE INTO node(id) VALUES(?)", ((p,) for _, p in edges))
                db.executemany("UPDATE node SET done=1 WHERE id=?", ((c,) for c in batch))
            completed += 1
            if completed % 50 == 0:
                print(
                    "Checked source classes:",
                    db.execute("SELECT count(*) FROM node WHERE done=1").fetchone()[0],
                    flush=True,
                )
        if db.execute("SELECT 1 FROM node WHERE done=0 LIMIT 1").fetchone():
            return False
        with db:
            db.execute("DELETE FROM release")
            db.execute("INSERT INTO release SELECT revision,?,unixepoch() FROM input", (signature(db),))
        # Windows requires the database handle to close before replacement.
        db.close()
        os.replace(partial, file)
        file.with_suffix(".retry").unlink(missing_ok=True)
        return True


def tick(world, file, *, pages=8):
    try:
        if time.time() < due(world, file):
            return False
        return capture(world, file, pages=pages)
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        file.with_suffix(".retry").touch()
        print(f"Taxonomy refresh deferred: {error}", flush=True)
        return False


def attach(db, file, fingerprint):
    if not file.is_file():
        return None
    db.execute("ATTACH DATABASE ? AS taxonomy", (file.resolve().as_uri() + "?mode=ro&immutable=1",))
    row = db.execute(
        "SELECT revision,captured FROM taxonomy.release WHERE world=?", (fingerprint,)
    ).fetchone()
    if not row:
        return None
    if (
        signature(db, "taxonomy") != row[0]
        or db.execute("SELECT 1 FROM taxonomy.node WHERE done=0 LIMIT 1").fetchone()
    ):
        raise ValueError("The published taxonomy failed verification.")
    db.execute("INSERT INTO source VALUES(?,?,?,?, '{}')", (SOURCE, row[0], URL, row[1]))
    return row[0]


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--db", type=Path, default=Path(os.environ.get("DB_PATH", "storage/quiz.sqlite")))
    cli.add_argument("--world", type=Path)
    args = cli.parse_args()
    destination = location(args.db.parent)
    capture(args.world or index_path(args.db), destination)
    print("Published", destination, flush=True)
