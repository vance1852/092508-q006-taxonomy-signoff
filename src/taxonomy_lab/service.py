"""分类实验观察采信服务的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal
from typing import Any, Iterable, Mapping

from .analysis import ALGORITHM_VERSION, analyze
from .clock import SystemClock, isoformat
from .contracts import EvidenceItem, EvidenceProtocol, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, PublicationBlocked, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


REVIEW_STAGES = ("initial_review", "specialist_review", "final_review")
STAGE_ROLE = {
    "initial_review": "initial_reviewer",
    "specialist_review": "specialist_reviewer",
    "final_review": "final_reviewer",
}
ROLE_STAGE = {role: stage for stage, role in STAGE_ROLE.items()}
SPECIMEN_EVIDENCE_KINDS = ("initial_identification", "molecular_batch", "type_photograph")

ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "evidence_item.import",
        "exclusion.request", "exclusion.revoke",
        "specimen.register", "specimen_evidence.write", "determination.create",
    },
    "statistician": {
        "evidence_protocol.publish", "batch.seal", "exclusion.review", "analysis.run",
        "determination.read",
    },
    "approver": {
        "decision.write", "reviewer.assign", "conflict.declare", "determination.read",
    },
    "auditor": {"report.read", "audit.read", "determination.read"},
    "initial_reviewer": {"determination.sign.initial", "determination.read", "conflict.declare"},
    "specialist_reviewer": {"determination.sign.specialist", "determination.read", "conflict.declare"},
    "final_reviewer": {"determination.sign.final", "determination.read", "conflict.declare"},
}


class TaxonomyLabService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_device(
        self, actor_id: str, device_id: str, model_name: str, vendor: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO capture_devices(device_id,model_name,vendor,created_at) VALUES(?,?,?,?)",
                    (device_id, model_name, vendor, self._now()),
                )
                self._audit("device", device_id, "device.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"实验采集设备已存在: {device_id}") from exc
        return {"device_id": device_id, "model_name": model_name, "vendor": vendor}

    def register_build(
        self, actor_id: str, build_id: str, device_id: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("构建摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO builds(build_id,device_id,version,content_sha256,created_at) VALUES(?,?,?,?,?)",
                    (build_id, device_id, version, content_sha256.lower(), self._now()),
                )
                self._audit("build", build_id, "build.registered", actor_id, {"device_id": device_id, "version": version})
        except sqlite3.IntegrityError as exc:
            raise Conflict("构建编号、版本或摘要冲突") from exc
        return {"build_id": build_id, "device_id": device_id, "version": version}

    def publish_evidence_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "evidence_protocol.publish")
        try:
            evidence_protocol = EvidenceProtocol.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_protocol_catalog(evidence_protocol_id,version,title,task_family,canonical_json,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        evidence_protocol.evidence_protocol_id,
                        evidence_protocol.version,
                        evidence_protocol.title,
                        evidence_protocol.task_family,
                        text,
                        digest,
                        self._now(),
                    ),
                )
                identity = f"{evidence_protocol.evidence_protocol_id}@{evidence_protocol.version}"
                self._audit("evidence_protocol", identity, "evidence_protocol.published", actor_id, {"sha256": digest})
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"evidence_protocol_id": evidence_protocol.evidence_protocol_id, "version": evidence_protocol.version, "sha256": digest}

    def _evidence_protocol(self, evidence_protocol_id: str, version: int) -> tuple[EvidenceProtocol, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM evidence_protocol_catalog WHERE evidence_protocol_id=? AND version=?",
            (evidence_protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("协议版本不存在")
        return EvidenceProtocol.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    def create_batch(
        self,
        actor_id: str,
        batch_id: str,
        evidence_protocol_id: str,
        evidence_protocol_version: int,
        build_id: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "batch.create")
        self._evidence_protocol(evidence_protocol_id, evidence_protocol_version)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO batches(batch_id,evidence_protocol_id,evidence_protocol_version,build_id,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, evidence_protocol_id, evidence_protocol_version, build_id, "draft", actor_id, self._now()),
                )
                self._audit("batch", batch_id, "batch.created", actor_id, {"build_id": build_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次编号冲突或构建不存在") from exc
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return dict(row)

    def start_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.start")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE batches SET state='running',revision=revision+1,started_at=? "
                "WHERE batch_id=? AND state='draft' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次不是当前草稿版本")
            self._audit("batch", batch_id, "batch.started", actor_id, {"from_revision": expected_revision})
        return self.get_batch(batch_id)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def import_evidence_items(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        raw_rows: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence_item.import")
        rows = tuple(raw_rows)
        if not rows:
            raise ValidationFailed("观察记录数组不能为空")
        request_digest = content_digest(rows)
        scope = f"evidence_items:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.get_batch(batch_id)
        if batch["state"] != "running":
            raise InvalidState("只有运行中的批次可以导入观察记录")
        evidence_protocol, _ = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        parsed: list[EvidenceItem] = []
        for raw in rows:
            try:
                item = EvidenceItem.from_dict(raw, evidence_protocol)
            except ValidationError as exc:
                raise ValidationFailed(str(exc)) from exc
            if item.device_id != self.connection.execute(
                "SELECT device_id FROM builds WHERE build_id=?", (batch["build_id"],)
            ).fetchone()["device_id"]:
                raise ValidationFailed("观察记录设备与批次登记不一致")
            parsed.append(item)
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        try:
            with transaction(self.connection, immediate=True):
                for item, raw in zip(parsed, rows):
                    self.connection.execute(
                        "INSERT INTO evidence_items(batch_id,source_batch,source_row,device_id,evidence_group_key,observed_at," 
                        "indicators_json,content_sha256,imported_by,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            batch_id,
                            item.source_batch,
                            item.source_row,
                            item.device_id,
                            item.evidence_group_key,
                            item.observed_at,
                            canonical_json({key: format(value, "f") for key, value in item.indicators.items()}),
                            content_digest([raw]),
                            actor_id,
                            self._now(),
                        ),
                    )
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
                self._audit("batch", batch_id, "evidence_items.imported", actor_id, response)
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源行重复或幂等键并发冲突") from exc
        return response

    def request_exclusion(self, actor_id: str, evidence_item_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.request")
        evidence_item = self.connection.execute(
            "SELECT evidence_item_id,batch_id FROM evidence_items WHERE evidence_item_id=?", (evidence_item_id,)
        ).fetchone()
        if evidence_item is None:
            raise NotFound("观察记录不存在")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO exclusion_requests(evidence_item_id,status,reason,requested_by,requested_at) "
                    "VALUES(?,?,?,?,?)",
                    (evidence_item_id, "pending", reason, actor_id, self._now()),
                )
                exclusion_id = cursor.lastrowid
                self._audit("evidence_item", str(evidence_item_id), "exclusion.requested", actor_id, {"reason": reason})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该观察记录已有待处理或生效排除") from exc
        return {"exclusion_id": exclusion_id, "status": "pending"}

    def review_exclusion(
        self, actor_id: str, exclusion_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "exclusion.review")
        row = self.connection.execute(
            "SELECT * FROM exclusion_requests WHERE exclusion_id=?", (exclusion_id,)
        ).fetchone()
        if row is None:
            raise NotFound("排除申请不存在")
        if row["status"] != "pending":
            raise InvalidState("排除申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的排除申请")
        status = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE exclusion_requests SET status=?,reviewed_by=?,reviewed_at=?,review_note=? "
                "WHERE exclusion_id=? AND status='pending'",
                (status, actor_id, self._now(), note, exclusion_id),
            )
            self._audit("exclusion", str(exclusion_id), f"exclusion.{status}", actor_id, {"note": note})
        return {"exclusion_id": exclusion_id, "status": status}

    def revoke_exclusion(self, actor_id: str, exclusion_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "exclusion.revoke")
        row = self.connection.execute(
            "SELECT e.*,o.batch_id FROM exclusion_requests e "
            "JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id WHERE e.exclusion_id=?",
            (exclusion_id,),
        ).fetchone()
        if row is None:
            raise NotFound("排除记录不存在")
        if row["status"] != "approved":
            raise InvalidState("只有已批准的排除可以撤销")
        if row["requested_by"] != actor_id:
            raise Forbidden("只有原申请人可以撤销排除")
        batch = self.get_batch(row["batch_id"])
        if batch["state"] != "running":
            raise InvalidState("批次封存后不能改变排除状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE exclusion_requests SET status='revoked',review_note=?,reviewed_at=? "
                "WHERE exclusion_id=? AND status='approved'",
                (reason, self._now(), exclusion_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("排除状态已变化")
            self._audit(
                "evidence_item",
                str(row["evidence_item_id"]),
                "exclusion.revoked",
                actor_id,
                {"exclusion_id": exclusion_id, "reason": reason},
            )
        return {"exclusion_id": exclusion_id, "status": "revoked"}

    def seal_batch(self, actor_id: str, batch_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "batch.seal")
        with transaction(self.connection, immediate=True):
            pending = self.connection.execute(
                "SELECT count(*) FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
                "WHERE o.batch_id=? AND e.status='pending'", (batch_id,)
            ).fetchone()[0]
            if pending:
                raise InvalidState("仍有待复核的排除申请")
            cursor = self.connection.execute(
                "UPDATE batches SET state='sealed',revision=revision+1,sealed_at=? "
                "WHERE batch_id=? AND state='running' AND revision=?",
                (self._now(), batch_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("批次状态或版本已变化")
            new_revision = expected_revision + 1
            now = self._now()
            self.connection.execute(
                "INSERT INTO analysis_jobs(batch_id,batch_revision,state,available_at,created_at,updated_at) "
                "VALUES(?,?, 'queued', ?,?,?)",
                (batch_id, new_revision, now, now, now),
            )
            self._audit("batch", batch_id, "batch.sealed", actor_id, {"revision": new_revision})
        return self.get_batch(batch_id)

    def claim_job(self, worker_id: str, lease_seconds: int = 60) -> dict[str, Any] | None:
        if lease_seconds <= 0:
            raise ValidationFailed("租约时长必须大于零")
        now = self._now()
        expires = isoformat(self.clock.now() + timedelta(seconds=lease_seconds))
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT job_id FROM analysis_jobs WHERE "
                "(state='queued' AND available_at<=?) OR (state='leased' AND lease_expires_at<=?) "
                "ORDER BY available_at,job_id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            self.connection.execute(
                "UPDATE analysis_jobs SET state='leased',attempts=attempts+1,lease_owner=?,lease_expires_at=?,updated_at=? "
                "WHERE job_id=?",
                (worker_id, expires, now, row["job_id"]),
            )
            claimed = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (row["job_id"],)).fetchone()
        return dict(claimed)

    def _analysis_evidence_items(self, batch_id: str, evidence_protocol: EvidenceProtocol) -> tuple[EvidenceItem, ...]:
        rows = self.connection.execute(
            "SELECT o.*,e.reason AS excluded_reason FROM evidence_items o "
            "LEFT JOIN exclusion_requests e ON e.evidence_item_id=o.evidence_item_id AND e.status='approved' "
            "WHERE o.batch_id=? ORDER BY o.evidence_item_id",
            (batch_id,),
        ).fetchall()
        items: list[EvidenceItem] = []
        for row in rows:
            indicators = json.loads(row["indicators_json"])
            items.append(EvidenceItem(
                source_batch=row["source_batch"],
                source_row=row["source_row"],
                device_id=row["device_id"],
                evidence_protocol_id=evidence_protocol.evidence_protocol_id,
                evidence_protocol_version=evidence_protocol.version,
                evidence_group_key=row["evidence_group_key"],
                observed_at=row["observed_at"],
                indicators={key: Decimal(str(value)) for key, value in indicators.items()},
                excluded_reason=row["excluded_reason"],
            ))
        return tuple(items)

    def complete_job(self, worker_id: str, job_id: int, statistician_id: str) -> dict[str, Any]:
        self._require(statistician_id, "analysis.run")
        job = self.connection.execute("SELECT * FROM analysis_jobs WHERE job_id=?", (job_id,)).fetchone()
        if job is None:
            raise NotFound("分析任务不存在")
        if job["state"] != "leased" or job["lease_owner"] != worker_id:
            raise InvalidState("任务未由当前工作进程持有")
        if job["lease_expires_at"] <= self._now():
            raise InvalidState("任务租约已经过期")
        batch = self.get_batch(job["batch_id"])
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        evidence_items = self._analysis_evidence_items(batch["batch_id"], evidence_protocol)
        snapshot_rows = [
            {
                "source_batch": item.source_batch,
                "source_row": item.source_row,
                "evidence_group": item.evidence_group_key,
                "indicators": {key: format(value, "f") for key, value in item.indicators.items()},
                "excluded_reason": item.excluded_reason,
            }
            for item in evidence_items
        ]
        input_digest = content_digest(snapshot_rows)
        result = analyze(evidence_protocol, evidence_items)
        with transaction(self.connection, immediate=True):
            existing = self.connection.execute(
                "SELECT analysis_id,result_json FROM analyses WHERE batch_id=? AND batch_revision=? AND input_sha256=?",
                (batch["batch_id"], job["batch_revision"], input_digest),
            ).fetchone()
            if existing is None:
                cursor = self.connection.execute(
                    "INSERT INTO analyses(batch_id,batch_revision,evidence_protocol_sha256,input_sha256,algorithm_version,seed," 
                    "result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        batch["batch_id"], job["batch_revision"], evidence_protocol_digest, input_digest,
                        ALGORITHM_VERSION, evidence_protocol.seed, canonical_json(result), statistician_id, self._now(),
                    ),
                )
                analysis_id = cursor.lastrowid
            else:
                analysis_id = existing["analysis_id"]
                result = json.loads(existing["result_json"])
            self.connection.execute(
                "UPDATE analysis_jobs SET state='succeeded',lease_owner=NULL,lease_expires_at=NULL,updated_at=? "
                "WHERE job_id=? AND state='leased' AND lease_owner=?",
                (self._now(), job_id, worker_id),
            )
            self.connection.execute(
                "UPDATE batches SET state='analyzed' WHERE batch_id=? AND state IN ('sealed','analyzing')",
                (batch["batch_id"],),
            )
            self._audit(
                "batch",
                batch["batch_id"],
                "analysis.completed",
                statistician_id,
                {"analysis_id": analysis_id, "input_sha256": input_digest},
            )
        return {"analysis_id": analysis_id, "input_sha256": input_digest, "result": result}

    def fail_job(self, worker_id: str, job_id: int, error: str, retry_seconds: int = 0) -> dict[str, Any]:
        available = isoformat(self.clock.now() + timedelta(seconds=retry_seconds))
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE analysis_jobs SET state='queued',available_at=?,lease_owner=NULL,lease_expires_at=NULL," 
                "last_error=?,updated_at=? WHERE job_id=? AND state='leased' AND lease_owner=?",
                (available, error[:1000], self._now(), job_id, worker_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("任务未由当前工作进程持有")
        return {"job_id": job_id, "state": "queued", "available_at": available}

    def decide(
        self, actor_id: str, batch_id: str, analysis_id: int, decision: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if decision not in {"needs_more_data", "approved", "rejected"}:
            raise ValidationFailed("未知观察材料采信决定")
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE analysis_id=? AND batch_id=?", (analysis_id, batch_id)
        ).fetchone()
        if analysis_row is None:
            raise NotFound("分析版本不存在")
        if analysis_row["created_by"] == actor_id:
            raise Forbidden("统计负责人不能批准自己的分析")
        batch = self.get_batch(batch_id)
        if batch["state"] != "analyzed" or batch["revision"] != analysis_row["batch_revision"]:
            raise InvalidState("分析不是批次当前可审批版本")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO decisions(batch_id,analysis_id,decision,reason,decided_by,decided_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (batch_id, analysis_id, decision, reason, actor_id, self._now()),
                )
                self.connection.execute("UPDATE batches SET state='decided' WHERE batch_id=?", (batch_id,))
                self._audit(
                    "batch",
                    batch_id,
                    "decision.recorded",
                    actor_id,
                    {"decision_id": cursor.lastrowid, "analysis_id": analysis_id, "decision": decision},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该分析版本已经形成决定") from exc
        return {"batch_id": batch_id, "analysis_id": analysis_id, "decision": decision}

    # ------------------------------------------------------------------
    # 标本登记与证据
    # ------------------------------------------------------------------

    def register_specimen(
        self, actor_id: str, specimen_id: str, catalog_code: str, taxon_group: str
    ) -> dict[str, Any]:
        self._require(actor_id, "specimen.register")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO specimens(specimen_id,catalog_code,taxon_group,registered_by,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (specimen_id, catalog_code, taxon_group, actor_id, self._now()),
                )
                self._audit("specimen", specimen_id, "specimen.registered", actor_id, {"catalog_code": catalog_code})
        except sqlite3.IntegrityError as exc:
            raise Conflict("标本编号或馆藏目录号已存在") from exc
        return {"specimen_id": specimen_id, "catalog_code": catalog_code, "taxon_group": taxon_group}

    def _get_specimen(self, specimen_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM specimens WHERE specimen_id=?", (specimen_id,)
        ).fetchone()
        if row is None:
            raise NotFound("标本不存在")
        return row

    def register_specimen_evidence(
        self,
        actor_id: str,
        evidence_id: str,
        specimen_id: str,
        evidence_kind: str,
        title: str,
        content_sha256: str,
        provided_by: str,
        analysis_sha256: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "specimen_evidence.write")
        if evidence_kind not in SPECIMEN_EVIDENCE_KINDS:
            raise ValidationFailed("证据类型必须是 initial_identification、molecular_batch 或 type_photograph")
        if len(content_sha256) != 64:
            raise ValidationFailed("证据摘要必须是 64 位 SHA-256")
        if analysis_sha256 is not None and len(analysis_sha256) != 64:
            raise ValidationFailed("分析批次摘要必须是 64 位 SHA-256")
        self._get_specimen(specimen_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO specimen_evidence(evidence_id,specimen_id,evidence_kind,title,"
                    "analysis_sha256,provided_by,content_sha256,status,registered_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,'active',?,?)",
                    (evidence_id, specimen_id, evidence_kind, title, analysis_sha256,
                     provided_by, content_sha256, actor_id, self._now()),
                )
                # 新增材料后，基于旧证据集合形成的当前有效结论失效，必须重走会签。
                invalidated = self._invalidate_published_for_specimen(
                    actor_id, specimen_id, f"标本新增材料 {evidence_id}（{evidence_kind}），原结论需重新评估"
                )
                self._audit("specimen_evidence", evidence_id, "specimen_evidence.registered", actor_id,
                            {"specimen_id": specimen_id, "evidence_kind": evidence_kind,
                             "invalidated_versions": invalidated})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号冲突或同内容证据已登记") from exc
        return {"evidence_id": evidence_id, "specimen_id": specimen_id, "status": "active",
                "invalidated_versions": invalidated}

    def withdraw_specimen_evidence(
        self, actor_id: str, evidence_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "specimen_evidence.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM specimen_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFound("证据不存在")
            if row["status"] != "active":
                raise InvalidState("证据已经撤回")
            cursor = self.connection.execute(
                "UPDATE specimen_evidence SET status='withdrawn',withdrawn_reason=?,withdrawn_by=?,withdrawn_at=? "
                "WHERE evidence_id=? AND status='active'",
                (reason, actor_id, self._now(), evidence_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("证据状态已变化")
            invalidated = self._invalidate_versions_for_evidence(
                actor_id, evidence_id, f"引用证据 {evidence_id} 已撤回：{reason}"
            )
            self._audit("specimen_evidence", evidence_id, "specimen_evidence.withdrawn", actor_id,
                        {"reason": reason, "invalidated_versions": invalidated})
        return {"evidence_id": evidence_id, "status": "withdrawn"}

    # ------------------------------------------------------------------
    # 复核人员指派与利益回避
    # ------------------------------------------------------------------

    def assign_reviewer(self, actor_id: str, user_id: str, stage: str) -> dict[str, Any]:
        self._require(actor_id, "reviewer.assign")
        if stage not in STAGE_ROLE:
            raise ValidationFailed("会签环节必须是 initial_review、specialist_review 或 final_review")
        user = self._user(user_id)
        if user["role"] != STAGE_ROLE[stage]:
            raise Forbidden(f"用户角色 {user['role']} 不匹配 {stage} 环节")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO determination_reviewers(user_id,stage,assigned_at) VALUES(?,?,?) "
                "ON CONFLICT(user_id,stage) DO UPDATE SET assigned_at=excluded.assigned_at",
                (user_id, stage, self._now()),
            )
            self._audit("reviewer", user_id, "reviewer.assigned", actor_id, {"stage": stage})
        return {"user_id": user_id, "stage": stage}

    def declare_reviewer_conflict(
        self, actor_id: str, user_id: str, specimen_id: str, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "conflict.declare")
        if not reason.strip():
            raise ValidationFailed("利益回避原因不能为空")
        self._get_specimen(specimen_id)
        self._user(user_id)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO reviewer_conflicts(user_id,specimen_id,reason,declared_by,declared_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(user_id,specimen_id) DO UPDATE SET "
                "reason=excluded.reason,declared_by=excluded.declared_by,declared_at=excluded.declared_at",
                (user_id, specimen_id, reason, actor_id, self._now()),
            )
            self._audit("reviewer_conflict", f"{user_id}:{specimen_id}", "reviewer_conflict.declared",
                        actor_id, {"user_id": user_id, "specimen_id": specimen_id})
        return {"user_id": user_id, "specimen_id": specimen_id, "declared": True}

    # ------------------------------------------------------------------
    # 版本化鉴定稿
    # ------------------------------------------------------------------

    def _next_version_no(self, specimen_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version_no),0)+1 FROM determination_versions WHERE specimen_id=?",
            (specimen_id,),
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _determination_content(
        scientific_name: str, authorship: str | None, rationale: str, evidence_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        return [
            {"scientific_name": scientific_name, "authorship": authorship, "rationale": rationale},
            {"evidence_ids": sorted(evidence_ids)},
        ]

    def create_determination(
        self,
        actor_id: str,
        specimen_id: str,
        scientific_name: str,
        rationale: str,
        evidence_ids: list[str],
        authorship: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "determination.create")
        scientific_name = scientific_name.strip()
        rationale = rationale.strip()
        if not scientific_name or not rationale:
            raise ValidationFailed("学名与鉴定理由不能为空")
        if not evidence_ids:
            raise ValidationFailed("鉴定稿至少引用一条证据")
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValidationFailed("证据引用不能重复")
        digest = content_digest(self._determination_content(
            scientific_name, authorship, rationale, tuple(evidence_ids)
        ))
        now = self._now()
        with transaction(self.connection, immediate=True):
            self._get_specimen(specimen_id)
            refs: list[sqlite3.Row] = []
            for evidence_id in evidence_ids:
                row = self.connection.execute(
                    "SELECT * FROM specimen_evidence WHERE evidence_id=? AND specimen_id=?",
                    (evidence_id, specimen_id),
                ).fetchone()
                if row is None:
                    raise NotFound(f"证据不存在或不属于该标本: {evidence_id}")
                refs.append(row)
            if any(row["status"] != "active" for row in refs):
                raise InvalidState("不能引用已撤回的证据")
            existing_draft = self.connection.execute(
                "SELECT version_id FROM determination_versions WHERE specimen_id=? AND status='draft'",
                (specimen_id,),
            ).fetchone()
            if existing_draft is not None:
                raise Conflict("该标本已有进行中的鉴定稿，请先完成或驳回")
            version_no = self._next_version_no(specimen_id)
            cursor = self.connection.execute(
                "INSERT INTO determination_versions(specimen_id,version_no,scientific_name,authorship,"
                "rationale,content_sha256,status,created_by,created_at) VALUES(?,?,?,?,?,?, 'draft',?,?)",
                (specimen_id, version_no, scientific_name, authorship, rationale, digest, actor_id, now),
            )
            version_id = cursor.lastrowid
            for row in refs:
                self.connection.execute(
                    "INSERT INTO determination_evidence_refs(version_id,evidence_id,evidence_kind,title,"
                    "content_sha256,analysis_sha256,captured_status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (version_id, row["evidence_id"], row["evidence_kind"], row["title"],
                     row["content_sha256"], row["analysis_sha256"], row["status"], now),
                )
            self._audit("determination_version", str(version_id), "determination.created", actor_id,
                        {"specimen_id": specimen_id, "version_no": version_no, "evidence_count": len(refs)})
        return self.get_determination(specimen_id, version_id)

    def _get_version(self, specimen_id: str, version_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM determination_versions WHERE version_id=? AND specimen_id=?",
            (version_id, specimen_id),
        ).fetchone()
        if row is None:
            raise NotFound("鉴定稿版本不存在")
        return row

    def _version_refs(self, version_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM determination_evidence_refs WHERE version_id=? ORDER BY ref_id",
            (version_id,),
        ))

    def _version_signoffs(self, version_id: int) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM determination_signoffs WHERE version_id=? ORDER BY signoff_id",
            (version_id,),
        ))

    def get_determination(self, specimen_id: str, version_id: int) -> dict[str, Any]:
        self._get_specimen(specimen_id)
        version = self._get_version(specimen_id, version_id)
        return {
            "version": dict(version),
            "evidence_refs": [dict(row) for row in self._version_refs(version_id)],
            "signoffs": [dict(row) for row in self._version_signoffs(version_id)],
        }

    def read_determination(self, actor_id: str, specimen_id: str, version_id: int) -> dict[str, Any]:
        self._require(actor_id, "determination.read")
        return self.get_determination(specimen_id, version_id)

    def current_determination(self, specimen_id: str) -> dict[str, Any] | None:
        """科普端：返回当前有效结论；无当前结论时返回 None。"""
        self._get_specimen(specimen_id)
        row = self.connection.execute(
            "SELECT * FROM determination_versions WHERE specimen_id=? AND status='published'",
            (specimen_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "specimen_id": specimen_id,
            "version_no": row["version_no"],
            "scientific_name": row["scientific_name"],
            "authorship": row["authorship"],
            "published_at": row["published_at"],
            "evidence_refs": [
                {"evidence_id": r["evidence_id"], "evidence_kind": r["evidence_kind"], "title": r["title"]}
                for r in self._version_refs(row["version_id"])
            ],
        }

    def determination_history(self, actor_id: str, specimen_id: str) -> dict[str, Any]:
        """研究端：完整修订链、签署人与引用依据。"""
        self._require(actor_id, "determination.read")
        self._get_specimen(specimen_id)
        versions = self.connection.execute(
            "SELECT version_id FROM determination_versions WHERE specimen_id=? ORDER BY version_no",
            (specimen_id,),
        ).fetchall()
        return {
            "specimen_id": specimen_id,
            "versions": [self.get_determination(specimen_id, row["version_id"]) for row in versions],
        }

    def _invalidate_published_for_specimen(
        self, actor_id: str, specimen_id: str, reason: str
    ) -> list[int]:
        """新增材料时仅使当前有效（published）版本失效；草稿与历史版本不动。"""
        invalidated: list[int] = []
        rows = self.connection.execute(
            "SELECT version_id FROM determination_versions WHERE specimen_id=? AND status='published'",
            (specimen_id,),
        ).fetchall()
        now = self._now()
        for row in rows:
            self.connection.execute(
                "UPDATE determination_versions SET status='invalidated',invalidation_reason=?,invalidated_at=? "
                "WHERE version_id=? AND status='published'",
                (reason, now, row["version_id"]),
            )
            self._audit("determination_version", str(row["version_id"]),
                        "determination.invalidated", actor_id, {"reason": reason})
            invalidated.append(row["version_id"])
        return invalidated

    def _invalidate_versions_for_evidence(
        self, actor_id: str, evidence_id: str, reason: str
    ) -> list[int]:
        """使引用该证据的会签中/当前有效版本失效；已终结的历史版本保持原样。"""
        invalidated: list[int] = []
        rows = self.connection.execute(
            "SELECT v.version_id FROM determination_versions v "
            "JOIN determination_evidence_refs r ON r.version_id=v.version_id "
            "WHERE r.evidence_id=? AND v.status IN ('draft','published')",
            (evidence_id,),
        ).fetchall()
        now = self._now()
        for row in rows:
            cursor = self.connection.execute(
                "UPDATE determination_versions SET status='invalidated',invalidation_reason=?,invalidated_at=? "
                "WHERE version_id=? AND status IN ('draft','published')",
                (reason, now, row["version_id"]),
            )
            if cursor.rowcount == 1:
                self._audit("determination_version", str(row["version_id"]),
                            "determination.invalidated", actor_id, {"reason": reason})
                invalidated.append(row["version_id"])
        return invalidated

    def sign_determination(
        self,
        actor_id: str,
        specimen_id: str,
        version_id: int,
        approve: bool,
        comment: str,
    ) -> dict[str, Any]:
        user = self._user(actor_id)
        stage = ROLE_STAGE.get(user["role"])
        if stage is None:
            raise Forbidden("当前角色不能会签鉴定稿")
        self._require(actor_id, f"determination.sign.{stage.replace('_review', '')}")
        if not comment.strip():
            raise ValidationFailed("会签意见不能为空")
        with transaction(self.connection, immediate=True):
            version = self._get_version(specimen_id, version_id)
            if version["status"] != "draft":
                raise InvalidState(f"鉴定稿当前状态 {version['status']} 不接受会签")
            assigned = self.connection.execute(
                "SELECT 1 FROM determination_reviewers WHERE user_id=? AND stage=?",
                (actor_id, stage),
            ).fetchone()
            if assigned is None:
                raise Forbidden(f"用户未被指派为 {stage} 环节复核人")
            conflict = self.connection.execute(
                "SELECT reason FROM reviewer_conflicts WHERE user_id=? AND specimen_id=?",
                (actor_id, specimen_id),
            ).fetchone()
            if conflict is not None:
                raise Forbidden(f"存在利益回避，不得会签：{conflict['reason']}")
            expected_index = REVIEW_STAGES.index(stage)
            prior = self.connection.execute(
                "SELECT stage,decision FROM determination_signoffs WHERE version_id=?",
                (version_id,),
            ).fetchall()
            prior_by_stage = {row["stage"]: row["decision"] for row in prior}
            for earlier in REVIEW_STAGES[:expected_index]:
                if prior_by_stage.get(earlier) != "approved":
                    raise InvalidState(f"必须先完成 {earlier} 环节的批准")
            if stage in prior_by_stage:
                raise Conflict("该环节已经签署，重复签署不得生成第二份决定")
            decision = "approved" if approve else "rejected"
            now = self._now()
            self.connection.execute(
                "INSERT INTO determination_signoffs(version_id,stage,decision,reviewer_id,comment,signed_at) "
                "VALUES(?,?,?,?,?,?)",
                (version_id, stage, decision, actor_id, comment, now),
            )
            self._audit("determination_version", str(version_id), f"determination.signed.{stage}", actor_id,
                        {"decision": decision})
            if not approve:
                self.connection.execute(
                    "UPDATE determination_versions SET status='rejected' WHERE version_id=? AND status='draft'",
                    (version_id,),
                )
                self._audit("determination_version", str(version_id), "determination.rejected", actor_id,
                            {"stage": stage})
                return self.get_determination(specimen_id, version_id)
            if stage == "final_review":
                block_reasons = self._publication_block_reasons(specimen_id, version)
                if block_reasons:
                    raise PublicationBlocked(block_reasons)
                self._publish(actor_id, specimen_id, version_id)
        return self.get_determination(specimen_id, version_id)

    def _publication_block_reasons(self, specimen_id: str, version: sqlite3.Row) -> list[str]:
        reasons: list[str] = []
        refs = self._version_refs(version["version_id"])
        if not refs:
            reasons.append("证据缺口：鉴定稿没有引用任何证据")
        kinds = {ref["evidence_kind"] for ref in refs}
        kind_labels = {
            "initial_identification": "初鉴形态记录",
            "molecular_batch": "分子分析批次",
            "type_photograph": "模式照片",
        }
        for required in SPECIMEN_EVIDENCE_KINDS:
            if required not in kinds:
                reasons.append(f"证据缺口：缺少{kind_labels[required]}类证据")
        withdrawn = [ref["evidence_id"] for ref in refs if ref["captured_status"] != "active"]
        if withdrawn:
            reasons.append(f"证据缺口：引用证据已撤回 {withdrawn}")
        ruling = self.connection.execute(
            "SELECT t.specimen_id FROM taxon_name_rulings t "
            "JOIN determination_versions v ON v.version_id=t.version_id "
            "WHERE t.scientific_name=? AND t.specimen_id<>? AND v.status='published'",
            (version["scientific_name"], specimen_id),
        ).fetchone()
        if ruling is not None:
            reasons.append(f"学名冲突：{version['scientific_name']} 当前归属标本 {ruling['specimen_id']}")
        return reasons

    def _publish(self, actor_id: str, specimen_id: str, version_id: int) -> None:
        """在会签事务内发布：旧当前版本置为 superseded，写入学名裁定。"""
        version = self._get_version(specimen_id, version_id)
        now = self._now()
        self.connection.execute(
            "UPDATE determination_versions SET status='superseded' "
            "WHERE specimen_id=? AND status='published'",
            (specimen_id,),
        )
        cursor = self.connection.execute(
            "UPDATE determination_versions SET status='published',published_at=? "
            "WHERE version_id=? AND status='draft'",
            (now, version_id),
        )
        if cursor.rowcount != 1:
            raise InvalidState("鉴定稿状态已变化，无法发布")
        self.connection.execute(
            "INSERT INTO taxon_name_rulings(scientific_name,specimen_id,version_id,ruled_at) "
            "VALUES(?,?,?,?) ON CONFLICT(scientific_name) DO UPDATE SET "
            "specimen_id=excluded.specimen_id,version_id=excluded.version_id,ruled_at=excluded.ruled_at",
            (version["scientific_name"], specimen_id, version_id, now),
        )
        self._audit("determination_version", str(version_id), "determination.published", actor_id,
                    {"scientific_name": version["scientific_name"]})

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor",
                                "initial_reviewer", "specialist_reviewer", "final_reviewer"}:
            raise Forbidden("当前角色不能读取完整报告")
        batch = self.get_batch(batch_id)
        evidence_protocol, evidence_protocol_digest = self._evidence_protocol(batch["evidence_protocol_id"], batch["evidence_protocol_version"])
        analysis_row = self.connection.execute(
            "SELECT * FROM analyses WHERE batch_id=? ORDER BY analysis_id DESC LIMIT 1", (batch_id,)
        ).fetchone()
        decision_row = None
        if analysis_row is not None:
            decision_row = self.connection.execute(
                "SELECT * FROM decisions WHERE analysis_id=?", (analysis_row["analysis_id"],)
            ).fetchone()
        exclusions = self.connection.execute(
            "SELECT e.exclusion_id,e.evidence_item_id,e.status,e.reason,e.requested_by,e.reviewed_by "
            "FROM exclusion_requests e JOIN evidence_items o ON o.evidence_item_id=e.evidence_item_id "
            "WHERE o.batch_id=? ORDER BY e.exclusion_id", (batch_id,)
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? "
            "ORDER BY event_id", (batch_id,)
        ).fetchall()
        return {
            "batch": batch,
            "evidence_protocol": {
                "evidence_protocol_id": evidence_protocol.evidence_protocol_id,
                "version": evidence_protocol.version,
                "sha256": evidence_protocol_digest,
                "seed": evidence_protocol.seed,
                "bootstrap_samples": evidence_protocol.bootstrap_samples,
            },
            "analysis": None if analysis_row is None else {
                "analysis_id": analysis_row["analysis_id"],
                "input_sha256": analysis_row["input_sha256"],
                "algorithm_version": analysis_row["algorithm_version"],
                "created_by": analysis_row["created_by"],
                "result": json.loads(analysis_row["result_json"]),
            },
            "decision": None if decision_row is None else dict(decision_row),
            "exclusions": [dict(row) for row in exclusions],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }
