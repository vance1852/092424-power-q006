from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from plant_science.api import JsonApplication, Response
from plant_science.jsonio import load_json
from plant_science.service import TrialService
from plant_science.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_user_route(self) -> None:
        payload = json.dumps({"user_id": "u1", "display_name": "操作员", "role": "operator"}).encode()
        response = self.app.handle("POST", "/users", body=payload)
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["role"], "operator")


class ImportApiTests(unittest.TestCase):
    """通过 HTTP 接口驱动完整准入流程，覆盖导入校验、权限隔离与重启恢复。"""

    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(TrialService(self.connection))
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self._prepare(self.app)

    def tearDown(self) -> None:
        self.connection.close()

    @staticmethod
    def _actor(user_id: str) -> dict[str, str]:
        return {"X-Actor-Id": user_id}

    def _prepare(self, app: JsonApplication) -> None:
        protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            response = app.handle(
                "POST", "/users",
                body=json.dumps({"user_id": user_id, "display_name": user_id, "role": role}).encode(),
            )
            assert response.status == 201, response.body
        response = app.handle(
            "POST", "/robots", self._actor("operator"),
            json.dumps({"robot_id": "robot-a", "model_name": "A 型", "vendor": "厂商"}).encode(),
        )
        assert response.status == 201, response.body
        response = app.handle(
            "POST", "/builds", self._actor("operator"),
            json.dumps({
                "build_id": "build-a", "robot_id": "robot-a", "version": "1.0",
                "content_sha256": "b" * 64,
            }).encode(),
        )
        assert response.status == 201, response.body
        response = app.handle("POST", "/protocols", self._actor("stat"), json.dumps(protocol).encode())
        assert response.status == 201, response.body
        response = app.handle(
            "POST", "/batches", self._actor("operator"),
            json.dumps({
                "batch_id": "batch-a", "protocol_id": "demo-delivery-v1",
                "protocol_version": 1, "build_id": "build-a",
            }).encode(),
        )
        assert response.status == 201, response.body
        response = app.handle(
            "POST", "/batches/batch-a/start", self._actor("operator"),
            json.dumps({"expected_revision": 1}).encode(),
        )
        assert response.status == 200, response.body

    def _import(self, app: JsonApplication, actor: str, key: str, rows: list) -> Response:
        return app.handle(
            "POST", "/batches/batch-a/observations",
            self._actor(actor) | {"Idempotency-Key": key},
            json.dumps({"observations": rows}).encode(),
        )

    def _corrupted_rows(self) -> list[dict]:
        bad = [dict(item) for item in self.rows]
        bad[2] = dict(bad[2])
        bad[2]["observed_at"] = "2026年9月21日 09:20"
        bad[2]["metrics"] = {**bad[2]["metrics"], "interventions": -2}
        return bad

    def _import_state(self) -> tuple[int, int, int]:
        return (
            self.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
            self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0],
            self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
        )

    def test_invalid_row_returns_field_and_protocol_and_leaves_no_trace(self) -> None:
        before = self._import_state()
        response = self._import(self.app, "operator", "key-1", self._corrupted_rows())
        self.assertEqual(response.status, 422)
        error = response.body["error"]
        self.assertEqual(error["code"], "validation_failed")
        self.assertIn("observation.observed_at", error["message"])
        self.assertIn("demo-delivery-v1@1", error["message"])
        self.assertEqual(error["details"]["field"], "observation.observed_at")
        self.assertEqual(error["details"]["protocol"], "demo-delivery-v1@1")
        self.assertEqual(error["details"]["row"], 3)
        self.assertEqual(self._import_state(), before)
        # 同一幂等键修正重试成功，重放返回同一摘要
        imported = self._import(self.app, "operator", "key-1", self.rows)
        self.assertEqual(imported.status, 200)
        self.assertEqual(imported.body["inserted"], 6)
        replay = self._import(self.app, "operator", "key-1", self.rows)
        self.assertEqual(replay.status, 200)
        self.assertEqual(replay.body, imported.body)

    def test_duplicate_source_rows_within_request_are_rejected(self) -> None:
        before = self._import_state()
        response = self._import(self.app, "operator", "key-1", [self.rows[0], self.rows[0]])
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["details"]["field"], "observation.source_row")
        self.assertEqual(self._import_state(), before)

    def test_duplicate_against_committed_rows_is_conflict(self) -> None:
        first = self._import(self.app, "operator", "key-1", self.rows[:1])
        self.assertEqual(first.status, 200)
        response = self._import(self.app, "operator", "key-2", self.rows[:2])
        self.assertEqual(response.status, 409)
        self.assertEqual(response.body["error"]["code"], "conflict")
        self.assertEqual(response.body["error"]["details"]["duplicates"], ["hall-a-20260921/001"])
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 1)

    def test_permission_isolation_for_import_seal_and_report(self) -> None:
        forbidden = self._import(self.app, "stat", "key-1", self.rows)
        self.assertEqual(forbidden.status, 403)
        self.assertEqual(forbidden.body["error"]["code"], "forbidden")
        seal = self.app.handle(
            "POST", "/batches/batch-a/seal", self._actor("operator"),
            json.dumps({"expected_revision": 2}).encode(),
        )
        self.assertEqual(seal.status, 403)
        report = self.app.handle("GET", "/batches/batch-a/report", self._actor("operator"))
        self.assertEqual(report.status, 403)
        missing_actor = self.app.handle(
            "POST", "/batches/batch-a/observations",
            {"Idempotency-Key": "key-1"}, json.dumps({"observations": self.rows}).encode(),
        )
        self.assertEqual(missing_actor.status, 422)
        missing_key = self.app.handle(
            "POST", "/batches/batch-a/observations",
            self._actor("operator"), json.dumps({"observations": self.rows}).encode(),
        )
        self.assertEqual(missing_key.status, 422)
        # 权限拦截同样不留下任何导入痕迹
        self.assertEqual(self._import_state(), (0, 0, 5))

    def test_restart_recovers_idempotency_and_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "api-restart.sqlite3"
            first_connection = connect(database)
            first_app = JsonApplication(TrialService(first_connection))
            self._prepare(first_app)
            imported = self._import(first_app, "operator", "key-1", self.rows)
            self.assertEqual(imported.status, 200)
            sealed = first_app.handle(
                "POST", "/batches/batch-a/seal", self._actor("stat"),
                json.dumps({"expected_revision": 2}).encode(),
            )
            self.assertEqual(sealed.status, 200)
            claimed = first_app.handle(
                "POST", "/jobs/claim", body=json.dumps({"worker_id": "worker-1", "lease_seconds": 600}).encode()
            )
            self.assertEqual(claimed.status, 200)
            job_id = claimed.body["job"]["job_id"]
            first_connection.close()
            # 进程重启：同一数据库文件上的新应用实例
            second_connection = connect(database)
            try:
                second_app = JsonApplication(TrialService(second_connection))
                replay = self._import(second_app, "operator", "key-1", self.rows)
                self.assertEqual(replay.status, 200)
                self.assertEqual(replay.body, imported.body)
                completed = second_app.handle(
                    "POST", f"/jobs/{job_id}/complete", self._actor("stat"),
                    json.dumps({"worker_id": "worker-1"}).encode(),
                )
                self.assertEqual(completed.status, 200)
                self.assertEqual(completed.body["result"]["conclusion"], "pass")
                report = second_app.handle("GET", "/batches/batch-a/report", self._actor("auditor"))
                self.assertEqual(report.status, 200)
                self.assertEqual(report.body["batch"]["state"], "analyzed")
            finally:
                second_connection.close()


if __name__ == "__main__":
    unittest.main()
