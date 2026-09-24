"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import copy
import json
import tempfile
from pathlib import Path

from .errors import Conflict, ValidationFailed
from .jsonio import load_json
from .service import TrialService
from .storage import connect, inspect_schema


def _scope_counts(connection, batch_id: str) -> dict[str, int]:
    scope = f"observations:{batch_id}"
    return {
        "observations": connection.execute(
            "SELECT count(*) FROM observations WHERE batch_id=?", (batch_id,)
        ).fetchone()[0],
        "idempotency_keys": connection.execute(
            "SELECT count(*) FROM idempotency_keys WHERE scope=?", (scope,)
        ).fetchone()[0],
        "import_audits": connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_type='batch' AND entity_id=? "
            "AND event_type='observations.imported'",
            (batch_id,),
        ).fetchone()[0],
    }


def _guard_invalid_imports(service: TrialService, connection, observation_rows: list) -> dict[str, bool]:
    """重复来源行与部分失败都必须整体拒绝，不污染任何状态。"""

    service.create_batch("operator-1", "batch-guard", observation_rows[0]["protocol_id"], 1, "build-a1")
    service.start_batch("operator-1", "batch-guard", 1)

    assert _scope_counts(connection, "batch-guard") == {
        "observations": 0,
        "idempotency_keys": 0,
        "import_audits": 0,
    }

    # 先合法导入一条来源行。
    first = service.import_observations("operator-1", "batch-guard", "guard-1", observation_rows[:1])
    assert first["inserted"] == 1

    # 同一请求内重复来源行：必须在写入前拒绝，且保留已提交的那一条。
    duplicated = [copy.deepcopy(observation_rows[1]), copy.deepcopy(observation_rows[1])]
    try:
        service.import_observations("operator-1", "batch-guard", "guard-dup", duplicated)
        raise RuntimeError("请求内重复来源行未被拒绝")
    except ValidationFailed as exc:
        assert exc.details["field"].endswith("source_row")
        assert exc.details["protocol_version"] == observation_rows[0]["protocol_version"]
    assert _scope_counts(connection, "batch-guard")["observations"] == 1

    # 换新幂等键重放已提交的来源行：冲突但不写入第二个幂等键或审计事件。
    before = _scope_counts(connection, "batch-guard")
    try:
        service.import_observations("operator-1", "batch-guard", "guard-other", observation_rows[:2])
        raise RuntimeError("跨请求重复来源行未被拒绝")
    except Conflict:
        pass
    after = _scope_counts(connection, "batch-guard")
    assert after == before, f"重复来源行污染了状态: {before} -> {after}"

    # 部分失败：前两行合法、第三行时间格式与负值计数违约，整批回滚。
    partial = copy.deepcopy(observation_rows[1:4])
    partial[2] = copy.deepcopy(partial[2])
    partial[2]["observed_at"] = "2026/09/21 10:20"
    partial[2]["metrics"] = dict(partial[2]["metrics"])
    partial[2]["metrics"]["interventions"] = -1
    try:
        service.import_observations("operator-1", "batch-guard", "guard-partial", partial)
        raise RuntimeError("部分失败的批次未被整体拒绝")
    except ValidationFailed as exc:
        assert exc.details["index"] == 2
        assert exc.details["protocol_id"] == observation_rows[0]["protocol_id"]
        assert exc.details["protocol_version"] == observation_rows[0]["protocol_version"]
    assert _scope_counts(connection, "batch-guard") == before

    # 合法批次以同一幂等键重放，必须返回同一摘要。
    replay = service.import_observations("operator-1", "batch-guard", "guard-1", observation_rows[:1])
    assert replay == first
    return {"duplicate_rejected": True, "partial_failure_rejected": True, "guard_replay_match": replay == first}


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "demo_protocol.json")
    observation_rows = [
        json.loads(line)
        for line in (fixtures / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="robot-trials-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        service = TrialService(connection)
        service.create_user("operator-1", "测试操作员", "operator")
        service.create_user("stat-1", "统计负责人", "statistician")
        service.create_user("approver-1", "分析准入审批人", "approver")
        service.create_user("auditor-1", "审计人员", "auditor")
        service.register_robot("operator-1", "robot-a", "A 型人形传感器", "示例厂商")
        service.register_build("operator-1", "build-a1", "robot-a", "1.0.0", "a" * 64)
        service.publish_protocol("stat-1", protocol)
        service.create_batch("operator-1", "batch-demo", protocol["protocol_id"], protocol["version"], "build-a1")
        service.start_batch("operator-1", "batch-demo", 1)
        imported = service.import_observations(
            "operator-1", "batch-demo", "demo-import-1", observation_rows
        )

        # 准入护栏：重复来源行与部分失败零污染。
        guard = _guard_invalid_imports(service, connection, observation_rows)

        # 封存产生排队任务，随后模拟进程重启：关闭并以新服务重开同一数据库文件。
        service.seal_batch("stat-1", "batch-demo", 2)
        queued_job = connection.execute(
            "SELECT job_id FROM analysis_jobs WHERE batch_id='batch-demo'"
        ).fetchone()["job_id"]
        connection.close()

        restarted = connect(database)
        try:
            recovered = TrialService(restarted)
            replay = recovered.import_observations(
                "operator-1", "batch-demo", "demo-import-1", observation_rows
            )
            restart_replay_match = replay == imported
            job = recovered.claim_job("worker-1", lease_seconds=60)
            if job is None or job["job_id"] != queued_job:
                raise RuntimeError("重启后未能恢复排队的分析任务")
            analysis = recovered.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            recovered.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            report = recovered.report("auditor-1", "batch-demo")
            schema = inspect_schema(restarted)
        finally:
            restarted.close()
    if schema["missing_tables"] or schema["schema_version"] != "2":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "protocol": f"{protocol['protocol_id']}@{protocol['version']}",
        "observation_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "event_count": len(report["events"]),
        "restart_replay_match": restart_replay_match,
        "restart_job_recovered": job["job_id"] == queued_job,
        "schema": schema,
        **guard,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行校准数据基础工具的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
