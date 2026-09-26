from __future__ import annotations

import json
import sqlite3
import unittest

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.service import TaxonomyLabService


def sha(seed: int) -> str:
    return f"{seed:x}".rjust(64, "0")


class DeterminationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("operator", "operator"),
            ("approver", "approver"),
            ("rev-init", "initial_reviewer"),
            ("rev-spec", "specialist_reviewer"),
            ("rev-final", "final_reviewer"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.service.register_specimen("operator", "sp-1", "C-001", "coleoptera")
        for evidence_id, kind, seed in (
            ("ev-init", "initial_identification", 1),
            ("ev-mol", "molecular_batch", 2),
            ("ev-type", "type_photograph", 3),
        ):
            self.service.register_specimen_evidence(
                "operator", evidence_id, "sp-1", kind, kind, sha(seed), "team")
        for reviewer, stage in (
            ("rev-init", "initial_review"),
            ("rev-spec", "specialist_review"),
            ("rev-final", "final_review"),
        ):
            self.service.assign_reviewer("approver", reviewer, stage)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if actor:
            headers["X-Actor-Id"] = actor
        response = self.app.handle(
            "POST", path, headers, json.dumps(payload).encode("utf-8"))
        return response.status, response.body

    def _get(self, path: str, actor: str | None = None) -> tuple[int, dict]:
        headers = {"X-Actor-Id": actor} if actor else {}
        response = self.app.handle("GET", path, headers)
        return response.status, response.body

    def _publish(self) -> int:
        status, body = self._post(
            "/specimens/sp-1/determinations",
            {"scientific_name": "Carabus apiensis", "rationale": "三项一致",
             "evidence_ids": ["ev-init", "ev-mol", "ev-type"]},
            "operator")
        self.assertEqual(status, 201)
        version_id = body["version"]["version_id"]
        for reviewer, comment in (
            ("rev-init", "初审"), ("rev-spec", "专科"), ("rev-final", "终审")):
            status, body = self._post(
                f"/specimens/sp-1/determinations/{version_id}/sign",
                {"approve": True, "comment": comment}, reviewer)
            self.assertEqual(status, 200, body)
        return version_id

    def test_public_current_and_research_history(self) -> None:
        version_id = self._publish()
        status, body = self._get("/specimens/sp-1/determination/current")
        self.assertEqual(status, 200)
        self.assertEqual(body["scientific_name"], "Carabus apiensis")
        self.assertEqual(len(body["evidence_refs"]), 3)
        # 研究端：需要 X-Actor-Id 且具备读权限
        status, body = self._get("/specimens/sp-1/determinations")
        self.assertEqual(status, 422)
        status, body = self._get("/specimens/sp-1/determinations", "operator")
        self.assertEqual(status, 403)
        status, history = self._get("/specimens/sp-1/determinations", "approver")
        self.assertEqual(status, 200)
        self.assertEqual(history["versions"][0]["version"]["version_no"], version_id)
        self.assertEqual(
            [s["reviewer_id"] for s in history["versions"][0]["signoffs"]],
            ["rev-init", "rev-spec", "rev-final"],
        )
        status, detail = self._get(f"/specimens/sp-1/determinations/{version_id}", "approver")
        self.assertEqual(status, 200)
        self.assertEqual(detail["version"]["status"], "published")

    def test_current_returns_404_without_published_version(self) -> None:
        status, body = self._get("/specimens/sp-1/determination/current")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"]["code"], "no_current_determination")

    def test_evidence_gap_block_shape(self) -> None:
        status, body = self._post(
            "/specimens/sp-1/determinations",
            {"scientific_name": "Carabus gappensis", "rationale": "缺分子",
             "evidence_ids": ["ev-init", "ev-type"]},
            "operator")
        self.assertEqual(status, 201)
        version_id = body["version"]["version_id"]
        self._post(f"/specimens/sp-1/determinations/{version_id}/sign",
                   {"approve": True, "comment": "初审"}, "rev-init")
        self._post(f"/specimens/sp-1/determinations/{version_id}/sign",
                   {"approve": True, "comment": "专科"}, "rev-spec")
        status, body = self._post(f"/specimens/sp-1/determinations/{version_id}/sign",
                                  {"approve": True, "comment": "终审"}, "rev-final")
        self.assertEqual(status, 422)
        self.assertEqual(body["error"]["code"], "publication_blocked")
        self.assertIn("分子", body["error"]["message"])

    def test_withdraw_invalidates_via_api_and_current_disappears(self) -> None:
        version_id = self._publish()
        status, body = self._post("/specimen_evidence/ev-mol/withdraw",
                                  {"reason": "污染撤回"}, "operator")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "withdrawn")
        status, _ = self._get("/specimens/sp-1/determination/current")
        self.assertEqual(status, 404)
        status, history = self._get("/specimens/sp-1/determinations", "approver")
        self.assertEqual(history["versions"][0]["version"]["status"], "invalidated")
        self.assertTrue(history["versions"][0]["version"]["invalidation_reason"])
        self.assertEqual(version_id, history["versions"][0]["version"]["version_id"])

    def test_conflict_of_interest_forbidden(self) -> None:
        self._post("/reviewer_conflicts",
                   {"user_id": "rev-init", "specimen_id": "sp-1", "reason": "参与采集"},
                   "approver")
        self._post(
            "/specimens/sp-1/determinations",
            {"scientific_name": "Carabus x", "rationale": "y",
             "evidence_ids": ["ev-init", "ev-mol", "ev-type"]},
            "operator")
        rows = self.connection.execute(
            "SELECT version_id FROM determination_versions WHERE specimen_id='sp-1'").fetchall()
        version_id = rows[0]["version_id"]
        status, body = self._post(f"/specimens/sp-1/determinations/{version_id}/sign",
                                  {"approve": True, "comment": "初审"}, "rev-init")
        self.assertEqual(status, 403)
        self.assertIn("利益回避", body["error"]["message"])


if __name__ == "__main__":
    unittest.main()
