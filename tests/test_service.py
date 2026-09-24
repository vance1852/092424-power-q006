from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from plant_science.clock import FrozenClock
from plant_science.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from plant_science.jsonio import load_json
from plant_science.service import TrialService
from plant_science.storage import connect


ROOT = Path(__file__).resolve().parents[1]


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.service = TrialService(self.connection, self.clock)
        for user_id, role in (
            ("operator", "operator"),
            ("stat", "statistician"),
            ("approver", "approver"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.service.register_robot("operator", "robot-a", "A 型", "厂商")
        self.service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
        self.service.publish_protocol("stat", self.protocol)
        self.service.create_batch("operator", "batch-a", "demo-delivery-v1", 1, "build-a")
        self.service.start_batch("operator", "batch-a", 1)

    def tearDown(self) -> None:
        self.connection.close()

    def test_complete_workflow(self) -> None:
        imported = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker", 30)
        analysis = self.service.complete_job("worker", job["job_id"], "stat")
        self.service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "满足规则")
        report = self.service.report("auditor", "batch-a")
        self.assertEqual(report["batch"]["state"], "decided")
        self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")

    def test_idempotent_replay_and_conflict(self) -> None:
        first = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        second = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(first, second)
        changed = [dict(item) for item in self.rows]
        changed[0] = dict(changed[0])
        changed[0]["metrics"] = dict(changed[0]["metrics"])
        changed[0]["metrics"]["completion_seconds"] = "99"
        with self.assertRaises(Conflict):
            self.service.import_observations("operator", "batch-a", "key-1", changed)
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 6)

    def test_import_rolls_back_when_one_source_row_duplicates(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows[:1])
        with self.assertRaises(Conflict) as caught:
            self.service.import_observations("operator", "batch-a", "key-2", self.rows[:2])
        self.assertIn("hall-a-20260921/001", str(caught.exception))
        self.assertEqual(caught.exception.details["duplicates"], ["hall-a-20260921/001"])
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 1)

    def _import_state(self) -> tuple[int, int, int]:
        return (
            self.connection.execute("SELECT count(*) FROM observations").fetchone()[0],
            self.connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0],
            self.connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
        )

    def _corrupted_rows(self) -> list[dict]:
        bad = [dict(item) for item in self.rows]
        bad[2] = dict(bad[2])
        bad[2]["observed_at"] = "2026年9月21日 09:20"
        bad[2]["metrics"] = {**bad[2]["metrics"], "interventions": -2}
        return bad

    def test_invalid_row_leaves_no_trace_and_key_stays_reusable(self) -> None:
        before = self._import_state()
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_observations("operator", "batch-a", "key-1", self._corrupted_rows())
        message = str(caught.exception)
        self.assertIn("observation.observed_at", message)
        self.assertIn("demo-delivery-v1@1", message)
        self.assertEqual(caught.exception.details["field"], "observation.observed_at")
        self.assertEqual(caught.exception.details["row"], 3)
        self.assertEqual(caught.exception.details["protocol"], "demo-delivery-v1@1")
        self.assertEqual(self._import_state(), before)
        # 同一幂等键修正重试成功，重放返回同一摘要
        imported = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(imported["inserted"], 6)
        replay = self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.assertEqual(replay, imported)

    def test_negative_count_is_rejected_without_pollution(self) -> None:
        bad = [dict(item) for item in self.rows]
        bad[1] = dict(bad[1])
        bad[1]["metrics"] = {**bad[1]["metrics"], "interventions": -1}
        before = self._import_state()
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_observations("operator", "batch-a", "key-1", bad)
        self.assertEqual(caught.exception.details["field"], "observation.metrics.interventions")
        self.assertEqual(caught.exception.details["row"], 2)
        self.assertEqual(self._import_state(), before)

    def test_duplicate_source_rows_within_request_are_rejected(self) -> None:
        before = self._import_state()
        with self.assertRaises(ValidationFailed) as caught:
            self.service.import_observations("operator", "batch-a", "key-1", [self.rows[0], self.rows[0]])
        self.assertIn("hall-a-20260921/001", str(caught.exception))
        self.assertEqual(caught.exception.details["field"], "observation.source_row")
        self.assertEqual(self._import_state(), before)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.seal_batch("operator", "batch-a", 2)
        with self.assertRaises(Forbidden):
            self.service.report("operator", "batch-a")

    def test_exclusion_review_and_revoke_leave_history(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations ORDER BY observation_id LIMIT 1"
        ).fetchone()[0]
        requested = self.service.request_exclusion("operator", observation_id, "现场记录失效")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "证据充分")
        self.assertEqual(reviewed["status"], "approved")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")
        events = self.connection.execute(
            "SELECT event_type FROM audit_events WHERE entity_type='observation' AND entity_id=? ORDER BY event_id",
            (str(observation_id),),
        ).fetchall()
        self.assertEqual([row[0] for row in events], ["exclusion.requested", "exclusion.revoked"])

    def test_failed_job_returns_to_queue_after_delay(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker-a", 10)
        failed = self.service.fail_job("worker-a", job["job_id"], "临时计算失败", retry_seconds=5)
        self.assertEqual(failed["state"], "queued")
        self.assertIsNone(self.service.claim_job("worker-b", 10))
        self.clock.advance(seconds=5)
        retried = self.service.claim_job("worker-b", 10)
        self.assertEqual(retried["job_id"], job["job_id"])
        self.assertEqual(retried["attempts"], 2)

    def test_lease_can_be_reclaimed_after_expiry(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        first = self.service.claim_job("worker-a", 10)
        self.clock.advance(seconds=11)
        second = self.service.claim_job("worker-b", 10)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(second["lease_owner"], "worker-b")
        with self.assertRaises(InvalidState):
            self.service.complete_job("worker-a", first["job_id"], "stat")

    def test_analysis_task_requires_statistician_and_lease_owner(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        self.service.seal_batch("stat", "batch-a", 2)
        job = self.service.claim_job("worker-a", 30)
        with self.assertRaises(Forbidden):
            self.service.complete_job("worker-a", job["job_id"], "operator")
        with self.assertRaises(InvalidState):
            self.service.fail_job("worker-b", job["job_id"], "越权失败")
        with self.assertRaises(InvalidState):
            self.service.complete_job("worker-b", job["job_id"], "stat")
        # 合法持有者仍可完成，权限检查没有破坏租约流程
        analysis = self.service.complete_job("worker-a", job["job_id"], "stat")
        self.assertIn("analysis_id", analysis)

    def test_exclusion_review_and_revoke_permission_isolation(self) -> None:
        self.service.import_observations("operator", "batch-a", "key-1", self.rows)
        observation_id = self.connection.execute(
            "SELECT observation_id FROM observations ORDER BY observation_id LIMIT 1"
        ).fetchone()[0]
        with self.assertRaises(Forbidden):
            self.service.request_exclusion("stat", observation_id, "统计负责人不能申请排除")
        requested = self.service.request_exclusion("operator", observation_id, "现场记录失效")
        with self.assertRaises(Forbidden):
            self.service.review_exclusion("operator", requested["exclusion_id"], True, "不能自审")
        with self.assertRaises(Forbidden):
            self.service.review_exclusion("approver", requested["exclusion_id"], True, "审批人无复核权")
        reviewed = self.service.review_exclusion("stat", requested["exclusion_id"], True, "证据充分")
        self.assertEqual(reviewed["status"], "approved")
        self.service.create_user("operator-2", "operator-2", "operator")
        with self.assertRaises(Forbidden):
            self.service.revoke_exclusion("operator-2", requested["exclusion_id"], "非本人申请")
        revoked = self.service.revoke_exclusion("operator", requested["exclusion_id"], "已找回原始记录")
        self.assertEqual(revoked["status"], "revoked")

    def test_restart_recovers_import_lease_and_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "restart.sqlite3"
            connection = connect(database)
            clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
            service = TrialService(connection, clock)
            for user_id, role in (
                ("operator", "operator"),
                ("stat", "statistician"),
                ("approver", "approver"),
                ("auditor", "auditor"),
            ):
                service.create_user(user_id, user_id, role)
            service.register_robot("operator", "robot-a", "A 型", "厂商")
            service.register_build("operator", "build-a", "robot-a", "1.0", "b" * 64)
            service.publish_protocol("stat", self.protocol)
            service.create_batch("operator", "batch-a", "demo-delivery-v1", 1, "build-a")
            service.start_batch("operator", "batch-a", 1)
            with self.assertRaises(ValidationFailed):
                service.import_observations("operator", "batch-a", "key-1", self._corrupted_rows())
            imported = service.import_observations("operator", "batch-a", "key-1", self.rows)
            service.seal_batch("stat", "batch-a", 2)
            job = service.claim_job("worker-a", 600)
            connection.close()
            # 进程重启：同一数据库文件重新建连
            connection = connect(database)
            service = TrialService(connection, clock)
            try:
                replay = service.import_observations("operator", "batch-a", "key-1", self.rows)
                self.assertEqual(replay, imported)
                count = connection.execute("SELECT count(*) FROM observations").fetchone()[0]
                self.assertEqual(count, 6)
                analysis = service.complete_job("worker-a", job["job_id"], "stat")
                service.decide("approver", "batch-a", analysis["analysis_id"], "approved", "重启后完成")
                report = service.report("auditor", "batch-a")
                self.assertEqual(report["batch"]["state"], "decided")
                self.assertEqual(report["analysis"]["result"]["conclusion"], "pass")
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
