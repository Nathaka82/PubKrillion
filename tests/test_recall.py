import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "packs"))
from recall import apply_recall


class RecallTests(unittest.TestCase):
    def setUp(self):
        self.question = {
            "id": "demo",
            "prompt": "Name an entry.",
            "answers": [{"name": str(i), "depth": 3} for i in range(20)],
        }
        self.sample = {
            "participants": 500,
            "prompt": "Name an entry.",
            "counts": {str(i): 100 for i in range(20)},
        }

    def test_only_the_rare_tail_can_win(self):
        self.sample["counts"].update({"0": 0, "1": 3, "2": 490})
        apply_recall(self.question, self.sample)
        self.assertEqual([a["depth"] for a in self.question["answers"][:3]], [5, 4, 1])

    def test_equal_evidence_does_not_split_the_last_slot(self):
        self.sample["counts"].update({"0": 0, "1": 0})
        apply_recall(self.question, self.sample)
        self.assertFalse(any(a["depth"] == 5 for a in self.question["answers"]))

    def test_missing_coverage_or_different_prompt_is_rejected(self):
        for change in [
            {"participants": 20},
            {"prompt": "Another edition"},
            {"counts": {"0": 0}},
        ]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                apply_recall(self.question, {**self.sample, **change})
