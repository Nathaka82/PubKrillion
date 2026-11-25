"""Verify the facts for a dated relationship directly in the public graph."""

import re
import time

from class_updates import page_names
from date_rules import date_detail
from date_updates import PREFIX, parse_row
from date_updates import body as date_body
from graph import digest
from places import collect_paths, expression, matching_paths
from world_facts import bindings


def capture(rule, *, size=1000):
    target = f"wd:Q{rule.target}"
    direct = rule.mode == "w"
    if direct:
        related = f"""?item wdt:P{rule.prop} {target}; p:P{rule.prop} ?edge .
            ?edge a wikibase:BestRank; <http://www.wikidata.org/prop/statement/P{rule.prop}> {target} ."""
    else:
        related = f"?item {expression(rule.prop)} {target} ."
        if rule.prop == 131:
            related += f" FILTER(?item!={target})"
    scope = related + " " + date_body(rule.date)
    fields = "?s ?item ?date ?precision" + (" ?edge" if direct else "")

    preferred_order = {}

    def read_page(filter_by, suffix):
        # The two equivalent query shapes give the public services different
        # join orders for broad classes. Both require exactly the same evidence.
        preferred = preferred_order.get(filter_by, False)
        for narrow_first in (preferred, not preferred):
            where = filter_by + " " + date_body(rule.date, narrow_first=narrow_first)
            try:
                rows = bindings(PREFIX + f"SELECT DISTINCT {fields} WHERE {{ {where} }}" + suffix)
                preferred_order[filter_by] = narrow_first
                return rows
            except (OSError, ValueError):
                if narrow_first != preferred:
                    raise

    filter_by = related
    forward = False
    if not direct:
        # A reverse path from a whole country can span millions of entities. When
        # the dated group fits one page, verify geography from its members instead.
        sample = read_page("", f" LIMIT {size + 1}")
        forward = len(sample) <= size
        if forward:
            filter_by = ""

    def fetch_facts(initial=None):
        # Unordered results are sufficient for small intersections. If the first
        # page fills, restart with a stable order and compare two complete passes.
        batch = initial if initial is not None else read_page(filter_by, f" LIMIT {size + 1}")
        paged = len(batch) > size
        facts = set()
        offset = 0
        while True:
            if paged:
                order = " ORDER BY ?s" + (" ?edge" if direct else "")
                batch = read_page(filter_by, order + f" LIMIT {size} OFFSET {offset}")
            for row in batch:
                guid, item, _, value, precision = parse_row(row, rule.date)
                edge = ""
                if direct:
                    match = re.fullmatch(
                        r"https?://www\.wikidata\.org/entity/statement/([Qq]([1-9]\d*)-[a-zA-Z0-9-]+)",
                        row["edge"]["value"],
                    )
                    if not match or int(match[2]) != item:
                        raise ValueError("The relationship statement belongs to a different entity.")
                    edge = match[1]
                fact = item, guid, value, precision, edge
                if fact in facts:
                    raise ValueError("The source repeated a dated relationship result.")
                facts.add(fact)
            if not paged or len(batch) < size:
                return facts
            offset += len(batch)

    facts = fetch_facts(sample if forward else None)
    proofs = {}
    if forward:
        dated_facts = facts
        all_ids = sorted({row[0] for row in facts})
        found, proofs[rule.link] = matching_paths(all_ids, rule.prop, rule.target, bindings)
        facts = {row for row in facts if row[0] in found}
    members = sorted({r[0] for r in facts})
    entities = [{"item": {"value": f"http://www.wikidata.org/entity/Q{i}"}} for i in members]
    rows = sorted([qid, name, sorted(aliases)] for qid, name, aliases, _ in page_names(entities))
    named = {row[0] for row in rows}
    if len(named) != len(rows) or not named <= {f"Q{i}" for i in members} or (members and not named):
        raise ValueError("The source could not identify the members of this dated relationship.")
    if forward:
        count = bindings(
            PREFIX
            + "SELECT (COUNT(DISTINCT ?s) AS ?n) (COUNT(DISTINCT ?item) AS ?members) WHERE { "
            + date_body(rule.date, narrow_first=True)
            + " }"
        )
        totals = (len(dated_facts), len(all_ids))
        if len(count) != 1 or tuple(int(count[0][field]["value"]) for field in ("n", "members")) != totals:
            raise ValueError("The complete date group could not be verified for the location check.")
        replay = fetch_facts()
        if replay != dated_facts or matching_paths(all_ids, rule.prop, rule.target, bindings) != (
            found,
            proofs[rule.link],
        ):
            raise ValueError("The date or location records changed during the source check.")
    else:
        count = bindings(PREFIX + f"SELECT (COUNT(DISTINCT ?item) AS ?n) WHERE {{ {scope} }}")
        if len(count) != 1 or int(count[0]["n"]["value"]) != len(members) or fetch_facts() != facts:
            raise ValueError("The source facts changed during the dated relationship check.")
    if not direct and not forward:
        proofs[rule.link] = collect_paths([int(qid[1:]) for qid in named], rule.prop, rule.target, bindings)
    dates, links = {}, {}
    for item, guid, value, precision, edge in sorted(facts):
        eid = f"wd:Q{item}"
        dates.setdefault(eid, {})[guid] = date_detail(guid, value, precision)
        if edge:
            links.setdefault(eid, set()).add(edge)
    dates = {eid: list(records.values()) for eid, records in dates.items()}
    statements = {eid: sorted(guids) for eid, guids in links.items()}
    scanned = len(members)
    revision = digest([rule.key, rows, scanned, {}, proofs, dates, statements])
    return {
        "key": rule.key,
        "revision": revision,
        "captured": int(time.time()),
        "rows": rows,
        "scanned": scanned,
        "areas": {},
        "proofs": proofs,
        "dates": dates,
        "statements": statements,
    }
