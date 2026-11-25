"""Build and read a standalone, versioned question catalogue from source records."""

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import urllib.request
from contextlib import closing
from pathlib import Path

from graph import ROOT, digest, folded_spelling, ingest, norm, open_graph, spelling
from sources import AGENT, bootstrap, import_file
from world_index import SOURCE, index_path, install

FORMAT = 26
RULE_REVISION = "source-topics-1"
WORLD_TABLES = ("item", "predicate", "pattern", "topic", "world_homonym", "world_member")


class RuleWithdrawn(ValueError):
    """The selected rule has no playable group in this release."""


class UnverifiedRule(ValueError):
    def __init__(self, key, message="This edition has no verified answers for that question.", *, requests=None):
        self.key = key
        self.requests = requests or [key]
        super().__init__(message)


class EmptyIntersection(UnverifiedRule):
    def __init__(self, key):
        super().__init__(key, "No answer in this edition has source evidence for both conditions.")


SCHEMA = """
CREATE TABLE publication(edition TEXT NOT NULL, format INTEGER NOT NULL);
CREATE TABLE live_rule(key TEXT PRIMARY KEY,revision TEXT,captured INTEGER,n INTEGER,release BLOB);
CREATE TABLE item(id INTEGER PRIMARY KEY, name TEXT NOT NULL, aliases TEXT NOT NULL, incoming INTEGER NOT NULL);
CREATE TABLE predicate(id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE pattern(cls INTEGER, prop INTEGER, target INTEGER, n INTEGER NOT NULL, items TEXT NOT NULL,
    PRIMARY KEY(cls,prop,target)) WITHOUT ROWID;
CREATE INDEX target_patterns ON pattern(target,n);
CREATE INDEX class_patterns ON pattern(cls,n);
CREATE TABLE topic(term TEXT, id INTEGER, PRIMARY KEY(term,id)) WITHOUT ROWID;
CREATE TABLE topic_spelling(term TEXT,id INTEGER,PRIMARY KEY(term,id)) WITHOUT ROWID;
CREATE TABLE class_topic(term TEXT,key TEXT,PRIMARY KEY(term,key)) WITHOUT ROWID;
CREATE TABLE register_rule(key TEXT PRIMARY KEY, source TEXT, class TEXT, n INTEGER NOT NULL);
CREATE INDEX register_class ON register_rule(class);
CREATE TABLE class_rule(key TEXT PRIMARY KEY, class TEXT UNIQUE, term TEXT, n INTEGER NOT NULL);
CREATE INDEX class_term ON class_rule(term);
CREATE TABLE wording(source TEXT,class TEXT,prompt TEXT NOT NULL,PRIMARY KEY(source,class)) WITHOUT ROWID;
CREATE TABLE world_homonym(term TEXT,id INTEGER,PRIMARY KEY(term,id)) WITHOUT ROWID;
CREATE TABLE world_member(id INTEGER,cls INTEGER,PRIMARY KEY(id,cls)) WITHOUT ROWID;
CREATE TABLE feed_pattern(key TEXT PRIMARY KEY,class TEXT,relation TEXT,target TEXT,prompt TEXT,n INTEGER);
CREATE TABLE feed_topic(term TEXT,key TEXT,PRIMARY KEY(term,key)) WITHOUT ROWID;
CREATE TABLE facet_rule(key TEXT PRIMARY KEY,source TEXT,class TEXT,relation TEXT,target TEXT,prompt TEXT,n INTEGER);
CREATE TABLE facet_topic(term TEXT,key TEXT,PRIMARY KEY(term,key)) WITHOUT ROWID;
CREATE TABLE question_text(term TEXT,rule TEXT,prompt TEXT,n INTEGER,complete INTEGER,
    PRIMARY KEY(term,rule)) WITHOUT ROWID;
"""


def catalogue_path(game):
    override = os.environ.get("KNOWLEDGE_CATALOGUE")
    return Path(override) if override else Path(game).parent / "knowledge-catalogue.sqlite"


def reader(file):
    db = sqlite3.connect(Path(file).resolve().as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.create_function("normalize", 1, norm, deterministic=True)
    db.create_function("spelling_key", 1, folded_spelling, deterministic=True)
    return db


def compile_world(db):
    # Direct class and relationship assertions have the same subject ID. A shared
    # word in two article titles never establishes membership.
    db.executescript("""
        INSERT INTO predicate SELECT id,substr(names,1,instr(names||char(9),char(9))-1) FROM world.property;
        INSERT INTO item SELECT e.id,coalesce(t.name,substr(e.names,1,instr(e.names||char(9),char(9))-1)),
            e.names,coalesce(i.n,0) FROM world.entity e LEFT JOIN world.title t USING(id)
            LEFT JOIN world.incoming i USING(id);
        INSERT INTO pattern
        SELECT ty.o,link.p,link.o,count(DISTINCT link.s),json_group_array(DISTINCT link.s)
        FROM world.triple ty JOIN world.triple link ON link.s=ty.s
        JOIN item s ON s.id=link.s JOIN item c ON c.id=ty.o JOIN item t ON t.id=link.o
        JOIN predicate p ON p.id=link.p
        WHERE ty.p=31 AND link.p NOT IN (31,279,106) AND link.o<>link.s
            AND length(c.name) BETWEEN 1 AND 70 AND length(t.name) BETWEEN 1 AND 140
            AND length(p.name) BETWEEN 1 AND 55 AND p.name NOT LIKE '% ID'
        GROUP BY ty.o,link.p,link.o HAVING count(DISTINCT link.s)>=5;
        CREATE TEMP TABLE anchors(id INTEGER PRIMARY KEY);
        INSERT INTO anchors SELECT cls FROM pattern UNION SELECT target FROM pattern;
        INSERT OR IGNORE INTO topic SELECT a.term,a.id FROM world.entity_alias a CROSS JOIN anchors k ON k.id=a.id;
        INSERT OR IGNORE INTO topic SELECT t.term,t.id FROM anchors k JOIN world.title t ON t.id=k.id;
        CREATE TEMP TABLE ambiguous_term(term TEXT PRIMARY KEY) WITHOUT ROWID;
        INSERT INTO ambiguous_term SELECT term FROM (
            SELECT term,id FROM world.entity_alias UNION ALL SELECT term,id FROM world.title)
            WHERE term<>'' GROUP BY term HAVING min(id)<>max(id);
        INSERT OR IGNORE INTO world_homonym SELECT a.term,a.id FROM ambiguous_term aterm
            CROSS JOIN world.entity_alias a ON a.term=aterm.term;
        INSERT OR IGNORE INTO world_homonym SELECT t.term,t.id FROM ambiguous_term aterm
            CROSS JOIN world.title t ON t.term=aterm.term;
        INSERT OR IGNORE INTO world_member SELECT s,o FROM world.triple WHERE p=31;
    """)


def compile_registers(db, include_pack_wording=True):
    for source in db.execute("SELECT id,census FROM source ORDER BY id").fetchall():
        for cls, expected in json.loads(source["census"]).items():
            actual = db.execute("""SELECT count(DISTINCT m.id) FROM member m JOIN entity e
                ON e.source=m.source AND e.id=m.id WHERE m.source=? AND m.class=?""",
                (source["id"], cls)).fetchone()[0]
            if actual != expected:
                raise ValueError(f"{source['id']}: expected {expected} entries for {cls}, found {actual}")
            if actual >= 2:
                key = "r-" + digest([source["id"], cls])
                db.execute("INSERT INTO register_rule VALUES(?,?,?,?)", (key, source["id"], cls, actual))
    for row in db.execute("""SELECT f.source,f.class,f.relation,e.target,f.prompt,count(DISTINCT e.id)
        FROM complete_facet f JOIN member m ON (m.source,m.class)=(f.source,f.class)
        JOIN edge e ON (e.source,e.id,e.relation)=(m.source,m.id,f.relation)
        GROUP BY f.source,f.class,f.relation,e.target HAVING count(DISTINCT e.id)>=2"""):
        source, cls, relation, target, template, size = row
        target_row = db.execute("SELECT name FROM entity WHERE id=? ORDER BY source=? DESC,source LIMIT 1", (target, source)).fetchone()
        labels = db.execute("SELECT alias FROM term_display WHERE key=? AND alias<>? ORDER BY source=? DESC,length(alias),alias LIMIT 1",
                            (target, target, source)).fetchone()
        value = target_row[0] if target_row else labels[0] if labels else target
        prompt = template.replace("{value}", value)
        if len(prompt) > 240:
            continue
        key = "v-" + digest([source, cls, relation, target])
        db.execute("INSERT INTO facet_rule VALUES(?,?,?,?,?,?,?)", (key, source, cls, relation, target, prompt, size))
        terms = {value, relation, f"{value} {cls}", f"{cls} {value}", f"{cls} {relation} {value}", prompt}
        db.executemany("INSERT OR IGNORE INTO facet_topic VALUES(?,?)", ((folded_spelling(t), key) for t in terms))
    # A feed can describe an open class without declaring a complete census.
    # Link publishers by entity ID and include descendants just as the game does.
    for row in db.execute("""WITH RECURSIVE hierarchy(cls,child) AS (
        SELECT DISTINCT class,class FROM member UNION SELECT parent,child FROM parent
        UNION SELECT p.parent,h.child FROM parent p JOIN hierarchy h ON h.cls=p.child)
        SELECT h.cls,count(DISTINCT m.id) n FROM hierarchy h JOIN member m ON m.class=h.child
        GROUP BY h.cls HAVING n>=2 AND n>coalesce((SELECT max(n) FROM register_rule WHERE class=h.cls),0)"""):
        cls, size = row
        db.execute("INSERT INTO class_rule VALUES(?,?,?,?)", ("c-" + digest(cls), cls, norm(cls), size))
    # Retain the actual source question when a bundled pack uses a machine-readable
    # class ID. The standalone game does not need the original pack to display it.
    if not include_pack_wording:
        return
    for file in sorted((ROOT / "data/packs").glob("*.json")):
        if file.stem in ("index", "sources"):
            continue
        for question in json.loads(file.read_bytes())["questions"]:
            prompt = question["prompt"]
            if len(prompt) > 240:
                continue
            body = re.sub(r"^name (?:an? )?", "", prompt.rstrip(".?!"), flags=re.I)
            db.execute("""INSERT OR IGNORE INTO wording SELECT source,key,? FROM term
                WHERE source=? AND alias=?""", (prompt.rstrip(".?!"), question["id"], norm(body)))


def compile_feed_patterns(db):
    has_world = any(r[1] == "world" for r in db.execute("PRAGMA database_list"))
    groups = db.execute("""WITH RECURSIVE hierarchy(cls,child) AS (
        SELECT DISTINCT class,class FROM member UNION SELECT parent,child FROM parent
        UNION SELECT p.parent,h.child FROM parent p JOIN hierarchy h ON h.cls=p.child)
        SELECT h.cls,e.relation,e.target,count(DISTINCT e.id) n
        FROM edge e JOIN member m ON m.id=e.id JOIN hierarchy h ON h.child=m.class
        GROUP BY h.cls,e.relation,e.target HAVING n>=2""")
    for cls, relation, target, count in groups:
        named = db.execute("SELECT name FROM entity WHERE id=? ORDER BY source LIMIT 1", (target,)).fetchone()
        qid = re.fullmatch(r"(?:wd:)?Q(\d+)", target)
        if named is None and qid and has_world:
            named = db.execute("SELECT name FROM world.title WHERE id=?", (int(qid[1]),)).fetchone()
        aliases = [r[0] for r in db.execute("SELECT DISTINCT alias FROM term_display WHERE key=? ORDER BY length(alias),alias", (target,))]
        label = named[0] if named else (next((a for a in aliases if a != target), target)
                                         if ":" in target or qid else target)
        if re.search(r"://|^[\w.-]+:|^Q\d+$", label) or "@" in label or re.match(r"^[a-z]+-[a-z]+-", cls):
            continue
        article = "an" if cls[:1].lower() in "aeiou" else "a"
        if relation == "standard state":
            phrase, label = "whose standard state is", label.lower()
        elif relation == "group":
            phrase, label = "in the group", label
        elif relation in ("from", "in", "on") or relation.endswith((" by", " in", " of", " at", " for", " from", " as")):
            phrase = relation
        elif relation in ("reached", "won"):
            phrase = "that " + relation
        else:
            phrase = f'with “{relation}” recorded as'
        prompt = f"Name {article} {cls} {phrase} {label}"
        if len(prompt) > 240:
            continue
        key = "f-" + digest([cls, relation, target])
        db.execute("INSERT INTO feed_pattern VALUES(?,?,?,?,?,?)", (key, cls, relation, target, prompt, count))
        topics = {cls, label, target, relation, prompt.partition(" ")[2], *aliases}
        topics.update(r[0] for r in db.execute("SELECT alias FROM term_display WHERE key=?", (cls,)))
        db.executemany("INSERT OR IGNORE INTO feed_topic VALUES(?,?)", ((folded_spelling(t), key) for t in topics if t))


def import_feed(db, manifest, cache, offline, definition=None):
    if definition is None:
        definition = json.loads(manifest.read_bytes())
    if "file" not in definition:
        import_file(db, manifest, definition=definition)
        return definition
    local = manifest.parent / definition["file"]
    remote = definition.get("download")
    def ingest_file(path):
        with path.open("rb") as data:
            fingerprint = hashlib.file_digest(data, "sha256").hexdigest()
        if definition.get("sha256", fingerprint) != fingerprint:
            raise ValueError(f"{manifest.name}: source checksum does not match.")
        revision = digest([definition, fingerprint])
        import_file(db, manifest, data_path=path, revision=revision, definition=definition)
        return revision
    if remote is not None and (not isinstance(remote, str) or not remote.startswith("https://")):
        raise ValueError(f"{manifest.name}: remote feeds require an HTTPS download URL.")
    cache.mkdir(parents=True, exist_ok=True)
    cached = cache / (digest([definition["source"], remote]) + local.suffix)
    if remote and offline:
        return ingest_file(cached)
    limit = 32_000_000 if local.suffix in (".json", ".jsonld") else 512_000_000
    with tempfile.TemporaryDirectory(dir=cache) as folder:
        stage = Path(folder) / ("records" + local.suffix)
        source = (urllib.request.urlopen(urllib.request.Request(remote, headers={"User-Agent": AGENT}), timeout=45)
                  if remote else local.open("rb"))
        with source as response, stage.open("wb") as stream:
            before = None if remote else os.fstat(response.fileno())
            while block := response.read(1024 * 1024):
                if stream.tell() + len(block) > limit:
                    raise ValueError(f"{manifest.name}: remote release exceeds the import size limit.")
                stream.write(block)
            if before is not None:
                after = os.fstat(response.fileno())
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise ValueError(f"{manifest.name}: source changed while the release was being read.")
        revision = ingest_file(stage)
        if remote:
            os.replace(stage, cached)
        return revision


def build(world, output, feeds, *, offline=False, include_bundled=True, measurements=None, discover_dates=False, discover_names=False):
    import attested_names
    import comparisons
    import taxonomy
    import world_classes
    import world_links
    import class_updates
    if world is not None and measurements is None and include_bundled:
        measurements = comparisons.DEFAULT_EXPORT
    if measurements is not None and world is None:
        raise ValueError("A Wikidata measurement export needs the matching world entity index.")
    output.parent.mkdir(parents=True, exist_ok=True)
    if any(output.resolve() == source.resolve() for source in (world, measurements) if source is not None):
        raise ValueError("The publication needs its own path.")
    def verify_inputs():
        for source, before in inputs:
            after = source.stat()
            if (not os.path.samestat(before, after) or
                    (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                raise ValueError(f"The input {source.name} changed while the catalogue was being built.")
    # Build under a unique name on the destination volume. Failed or interrupted
    # builds are never opened by the game. The lock serializes competing publishers.
    from world_index import install_lock
    lock_dir = output.parent / (output.name + ".build")
    lock_dir.mkdir(exist_ok=True)
    with install_lock(lock_dir), tempfile.TemporaryDirectory(dir=output.parent) as staging:
        stage = Path(staging) / "catalogue.sqlite"
        inputs = [(source, source.stat()) for source in (world, measurements) if source is not None]
        with closing(open_graph(stage, uri=True)) as db:
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=OFF")
            db.execute("PRAGMA cache_size=-131072")
            page_size = db.execute("PRAGMA page_size").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={50_000_000_000 // page_size}")
            if world is not None:
                db.execute("ATTACH DATABASE ? AS world", (world.resolve().as_uri() + "?mode=ro&immutable=1",))
            db.executescript(SCHEMA)
            db.executescript(comparisons.SCHEMA)
            db.executescript(world_classes.SCHEMA)
            import target_lookup, topic_sources
            db.executescript(target_lookup.SCHEMA)
            db.executescript(topic_sources.SCHEMA)
            db.executescript(world_links.SCHEMA)
            db.executescript(attested_names.SCHEMA)
            import current_questions, date_rules, date_updates, date_properties, dated_links
            db.executescript(current_questions.SCHEMA)
            db.executescript(date_rules.SCHEMA)
            db.executescript(dated_links.SCHEMA)
            date_dictionary = date_properties.install(db,output.parent)
            db.executescript(class_updates.SCHEMA)
            measurement_digest = comparisons.export_signature(db, measurements)
            print("Validating source registers", flush=True)
            element_names = bootstrap(db) if include_bundled else None
            if include_bundled and db.execute("SELECT 1 FROM source WHERE id='fishbase'").fetchone():
                from sources import refresh_fish
                refresh_fish(db, output.parent / "knowledge-sources" / "fishbase.sqlite", offline)
            if isinstance(element_names, list):
                from pubchem_register import refresh
                refresh(db, output.parent / "knowledge-sources" / "pubchem-periodic-table.json", element_names, offline)
            from nobel_register import import_snapshot
            if include_bundled:
                import_snapshot(db, output.parent / "knowledge-sources" / "nobel-prizes.json", offline)
            feed_cache = output.parent / "knowledge-sources" / "feeds"
            feeds_signature, owners = [], set()
            from wikidata_registers import Scope, refresh_scopes
            wikidata_scopes = []
            for manifest in sorted(feeds.glob("*.manifest.json")):
                definition = json.loads(manifest.read_bytes())
                if "wikidata" in definition:
                    scope = Scope.from_feed(definition)
                    wikidata_scopes.append(scope)
                    if scope.source in owners:
                        raise ValueError(f"Multiple feed manifests claim the same publisher ID: {scope.source}")
                    owners.add(scope.source)
                    feeds_signature.append(definition)
                    continue
                owner = ("results-" + definition["competition"]["key"]
                         if "competition" in definition else definition["source"])
                if owner.startswith(("wd:", "wd-rel:")):
                    raise ValueError("Wikidata source identifiers are reserved for Wikidata scope manifests.")
                if owner in owners:
                    raise ValueError(f"Multiple feed manifests claim the same publisher ID: {owner}")
                owners.add(owner)
                feeds_signature.append(import_feed(db, manifest, feed_cache, offline, definition))
            refresh_scopes(db, output.parent / "knowledge-sources" / "wikidata", wikidata_scopes, offline)
            compile_registers(db, include_pack_wording=include_bundled)
            compile_feed_patterns(db)
            revisions = list(map(tuple, db.execute("SELECT id,revision FROM source ORDER BY id")))
            # The official index is an immutable, checksum-verified release. Include the
            # actual input digest as well so a locally supplied snapshot gets its own edition.
            fingerprint = None
            hierarchy = None
            watched = []
            if world is not None:
                from watched_rules import collect
                if discover_dates and not offline:
                    import date_discovery, target_discovery
                    try:
                        date_discovery.tick(db,output.parent)
                    except (OSError,ValueError,KeyError) as error:
                        print(f"Date-topic discovery deferred: {error}",flush=True)
                    try:
                        target_discovery.tick(output.parent,output)
                    except (OSError,ValueError,KeyError) as error:
                        print(f"Relationship-target discovery deferred: {error}",flush=True)
                topics = topic_sources.collect(db,output.parent,offline,previous=output)
                watched = sorted((t["key"],t["revision"]) for t in topics)
                watched += target_lookup.collect(db,output.parent,offline,previous=output,topic_captures=topics)
                watched += collect(db, output.parent, offline, publication=output)
                watched += class_updates.refresh(db,output.parent,offline)
                watched += date_updates.collect(db,output.parent,offline,previous=output)
                if discover_names and not offline:
                    try:
                        attested_names.refresh(db, output.parent,previous=output)
                    except (OSError, ValueError, KeyError) as error:
                        print(f"Recorded name update deferred: {error}",flush=True)
                with world.open("rb") as data:
                    fingerprint = hashlib.file_digest(data, "sha256").hexdigest()
                hierarchy = taxonomy.attach(db,taxonomy.location(output.parent),fingerprint)
            name_revision = attested_names.install(db,output.parent,output) if world is not None else None
            edition = digest([FORMAT, RULE_REVISION, fingerprint, revisions, feeds_signature, watched, measurement_digest,hierarchy,date_dictionary,name_revision])
            reuse_world = False
            if output.is_file():
                with closing(reader(output)) as previous:
                    if previous.execute("SELECT 1 FROM publication WHERE edition=? AND format=?", (edition, FORMAT)).fetchone():
                        verify_inputs()
                        return summary(previous)
                    reuse_world = world is not None and previous.execute("""SELECT 1 FROM source,publication
                        WHERE source.id='wikidata5m-catalogue-input' AND source.revision=? AND publication.format BETWEEN 5 AND ?""",
                        (fingerprint, FORMAT)).fetchone() is not None
            if world is not None:
                db.execute("INSERT INTO source VALUES('wikidata5m-catalogue-input',?,?,unixepoch(),'{}')", (fingerprint, SOURCE))
                if reuse_world:
                    print("Reusing the verified world graph from the previous edition", flush=True)
                    db.execute("ATTACH DATABASE ? AS prior", (output.resolve().as_uri() + "?mode=ro&immutable=1",))
                    for table in WORLD_TABLES:
                        db.execute(f"INSERT INTO main.{table} SELECT * FROM prior.{table}")
                else:
                    print("Indexing direct class and relationship evidence", flush=True)
                    compile_world(db)
                world_classes.publish(db, current_taxonomy=bool(hierarchy))
                print("Indexing individual entity relationships and their names", flush=True)
                reuse_links = reuse_world and db.execute("SELECT format FROM prior.publication").fetchone()[0] >= 17
                world_links.publish(db,reuse_links)
                current_questions.publish(db)
                date_rules.index_questions(db)
                dated_links.discover(db)
            comparisons.compile_index(db, measurement_digest)
            compile_topic_spellings(db)
            compile_question_text(db)
            db.execute("""UPDATE question_text AS q SET n=(SELECT n FROM live_rule l WHERE l.key=q.rule)
                WHERE q.rule IN (SELECT key FROM live_rule)""")
            db.execute("INSERT INTO publication VALUES(?,?)", (edition, FORMAT))
            db.commit()
            stats = summary(db)
            if not any(stats[k] for k in ("patterns", "registers", "openClasses", "feedPatterns", "graphClasses")) or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("The publication failed validation.")
            verify_inputs()
        for attempt in range(31):
            try:
                os.replace(stage, output)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 30:
                    raise
                time.sleep(1)
        return stats


def summary(db):
    file_format = db.execute("SELECT format FROM publication").fetchone()[0]
    return {
        "edition": db.execute("SELECT edition FROM publication").fetchone()[0],
        "format": file_format,
        "researchedTopics": db.execute("SELECT count(*) FROM source_topic").fetchone()[0] if file_format>=26 else 0,
        "playableTopicRules": db.execute("""SELECT count(DISTINCT t.rule) FROM source_topic_rule t
            JOIN current_question q USING(rule) WHERE q.n>=2""").fetchone()[0] if file_format>=26 else 0,
        "resolvedTargets": db.execute("SELECT count(*) FROM current_target").fetchone()[0] if file_format>=25 else 0,
        "recordedNames": db.execute("SELECT count(*) FROM attested_name").fetchone()[0] if file_format>=22 else 0,
        "dateStatements": db.execute("SELECT count(DISTINCT statement) FROM date_fact").fetchone()[0] if file_format>=20 else 0,
        "dateProperties": db.execute("SELECT count(*) FROM date_property").fetchone()[0] if file_format>=20 else 0,
        "datedRelationshipQuestions": db.execute("SELECT count(*) FROM current_question WHERE rule GLOB 'during:*'").fetchone()[0] if file_format>=24 else 0,
        "verifiedDatedRelationships": db.execute("SELECT count(*) FROM live_rule WHERE key GLOB 'during:*' AND n>0").fetchone()[0] if file_format>=24 else 0,
        "entities": db.execute("SELECT count(*) FROM item").fetchone()[0],
        "individualRelationships": db.execute("SELECT count(*) FROM world_link").fetchone()[0] if
            db.execute("SELECT format FROM publication").fetchone()[0] >= 17 else 0,
        "patterns": db.execute("SELECT count(*) FROM pattern").fetchone()[0],
        "properties": db.execute("SELECT count(DISTINCT prop) FROM world_link" if file_format>=17 else
                                 "SELECT count(DISTINCT prop) FROM pattern").fetchone()[0],
        "indexedNameEntityPairs": db.execute("SELECT count(*) FROM world_name").fetchone()[0] if file_format>=18 else 0,
        "parentLinks": db.execute("SELECT count(*) FROM world_parent").fetchone()[0] if file_format>=14 else 0,
        "registers": db.execute("SELECT count(*) FROM register_rule").fetchone()[0],
        "openClasses": db.execute("SELECT count(*) FROM class_rule").fetchone()[0],
        "feedPatterns": db.execute("SELECT count(*) FROM feed_pattern").fetchone()[0],
        "completeProperties": db.execute("SELECT count(*) FROM facet_rule").fetchone()[0],
        "sourceEntities": db.execute("SELECT count(DISTINCT id) FROM entity WHERE source<>'world5m-measure-members'").fetchone()[0],
        "measuredValues": db.execute("SELECT count(*) FROM measure_value").fetchone()[0] if
            db.execute("SELECT format FROM publication").fetchone()[0] >= 10 else 0,
        "watchedQuestions": db.execute("SELECT count(*) FROM live_rule").fetchone()[0] if
            db.execute("SELECT format FROM publication").fetchone()[0] >= 9 else 0,
        "graphClasses": db.execute("SELECT count(DISTINCT cls) FROM world_class_name").fetchone()[0] if
            db.execute("SELECT format FROM publication").fetchone()[0] >= 14 else 0,
        "currentGraphClasses": db.execute("SELECT count(*) FROM current_class").fetchone()[0] if
            db.execute("SELECT format FROM publication").fetchone()[0] >= 15 else 0,
    }


RELATION_PHRASES = {
    17: "in",
    19: "born in", 20: "who died in", 50: "written by", 57: "directed by", 58: "with a screenplay by",
    84: "designed by the architect", 86: "composed by", 87: "with a libretto by", 110: "illustrated by",
    123: "published by", 127: "owned by", 131: "in the administrative area of", 136: "in the genre",
    161: "whose cast includes", 162: "produced by", 170: "created by", 175: "performed by",
    176: "manufactured by", 178: "developed by", 179: "in the series", 264: "on the record label",
    291: "published in", 375: "launched using", 400: "released for", 421: "in the time zone", 449: "originally broadcast on",
    495: "whose country of origin is", 725: "with a voice performance by", 750: "distributed by",
    676: "with lyrics by", 793: "whose recorded events include", 840: "set in", 915: "filmed in",
    921: "whose main subject is",
}


def named_question(subject, clause):
    subject = subject if subject.isupper() else subject[:1].lower() + subject[1:]
    if subject == "human":
        subject = "person"
    article = "an" if subject[:1].lower() in "aeiou" else "a"
    return f"Name {article} {subject} {clause}"


def world_clause(prop, relation, target):
    phrase = RELATION_PHRASES.get(prop)
    if prop in (17,19,20,840,915):
        phrase = f"whose {relation} is"
    if not phrase:
        phrase = relation if relation.endswith((" by", " of", " in")) else f'with “{relation}” recorded as'
    return f"{phrase} {target}"


def world_question(subject, prop, relation, target):
    return named_question(subject, world_clause(prop, relation, target))


def register_card(db, row, edition):
    article = "an" if row["class"][:1].lower() in "aeiou" else "a"
    source = row["source"] if "source" in row.keys() else None
    wording = db.execute("SELECT prompt FROM wording WHERE class=? AND source=coalesce(?,class)",
                         (row["class"], source)).fetchone()
    return {"key": row["key"], "edition": edition, "prompt": wording[0] if wording else f"Name {article} {row['class']}",
            "records": row["n"], "complete": True}


def world_card(db, row, edition):
    key = f"w-{row['cls']}-{row['prop']}-{row['target']}"
    n = current_count(db, key, row["n"], edition[1])
    target = row["object"]
    subject = row["subject"]
    if edition[1]>=25:
        from world_links import identifiers,display_subject
        subject = display_subject(db,row["cls"],subject)
        if identifiers(db,folded_spelling(target),"target") != [row["target"]]:
            target = f"{target} (Q{row['target']})"
    return {"key": key, "edition": edition[0],
            "prompt": world_question(subject, row["prop"], row["relation"], target),
            "records": n, "complete": False}


def current_count(db, key, historical, file_format):
    if file_format >= 9:
        row = db.execute("SELECT n FROM live_rule WHERE key=?", (key,)).fetchone()
        if row:
            return row[0]
    if file_format >= 17 and key.startswith("w-"):
        from world_links import members
        query,args = members(db,*map(int,key[2:].split("-")))
        return db.execute(f"SELECT count(*) FROM ({query})",args).fetchone()[0]
    return historical


COMMAND = re.compile(r"^(?:(?:please |can you )?(?:name|identify|list|give me)) (?:an? |the )?")


def prompt_term(prompt):
    return COMMAND.sub("", folded_spelling(prompt).strip(), count=1)


def compile_topic_spellings(db):
    db.create_function("spelling_key", 1, folded_spelling, deterministic=True)
    for table, scope in (("register_rule", "AND t.source=r.source"), ("class_rule", "")):
        db.execute(f"INSERT OR IGNORE INTO class_topic SELECT spelling_key(class),key FROM {table}")
        db.execute(f"""INSERT OR IGNORE INTO class_topic SELECT spelling_key(t.alias),r.key
            FROM {table} r JOIN term_display t ON t.key=r.class {scope}""")
    # Recover original spellings from the world labels. Legacy source indexes
    # also supply aliases with no corresponding display name.
    db.execute("CREATE TEMP TABLE plain_topic(term TEXT,id INTEGER,PRIMARY KEY(term,id)) WITHOUT ROWID")
    for item, name, aliases in db.execute("""SELECT i.id,i.name,i.aliases FROM
            (SELECT cls id FROM pattern UNION SELECT target FROM pattern) a JOIN item i USING(id)"""):
        names = {name, *aliases.split("\t")} - {""}
        db.executemany("INSERT OR IGNORE INTO topic_spelling VALUES(?,?)", ((folded_spelling(n), item) for n in names))
        db.executemany("INSERT OR IGNORE INTO plain_topic VALUES(?,?)", ((norm(n), item) for n in names))
    db.execute("""INSERT OR IGNORE INTO topic_spelling SELECT term,id FROM topic
        EXCEPT SELECT term,id FROM plain_topic""")


def compile_question_text(db):
    # A displayed question must still select precisely the same rule when a
    # host types it directly. This includes properties outside the hand-written
    # English phrasing templates and targets with significant symbols.
    db.create_function("prompt_term", 1, prompt_term, deterministic=True)
    db.create_function("world_prompt", 4, world_question, deterministic=True)
    db.execute("""WITH cards AS MATERIALIZED (
        SELECT 'w-'||p.cls||'-'||p.prop||'-'||p.target rule,world_prompt(c.name,p.prop,r.name,t.name) prompt,p.n
        FROM pattern p JOIN item c ON c.id=p.cls JOIN predicate r ON r.id=p.prop JOIN item t ON t.id=p.target)
        INSERT INTO question_text SELECT prompt_term(prompt),rule,prompt,n,0 FROM cards WHERE length(prompt)<=240""")
    for table, complete in (("register_rule", 1), ("class_rule", 0)):
        for row in db.execute(f"SELECT * FROM {table}"):
            prompt = register_card(db, row, "")["prompt"]
            db.execute("INSERT INTO question_text VALUES(?,?,?,?,?)", (prompt_term(prompt), row["key"], prompt, row["n"], complete))
    for table, complete in (("feed_pattern", 0), ("facet_rule", 1)):
        db.execute(f"INSERT INTO question_text SELECT prompt_term(prompt),key,prompt,n,? FROM {table}", (complete,))
    # The open join contains all same-source matches. A validated property rule
    # with the same operands and count therefore covers exactly those IDs. Keep
    # the saved open rule playable, but direct hosts to its verified register.
    db.executescript("""CREATE TEMP TABLE equivalents AS SELECT f.key open_key,v.key closed_key,v.prompt FROM
        feed_pattern f JOIN facet_rule v ON (v.class,v.relation,v.target,v.n)=(f.class,f.relation,f.target,f.n);
        INSERT OR IGNORE INTO question_text SELECT t.term,e.closed_key,e.prompt,t.n,1 FROM
            equivalents e JOIN question_text t ON t.rule=e.open_key;
        INSERT OR IGNORE INTO facet_topic SELECT t.term,e.closed_key FROM equivalents e JOIN feed_topic t ON t.key=e.open_key;
        DELETE FROM question_text WHERE rule IN (SELECT open_key FROM equivalents);
        DELETE FROM feed_topic WHERE key IN (SELECT open_key FROM equivalents);""")


def surprise(db, complete_only=False):
    edition = db.execute("SELECT edition,format FROM publication WHERE format BETWEEN 4 AND ?", (FORMAT,)).fetchone()
    if not edition:
        raise ValueError("Rebuild the knowledge catalogue with this version of Krillion.")
    registers = db.execute("""SELECT * FROM (
        SELECT *,row_number() OVER (PARTITION BY source ORDER BY random()) pos FROM register_rule)
        ORDER BY pos,random() LIMIT 3""").fetchall()
    cards = [register_card(db, row, edition[0]) for row in registers]
    if edition[1] >= 6:
        facet = db.execute("SELECT key,prompt,n FROM facet_rule ORDER BY random() LIMIT 1").fetchone()
        if facet:
            cards.insert(1, {"key": facet[0], "edition": edition[0], "prompt": facet[1], "records": facet[2], "complete": True})
    if complete_only:
        return cards[:3]
    result = cards[:1]
    feed_cards = []
    if edition[1] >= 5:
        feed_cards = [{"key": key, "edition": edition[0], "prompt": prompt, "records": n, "complete": False}
                      for key, prompt, n in db.execute("""SELECT key,prompt,n FROM feed_pattern
                          WHERE key IN (SELECT key FROM feed_topic) ORDER BY random() LIMIT 3""")]
        result.extend(feed_cards[:1])
    # Sample keys through a covering index. Large membership arrays stay on disk.
    marks = ",".join("?" for _ in RELATION_PHRASES)
    rows = db.execute(f"""WITH sample AS MATERIALIZED (
        SELECT cls,prop,target,n FROM pattern INDEXED BY target_patterns
        WHERE n BETWEEN 5 AND 250 AND prop IN ({marks}) ORDER BY random() LIMIT 96)
        SELECT s.*,c.name subject,r.name relation,t.name object FROM sample s
        JOIN item c ON c.id=s.cls JOIN item t ON t.id=s.target JOIN predicate r ON r.id=s.prop
        WHERE c.name NOT LIKE '%(%' AND t.name NOT LIKE '%(%' ORDER BY t.incoming DESC,s.n DESC""", tuple(RELATION_PHRASES))
    used = set()
    for row in rows:
        card = world_card(db, row, edition)
        if card["records"] and row["prop"] not in used and len(card["prompt"]) <= 240:
            result.append(card)
            used.add(row["prop"])
            if len(result) == 3:
                break
    if len(result) < 3:
        result.extend(feed_cards[1:] + cards[1:])
    if len(result) < 3:
        for row in db.execute("SELECT * FROM class_rule ORDER BY random() LIMIT 3"):
            card = register_card(db, row, edition[0])
            if all(previous["prompt"] != card["prompt"] for previous in result):
                result.append({**card, "complete": False})
    if edition[1] >= 19 and (len(result) < 3 or db.execute("SELECT random()%2=0").fetchone()[0]):
        import current_questions
        for card in current_questions.cards(db, edition[0]):
            if all(c["prompt"] != card["prompt"] for c in result[:3]):
                result = result[:2] + [card]
    return result[:3]


def relationship_choices(db,card,complete_only,*,measurements=True):
    if complete_only and not card["complete"]:
        return []
    from measured_rules import alternatives
    import places
    areas=places.alternatives(db,card) if card["key"].startswith("g-") else []
    sizes=alternatives(db,card,complete_only) if measurements else []
    import dated_links
    dates=dated_links.alternatives(db,card) if not complete_only else []
    related = []
    if not complete_only and db.execute("SELECT format FROM publication").fetchone()[0] >= 25:
        import target_lookup
        related = target_lookup.alternatives(db,card["edition"],original=card["key"])
    return [card, *areas[:1], *sizes[:1], *dates, *related, *areas[1:], *sizes[1:]][:3]


def search(db, idea, complete_only=False, *, comparisons=True):
    import date_rules
    edition_id,file_format = db.execute("SELECT edition,format FROM publication").fetchone()
    if file_format>=20 and not complete_only:
        import dated_links
        if year := re.fullmatch(r"(?:in |the year |year )?([1-9]\d{3})",folded_spelling(idea)):
            import current_questions
            if dated := current_questions.cards(db,edition_id,year[1]):
                return dated
        try:
            if dated := dated_links.match(db,topic_subject(spelling(idea)),edition_id):
                return [dated]
            if dated := date_rules.match(db,topic_subject(spelling(idea)),edition_id):
                return [dated,*dated_links.alternatives(db,dated)]
        except date_rules.DateChoices as choices:
            if choices.cards:
                return choices.cards
            raise
    command = COMMAND.match(folded_spelling(idea))
    if file_format>=17 and (not command or file_format>=25 and not complete_only):
        try:
            if command:
                body=COMMAND.sub("",spelling(idea),count=1).removesuffix("?").strip()
                direct=match_relation(db,folded_spelling(body),body,(edition_id,file_format),include_empty=True,request_target=True)
            else:
                direct = match_single(db,"Name a "+topic_subject(idea.strip()), include_empty=True, request_target=not complete_only)
        except date_rules.DateChoices as choices:
            if choices.cards:
                return [] if complete_only else choices.cards
            raise
        if direct and direct["key"].startswith(("w-","g-","v-","f-")):
            if not direct["records"]:
                if complete_only:
                    return []
                raise UnverifiedRule(direct["key"])
            return relationship_choices(db,direct,complete_only,measurements=comparisons)
    cards = search_topics(db, idea, complete_only)
    if file_format>=17 and not cards and not complete_only:
        from world_links import suggestions
        edition = db.execute("SELECT edition,format FROM publication").fetchone()
        cards = suggestions(db,topic_subject(folded_spelling(idea)),edition)
    if file_format>=14 and not complete_only and not any(c["key"].startswith(("r-","c-")) for c in cards):
        from world_classes import card as class_card
        subject = topic_subject(folded_spelling(idea))
        candidate = class_card(db,subject) or class_card(db,"type of "+subject)
        if candidate:
            cards = [candidate,*cards][:3]
    if file_format >= 19 and not complete_only:
        import current_questions
        edition = db.execute("SELECT edition FROM publication").fetchone()[0]
        refreshed = current_questions.cards(db, edition, topic_subject(folded_spelling(idea)))
        if refreshed:
            ordered = {}
            for card in cards[:1] + refreshed + cards[1:]:
                ordered.setdefault(card["key"], card)
            cards = list(ordered.values())[:3]
    if comparisons and file_format >= 11:
        from comparisons import suggest
        card = suggest(db, idea, complete_only)
        if card:
            return cards[:2] + [card]
    return cards


def topic_subject(text):
    subject = re.sub(r"^(?:please |can you )?(?:name|list|identify|give me|create|make) ", "", text)
    subject = re.sub(r"^(?:an? |the )?(?:(?:quiz|round|questions?|topic) (?:about|on) )?", "", subject)
    return re.sub(r" (?:for (?:a |an )?(?:mixed ages?(?: table)?|mixed age group|quiz night)|quiz questions|quiz)$", "", subject)


def search_topics(db, idea, complete_only=False):
    edition = db.execute("SELECT edition,format FROM publication WHERE format BETWEEN 4 AND ?", (FORMAT,)).fetchone()
    if not edition:
        raise ValueError("Rebuild the knowledge catalogue with this version of Krillion.")
    normalize = folded_spelling if edition[1] >= 8 else norm
    text = normalize(idea)
    subject = topic_subject(text)
    forms = {text, subject} - {""}
    forms.update(p[:-1] for p in list(forms) if p.endswith("s"))
    if not forms:
        return []
    marks = ",".join("?" for _ in forms)
    matches = []
    register_topics = ("JOIN class_topic t ON t.key=r.key WHERE t.term" if edition[1] >= 8 else
                       "JOIN term t ON t.key=r.class AND t.source=r.source WHERE t.alias")
    for row in db.execute(f"""SELECT DISTINCT r.key,r.source,r.class,r.n FROM register_rule r
        {register_topics} IN ({marks})
        ORDER BY r.n DESC,r.key LIMIT 3""", tuple(forms)):
        matches.append(register_card(db, row, edition[0]))
    if edition[1] >= 6 and len(matches) < 3:
        for row in db.execute(f"""SELECT DISTINCT r.key,r.prompt,r.n FROM facet_topic t JOIN facet_rule r USING(key)
            WHERE t.term IN ({marks}) ORDER BY r.n DESC,r.key LIMIT ?""", (*forms, 3-len(matches))):
            matches.append({"key": row[0], "edition": edition[0], "prompt": row[1], "records": row[2], "complete": True})
    if complete_only or len(matches) == 3:
        return matches
    extra = []
    if edition[1] >= 5:
        for row in db.execute(f"""SELECT DISTINCT p.key,p.prompt,p.n FROM feed_topic t JOIN feed_pattern p USING(key)
                WHERE t.term IN ({marks}) ORDER BY p.n DESC,p.key LIMIT 3""", tuple(forms)):
            extra.append({"key": row["key"], "edition": edition[0], "prompt": row["prompt"], "records": row["n"], "complete": False})
        matches.extend(extra[:1])
        extra = extra[1:]
        if len(matches) == 3:
            return matches
    if edition[1] >= 8:
        classes = db.execute(f"""SELECT DISTINCT r.key,r.class,r.n FROM class_topic t JOIN class_rule r USING(key)
            WHERE t.term IN ({marks}) ORDER BY r.n DESC,r.key LIMIT 3""", tuple(forms))
    else:
        classes = db.execute(f"""SELECT key,class,n FROM class_rule WHERE term IN ({marks}) OR class IN
            (SELECT key FROM term WHERE alias IN ({marks})) ORDER BY n DESC,key LIMIT 3""", (*forms, *forms))
    for row in classes:
        card = register_card(db, row, edition[0])
        card["complete"] = False
        matches.append(card)
        if len(matches) == 3:
            return matches
    topics = "topic_spelling" if edition[1] >= 8 else "topic"
    anchors = db.execute(f"""SELECT DISTINCT t.id,i.name FROM {topics} t JOIN item i USING(id)
        WHERE term IN ({marks}) ORDER BY t.id LIMIT 13""", tuple(forms)).fetchall()
    if len(anchors) == 13:
        return (matches + extra)[:3]
    if len(anchors) > 1:
        anchors = [a for a in anchors if normalize(a["name"]) in forms]
    if len(anchors) != 1:
        return (matches + extra)[:3]
    anchor = anchors[0]["id"]
    # First search groups about the named target, then groups of the named class.
    # Avoid compiling an enormous category when a smaller playable one exists.
    used = set()
    for axis in ("target", "cls"):
        rows = db.execute(f"""SELECT p.cls,p.prop,p.target,p.n,c.name subject,r.name relation,t.name object
            FROM pattern p JOIN item c ON c.id=p.cls JOIN item t ON t.id=p.target JOIN predicate r ON r.id=p.prop
            WHERE p.{axis}=? ORDER BY (p.n>4000), CASE WHEN p.n>4000 THEN p.n END,
                {'t.incoming DESC,' if axis == 'cls' else ''} p.n DESC,p.prop,p.cls,p.target LIMIT 80""", (anchor,))
        for row in rows:
            if row["prop"] in used:
                continue
            card = world_card(db, row, edition)
            if not card["records"] or row["prop"] in used or len(card["prompt"]) > 240:
                continue
            used.add(row["prop"])
            matches.append(card)
            if len(matches) == 3:
                return matches
    return (matches + extra)[:3]


def match_question(db, prompt):
    edition,file_format = db.execute("SELECT edition,format FROM publication").fetchone()
    if file_format>=20 and COMMAND.match(folded_spelling(prompt)):
        import date_rules
        import dated_links
        body=COMMAND.sub("",spelling(prompt),count=1)
        if dated := dated_links.match(db,body,edition) or date_rules.match(db,body,edition):
            return dated
    if file_format >= 10:
        from comparisons import parse
        measured = parse(db, prompt)
        if measured:
            return measured
    direct = match_single(db, prompt, include_empty=True)
    if (direct and direct["records"]) or not COMMAND.match(folded_spelling(prompt)):
        return direct
    missing = direct["key"] if direct else None
    if db.execute("SELECT format FROM publication").fetchone()[0]>=16:
        from measured_rules import match
        combined = match(db,prompt)
        if combined:
            return combined
    if db.execute("SELECT format FROM publication").fetchone()[0] >= 14:
        from world_classes import card as class_card
        direct = class_card(db,prompt_term(prompt).rstrip("?").strip(),strict=True)
        if direct:
            return direct
    # Keep "and" inside source names intact. A split is valid only when both
    # complete clauses independently resolve against the same published class.
    options = {}
    empty = []
    for split in re.finditer(r"\s+and\s+", prompt, re.I):
        first = match_single(db, prompt[:split.start()], include_empty=True)
        if not first or not first["key"].startswith(("w-", "g-", "v-", "f-")):
            continue
        head = rule_subject(db, first["key"])
        second = match_single(db, "Name a " + head + " " + prompt[split.end():], include_empty=True)
        if not second or second["key"] == first["key"]:
            continue
        keys = sorted([first["key"], second["key"]])
        operands = intersection(db, keys)
        if not operands:
            continue
        query, args, _ = operands
        count = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        key = "and:" + "+".join(keys)
        count = current_count(db, key, count, db.execute("SELECT format FROM publication").fetchone()[0])
        if count:
            options[key] = {"key": key, "prompt": prompt.strip(), "records": count,
                            "edition": first["edition"], "complete": first["complete"] and second["complete"]}
        else:
            empty.append(key)
    if len(options) > 1:
        raise ValueError("Several pairs of source rules match. Specify the class and both relationships.")
    if empty and not options:
        raise EmptyIntersection(empty[0])
    if missing and not options:
        raise UnverifiedRule(missing)
    if not options and file_format >= 25:
        body=COMMAND.sub("",spelling(prompt),count=1).removesuffix("?").strip()
        relation = match_relation(db,folded_spelling(body),body,(edition,file_format),request_target=True)
        if not relation and file_format >= 26:
            import topic_sources
            if topic := topic_sources.parse(db,prompt,bare=False):
                topic_sources.choices(db,topic,edition)
        return relation
    return next(iter(options.values()), None)


def rule_subject(db, key):
    if key.startswith(("w-","g-")):
        name = db.execute("SELECT name FROM item WHERE id=?", (int(key.split("-")[1]),)).fetchone()[0]
        return "person" if name == "human" else name
    table = "facet_rule" if key.startswith("v-") else "feed_pattern"
    return db.execute(f"SELECT class FROM {table} WHERE key=?", (key,)).fetchone()[0]


def intersection(db, keys):
    """Return an indexed membership query and the two source predicates."""
    if len(keys) != 2 or keys[0] >= keys[1]:
        return None
    graph = all(k.startswith(("g-","w-")) for k in keys)
    if not graph and keys[0][:2] != keys[1][:2]:
        return None
    selects, args, conditions = [], [], []
    if graph:
        linked = db.execute("SELECT format FROM publication").fetchone()[0]>=17
        for key in keys:
            ids = re.fullmatch(r"[gw]-(\d+)-(\d+)-(\d+)", key)
            if not ids:
                return None
            cls, prop, target = map(int, ids.groups())
            geo = key.startswith("g-")
            if geo:
                import places
                if not linked or not (area := places.condition(db,cls,prop,target)):
                    return None
            if linked:
                from world_links import current_members,info,members
                row = (area["class"],area["relation"],area["label"]) if geo else info(db,cls,prop,target)
                captured=current_members(db,key)
                if captured:
                    query="SELECT value id FROM json_each(?)"
                    values=(json.dumps([int(qid[1:]) for qid,_,_ in captured[2]]),)
                else:
                    query,values = (places.members(cls,prop,target) if geo else members(db,cls,prop,target))
            else:
                row = db.execute("""SELECT c.name,r.name,t.name FROM pattern p JOIN item c ON c.id=p.cls
                JOIN predicate r ON r.id=p.prop JOIN item t ON t.id=p.target WHERE cls=? AND prop=? AND target=?""",
                (cls, prop, target)).fetchone()
                query = "SELECT j.value id FROM pattern p,json_each(p.items) j WHERE cls=? AND prop=? AND target=?"
                values = (cls,prop,target)
            if not row or (conditions and cls != conditions[0]["classId"]):
                return None
            selects.append(f"SELECT id FROM ({query})")
            args.extend(values)
            conditions.append(area if geo else {"classId": cls, "class": row[0], "prop": prop, "relation": row[1],
                               "target": f"wd:Q{target}", "label": row[2]})
    elif keys[0].startswith(("v-", "f-")):
        closed = keys[0].startswith("v-")
        table = "facet_rule" if closed else "feed_pattern"
        for key in keys:
            row = db.execute(f"SELECT * FROM {table} WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            cond = {k: row[k] for k in ("class", "relation", "target")}
            if closed:
                cond["source"] = row["source"]
            if conditions and (cond["class"], cond.get("source")) != (conditions[0]["class"], conditions[0].get("source")):
                return None
            if closed:
                selects.append("""SELECT m.id FROM member m JOIN edge e ON (e.source,e.id)=(m.source,m.id)
                    WHERE m.source=? AND m.class=? AND e.relation=? AND e.target=?""")
                args.append(cond["source"])
            else:
                selects.append("""SELECT id FROM (WITH RECURSIVE cl(k) AS
                    (VALUES(?) UNION SELECT p.child FROM parent p JOIN cl ON p.parent=cl.k)
                    SELECT m.id FROM cl JOIN member m ON m.class=cl.k JOIN edge e ON e.id=m.id
                    WHERE e.relation=? AND e.target=?)""")
            args.extend([cond["class"], cond["relation"], cond["target"]])
            conditions.append(cond)
    else:
        return None
    return " INTERSECT ".join(selects), args, conditions


def match_single(db, prompt, *, include_empty=False, request_target=False):
    """Resolve fully worded questions only when every qualifier matches a published rule."""
    command = COMMAND
    edition = db.execute("SELECT edition,format FROM publication WHERE format BETWEEN 4 AND ?", (FORMAT,)).fetchone()
    if not edition:
        raise ValueError("Rebuild the knowledge catalogue with this version of Krillion.")
    normalize = folded_spelling if edition[1] >= 8 else norm
    text = normalize(prompt)
    if not command.match(text):
        return None
    body = re.sub(command, "", text, count=1).removesuffix("?").strip()
    place_body = re.sub(command,"",spelling(prompt),count=1).removesuffix("?").strip()
    if edition[1] >= 7:
        term = prompt_term(prompt)
        rows = db.execute("SELECT rule,prompt,n,complete FROM question_text WHERE term=? LIMIT 2", (term,)).fetchall()
        if not rows and text.endswith("?"):
            text = text[:-1].rstrip()
            rows = db.execute("SELECT rule,prompt,n,complete FROM question_text WHERE term=? LIMIT 2", (term[:-1].rstrip(),)).fetchall()
        if len(rows) > 1:
            raise ValueError("Several source rules use this wording. Select a question from the topic suggestions.")
        if rows:
            key, wording, n, complete = rows[0]
            if edition[1]>=17 and key.startswith("w-"):
                import places
                if area := places.match(db,place_body,edition[0]):
                    return area
                if edition[1]>=25:
                    from world_links import identifiers,target_label
                    target=int(key.split("-")[3])
                    label=target_label(db,target)
                    if label and identifiers(db,folded_spelling(label),"target") != [target]:
                        return match_relation(db,body,place_body,edition,include_empty=include_empty,
                                              request_target=request_target)
                n = current_count(db,key,n,edition[1])
            if not n:
                raise RuleWithdrawn("The latest source check contains no named members for this rule.")
            return {"key": key, "edition": edition[0], "prompt": wording, "records": n, "complete": bool(complete)}
    if edition[1] >= 8:
        entries = db.execute("SELECT key FROM class_topic WHERE term=? LIMIT 2", (body,)).fetchall()
        if len(entries) == 1:
            key = entries[0][0]
            complete = key.startswith("r-")
            table = "register_rule" if complete else "class_rule"
            saved = db.execute(f"SELECT * FROM {table} WHERE key=?", (key,)).fetchone()
            return {**register_card(db, saved, edition[0]), "complete": complete}
    exact = []
    for card in search_topics(db, body):
        if card["key"].startswith(("r-", "c-")):
            table = "register_rule" if card["complete"] else "class_rule"
            cls = db.execute(f"SELECT class FROM {table} WHERE key=?", (card["key"],)).fetchone()[0]
            if body == normalize(cls) or re.sub(command, "", normalize(card["prompt"]), count=1) == body:
                exact.append(card)
    for card in search_topics(db, text, complete_only=True):
        if card["key"].startswith("v-") and re.sub(command, "", normalize(card["prompt"]), count=1) == body:
            exact.append(card)
    if exact:
        return exact[0] if len(exact) == 1 else None
    # Preserve the complete subject and target on either side of the relation phrase.
    if edition[1] >= 5:
        rows = db.execute("""SELECT p.* FROM feed_topic t JOIN feed_pattern p USING(key) WHERE t.term=?""",
                          (re.sub(r"^(?:please |can you )?(?:name|identify|list|give me) ", "", text),))
        direct = [r for r in rows if re.sub(command, "", normalize(r["prompt"]), count=1) == body]
        if len(direct) == 1:
            row = direct[0]
            return {"key": row["key"], "edition": edition[0], "prompt": row["prompt"], "records": row["n"], "complete": False}
    return match_relation(db,body,place_body,edition,include_empty=include_empty,request_target=request_target)


def match_relation(db, body, place_body, edition, *, include_empty=False, resolve_only=False, request_target=False, date_key=""):
    """Resolve a relationship independently of any enclosing date or membership check."""
    normalize = folded_spelling if edition[1] >= 8 else norm
    options = {}
    unresolved = {}
    class_choices = {}
    if edition[1]>=17:
        import places
        if area := places.match(db,place_body,edition[0],include_empty=include_empty,resolve_only=resolve_only,
                                request_target=request_target and edition[1]>=25,date_key=date_key):
            return area
    phrases = [*RELATION_PHRASES.items(), (161, "starring"), (161, "with a cast including"),
               (17,"in"), (17,"in the country of")]
    # A host can also use the published property label, including properties with no custom wording.
    generic = re.fullmatch(r".+? whose (.+?) is .+", body)
    if generic:
        phrases.extend((p,"whose "+generic[1]+" is") for p, in
                       db.execute("SELECT id FROM predicate WHERE lower(name)=?",(generic[1],)))
    if edition[1]>=17:
        from world_links import identifiers,info
        # Some source properties are already phrases, such as "followed by".
        # A question generated from them should resolve again when typed verbatim.
        if any(end in body for end in (" by "," of "," in ")):
            for pid,label in db.execute("SELECT id,name FROM predicate WHERE name LIKE '% by' OR name LIKE '% of' OR name LIKE '% in'"):
                if " "+normalize(label)+" " in body:
                    phrases.append((pid,label))
        explicit = re.fullmatch(r".+? (whose (.+?) is|with [“\x22](.+?)[”\x22] recorded as) .+",body)
        if explicit:
            prop_name = explicit[2] or explicit[3]
            if re.fullmatch(r"p[1-9]\d{0,11}",prop_name):
                phrases.append((int(prop_name[1:]),explicit[1]))
            names = db.execute("""SELECT w.prop,p.name FROM world_property_name w
                JOIN predicate p ON p.id=w.prop WHERE w.term=?""",(prop_name,)).fetchall()
            canonical = [p for p,label in names if normalize(label) == prop_name]
            phrases.extend((p,explicit[1]) for p in (canonical or [p for p,_ in names]))
    for prop, phrase in dict.fromkeys(phrases):
        subject, separator, target = body.partition(" " + normalize(phrase) + " ")
        if not separator or not subject or not target:
            continue
        if subject == "person":
            subject = "human"
        if edition[1]>=17:
            classes,targets = identifiers(db,subject,"class"),identifiers(db,target,"target")
            if edition[1]>=25 and len(targets)==1 and not resolve_only:
                from target_lookup import class_choices as current_classes
                class_choices.update((c["key"],c) for c in current_classes(db,subject,prop,targets[0],edition[0]))
            if edition[1]>=25 and len(classes)==1:
                import target_lookup
                raw = place_body.partition(" "+spelling(phrase)+" ")[2] or target
                request = target_lookup.Request(classes[0],prop,spelling(raw),date_key)
                known = db.execute("SELECT 1 FROM target_lookup WHERE key=?", (request.key,)).fetchone()
                if known:
                    targets = [target_lookup.require(db,request,edition[0])]
                elif (request_target and (len(targets)!=1 or not info(db,classes[0],prop,targets[0]))
                      and target_lookup.can_request(db,request)):
                    unresolved[request]=max(unresolved.get(request,0),len(phrase))
            if len(classes)>1 or len(targets)>1:
                continue
            if classes and targets:
                cls,obj = classes[0],targets[0]
                labels = info(db,cls,prop,obj)
                if labels:
                    if resolve_only:
                        key=f"w-{cls}-{prop}-{obj}"
                        options[key]={"key": key}
                        continue
                    row=dict(cls=cls,prop=prop,target=obj,n=0,subject=labels[0],relation=labels[1],object=labels[2])
                    card=world_card(db,row,edition)
                    if card["records"] or include_empty:
                        options[card["key"]]=card
                continue
        topics = "topic_spelling" if edition[1] >= 8 else "topic"
        rows = db.execute(f"""SELECT p.cls,p.prop,p.target,p.n,c.name subject,r.name relation,t.name object
            FROM {topics} tc JOIN pattern p ON p.cls=tc.id JOIN {topics} tt ON tt.id=p.target AND tt.term=?
            JOIN item c ON c.id=p.cls JOIN item t ON t.id=p.target JOIN predicate r ON r.id=p.prop
            WHERE tc.term=? AND p.prop=? LIMIT 2""", (target, subject, prop))
        for row in rows:
            card = world_card(db, row, edition)
            if card["records"]:
                options[card["key"]] = card
    if class_choices and not any(c.get("records") for c in options.values()):
        from date_rules import DateChoices
        raise DateChoices(list(class_choices.values()),"Select the subject meant by this name.")
    if not options and unresolved:
        from target_lookup import require
        longest=max(unresolved.values())
        specific=[request for request,weight in unresolved.items() if weight==longest]
        if len(specific)==1:
            require(db,specific[0],edition[0])
    if len(options) != 1:
        return None
    selected = next(iter(options.values()))
    if resolve_only:
        return selected
    if not selected["records"] and db.execute("SELECT 1 FROM live_rule WHERE key=?", (selected["key"],)).fetchone():
        raise RuleWithdrawn("The latest source check contains no named members for this rule.")
    return selected


def play(db, game, key, prompt, edition=None):
    from compiler import evaluate
    release = db.execute("SELECT edition,format FROM publication WHERE format BETWEEN 4 AND ?", (FORMAT,)).fetchone()
    if not release or edition and edition != release[0]:
        raise ValueError("The knowledge publication changed. Search again for the latest edition.")
    if key.startswith("during:") and release[1]>=20:
        import dated_links
        if rule := dated_links.Rule.decode(key):
            return dated_links.play(db,game,rule)
        raise ValueError("Unknown dated relationship.")
    if key.startswith("date-"):
        if release[1]<20:
            raise RuleWithdrawn("This edition has no published date records.")
        import date_rules
        if rule := date_rules.Rule.decode(key):
            return date_rules.play(db,game,rule,prompt,release[1])
        raise ValueError("Unknown date question.")
    if key.startswith("mixed:") and release[1]>=16:
        from measured_rules import play as play_mixed
        return play_mixed(db,game,key,prompt)
    if key.startswith(("type:","kind:")) and release[1] >= 14:
        from world_classes import play as play_class
        return play_class(db,game,key,prompt)
    if key.startswith("compare:") and release[1] >= 10:
        from comparisons import play as play_comparison
        return play_comparison(db, game, key, prompt)
    if key.startswith("and:"):
        operands = intersection(db, key[4:].split("+"))
        if not operands:
            raise RuleWithdrawn("The source no longer offers both relationships for this question.")
        query, args, conditions = operands
        count = db.execute(f"SELECT count(*) FROM ({query})", args).fetchone()[0]
        count = current_count(db, key, count, release[1])
        if not count:
            raise RuleWithdrawn("No source records currently meet both conditions.")
        if "classId" not in conditions[0]:
            plan = {"class": conditions[0]["class"], "conditions": conditions, "catalogueKey": key}
            if "source" in conditions[0]:
                plan.update(register=conditions[0]["source"], expectedMatches=count)
            return evaluate(db, game, prompt, plan)
        return play_world(db, game, key, prompt, query, args, conditions, release[1])
    if key.startswith("v-") and release[1] >= 6:
        row = db.execute("SELECT source,class,relation,target,n FROM facet_rule WHERE key=?", (key,)).fetchone()
        if not row:
            raise RuleWithdrawn("This property is no longer in the source register.")
        return evaluate(db, game, prompt, {"class": row["class"], "register": row["source"], "edgeSource": row["source"],
                        "relation": row["relation"], "target": row["target"], "expectedMatches": row["n"], "catalogueKey": key})
    if key.startswith("f-") and release[1] >= 5:
        row = db.execute("SELECT class,relation,target FROM feed_pattern WHERE key=?", (key,)).fetchone()
        if not row:
            raise RuleWithdrawn("This relationship is no longer in the publication.")
        return evaluate(db, game, prompt, {**dict(row), "catalogueKey": key})
    if key.startswith("c-"):
        row = db.execute("SELECT class FROM class_rule WHERE key=?", (key,)).fetchone()
        if not row:
            raise RuleWithdrawn("This class is no longer in the publication.")
        return evaluate(db, game, prompt, {"class": row["class"], "catalogueKey": key})
    if key.startswith("r-"):
        rule = db.execute("SELECT source,class FROM register_rule WHERE key=?", (key,)).fetchone()
        if not rule:
            raise RuleWithdrawn("This register is no longer in the publication.")
        return evaluate(db, game, prompt, {"class": rule["class"], "register": rule["source"], "catalogueKey": key})
    if release[1]>=17 and (geo := re.fullmatch(r"g-([1-9]\d{0,11})-([1-9]\d{0,11})-([1-9]\d{0,11})",key)):
        import places
        cls,prop,target = map(int,geo.groups())
        if not (condition := places.condition(db,cls,prop,target)):
            raise RuleWithdrawn("The geographic source rule is unavailable in this edition.")
        query,args=places.members(cls,prop,target)
        return play_world(db,game,key,prompt,query,args,[condition],release[1])
    ids = re.fullmatch(r"w-(\d+)-(\d+)-(\d+)", key)
    if not ids:
        raise ValueError("Unknown catalogue question.")
    cls, prop, target = map(int, ids.groups())
    if release[1]>=17:
        from world_links import info,members
        rule = info(db,cls,prop,target)
        query,args = members(db,cls,prop,target)
    else:
        rule = db.execute("""SELECT c.name,r.name,t.name FROM pattern p JOIN item c ON c.id=p.cls
        JOIN predicate r ON r.id=p.prop JOIN item t ON t.id=p.target WHERE cls=? AND prop=? AND target=?""",
        (cls, prop, target)).fetchone()
        query,args = "SELECT j.value id FROM pattern p,json_each(p.items) j WHERE cls=? AND prop=? AND target=?", (cls,prop,target)
    if not rule:
        raise RuleWithdrawn("This relationship is no longer in the publication.")
    conditions = [{"classId": cls, "class": rule[0], "prop": prop, "relation": rule[1],
                   "target": f"wd:Q{target}", "label": rule[2]}]
    return play_world(db, game, key, prompt,query,args,conditions,release[1])


def play_world(db, game, key, prompt, query, args, conditions, file_format, *, date_capture=None, date_proof=None,related_source=None):
    from compiler import evaluate
    from world_classes import DESCENDANTS
    first = conditions[0]
    geographic = [c for c in conditions if c.get("geo")]
    roles = date_capture is not None or (bool(geographic) and len(geographic)==len(conditions))
    cls, subject = first["classId"], first["class"]
    live = date_capture
    if live is None and file_format>=9:
        live = db.execute("SELECT * FROM live_rule WHERE key=?", (key,)).fetchone()
    joined=None
    if not live and file_format>=17 and key.startswith("and:"):
        from world_links import joined_release
        joined=joined_release(db,key[4:].split("+"),query,args)
        live=joined
    if live and not live["n"]:
        raise RuleWithdrawn("The latest source check contains no named members for this rule.")
    captured = date_capture or joined or (json.loads(gzip.decompress(live["release"])) if live else {})
    linked = file_format >= 17 and not live
    root = "wd:Q"+str(cls) if linked else subject
    if linked:
        db.execute("DROP TABLE IF EXISTS temp.link_scope")
        db.execute("CREATE TEMP TABLE link_scope(id INTEGER PRIMARY KEY)")
        db.execute("INSERT INTO link_scope "+DESCENDANTS+" SELECT id FROM scope",(cls,))
    def items():
        if live:
            for item, name, aliases in captured["rows"]:
                source_id = int(item[1:])
                known = db.execute("SELECT incoming FROM item WHERE id=?", (source_id,)).fetchone()
                yield source_id, name, aliases, known[0] if known else 0
        else:
            for row in db.execute(f"SELECT i.* FROM ({query}) m JOIN item i ON i.id=m.id", args):
                yield row["id"], row["name"], row["aliases"].split("\t"), row["incoming"]
    def records():
        for item, name, aliases, incoming in items():
            membership_property = 31
            types = (["wd:Q"+str(row[0]) for row in db.execute(
                "SELECT m.cls FROM world_member m JOIN link_scope s ON s.id=m.cls WHERE m.id=?",(item,))]
                if linked else [subject])
            if linked and roles and not types:
                membership_property = 106
                types=["wd:Q"+str(row[0]) for row in db.execute(
                    "SELECT r.role FROM link_scope s CROSS JOIN world_role r ON (r.role,r.id)=(s.id,?)",(item,))]
            url=f"https://www.wikidata.org/wiki/Q{item}#P{membership_property if linked else first['prop']}"
            if roles and live:
                from class_updates import evidence
                url=evidence(f"type:Q{cls}",f"Q{item}")
            record = {"id": f"wd:Q{item}", "name": name, "aliases": [a for a in aliases if 0 < len(a) <= 140],
                      "url": url, "types": types,
                      "edges": [(c["relation"], c["target"]) for c in conditions if not related_source or c.get("dated")], "depth": 1 if incoming >= 1000 else 2}
            if incoming:
                record["graph_recognition"] = [0, incoming]
            yield record
    # Only this answering group is projected. The published catalogue stays read-only.
    with tempfile.TemporaryDirectory() as folder, closing(open_graph(Path(folder) / "selection.sqlite")) as graph:
        world_revision = db.execute("SELECT revision FROM source WHERE id='wikidata5m-catalogue-input'").fetchone()[0]
        source = ({"source": "wikidata-query-" + digest(key), "revision": digest([live["revision"], world_revision]), "captured": live["captured"],
                   "url": f"https://www.wikidata.org/wiki/Q{cls}"} if live else
                  {"source": "world5m-" + key, "revision": world_revision, "url": SOURCE})
        if joined:
            hierarchy = db.execute("SELECT revision FROM source WHERE id='wikidata-current-taxonomy'").fetchone()
            source["source"]="wikidata-joined-"+digest(key)
            source["revision"]=digest([source["revision"],hierarchy[0] if hierarchy else None])
        ingest(graph, {**source,
                       "terms": {root: [subject], **{c["target"]: [c["label"]] for c in conditions}}}, records())
        if related_source:
            r=related_source
            graph.execute("INSERT INTO source VALUES(?,?,?,?,'{}')",(r["source"],r["revision"],r["url"],r["captured"]))
            graph.executemany("INSERT INTO edge SELECT ?,id,?,? FROM entity",(
                (r["source"],c["relation"],c["target"]) for c in conditions if not c.get("dated")))
            graph.commit()
        if not graph.execute("SELECT 1 FROM entity LIMIT 1").fetchone():
            raise RuleWithdrawn("This edition has no named members for this relationship.")
        if linked:
            taxonomy = "world5m-class-taxonomy"
            hierarchy = db.execute("SELECT revision,url,checked FROM source WHERE id='wikidata-current-taxonomy'").fetchone()
            if hierarchy:
                taxonomy = "wikidata-current-taxonomy"
            checked = db.execute("SELECT checked FROM source WHERE id='wikidata5m-catalogue-input'").fetchone()[0]
            graph.execute("INSERT INTO source VALUES(?,?,?,?,'{}')",(taxonomy,*(hierarchy or (world_revision,SOURCE,checked))))
            graph.executemany("INSERT INTO parent VALUES(?,?,?)",(
                (taxonomy,"wd:Q"+str(child),"wd:Q"+str(parent)) for child,parent in db.execute(
                "SELECT p.child,p.parent FROM link_scope s CROSS JOIN world_parent p ON p.parent=s.id")))
            graph.commit()
        outside_names = ()
        if file_format >= 18:
            from world_links import excluded_names
            wanted = {folded_spelling(name) for row in graph.execute("SELECT name,aliases FROM entity")
                      for name in (row[0],*json.loads(row[1]))}
            included = {int(row[0][4:]) for row in graph.execute("SELECT id FROM entity")}
            outside_names = excluded_names(db,cls,wanted,included,roles=roles)
            if joined:
                from itertools import chain
                from world_links import current_members
                def excluded_current():
                    for operand in key[4:].split("+"):
                        release=current_members(db,operand)
                        for qid,name,aliases in release[2] if release else ():
                            if int(qid[1:]) not in included:
                                for pos,form in enumerate((name,*aliases)):
                                    if folded_spelling(form) in wanted:
                                        yield "wd:"+qid,form,pos==0
                outside_names=chain(outside_names,excluded_current())
        elif file_format >= 5:
            db.executescript("""CREATE TEMP TABLE IF NOT EXISTS wanted(term TEXT PRIMARY KEY) WITHOUT ROWID;
                CREATE TEMP TABLE IF NOT EXISTS included(id INTEGER PRIMARY KEY); DELETE FROM wanted; DELETE FROM included;""")
            db.executemany("INSERT INTO wanted VALUES(?)", graph.execute("SELECT DISTINCT term FROM entity_name"))
            db.executemany("INSERT INTO included VALUES(?)", graph.execute("SELECT CAST(substr(id,5) AS INTEGER) FROM entity"))
            if live and file_format>=17:
                others=db.execute(DESCENDANTS+""" SELECT DISTINCT i.id,i.name,i.aliases FROM scope s
                    CROSS JOIN world_member m INDEXED BY world_members_by_class ON m.cls=s.id CROSS JOIN item i ON i.id=m.id
                    WHERE NOT EXISTS(SELECT 1 FROM included WHERE id=i.id)""",(cls,))
                wanted = {folded_spelling(name) for row in graph.execute("SELECT name,aliases FROM entity")
                          for name in (row[0],*json.loads(row[1]))}
            else:
                wanted = None
                scope_clause = "m.cls IN (SELECT id FROM link_scope)" if linked else "m.cls=?"
                others = db.execute(f"""SELECT DISTINCT i.id,i.name,i.aliases FROM wanted w
                CROSS JOIN world_homonym h ON h.term=w.term CROSS JOIN world_member m ON m.id=h.id AND {scope_clause}
                CROSS JOIN item i ON i.id=m.id WHERE NOT EXISTS(SELECT 1 FROM included WHERE id=i.id)""", () if linked else (cls,))
            outside_names = (("wd:Q" + str(row[0]), name, pos == 0) for row in others
                             for pos, name in enumerate((row[1], *row[2].split("\t"))) if wanted is None or folded_spelling(name) in wanted)
        plan = {"class": root, "classLabel": subject, "catalogueKey": key}
        if live:
            plan["liveClass"] = cls
        if len(conditions) == 1 and not linked and not geographic and date_capture is None:
            plan.update(relation=first["relation"], target=first["target"])
        else:
            plan["conditions"] = conditions
        proof=date_proof
        if date_capture is not None:
            plan["dateScope"] = ("Wikidata must record the "+first["relation"]+" "+first["label"]+
                " in the Gregorian calendar, with at least year precision. Any matching recorded date qualifies.")
            if first["prop"]==577:
                plan["dateScope"] += " Regional releases and re-releases count."
            if file_format>=21:
                wording=db.execute("""SELECT s.name,i.name,s.description FROM date_subject s JOIN item i
                    ON i.id=s.cls WHERE s.cls=?""",(cls,)).fetchone()
                if wording and wording[2] and folded_spelling(wording[0])!=folded_spelling(wording[1]):
                    plan["dateScope"] += " Wikidata describes this class as “"+wording[2]+"”."
        if related_source:
            from places import scope
            plan["dateScope"] += " "+scope(conditions[1:])
        elif geographic:
            import places
            keys=[f"g-{cls}-{c['prop']}-{c['target'][4:]}" for c in geographic]
            checked=[k for k in keys if live and (not joined or db.execute("SELECT 1 FROM live_rule WHERE key=?",(k,)).fetchone())]
            proof=places.proof_reader(db,checked,None if joined else captured.get("proofs",{}))
            plan["geographicScope"]=places.scope(conditions)
        unnamed=captured["scanned"]-len(captured["rows"]) if "scanned" in captured else 0
        edition = db.execute("SELECT edition FROM publication").fetchone()[0]
        return evaluate(graph, game, prompt, plan, outside_names=outside_names,name_edition=edition,name_catalogue=db,
                        relationship_proof=proof,unnamed_members=unnamed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(os.environ.get("DB_PATH", "storage/quiz.sqlite")),
                        help="Use this game's storage paths. The builder never opens the game database.")
    parser.add_argument("--world", type=Path, help="An installed Wikidata5M source index")
    parser.add_argument("--measurements", type=Path, help="A class-independent Wikidata measurement export")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--feeds", type=Path, help="Directory of additional source manifests")
    parser.add_argument("--feeds-only", action="store_true", help="Build solely from the supplied manifests without installing bundled sources")
    parser.add_argument("--offline", action="store_true", help="Use installed sources and the last validated source downloads")
    parser.add_argument("--watch", action="store_true", help="Refresh daily and retain the last publication if a source fails")
    args = parser.parse_args()
    if args.feeds_only and args.world:
        parser.error("--feeds-only and --world select different input sources")
    world = None if args.feeds_only else args.world or index_path(args.db)
    while True:
        try:
            if world is not None and not args.world and not args.offline and not world.exists():
                install(world)
            if world is not None and not args.offline:
                import date_properties
                try:
                    date_properties.refresh((args.output or catalogue_path(args.db)).parent)
                except (OSError,ValueError,KeyError) as error:
                    print(f"Date-property update deferred: {error}",flush=True)
            if world is not None and not args.offline and not args.watch:
                import taxonomy
                taxonomy.tick(world,taxonomy.location((args.output or catalogue_path(args.db)).parent),pages=None)
            result = build(world, args.output or catalogue_path(args.db),
                           args.feeds or args.db.parent / (args.db.name + ".knowledge") / "feeds",
                           offline=args.offline, include_bundled=not args.feeds_only, measurements=args.measurements,
                           discover_dates=True,discover_names=True)
            print(json.dumps(result), flush=True)
            delay = 86400
        except (OSError, ValueError, sqlite3.Error, KeyError) as error:
            if not args.watch:
                raise
            print(f"Publication deferred: {error}", flush=True)
            delay = 300
        if not args.watch:
            break
        until = time.monotonic() + delay
        while (remaining := until-time.monotonic()) > 0:
            time.sleep(min(60, remaining))
            if not args.offline and world is not None and delay == 86400:
                import taxonomy
                from watched_rules import due_requests
                storage = (args.output or catalogue_path(args.db)).parent
                import attested_names, date_discovery, target_discovery
                if taxonomy.tick(world,taxonomy.location(storage)) or due_requests(storage,args.output or catalogue_path(args.db)) or date_discovery.is_due(storage) or target_discovery.is_due(storage) or attested_names.is_due(storage):
                    break
