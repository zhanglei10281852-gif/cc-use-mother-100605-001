"""SQLite 持久化层。

设计要点：
- 每个事务独立连接，便于多线程 HTTP 服务与服务重启后恢复；
- 写事务使用 BEGIN IMMEDIATE，提交前即锁定数据库，配合 busy_timeout
  让并发写得到确定性的先后结果；
- 所有待办（复核任务、合并建议）都是普通数据行，服务重启后天然保留。
"""

from contextlib import contextmanager
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    display_name  TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    salt          TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id    TEXT NOT NULL REFERENCES users(id),
    role       TEXT NOT NULL,
    granted_by TEXT,
    granted_at TEXT NOT NULL,
    PRIMARY KEY (user_id, role)
);

CREATE TABLE IF NOT EXISTS tokens (
    token      TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    expires_at TEXT,
    revoked    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS entries (
    id                 TEXT PRIMARY KEY,
    entry_no           INTEGER NOT NULL UNIQUE,
    status             TEXT NOT NULL,
    current_version_id TEXT,
    latest_version_id  TEXT,
    canonical_entry_id TEXT,
    source_ref         TEXT UNIQUE,
    created_by         TEXT NOT NULL,
    created_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    id                    TEXT PRIMARY KEY,
    entry_id              TEXT NOT NULL REFERENCES entries(id),
    version_no            INTEGER NOT NULL,
    equipment_model       TEXT NOT NULL,
    process_step          TEXT NOT NULL,
    applicable_conditions TEXT NOT NULL,
    evidence_source       TEXT NOT NULL,
    content               TEXT NOT NULL,
    terms_json            TEXT NOT NULL DEFAULT '[]',
    valid_until           TEXT,
    status                TEXT NOT NULL,
    base_version_id       TEXT,
    created_by            TEXT NOT NULL,
    created_at            TEXT NOT NULL,
    reviewed_by           TEXT,
    reviewed_at           TEXT,
    review_rationale      TEXT,
    withdrawn_by          TEXT,
    withdrawn_at          TEXT,
    withdrawn_reason      TEXT,
    expired_at            TEXT,
    expired_reason        TEXT,
    UNIQUE (entry_id, version_no)
);

CREATE TABLE IF NOT EXISTS review_tasks (
    id            TEXT PRIMARY KEY,
    entry_id      TEXT NOT NULL REFERENCES entries(id),
    version_id    TEXT NOT NULL UNIQUE REFERENCES versions(id),
    required_role TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    decided_by    TEXT,
    decided_at    TEXT,
    rationale     TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_tasks_status ON review_tasks(status);

CREATE TABLE IF NOT EXISTS merge_suggestions (
    id                 TEXT PRIMARY KEY,
    entry_id_a         TEXT NOT NULL,
    entry_id_b         TEXT NOT NULL,
    score              REAL NOT NULL,
    reasons_json       TEXT NOT NULL,
    status             TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    decided_by         TEXT,
    decided_at         TEXT,
    decision_note      TEXT,
    canonical_entry_id TEXT,
    UNIQUE (entry_id_a, entry_id_b)
);

CREATE TABLE IF NOT EXISTS term_aliases (
    alias     TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_batches (
    batch_key   TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    entry_id    TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_entry ON events(entry_id);
"""

# 预置班组同义术语，管理员可继续扩充
SEED_ALIASES = {
    "主轴抱死": "主轴卡死",
    "刀架": "刀塔",
    "冷却液": "切削液",
    "乳化液": "切削液",
    "滑道": "导轨",
    "变速箱": "齿轮箱",
    "轴承温升过高": "轴承发热",
    "卡盘松动": "卡盘松旷",
}


class Database:
    def __init__(self, path):
        self.path = str(path)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            for alias, canonical in SEED_ALIASES.items():
                conn.execute(
                    "INSERT OR IGNORE INTO term_aliases(alias, canonical) VALUES (?, ?)",
                    (alias, canonical),
                )
            conn.commit()
        finally:
            conn.close()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @contextmanager
    def write(self):
        """写事务：BEGIN IMMEDIATE，进入即取得写锁，异常自动回滚。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def read(self):
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()
