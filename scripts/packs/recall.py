"""Calibrate a bank using independent, unprompted recall of its exact prompt."""

from math import sqrt


def apply_recall(question, sample):
    if not isinstance(sample, dict):
        raise ValueError(f"Expected a recall sample for {question['id']}")
    population, counts = sample.get("participants"), sample.get("counts")
    names = {a["name"] for a in question["answers"]}
    if (
        type(population) is not int
        or population < 200
        or not isinstance(counts, dict)
        or counts.keys() != names
        or sample.get("prompt") != question["prompt"]
        or any(type(n) is not int or not 0 <= n <= population for n in counts.values())
        or not any(counts.values())
    ):
        raise ValueError(
            f"Invalid recall sample for {question['id']}: check prompt, population and canonical counts"
        )

    # Wilson upper 95% bound: a small observed count alone cannot earn 100.
    def upper(n):
        p = n / population
        z2 = 1.96**2
        return (
            p
            + z2 / (2 * population)
            + 1.96 * sqrt((p * (1 - p) + z2 / (4 * population)) / population)
        ) / (1 + z2 / population)

    budget = max(1, len(names) // 20)
    groups = {}
    for answer in question["answers"]:
        n = counts[answer["name"]]
        bound = upper(n)
        prior = answer["depth"]
        answer["depth"] = (
            1 if bound >= 0.35 else 2 if bound >= 0.15 else 3 if bound >= 0.05 else 4
        )
        if bound < 0.02:
            groups.setdefault((n, -prior), []).append(answer)
    for _, tied in sorted(groups.items()):
        if len(tied) > budget:
            break
        for answer in tied:
            answer["depth"] = 5
        budget -= len(tied)
