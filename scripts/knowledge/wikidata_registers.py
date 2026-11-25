"""Publish reusable Wikidata scope snapshots without touching a running game."""

import gzip
import json
import re
import sqlite3
import tempfile
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from graph import digest, ingest, label
from sources import relation_source, wikidata


@dataclass(frozen=True)
class Scope:
    qid: str
    term: str
    pid: str = ""
    relation: str = ""

    def __post_init__(self):
        if label(self.term) != self.term:
            raise ValueError("Remove surrounding whitespace from the Wikidata class name.")
        if not re.fullmatch(r"Q[1-9]\d*", self.qid):
            raise ValueError("A Wikidata class needs its Q identifier.")
        if self.pid or self.relation:
            if not re.fullmatch(r"P[1-9]\d*", self.pid):
                raise ValueError("A Wikidata relationship needs its P identifier.")
            if label(self.relation) != self.relation:
                raise ValueError("Remove surrounding whitespace from the Wikidata relationship.")

    @property
    def source(self):
        return f"wd-rel:{self.qid}:{self.pid}" if self.pid else f"wd:{self.qid}:{self.term}"

    @property
    def base(self):
        return Scope(self.qid, self.term)

    @classmethod
    def from_feed(cls, feed):
        spec = feed["wikidata"]
        scope = cls(spec["classId"], spec["label"], spec.get("propertyId", ""), spec.get("relation", ""))
        if feed.get("source", scope.source) != scope.source:
            raise ValueError(f"The source ID for this Wikidata scope is {scope.source}.")
        return scope


def state(db, source):
    return db.execute("SELECT revision,checked FROM source WHERE id=?", (source,)).fetchone()


def refresh_scopes(db, cache, configured, offline):
    scopes = {}
    def add(scope):
        if scopes.setdefault(scope.source, scope) != scope:
            raise ValueError(f"Conflicting definitions of {scope.source}.")
    for row in db.execute("SELECT id FROM source WHERE id LIKE 'wd:%'"):
        _, qid, term = row[0].split(":", 2)
        add(Scope(qid, term))
    for source, args in db.execute("SELECT source,args FROM recipe WHERE source LIKE 'wd-rel:%'"):
        term, qid, relation, *_ = json.loads(args)
        add(Scope(qid, term, source.split(":")[2], relation))
    for scope in configured:
        add(scope)
    for scope in tuple(scopes.values()):
        add(scope.base)
    if not scopes:
        return
    cache_file = cache / "snapshots.sqlite"
    if not offline:
        cache.mkdir(parents=True, exist_ok=True)
    location = cache_file
    if offline:
        location = cache_file.resolve().as_uri() + "?mode=ro" if cache_file.is_file() else ":memory:"
    with closing(sqlite3.connect(location, uri=True, timeout=60)) as snapshots, snapshots:
        if not offline or not cache_file.is_file():
            snapshots.execute("CREATE TABLE IF NOT EXISTS snapshot(scope TEXT PRIMARY KEY, captured INTEGER, release BLOB)")
        refresh_group(db, cache, scopes, snapshots, offline)


def refresh_group(db, cache, scopes, snapshots, offline):
    # A bundled property snapshot was captured alongside its bundled membership.
    # A downloaded property snapshot records its precise membership dependency.
    baseline = {s.source: row[0] for s in scopes.values() if not s.pid and (row := state(db, s.source))}
    for scope in sorted(scopes.values(), key=lambda s: (bool(s.pid), s.source)):
        installed = state(db, scope.source)
        captured = installed[1] if installed else 0
        base_revision = state(db, scope.base.source)[0] if scope.pid else None
        compatible = not scope.pid or (installed and baseline.get(scope.base.source) == base_revision)
        key = digest([scope.qid, scope.term, scope.pid, scope.relation])
        cached = snapshots.execute("SELECT release FROM snapshot WHERE scope=? AND captured>=?", (key, captured)).fetchone()
        if cached:
            release = json.loads(gzip.decompress(cached[0]))
            src = release["source"]
            if src["source"] != scope.source:
                raise ValueError(f"Cached Wikidata source does not match {scope.source}.")
            if src["captured"] >= captured and (not scope.pid or src.get("baseRevision") == base_revision):
                ingest(db, src, release["rows"])
                captured, compatible = src["captured"], True
                installed = state(db, scope.source)
            del release
        if offline:
            if not installed or not compatible:
                raise ValueError(f"No compatible offline snapshot for {scope.source}. Run the publisher online first.")
            continue
        if installed and compatible and time.time() - captured < 86400:
            continue
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=cache) as staging:
            export = Path(staging) / "scope.json.gz"
            try:
                if scope.pid:
                    relation_source(db, scope.term, scope.qid, scope.relation, "", export, scope.pid)
                else:
                    wikidata(db, scope.term, scope.qid, export, allow_empty=True)
            except (OSError, ValueError, KeyError) as err:
                current = state(db, scope.source)
                if not installed or not compatible or not current or current[0] != installed[0]:
                    raise ValueError(f"Could not refresh {scope.source}: {err}") from err
                print(f"Retaining {scope.source} as captured at {captured}: {err}", flush=True)
                continue
            snapshots.execute("INSERT OR REPLACE INTO snapshot VALUES(?,?,?)", (key, int(time.time()), export.read_bytes()))
