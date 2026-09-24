"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .errors import ValidationFailed
from .jsonio import load_json
from .service import TrialService
from .storage import connect, inspect_schema


def _import_state(service: TrialService) -> dict[str, int]:
    """导入会触碰的三类状态计数，用于验证失败不留痕迹。"""

    connection = service.connection
    return {
        "observations": connection.execute("SELECT count(*) FROM observations").fetchone()[0],
        "idempotency_keys": connection.execute("SELECT count(*) FROM idempotency_keys").fetchone()[0],
        "audit_events": connection.execute("SELECT count(*) FROM audit_events").fetchone()[0],
    }


def _expect_rejection(
    service: TrialService,
    batch_id: str,
    key: str,
    rows: Sequence[Mapping[str, Any]],
    identity: str,
    field: str,
) -> None:
    """非法批次必须被整批拒绝，且错误指出字段与协议版本，状态不被污染。"""

    before = _import_state(service)
    try:
        service.import_observations("operator-1", batch_id, key, rows)
    except ValidationFailed as exc:
        if exc.details.get("protocol") != identity or exc.details.get("field") != field:
            raise RuntimeError(f"错误响应未指出字段与协议版本: {exc!r}") from exc
    else:
        raise RuntimeError("非法测点导入未被拒绝")
    if _import_state(service) != before:
        raise RuntimeError("非法导入污染了观测、幂等或审计状态")


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "demo_protocol.json")
    identity = f"{protocol['protocol_id']}@{protocol['version']}"
    observation_rows = [
        json.loads(line)
        for line in (fixtures / "demo_observations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="robot-trials-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
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
            # 部分失败：第三行时间格式与负值计数违反已发布协议，整批不得落库。
            bad_rows = [dict(row) for row in observation_rows]
            bad_rows[2] = dict(bad_rows[2])
            bad_rows[2]["observed_at"] = "2026年9月21日 09:20"
            bad_rows[2]["metrics"] = {**bad_rows[2]["metrics"], "interventions": -2}
            _expect_rejection(
                service, "batch-demo", "demo-import-1", bad_rows, identity, "observation.observed_at"
            )
            # 请求内重复来源行同样整批拒绝。
            _expect_rejection(
                service,
                "batch-demo",
                "demo-import-dup",
                [observation_rows[0], observation_rows[0]],
                identity,
                "observation.source_row",
            )
            # 修正后同一幂等键重试成功，合法批次重放返回同一摘要。
            imported = service.import_observations(
                "operator-1", "batch-demo", "demo-import-1", observation_rows
            )
            replay = service.import_observations("operator-1", "batch-demo", "demo-import-1", observation_rows)
            if replay != imported:
                raise RuntimeError("合法批次重放未返回同一摘要")
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
        finally:
            connection.close()
        # 模拟进程重启：重新打开同一 SQLite 文件，租约与幂等状态必须恢复。
        connection = connect(database)
        try:
            service = TrialService(connection)
            replayed = service.import_observations(
                "operator-1", "batch-demo", "demo-import-1", observation_rows
            )
            if replayed != imported:
                raise RuntimeError("进程重启后重放未返回同一摘要")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            report = service.report("auditor-1", "batch-demo")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "2":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "protocol": identity,
        "observation_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "event_count": len(report["events"]),
        "rejected_imports": 2,
        "replay_consistent": True,
        "restart_recovered": True,
        "schema": schema,
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
