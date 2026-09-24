from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from pathlib import Path

from plant_science.api import JsonApplication
from plant_science.jsonio import load_json
from plant_science.service import TrialService


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.post("/robots", {"robot_id": "robot-a", "model_name": "A", "vendor": "厂"}, actor="operator")
        self.post(
            "/builds",
            {"build_id": "build-a", "robot_id": "robot-a", "version": "1.0", "content_sha256": "b" * 64},
            actor="operator",
        )
        self.post("/protocols", self.protocol, actor="stat")
        self.post(
            "/batches",
            {"batch_id": "batch-a", "protocol_id": "demo-delivery-v1", "protocol_version": 1, "build_id": "build-a"},
            actor="operator",
        )
        self.post("/batches/batch-a/start", {"expected_revision": 1}, actor="operator")

    def tearDown(self) -> None:
        self.connection.close()

    def post(self, path: str, payload: dict, *, actor: str | None = "operator", key: str | None = None):
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor-Id"] = actor
        if key is not None:
            headers["Idempotency-Key"] = key
        return self.app.handle(
            "POST", path, headers, json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        response = self.app.handle(
            "POST",
            "/users",
            body=json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode(),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")

    def test_invalid_row_error_names_field_and_protocol_version(self) -> None:
        bad = copy.deepcopy(self.rows)
        bad[2] = copy.deepcopy(bad[2])
        bad[2]["metrics"] = dict(bad[2]["metrics"])
        bad[2]["metrics"]["interventions"] = -1
        response = self.post("/batches/batch-a/observations", {"observations": bad}, key="incident")
        self.assertEqual(response.status, 422)
        error = response.body["error"]
        self.assertEqual(error["code"], "validation_failed")
        details = error["details"]
        self.assertEqual(details["field"], "observations[2].metrics.interventions")
        self.assertEqual(details["index"], 2)
        self.assertEqual(details["protocol_id"], "demo-delivery-v1")
        self.assertEqual(details["protocol_version"], 1)
        # 非法批次不得留下观测、幂等键或审计事件。
        self.assertEqual(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0], 0)
        self.assertEqual(
            self.connection.execute(
                "SELECT count(*) FROM audit_events WHERE event_type='observations.imported'"
            ).fetchone()[0],
            0,
        )

    def test_failed_batch_can_then_succeed_and_replay_identically(self) -> None:
        bad = copy.deepcopy(self.rows)
        bad[2] = copy.deepcopy(bad[2])
        bad[2]["observed_at"] = "2026/09/21 09:20"
        rejected = self.post("/batches/batch-a/observations", {"observations": bad}, key="incident")
        self.assertEqual(rejected.status, 422)
        first = self.post("/batches/batch-a/observations", {"observations": self.rows}, key="incident")
        replay = self.post("/batches/batch-a/observations", {"observations": self.rows}, key="incident")
        self.assertEqual(first.status, 200)
        self.assertEqual(replay.status, 200)
        self.assertEqual(first.body, replay.body)
        self.assertEqual(first.body["inserted"], 6)

    def test_import_permission_is_isolated(self) -> None:
        # 统计负责人不能导入测点；操作员不能封存批次。
        denied = self.post(
            "/batches/batch-a/observations", {"observations": self.rows}, actor="stat", key="k"
        )
        self.assertEqual(denied.status, 403)
        seal_denied = self.post("/batches/batch-a/seal", {"expected_revision": 2}, actor="operator")
        self.assertEqual(seal_denied.status, 403)
        self.assertEqual(self.connection.execute("SELECT count(*) FROM observations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
