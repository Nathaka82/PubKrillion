"""Combine a published relationship with a measured predicate on the same entity."""

import gzip
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal

import comparisons
from graph import digest, folded_spelling


@dataclass
class MeasuredRule:
    key: str
    spec: list
    condition: dict
    description: str
    world: tuple | None = None

    @property
    def complete(self):
        return self.key.startswith("v-")

    def decisions(self, query, args):
        absent = (
            "'outside'"
            if self.complete
            else "CASE WHEN d.outcome='outside' THEN 'outside' ELSE 'missing' END"
        )
        return (
            f"""SELECT d.id,CASE WHEN f.id IS NULL THEN {absent} ELSE d.outcome END AS outcome
            FROM ({query}) d LEFT JOIN filter_member f ON f.id=d.id""",
            args,
        )

    def counts(self, db, query, args):
        total, valid, unknown = db.execute(
            f"""SELECT count(*),count(*) FILTER(WHERE outcome='accepted'),
            count(*) FILTER(WHERE outcome IN ('missing','borderline')) FROM ({query})""",
            args,
        ).fetchone()
        if self.complete and not unknown:
            source = self.condition["source"]
            registered = db.execute(
                "SELECT n FROM register_rule WHERE class=? AND source=?", (self.spec[0], source)
            ).fetchone()
            if registered and total == registered[0]:
                return valid, source
        return valid, None

    def outside_names(self, db):
        """Freshly learned names can collide with previously unique local names."""
        if not self.world or not db.execute("SELECT 1 FROM live_rule WHERE key=?", (self.key,)).fetchone():
            return
        from world_classes import DESCENDANTS

        wanted = {r[0] for r in db.execute("SELECT DISTINCT folded FROM spellings")}
        if db.execute("SELECT format FROM publication").fetchone()[0] >= 18:
            from world_links import excluded_names

            included = (
                int(r[0][4:])
                for r in db.execute(
                    "SELECT id FROM measure_decision WHERE outcome='accepted' AND id GLOB 'wd:Q[0-9]*'"
                )
                if r[0][4:].isdigit()
            )
            yield from excluded_names(db, self.world[0], wanted, included,roles=bool(self.condition.get("geo")))
        else:
            rows = db.execute(
                DESCENDANTS
                + """ SELECT DISTINCT i.id,i.name,i.aliases FROM scope s
            CROSS JOIN world_member m INDEXED BY world_members_by_class ON m.cls=s.id CROSS JOIN item i ON i.id=m.id
            WHERE NOT EXISTS(SELECT 1 FROM measure_decision d WHERE d.id='wd:Q'||i.id AND d.outcome='accepted')""",
                (self.world[0],),
            )
            for item, name, aliases in rows:
                for pos, form in enumerate((name, *aliases.split("\t"))):
                    if folded_spelling(form) in wanted:
                        yield "wd:Q" + str(item), form, pos == 0
        rows = db.execute("""SELECT f.id,f.name,f.aliases FROM filter_member f WHERE NOT EXISTS(
                           SELECT 1 FROM measure_decision d WHERE d.id=f.id AND d.outcome='accepted')""")
        for item, name, aliases in rows:
            for pos, form in enumerate((name, *json.loads(aliases))):
                if folded_spelling(form) in wanted:
                    yield item, form, pos == 0

    @contextmanager
    def project(self, db):
        # These connection-local views expose source evidence to the ordinary
        # comparison compiler. The published database stays read-only.
        views = []

        def view(name, select):
            db.execute(f"CREATE TEMP VIEW {name} AS {select}")
            views.append(name)

        db.execute("""CREATE TEMP TABLE filter_member(id TEXT PRIMARY KEY,name TEXT,aliases TEXT,depth INTEGER,url TEXT)
                      WITHOUT ROWID""")
        try:
            if self.world:
                cls = self.world[0]
                live = db.execute(
                    "SELECT revision,captured,release FROM live_rule WHERE key=?", (self.key,)
                ).fetchone()
                historical = db.execute(
                    "SELECT revision,url,checked FROM main.source WHERE id='wikidata5m-catalogue-input'"
                ).fetchone()
                if live:
                    items = json.loads(gzip.decompress(live[2]))["rows"]
                    db.executemany(
                        """INSERT INTO filter_member VALUES(?,?,?,
                        CASE WHEN (SELECT incoming FROM item WHERE id=?)>=1000 THEN 1 ELSE 2 END,?)""",
                        (
                            (
                                "wd:" + qid,
                                name,
                                json.dumps(aliases),
                                int(qid[1:]),
                                "https://www.wikidata.org/wiki/" + qid + "#P31",
                            )
                            for qid, name, aliases in items
                        ),
                    )
                    source = (
                        "wikidata-query-" + digest(self.key),
                        digest([live[0], historical[0]]),
                        "https://www.wikidata.org/wiki/Q" + str(cls),
                        live[1],
                    )
                else:
                    if self.condition.get("geo"):
                        import places
                        query,args=places.members(*self.world)
                    elif db.execute("SELECT format FROM publication").fetchone()[0] >= 17:
                        from world_links import members as world_members

                        query, args = world_members(db, *self.world)
                    else:
                        query, args = (
                            "SELECT j.value id FROM pattern p,json_each(p.items) j WHERE p.cls=? AND p.prop=? AND p.target=?",
                            self.world,
                        )

                    def members():
                        names = "answer_item" if db.execute("SELECT format FROM publication").fetchone()[0] >= 33 else "item"
                        for item, name, aliases, incoming in db.execute(
                            f"""SELECT i.id,i.name,i.aliases,i.incoming FROM
                                ({query}) m JOIN {names} i ON i.id=m.id""",
                            args,
                        ):
                            yield (
                                "wd:Q" + str(item),
                                name,
                                json.dumps([a for a in aliases.split("\t") if 0 < len(a) <= 140]),
                                1 if incoming >= 1000 else 2,
                                f"https://www.wikidata.org/wiki/Q{item}#P31",
                            )

                    db.executemany("INSERT INTO filter_member VALUES(?,?,?,?,?)", members())
                    source = ("world5m-" + self.key, *historical)
                db.execute(
                    """CREATE TEMP TABLE filter_source AS SELECT ? id,? revision,? url,? checked,'{}' census,
                           ? cls,? relation,? target""",
                    (*source, self.spec[0], self.condition["relation"], self.condition["target"]),
                )
                self.condition["source"] = source[0]
                view(
                    "source",
                    "SELECT * FROM main.source UNION ALL SELECT id,revision,url,checked,census FROM filter_source",
                )
                typed = "SELECT s.id,s.cls,m.id FROM filter_source s, filter_member m"
                if not live and db.execute("SELECT format FROM publication").fetchone()[0] >= 17:
                    from world_classes import DESCENDANTS

                    db.execute("CREATE TEMP TABLE filter_scope(id INTEGER PRIMARY KEY)")
                    db.execute("INSERT INTO filter_scope " + DESCENDANTS + " SELECT id FROM scope", (cls,))
                    typed = """SELECT s.id,'wd:Q'||w.cls,f.id FROM filter_source s CROSS JOIN filter_member f
                        CROSS JOIN world_member w ON w.id=CAST(substr(f.id,5) AS INTEGER)
                        CROSS JOIN filter_scope c ON c.id=w.cls"""
                    if self.condition.get("geo"):
                        db.execute("""UPDATE filter_member AS f SET url=replace(url,'#P31','#P106')
                            WHERE NOT EXISTS(SELECT 1 FROM world_member w JOIN filter_scope c ON c.id=w.cls
                            WHERE w.id=CAST(substr(f.id,5) AS INTEGER))""")
                        typed += """ UNION ALL SELECT s.id,'wd:Q'||r.role,f.id FROM filter_source s
                            CROSS JOIN filter_scope c CROSS JOIN world_role r ON r.role=c.id
                            CROSS JOIN filter_member f ON f.id='wd:Q'||r.id WHERE f.url LIKE '%#P106'"""
                    hierarchy = db.execute(
                        "SELECT id FROM source WHERE id='wikidata-current-taxonomy'"
                    ).fetchone()
                    db.execute(
                        """CREATE TEMP TABLE filter_parent AS SELECT ? source,
                        'wd:Q'||p.child child,'wd:Q'||p.parent parent FROM filter_scope c CROSS JOIN world_parent p
                        ON p.parent=c.id WHERE NOT EXISTS(SELECT 1 FROM main.parent old
                        WHERE (old.child,old.parent)=('wd:Q'||p.child,'wd:Q'||p.parent))""",
                        (hierarchy[0] if hierarchy else "wikidata5m-catalogue-input",),
                    )
                    if db.execute("SELECT 1 FROM filter_parent LIMIT 1").fetchone():
                        view(
                            "parent",
                            "SELECT * FROM main.parent UNION ALL SELECT source,child,parent FROM filter_parent",
                        )
                view("member", "SELECT * FROM main.member UNION ALL " + typed)
                view(
                    "entity",
                    "SELECT * FROM main.entity UNION ALL SELECT s.id,m.id,m.name,m.aliases,m.depth,m.url FROM filter_member m,filter_source s",
                )
                view(
                    "edge",
                    "SELECT s.id AS source,m.id AS id,s.relation,s.target FROM filter_member m,filter_source s",
                )
            else:
                cls, rel, target = self.spec[0], self.condition["relation"], self.condition["target"]
                if self.complete:
                    source = self.condition["source"]
                    db.execute("CREATE TEMP TABLE filter_source AS SELECT ? id,? cls", (source, cls))
                    view(
                        "member",
                        "SELECT m.* FROM filter_source s JOIN main.member m ON (m.source,m.class)=(s.id,s.cls)",
                    )
                    db.execute(
                        """INSERT INTO filter_member(id) SELECT m.id FROM member m JOIN main.edge e ON
                        (e.source,e.id)=(m.source,m.id) WHERE e.relation=? AND e.target=?""",
                        (rel, target),
                    )
                else:
                    db.execute(
                        "INSERT INTO filter_member(id) "
                        + comparisons.CLASS_MEMBERS
                        + """ SELECT DISTINCT m.id FROM members m JOIN main.edge e ON e.id=m.id
                                   WHERE e.relation=? AND e.target=?""",
                        (cls, rel, target),
                    )
            yield
        finally:
            for name in reversed(views):
                db.execute(f"DROP VIEW temp.{name}")
            db.execute("DROP TABLE IF EXISTS temp.filter_source")
            db.execute("DROP TABLE IF EXISTS temp.filter_scope")
            db.execute("DROP TABLE IF EXISTS temp.filter_parent")
            db.execute("DROP TABLE temp.filter_member")


def rule(db, key, spec):
    if not isinstance(key, str) or not isinstance(spec, list) or len(spec) != 6:
        return None
    world = re.fullmatch(r"[gw]-([1-9]\d{0,11})-([1-9]\d{0,11})-([1-9]\d{0,11})", key)
    if world:
        cls, prop, target = map(int, world.groups())
        if spec[0] != "wd:Q" + str(cls):
            return None
        if key.startswith("g-"):
            import places
            c=places.condition(db,cls,prop,target)
            return MeasuredRule(key,spec,c,places.scope([c]),(cls,prop,target)) if c else None
        row = db.execute(
            "SELECT r.name,t.name FROM predicate r,item t WHERE r.id=? AND t.id=?", (prop, target)
        ).fetchone()
        if row:
            return MeasuredRule(
                key,
                spec,
                {"relation": row[0], "target": "wd:Q" + str(target), "label": row[1], "prop": prop},
                f"The source must list {row[1]} as the {row[0]}.",
                (cls, prop, target),
            )
    elif key.startswith(("v-", "f-")):
        table = "facet_rule" if key.startswith("v-") else "feed_pattern"
        row = db.execute(f"SELECT * FROM {table} WHERE key=? AND class=?", (key, spec[0])).fetchone()
        if row:
            label = db.execute(
                "SELECT name FROM main.entity WHERE id=? ORDER BY source LIMIT 1", (row["target"],)
            ).fetchone()
            condition = {
                "relation": row["relation"],
                "target": row["target"],
                "label": label[0] if label else row["target"],
            }
            if key.startswith("v-"):
                condition["source"] = row["source"]
            return MeasuredRule(
                key,
                spec,
                condition,
                f"The source must record “{condition['label']}” for “{condition['relation']}”.",
            )
    return None


def match(db, prompt):
    from catalogue import EmptyIntersection, match_single, rule_subject

    if not comparisons.COMPARE.search(prompt) or not comparisons.COMMAND.match(prompt):
        return None
    options = {}
    # Try both clause orders and consume every word. Names containing "and" remain intact.
    for split in re.finditer(r"\s+(?:and\s+)?", prompt, re.IGNORECASE):
        left, right = prompt[: split.start()], prompt[split.end() :]
        if not right:
            continue
        if comparisons.COMPARE.search(left):
            terms = comparisons.comparison_terms(left)
            if not terms or not (cls := comparisons.resolve(db, "class", terms.subject, terms.dimension)):
                continue
            name = db.execute("SELECT label FROM measure_scope WHERE id=?", (cls,)).fetchone()[0]
            card = match_single(db, "Name a " + name + " " + right)
            if not card or not card["key"].startswith(("w-", "g-", "v-", "f-")):
                continue
            # An ambiguous fragment of a longer reference is irrelevant unless the
            # rest of the prompt independently describes a relationship.
            spec = comparisons.resolve_spec(db, terms, cls)
        elif comparisons.COMPARE.fullmatch("subject " + right) or comparisons.SCOPE.search(
            " " + right + " "
        ):
            card = match_single(db, left)
            if not card or not card["key"].startswith(("w-", "g-", "v-", "f-")):
                continue
            spec = comparisons.parse_spec(db, "Name a " + rule_subject(db, card["key"]) + " " + right)
        else:
            continue
        if card and spec and (combined := rule(db, card["key"], spec)):
            key = "mixed:" + json.dumps([card["key"], spec], ensure_ascii=False, separators=(",", ":"))
            options[key] = combined
    if not options:
        return None
    if len(options) != 1:
        raise ValueError(
            "Several source rules match this comparison. Specify the relationship and the measurement."
        )
    key, combined = next(iter(options.items()))
    with combined.project(db):
        n, census = combined.counts(
            db, *combined.decisions(*comparisons.classifications(db, *combined.spec))
        )
    if not n:
        raise EmptyIntersection(key)
    return {
        "key": key,
        "prompt": prompt,
        "records": n,
        "complete": bool(census),
        "edition": db.execute("SELECT edition FROM publication").fetchone()[0],
    }


def play(db, game, key, prompt):
    from catalogue import RuleWithdrawn

    parts = json.loads(key[6:])
    combined = rule(db, *parts) if isinstance(parts, list) and len(parts) == 2 else None
    if not combined:
        raise RuleWithdrawn("The relationship or measurement is no longer available for this question.")
    with combined.project(db):
        return comparisons.play(db, game, key, prompt, relation=combined)


def alternatives(db, card, complete_only):
    """Choose thresholds from measurements of the actual relationship group."""
    key = card["key"]
    if not 3 <= card["records"] <= 4000 or (complete_only and not card["complete"]):
        return []
    if key.startswith(("w-","g-")):
        cls = "wd:Q" + key.split("-")[1]
    elif key.startswith(("f-", "v-")):
        table = "feed_pattern" if key.startswith("f-") else "facet_rule"
        cls = db.execute(f"SELECT class FROM {table} WHERE key=?", (key,)).fetchone()[0]
    else:
        return []
    dimensions = db.execute("SELECT property FROM measure_dimension WHERE class=?", (cls,)).fetchall()
    options = []
    for (dimension,) in dimensions:
        spec = [cls, dimension, "gt", None, "0", "typical"]
        group = rule(db, key, spec)
        with group.project(db):
            values = [
                v
                for (v,) in db.execute(
                    """SELECT v.low FROM filter_member m CROSS JOIN measure_value v
                ON (v.property,v.id)=(?,m.id) WHERE v.low>0 ORDER BY v.low""",
                    (dimension,),
                )
            ]
        if len(values) < 3:
            continue
        median = Decimal(str(values[(len(values) - 1) // 2]))
        units = (
            ((100000, "km"), (100, "m"), (1, "cm")) if dimension == "length" else ((1000, "kg"), (1, "g"))
        )
        scale, unit = next(((s, u) for s, u in units if median >= s), units[-1])
        number = median / scale
        number = number.quantize(Decimal(1).scaleb(number.adjusted()))
        phrase = "longer than" if dimension == "length" else "heavier than"
        prompt = f"{card['prompt'].rstrip('?')} {phrase} {number:f} {unit}"
        if len(prompt) > 240:
            continue
        try:
            option = match(db, prompt)
        except ValueError:
            continue
        if (
            option
            and 2 <= option["records"] < card["records"]
            and (option["complete"] or not complete_only)
        ):
            options.append(option)
    return options
