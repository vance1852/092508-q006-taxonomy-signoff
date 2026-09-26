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
            service.create_user("reviewer-init", "初审人", "initial_reviewer")
            service.create_user("reviewer-spec", "专科复核人", "specialist_reviewer")
            service.create_user("reviewer-final", "终审人", "final_reviewer")
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

            # ---- 版本化鉴定稿：登记标本与三类证据，三级会签发布 ----
            service.register_specimen("operator-1", "sp-001", "BJ-ENT-0001", "coleoptera")
            service.register_specimen_evidence(
                "operator-1", "ev-init-1", "sp-001", "initial_identification",
                "初鉴形态记录", "0" * 64, "field-team-a")
            service.register_specimen_evidence(
                "operator-1", "ev-mol-1", "sp-001", "molecular_batch",
                "COI 分子分析批次", "1" * 64, "molecular-team-b",
                analysis_sha256=analysis["input_sha256"])
            service.register_specimen_evidence(
                "operator-1", "ev-type-1", "sp-001", "type_photograph",
                "模式标本照片", "2" * 64, "curator-team-c")
            for reviewer, stage in (
                ("reviewer-init", "initial_review"),
                ("reviewer-spec", "specialist_review"),
                ("reviewer-final", "final_review"),
            ):
                service.assign_reviewer("approver-1", reviewer, stage)
            draft = service.create_determination(
                "operator-1", "sp-001", "Carabus demoensis sp. nov.",
                "形态、分子与模式照片三项一致",
                ["ev-init-1", "ev-mol-1", "ev-type-1"], authorship="Li et al., 2026")
            version_id = draft["version"]["version_id"]
            service.sign_determination("reviewer-init", "sp-001", version_id, True, "初审通过")
            service.sign_determination("reviewer-spec", "sp-001", version_id, True, "专科复核通过")
            service.sign_determination("reviewer-final", "sp-001", version_id, True, "终审通过，发布")
            current = service.current_determination("sp-001")
            if current is None or current["version_no"] != 1:
                raise RuntimeError("鉴定稿未能发布为当前有效版本")

            # ---- 撤回被引用证据：已发布版本失效但历史保留 ----
            service.withdraw_specimen_evidence("operator-1", "ev-mol-1", "分子批次污染，原始结果撤回")
            invalidated = service.get_determination("sp-001", version_id)
            if invalidated["version"]["status"] != "invalidated":
                raise RuntimeError("撤回引用证据后已签版本未失效")
            if service.current_determination("sp-001") is not None:
                raise RuntimeError("版本失效后不应仍返回当前结论")

            # ---- 用新材料形成第 2 版并重新会签发布 ----
            service.register_specimen_evidence(
                "operator-1", "ev-mol-2", "sp-001", "molecular_batch",
                "重做 COI 分子分析批次", "3" * 64, "molecular-team-b")
            draft2 = service.create_determination(
                "operator-1", "sp-001", "Carabus demoensis sp. nov.",
                "更换分子批次后三项证据仍一致",
                ["ev-init-1", "ev-mol-2", "ev-type-1"], authorship="Li et al., 2026")
            version_id_2 = draft2["version"]["version_id"]
            service.sign_determination("reviewer-init", "sp-001", version_id_2, True, "初审通过")
            service.sign_determination("reviewer-spec", "sp-001", version_id_2, True, "专科复核通过")
            service.sign_determination("reviewer-final", "sp-001", version_id_2, True, "终审通过，发布")
            history = service.determination_history("auditor-1", "sp-001")
            current2 = service.current_determination("sp-001")
            if current2 is None or current2["version_no"] != 2:
                raise RuntimeError("第 2 版未能成为当前有效版本")
            if len(history["versions"]) != 2:
                raise RuntimeError("鉴定稿修订链必须保留全部历史版本")

            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "3":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "evidence_protocol": f"{evidence_protocol['evidence_protocol_id']}@{evidence_protocol['version']}",
        "evidence_item_count": imported["inserted"],
        "analysis_id": analysis["analysis_id"],
        "input_sha256": analysis["input_sha256"],
        "conclusion": analysis["result"]["conclusion"],
        "decision": report["decision"]["decision"],
        "event_count": len(report["events"]),
        "determination_versions": [
            item["version"]["status"] for item in history["versions"]
        ],
        "current_determination_version": current2["version_no"],
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
