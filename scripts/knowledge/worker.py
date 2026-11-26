"""Run one bounded local research job. The web process handles players independently."""

import argparse
import contextlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from compiler import evaluate, interpret, noun
from graph import ROOT, digest, norm, open_graph
from sources import (
    bootstrap,
    refresh_fish,
    import_file,
    relation_request,
    wikidata,
)
from world import Ambiguous, source_revision
from world import prepare as prepare_world
from world_facts import refresh_watched
from world_index import SOURCE as WORLD_SOURCE
from world_index import index_path


@contextlib.contextmanager
def writer_lock(path):
    with path.open("a+b") as f:
        if os.name == "nt":
            import msvcrt

            if not f.tell():
                f.write(b"0")
                f.flush()
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def prune_previews(game):
    cutoff = int((time.time() - 7 * 86400) * 1000)
    with game:
        game.execute(
            "DELETE FROM kb_job WHERE created<? AND status IN ('ready','failed','suggested') AND NOT EXISTS(SELECT 1 FROM kb_catalogue_entry e WHERE e.job=kb_job.id)",
            (cutoff,),
        )
    for (
        bank,
    ) in game.execute("""SELECT b.id FROM kb_bank b WHERE NOT EXISTS(SELECT 1 FROM kb_job j WHERE j.bank=b.id)
        AND NOT EXISTS(SELECT 1 FROM library l WHERE json_extract(l.content,'$.knowledge')=b.id)
        AND NOT EXISTS(SELECT 1 FROM rounds r WHERE json_extract(r.snapshot,'$.knowledge')=b.id)
        AND NOT EXISTS(SELECT 1 FROM kb_round_proof p WHERE p.bank=b.id) LIMIT 4""").fetchall():
        with game:
            for table in ("kb_name", "kb_text", "kb_group", "kb_answer_key", "kb_member"):
                game.execute(f"DELETE FROM {table} WHERE bank=?", (bank,))
            game.execute("DELETE FROM kb_bank WHERE id=?", (bank,))


def check_budget(folder, gamefile):
    used = sum(p.stat().st_size for p in folder.rglob("*") if p.is_file())
    used += sum(
        p.stat().st_size
        for p in gamefile.parent.glob(gamefile.name + "*")
        if p.is_file()
    )
    feeds = sum(p.stat().st_size for p in (folder / "feeds").glob("*") if p.is_file())
    if (
        used > 30_000_000_000
        or feeds > 2_000_000_000
        or shutil.disk_usage(folder).free < 2_000_000_000
    ):
        raise ValueError(
            "Knowledge storage budget reached. Existing rounds remain available."
        )


def evidence_for_review(db, game, req):
    row = game.execute(
        "SELECT snapshot FROM rounds WHERE id=?", (req["round"],)
    ).fetchone()
    q = json.loads(row[0])
    plan = q.get("plan", {})
    links = []
    for entity in db.execute(
        """SELECT DISTINCT e.id,e.name,e.url FROM entity_name n
        JOIN entity e USING(id) WHERE term=? ORDER BY name LIMIT 8""",
        (norm(req["raw"]),),
    ):
        if plan.get("class"):
            member = db.execute(
                """WITH RECURSIVE c(k) AS (VALUES(?) UNION SELECT child FROM parent JOIN c ON parent=c.k)
                SELECT 1 FROM member JOIN c ON class=k WHERE id=? LIMIT 1""",
                (plan["class"], entity["id"]),
            ).fetchone()
            if not member:
                continue
        if "property" in plan:
            for fact in db.execute(
                "SELECT low,high,basis,url FROM fact WHERE id=? AND property=? ORDER BY basis DESC LIMIT 3",
                (entity["id"], plan["property"]),
            ):
                unit = {"length": "cm", "mass": "g", "year": "years"}[plan["property"]]
                links.append(
                    {
                        "label": entity["name"]
                        + " · "
                        + fact["basis"]
                        + " "
                        + str(fact["low"])
                        + "–"
                        + str(fact["high"])
                        + " "
                        + unit,
                        "url": fact["url"],
                    }
                )
        links.append({"label": entity["name"], "url": entity["url"]})
        if len(links) >= 5:
            break
    if not links:
        # Share a search link with the host. Player-submitted text stays local until the host opens it.
        from urllib.parse import urlencode

        url = "https://www.wikidata.org/w/index.php?" + urlencode(
            {"search": req["raw"]}
        )
        links.append({"label": "Search Wikidata for " + req["raw"], "url": url})
    owners = db.execute(
        "SELECT DISTINCT id FROM entity_name WHERE term=? LIMIT 2", (norm(req["raw"]),)
    ).fetchall()
    if (
        len(owners) == 1
        and links
        and links[0]["label"] != "Search Wikidata for " + req["raw"]
    ):
        links[0]["entity"] = owners[0][0]
    return links[:5]


def queue_saved(db, game):
    if not game.execute("SELECT 1 FROM sqlite_master WHERE name='library'").fetchone():
        return
    revisions = db.execute(
        "SELECT id,revision FROM source WHERE id NOT LIKE 'world5m-%' AND id NOT LIKE 'quantity-ref-%' AND id NOT LIKE 'quantity-class-%' ORDER BY id"
    )
    release = digest(["source-judged-editions-v2", [tuple(row) for row in revisions]])
    with game:
        if game.execute(
            "SELECT 1 FROM kb_sync WHERE id=1 AND sources=?", (release,)
        ).fetchone():
            return
        import uuid

        for (
            room,
            entry,
            revision,
            prompt,
        ) in game.execute("""SELECT l.room,l.id,l.revision,json_extract(l.content,'$.prompt')
            FROM library l JOIN rooms r ON r.code=l.room WHERE NOT r.finished AND json_extract(l.content,'$.knowledge') IS NOT NULL
            AND json_extract(l.content,'$.plan.catalogueKey') IS NULL"""):
            job = uuid.uuid4().hex
            game.execute(
                "INSERT INTO kb_job(id,room,prompt,research,status,created) VALUES(?,?,?,2,'queued',?)",
                (job, room, prompt, int(time.time() * 1000)),
            )
            game.execute("INSERT INTO kb_rebuild VALUES(?,?,?)", (job, entry, revision))
        game.execute("INSERT OR REPLACE INTO kb_sync VALUES(1,?)", (release,))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("build", "import", "refresh", "review"))
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    folder = args.db.parent / (args.db.name + ".knowledge")
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["SQLITE_TMPDIR"] = str(folder)
    req = json.load(sys.stdin) if args.action == "build" else None
    with writer_lock(folder / "writer.lock"):
        check_budget(folder, args.db)
        if req:
            from catalogue import UnverifiedRule, catalogue_path, match_question, play, reader
            from watched_rules import follow
            published = catalogue_path(args.db)
            if req.get("catalogueKey") or (published.is_file() and not req.get("research")):
                with contextlib.closing(reader(published)) as publication:
                    key = req.get("catalogueKey")
                    if not key:
                        try:
                            from date_rules import DateChoices
                            match = match_question(publication, req["prompt"])
                        except DateChoices as choices:
                            if choices.cards:
                                print(json.dumps({"chooseTopic":True,"message":str(choices)}))
                                return
                            raise
                        except UnverifiedRule as empty:
                            registered=[follow(args.db.parent,missing) for missing in empty.requests]
                            if all(registered):
                                edition=publication.execute("SELECT edition FROM publication").fetchone()[0]
                                print(json.dumps({"awaitingEdition":edition}))
                                return
                            raise
                        key = match["key"] if match else None
                    if key:
                        if not key.startswith("date-"):
                            follow(args.db.parent, key)
                        with contextlib.closing(sqlite3.connect(args.db, timeout=5)) as game:
                            game.execute("PRAGMA foreign_keys=ON")
                            prune_previews(game)
                            bank, _ = play(publication, game, key, req["prompt"], req.get("edition"))
                            if publication.execute("SELECT format FROM publication").fetchone()[0] >= 33:
                                import entity_labels
                                try:
                                    entity_labels.request(args.db.parent, publication, game, bank)
                                except (OSError,sqlite3.Error) as error:
                                    print(f"Name-check queue unavailable: {error}",file=sys.stderr)
                        if key.startswith("date-"):
                            from date_rules import Rule,segments
                            for source in {row[0] for row in segments(publication,Rule.decode(key))}:
                                follow(args.db.parent,source)
                        edition = publication.execute("SELECT edition FROM publication").fetchone()[0]
                        print(json.dumps({"bank": bank, "rule": key, "edition": edition}))
                        return
        db = open_graph(folder / "graph.sqlite")
        game = sqlite3.connect(args.db, timeout=5)
        game.execute("PRAGMA foreign_keys=ON")
        game.execute("PRAGMA max_page_count=2000000")
        game.executescript((ROOT / "server/knowledge.sql").read_text())
        try:
            bootstrap(db)
            import m49
            m49.refresh(db, args.db.parent / "knowledge-sources" / "un-m49.json.gz", offline=True)
            if index_path(args.db).is_file():
                release = source_revision()
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO source SELECT 'wikidata5m',?,?,unixepoch(),'{}' WHERE NOT EXISTS (SELECT 1 FROM source WHERE id='wikidata5m' AND revision=?)",
                        (release, WORLD_SOURCE, release),
                    )
            if args.action == "review":
                print(
                    json.dumps(
                        {
                            "evidence": evidence_for_review(
                                db, game, json.load(sys.stdin)
                            )
                        }
                    )
                )
                return
            if args.action == "import":
                import_file(db, args.manifest)
                queue_saved(db, game)
                return
            if args.action == "refresh":
                prune_previews(game)
                for manifest in sorted((folder / "feeds").glob("*.manifest.json")):
                    import_file(db, manifest)
                queue_saved(db, game)
                failures = refresh_watched(db)
                refresh_fish(db, args.db.parent / "knowledge-sources" / "fishbase.sqlite", False)
                for row in db.execute(
                    "SELECT id FROM source WHERE id LIKE 'wd:%' AND NOT EXISTS (SELECT 1 FROM world_watch w WHERE w.kind='class' AND source.id='wd:'||json_extract(w.args,'$[1]')||':'||json_extract(w.args,'$[0]')) ORDER BY checked LIMIT 3"
                ).fetchall():
                    _, qid, term = row[0].split(":", 2)
                    try:
                        wikidata(db, term, qid)
                    except (OSError, ValueError) as err:
                        failures.append(term + ": " + str(err))
                for row in db.execute("SELECT args FROM recipe LIMIT 3").fetchall():
                    term, qid, relation, target, *pid = json.loads(row[0])
                    try:
                        from sources import relation_source
                        relation_source(db, term, qid, relation, target, None, pid[0] if pid else None)
                    except (OSError, ValueError) as err:
                        failures.append(term + " " + relation + ": " + str(err))
                if failures:
                    raise ValueError(
                        "Some source updates were deferred. " + "; ".join(failures)
                    )
                return
            prompt = req["prompt"]
            inbox = folder / "feeds"
            for manifest in sorted(inbox.glob("*.manifest.json")):
                import_file(db, manifest)
            queue_saved(db, game)
            subject = noun(
                re.sub(r"^(?:name|give me|identify) ", "", prompt, flags=re.IGNORECASE)
            )
            from scoped_topics import require_explicit_subject
            require_explicit_subject(db, subject)
            simple = bool(
                re.fullmatch(r"[\w '’\-]{1,80}", subject)
                and not re.search(
                    r"\b(that|which|who|than|and|or|between|from|before|after)\b",
                    subject,
                    re.IGNORECASE,
                )
            )
            simple = (
                simple
                and len(subject.split()) <= 5
                and not re.match(
                    r"^(which|what|where|when|who|how|can|could|does|is)\b",
                    subject,
                    re.IGNORECASE,
                )
            )
            linked = relation_request(subject)
            try:
                world_plan = prepare_world(
                    db, args.db, prompt, req.get("research", False), req.get("snapshotOnly", False)
                )
            except Ambiguous as ambiguity:
                # Specialised sources can already identify the intended subject unambiguously.
                try:
                    interpret(db, prompt)
                except ValueError:
                    raise ambiguity
                world_plan = None
            known_relation = (
                linked
                and db.execute(
                    "SELECT 1 FROM edge e JOIN term t ON t.key=e.target JOIN member m ON m.id=e.id JOIN term c ON c.key=m.class WHERE e.relation=? AND t.alias=? AND c.alias=? LIMIT 1",
                    (linked[1], norm(linked[2]), norm(linked[0])),
                ).fetchone()
            )
            if (
                not world_plan
                and not req.get("snapshotOnly")
                and linked
                and (req.get("research") or not known_relation)
            ):
                term, relation, target = linked
                wikidata(db, term, relation=relation, target=target)
            elif not req.get("snapshotOnly") and (
                not world_plan
                and req.get("research")
                or simple
                and not world_plan
                and not db.execute(
                    "SELECT 1 FROM term WHERE alias=?", (norm(subject),)
                ).fetchone()
            ):
                if simple:
                    from compiler import class_term

                    try:
                        subject = class_term(db, subject)
                    except ValueError:
                        subject = subject.lower()
                    wikidata(db, subject)
                elif not world_plan:
                    raise ValueError(
                        "Live class discovery needs a single named subject. Compile comparisons using indexed measurements."
                    )
            # Cache validated interpretations by the active source and model revisions.

            fingerprint = digest(
                [
                    4,
                    prompt,
                    [
                        tuple(r)
                        for r in db.execute(
                            "SELECT id,revision FROM source ORDER BY id"
                        )
                    ],
                    os.environ.get("KNOWLEDGE_MODEL", ""),
                    os.environ.get("KNOWLEDGE_MODEL_URL", ""),
                ]
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS query_plan(key TEXT PRIMARY KEY, plan TEXT NOT NULL)"
            )
            cached = db.execute(
                "SELECT plan FROM query_plan WHERE key=?", (fingerprint,)
            ).fetchone()
            plan = world_plan or (
                json.loads(cached[0]) if cached else interpret(db, prompt)
            )
            if not cached:
                with db:
                    db.execute(
                        "INSERT OR REPLACE INTO query_plan VALUES(?,?)",
                        (fingerprint, json.dumps(plan)),
                    )
            bank, _meta = evaluate(db, game, prompt, plan)
            print(json.dumps({"bank": bank}))
        finally:
            if args.action == "refresh":
                queue_saved(db, game)
            game.close()
            db.close()


if __name__ == "__main__":
    from catalogue import RuleWithdrawn
    try:
        main()
    except RuleWithdrawn as e:
        print(str(e), file=sys.stderr)
        sys.exit(3)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, ImportError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)
