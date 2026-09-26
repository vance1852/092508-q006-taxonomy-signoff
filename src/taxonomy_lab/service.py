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
from .errors import Conflict, Forbidden, InvalidState, NotFound, PublishBlocked, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "operator": {
        "catalog.write", "batch.create", "batch.start", "evidence_item.import",
        "exclusion.request", "exclusion.revoke",
    },
    "statistician": {"evidence_protocol.publish", "batch.seal", "exclusion.review", "analysis.run"},
    "approver": {"decision.write"},
    "auditor": {"report.read", "audit.read"},
    "initial_reviewer": {"determination.sign.initial"},
    "specialist": {"determination.sign.specialist"},
    "final_reviewer": {"determination.sign.final"},
}

DETERMINATION_STAGES = ("initial", "specialist", "final")
STAGE_PERMISSIONS = {
    "initial": "determination.sign.initial",
    "specialist": "determination.sign.specialist",
    "final": "determination.sign.final",
}
STAGE_LABELS = {"initial": "初审", "specialist": "专科复核", "final": "终审"}
DETERMINATION_HISTORY_ROLES = {
    "statistician", "approver", "auditor", "initial_reviewer", "specialist", "final_reviewer",
}
EVIDENCE_TYPES = ("material", "analysis_batch", "type_photo")
REQUIRED_CITATION_TYPES = ("material", "analysis_batch")


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

    def report(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"statistician", "approver", "auditor"}:
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

    # ------------------------------------------------------------------
    # 标本级版本化鉴定稿（模块 006）
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_scientific_name(name: object) -> str:
        if not isinstance(name, str):
            raise ValidationFailed("学名必须是字符串")
        normalized = " ".join(name.split())
        tokens = normalized.split(" ")
        if len(tokens) < 2:
            raise ValidationFailed("学名必须至少包含属名和种加词")
        if not tokens[0][0].isupper():
            raise ValidationFailed("属名首字母必须大写")
        for token in tokens:
            if not all(character.isalpha() or character in "-." for character in token):
                raise ValidationFailed(f"学名包含非法词元: {token}")
        return normalized

    def _specimen(self, specimen_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM specimens WHERE specimen_id=?", (specimen_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"标本不存在: {specimen_id}")
        return row

    def _draft(self, draft_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM determination_drafts WHERE draft_id=?", (draft_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"鉴定稿不存在: {draft_id}")
        return row

    def _draft_signoffs(self, draft_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM draft_signoffs WHERE draft_id=? ORDER BY signoff_id", (draft_id,)
        ).fetchall()

    def _draft_citations(self, draft_id: int) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT c.cited_at,e.* FROM draft_citations c "
            "JOIN specimen_evidence e ON e.evidence_id=c.evidence_id "
            "WHERE c.draft_id=? ORDER BY e.evidence_id",
            (draft_id,),
        ).fetchall()

    def _serialize_draft(self, draft: sqlite3.Row) -> dict[str, Any]:
        signoffs = self._draft_signoffs(draft["draft_id"])
        citations = self._draft_citations(draft["draft_id"])
        next_stage = None
        if draft["state"] == "in_review" and len(signoffs) < len(DETERMINATION_STAGES):
            next_stage = DETERMINATION_STAGES[len(signoffs)]
        return {
            "draft_id": draft["draft_id"],
            "specimen_id": draft["specimen_id"],
            "version_no": draft["version_no"],
            "scientific_name": draft["scientific_name"],
            "determination_basis": draft["determination_basis"],
            "content_sha256": draft["content_sha256"],
            "disclosures": json.loads(draft["disclosures_json"]),
            "state": draft["state"],
            "created_by": draft["created_by"],
            "created_at": draft["created_at"],
            "published_at": draft["published_at"],
            "invalidated_at": draft["invalidated_at"],
            "invalidation_reason": draft["invalidation_reason"],
            "next_stage": next_stage,
            "citations": [
                {
                    "evidence_id": row["evidence_id"],
                    "evidence_type": row["evidence_type"],
                    "external_ref": row["external_ref"],
                    "evidence_status": row["status"],
                    "cited_at": row["cited_at"],
                }
                for row in citations
            ],
            "signoffs": [
                {
                    "stage": row["stage"],
                    "stage_label": STAGE_LABELS[row["stage"]],
                    "signer_id": row["signer_id"],
                    "comment": row["comment"],
                    "signed_at": row["signed_at"],
                }
                for row in signoffs
            ],
        }

    def register_specimen(
        self,
        actor_id: str,
        specimen_id: str,
        catalog_number: str,
        common_name: str,
        collected_at: str | None = None,
        location: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        for label, value in (("标本编号", specimen_id), ("馆藏号", catalog_number), ("中文名", common_name)):
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed(f"{label}不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO specimens(specimen_id,catalog_number,common_name,collected_at,location,registered_by,registered_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        specimen_id.strip(), catalog_number.strip(), common_name.strip(),
                        collected_at, location, actor_id, self._now(),
                    ),
                )
                self._audit("specimen", specimen_id, "specimen.registered", actor_id, {"catalog_number": catalog_number})
        except sqlite3.IntegrityError as exc:
            raise Conflict("标本编号或馆藏号已存在") from exc
        return {"specimen_id": specimen_id.strip(), "catalog_number": catalog_number.strip()}

    def _invalidate_current_determination(
        self, specimen_id: str, reason: str, actor_id: str, trigger: str
    ) -> int | None:
        """使标本当前有效版本失效；历史版本与签署记录全部保留。"""

        row = self.connection.execute(
            "SELECT draft_id,version_no FROM determination_drafts WHERE specimen_id=? AND state='published'",
            (specimen_id,),
        ).fetchone()
        if row is None:
            return None
        cursor = self.connection.execute(
            "UPDATE determination_drafts SET state='invalidated',invalidated_at=?,invalidation_reason=? "
            "WHERE draft_id=? AND state='published'",
            (self._now(), reason, row["draft_id"]),
        )
        if cursor.rowcount != 1:
            raise Conflict("当前有效版本状态已变化")
        self._audit(
            "determination",
            str(row["draft_id"]),
            "determination.invalidated",
            actor_id,
            {"version_no": row["version_no"], "reason": reason, "trigger": trigger},
        )
        return row["draft_id"]

    def register_specimen_evidence(
        self,
        actor_id: str,
        specimen_id: str,
        evidence_type: str,
        external_ref: str,
        description: str,
        content_sha256: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._specimen(specimen_id)
        if evidence_type not in EVIDENCE_TYPES:
            raise ValidationFailed(f"未知证据类型: {evidence_type}")
        if not isinstance(external_ref, str) or not external_ref.strip():
            raise ValidationFailed("证据外部引用不能为空")
        if not isinstance(description, str) or not description.strip():
            raise ValidationFailed("证据描述不能为空")
        if content_sha256 is not None and (
            not isinstance(content_sha256, str) or len(content_sha256) != 64
        ):
            raise ValidationFailed("证据摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO specimen_evidence(specimen_id,evidence_type,external_ref,description,content_sha256,"
                    "status,registered_by,registered_at) VALUES(?,?,?,?,?, 'active', ?,?)",
                    (
                        specimen_id, evidence_type, external_ref.strip(), description.strip(),
                        None if content_sha256 is None else content_sha256.lower(),
                        actor_id, self._now(),
                    ),
                )
                evidence_id = cursor.lastrowid
                self._audit(
                    "specimen_evidence", str(evidence_id), "specimen_evidence.registered", actor_id,
                    {"specimen_id": specimen_id, "evidence_type": evidence_type, "external_ref": external_ref},
                )
                # 新增材料使已签版本失效，但历史版本与签署记录完整保留。
                invalidated = self._invalidate_current_determination(
                    specimen_id,
                    f"登记了新证据 {evidence_id}（{evidence_type}），已签版本需重新鉴定",
                    actor_id,
                    "evidence.registered",
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一标本下相同类型与外部引用的证据已存在") from exc
        return {"evidence_id": evidence_id, "status": "active", "invalidated_draft_id": invalidated}

    def withdraw_specimen_evidence(self, actor_id: str, evidence_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        row = self.connection.execute(
            "SELECT * FROM specimen_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"证据不存在: {evidence_id}")
        if row["status"] != "active":
            raise InvalidState("证据已撤回")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE specimen_evidence SET status='withdrawn',withdrawn_by=?,withdrawn_at=?,withdraw_reason=? "
                "WHERE evidence_id=? AND status='active'",
                (actor_id, self._now(), reason.strip(), evidence_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("证据状态已变化")
            self._audit(
                "specimen_evidence", str(evidence_id), "specimen_evidence.withdrawn", actor_id,
                {"specimen_id": row["specimen_id"], "reason": reason},
            )
            # 撤回被当前有效版本引用的证据时，已签版本失效但保留历史。
            cited = self.connection.execute(
                "SELECT d.draft_id FROM draft_citations c JOIN determination_drafts d ON d.draft_id=c.draft_id "
                "WHERE c.evidence_id=? AND d.state='published'",
                (evidence_id,),
            ).fetchone()
            invalidated = None
            if cited is not None:
                invalidated = self._invalidate_current_determination(
                    row["specimen_id"],
                    f"引用的证据 {evidence_id} 被撤回: {reason.strip()}",
                    actor_id,
                    "evidence.withdrawn",
                )
        return {"evidence_id": evidence_id, "status": "withdrawn", "invalidated_draft_id": invalidated}

    def create_determination_draft(
        self,
        actor_id: str,
        specimen_id: str,
        scientific_name: str,
        determination_basis: str,
        evidence_ids: Iterable[int],
        disclosures: Iterable[str] = (),
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._specimen(specimen_id)
        normalized_name = self._normalize_scientific_name(scientific_name)
        if not isinstance(determination_basis, str) or not determination_basis.strip():
            raise ValidationFailed("鉴定依据不能为空")
        ids = tuple(dict.fromkeys(int(value) for value in evidence_ids))
        if not ids:
            raise ValidationFailed("鉴定稿必须引用至少一条证据")
        declared = []
        for value in disclosures:
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed("利益申报条目必须是非空字符串")
            declared.append(value.strip())
        citations = []
        for evidence_id in ids:
            row = self.connection.execute(
                "SELECT * FROM specimen_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"证据不存在: {evidence_id}")
            if row["specimen_id"] != specimen_id:
                raise ValidationFailed(f"证据 {evidence_id} 不属于标本 {specimen_id}")
            if row["status"] != "active":
                raise ValidationFailed(f"证据 {evidence_id} 已撤回，不能引用")
            citations.append(row)
        digest = content_digest([{
            "specimen_id": specimen_id,
            "scientific_name": normalized_name,
            "determination_basis": determination_basis.strip(),
            "evidence_ids": sorted(ids),
        }])
        try:
            with transaction(self.connection, immediate=True):
                version_row = self.connection.execute(
                    "SELECT COALESCE(MAX(version_no), 0) + 1 AS next_version FROM determination_drafts "
                    "WHERE specimen_id=?",
                    (specimen_id,),
                ).fetchone()
                version_no = version_row["next_version"]
                cursor = self.connection.execute(
                    "INSERT INTO determination_drafts(specimen_id,version_no,scientific_name,determination_basis,"
                    "content_sha256,disclosures_json,state,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?, 'in_review', ?,?)",
                    (
                        specimen_id, version_no, normalized_name, determination_basis.strip(),
                        digest, canonical_json(declared), actor_id, self._now(),
                    ),
                )
                draft_id = cursor.lastrowid
                for row in citations:
                    self.connection.execute(
                        "INSERT INTO draft_citations(draft_id,evidence_id,cited_at) VALUES(?,?,?)",
                        (draft_id, row["evidence_id"], self._now()),
                    )
                self._audit(
                    "determination", str(draft_id), "determination.draft_created", actor_id,
                    {
                        "specimen_id": specimen_id,
                        "version_no": version_no,
                        "scientific_name": normalized_name,
                        "content_sha256": digest,
                        "evidence_ids": sorted(ids),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("鉴定稿版本冲突，请重试") from exc
        return self._serialize_draft(self._draft(draft_id))

    def _publish_blockers(
        self,
        draft: sqlite3.Row,
        signoffs: list[sqlite3.Row],
        citations: list[sqlite3.Row],
        actor_id: str,
    ) -> list[dict[str, str]]:
        """汇总发布阻断原因：利益回避、证据缺口、学名冲突与重复决定。"""

        reasons: list[dict[str, str]] = []
        disclosures = set(json.loads(draft["disclosures_json"]))
        if actor_id == draft["created_by"] or actor_id in disclosures:
            reasons.append({
                "code": "conflict_of_interest",
                "message": f"签署人 {actor_id} 与该鉴定稿存在利益关系（起草人或已申报利益方），应当回避",
            })
        # 证据缺口与学名冲突只在终审发布时阻断；利益回避在任何签署阶段都阻断。
        if len(signoffs) == len(DETERMINATION_STAGES) - 1:
            withdrawn = [row["evidence_id"] for row in citations if row["status"] != "active"]
            if withdrawn:
                reasons.append({
                    "code": "evidence_gap",
                    "message": f"引用的证据已被撤回: {withdrawn}",
                })
            active_types = {row["evidence_type"] for row in citations if row["status"] == "active"}
            for required in REQUIRED_CITATION_TYPES:
                if required not in active_types:
                    reasons.append({
                        "code": "evidence_gap",
                        "message": f"缺少有效的 {required} 类证据引用",
                    })
            current = self.connection.execute(
                "SELECT draft_id,version_no,scientific_name,content_sha256 FROM determination_drafts "
                "WHERE specimen_id=? AND state='published'",
                (draft["specimen_id"],),
            ).fetchone()
            if current is not None:
                if current["content_sha256"] == draft["content_sha256"]:
                    reasons.append({
                        "code": "duplicate_determination",
                        "message": f"与当前有效版本 v{current['version_no']} 内容一致，不得生成第二份决定",
                    })
                else:
                    reasons.append({
                        "code": "name_conflict",
                        "message": (
                            f"标本已存在当前有效鉴定 v{current['version_no']}"
                            f"（{current['scientific_name']}），新结论与之冲突，须先使旧版本失效"
                        ),
                    })
        return reasons

    def sign_determination(self, actor_id: str, draft_id: int, comment: str) -> dict[str, Any]:
        draft = self._draft(draft_id)
        signoffs = self._draft_signoffs(draft_id)
        # 重复签署幂等：同一签署人重复提交返回既有结果，不生成第二份决定。
        if any(row["signer_id"] == actor_id for row in signoffs):
            return self._serialize_draft(draft)
        if draft["state"] != "in_review":
            raise InvalidState("鉴定稿已发布或已失效，不能继续签署")
        if len(signoffs) >= len(DETERMINATION_STAGES):
            raise InvalidState("鉴定稿签署流程已结束")
        stage = DETERMINATION_STAGES[len(signoffs)]
        self._require(actor_id, STAGE_PERMISSIONS[stage])
        if not isinstance(comment, str) or not comment.strip():
            raise ValidationFailed("签署意见不能为空")
        citations = self._draft_citations(draft_id)
        blockers = self._publish_blockers(draft, signoffs, citations, actor_id)
        if blockers:
            raise PublishBlocked(blockers)
        try:
            with transaction(self.connection, immediate=True):
                current_count = self.connection.execute(
                    "SELECT count(*) FROM draft_signoffs WHERE draft_id=?", (draft_id,)
                ).fetchone()[0]
                if current_count != len(signoffs):
                    raise Conflict("签署状态已变化，请重试")
                self.connection.execute(
                    "INSERT INTO draft_signoffs(draft_id,stage,signer_id,comment,signed_at) VALUES(?,?,?,?,?)",
                    (draft_id, stage, actor_id, comment.strip(), self._now()),
                )
                self._audit(
                    "determination", str(draft_id), "determination.signoff_recorded", actor_id,
                    {"stage": stage, "stage_label": STAGE_LABELS[stage]},
                )
                if stage == DETERMINATION_STAGES[-1]:
                    cursor = self.connection.execute(
                        "UPDATE determination_drafts SET state='published',published_at=? "
                        "WHERE draft_id=? AND state='in_review'",
                        (self._now(), draft_id),
                    )
                    if cursor.rowcount != 1:
                        raise Conflict("鉴定稿状态已变化，请重试")
                    self._audit(
                        "determination", str(draft_id), "determination.published", actor_id,
                        {"version_no": draft["version_no"], "scientific_name": draft["scientific_name"]},
                    )
        except sqlite3.IntegrityError as exc:
            raise Conflict("签署冲突：同一阶段只能签署一次，且标本只能有一个当前有效版本") from exc
        return self._serialize_draft(self._draft(draft_id))

    def current_determination(self, specimen_id: str) -> dict[str, Any]:
        """科普内容使用的当前结论；无需角色，无有效版本时返回最近失效版本。"""

        specimen = self._specimen(specimen_id)
        row = self.connection.execute(
            "SELECT * FROM determination_drafts WHERE specimen_id=? AND state='published'",
            (specimen_id,),
        ).fetchone()
        invalidated = None
        if row is None:
            invalidated_row = self.connection.execute(
                "SELECT * FROM determination_drafts WHERE specimen_id=? AND state='invalidated' "
                "ORDER BY version_no DESC LIMIT 1",
                (specimen_id,),
            ).fetchone()
            if invalidated_row is not None:
                invalidated = self._serialize_draft(invalidated_row)
        return {
            "specimen_id": specimen["specimen_id"],
            "catalog_number": specimen["catalog_number"],
            "common_name": specimen["common_name"],
            "current": None if row is None else self._serialize_draft(row),
            "last_invalidated": invalidated,
        }

    def _require_history_reader(self, actor_id: str) -> None:
        user = self._user(actor_id)
        if user["role"] not in DETERMINATION_HISTORY_ROLES:
            raise Forbidden("当前角色不能追溯鉴定历史")

    def determination_history(self, actor_id: str, specimen_id: str) -> dict[str, Any]:
        """研究人员追溯每次修订、签署人与引用依据。"""

        self._require_history_reader(actor_id)
        specimen = self._specimen(specimen_id)
        drafts = self.connection.execute(
            "SELECT * FROM determination_drafts WHERE specimen_id=? ORDER BY version_no",
            (specimen_id,),
        ).fetchall()
        evidence_rows = self.connection.execute(
            "SELECT * FROM specimen_evidence WHERE specimen_id=? ORDER BY evidence_id",
            (specimen_id,),
        ).fetchall()
        events = self.connection.execute(
            "SELECT event_type,entity_id,actor_id,payload_json,created_at FROM audit_events "
            "WHERE (entity_type='determination' AND entity_id IN "
            "(SELECT CAST(draft_id AS TEXT) FROM determination_drafts WHERE specimen_id=?)) "
            "OR (entity_type='specimen_evidence' AND entity_id IN "
            "(SELECT CAST(evidence_id AS TEXT) FROM specimen_evidence WHERE specimen_id=?)) "
            "OR (entity_type='specimen' AND entity_id=?) "
            "ORDER BY event_id",
            (specimen_id, specimen_id, specimen_id),
        ).fetchall()
        return {
            "specimen": dict(specimen),
            "evidence": [dict(row) for row in evidence_rows],
            "drafts": [self._serialize_draft(row) for row in drafts],
            "events": [dict(row) | {"payload": json.loads(row["payload_json"])} for row in events],
        }

    def get_determination_draft(self, actor_id: str, draft_id: int) -> dict[str, Any]:
        self._require_history_reader(actor_id)
        return self._serialize_draft(self._draft(draft_id))
