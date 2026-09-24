from __future__ import annotations

import copy
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

    def _import_state(self, batch_id: str = "batch-a") -> dict[str, int]:
        scope = f"observations:{batch_id}"
        return {
            "observations": self.connection.execute(
                "SELECT count(*) FROM observations WHERE batch_id=?", (batch_id,)
            ).fetchone()[0],
            "idempotency_keys": self.connection.execute(
                "SELECT count(*) FROM idempotency_keys WHERE scope=?", (scope,)
            ).fetchone()[0],
            "import_audits": self.connection.execute(
                "SELECT count(*) FROM audit_events WHERE entity_type='batch' AND entity_id=? "
                "AND event_type='observations.imported'",
                (batch_id,),
            ).fetchone()[0],
        }

    def test_invalid_row_leaves_no_observations_idempotency_or_audit(self) -> None:
        """复现事故：第三行时间格式与负值计数违约时，前三类状态必须保持干净。"""

        bad = copy.deepcopy(self.rows)
        bad[2] = copy.deepcopy(bad[2])
        bad[2]["observed_at"] = "2026/09/21 09:20"
        bad[2]["metrics"] = dict(bad[2]["metrics"])
        bad[2]["metrics"]["interventions"] = -1
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.import_observations("operator", "batch-a", "incident", bad)
        self.assertEqual(ctx.exception.details["index"], 2)
        self.assertEqual(ctx.exception.details["field"], "observations[2].metrics.interventions")
        self.assertEqual(ctx.exception.details["protocol_id"], "demo-delivery-v1")
        self.assertEqual(ctx.exception.details["protocol_version"], 1)
        self.assertEqual(
            self._import_state(),
            {"observations": 0, "idempotency_keys": 0, "import_audits": 0},
        )

        # 事故中无法重试的批次，修复后必须能用同一幂等键成功导入并稳定重放。
        first = self.service.import_observations("operator", "batch-a", "incident", self.rows)
        replay = self.service.import_observations("operator", "batch-a", "incident", self.rows)
        self.assertEqual(first, replay)
        self.assertEqual(first["inserted"], 6)

    def test_bad_timestamp_alone_is_rejected_with_field(self) -> None:
        bad = copy.deepcopy(self.rows)
        bad[2] = copy.deepcopy(bad[2])
        bad[2]["observed_at"] = "2026-09-21 09:20:00"  # 缺时区、分隔符不符 ISO-8601
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.import_observations("operator", "batch-a", "bad-time", bad)
        self.assertEqual(ctx.exception.details["field"], "observations[2].observed_at")
        self.assertEqual(ctx.exception.details["index"], 2)
        self.assertEqual(self._import_state()["observations"], 0)

    def test_duplicate_source_row_within_request_is_rejected_before_insert(self) -> None:
        duplicated = [copy.deepcopy(self.rows[0]), copy.deepcopy(self.rows[0])]
        with self.assertRaises(ValidationFailed) as ctx:
            self.service.import_observations("operator", "batch-a", "dup", duplicated)
        self.assertEqual(ctx.exception.details["field"], "observations[1].source_row")
        self.assertEqual(ctx.exception.details["source_row"], "001")
        self.assertEqual(self._import_state()["observations"], 0)

    def test_partial_failure_rolls_back_preceding_rows(self) -> None:
        partial = copy.deepcopy(self.rows[:3])
        partial[2] = copy.deepcopy(partial[2])
        partial[2]["metrics"] = dict(partial[2]["metrics"])
        partial[2]["metrics"]["interventions"] = -3
        with self.assertRaises(ValidationFailed):
            self.service.import_observations("operator", "batch-a", "partial", partial)
        self.assertEqual(
            self._import_state(),
            {"observations": 0, "idempotency_keys": 0, "import_audits": 0},
        )

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
        with self.assertRaises(Conflict):
            self.service.import_observations("operator", "batch-a", "key-2", self.rows[:2])
        count = self.connection.execute("SELECT count(*) FROM observations").fetchone()[0]
        self.assertEqual(count, 1)

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


class RestartRecoveryTests(unittest.TestCase):
    """用文件数据库验证进程重启后的状态恢复与合法批次重放。"""

    PROTOCOL_ID = "demo-delivery-v1"

    def setUp(self) -> None:
        self.protocol = load_json(ROOT / "fixtures" / "demo_protocol.json")
        self.rows = [
            json.loads(line)
            for line in (ROOT / "fixtures" / "demo_observations.jsonl")
            .read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.clock = FrozenClock(datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc))
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "restart.sqlite3"
        service = TrialService(connect(self.database), self.clock)
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
        service.create_batch("operator", "batch-a", self.PROTOCOL_ID, 1, "build-a")
        service.start_batch("operator", "batch-a", 1)
        service.connection.close()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _service(self) -> TrialService:
        return TrialService(connect(self.database), self.clock)

    def test_replay_and_queued_job_survive_restart(self) -> None:
        service = self._service()
        first = service.import_observations("operator", "batch-a", "key-1", self.rows)
        service.seal_batch("stat", "batch-a", 2)
        queued_job = service.connection.execute(
            "SELECT job_id FROM analysis_jobs WHERE batch_id='batch-a'"
        ).fetchone()["job_id"]
        service.connection.close()

        # 模拟进程重启：新连接、新服务实例重开同一数据库文件。
        recovered = self._service()
        self.assertEqual(
            recovered.import_observations("operator", "batch-a", "key-1", self.rows), first
        )
        claimed = recovered.claim_job("worker-restart", 30)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job_id"], queued_job)
        analysis = recovered.complete_job("worker-restart", claimed["job_id"], "stat")
        self.assertEqual(analysis["result"]["conclusion"], "pass")
        recovered.connection.close()

    def test_failed_import_stays_clean_across_restart_then_succeeds(self) -> None:
        bad = copy.deepcopy(self.rows)
        bad[2] = copy.deepcopy(bad[2])
        bad[2]["observed_at"] = "2026/09/21 09:20"
        bad[2]["metrics"] = dict(bad[2]["metrics"])
        bad[2]["metrics"]["interventions"] = -1

        service = self._service()
        with self.assertRaises(ValidationFailed):
            service.import_observations("operator", "batch-a", "incident", bad)
        service.connection.close()

        recovered = self._service()
        # 重启后事故批次仍干净：同一幂等键可重试合法数据，且重放稳定。
        first = recovered.import_observations("operator", "batch-a", "incident", self.rows)
        self.assertEqual(first["inserted"], 6)
        self.assertEqual(
            recovered.import_observations("operator", "batch-a", "incident", self.rows), first
        )
        recovered.connection.close()


if __name__ == "__main__":
    unittest.main()
