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
        self.assertTrue(result["duplicate_rejected"])
        self.assertTrue(result["partial_failure_rejected"])
        self.assertTrue(result["guard_replay_match"])
        self.assertTrue(result["restart_replay_match"])
        self.assertTrue(result["restart_job_recovered"])


if __name__ == "__main__":
    unittest.main()
