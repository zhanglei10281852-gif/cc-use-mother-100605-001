"""SQLite 持久层：WAL、立即加锁写事务、崩溃后自动恢复。

设计要点：
- 所有写操作走 BEGIN IMMEDIATE，SQLite 写锁把并发提交串行化，重启不丢已提交事务；
- 内容哈希全局唯一，重复导入得到稳定的"已存在"结果；
- 事件表只增不改，event.seq 提供全库单调时序，重启后待审与链路均可重建；
- users/角色实时读取，权限变更即时生效；events 中固化 actor_role 快照。
"""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .hashing import sha256_hex

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    username   TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    seq      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       TEXT NOT NULL,
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,
    target   TEXT NOT NULL DEFAULT '',
    detail   TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS synonym_groups (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    words_json TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id         TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entry_versions (
    id            TEXT PRIMARY KEY,
    entry_id      TEXT NOT NULL REFERENCES entries(id),
    seq           INTEGER NOT NULL,
    submitter     TEXT NOT NULL,
    status        TEXT NOT NULL,
    title         TEXT NOT NULL,
    content_json  TEXT NOT NULL,
    content_hash  TEXT NOT NULL UNIQUE,
    expires_at    TEXT NOT NULL DEFAULT '',
    decision_note TEXT NOT NULL DEFAULT '',
    reviewer      TEXT NOT NULL DEFAULT '',
    merged_into   TEXT NOT NULL DEFAULT '',
    superseded_by TEXT NOT NULL DEFAULT '',
    rev           INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    decided_at    TEXT NOT NULL DEFAULT '',
    UNIQUE(entry_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_versions_entry ON entry_versions(entry_id, seq);
CREATE INDEX IF NOT EXISTS idx_versions_status ON entry_versions(status);

CREATE TABLE IF NOT EXISTS events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id   TEXT NOT NULL,
    version_id TEXT NOT NULL,
    kind       TEXT NOT NULL,
    actor      TEXT NOT NULL,
    actor_role TEXT NOT NULL,
    ts         TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_entry ON events(entry_id, seq);
CREATE INDEX IF NOT EXISTS idx_events_version ON events(version_id, seq);

CREATE TABLE IF NOT EXISTS merge_suggestions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    left_version_id   TEXT NOT NULL,
    right_version_id  TEXT NOT NULL,
    score             REAL NOT NULL,
    reasons_json      TEXT NOT NULL DEFAULT '[]',
    status            TEXT NOT NULL DEFAULT 'open',  -- open/accepted/dismissed
    decided_by        TEXT NOT NULL DEFAULT '',
    decided_at        TEXT NOT NULL DEFAULT '',
    reason            TEXT NOT NULL DEFAULT '',
    merged_version_id TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    UNIQUE(left_version_id, right_version_id)
);
CREATE INDEX IF NOT EXISTS idx_suggestions_status
    ON merge_suggestions(status, id);
"""

# 初始账号：开发/本地部署的引导口令，生产可通过管理员接口轮换
SEED_USERS = [
    ("admin", "admin", "token-admin"),
    ("engineer", "engineer", "token-engineer"),
    ("reviewer", "reviewer", "token-reviewer"),
]


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout=10000")
    # WAL 在服务重启/进程崩溃后仍可恢复；FULL 保证提交即落盘
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def initialize(db_path: str | Path, clock) -> None:
    """建表并写入种子数据。可重复调用、跨重启安全。"""
    conn = connect(db_path)
    try:
        # executescript 会先隐式提交任何打开的事务，DDL 单独执行
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('version', ?)",
                (str(SCHEMA_VERSION),))
            now = clock()
            for username, role, token in SEED_USERS:
                conn.execute(
                    """INSERT OR IGNORE INTO users
                       (username, role, token_hash, active, created_at, updated_at)
                       VALUES (?, ?, ?, 1, ?, ?)""",
                    (username, role, sha256_hex(token), now, now))
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
