# /// script
# requires-python = ">=3.11"
# dependencies = ["beautifulsoup4==4.14.3", "openpyxl==3.1.5", "wordfreq==3.1.1", "pymupdf==1.26.7"]
# ///
"""Build the catalogue or compare every released identity with the source projection."""

import argparse
import importlib

from common import PACKS, ROOT, json, packs, sources

OUT = ROOT / "data" / "packs"
DESCRIPTIONS = {
    "general": "From the periodic table and the Booker Prize to the Olympic programme and the cocktail bar.",
    "screen": "British telly, animation studios, film awards and the people on screen.",
    "music": "UK number ones, festival headliners, Eurovision, instruments and artists’ back catalogues.",
    "geography": "Countries, capitals, mountains and rivers, with a closer look at the UK.",
    "animals": "British birds, living mammals and the English names of animal families.",
    "ultimate": "22 playing and rules rounds, plus eight rounds on university, club and international competition.",
}


def output(path, value, check):
    if check:
        old = json.loads(path.read_text("utf8"))
        if old != value:
            if isinstance(value, dict) and "questions" in value:
                previous = {q["id"]: q for q in old["questions"]}
                for q in value["questions"]:
                    a = {r["name"]: r for r in q["answers"]}
                    b = {
                        r["name"]: r
                        for r in previous.get(q["id"], {}).get("answers", [])
                    }
                    if a != b:
                        print(
                            q["id"],
                            "missing:",
                            sorted(a.keys() - b.keys())[:12],
                            "unexpected:",
                            sorted(b.keys() - a.keys())[:12],
                            "changed:",
                            [k for k in a.keys() & b.keys() if a[k] != b[k]][:12],
                        )
            raise ValueError(f"Released snapshot differs: {path.name}")
    else:
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", "utf8", newline="\n"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    index = []
    outputs = {}
    # Published counts catch truncated pages and accidental merging of identities.
    census = {
        "general-elements": 118,
        "general-stars": 88,
        "general-paris-events": 329,
        "general-bible": 66,
        "general-prime-ministers": 58,
        "geography-un": 193,
        "geography-counties": 48,
        "geography-departments": 101,
        "geography-prefectures": 47,
        "geography-islands": 100,
        "geography-rivers": 100,
        "geography-states": 50,
        "animals-british-list": 636,
        "animals-bears": 8,
        "animals-monotremes": 5,
        "screen-bond": 25,
        "music-planets": 7,
        "ultimate-signals": 24,
        "ultimate-throws": 10,
        "ultimate-spirit": 5,
        "ultimate-chengdu": 8,
    }
    for key, title in PACKS.items():
        module = importlib.import_module(key)
        module.build()
        questions = packs[key]
        counts = [len(q["answers"]) for q in questions]
        assert (
            len(questions)
            == len({q["id"] for q in questions})
            == len({q["prompt"] for q in questions})
            == 30
        ), key
        for question in questions:
            if question["id"] in census:
                assert len(question["answers"]) == census[question["id"]], question[
                    "id"
                ]
        assert key == "ultimate" or sum(n >= 30 for n in counts) >= 20, (key, counts)
        print(
            f"{title}: {len(questions)} rounds, {sum(counts)} scoring answers, {sum(n >= 30 for n in counts)} large rounds",
            flush=True,
        )
        outputs[OUT / (key + ".json")] = {"title": title, "questions": questions}
        index.append(
            dict(
                id=key,
                title=title,
                count=len(questions),
                description=DESCRIPTIONS[key],
                answers=sum(counts),
            )
        )
    from vocabulary import build as build_vocabulary

    outputs[ROOT / "data" / "vocabulary.json"] = build_vocabulary()
    outputs[OUT / "sources.json"] = dict(sorted(sources.items()))
    outputs[OUT / "index.json"] = index
    for path, value in outputs.items():
        output(path, value, args.check)


if __name__ == "__main__":
    main()
