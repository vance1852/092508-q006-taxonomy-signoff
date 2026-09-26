"""分类实验观察采信服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 3

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_protocol_catalog (
    evidence_protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    task_family TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (evidence_protocol_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'statistician', 'approver', 'auditor', 'initial_reviewer', 'specialist_reviewer', 'final_reviewer')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS capture_devices (
    device_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS builds (
    build_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (device_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    evidence_protocol_id TEXT NOT NULL,
    evidence_protocol_version INTEGER NOT NULL,
    build_id TEXT NOT NULL REFERENCES builds(build_id),
    state TEXT NOT NULL CHECK (state IN ('draft', 'running', 'sealed', 'analyzing', 'analyzed', 'decided')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    started_at TEXT,
    sealed_at TEXT,
    FOREIGN KEY (evidence_protocol_id, evidence_protocol_version) REFERENCES evidence_protocol_catalog(evidence_protocol_id, version)
);

CREATE TABLE IF NOT EXISTS evidence_items (
    evidence_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    source_batch TEXT NOT NULL,
    source_row TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES capture_devices(device_id),
    evidence_group_key TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    indicators_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    imported_by TEXT NOT NULL REFERENCES users(user_id),
    imported_at TEXT NOT NULL,
    UNIQUE (batch_id, source_batch, source_row)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS exclusion_requests (
    exclusion_id INTEGER PRIMARY KEY AUTOINCREMENT,
    evidence_item_id INTEGER NOT NULL REFERENCES evidence_items(evidence_item_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'revoked')),
    reason TEXT NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(user_id),
    reviewed_at TEXT,
    review_note TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_exclusion_per_evidence_item
ON exclusion_requests(evidence_item_id)
WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision)
);

CREATE TABLE IF NOT EXISTS analyses (
    analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    batch_revision INTEGER NOT NULL,
    evidence_protocol_sha256 TEXT NOT NULL CHECK (length(evidence_protocol_sha256) = 64),
    input_sha256 TEXT NOT NULL CHECK (length(input_sha256) = 64),
    algorithm_version TEXT NOT NULL,
    seed INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (batch_id, batch_revision, input_sha256)
);

CREATE TABLE IF NOT EXISTS decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    analysis_id INTEGER NOT NULL REFERENCES analyses(analysis_id),
    decision TEXT NOT NULL CHECK (decision IN ('needs_more_data', 'approved', 'rejected')),
    reason TEXT NOT NULL,
    decided_by TEXT NOT NULL REFERENCES users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE (batch_id, analysis_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS specimens (
    specimen_id TEXT PRIMARY KEY,
    catalog_code TEXT NOT NULL,
    taxon_group TEXT NOT NULL,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (catalog_code)
);

-- 三类外部证据：初鉴形态记录、分子分析批次、模式照片。状态机 active <-> withdrawn。
CREATE TABLE IF NOT EXISTS specimen_evidence (
    evidence_id TEXT PRIMARY KEY,
    specimen_id TEXT NOT NULL REFERENCES specimens(specimen_id),
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('initial_identification', 'molecular_batch', 'type_photograph')),
    title TEXT NOT NULL,
    -- 对分子证据可登记 taxonomy_lab.analyses 的内容摘要，便于跨批次核对
    analysis_sha256 TEXT CHECK (analysis_sha256 IS NULL OR length(analysis_sha256) = 64),
    provided_by TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    withdrawn_reason TEXT,
    withdrawn_by TEXT REFERENCES users(user_id),
    withdrawn_at TEXT,
    registered_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (specimen_id, evidence_kind, content_sha256)
);

-- 每个标本的版本化鉴定稿；部分唯一索引保证至多一个草稿/一个当前有效版本。
CREATE TABLE IF NOT EXISTS determination_versions (
    version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    specimen_id TEXT NOT NULL REFERENCES specimens(specimen_id),
    version_no INTEGER NOT NULL CHECK (version_no > 0),
    scientific_name TEXT NOT NULL,
    authorship TEXT,
    rationale TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    -- draft：会签中或待发布；published：当前有效；rejected：终审驳回；superseded：被新版本接替；invalidated：证据变动失效
    status TEXT NOT NULL CHECK (status IN ('draft', 'published', 'rejected', 'superseded', 'invalidated')),
    invalidation_reason TEXT,
    invalidated_at TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    published_at TEXT,
    UNIQUE (specimen_id, version_no)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_draft_determination_per_specimen
ON determination_versions(specimen_id)
WHERE status = 'draft';

CREATE UNIQUE INDEX IF NOT EXISTS one_current_determination_per_specimen
ON determination_versions(specimen_id)
WHERE status = 'published';

-- 建稿时固化的证据引用快照，永不更新或删除（失效证据保留并标注状态）。
CREATE TABLE IF NOT EXISTS determination_evidence_refs (
    ref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    evidence_id TEXT NOT NULL,
    evidence_kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    analysis_sha256 TEXT,
    captured_status TEXT NOT NULL CHECK (captured_status IN ('active', 'withdrawn')),
    created_at TEXT NOT NULL,
    UNIQUE (version_id, evidence_id)
);

-- 顺序会签：initial_review -> specialist_review -> final_review。
CREATE TABLE IF NOT EXISTS determination_signoffs (
    signoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    stage TEXT NOT NULL CHECK (stage IN ('initial_review', 'specialist_review', 'final_review')),
    decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reviewer_id TEXT NOT NULL REFERENCES users(user_id),
    comment TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    UNIQUE (version_id, stage)
);

CREATE TABLE IF NOT EXISTS determination_reviewers (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    stage TEXT NOT NULL CHECK (stage IN ('initial_review', 'specialist_review', 'final_review')),
    assigned_at TEXT NOT NULL,
    PRIMARY KEY (user_id, stage)
);

-- 利益回避：登记后该用户不得会签对应标本的鉴定稿。
CREATE TABLE IF NOT EXISTS reviewer_conflicts (
    user_id TEXT NOT NULL REFERENCES users(user_id),
    specimen_id TEXT NOT NULL REFERENCES specimens(specimen_id),
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL REFERENCES users(user_id),
    declared_at TEXT NOT NULL,
    PRIMARY KEY (user_id, specimen_id)
);

-- 已裁定的学名归属：同一学名只能指向一个标本，供发布前冲突检查。
CREATE TABLE IF NOT EXISTS taxon_name_rulings (
    scientific_name TEXT PRIMARY KEY,
    specimen_id TEXT NOT NULL REFERENCES specimens(specimen_id),
    version_id INTEGER NOT NULL REFERENCES determination_versions(version_id),
    ruled_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "evidence_protocol_catalog", "users", "capture_devices", "builds", "batches",
    "evidence_items", "idempotency_keys", "exclusion_requests", "analysis_jobs",
    "analyses", "decisions", "audit_events",
    "specimens", "specimen_evidence", "determination_versions", "determination_evidence_refs",
    "determination_signoffs", "determination_reviewers", "reviewer_conflicts", "taxon_name_rulings",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。"""

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    # 多线程 HTTP 服务共享连接：写操作一律 BEGIN IMMEDIATE，配合 busy_timeout 串行化；
    # JsonApplication 另以锁保证同一时刻只有一个请求使用该连接。
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
