"""Install the complete Wikidata5M transductive graph as a read-only quiz index."""

import argparse
import contextlib
import gzip
import hashlib
import os
import shutil
import sqlite3
import sys
import tarfile
import urllib.request
from pathlib import Path

from graph import ROOT, norm

RELEASE = "6b2b09672129e280c0c9da97ab58154e9d535e6b"
SOURCE = "https://deepgraphlearning.github.io/project/wikidata5m"
BASE = f"https://huggingface.co/datasets/intfloat/wikidata5m/resolve/{RELEASE}/"
ARCHIVES = {
    "wikidata5m_transductive.tar.gz": "383160990b41c0905fc03f4a8afbb9b12be1ca3591e026bde6cdc94a59542597",
    "wikidata5m_alias.tar.gz": "0330f580c9f7a57cbad949ac380835fdd2a2e14d96cc0f13fc435401d6b463a8",
}
INDEX_NAME = "wikidata5m-v1.sqlite"
TRIPLE_FILES = {
    f"wikidata5m_transductive_{split}.txt" for split in ("train", "valid", "test")
}
TITLE_RELEASE = "4e2b0b368762a15a3dafd1bcf353825513851695"


def add_titles(db):
    source = ROOT / "data/knowledge/wikimedia-en-titles.tsv.gz"
    with source.open("rb") as raw:
        if (
            hashlib.file_digest(raw, "sha256").hexdigest()
            != "d2fe090091016f7a8577a3c6af6e1fd7b1169daf37acdcd101a78c431fe582d8"
        ):
            raise ValueError(
                "The bundled Wikimedia title projection failed its checksum."
            )
    db.execute("CREATE TABLE title(id INTEGER PRIMARY KEY,name TEXT,term TEXT)")

    def titles(stream):
        for line in stream:
            qid, text = line.rstrip("\n").split("\t", 1)
            yield int(qid), text, norm(text)

    with gzip.open(source, "rt", encoding="utf8") as stream:
        db.executemany("INSERT INTO title VALUES(?,?,?)", titles(stream))
    db.execute("CREATE INDEX by_title ON title(term,id)")


def index_path(game):
    override = os.environ.get("KNOWLEDGE_WORLD_DB")
    return Path(override) if override else game.parent / "world" / INDEX_NAME


def archive_path(folder, cache, name):
    local = cache / name if cache else None
    path = local if local and local.is_file() else folder / name
    if path.is_file():
        with path.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == ARCHIVES[name]:
                return path
        if path == local:
            raise ValueError("Invalid checksum for " + name)
    partial = folder / (name + ".part")
    with (
        urllib.request.urlopen(BASE + name, timeout=120) as remote,
        partial.open("wb") as out,
    ):
        shutil.copyfileobj(remote, out, length=1024 * 1024)
    with partial.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != ARCHIVES[name]:
            raise ValueError("Invalid checksum for " + name)
    partial.replace(path)
    return path


def archive_rows(path, members):
    seen = set()
    with tarfile.open(path, "r|gz") as archive:
        for item in archive:
            if item.name not in members:
                continue
            if not item.isfile() or item.name in seen:
                raise ValueError("Invalid archive member " + item.name)
            seen.add(item.name)
            with archive.extractfile(item) as data:
                yield item.name, data
    if seen != members:
        raise ValueError("Missing files in " + path.name)


def triple_rows(stream):
    for line in stream:
        s, p, o = line.rstrip().split(b"\t")
        if s[:1] != b"Q" or p[:1] != b"P" or o[:1] != b"Q":
            raise ValueError("Unexpected Wikidata identifiers")
        yield int(s[1:]), int(p[1:]), int(o[1:])


def alias_batches(stream, prefix):
    rows = []
    lookup = []
    for line in stream:
        raw, *names = line.decode("utf8").rstrip("\r\n").split("\t")
        if not raw.startswith(prefix):
            raise ValueError("Unexpected alias identifier")
        entity = int(raw[1:])
        # Keep the actual published spellings. The archive has no designated canonical name.
        forms = {}
        for name in names:
            if (
                name != name.strip()
                or not 0 < len(name) <= 140
                or not name.isprintable()
            ):
                continue
            key = norm(name)
            if key and (
                key not in forms or forms[key].islower() and not name.islower()
            ):
                forms[key] = name
        if not forms:
            continue
        rows.append((entity, "\t".join(forms.values())))
        lookup.extend((key, entity) for key in forms)
        if len(rows) >= 5000:
            yield rows, lookup
            rows, lookup = [], []
    if rows:
        yield rows, lookup


def build(file, triples, aliases):
    db = sqlite3.connect(file)
    try:
        # This file is private until the atomic rename. It can be rebuilt after interruption.
        db.executescript("""
            PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-98304;
            PRAGMA temp_store=FILE;
            CREATE TABLE triple(s INTEGER,p INTEGER,o INTEGER);
            CREATE TABLE entity(id INTEGER PRIMARY KEY, names TEXT NOT NULL);
            CREATE TABLE property(id INTEGER PRIMARY KEY, names TEXT NOT NULL);
            CREATE TABLE entity_alias(term TEXT,id INTEGER);
            CREATE TABLE property_alias(term TEXT,id INTEGER);
        """)
        for name, stream in archive_rows(triples, TRIPLE_FILES):
            print("Indexing " + name, flush=True)
            db.executemany("INSERT INTO triple VALUES(?,?,?)", triple_rows(stream))
        for name, stream in archive_rows(
            aliases, {"wikidata5m_entity.txt", "wikidata5m_relation.txt"}
        ):
            table = "entity" if name == "wikidata5m_entity.txt" else "property"
            print("Indexing " + name, flush=True)
            for rows, labels in alias_batches(
                stream, "Q" if table == "entity" else "P"
            ):
                db.executemany(f"INSERT INTO {table} VALUES(?,?)", rows)
                db.executemany(f"INSERT INTO {table}_alias VALUES(?,?)", labels)
        print("Building property, subject and name indexes", flush=True)
        db.executescript("""
            CREATE INDEX by_object ON triple(o,p,s);
            CREATE INDEX by_subject ON triple(s,p,o);
            CREATE INDEX by_name ON entity_alias(term,id);
            CREATE INDEX property_name ON property_alias(term,id);
            CREATE TABLE incoming(id INTEGER PRIMARY KEY,n INTEGER);
            INSERT INTO incoming SELECT o,count(*) FROM triple GROUP BY o;
            ANALYZE;
        """)
        count = db.execute("SELECT count(*) FROM triple").fetchone()[0]
        if count != 20624575:
            raise ValueError(f"Expected 20,624,575 source statements, found {count:,}")
        print("Indexing English article titles", flush=True)
        add_titles(db)
        db.execute("PRAGMA user_version=1")
        db.commit()
    finally:
        db.close()


@contextlib.contextmanager
def install_lock(folder):
    with (folder / "install.lock").open("a+b") as lock:
        if os.name == "nt":
            import msvcrt

            if lock.tell() == 0:
                lock.write(b"0")
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def install(dest, cache=None):
    if dest.is_file():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    with install_lock(dest.parent):
        if dest.is_file():
            return
        if shutil.disk_usage(dest.parent).free < 6_000_000_000:
            raise ValueError(
                "The initial Wikidata5M build needs 6 GB of free disk space."
            )
        folder = dest.parent / (dest.name + ".staging")
        folder.mkdir(exist_ok=True)
        files = [archive_path(folder, cache, name) for name in ARCHIVES]
        output = folder / INDEX_NAME
        # A stopped process may have left a private, unpublished SQLite file here.
        output.unlink(missing_ok=True)
        build(output, *files)
        os.replace(output, dest)
        for name in ARCHIVES:
            (folder / name).unlink(missing_ok=True)
        folder.rmdir()
        print("Ready: " + str(dest), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("storage/quiz.sqlite"))
    parser.add_argument(
        "--archives",
        type=Path,
        help="Use previously downloaded, checksum-verified archives",
    )
    args = parser.parse_args()
    try:
        install(index_path(args.db), args.archives)
    except (ValueError, OSError, sqlite3.Error) as err:
        sys.exit(str(err))
