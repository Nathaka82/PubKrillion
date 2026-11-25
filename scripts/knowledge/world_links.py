"""Index source relationships independently of the questions that might use them."""

import gzip
import json

from graph import folded_spelling
from world_classes import DESCENDANTS

SCHEMA = """
CREATE TABLE world_link(prop INTEGER,target INTEGER,id INTEGER,PRIMARY KEY(prop,target,id)) WITHOUT ROWID;
CREATE TABLE world_name(term TEXT,id INTEGER,preferred INTEGER,PRIMARY KEY(term,id)) WITHOUT ROWID;
CREATE TABLE world_target(id INTEGER PRIMARY KEY);
CREATE VIEW world_target_name AS SELECT n.term,n.id target,n.preferred FROM world_name n JOIN world_target t ON t.id=n.id
    UNION ALL SELECT term,target,preferred FROM current_target_name;
CREATE TABLE world_property_name(term TEXT,prop INTEGER,PRIMARY KEY(term,prop)) WITHOUT ROWID;
"""


def publish(db, reuse):
    tables = ("world_link", "world_property_name")
    if reuse:
        for table in tables:
            db.execute(f"INSERT INTO main.{table} SELECT * FROM prior.{table}")
    else:
        db.execute("""INSERT OR IGNORE INTO world_link SELECT p,o,s FROM world.triple
                      WHERE p NOT IN (31,106,279) ORDER BY p,o,s""")
        db.executemany(
            "INSERT OR IGNORE INTO world_property_name VALUES(?,?)",
            (
                (folded_spelling(name), pid)
                for pid, aliases in db.execute("SELECT id,names FROM world.property")
                for name in aliases.split("\t")
                if name
            ),
        )
    if reuse and db.execute("SELECT format FROM prior.publication").fetchone()[0] >= 18:
        db.execute("INSERT INTO world_name SELECT * FROM prior.world_name")
        db.execute("INSERT INTO world_target SELECT * FROM prior.world_target")
    else:
        db.execute("INSERT INTO world_target SELECT DISTINCT target FROM world_link")

        def names():
            for item, label, aliases in db.execute("SELECT id,name,aliases FROM item"):
                for name in {label, *aliases.split("\t")}:
                    if name and len(name) <= 140:
                        yield folded_spelling(name), item, int(name == label)

        db.executemany(
            """INSERT INTO world_name VALUES(?,?,?) ON CONFLICT(term,id)
                       DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
            names(),
        )
    db.execute("CREATE INDEX world_link_by_member ON world_link(id,prop,target)")


def excluded_names(db, cls, wanted, included, *, roles=False):
    """Resolve an incoming alias against all known names in the same source class."""
    role_query = """ UNION ALL SELECT 1 FROM scope s CROSS JOIN world_role r
        ON (r.role,r.id)=(s.id,n.id)""" if roles else ""
    lookup = (cls,json.dumps(list(wanted)),json.dumps(list(included)))
    file_format = db.execute("SELECT format FROM publication").fetchone()[0]
    if file_format>=20:
        names, index = ("scope_name","scope_names_by_term") if file_format>=23 else ("date_name","date_names_by_term")
        for item,display,preferred in db.execute(DESCENDANTS+f""" SELECT DISTINCT n.id,n.display,n.preferred
            FROM json_each(?) w CROSS JOIN {names} n INDEXED BY {index} ON n.term=w.value
            WHERE +n.cls IN (SELECT id FROM scope) AND n.id NOT IN (SELECT value FROM json_each(?))""",lookup):
            yield "wd:Q"+str(item),display,bool(preferred)
    rows = db.execute(
        DESCENDANTS
        + f""" SELECT DISTINCT i.id,i.name,i.aliases FROM json_each(?) w
        CROSS JOIN world_name n ON n.term=w.value CROSS JOIN item i ON i.id=n.id WHERE
        n.id NOT IN (SELECT value FROM json_each(?)) AND EXISTS(
        SELECT 1 FROM world_member m WHERE m.id=n.id AND m.cls IN (SELECT id FROM scope){role_query})""",
        lookup,
    )
    for item, name, aliases in rows:
        for pos, form in enumerate((name, *aliases.split("\t"))):
            if folded_spelling(form) in wanted:
                yield "wd:Q" + str(item), form, pos == 0


def members(db, cls, prop, target):
    """Drive the join from a small target group or, for common targets, from the class."""
    many = db.execute(
        "SELECT 1 FROM world_link WHERE prop=? AND target=? LIMIT 1 OFFSET 4096", (prop, target)
    ).fetchone()
    if many:
        return (
            DESCENDANTS
            + """ SELECT DISTINCT m.id FROM scope s
            CROSS JOIN world_member m INDEXED BY world_members_by_class ON m.cls=s.id
            CROSS JOIN world_link l ON (l.id,l.prop,l.target)=(m.id,?,?)
            CROSS JOIN item i ON i.id=m.id WHERE length(i.name) BETWEEN 1 AND 140""",
            (cls, prop, target),
        )
    return (
        DESCENDANTS
        + """ SELECT l.id FROM world_link l CROSS JOIN item i ON i.id=l.id
        WHERE l.prop=? AND l.target=? AND length(i.name) BETWEEN 1 AND 140 AND EXISTS(
        SELECT 1 FROM world_member m WHERE m.id=l.id AND m.cls IN (SELECT id FROM scope))""",
        (cls, prop, target),
    )


def identifiers(db, term, role):
    import re

    column, table, prefix = (
        ("cls", "world_class_name", "q") if role == "class" else ("target", "world_target_name", "q")
    )
    if re.fullmatch(prefix + r"[1-9]\d{0,11}", term):
        return [int(term[1:])]
    if role=="target" and (tagged:=re.fullmatch(r"(.+) q([1-9]\d{0,11})",term)):
        item=int(tagged[2])
        if db.execute("SELECT 1 FROM world_target_name WHERE term=? AND target=?",(tagged[1],item)).fetchone():
            return [item]
        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='current_target'").fetchone()
                and db.execute("""SELECT 1 FROM current_target t JOIN world_name n ON n.id=t.id
                    WHERE t.id=? AND n.term=?""",(item,tagged[1])).fetchone()):
            return [item]
    rows = db.execute(
        f"SELECT {column},max(preferred) AS best FROM {table} WHERE term=? GROUP BY {column} ORDER BY best DESC LIMIT 13", (term,)
    ).fetchall()
    if role == "target" and term.startswith("the ") and (not rows or not rows[0][1]):
        canonical = db.execute(
            "SELECT DISTINCT target FROM world_target_name WHERE term=? AND preferred=1 LIMIT 2", (term[4:],)
        ).fetchall()
        if len(canonical) == 1:
            return [canonical[0][0]]
    if not rows and role == "class" and term.endswith("s"):
        return identifiers(db, term[:-1], role)
    best = [row[0] for row in rows if row[1] == rows[0][1]]
    return best if len(best) < 13 else []


def info(db, cls, prop, target):
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='current_target'").fetchone():
        return db.execute("""SELECT c.name,r.name,coalesce(t.name,i.name) FROM item c CROSS JOIN predicate r
            LEFT JOIN current_target t ON t.id=? LEFT JOIN item i ON i.id=?
            WHERE c.id=? AND r.id=? AND coalesce(t.name,i.name) IS NOT NULL""",(target,target,cls,prop)).fetchone()
    row = db.execute(
        """SELECT c.name,r.name,t.name FROM item c,predicate r,item t
        WHERE c.id=? AND r.id=? AND t.id=?""",
        (cls, prop, target),
    ).fetchone()
    return row


def target_label(db, target):
    row = None
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='current_target'").fetchone():
        row = db.execute("SELECT name FROM current_target WHERE id=?", (target,)).fetchone()
    if not row:
        row = db.execute("SELECT name FROM item WHERE id=?", (target,)).fetchone()
    return row[0] if row else None


def display_subject(db, cls, label):
    """Prefer an attested, unambiguous alias over a parenthesised catalogue title."""
    import re
    if parts := re.fullmatch(r"(.+) \(([^()]+)\)",label):
        candidate=parts[2]+" "+parts[1].lower()
        if identifiers(db,folded_spelling(candidate),"class")==[cls]:
            return candidate
    return label


def current_members(db, key):
    row = db.execute("SELECT revision,captured,release FROM live_rule WHERE key=?", (key,)).fetchone()
    if row:
        return row[0], row[1], json.loads(gzip.decompress(row[2]))["rows"]


def joined_release(db, keys, query, args):
    """Resolve names from current operands when the combined rule has never been captured."""
    from graph import digest

    revisions, names, dates = [], {}, []
    for key in keys:
        capture = current_members(db, key)
        if not capture:
            continue
        revision, checked, rows = capture
        revisions.append((key, revision))
        dates.append(checked)
        for qid, name, aliases in rows:
            if qid in names:
                names[qid][1].update((name, *aliases))
            else:
                names[qid] = (name, set(aliases))
    if not revisions:
        return None
    result = []
    for (item,) in db.execute(query, args):
        qid = "Q" + str(item)
        name, aliases = names[qid]
        result.append((qid, name, sorted(aliases - {name})))
    return {"revision": digest(revisions), "captured": min(dates), "rows": result, "n": len(result)}


def suggestions(db, phrase, edition):
    """Find playable questions for targets too small to have a precomputed topic."""
    from collections import Counter

    from catalogue import world_card

    targets = identifiers(db, phrase, "target")
    if len(targets) != 1:
        return []
    (target,) = targets
    properties = db.execute(
        """SELECT p.id FROM predicate p WHERE EXISTS(
        SELECT 1 FROM world_link l WHERE (l.prop,l.target)=(p.id,?)) ORDER BY p.id""",
        (target,),
    )
    result = []
    for (prop,) in properties:
        # Sampling only selects a useful subject. The answer count and bank always
        # come from the complete indexed join, including indirect class membership.
        sample = db.execute(
            """SELECT m.cls FROM (SELECT id FROM world_link WHERE prop=? AND target=? LIMIT 64) l
            CROSS JOIN world_member m ON m.id=l.id""",
            (prop, target),
        )
        for cls, n in Counter(row[0] for row in sample).most_common(3):
            if n < 2:
                break
            labels = info(db, cls, prop, target)
            if not labels:
                continue
            card = world_card(
                db,
                {
                    "cls": cls,
                    "prop": prop,
                    "target": target,
                    "n": 0,
                    "subject": labels[0],
                    "relation": labels[1],
                    "object": labels[2],
                },
                edition,
            )
            if card["records"] >= 2 and len(card["prompt"]) <= 240:
                result.append(card)
                break
        if len(result) == 3:
            break
    return result
