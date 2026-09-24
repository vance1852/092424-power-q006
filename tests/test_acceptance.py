from __future__ import annotations

import unittest
from pathlib import Path

from plant_science.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["observation_count"], 6)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["conclusion"], "pass")
        self.assertEqual(result["decision"], "approved")
        self.assertEqual(len(result["input_sha256"]), 64)
        self.assertEqual(result["rejected_imports"], 2)
        self.assertTrue(result["replay_consistent"])
        self.assertTrue(result["restart_recovered"])


if __name__ == "__main__":
    unittest.main()
