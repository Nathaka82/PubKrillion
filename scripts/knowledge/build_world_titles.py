"""Rebuild the bundled English article-title projection from Wikimedia's pinned Parquet release."""

import argparse
import gzip
import hashlib
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from graph import ROOT
from world_index import INDEX_NAME

URL = "https://huggingface.co/datasets/wikimedia/wikidata-title-desc/resolve/4e2b0b368762a15a3dafd1bcf353825513851695/data/en/train.parquet"
SHA256 = "b8b52e28cbb4a0f25d7d2356cf3702d335c13435777755a4359ce89b51b165ae"


def project(parquet, index, destination):
    import duckdb

    with parquet.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SHA256:
            raise ValueError("Unexpected Wikimedia release.")
    with (
        closing(
            duckdb.connect(config={"memory_limit": "384MB", "threads": "1"})
        ) as reader,
        tempfile.TemporaryDirectory() as folder,
        closing(sqlite3.connect(Path(folder) / "titles.sqlite", uri=True)) as db,
    ):
        if not index.is_file():
            raise ValueError("Build the Wikidata5M index before projecting titles.")
        db.execute("ATTACH DATABASE ? AS snapshot", (str(index.resolve()),))
        db.execute("CREATE TABLE upstream(id INTEGER PRIMARY KEY, title TEXT)")
        cursor = reader.execute(
            "SELECT try_cast(substr(qid,2) AS BIGINT),title FROM read_parquet(?) WHERE lang='en' AND length(title) BETWEEN 1 AND 140",
            [str(parquet)],
        )
        while rows := cursor.fetchmany(10000):
            db.executemany(
                "INSERT OR IGNORE INTO upstream VALUES(?,?)",
                ((i, t) for i, t in rows if i and t.isprintable() and t.strip() == t),
            )
        with (
            destination.open("wb") as output,
            gzip.GzipFile(
                fileobj=output, filename="", mode="wb", mtime=0
            ) as compressed,
        ):
            for i, title in db.execute(
                "SELECT u.id,u.title FROM upstream u JOIN snapshot.entity e ON e.id=u.id ORDER BY u.id"
            ):
                compressed.write(f"{i}\t{title}\n".encode())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("parquet", type=Path)
    parser.add_argument(
        "--index", default=ROOT / "storage/world" / INDEX_NAME, type=Path
    )
    parser.add_argument(
        "--output",
        default=ROOT / "data/knowledge/wikimedia-en-titles.tsv.gz",
        type=Path,
    )
    args = parser.parse_args()
    project(args.parquet, args.index, args.output)
