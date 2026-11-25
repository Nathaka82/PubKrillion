"""Index measurement evidence and compile comparison rules in a standalone publication."""

import hashlib
import json
import math
import re
from decimal import Decimal
from pathlib import Path

from graph import ROOT, UNITS, folded_spelling

DEFAULT_EXPORT = ROOT / "data/knowledge/wikidata-quantities.sqlite"
SCHEMA = """
CREATE TABLE measure_value(property TEXT,id TEXT,low REAL,high REAL,PRIMARY KEY(property,id)) WITHOUT ROWID;
CREATE TABLE measure_extra(basis TEXT,property TEXT,id TEXT,low REAL,high REAL,PRIMARY KEY(basis,property,id)) WITHOUT ROWID;
CREATE TABLE measure_scope(id TEXT PRIMARY KEY,label TEXT,world INTEGER);
CREATE TABLE measure_dimension(class TEXT,property TEXT,PRIMARY KEY(class,property)) WITHOUT ROWID;
CREATE TABLE measure_name(role TEXT,term TEXT,id TEXT,preferred INTEGER,PRIMARY KEY(role,term,id)) WITHOUT ROWID;
CREATE TABLE measure_idea(class TEXT,property TEXT,ref TEXT,position INTEGER,PRIMARY KEY(class,property,position)) WITHOUT ROWID;
CREATE TABLE measure_ambiguous(term TEXT,id TEXT,display TEXT,preferred INTEGER,PRIMARY KEY(term,id,display)) WITHOUT ROWID;
CREATE INDEX world_members_by_class ON world_member(cls,id);
"""
WORLD_SOURCE = "world5m-measure-members"
COMMAND = re.compile(r"^(?:(?:please |can you )?(?:name|list|identify|give me))\s+", re.IGNORECASE)
COMPARE = re.compile(
    r"(.+?)\s+(?:(?:that|which|whose (?:length|mass))\s+)?(?:(?:is|weighs|weighing|measures|grows)\s+)?"
    r"((?:longer|shorter|bigger|larger|smaller|heavier|lighter|more|less) than|at least|at most|"
    r"no more than|no less than|over|under)\s+(.+)",
    re.IGNORECASE,
)
SCOPE = re.compile(
    r"\s+(?:(?:whose|with(?: an?)?)\s+(?:(?:reported|recorded)\s+)?maximum (length|mass)(?: is| of)?|"
    r"that can grow|which can grow|can grow|(?:that |which )?(?:is usually|is typically|typically measures|usually measures))\s+",
    re.IGNORECASE,
)
UNIT_NAMES = {
    name: unit
    for unit, names in {
        "mm": ("mm", "millimetre", "millimeter"),
        "cm": ("cm", "centimetre", "centimeter"),
        "m": ("m", "metre", "meter"),
        "km": ("km", "kilometre", "kilometer"),
        "g": ("g", "gram", "gramme"),
        "kg": ("kg", "kilogram", "kilogramme"),
        "in": ("in", "inch", "inches"),
        "ft": ("ft", "foot", "feet"),
        "yd": ("yd", "yard"),
        "mi": ("mi", "mile"),
        "oz": ("oz", "ounce"),
        "lb": ("lb", "pound"),
    }.items()
    for name in names
}
NUMERIC = re.compile(
    r"((?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)\s*([a-z]+)(?:\s+(long|in length|in mass))?", re.IGNORECASE
)
TEST_BOUNDS = {"gt": (">", "<="), "ge": (">=", "<"), "lt": ("<", ">="), "le": ("<=", ">")}


def export_signature(db, path):
    if path is None:
        return None
    with Path(path).open("rb") as stream:
        fingerprint = hashlib.file_digest(stream, "sha256").hexdigest()
    db.execute("ATTACH DATABASE ? AS quantities", (Path(path).resolve().as_uri() + "?mode=ro&immutable=1",))
    return fingerprint


def import_world_values(db, fingerprint):
    # Keep measurements attached to the original Wikidata IDs. Every fact has
    # its own property citation, separate from the historical class assertion.
    db.execute("""CREATE TEMP TABLE measured_world AS SELECT m.* FROM quantities.measure m
        CROSS JOIN item i ON i.id=m.id WHERE length(i.name) BETWEEN 1 AND 140""")
    classes = db.execute("SELECT revision,url FROM source WHERE id='wikidata5m-catalogue-input'").fetchone()
    db.execute("INSERT INTO source VALUES(?,?,?,unixepoch(),'{}')", (WORLD_SOURCE, *classes))
    for dimension, pid, revision in db.execute("SELECT dimension,pid,revision FROM quantities.release"):
        if (dimension, pid) not in (("length", "P2043"), ("mass", "P2067")):
            raise ValueError("Unknown Wikidata measurement property.")
        sid = "wikidata-measure-" + dimension
        db.execute(
            "INSERT INTO source VALUES(?,?,?,unixepoch(),'{}')",
            (sid, revision + "-" + fingerprint, "https://www.wikidata.org/wiki/Property:" + pid),
        )
        db.execute(
            """INSERT INTO fact SELECT ?, 'wd:Q'||id,dimension,low,high,'typical',
            'https://www.wikidata.org/wiki/Q'||id||'#'||? FROM measured_world WHERE dimension=?""",
            (sid, pid, dimension),
        )
    if db.execute("""SELECT 1 FROM measured_world WHERE low IS NULL OR high IS NULL OR low<0 OR high<low
                     OR abs(high)>1.7976931348623157e308 LIMIT 1""").fetchone():
        raise ValueError("A published Wikidata measurement has invalid bounds.")
    db.execute("CREATE TEMP TABLE measured_id(id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO measured_id SELECT DISTINCT id FROM measured_world")
    db.create_function(
        "aliases_json",
        1,
        lambda s: json.dumps([v for v in s.split("\t") if 0 < len(v) <= 140], ensure_ascii=False),
    )
    db.execute(
        """INSERT INTO entity SELECT ?, 'wd:Q'||i.id,i.name,aliases_json(i.aliases),NULL,
        'https://www.wikidata.org/wiki/Q'||i.id||'#P31' FROM measured_id m CROSS JOIN item i ON i.id=m.id""",
        (WORLD_SOURCE,),
    )
    db.execute(
        """INSERT INTO member SELECT ?, 'wd:Q'||w.cls,'wd:Q'||m.id FROM measured_id m
        CROSS JOIN world_member w ON w.id=m.id""",
        (WORLD_SOURCE,),
    )
    hierarchy = db.execute("SELECT id FROM source WHERE id='wikidata-current-taxonomy'").fetchone()
    db.execute("""INSERT OR IGNORE INTO parent SELECT ?,'wd:Q'||child,'wd:Q'||parent FROM world_parent""",
               ((hierarchy[0] if hierarchy else WORLD_SOURCE),))
    db.execute(
        """INSERT INTO graph_familiarity SELECT ?, 'wd:Q'||i.id,0,i.incoming FROM measured_id m
        CROSS JOIN item i ON i.id=m.id WHERE i.incoming>0""",
        (WORLD_SOURCE,),
    )


def compile_shared_names(db, has_world):
    db.execute("""CREATE TEMP TABLE names_to_check(term TEXT,id TEXT,display TEXT,preferred INTEGER,
        PRIMARY KEY(term,id,display)) WITHOUT ROWID""")

    def forms():
        for item, name, aliases in db.execute("SELECT id,name,aliases FROM entity"):
            yield folded_spelling(name), item, name, 1
            for alias in json.loads(aliases):
                yield folded_spelling(alias), item, alias, 0
        if has_world:
            # Include names from unmeasured items, including aliases shared only
            # between a specialist source and the general graph.
            extra = """WITH external_terms AS MATERIALIZED (SELECT DISTINCT term FROM entity_name),
                ids(id) AS (SELECT id FROM world_homonym UNION
                SELECT a.id FROM external_terms e CROSS JOIN world.entity_alias a ON a.term=e.term UNION
                SELECT t.id FROM external_terms e CROSS JOIN world.title t ON t.term=e.term)
                SELECT i.id,i.name,i.aliases FROM ids CROSS JOIN item i ON i.id=ids.id
                WHERE NOT EXISTS(SELECT 1 FROM measured_id m WHERE m.id=i.id)"""
            for item, name, aliases in db.execute(extra):
                for pos, alias in enumerate((name, *aliases.split("\t"))):
                    if alias and len(alias) <= 140:
                        yield folded_spelling(alias), "wd:Q" + str(item), alias, pos == 0

    db.executemany(
        """INSERT INTO names_to_check VALUES(?,?,?,?) ON CONFLICT(term,id,display)
                      DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
        forms(),
    )
    db.execute("""WITH shared AS MATERIALIZED (SELECT term FROM names_to_check GROUP BY term HAVING min(id)<>max(id))
        INSERT INTO measure_ambiguous SELECT n.* FROM shared s CROSS JOIN names_to_check n ON n.term=s.term""")
    db.execute("DROP TABLE names_to_check")


def compile_index(db, fingerprint):
    if fingerprint:
        import_world_values(db, fingerprint)
    # A lower bound can prove "longer than". It cannot supply an upper bound.
    db.execute("""INSERT INTO measure_value SELECT property,id,min(low),
        CASE WHEN min(basis='typical') THEN max(high) END FROM fact
        WHERE basis IN ('typical','typical-lower-bound') GROUP BY property,id""")
    db.execute("""INSERT INTO measure_extra SELECT 'record',property,id,max(low),
        CASE WHEN min(basis='record') THEN max(high) END FROM fact WHERE basis IN ('record','record-lower-bound')
        GROUP BY property,id""")
    db.execute("""INSERT INTO measure_extra SELECT 'maturity',property,id,min(low),NULL FROM fact
        WHERE basis='maturity-lower-bound' GROUP BY property,id""")
    db.execute("""WITH RECURSIVE scopes(k,p) AS (
        SELECT DISTINCT m.class,v.property FROM (SELECT property,id FROM measure_value UNION
            SELECT property,id FROM measure_extra) v CROSS JOIN member m ON m.id=v.id
        UNION SELECT parent.parent,s.p FROM scopes s JOIN parent ON parent.child=s.k)
        INSERT INTO measure_dimension SELECT k,p FROM scopes""")
    db.execute("""INSERT INTO measure_scope SELECT d.class,coalesce(i.name,d.class),i.id
        FROM (SELECT DISTINCT class FROM measure_dimension) d LEFT JOIN item i ON
        d.class='wd:Q'||i.id AND i.id=CAST(substr(d.class,5) AS INTEGER)""")

    def index(role, key, name, preferred):
        return role, folded_spelling(name), key, preferred

    def names():
        for key, name, wit in db.execute("SELECT id,label,world FROM measure_scope"):
            yield index("class", key, name, 1)
            yield index("class", key, key, 1)
            if wit:
                aliases = db.execute("SELECT aliases FROM item WHERE id=?", (wit,)).fetchone()[0]
                for alias in aliases.split("\t"):
                    yield index("class", key, alias, 0)
        for key, alias in db.execute(
            "SELECT DISTINCT d.key,d.alias FROM term_display d JOIN measure_scope s ON s.id=d.key"
        ):
            yield index("class", key, alias, 0)
        for key, name, aliases in db.execute("""SELECT e.id,e.name,e.aliases FROM
                (SELECT DISTINCT id FROM measure_value) v CROSS JOIN entity e ON e.id=v.id"""):
            yield index("reference", key, name, 1)
            yield index("reference", key, key, 1)
            for alias in json.loads(aliases):
                yield index("reference", key, alias, 0)
        for key, alias in db.execute("""SELECT DISTINCT d.key,d.alias FROM
                (SELECT DISTINCT id FROM measure_value) v CROSS JOIN term_display d ON d.key=v.id"""):
            yield index("reference", key, alias, 0)

    db.executemany(
        """INSERT INTO measure_name VALUES(?,?,?,?) ON CONFLICT(role,term,id)
                      DO UPDATE SET preferred=max(preferred,excluded.preferred)""",
        names(),
    )
    # Choose named references inside the distribution so both easier and
    # harder comparisons have evidence. Avoid precomputing every possible pair.
    db.execute("""WITH eligible AS MATERIALIZED (SELECT DISTINCT m.class,v.property,v.id,v.low,v.high
        FROM measure_value v CROSS JOIN member m ON m.id=v.id WHERE v.high IS NOT NULL),
        bands AS (SELECT *,ntile(5) OVER grp band,count(*) OVER (PARTITION BY class,property) n
        FROM eligible WINDOW grp AS (PARTITION BY class,property ORDER BY low,high,id)),
        ranked AS (SELECT b.*,row_number() OVER (PARTITION BY class,property ORDER BY
            coalesce(i.incoming,0) DESC,abs(band-3),length(b.id),b.id) pos FROM bands b
            LEFT JOIN item i ON i.id=CAST(substr(b.id,5) AS INTEGER) AND b.id='wd:Q'||i.id
            WHERE n>=4 AND band BETWEEN 2 AND 4)
        INSERT INTO measure_idea SELECT class,property,id,pos FROM ranked WHERE pos<=3""")
    if db.execute("SELECT 1 FROM measure_dimension LIMIT 1").fetchone():
        compile_shared_names(db, bool(fingerprint))


def resolve(db, role, phrase, dimension):
    term = folded_spelling(re.sub(r"^(?:an?|the)\s+", "", phrase.strip(), flags=re.IGNORECASE))
    lookup = (
        "JOIN measure_dimension v ON v.class=n.id"
        if role == "class"
        else "JOIN measure_value v ON v.id=n.id"
    )

    def find(spelling):
        return db.execute(
            f"""SELECT n.id,n.preferred FROM measure_name n {lookup}
            WHERE n.role=? AND n.term=? AND v.property=? ORDER BY n.preferred DESC,n.id LIMIT 3""",
            (role, spelling, dimension),
        ).fetchall()

    rows = find(term)
    if not rows and re.fullmatch(r"q[1-9]\d*", term):
        rows = find("wd:" + term)
    if not rows and role == "class" and term.endswith("s"):
        rows = find(term[:-1])
    if len(rows) > 1 and rows[0][1] == rows[1][1]:
        raise ValueError(
            "Several measured records match “"
            + phrase
            + "”. Specify a source name or ID: "
            + ", ".join(r[0] for r in rows)
        )
    return rows[0][0] if rows else None


CLASS_MEMBERS = """WITH RECURSIVE classes(k) AS (VALUES(?) UNION SELECT p.child FROM parent p JOIN classes c ON p.parent=c.k),
    members(id) AS (SELECT DISTINCT m.id FROM classes c JOIN member m ON m.class=c.k)"""


def classifications(db, cls, dimension, op, ref, threshold, mode="typical"):
    if threshold is None:
        bounds = db.execute(
            "SELECT low,high FROM measure_value WHERE property=? AND id=?", (dimension, ref)
        ).fetchone()
        if not bounds:
            return None
        low, high = bounds
    else:
        low = high = float(threshold)
    above = op in ("gt", "ge")
    first, second = TEST_BOUNDS[op]
    yes, no = (
        ("v.low" + first + "?", "v.high" + second + "?")
        if above
        else ("v.high" + first + "?", "v.low" + second + "?")
    )
    limits = (high, low) if above else (low, high)
    inclusive = op in ("ge", "le")
    params = (dimension,)
    join = "LEFT JOIN measure_value v ON (v.property,v.id)=(?,m.id)"
    if mode == "record":
        join = "LEFT JOIN measure_extra v ON (v.basis,v.property,v.id)=('record',?,m.id)"
    elif mode == "common-or-maturity":
        join += (
            " LEFT JOIN measure_extra a ON (a.basis,a.property,a.id)=('maturity',?,m.id) AND v.id IS NULL"
        )
        params = (dimension, dimension)
        yes, no = (
            yes.replace("v.low", "coalesce(v.low,a.low)"),
            no.replace("v.low", "coalesce(v.low,a.low)"),
        )
    unknown = "v.id IS NULL AND a.id IS NULL" if mode == "common-or-maturity" else "v.id IS NULL"
    query = (
        CLASS_MEMBERS
        + f""" SELECT m.id,CASE WHEN {unknown} THEN 'missing'
        WHEN m.id=? THEN ? WHEN {yes} THEN 'accepted' WHEN {no} THEN 'outside' ELSE 'borderline' END outcome
        FROM members m {join}"""
    )
    return query, (
        cls,
        ref if mode != "record" else None,
        "accepted" if inclusive else "outside",
        *limits,
        *params,
    )


def stats(db, cls, query, args):
    total, valid, outside = db.execute(
        f"""SELECT count(*),count(*) FILTER(WHERE outcome='accepted'),
        count(*) FILTER(WHERE outcome='outside') FROM ({query})""",
        args,
    ).fetchone()
    census = (
        db.execute(
            """SELECT r.source FROM register_rule r JOIN measure_scope s ON s.id=r.class
                           WHERE r.class=? AND r.n=? AND s.world IS NULL ORDER BY r.source LIMIT 1""",
            (cls, total),
        ).fetchone()
        if total == valid + outside
        else None
    )
    return valid, census[0] if census else None


def parse_spec(db, prompt):
    if not COMMAND.match(prompt):
        return None
    body = COMMAND.sub("", prompt, count=1).strip().rstrip(".?").strip()
    modifier = SCOPE.search(body)
    mode = "default"
    forced = None
    if modifier:
        phrase = modifier[0].lower()
        mode = "record" if ("maximum" in phrase or "can grow" in phrase) else "typical"
        forced = modifier[1].lower() if modifier[1] else None
        body = body[: modifier.start()] + " " + body[modifier.end() :]
    match = COMPARE.fullmatch(body)
    if not match:
        return None
    subject, word, target = match.groups()
    word = word.lower()
    op = (
        "ge"
        if word in ("at least", "no less than")
        else "le"
        if word in ("at most", "no more than")
        else "lt"
        if word in ("shorter than", "smaller than", "lighter than", "less than", "under")
        else "gt"
    )
    if modifier and "can grow" in modifier[0].lower() and op in ("lt", "le"):
        raise ValueError("Use a recorded maximum length or a typical length for a shorter-than comparison.")
    prefix = match[0][: match.start(3)]
    mass = word in ("heavier than", "lighter than") or re.search(
        r"\b(weighs|weighing|whose mass)\b", prefix, re.IGNORECASE
    )
    length = word in ("longer than", "shorter than") or re.search(r"\bwhose length\b", prefix, re.IGNORECASE)
    dimension = "mass" if mass else "length"
    if forced and ((mass and forced != "mass") or (length and forced != "length")):
        raise ValueError("The question describes two different measurement dimensions.")
    dimension = forced or dimension
    number = NUMERIC.fullmatch(target)
    threshold, ref = None, None
    if number:
        spelling = number[2].lower()
        unit = UNIT_NAMES.get(spelling) or UNIT_NAMES.get(spelling.removesuffix("s"))
        if not unit:
            return None
        dtype, multiplier = UNITS[unit]
        if not mass and not length and not forced:
            dimension = dtype
        if dimension != dtype or (number[3] and (number[3].lower() == "in mass") != (dtype == "mass")):
            raise ValueError("The unit and the requested measurement describe different quantities.")
        value = Decimal(number[1].replace(",", "")) * Decimal(str(multiplier))
        if not math.isfinite(float(value)) or (value and not float(value)):
            raise ValueError("The comparison threshold is outside the supported numeric range.")
        threshold = format(value.normalize(), "f")
    else:
        if word in (
            "at least",
            "at most",
            "no more than",
            "no less than",
            "over",
            "under",
            "more than",
            "less than",
        ) and not (mass or length or forced):
            return None
        ref = resolve(db, "reference", target, dimension)
        if not ref:
            return None
    cls = resolve(db, "class", subject, dimension)
    if not cls:
        return None
    spec = [cls, dimension, op, ref, threshold]
    fmt = db.execute("SELECT format FROM publication").fetchone()[0]
    if fmt >= 13:
        if mode == "default":
            has_maturity = db.execute(
                CLASS_MEMBERS
                + """ SELECT 1 FROM members m JOIN measure_extra e
                ON (e.id,e.property,e.basis)=(m.id,?,'maturity') LIMIT 1""",
                (cls, dimension),
            ).fetchone()
            mode = "common-or-maturity" if has_maturity else "typical"
        spec.append(mode)
    elif mode == "record":
        return None
    return spec


def parse(db, prompt):
    spec = parse_spec(db,prompt)
    if not spec:
        return None
    cls = spec[0]
    valid, census = stats(db, cls, *classifications(db, *spec))
    if not valid:
        raise ValueError("No indexed measurement proves that a member meets this comparison.")
    return {
        "key": "compare:" + json.dumps(spec, ensure_ascii=False, separators=(",", ":")),
        "prompt": prompt,
        "edition": db.execute("SELECT edition FROM publication").fetchone()[0],
        "records": valid,
        "complete": bool(census),
    }


def alternatives(db, card, complete_only):
    """Offer explicit measurement rules for an unqualified comparison in the host's draft picker."""
    spec = json.loads(card["key"].removeprefix("compare:"))
    if len(spec) != 6 or SCOPE.search(card["prompt"]) or spec[-1] != "common-or-maturity":
        return []
    cls, dimension, op, ref, threshold = spec[:5]
    name = db.execute("SELECT label FROM measure_scope WHERE id=?", (cls,)).fetchone()[0]
    article = "an " if name[:1].lower() in "aeiou" else "a "
    if ref:
        target = db.execute(
            "SELECT name FROM entity WHERE id=? ORDER BY source LIMIT 1", (ref,)
        ).fetchone()[0]
    else:
        target = threshold + (" cm" if dimension == "length" else " g")
    direction = (
        {"gt": "longer than", "lt": "shorter than"}
        if dimension == "length"
        else {"gt": "heavier than", "lt": "lighter than"}
    )
    operator = {"gt": "over", "lt": "under", "ge": "at least", "le": "at most"}[op]
    clauses = [
        "that is usually " + direction.get(op, operator),
        f"with a recorded maximum {dimension} {operator}",
    ]
    options = []
    for clause in clauses:
        prompt = f"Name {article}{name} {clause} {target}"
        if len(prompt) > 240:
            continue
        try:
            option = parse(db, prompt)
        except ValueError:
            continue
        if (
            option
            and option["key"] != card["key"]
            and (option["complete"] or not complete_only)
            and json.loads(option["key"].removeprefix("compare:"))[:5] == spec[:5]
        ):
            options.append(option)
    return options


def suggest(db, idea, complete_only):
    text = re.sub(
        r"^(?:(?:please |can you )?(?:create|make|give me) )?(?:a |an )?(?:(?:quiz|questions?|round|topic) (?:on|about) )?",
        "",
        idea.strip(),
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r" (?:for (?:a |an )?(?:mixed ages?(?: table)?|mixed age group|quiz night)|quiz questions|quiz)$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    def possibilities(dimension):
        cls = resolve(db, "class", text, dimension)
        if cls:
            for (ref,) in db.execute(
                "SELECT ref FROM measure_idea WHERE class=? AND property=? ORDER BY position",
                (cls, dimension),
            ):
                yield cls, ref
        ref = resolve(db, "reference", text, dimension)
        if ref:
            # Start with the item's own classes. Also try parent classes with a usable distribution.
            query = """WITH RECURSIVE lineage(k) AS (SELECT class FROM member WHERE id=?
                UNION SELECT p.parent FROM parent p JOIN lineage ON lineage.k=p.child)
                SELECT s.id FROM lineage l JOIN measure_scope s ON s.id=l.k
                JOIN measure_idea i ON (i.class,i.property,i.position)=(s.id,?,1)
                ORDER BY EXISTS(SELECT 1 FROM member m WHERE m.class=s.id AND m.id=?) DESC,
                length(s.label),s.label,s.id LIMIT 8"""
            for (scope,) in db.execute(query, (ref, dimension, ref)):
                yield scope, ref

    for dimension, words in (("length", ("longer", "shorter")), ("mass", ("heavier", "lighter"))):
        try:
            candidates = list(dict.fromkeys(possibilities(dimension)))
        except ValueError:
            continue
        for cls, ref in candidates:
            subject = db.execute("SELECT label FROM measure_scope WHERE id=?", (cls,)).fetchone()[0]
            subject = subject if subject.isupper() else subject[:1].lower() + subject[1:]
            article = "an" if subject[:1] in "aeiou" else "a"
            label = db.execute(
                "SELECT name FROM entity WHERE id=? ORDER BY source LIMIT 1", (ref,)
            ).fetchone()[0]
            for word in words:
                prompt = f"Name {article} {subject} {word} than {label}"
                if len(prompt) > 240:
                    continue
                try:
                    card = parse(db, prompt)
                except ValueError:
                    continue
                if card and card["records"] >= 2 and (card["complete"] or not complete_only):
                    return card


def play(db, game, key, prompt, *, relation=None):
    from catalogue import RuleWithdrawn
    from compiler import evaluate

    spec = relation.spec if relation else json.loads(key.removeprefix("compare:"))
    if not isinstance(spec, list) or len(spec) not in (5, 6):
        raise RuleWithdrawn("This measurement rule is unavailable.")
    cls, dimension, op, ref, threshold, *modes = spec
    mode = modes[0] if modes else "typical"
    if (
        not isinstance(cls, str)
        or dimension not in ("length", "mass")
        or op not in tuple(TEST_BOUNDS)
        or mode not in ("typical", "record", "common-or-maturity")
        or (ref is None) == (threshold is None)
        or (ref is not None and not isinstance(ref, str))
        or (
            threshold is not None
            and (
                not isinstance(threshold, str)
                or not re.fullmatch(r"\d+(?:\.\d+)?", threshold)
                or not math.isfinite(float(threshold))
            )
        )
    ):
        raise RuleWithdrawn("This measurement rule is unavailable.")
    scope = db.execute("SELECT label,world FROM measure_scope WHERE id=?", (cls,)).fetchone()
    query = classifications(db, cls, dimension, op, ref, threshold, mode) if scope else None
    if not query:
        raise RuleWithdrawn("The source measurements no longer support this comparison.")
    if relation:
        query = relation.decisions(*query)
    db.executescript("""DROP TABLE IF EXISTS temp.measure_decision;
        CREATE TEMP TABLE measure_decision(id TEXT PRIMARY KEY,outcome TEXT) WITHOUT ROWID;""")
    db.execute("INSERT INTO measure_decision " + query[0], query[1])
    db.execute("CREATE INDEX temp.decision_outcome ON measure_decision(outcome,id)")
    result = (relation.counts(db,"SELECT outcome FROM measure_decision",()) if relation else
              stats(db, cls, "SELECT outcome FROM measure_decision", ()))
    if not result[0]:
        raise RuleWithdrawn("The source measurements no longer support this comparison.")
    plan = {"class": cls, "property": dimension, "op": op, "catalogueKey": key}
    plan["measureMode"] = mode
    if threshold is None:
        plan["reference"] = ref
    else:
        plan["threshold"] = threshold
    if relation:
        plan["conditions"] = [relation.condition]
        plan["qualifierLabel"] = relation.description
        if relation.complete and result[1] == relation.condition["source"]:
            plan["measuredCensus"] = result[1]
    elif result[1]:
        plan["measuredCensus"] = result[1]
    missing = 0
    world_scope = scope["world"] if not (relation and relation.complete) else None
    if world_scope:
        # Unmeasured named class members contribute to coverage and alias
        # ambiguity even though their full name rows are not duplicated.
        db.execute("DROP TABLE IF EXISTS temp.unmeasured_world")
        db.execute("""CREATE TEMP TABLE unmeasured_world(id INTEGER PRIMARY KEY)""")
        db.execute(
            """WITH RECURSIVE classes(k) AS (VALUES(?) UNION SELECT p.child FROM parent p JOIN classes c ON p.parent=c.k)
            INSERT OR IGNORE INTO unmeasured_world SELECT w.id FROM classes c CROSS JOIN world_member w INDEXED BY world_members_by_class
            ON w.cls=CAST(substr(c.k,5) AS INTEGER) AND c.k='wd:Q'||w.cls CROSS JOIN item i ON i.id=w.id
            WHERE NOT EXISTS(SELECT 1 FROM measure_decision d WHERE d.id='wd:Q'||w.id)""",
            (cls,),
        )
        missing = db.execute("SELECT count(*) FROM unmeasured_world").fetchone()[0]

    def aliases():
        # Only excluded members sharing a search spelling with a qualified answer
        # can change adjudication. Look them up after the playable names are known.
        if db.execute("SELECT format FROM publication").fetchone()[0] >= 12:
            if relation:
                yield from relation.outside_names(db)
            unknown = (
                """ OR EXISTS(SELECT 1 FROM unmeasured_world u WHERE u.id=CAST(substr(a.id,5) AS INTEGER)
                AND a.id='wd:Q'||u.id)"""
                if world_scope
                else ""
            )
            yield from db.execute(f"""SELECT a.id,a.display,a.preferred FROM (SELECT DISTINCT folded FROM spellings) s
                CROSS JOIN measure_ambiguous a ON a.term=s.folded WHERE EXISTS(SELECT 1 FROM measure_decision d
                WHERE d.id=a.id AND d.outcome<>'accepted'){unknown}""")
            return
        colliding = """WITH other AS MATERIALIZED (SELECT DISTINCT n.id FROM (SELECT DISTINCT legacy FROM spellings) s
            CROSS JOIN entity_name n ON n.term=s.legacy CROSS JOIN measure_decision d ON d.id=n.id
            WHERE d.outcome<>'accepted') SELECT e.id,e.name,e.aliases FROM other o CROSS JOIN entity e ON e.id=o.id"""
        for item, name, forms in db.execute(colliding):
            for pos, form in enumerate((name, *json.loads(forms))):
                yield item, form, pos == 0
        if world_scope:
            rows = db.execute("""SELECT DISTINCT i.id,i.name,i.aliases FROM (SELECT DISTINCT legacy FROM spellings) s
                CROSS JOIN world_homonym h ON h.term=s.legacy CROSS JOIN unmeasured_world u ON u.id=h.id
                CROSS JOIN item i ON i.id=u.id""")
            for item, name, forms in rows:
                for pos, form in enumerate((name, *forms.split("\t"))):
                    yield "wd:Q" + str(item), form, pos == 0

    proof=None
    if relation and relation.condition.get("geo"):
        import places
        live=db.execute("SELECT 1 FROM live_rule WHERE key=?",(relation.key,)).fetchone()
        proof=places.proof_reader(db,[relation.key] if live else [])
    return evaluate(
        db, game, prompt, plan, outside_names=aliases(), name_edition=db.execute("SELECT edition FROM publication").fetchone()[0],
        missing_members=missing, measured_decisions=True,relationship_proof=proof
    )
