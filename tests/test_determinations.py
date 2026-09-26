from __future__ import annotations

import sqlite3
import unittest

from taxonomy_lab.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    PublicationBlocked,
    ValidationFailed,
)
from taxonomy_lab.service import TaxonomyLabService


def sha(seed: int) -> str:
    return f"{seed:x}".rjust(64, "0")


class DeterminationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection)
        for user_id, role in (
            ("operator", "operator"),
            ("approver", "approver"),
            ("auditor", "auditor"),
            ("rev-init", "initial_reviewer"),
            ("rev-spec", "specialist_reviewer"),
            ("rev-final", "final_reviewer"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_specimen("operator", "sp-1", "C-001", "coleoptera")
        self.service.register_specimen("operator", "sp-2", "C-002", "coleoptera")
        for evidence_id, kind in (
            ("ev-init", "initial_identification"),
            ("ev-mol", "molecular_batch"),
            ("ev-type", "type_photograph"),
        ):
            self.service.register_specimen_evidence(
                "operator", evidence_id, "sp-1", kind, kind, sha(len(evidence_id)), "team"
            )
        for reviewer, stage in (
            ("rev-init", "initial_review"),
            ("rev-spec", "specialist_review"),
            ("rev-final", "final_review"),
        ):
            self.service.assign_reviewer("approver", reviewer, stage)

    def tearDown(self) -> None:
        self.connection.close()

    def _create(self, name: str = "Carabus testensis"):
        return self.service.create_determination(
            "operator", "sp-1", name, "三类证据一致",
            ["ev-init", "ev-mol", "ev-type"], authorship="Li, 2026",
        )

    def _sign_all(self, version_id: int) -> None:
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        self.service.sign_determination("rev-spec", "sp-1", version_id, True, "专科")
        self.service.sign_determination("rev-final", "sp-1", version_id, True, "终审")

    # ---- 发布与查询 ----

    def test_full_sequential_signoff_publishes(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self._sign_all(version_id)
        current = self.service.current_determination("sp-1")
        self.assertIsNotNone(current)
        self.assertEqual(current["version_no"], 1)
        self.assertEqual(current["scientific_name"], "Carabus testensis")
        self.assertEqual(len(current["evidence_refs"]), 3)

    def test_signoff_must_follow_stage_order(self) -> None:
        version_id = self._create()["version"]["version_id"]
        with self.assertRaises(InvalidState):
            self.service.sign_determination("rev-spec", "sp-1", version_id, True, "越级专科")
        with self.assertRaises(InvalidState):
            self.service.sign_determination("rev-final", "sp-1", version_id, True, "越级终审")

    def test_unassigned_reviewer_cannot_sign(self) -> None:
        version_id = self._create()["version"]["version_id"]
        # 已指派的初审可以签
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        # 未指派的同角色不能签专科环节
        self.service.create_user("rev-spec-2", "二号专科", "specialist_reviewer")
        with self.assertRaises(Forbidden):
            self.service.sign_determination("rev-spec-2", "sp-1", version_id, True, "未指派")

    def test_duplicate_signoff_does_not_create_second_decision(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        with self.assertRaises(Conflict):
            self.service.sign_determination("rev-init", "sp-1", version_id, True, "重复初审")
        rows = self.connection.execute(
            "SELECT count(*) FROM determination_signoffs WHERE version_id=? AND stage='initial_review'",
            (version_id,),
        ).fetchone()[0]
        self.assertEqual(rows, 1)

    def test_rejection_terminates_draft_and_keeps_history(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        self.service.sign_determination("rev-spec", "sp-1", version_id, False, "特征不符，驳回")
        detail = self.service.get_determination("sp-1", version_id)
        self.assertEqual(detail["version"]["status"], "rejected")
        # 驳回后可以另起新版本
        second = self._create()
        self.assertEqual(second["version"]["version_no"], 2)
        history = self.service.determination_history("auditor", "sp-1")
        self.assertEqual([v["version"]["version_no"] for v in history["versions"]], [1, 2])

    # ---- 发布阻断 ----

    def test_evidence_gap_blocks_publication(self) -> None:
        draft = self.service.create_determination(
            "operator", "sp-1", "Carabus gappensis", "仅有形态与照片", ["ev-init", "ev-type"]
        )
        version_id = draft["version"]["version_id"]
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        self.service.sign_determination("rev-spec", "sp-1", version_id, True, "专科")
        with self.assertRaises(PublicationBlocked) as caught:
            self.service.sign_determination("rev-final", "sp-1", version_id, True, "终审")
        self.assertTrue(any("分子" in reason for reason in caught.exception.reasons))
        # 阻断必须回滚：终审签署不落库，版本仍是 draft
        detail = self.service.get_determination("sp-1", version_id)
        self.assertEqual(detail["version"]["status"], "draft")
        self.assertNotIn("final_review", {s["stage"] for s in detail["signoffs"]})
        self.assertIsNone(self.service.current_determination("sp-1"))

    def test_conflict_of_interest_blocks_signoff(self) -> None:
        self.service.declare_reviewer_conflict(
            "approver", "rev-init", "sp-1", "初审人参与过该标本采集"
        )
        version_id = self._create()["version"]["version_id"]
        with self.assertRaises(Forbidden):
            self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        # 其他标本不受影响
        self.service.register_specimen_evidence(
            "operator", "i2", "sp-2", "initial_identification", "i", sha(2), "t")
        self.service.register_specimen_evidence(
            "operator", "m2", "sp-2", "molecular_batch", "m", sha(3), "t")
        self.service.register_specimen_evidence(
            "operator", "t2", "sp-2", "type_photograph", "t", sha(4), "t")
        draft2 = self.service.create_determination(
            "operator", "sp-2", "Carabus other", "x", ["i2", "m2", "t2"]
        )
        self.service.sign_determination(
            "rev-init", "sp-2", draft2["version"]["version_id"], True, "无关联可初审")

    def test_scientific_name_conflict_blocks_publication(self) -> None:
        first = self._create("Carabus controversa")
        self._sign_all(first["version"]["version_id"])
        # 第二个标本试图占用同一学名
        self.service.register_specimen_evidence(
            "operator", "i2", "sp-2", "initial_identification", "i", sha(5), "t")
        self.service.register_specimen_evidence(
            "operator", "m2", "sp-2", "molecular_batch", "m", sha(6), "t")
        self.service.register_specimen_evidence(
            "operator", "t2", "sp-2", "type_photograph", "t", sha(7), "t")
        second = self.service.create_determination(
            "operator", "sp-2", "Carabus controversa", "重名", ["i2", "m2", "t2"]
        )
        version_id_2 = second["version"]["version_id"]
        self.service.sign_determination("rev-init", "sp-2", version_id_2, True, "初审")
        self.service.sign_determination("rev-spec", "sp-2", version_id_2, True, "专科")
        with self.assertRaises(PublicationBlocked) as caught:
            self.service.sign_determination("rev-final", "sp-2", version_id_2, True, "终审")
        self.assertTrue(any("学名冲突" in reason for reason in caught.exception.reasons))

    def test_withdrawn_reference_blocks_future_publication(self) -> None:
        draft = self._create()
        version_id = draft["version"]["version_id"]
        self.service.sign_determination("rev-init", "sp-1", version_id, True, "初审")
        self.service.withdraw_specimen_evidence("operator", "ev-mol", "污染撤回")
        detail = self.service.get_determination("sp-1", version_id)
        self.assertEqual(detail["version"]["status"], "invalidated")
        self.assertTrue(detail["version"]["invalidation_reason"])
        # 失效版本不能继续会签
        with self.assertRaises(InvalidState):
            self.service.sign_determination("rev-spec", "sp-1", version_id, True, "专科")
        # 引用快照保留且标记撤回时状态（快照本身 captured_status 仍为 active，证据表现态可查）
        refs = self.service.determination_history("auditor", "sp-1")["versions"][0]["evidence_refs"]
        self.assertEqual({r["evidence_id"] for r in refs}, {"ev-init", "ev-mol", "ev-type"})

    # ---- 失效与历史 ----

    def test_withdrawing_cited_evidence_invalidates_published_but_keeps_history(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self._sign_all(version_id)
        self.service.withdraw_specimen_evidence("operator", "ev-type", "模式照片来源存疑撤回")
        self.assertIsNone(self.service.current_determination("sp-1"))
        history = self.service.determination_history("auditor", "sp-1")
        self.assertEqual(history["versions"][0]["version"]["status"], "invalidated")
        # 旧结论内容、引用与签署人全部可追溯
        version = history["versions"][0]
        self.assertEqual(len(version["evidence_refs"]), 3)
        self.assertEqual(
            {s["stage"] for s in version["signoffs"]},
            {"initial_review", "specialist_review", "final_review"},
        )

    def test_adding_material_invalidates_published_version(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self._sign_all(version_id)
        result = self.service.register_specimen_evidence(
            "operator", "ev-extra", "sp-1", "type_photograph", "补充的新照片", sha(9), "team"
        )
        self.assertEqual(result["invalidated_versions"], [version_id])
        self.assertIsNone(self.service.current_determination("sp-1"))

    def test_new_version_supersedes_previous_current(self) -> None:
        first = self._create()["version"]["version_id"]
        self._sign_all(first)
        # 新增材料使 v1 失效，再用全部当前证据出 v2
        self.service.register_specimen_evidence(
            "operator", "ev-mol-b", "sp-1", "molecular_batch", "新分子批", sha(10), "team")
        second = self.service.create_determination(
            "operator", "sp-1", "Carabus testensis", "复核", ["ev-init", "ev-mol-b", "ev-type"]
        )
        self.assertEqual(second["version"]["version_no"], 2)
        self._sign_all(second["version"]["version_id"])
        current = self.service.current_determination("sp-1")
        self.assertEqual(current["version_no"], 2)
        statuses = {
            v["version"]["version_no"]: v["version"]["status"]
            for v in self.service.determination_history("auditor", "sp-1")["versions"]
        }
        self.assertEqual(statuses[1], "invalidated")
        self.assertEqual(statuses[2], "published")

    # ---- 并发约束 ----

    def test_parallel_submissions_yield_single_current_version(self) -> None:
        # 同一标本同时只能有一个草稿
        self._create()
        with self.assertRaises(Conflict):
            self._create("Carabus parallelus")

    def test_concurrent_draft_submissions_serialize_to_one(self) -> None:
        import tempfile
        import threading
        from pathlib import Path
        from taxonomy_lab.storage import connect

        # 准备共享数据（文件库），随后每线程使用独立连接真正并发提交
        with tempfile.TemporaryDirectory() as directory:
            setup_connection = connect(Path(directory) / "concurrency.sqlite3")
            setup = TaxonomyLabService(setup_connection)
            for user_id, role in (("operator", "operator"),):
                setup.create_user(user_id, user_id, role)
            setup.register_specimen("operator", "sp-x", "C-X", "coleoptera")
            for evidence_id, kind, seed in (
                ("ev-init", "initial_identification", 1),
                ("ev-mol", "molecular_batch", 2),
                ("ev-type", "type_photograph", 3),
            ):
                setup.register_specimen_evidence(
                    "operator", evidence_id, "sp-x", kind, kind, sha(seed), "team")
            setup_connection.close()

            errors: list[BaseException] = []

            def submit(index: int) -> None:
                connection = connect(Path(directory) / "concurrency.sqlite3")
                try:
                    service = TaxonomyLabService(connection)
                    service.create_determination(
                        "operator", "sp-x", f"Carabus concurrens-{index}", "并行提交",
                        ["ev-init", "ev-mol", "ev-type"])
                except BaseException as exc:  # noqa: BLE001
                    errors.append(exc)
                finally:
                    connection.close()

            threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            check_connection = connect(Path(directory) / "concurrency.sqlite3")
            drafts = check_connection.execute(
                "SELECT count(*) FROM determination_versions WHERE specimen_id='sp-x' AND status='draft'"
            ).fetchone()[0]
            check_connection.close()
        self.assertEqual(drafts, 1)
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(isinstance(exc, Conflict) for exc in errors))

    def test_only_one_published_version_enforced(self) -> None:
        # 直接验证部分唯一索引：同一标本不可能同时存在两个 published
        first = self._create()["version"]["version_id"]
        self._sign_all(first)
        self.connection.execute(
            "INSERT INTO determination_versions(specimen_id,version_no,scientific_name,rationale,"
            "content_sha256,status,created_by,created_at) "
            "VALUES('sp-1',99,'X','y',?,'draft','operator','2026-09-26T00:00:00Z')",
            (sha(99),),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE determination_versions SET status='published' WHERE version_no=99"
            )

    # ---- 引用与输入校验 ----

    def test_cannot_reference_foreign_or_withdrawn_or_duplicate_evidence(self) -> None:
        self.service.register_specimen_evidence(
            "operator", "m2", "sp-2", "molecular_batch", "m", sha(12), "t")
        with self.assertRaises(Exception):
            self.service.create_determination(
                "operator", "sp-1", "X", "y", ["ev-init", "m2", "ev-type"])
        with self.assertRaises(ValidationFailed):
            self.service.create_determination(
                "operator", "sp-1", "X", "y", ["ev-init", "ev-init", "ev-mol", "ev-type"])
        self.service.withdraw_specimen_evidence("operator", "ev-mol", "撤回")
        with self.assertRaises(InvalidState):
            self.service.create_determination(
                "operator", "sp-1", "X", "y", ["ev-init", "ev-mol", "ev-type"])

    def test_research_history_requires_permission_but_current_is_public(self) -> None:
        version_id = self._create()["version"]["version_id"]
        self._sign_all(version_id)
        # 公共当前结论不需要登录角色
        self.assertIsNotNone(self.service.current_determination("sp-1"))
        # operator 没有研究端读权限
        with self.assertRaises(Forbidden):
            self.service.determination_history("operator", "sp-1")
        history = self.service.determination_history("auditor", "sp-1")
        self.assertEqual(history["versions"][0]["signoffs"][0]["reviewer_id"], "rev-init")


if __name__ == "__main__":
    unittest.main()
