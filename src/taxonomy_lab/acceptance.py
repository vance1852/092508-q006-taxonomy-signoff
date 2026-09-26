"""完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .jsonio import load_json
from .service import TaxonomyLabService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    evidence_protocol = load_json(fixtures / "demo_evidence_protocol.json")
    evidence_item_rows = [
        json.loads(line)
        for line in (fixtures / "demo_evidence_items.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    with tempfile.TemporaryDirectory(prefix="device-reviews-") as temporary:
        database = Path(temporary) / "foundation.sqlite3"
        connection = connect(database)
        try:
            service = TaxonomyLabService(connection)
            service.create_user("operator-1", "测试操作员", "operator")
            service.create_user("stat-1", "统计负责人", "statistician")
            service.create_user("approver-1", "观察材料采信审批人", "approver")
            service.create_user("auditor-1", "审计人员", "auditor")
            service.create_user("reviewer-1", "初审人", "initial_reviewer")
            service.create_user("reviewer-2", "专科复核人", "specialist")
            service.create_user("reviewer-3", "终审人", "final_reviewer")
            service.register_device("operator-1", "scope-a", "A 型标本事件实验采集设备", "示例设备供应商")
            service.register_build("operator-1", "build-a1", "scope-a", "1.0.0", "a" * 64)
            service.publish_evidence_protocol("stat-1", evidence_protocol)
            service.create_batch("operator-1", "batch-demo", evidence_protocol["evidence_protocol_id"], evidence_protocol["version"], "build-a1")
            service.start_batch("operator-1", "batch-demo", 1)
            imported = service.import_evidence_items(
                "operator-1", "batch-demo", "demo-import-1", evidence_item_rows
            )
            service.seal_batch("stat-1", "batch-demo", 2)
            job = service.claim_job("worker-1", lease_seconds=60)
            if job is None:
                raise RuntimeError("未能领取分析任务")
            analysis = service.complete_job("worker-1", job["job_id"], "stat-1")
            decision_value = "approved" if analysis["result"]["conclusion"] == "pass" else "rejected"
            service.decide(
                "approver-1", "batch-demo", analysis["analysis_id"], decision_value, "离线验收决定"
            )
            report = service.report("auditor-1", "batch-demo")
            service.register_specimen("operator-1", "sp-001", "NM-2026-0001", "拟步甲属待定种")
            material = service.register_specimen_evidence(
                "operator-1", "sp-001", "material", "loan-2026-114", "外借形态相近标本 3 头"
            )
            analysis_evidence = service.register_specimen_evidence(
                "operator-1", "sp-001", "analysis_batch", "batch-demo", "形态测量与分子联合分析批次"
            )
            type_photo = service.register_specimen_evidence(
                "operator-1", "sp-001", "type_photo", "typephoto://nm/2026/0001", "模式照片一组"
            )
            draft = service.create_determination_draft(
                "operator-1", "sp-001", "Blaps confusa",
                "外生殖器形态与 COI 序列均支持该处理",
                [material["evidence_id"], analysis_evidence["evidence_id"], type_photo["evidence_id"]],
            )
            service.sign_determination("reviewer-1", draft["draft_id"], "初审通过")
            service.sign_determination("reviewer-2", draft["draft_id"], "专科复核通过")
            published = service.sign_determination("reviewer-3", draft["draft_id"], "终审通过，同意发布")
            current = service.current_determination("sp-001")
            service.register_specimen_evidence(
                "operator-1", "sp-001", "material", "loan-2026-118", "新到补充标本 2 头"
            )
            history = service.determination_history("auditor-1", "sp-001")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    if current["current"]["state"] != "published":
        raise RuntimeError("鉴定稿发布后应能通过当前结论接口读取")
    if history["drafts"][0]["state"] != "invalidated":
        raise RuntimeError("新增材料后已签版本应失效且保留历史")
    return {
        "status": "ok",
        "evidence_protocol": f"{evidence_protocol['evidence_protocol_id']}@{evidence_protocol['version']}",
        "evidence_item_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "event_count": len(report["events"]),
        "determination": {
            "specimen_id": "sp-001",
            "published_version": published["version_no"],
            "signoff_stages": [item["stage"] for item in published["signoffs"]],
            "state_after_new_material": history["drafts"][0]["state"],
            "history_versions": len(history["drafts"]),
        },
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
