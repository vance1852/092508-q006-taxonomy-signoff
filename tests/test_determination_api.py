from __future__ import annotations

import json
import sqlite3
import unittest

from taxonomy_lab.api import JsonApplication
from taxonomy_lab.service import TaxonomyLabService


def _body(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class DeterminationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = TaxonomyLabService(self.connection)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("op", "operator"),
            ("rev1", "initial_reviewer"),
            ("rev2", "specialist"),
            ("rev3", "final_reviewer"),
            ("aud", "auditor"),
        ):
            self.app.handle("POST", "/users", body=_body(
                {"user_id": user_id, "display_name": user_id, "role": role}
            ))
        self.app.handle(
            "POST", "/specimens", {"x-actor-id": "op"},
            _body({"specimen_id": "sp-1", "catalog_number": "CAT-1", "common_name": "拟步甲"}),
        )
        self.evidence_ids = []
        for evidence_type, ref in (("material", "loan-1"), ("analysis_batch", "batch-1"), ("type_photo", "p-1")):
            response = self.app.handle(
                "POST", "/specimens/sp-1/evidence", {"x-actor-id": "op"},
                _body({"evidence_type": evidence_type, "external_ref": ref, "description": "证据"}),
            )
            self.evidence_ids.append(response.body["evidence_id"])

    def tearDown(self) -> None:
        self.connection.close()

    def _create_draft(self) -> int:
        response = self.app.handle(
            "POST", "/specimens/sp-1/determination_drafts", {"x-actor-id": "op"},
            _body({
                "scientific_name": "Blaps confusa",
                "determination_basis": "形态与分子证据一致",
                "evidence_ids": self.evidence_ids,
            }),
        )
        self.assertEqual(response.status, 201)
        return response.body["draft_id"]

    def test_countersign_flow_over_http(self) -> None:
        draft_id = self._create_draft()
        for actor, comment in (("rev1", "初审"), ("rev2", "复核"), ("rev3", "终审")):
            response = self.app.handle(
                "POST", f"/determinations/{draft_id}/sign", {"x-actor-id": actor},
                _body({"comment": comment}),
            )
            self.assertEqual(response.status, 200)
        self.assertEqual(response.body["state"], "published")
        current = self.app.handle("GET", "/specimens/sp-1/determination")
        self.assertEqual(current.status, 200)
        self.assertEqual(current.body["current"]["draft_id"], draft_id)
        history = self.app.handle("GET", "/specimens/sp-1/determinations", {"x-actor-id": "aud"})
        self.assertEqual(history.status, 200)
        self.assertEqual(len(history.body["drafts"]), 1)
        detail = self.app.handle("GET", f"/determinations/{draft_id}", {"x-actor-id": "aud"})
        self.assertEqual(detail.status, 200)
        self.assertEqual(len(detail.body["signoffs"]), 3)

    def test_current_endpoint_is_public_and_history_needs_actor(self) -> None:
        draft_id = self._create_draft()
        response = self.app.handle("GET", "/specimens/sp-1/determination")
        self.assertEqual(response.status, 200)
        self.assertIsNone(response.body["current"])
        missing_actor = self.app.handle("GET", "/specimens/sp-1/determinations")
        self.assertEqual(missing_actor.status, 422)
        denied = self.app.handle("GET", "/specimens/sp-1/determinations", {"x-actor-id": "op"})
        self.assertEqual(denied.status, 403)
        detail_denied = self.app.handle("GET", f"/determinations/{draft_id}", {"x-actor-id": "op"})
        self.assertEqual(detail_denied.status, 403)

    def test_publish_blocked_error_carries_reasons(self) -> None:
        draft_id = self._create_draft()
        self.app.handle("POST", f"/determinations/{draft_id}/sign", {"x-actor-id": "rev1"}, _body({"comment": "初审"}))
        self.app.handle("POST", f"/determinations/{draft_id}/sign", {"x-actor-id": "rev2"}, _body({"comment": "复核"}))
        self.app.handle(
            "POST", f"/evidence/{self.evidence_ids[1]}/withdraw", {"x-actor-id": "op"},
            _body({"reason": "批次作废"}),
        )
        blocked = self.app.handle(
            "POST", f"/determinations/{draft_id}/sign", {"x-actor-id": "rev3"}, _body({"comment": "终审"})
        )
        self.assertEqual(blocked.status, 422)
        self.assertEqual(blocked.body["error"]["code"], "determination_publish_blocked")
        self.assertEqual(blocked.body["error"]["reasons"][0]["code"], "evidence_gap")

    def test_unknown_specimen_and_draft_return_404(self) -> None:
        self.assertEqual(self.app.handle("GET", "/specimens/nope/determination").status, 404)
        self.assertEqual(self.app.handle("GET", "/determinations/99", {"x-actor-id": "aud"}).status, 404)


if __name__ == "__main__":
    unittest.main()
