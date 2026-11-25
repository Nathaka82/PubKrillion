"""Edition, winner and runner-up from the cited results for each competition."""

from common import add, record


def finalists(key, title, subject, source, editions, expected_years, aliases, common):
    assert [year for year, _, _ in editions] == expected_years
    rows = [
        record(name, *aliases.get(name, []))
        for _, winner, runner_up in editions
        for name in (winner, runner_up)
    ]
    first, last = expected_years[0], expected_years[-1]
    question = add(
        "general",
        key,
        title,
        f"Name {subject} between {first} and {last}, including winners and runners-up.",
        source,
        rows,
        f"Both sides of all {len(editions)} completed finals in this period.",
        common=common,
    )
    question["topic"] = "Sport"


def build():
    import json
    from pathlib import Path
    data = Path(__file__).resolve().parents[2] / "data/knowledge/finals.json"
    for src in json.loads(data.read_text("utf8")):
        finalists(src["key"], src["title"], src["subject"], src["url"], src["editions"],
                  src["expected"], src["aliases"], src["common"])
