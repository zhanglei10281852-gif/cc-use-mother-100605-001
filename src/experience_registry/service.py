"""业务服务层：经验条目的提交、复核、发布、撤回、合并与审计。

每个公开方法都是一个完整事务，权限在事务内实时校验（角色存于数据库，
授权/撤权立即生效）。所有状态迁移都会写入 events 表，使任何一条经验
从提交到生效、再到过期或撤回的完整链路都可以回放。
"""

from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import secrets
import uuid

from .errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from .models import (
    ENTRY_ACTIVE,
    ENTRY_MERGED,
    MERGE_ACCEPTED,
    MERGE_DISMISSED,
    MERGE_OPEN,
    PERMISSIONS,
    ROLES,
    TASK_APPROVED,
    TASK_CANCELLED,
    TASK_PENDING,
    TASK_REJECTED,
    VERSION_APPROVED,
    VERSION_EXPIRED,
    VERSION_PENDING,
    VERSION_REJECTED,
    VERSION_SUPERSEDED,
    VERSION_WITHDRAWN,
)
from .storage import Database
from .textnorm import SIMILARITY_THRESHOLD, entry_similarity, normalize_model, normalize_text

_FIELD_LIMIT = 2000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{2,64}$")
_REQUIRED_FIELDS = ("equipment_model", "process_step", "applicable_conditions", "evidence_source", "content")


def _new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _hash_password(password, salt):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 100_000).hex()


class KnowledgeService:
    def __init__(self, db_path, *, clock=None):
        self.db = Database(db_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------
    def _now(self):
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat(timespec="milliseconds")

    def _today(self):
        dt = self._clock()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date().isoformat()

    @staticmethod
    def _event(conn, ts, actor, action, entity_type, entity_id, entry_id=None, detail=None):
        conn.execute(
            "INSERT INTO events(ts, actor, action, entity_type, entity_id, entry_id, detail_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (ts, actor, action, entity_type, entity_id, entry_id, json.dumps(detail or {}, ensure_ascii=False)),
        )

    def _authorize(self, conn, token, permission):
        """解析令牌并实时从库中读取角色做权限校验。"""
        if not token:
            raise AuthenticationError("缺少访问令牌")
        row = conn.execute(
            "SELECT t.token, t.revoked, t.expires_at, u.id, u.username, u.display_name, u.active"
            " FROM tokens t JOIN users u ON u.id = t.user_id WHERE t.token = ?",
            (token,),
        ).fetchone()
        if row is None or row["revoked"] or not row["active"]:
            raise AuthenticationError("令牌无效或已注销")
        if row["expires_at"] and row["expires_at"] <= self._now():
            raise AuthenticationError("令牌已过期")
        roles = {r["role"] for r in conn.execute("SELECT role FROM user_roles WHERE user_id = ?", (row["id"],))}
        allowed = PERMISSIONS[permission]
        if not (roles & allowed):
            raise PermissionDeniedError(
                f"权限不足：需要 {'/'.join(sorted(allowed))} 角色",
                detail={"permission": permission},
            )
        return {"id": row["id"], "username": row["username"], "display_name": row["display_name"], "roles": roles}

    # ------------------------------------------------------------------
    # 用户、角色与令牌
    # ------------------------------------------------------------------
    def bootstrap_admin(self, username="admin", password="admin123", display_name="系统管理员"):
        """仅在系统无任何用户时创建初始管理员，重复调用结果不变。"""
        with self.db.write() as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]
            if count:
                return {"created": False, "username": username}
            self._create_user(conn, "system", username=username, password=password,
                              display_name=display_name, roles=list(ROLES))
            return {"created": True, "username": username}

    def _create_user(self, conn, actor, *, username, password, display_name, roles):
        username = (username or "").strip()
        if not _USERNAME_RE.match(username):
            raise ValidationError("用户名需为 2-64 位字母、数字、点、下划线或短横线")
        if not password or len(password) < 6:
            raise ValidationError("密码长度至少 6 位")
        unknown = set(roles) - set(ROLES)
        if unknown:
            raise ValidationError(f"未知角色：{sorted(unknown)}")
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise ConflictError("用户名已存在", code="USER_EXISTS")
        uid = _new_id("usr")
        salt = secrets.token_hex(16)
        ts = self._now()
        conn.execute(
            "INSERT INTO users(id, username, display_name, password_hash, salt, active, created_at)"
            " VALUES (?, ?, ?, ?, ?, 1, ?)",
            (uid, username, (display_name or username).strip(), _hash_password(password, salt), salt, ts),
        )
        for role in roles:
            conn.execute(
                "INSERT INTO user_roles(user_id, role, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                (uid, role, actor, ts),
            )
        self._event(conn, ts, actor, "USER_CREATED", "user", uid, detail={"username": username, "roles": list(roles)})
        return uid

    def create_user(self, token, *, username, password, display_name=None, roles=()):
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "user.manage")
            uid = self._create_user(conn, ctx["username"], username=username, password=password,
                                    display_name=display_name, roles=list(roles))
            return {"id": uid, "username": username.strip()}

    def list_users(self, token):
        with self.db.read() as conn:
            self._authorize(conn, token, "user.manage")
            rows = conn.execute("SELECT * FROM users ORDER BY username").fetchall()
            result = []
            for row in rows:
                roles = [r["role"] for r in conn.execute(
                    "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (row["id"],))]
                result.append({
                    "id": row["id"], "username": row["username"], "display_name": row["display_name"],
                    "active": bool(row["active"]), "roles": roles, "created_at": row["created_at"],
                })
            return result

    def change_role(self, token, username, role, action):
        """授权或撤权。角色存库、每次请求实时校验，因此变更立即生效。"""
        if role not in ROLES:
            raise ValidationError(f"未知角色：{role}")
        if action not in ("grant", "revoke"):
            raise ValidationError("action 只能是 grant 或 revoke")
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "user.manage")
            user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            if user is None:
                raise NotFoundError(f"用户不存在：{username}")
            ts = self._now()
            if action == "grant":
                conn.execute(
                    "INSERT OR IGNORE INTO user_roles(user_id, role, granted_by, granted_at) VALUES (?, ?, ?, ?)",
                    (user["id"], role, ctx["username"], ts),
                )
                self._event(conn, ts, ctx["username"], "ROLE_GRANTED", "user", user["id"],
                            detail={"username": username, "role": role})
            else:
                conn.execute("DELETE FROM user_roles WHERE user_id = ? AND role = ?", (user["id"], role))
                self._event(conn, ts, ctx["username"], "ROLE_REVOKED", "user", user["id"],
                            detail={"username": username, "role": role})
            roles = [r["role"] for r in conn.execute(
                "SELECT role FROM user_roles WHERE user_id = ? ORDER BY role", (user["id"],))]
            return {"username": username, "roles": roles}

    def authenticate(self, username, password):
        with self.db.write() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", ((username or "").strip(),)
            ).fetchone()
            if row is None or not row["active"]:
                raise AuthenticationError("用户名或密码错误")
            if not hmac.compare_digest(row["password_hash"], _hash_password(password or "", row["salt"])):
                raise AuthenticationError("用户名或密码错误")
            token = f"tok_{secrets.token_hex(16)}"
            ts = self._now()
            conn.execute(
                "INSERT INTO tokens(token, user_id, created_at) VALUES (?, ?, ?)",
                (token, row["id"], ts),
            )
            self._event(conn, ts, row["username"], "LOGIN", "user", row["id"])
            return {"token": token, "username": row["username"], "display_name": row["display_name"]}

    # ------------------------------------------------------------------
    # 条目提交与修订
    # ------------------------------------------------------------------
    @staticmethod
    def _validate_fields(fields):
        cleaned = {}
        for name in _REQUIRED_FIELDS:
            value = (fields.get(name) or "").strip()
            if not value:
                raise ValidationError(f"字段 {name} 不能为空")
            if len(value) > _FIELD_LIMIT:
                raise ValidationError(f"字段 {name} 超长（上限 {_FIELD_LIMIT} 字符）")
            cleaned[name] = value
        terms = fields.get("terms") or []
        if not isinstance(terms, (list, tuple)) or any(not isinstance(t, str) for t in terms):
            raise ValidationError("terms 必须是字符串数组")
        cleaned["terms"] = [t.strip() for t in terms if t.strip()][:50]
        valid_until = fields.get("valid_until")
        if valid_until:
            try:
                datetime.strptime(valid_until, "%Y-%m-%d")
            except (ValueError, TypeError):
                raise ValidationError("valid_until 需为 YYYY-MM-DD 格式")
        cleaned["valid_until"] = valid_until or None
        return cleaned

    def _insert_version(self, conn, ctx, entry_id, version_no, fields, base_version_id):
        vid = _new_id("ver")
        ts = self._now()
        conn.execute(
            "INSERT INTO versions(id, entry_id, version_no, equipment_model, process_step,"
            " applicable_conditions, evidence_source, content, terms_json, valid_until, status,"
            " base_version_id, created_by, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                vid, entry_id, version_no, fields["equipment_model"], fields["process_step"],
                fields["applicable_conditions"], fields["evidence_source"], fields["content"],
                json.dumps(fields["terms"], ensure_ascii=False), fields["valid_until"],
                VERSION_PENDING, base_version_id, ctx["username"], ts,
            ),
        )
        task_id = _new_id("rev")
        conn.execute(
            "INSERT INTO review_tasks(id, entry_id, version_id, required_role, status, created_at)"
            " VALUES (?, ?, ?, 'reviewer', ?, ?)",
            (task_id, entry_id, vid, TASK_PENDING, ts),
        )
        return vid, task_id, ts

    def submit_entry(self, token, *, source_ref=None, **fields):
        """提交经验条目：生成 v1、复核任务，并扫描相似条目给出合并建议。"""
        cleaned = self._validate_fields(fields)
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "entry.submit")
            if source_ref:
                dup = conn.execute("SELECT id FROM entries WHERE source_ref = ?", (source_ref,)).fetchone()
                if dup:
                    raise ConflictError("相同 source_ref 的记录已导入", code="DUPLICATE_SOURCE_REF",
                                        detail={"entry_id": dup["id"], "source_ref": source_ref})
            entry_id = _new_id("ent")
            entry_no = conn.execute("SELECT COALESCE(MAX(entry_no), 0) + 1 AS n FROM entries").fetchone()["n"]
            ts = self._now()
            conn.execute(
                "INSERT INTO entries(id, entry_no, status, source_ref, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (entry_id, entry_no, ENTRY_ACTIVE, source_ref, ctx["username"], ts),
            )
            vid, task_id, _ = self._insert_version(conn, ctx, entry_id, 1, cleaned, None)
            conn.execute("UPDATE entries SET latest_version_id = ? WHERE id = ?", (vid, entry_id))
            self._event(conn, ts, ctx["username"], "ENTRY_SUBMITTED", "entry", entry_id, entry_id,
                        detail={"version_id": vid, "version_no": 1,
                                "equipment_model": cleaned["equipment_model"],
                                "process_step": cleaned["process_step"]})
            version = self._get_version(conn, vid)
            suggestions = self._similarity_scan(conn, ts, ctx, version)
            return {
                "entry": self._entry_dict(conn, entry_id),
                "version": version,
                "review_task_id": task_id,
                "merge_suggestions": suggestions,
            }

    def revise_entry(self, token, entry_id, *, base_version_no, **fields):
        """基于指定版本修订。base_version_no 与当前最新版本不一致时返回
        VERSION_CONFLICT，保证并发修订有确定性的唯一胜者。"""
        cleaned = self._validate_fields(fields)
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "entry.revise")
            entry = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if entry is None:
                raise NotFoundError(f"条目不存在：{entry_id}")
            if entry["status"] == ENTRY_MERGED:
                raise ConflictError("条目已合并，请在主条目上修订", code="INVALID_STATE",
                                    detail={"canonical_entry_id": entry["canonical_entry_id"]})
            latest_no = conn.execute(
                "SELECT COALESCE(MAX(version_no), 0) AS n FROM versions WHERE entry_id = ?",
                (entry_id,),
            ).fetchone()["n"]
            if base_version_no != latest_no:
                raise ConflictError(
                    f"基于过期版本修订：当前最新为 v{latest_no}",
                    code="VERSION_CONFLICT",
                    detail={"latest_version_no": latest_no, "base_version_no": base_version_no},
                )
            base = conn.execute(
                "SELECT id FROM versions WHERE entry_id = ? AND version_no = ?", (entry_id, latest_no)
            ).fetchone()
            vid, task_id, ts = self._insert_version(conn, ctx, entry_id, latest_no + 1, cleaned, base["id"])
            conn.execute("UPDATE entries SET latest_version_id = ? WHERE id = ?", (vid, entry_id))
            self._event(conn, ts, ctx["username"], "VERSION_SUBMITTED", "version", vid, entry_id,
                        detail={"version_no": latest_no + 1, "base_version_no": base_version_no})
            version = self._get_version(conn, vid)
            suggestions = self._similarity_scan(conn, ts, ctx, version)
            return {
                "entry": self._entry_dict(conn, entry_id),
                "version": version,
                "review_task_id": task_id,
                "merge_suggestions": suggestions,
            }

    # ------------------------------------------------------------------
    # 复核
    # ------------------------------------------------------------------
    def pending_reviews(self, token):
        """待审队列。队列即数据库行，服务重启后结果不变。"""
        with self.db.read() as conn:
            self._authorize(conn, token, "review.decide")
            rows = conn.execute(
                "SELECT t.*, v.version_no, v.equipment_model, v.process_step, v.created_by AS submitted_by"
                " FROM review_tasks t JOIN versions v ON v.id = t.version_id"
                " WHERE t.status = ? ORDER BY t.created_at, t.id",
                (TASK_PENDING,),
            ).fetchall()
            return [dict(r) for r in rows]

    def decide_review(self, token, task_id, *, decision, rationale):
        """复核裁决。只有通过指定角色复核的版本才进入可检索目录；
        提交人不能复核本人版本。重复裁决返回稳定的 REVIEW_ALREADY_DECIDED。"""
        if decision not in ("approve", "reject"):
            raise ValidationError("decision 只能是 approve 或 reject")
        if not (rationale or "").strip():
            raise ValidationError("复核必须填写依据（rationale）")
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "review.decide")
            task = conn.execute("SELECT * FROM review_tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                raise NotFoundError(f"复核任务不存在：{task_id}")
            if task["status"] != TASK_PENDING:
                raise ConflictError(
                    "该复核任务已有裁决", code="REVIEW_ALREADY_DECIDED",
                    detail={"status": task["status"], "decided_by": task["decided_by"],
                            "decided_at": task["decided_at"]},
                )
            version = conn.execute("SELECT * FROM versions WHERE id = ?", (task["version_id"],)).fetchone()
            if version["created_by"] == ctx["username"]:
                raise PermissionDeniedError("不能复核本人提交的版本")
            ts = self._now()
            if decision == "approve":
                entry = conn.execute("SELECT * FROM entries WHERE id = ?", (task["entry_id"],)).fetchone()
                old_current = entry["current_version_id"]
                if old_current and old_current != version["id"]:
                    conn.execute("UPDATE versions SET status = ? WHERE id = ?",
                                 (VERSION_SUPERSEDED, old_current))
                    self._event(conn, ts, ctx["username"], "VERSION_SUPERSEDED", "version",
                                old_current, task["entry_id"],
                                detail={"superseded_by": version["id"]})
                conn.execute(
                    "UPDATE versions SET status = ?, reviewed_by = ?, reviewed_at = ?,"
                    " review_rationale = ? WHERE id = ?",
                    (VERSION_APPROVED, ctx["username"], ts, rationale.strip(), version["id"]),
                )
                conn.execute("UPDATE entries SET current_version_id = ? WHERE id = ?",
                             (version["id"], task["entry_id"]))
                conn.execute(
                    "UPDATE review_tasks SET status = ?, decided_by = ?, decided_at = ?, rationale = ?"
                    " WHERE id = ?",
                    (TASK_APPROVED, ctx["username"], ts, rationale.strip(), task_id),
                )
                self._event(conn, ts, ctx["username"], "REVIEW_APPROVED", "version", version["id"],
                            task["entry_id"],
                            detail={"version_no": version["version_no"], "rationale": rationale.strip()})
            else:
                conn.execute(
                    "UPDATE versions SET status = ?, reviewed_by = ?, reviewed_at = ?,"
                    " review_rationale = ? WHERE id = ?",
                    (VERSION_REJECTED, ctx["username"], ts, rationale.strip(), version["id"]),
                )
                conn.execute(
                    "UPDATE review_tasks SET status = ?, decided_by = ?, decided_at = ?, rationale = ?"
                    " WHERE id = ?",
                    (TASK_REJECTED, ctx["username"], ts, rationale.strip(), task_id),
                )
                self._event(conn, ts, ctx["username"], "REVIEW_REJECTED", "version", version["id"],
                            task["entry_id"],
                            detail={"version_no": version["version_no"], "rationale": rationale.strip()})
            return {"task_id": task_id, "decision": decision, "version_id": version["id"],
                    "decided_by": ctx["username"], "decided_at": ts}

    # ------------------------------------------------------------------
    # 撤回与过期
    # ------------------------------------------------------------------
    def withdraw_version(self, token, version_id, *, reason):
        """撤回版本。已生效版本由复核员/管理员撤回；待审版本提交人可自行撤回。
        撤回后退出可检索目录，但采用时的复核依据完整保留。"""
        if not (reason or "").strip():
            raise ValidationError("撤回必须填写原因（reason）")
        with self.db.write() as conn:
            version = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError(f"版本不存在：{version_id}")
            ts = self._now()
            if version["status"] == VERSION_PENDING:
                # 提交人撤回自己的待审版本
                ctx = self._authorize(conn, token, "entry.submit")
                if version["created_by"] != ctx["username"] and not (ctx["roles"] & PERMISSIONS["version.withdraw"]):
                    raise PermissionDeniedError("只能撤回本人提交的待审版本")
                conn.execute(
                    "UPDATE review_tasks SET status = ?, decided_by = ?, decided_at = ?, rationale = ?"
                    " WHERE version_id = ? AND status = ?",
                    (TASK_CANCELLED, ctx["username"], ts, "提交人撤回", version_id, TASK_PENDING),
                )
                self._event(conn, ts, ctx["username"], "REVIEW_TASK_CANCELLED", "version", version_id,
                            version["entry_id"], detail={"reason": reason.strip()})
            elif version["status"] == VERSION_APPROVED:
                ctx = self._authorize(conn, token, "version.withdraw")
            else:
                raise ConflictError(f"当前状态 {version['status']} 不可撤回", code="INVALID_STATE")
            conn.execute(
                "UPDATE versions SET status = ?, withdrawn_by = ?, withdrawn_at = ?, withdrawn_reason = ?"
                " WHERE id = ?",
                (VERSION_WITHDRAWN, ctx["username"], ts, reason.strip(), version_id),
            )
            conn.execute(
                "UPDATE entries SET current_version_id = NULL WHERE id = ? AND current_version_id = ?",
                (version["entry_id"], version_id),
            )
            self._event(conn, ts, ctx["username"], "VERSION_WITHDRAWN", "version", version_id,
                        version["entry_id"], detail={"reason": reason.strip(),
                                                     "version_no": version["version_no"]})
            return {"version_id": version_id, "status": VERSION_WITHDRAWN, "reason": reason.strip()}

    def expire_version(self, token, version_id, *, reason=None):
        """人工判定过期（如工艺换代）。自动过期见 sweep_expired。"""
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "version.expire")
            version = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
            if version is None:
                raise NotFoundError(f"版本不存在：{version_id}")
            if version["status"] != VERSION_APPROVED:
                raise ConflictError(f"当前状态 {version['status']} 不可置为过期", code="INVALID_STATE")
            ts = self._now()
            self._expire(conn, ts, ctx["username"], version, reason or "人工判定结论已过期")
            return {"version_id": version_id, "status": VERSION_EXPIRED}

    def _expire(self, conn, ts, actor, version, reason):
        conn.execute(
            "UPDATE versions SET status = ?, expired_at = ?, expired_reason = ? WHERE id = ?",
            (VERSION_EXPIRED, ts, reason, version["id"]),
        )
        conn.execute(
            "UPDATE entries SET current_version_id = NULL WHERE id = ? AND current_version_id = ?",
            (version["entry_id"], version["id"]),
        )
        self._event(conn, ts, actor, "VERSION_EXPIRED", "version", version["id"],
                    version["entry_id"], detail={"reason": reason, "version_no": version["version_no"]})

    def _sweep_expired(self, conn, ts, today):
        """有效期届满的生效版本自动过期，返回过期版本号列表（确定性输出）。"""
        rows = conn.execute(
            "SELECT * FROM versions WHERE status = ? AND valid_until IS NOT NULL AND valid_until < ?"
            " ORDER BY id",
            (VERSION_APPROVED, today),
        ).fetchall()
        for row in rows:
            self._expire(conn, ts, "system", row, f"有效期至 {row['valid_until']} 已届满")
        return [r["id"] for r in rows]

    def sweep_expired(self, token):
        with self.db.write() as conn:
            self._authorize(conn, token, "version.expire")
            expired = self._sweep_expired(conn, self._now(), self._today())
            return {"expired_version_ids": expired, "count": len(expired)}

    # ------------------------------------------------------------------
    # 检索与链路
    # ------------------------------------------------------------------
    def search_catalog(self, token, *, q=None, equipment_model=None, process_step=None):
        """检索目录：仅包含复核通过且当前生效的版本。检索前先做过期清扫。"""
        with self.db.write() as conn:
            self._authorize(conn, token, "catalog.read")
            self._sweep_expired(conn, self._now(), self._today())
            rows = conn.execute(
                "SELECT v.* FROM versions v JOIN entries e ON e.current_version_id = v.id"
                " WHERE v.status = ? AND e.status = ? ORDER BY e.entry_no",
                (VERSION_APPROVED, ENTRY_ACTIVE),
            ).fetchall()
            alias_map = self._alias_map(conn)
            nq = normalize_text(q, alias_map) if q else None
            nmodel = normalize_model(equipment_model) if equipment_model else None
            nproc = normalize_text(process_step, alias_map) if process_step else None
            result = []
            for row in rows:
                version = self._version_dict(row)
                if nmodel and normalize_model(version["equipment_model"]) != nmodel:
                    continue
                if nproc and nproc not in normalize_text(version["process_step"], alias_map):
                    continue
                if nq:
                    haystack = normalize_text(
                        " ".join([version["equipment_model"], version["process_step"],
                                  version["applicable_conditions"], version["content"],
                                  " ".join(version["terms"])]),
                        alias_map,
                    )
                    if nq not in haystack:
                        continue
                result.append(version)
            return result

    def get_entry(self, token, entry_id):
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            return self._entry_dict(conn, entry_id)

    def list_entries(self, token, *, status=None):
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            if status:
                rows = conn.execute(
                    "SELECT id FROM entries WHERE status = ? ORDER BY entry_no", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT id FROM entries ORDER BY entry_no").fetchall()
            return [self._entry_dict(conn, r["id"]) for r in rows]

    def entry_history(self, token, entry_id):
        """完整链路：所有版本、各自复核记录、相关合并建议与事件时间线。"""
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            entry = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
            if entry is None:
                raise NotFoundError(f"条目不存在：{entry_id}")
            versions = []
            for row in conn.execute(
                "SELECT * FROM versions WHERE entry_id = ? ORDER BY version_no", (entry_id,)):
                version = self._version_dict(row)
                task = conn.execute(
                    "SELECT * FROM review_tasks WHERE version_id = ?", (row["id"],)).fetchone()
                version["review_task"] = dict(task) if task else None
                versions.append(version)
            suggestions = [
                self._merge_dict(r) for r in conn.execute(
                    "SELECT * FROM merge_suggestions WHERE entry_id_a = ? OR entry_id_b = ?"
                    " ORDER BY created_at, id",
                    (entry_id, entry_id)).fetchall()
            ]
            timeline = [
                {"ts": e["ts"], "actor": e["actor"], "action": e["action"],
                 "entity_type": e["entity_type"], "entity_id": e["entity_id"],
                 "detail": json.loads(e["detail_json"])}
                for e in conn.execute(
                    "SELECT * FROM events WHERE entry_id = ? ORDER BY id", (entry_id,)).fetchall()
            ]
            return {
                "entry": self._entry_dict(conn, entry_id),
                "versions": versions,
                "merge_suggestions": suggestions,
                "timeline": timeline,
            }

    def explain_version(self, token, version_id):
        """解释一个版本：无论当前是否生效，都能说明当时为何被采用
        （复核人、复核依据、证据来源），以及后来为何退出目录。"""
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"版本不存在：{version_id}")
            version = self._version_dict(row)
            task = conn.execute(
                "SELECT * FROM review_tasks WHERE version_id = ?", (version_id,)).fetchone()
            adoption = None
            if version["reviewed_by"] and version["status"] != VERSION_REJECTED:
                adoption = {
                    "reviewed_by": version["reviewed_by"],
                    "reviewed_at": version["reviewed_at"],
                    "rationale": version["review_rationale"],
                    "evidence_source": version["evidence_source"],
                }
            retirement = None
            if version["status"] == VERSION_WITHDRAWN:
                retirement = {"kind": "withdrawn", "by": version["withdrawn_by"],
                              "at": version["withdrawn_at"], "reason": version["withdrawn_reason"]}
            elif version["status"] == VERSION_EXPIRED:
                retirement = {"kind": "expired", "at": version["expired_at"],
                              "reason": version["expired_reason"]}
            elif version["status"] == VERSION_SUPERSEDED:
                newer = conn.execute(
                    "SELECT version_no FROM versions WHERE entry_id = ? AND base_version_id = ?",
                    (version["entry_id"], version_id)).fetchone()
                retirement = {"kind": "superseded",
                              "by_version_no": newer["version_no"] if newer else None}
            events = [
                {"ts": e["ts"], "actor": e["actor"], "action": e["action"],
                 "detail": json.loads(e["detail_json"])}
                for e in conn.execute(
                    "SELECT * FROM events WHERE entity_id = ? ORDER BY id", (version_id,)).fetchall()
            ]
            return {
                "version": version,
                "review_task": dict(task) if task else None,
                "adoption": adoption,
                "retirement": retirement,
                "events": events,
            }

    # ------------------------------------------------------------------
    # 合并建议
    # ------------------------------------------------------------------
    def _alias_map(self, conn):
        return {r["alias"]: r["canonical"] for r in conn.execute("SELECT * FROM term_aliases")}

    def _similarity_scan(self, conn, ts, ctx, new_version):
        """把新版本与库中其他条目的代表版本比对，达到阈值即生成合并建议。
        同一对条目只保留一条 OPEN 建议（更新分数），已裁决的不再重开。"""
        alias_map = self._alias_map(conn)
        candidates = conn.execute(
            "SELECT v.* FROM versions v JOIN entries e ON e.id = v.entry_id"
            " WHERE e.id != ? AND e.status = ?"
            " AND v.id = COALESCE(e.current_version_id, e.latest_version_id)",
            (new_version["entry_id"], ENTRY_ACTIVE),
        ).fetchall()
        results = []
        for cand_row in candidates:
            cand = self._version_dict(cand_row)
            score, reasons = entry_similarity(new_version, cand, alias_map)
            if score < SIMILARITY_THRESHOLD:
                continue
            a, b = sorted([new_version["entry_id"], cand["entry_id"]])
            existing = conn.execute(
                "SELECT * FROM merge_suggestions WHERE entry_id_a = ? AND entry_id_b = ?", (a, b)
            ).fetchone()
            if existing is not None:
                if existing["status"] == MERGE_OPEN:
                    conn.execute(
                        "UPDATE merge_suggestions SET score = ?, reasons_json = ? WHERE id = ?",
                        (score, json.dumps(reasons, ensure_ascii=False), existing["id"]),
                    )
                    results.append(self._merge_dict(conn.execute(
                        "SELECT * FROM merge_suggestions WHERE id = ?", (existing["id"],)).fetchone()))
                continue
            sid = _new_id("mer")
            conn.execute(
                "INSERT INTO merge_suggestions(id, entry_id_a, entry_id_b, score, reasons_json,"
                " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, a, b, score, json.dumps(reasons, ensure_ascii=False), MERGE_OPEN, ts),
            )
            other = b if a == new_version["entry_id"] else a
            self._event(conn, ts, ctx["username"], "MERGE_SUGGESTED", "merge_suggestion", sid,
                        new_version["entry_id"],
                        detail={"other_entry_id": other, "score": score, "reasons": reasons})
            results.append(self._merge_dict(conn.execute(
                "SELECT * FROM merge_suggestions WHERE id = ?", (sid,)).fetchone()))
        return results

    def list_merge_suggestions(self, token, *, status=None):
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            if status:
                rows = conn.execute(
                    "SELECT * FROM merge_suggestions WHERE status = ? ORDER BY created_at, id",
                    (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM merge_suggestions ORDER BY created_at, id").fetchall()
            return [self._merge_dict(r) for r in rows]

    def accept_merge(self, token, suggestion_id, *, canonical_entry_id, note=None):
        """接受合并：指定主条目，另一条目标记为 MERGED 并退出检索目录；
        两条条目的全部原始版本都保留，可通过链路查询。"""
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "merge.decide")
            sug = conn.execute("SELECT * FROM merge_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            if sug is None:
                raise NotFoundError(f"合并建议不存在：{suggestion_id}")
            if sug["status"] != MERGE_OPEN:
                raise ConflictError("该合并建议已处理", code="MERGE_ALREADY_DECIDED",
                                    detail={"status": sug["status"]})
            pair = {sug["entry_id_a"], sug["entry_id_b"]}
            if canonical_entry_id not in pair:
                raise ValidationError("canonical_entry_id 必须是建议中的一方")
            merged_id = (pair - {canonical_entry_id}).pop()
            ts = self._now()
            conn.execute(
                "UPDATE entries SET status = ?, canonical_entry_id = ? WHERE id = ?",
                (ENTRY_MERGED, canonical_entry_id, merged_id),
            )
            conn.execute(
                "UPDATE merge_suggestions SET status = ?, decided_by = ?, decided_at = ?,"
                " decision_note = ?, canonical_entry_id = ? WHERE id = ?",
                (MERGE_ACCEPTED, ctx["username"], ts, note, canonical_entry_id, suggestion_id),
            )
            detail = {"canonical_entry_id": canonical_entry_id, "merged_entry_id": merged_id,
                      "note": note}
            self._event(conn, ts, ctx["username"], "MERGE_ACCEPTED", "merge_suggestion", suggestion_id,
                        canonical_entry_id, detail)
            self._event(conn, ts, ctx["username"], "MERGE_ACCEPTED", "merge_suggestion", suggestion_id,
                        merged_id, detail)
            return self._merge_dict(conn.execute(
                "SELECT * FROM merge_suggestions WHERE id = ?", (suggestion_id,)).fetchone())

    def dismiss_merge(self, token, suggestion_id, *, reason):
        if not (reason or "").strip():
            raise ValidationError("驳回合并建议必须填写原因")
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "merge.decide")
            sug = conn.execute("SELECT * FROM merge_suggestions WHERE id = ?", (suggestion_id,)).fetchone()
            if sug is None:
                raise NotFoundError(f"合并建议不存在：{suggestion_id}")
            if sug["status"] != MERGE_OPEN:
                raise ConflictError("该合并建议已处理", code="MERGE_ALREADY_DECIDED",
                                    detail={"status": sug["status"]})
            ts = self._now()
            conn.execute(
                "UPDATE merge_suggestions SET status = ?, decided_by = ?, decided_at = ?,"
                " decision_note = ? WHERE id = ?",
                (MERGE_DISMISSED, ctx["username"], ts, reason.strip(), suggestion_id),
            )
            self._event(conn, ts, ctx["username"], "MERGE_DISMISSED", "merge_suggestion", suggestion_id,
                        sug["entry_id_a"], detail={"reason": reason.strip()})
            return self._merge_dict(conn.execute(
                "SELECT * FROM merge_suggestions WHERE id = ?", (suggestion_id,)).fetchone())

    # ------------------------------------------------------------------
    # 批量导入（幂等）
    # ------------------------------------------------------------------
    def import_entries(self, token, *, batch_key, records):
        """批量导入。同一 batch_key 重放返回首次保存的结果，不产生重复数据；
        跨批次按 source_ref 去重。结果确定且可重放。"""
        if not (batch_key or "").strip():
            raise ValidationError("batch_key 不能为空")
        if not isinstance(records, list):
            raise ValidationError("records 必须是数组")
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "entry.import")
            existing = conn.execute(
                "SELECT result_json FROM import_batches WHERE batch_key = ?", (batch_key,)).fetchone()
            if existing is not None:
                return json.loads(existing["result_json"])
            result = {"batch_key": batch_key, "created": [], "duplicates": [], "errors": []}
            ts = self._now()
            for index, record in enumerate(records):
                source_ref = (record or {}).get("source_ref")
                try:
                    cleaned = self._validate_fields(record or {})
                except ValidationError as exc:
                    result["errors"].append({"index": index, "source_ref": source_ref,
                                             "code": exc.code, "message": exc.message})
                    continue
                if source_ref:
                    dup = conn.execute(
                        "SELECT id FROM entries WHERE source_ref = ?", (source_ref,)).fetchone()
                    if dup:
                        result["duplicates"].append({"index": index, "source_ref": source_ref,
                                                     "entry_id": dup["id"]})
                        continue
                entry_id = _new_id("ent")
                entry_no = conn.execute(
                    "SELECT COALESCE(MAX(entry_no), 0) + 1 AS n FROM entries").fetchone()["n"]
                conn.execute(
                    "INSERT INTO entries(id, entry_no, status, source_ref, created_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (entry_id, entry_no, ENTRY_ACTIVE, source_ref, ctx["username"], ts),
                )
                vid, _, _ = self._insert_version(conn, ctx, entry_id, 1, cleaned, None)
                conn.execute("UPDATE entries SET latest_version_id = ? WHERE id = ?", (vid, entry_id))
                self._event(conn, ts, ctx["username"], "ENTRY_SUBMITTED", "entry", entry_id, entry_id,
                            detail={"version_id": vid, "version_no": 1, "import_batch": batch_key,
                                    "source_ref": source_ref})
                version = self._get_version(conn, vid)
                self._similarity_scan(conn, ts, ctx, version)
                result["created"].append({"index": index, "source_ref": source_ref,
                                          "entry_id": entry_id, "version_id": vid})
            conn.execute(
                "INSERT INTO import_batches(batch_key, result_json, created_by, created_at)"
                " VALUES (?, ?, ?, ?)",
                (batch_key, json.dumps(result, ensure_ascii=False, sort_keys=True),
                 ctx["username"], ts),
            )
            self._event(conn, ts, ctx["username"], "IMPORT_COMPLETED", "import_batch", batch_key,
                        detail={"created": len(result["created"]),
                                "duplicates": len(result["duplicates"]),
                                "errors": len(result["errors"])})
            return result

    # ------------------------------------------------------------------
    # 术语别名
    # ------------------------------------------------------------------
    def add_term_alias(self, token, *, alias, canonical):
        alias, canonical = (alias or "").strip(), (canonical or "").strip()
        if not alias or not canonical:
            raise ValidationError("别名与标准术语都不能为空")
        with self.db.write() as conn:
            ctx = self._authorize(conn, token, "term.manage")
            conn.execute(
                "INSERT INTO term_aliases(alias, canonical) VALUES (?, ?)"
                " ON CONFLICT(alias) DO UPDATE SET canonical = excluded.canonical",
                (alias, canonical),
            )
            self._event(conn, self._now(), ctx["username"], "TERM_ALIAS_SET", "term_alias", alias,
                        detail={"canonical": canonical})
            return {"alias": alias, "canonical": canonical}

    def list_term_aliases(self, token):
        with self.db.read() as conn:
            self._authorize(conn, token, "catalog.read")
            return {r["alias"]: r["canonical"] for r in
                    conn.execute("SELECT * FROM term_aliases ORDER BY alias")}

    # ------------------------------------------------------------------
    # 行 -> 字典
    # ------------------------------------------------------------------
    def _get_version(self, conn, version_id):
        row = conn.execute("SELECT * FROM versions WHERE id = ?", (version_id,)).fetchone()
        return self._version_dict(row)

    @staticmethod
    def _version_dict(row):
        return {
            "id": row["id"],
            "entry_id": row["entry_id"],
            "version_no": row["version_no"],
            "equipment_model": row["equipment_model"],
            "process_step": row["process_step"],
            "applicable_conditions": row["applicable_conditions"],
            "evidence_source": row["evidence_source"],
            "content": row["content"],
            "terms": json.loads(row["terms_json"]),
            "valid_until": row["valid_until"],
            "status": row["status"],
            "base_version_id": row["base_version_id"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_rationale": row["review_rationale"],
            "withdrawn_by": row["withdrawn_by"],
            "withdrawn_at": row["withdrawn_at"],
            "withdrawn_reason": row["withdrawn_reason"],
            "expired_at": row["expired_at"],
            "expired_reason": row["expired_reason"],
        }

    def _entry_dict(self, conn, entry_id):
        row = conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"条目不存在：{entry_id}")
        current = self._get_version(conn, row["current_version_id"]) if row["current_version_id"] else None
        latest = self._get_version(conn, row["latest_version_id"]) if row["latest_version_id"] else None
        return {
            "id": row["id"],
            "entry_no": row["entry_no"],
            "label": f"KB-{row['entry_no']:04d}",
            "status": row["status"],
            "current_version": current,
            "latest_version": latest,
            "canonical_entry_id": row["canonical_entry_id"],
            "source_ref": row["source_ref"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _merge_dict(row):
        return {
            "id": row["id"],
            "entry_id_a": row["entry_id_a"],
            "entry_id_b": row["entry_id_b"],
            "score": row["score"],
            "reasons": json.loads(row["reasons_json"]),
            "status": row["status"],
            "created_at": row["created_at"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "decision_note": row["decision_note"],
            "canonical_entry_id": row["canonical_entry_id"],
        }
