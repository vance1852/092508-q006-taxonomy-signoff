from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from taxonomy_lab.clock import FrozenClock
from taxonomy_lab.errors import Conflict, Forbidden, InvalidState, PublishBlocked, ValidationFailed
from taxonomy_lab.service import TaxonomyLabService


class DeterminationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = TaxonomyLabService(self.connection, self.clock)
        for user_id, role in (
            ("op", "operator"),
            ("rev1", "initial_reviewer"),
            ("rev2", "specialist"),
            ("rev3", "final_reviewer"),
            ("rev4", "final_reviewer"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_specimen("op", "sp-1", "CAT-0001", "拟步甲")
        self.e_material = self.service.register_specimen_evidence(
            "op", "sp-1", "material", "loan-1", "外借标本 3 头"
        )["evidence_id"]
        self.e_analysis = self.service.register_specimen_evidence(
            "op", "sp-1", "analysis_batch", "batch-1", "形态测量分析批次"
        )["evidence_id"]
        self.e_photo = self.service.register_specimen_evidence(
            "op", "sp-1", "type_photo", "photo-1", "模式照片"
        )["evidence_id"]

    def tearDown(self) -> None:
        self.connection.close()

    def _draft(self, name="Blaps confusa", basis="形态与分子证据一致", evidence=None, disclosures=()):
        return self.service.create_determination_draft(
            "op", "sp-1", name, basis,
            evidence if evidence is not None else [self.e_material, self.e_analysis, self.e_photo],
            disclosures,
        )

    def _sign_all(self, draft_id: int):
        self.service.sign_determination("rev1", draft_id, "初审通过")
        self.service.sign_determination("rev2", draft_id, "专科复核通过")
        return self.service.sign_determination("rev3", draft_id, "终审通过")

    def test_full_countersign_workflow_publishes_current_version(self) -> None:
        draft = self._draft()
        self.assertEqual(draft["state"], "in_review")
        self.assertEqual(draft["next_stage"], "initial")
        published = self._sign_all(draft["draft_id"])
        self.assertEqual(published["state"], "published")
        self.assertEqual([s["stage"] for s in published["signoffs"]], ["initial", "specialist", "final"])
        self.assertEqual([s["signer_id"] for s in published["signoffs"]], ["rev1", "rev2", "rev3"])
        current = self.service.current_determination("sp-1")
        self.assertEqual(current["current"]["draft_id"], draft["draft_id"])
        self.assertEqual(current["current"]["scientific_name"], "Blaps confusa")
        self.assertEqual(len(current["current"]["citations"]), 3)
        history = self.service.determination_history("aud", "sp-1")
        self.assertEqual(len(history["drafts"]), 1)
        self.assertEqual(history["drafts"][0]["signoffs"][2]["comment"], "终审通过")

    def test_signoff_must_follow_stage_order_and_role(self) -> None:
        draft = self._draft()
        with self.assertRaises(Forbidden):
            self.service.sign_determination("rev2", draft["draft_id"], "越级复核")
        with self.assertRaises(Forbidden):
            self.service.sign_determination("rev3", draft["draft_id"], "越级终审")
        with self.assertRaises(Forbidden):
            self.service.sign_determination("op", draft["draft_id"], "操作员无权签署")
        self.service.sign_determination("rev1", draft["draft_id"], "初审通过")
        with self.assertRaises(Forbidden):
            self.service.sign_determination("rev3", draft["draft_id"], "仍未到终审")

    def test_repeat_signoff_is_idempotent_and_never_duplicates_decision(self) -> None:
        draft = self._draft()
        first = self.service.sign_determination("rev1", draft["draft_id"], "初审通过")
        replay = self.service.sign_determination("rev1", draft["draft_id"], "初审通过")
        self.assertEqual(first["signoffs"], replay["signoffs"])
        published = self._sign_all(draft["draft_id"])
        again = self.service.sign_determination("rev3", draft["draft_id"], "重复提交终审")
        self.assertEqual(again["signoffs"], published["signoffs"])
        count = self.connection.execute(
            "SELECT count(*) FROM draft_signoffs WHERE draft_id=?", (draft["draft_id"],)
        ).fetchone()[0]
        self.assertEqual(count, 3)
        with self.assertRaises(InvalidState):
            self.service.sign_determination("rev4", draft["draft_id"], "另一终审人重复签署")

    def test_conflict_of_interest_blocks_signoff(self) -> None:
        draft = self._draft(disclosures=["rev1"])
        with self.assertRaises(PublishBlocked) as caught:
            self.service.sign_determination("rev1", draft["draft_id"], "初审")
        self.assertEqual(caught.exception.reasons[0]["code"], "conflict_of_interest")

    def test_withdrawn_citation_blocks_final_signoff(self) -> None:
        draft = self._draft()
        self.service.sign_determination("rev1", draft["draft_id"], "初审通过")
        self.service.sign_determination("rev2", draft["draft_id"], "复核通过")
        self.service.withdraw_specimen_evidence("op", self.e_analysis, "批次数据作废")
        with self.assertRaises(PublishBlocked) as caught:
            self.service.sign_determination("rev3", draft["draft_id"], "终审")
        codes = {reason["code"] for reason in caught.exception.reasons}
        self.assertEqual(codes, {"evidence_gap"})

    def test_missing_required_evidence_type_blocks_publish(self) -> None:
        draft = self._draft(evidence=[self.e_material])
        self.service.sign_determination("rev1", draft["draft_id"], "初审通过")
        self.service.sign_determination("rev2", draft["draft_id"], "复核通过")
        with self.assertRaises(PublishBlocked) as caught:
            self.service.sign_determination("rev3", draft["draft_id"], "终审")
        self.assertIn("evidence_gap", {reason["code"] for reason in caught.exception.reasons})

    def test_parallel_drafts_yield_single_current_version(self) -> None:
        first = self._draft()
        self._sign_all(first["draft_id"])
        duplicate = self._draft()
        self.service.sign_determination("rev1", duplicate["draft_id"], "初审通过")
        self.service.sign_determination("rev2", duplicate["draft_id"], "复核通过")
        with self.assertRaises(PublishBlocked) as caught:
            self.service.sign_determination("rev3", duplicate["draft_id"], "终审")
        self.assertIn("duplicate_determination", {r["code"] for r in caught.exception.reasons})
        competing = self._draft(name="Blaps gigas")
        self.service.sign_determination("rev1", competing["draft_id"], "初审通过")
        self.service.sign_determination("rev2", competing["draft_id"], "复核通过")
        with self.assertRaises(PublishBlocked) as caught:
            self.service.sign_determination("rev3", competing["draft_id"], "终审")
        self.assertIn("name_conflict", {r["code"] for r in caught.exception.reasons})
        # 旧版本失效后，竞争稿才能成为唯一当前有效版本。
        self.service.register_specimen_evidence("op", "sp-1", "material", "loan-2", "补充标本")
        published = self.service.sign_determination("rev3", competing["draft_id"], "终审通过")
        self.assertEqual(published["state"], "published")
        self.assertEqual(published["version_no"], 3)
        current = self.service.current_determination("sp-1")
        self.assertEqual(current["current"]["draft_id"], competing["draft_id"])
        count = self.connection.execute(
            "SELECT count(*) FROM determination_drafts WHERE specimen_id='sp-1' AND state='published'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_new_material_invalidates_signed_version_but_keeps_history(self) -> None:
        draft = self._draft()
        self._sign_all(draft["draft_id"])
        result = self.service.register_specimen_evidence("op", "sp-1", "material", "loan-9", "新到标本")
        self.assertEqual(result["invalidated_draft_id"], draft["draft_id"])
        current = self.service.current_determination("sp-1")
        self.assertIsNone(current["current"])
        self.assertEqual(current["last_invalidated"]["draft_id"], draft["draft_id"])
        history = self.service.determination_history("aud", "sp-1")
        old = history["drafts"][0]
        self.assertEqual(old["state"], "invalidated")
        self.assertEqual(len(old["signoffs"]), 3)
        self.assertIn("新证据", old["invalidation_reason"])
        revised = self._draft(basis="补充标本后维持原结论")
        self.assertEqual(revised["version_no"], 2)

    def test_withdrawal_of_cited_evidence_invalidates_signed_version(self) -> None:
        draft = self._draft()
        self._sign_all(draft["draft_id"])
        result = self.service.withdraw_specimen_evidence("op", self.e_material, "借出单位召回")
        self.assertEqual(result["invalidated_draft_id"], draft["draft_id"])
        history = self.service.determination_history("aud", "sp-1")
        self.assertEqual(history["drafts"][0]["state"], "invalidated")
        self.assertIn("撤回", history["drafts"][0]["invalidation_reason"])

    def test_withdrawal_of_uncited_evidence_keeps_signed_version(self) -> None:
        draft = self._draft(evidence=[self.e_material, self.e_analysis])
        self._sign_all(draft["draft_id"])
        result = self.service.withdraw_specimen_evidence("op", self.e_photo, "照片底片损坏")
        self.assertIsNone(result["invalidated_draft_id"])
        self.assertIsNotNone(self.service.current_determination("sp-1")["current"])

    def test_history_requires_privileged_role_but_current_is_public(self) -> None:
        draft = self._draft()
        self._sign_all(draft["draft_id"])
        with self.assertRaises(Forbidden):
            self.service.determination_history("op", "sp-1")
        with self.assertRaises(Forbidden):
            self.service.get_determination_draft("op", draft["draft_id"])
        current = self.service.current_determination("sp-1")
        self.assertEqual(current["current"]["state"], "published")

    def test_scientific_name_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self._draft(name="confusa")
        with self.assertRaises(ValidationFailed):
            self._draft(name="blaps confusa")

    def test_draft_requires_active_local_evidence(self) -> None:
        with self.assertRaises(ValidationFailed):
            self._draft(evidence=[])
        self.service.register_specimen("op", "sp-2", "CAT-0002", "另一种甲虫")
        other = self.service.register_specimen_evidence("op", "sp-2", "material", "loan-x", "他标本材料")
        with self.assertRaises(ValidationFailed):
            self._draft(evidence=[other["evidence_id"], self.e_analysis])
        self.service.withdraw_specimen_evidence("op", self.e_photo, "底片丢失")
        with self.assertRaises(ValidationFailed):
            self._draft(evidence=[self.e_material, self.e_analysis, self.e_photo])

    def test_duplicate_specimen_and_evidence_rejected(self) -> None:
        with self.assertRaises(Conflict):
            self.service.register_specimen("op", "sp-1", "CAT-0001", "重复标本")
        with self.assertRaises(Conflict):
            self.service.register_specimen_evidence("op", "sp-1", "material", "loan-1", "重复登记")


if __name__ == "__main__":
    unittest.main()
