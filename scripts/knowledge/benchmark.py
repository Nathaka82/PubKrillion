"""Evaluate catalogue-backed host suggestions against a local QALD reference file."""

import argparse
import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

import catalogue
from graph import ROOT
from ideas import published_suggestions


def questions(dataset):
    item = re.compile(r"https?://www\.wikidata\.org/entity/(Q[1-9]\d*)")
    for entry in dataset["questions"]:
        answers = entry.get("answers", [])
        if len(answers) != 1 or len(variables := answers[0].get("head", {}).get("vars", [])) != 1:
            continue
        rows = [row.get(variables[0], {}) for row in answers[0].get("results", {}).get("bindings", [])]
        if any(row.get("type") != "uri" or not item.fullmatch(row.get("value", "")) for row in rows):
            continue
        expected = {row["value"].rsplit("/", 1)[1] for row in rows}
        prompt = next((q["string"] for q in entry["question"] if q["language"] == "en"), None)
        if prompt and len(expected) >= 2:
            yield entry["id"], prompt, expected


def bank_members(game, bank):
    members = set()
    name_count = 0
    for owner, evidence in game.execute("SELECT entity,evidence FROM kb_member WHERE bank=?", (bank,)):
        name_count += 1
        members.add(owner.removeprefix("wd:"))
        # Several qualifying items can share one playable name. Their original
        # membership records survive in the evidence for the merged display row.
        for fact in json.loads(evidence):
            if "classes" in fact and "entity" in fact:
                members.add(fact["entity"].removeprefix("wd:"))
    return members, name_count


def compare(db, game, prompt, reference):
    request = {"idea": prompt, "direct": False, "completeOnly": False}
    try:
        cards = published_suggestions(db, request)
    except ValueError as error:
        return {"status": "unavailable", "reason": str(error)}
    if not cards:
        return {"status": "unavailable"}
    if len(cards) != 1 or request.get("sourceNote"):
        return {"status": "qualification", "offered": [c["prompt"] for c in cards]}
    card, = cards
    # Compile the offered bank before scoring. The suggestion's displayed count
    # alone cannot establish whether it contains any of the reference answers.
    try:
        bank, _ = catalogue.play(db, game, card["key"], card["prompt"])
    except ValueError as error:
        return {"status": "compile_error", "reason": str(error)}
    members, names = bank_members(game, bank)
    return {"status": "compiled", "prompt": card["prompt"], "rule": card["key"], "members": len(members), "names": names,
            "exact": members == reference, "missing": sorted(reference - members), "extra": sorted(members - reference)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="QALD JSON file with Wikidata answer bindings")
    parser.add_argument("--catalogue", type=Path, default=ROOT / "storage/knowledge-catalogue.sqlite")
    parser.add_argument("--details", action="store_true", help="print each question's result")
    args = parser.parse_args()
    counts = Counter()
    with closing(catalogue.reader(args.catalogue)) as db, closing(sqlite3.connect(":memory:")) as game:
        edition = db.execute("SELECT edition FROM publication").fetchone()[0]
        game.executescript((ROOT / "server/knowledge.sql").read_text())
        for qid, prompt, expected in questions(json.loads(args.dataset.read_bytes())):
            result = compare(db, game, prompt, expected)
            counts["questions"] += 1
            counts[result["status"]] += 1
            if result["status"] == "compiled":
                counts["multi_answer"] += result["names"] >= 2
                counts["exact"] += result["exact"]
                counts["contains_all_reference"] += not result["missing"]
            if args.details:
                print(json.dumps({"id": qid, "question": prompt, **result}))
    if not counts["questions"]:
        parser.error("The dataset has no English questions with two distinct Wikidata item answers.")
    print(json.dumps({"edition": edition, **counts, "exact_percent": 100 * counts["exact"] / counts["questions"]}))


if __name__ == "__main__":
    main()
