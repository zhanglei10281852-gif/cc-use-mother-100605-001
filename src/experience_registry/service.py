"""业务服务层：所有用例都在一个 BEGIN IMMEDIATE 事务内完成。

写事务由 SQLite 写锁串行化，因此"查重 -> 建版本 -> 发建议"这类读改写序列
在并发下仍有稳定结果：后来者要么看到先到者已提交的数据，要么收到明确的 409，
不会产生重复版本、重复建议或丢失状态迁移。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable

from .errors import (AuthError, Conflict, NotFound, PermissionDenied,
                     ValidationError)
from .hashing import canonical_json, sha256_hex
from .models import (ACTIVE, IN_REVIEW, MERGED, REJECTED, ROLES, SUPERSEDED,
                     WITHDRAWN, EXPIRED, VersionContent, assert_transition)
from .similarity import DEFAULT_SYNONYM_GROUPS, canonical_synonym_map, score_pair
from .db import transaction

Clock = Callable[[], str]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class RegistryService:
    SIMILARITY_THRESHOLD = 0.34

    def __init__(self, conn: sqlite3.Connection, clock: Clock = utc_now):
        self.conn = conn
        self.clock = clock

    # ============ 账号与权限 ============
    def authenticate_token(self, token: str) -> dict[str, str]:
        if not token:
            raise AuthError("缺少访问令牌")
        row = self.conn.execute(
            "SELECT username, role, active FROM users WHERE token_hash=?",
            (sha256_hex(token),)).fetchone()
        if row is None or not row["active"]:
            raise AuthError("令牌无效或账号已停用")
        return {"username": row["username"], "role": row["role"]}

    def _require_role(self, actor: dict[str, str], roles: tuple[str, ...],
                      what: str) -> None:
        # 角色实时读取，权限调整立即影响后续操作
        row = self.conn.execute(
            "SELECT role, active FROM users WHERE username=?",
            (actor["username"],)).fetchone()
        if row is None or not row["active"]:
            raise PermissionDenied("账号不可用")
        actor["role"] = row["role"]
        if row["role"] not in roles:
            raise PermissionDenied(f"{what}需要角色：{'/'.join(roles)}")

    def create_user(self, actor: dict[str, str], username: str, role: str,
                    token: str) -> dict[str, str]:
        self._require_role(actor, ("admin",), "创建账号")
        username = username.strip()
        token = token.strip()
        if not username or not token:
            raise ValidationError("用户名和令牌不能为空")
        if role not in ROLES:
            raise ValidationError(f"角色必须是 {ROLES} 之一")
        now = self.clock()
        with transaction(self.conn):
            exists = self.conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if exists:
                raise Conflict(f"账号 {username} 已存在")
            self.conn.execute(
                """INSERT INTO users(username, role, token_hash, active,
                                    created_at, updated_at)
                   VALUES(?, ?, ?, 1, ?, ?)""",
                (username, role, sha256_hex(token), now, now))
            self._audit(username, "user.create", username,
                        {"role": role})
        return {"username": username, "role": role}

    def change_user_role(self, actor: dict[str, str], username: str,
                         role: str) -> dict[str, str]:
        self._require_role(actor, ("admin",), "调整角色")
        if role not in ROLES:
            raise ValidationError(f"角色必须是 {ROLES} 之一")
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT role FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                raise NotFound(f"账号 {username} 不存在")
            self.conn.execute(
                "UPDATE users SET role=?, updated_at=? WHERE username=?",
                (role, self.clock(), username))
            self._audit(actor["username"], "user.role_change", username,
                        {"from": row["role"], "to": role})
        return {"username": username, "role": role}

    def set_user_active(self, actor: dict[str, str], username: str,
                        active: bool) -> dict[str, Any]:
        self._require_role(actor, ("admin",), "停用/启用账号")
        if username == actor["username"] and not active:
            raise ValidationError("不能停用自己的账号")
        with transaction(self.conn):
            row = self.conn.execute(
                "SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                raise NotFound(f"账号 {username} 不存在")
            self.conn.execute(
                "UPDATE users SET active=?, updated_at=? WHERE username=?",
                (1 if active else 0, self.clock(), username))
            self._audit(actor["username"],
                        "user.activate" if active else "user.deactivate",
                        username, {})
        return {"username": username, "active": active}

    def list_users(self, actor: dict[str, str]) -> list[dict[str, Any]]:
        self._require_role(actor, ("admin",), "查看账号")
        rows = self.conn.execute(
            "SELECT username, role, active, created_at FROM users ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]

    # ============ 同义词（术语归一化） ============
    def _synonym_map(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT words_json FROM synonym_groups WHERE active=1").fetchall()
        groups = [json.loads(r["words_json"]) for r in rows]
        groups.extend(DEFAULT_SYNONYM_GROUPS)
        return canonical_synonym_map(groups)

    def add_synonym_group(self, actor: dict[str, str],
                          words: list[str]) -> dict[str, Any]:
        self._require_role(actor, ("admin",), "维护同义词")
        clean = sorted({w.strip().casefold() for w in words if w.strip()})
        if len(clean) < 2:
            raise ValidationError("同义词组至少需要两个不同的词")
        now = self.clock()
        with transaction(self.conn):
            cur = self.conn.execute(
                """INSERT INTO synonym_groups(words_json, active, created_by,
                                              created_at)
                   VALUES(?, 1, ?, ?)""",
                (_json_dumps(clean), actor["username"], now))
            self._audit(actor["username"], "synonym.add", str(cur.lastrowid),
                        {"words": clean})
        return {"id": cur.lastrowid, "words": clean}

    def list_synonyms(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT id, words_json, created_by, created_at
               FROM synonym_groups WHERE active=1 ORDER BY id""").fetchall()
        return [{"id": r["id"], "words": json.loads(r["words_json"]),
                 "created_by": r["created_by"], "created_at": r["created_at"]}
                for r in rows]

    # ============ 提交 / 修订 ============
    def submit(self, actor: dict[str, str], raw_content: Any,
               *, expires_at: str = "", import_id: str = "",
               ) -> dict[str, Any]:
        """提交一条新经验。重复内容返回既有版本（导入幂等）。"""
        self._require_role(actor, ("admin", "engineer"), "提交经验")
        content = VersionContent.from_dict(raw_content)
        return self._submit(content, actor["username"],
                            event_kind="submitted", expires_at=expires_at,
                            import_id=import_id)

    def revise(self, actor: dict[str, str], entry_id: str,
               raw_content: Any, *, expires_at: str = "") -> dict[str, Any]:
        """对既有条目提交修订版本；条目同时只能有一个待审版本。"""
        self._require_role(actor, ("admin", "engineer"), "修订经验")
        content = VersionContent.from_dict(raw_content)
        with transaction(self.conn):
            entry = self.conn.execute(
                "SELECT id FROM entries WHERE id=?", (entry_id,)).fetchone()
            if entry is None:
                raise NotFound(f"条目 {entry_id} 不存在")
            pending = self.conn.execute(
                "SELECT id FROM entry_versions WHERE entry_id=? AND status=?",
                (entry_id, IN_REVIEW)).fetchone()
            if pending is not None:
                raise Conflict(
                    f"条目 {entry_id} 已有待审版本 {pending['id']}，"
                    "请等待复核结束后再修订")
            return self._submit_locked(content, actor["username"],
                                       entry_id=entry_id,
                                       event_kind="revision_submitted",
                                       expires_at=expires_at)

    def _submit(self, content: VersionContent, username: str, *,
                event_kind: str, entry_id: str | None = None,
                expires_at: str = "", import_id: str = "") -> dict[str, Any]:
        with transaction(self.conn):
            return self._submit_locked(
                content, username, event_kind=event_kind, entry_id=entry_id,
                expires_at=expires_at, import_id=import_id)

    def _submit_locked(self, content: VersionContent, username: str, *,
                       event_kind: str, entry_id: str | None = None,
                       expires_at: str = "", import_id: str = "",
                       ) -> dict[str, Any]:
        """调用方必须已持有写事务。"""
        now = self.clock()
        duplicate = self.conn.execute(
            "SELECT id, entry_id FROM entry_versions WHERE content_hash=?",
            (content.content_hash,)).fetchone()
        if duplicate is not None:
            # 稳定的去重结果：不新建任何数据，返回既有版本
            return {"duplicate": True,
                    "version": self.get_version(duplicate["id"])}

        if entry_id is None:
            entry_id = "ent_" + content.content_hash[:12]
            self.conn.execute(
                "INSERT INTO entries(id, created_at) VALUES(?, ?)",
                (entry_id, now))

        seq_row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM entry_versions "
            "WHERE entry_id=?", (entry_id,)).fetchone()
        seq = seq_row["next_seq"]
        version_id = "ver_" + content.content_hash[:16]
        self.conn.execute(
            """INSERT INTO entry_versions
               (id, entry_id, seq, submitter, status, title, content_json,
                content_hash, expires_at, rev, created_at)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (version_id, entry_id, seq, username, IN_REVIEW, content.title,
             canonical_json(content.as_dict()), content.content_hash,
             expires_at.strip(), now))
        self._event(entry_id, version_id, event_kind, username,
                    {"import_id": import_id} if import_id else {})
        result = {"duplicate": False, "version": self.get_version(version_id)}
        self._scan_similarities_locked(version_id, entry_id)
        return result

    def _representative_of_entry(self, entry_id: str) -> sqlite3.Row | None:
        """返回条目的当前代表版本：优先最新生效版本，否则最新待审版本。"""
        active = self.conn.execute(
            "SELECT * FROM entry_versions WHERE entry_id=? AND status=? "
            "ORDER BY seq DESC LIMIT 1",
            (entry_id, ACTIVE)).fetchone()
        if active is not None:
            return active
        return self.conn.execute(
            "SELECT * FROM entry_versions WHERE entry_id=? AND status=? "
            "ORDER BY seq DESC LIMIT 1",
            (entry_id, IN_REVIEW)).fetchone()

    def _scan_similarities_locked(self, version_id: str,
                                  entry_id: str) -> list[dict[str, Any]]:
        """与每个其他条目的代表版本（生效优先，否则待审）比较，超阈值即建建议。"""
        synonyms = self._synonym_map()
        new_row = self.conn.execute(
            "SELECT content_json FROM entry_versions WHERE id=?",
            (version_id,)).fetchone()
        new_content = VersionContent.from_dict(
            json.loads(new_row["content_json"]))
        other_entries = self.conn.execute(
            "SELECT DISTINCT entry_id FROM entry_versions WHERE entry_id != ?",
            (entry_id,)).fetchall()
        created: list[dict[str, Any]] = []
        for ent in other_entries:
            rep = self._representative_of_entry(ent["entry_id"])
            if rep is None:
                continue
            other = VersionContent.from_dict(json.loads(rep["content_json"]))
            score = score_pair(new_content, other, synonyms)
            if score < self.SIMILARITY_THRESHOLD:
                continue
            left, right = sorted([version_id, rep["id"]])
            reasons = self._similarity_reasons(new_content, other, synonyms)
            try:
                self.conn.execute(
                    """INSERT INTO merge_suggestions
                       (left_version_id, right_version_id, score, reasons_json,
                        status, created_at)
                       VALUES(?, ?, ?, ?, 'open', ?)""",
                    (left, right, score, _json_dumps(reasons), self.clock()))
            except sqlite3.IntegrityError:
                continue  # 同一对版本已有建议，保持原建议，结果幂等
            suggestion_id = self.conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]
            self._event(entry_id, version_id, "suggestion_created", "system",
                        {"other_version_id": rep["id"], "score": score,
                         "suggestion_id": suggestion_id})
            self._event(rep["entry_id"], rep["id"], "suggestion_created",
                        "system",
                        {"other_version_id": version_id, "score": score,
                         "suggestion_id": suggestion_id})
            created.append({"id": suggestion_id, "left": left,
                            "right": right, "score": score})
        return created

    @staticmethod
    def _similarity_reasons(left: VersionContent, right: VersionContent,
                            synonyms: dict[str, str]) -> list[str]:
        reasons: list[str] = []
        if left.equipment_model.casefold() == right.equipment_model.casefold():
            reasons.append(f"设备型号一致：{left.equipment_model}")
        if left.process.casefold() == right.process.casefold():
            reasons.append(f"工序一致：{left.process}")
        shared = sorted({c.casefold() for c in left.conditions}
                        & {c.casefold() for c in right.conditions})
        if shared:
            reasons.append("适用条件重叠：" + "、".join(shared[:3]))
        if not reasons:
            reasons.append("标题/结论术语归一化后高度相似")
        return reasons

    # ============ 复核与生命周期 ============
    def review(self, actor: dict[str, str], version_id: str, approve: bool,
               note: str = "") -> dict[str, Any]:
        self._require_role(actor, ("admin", "reviewer"), "复核")
        note = note.strip()
        if not approve and not note:
            raise ValidationError("驳回必须填写原因")
        with transaction(self.conn):
            version = self._load_version_row(version_id)
            self._assert_status(version, IN_REVIEW)
            now = self.clock()
            if approve:
                self.conn.execute(
                    """UPDATE entry_versions SET status=?, reviewer=?,
                           decision_note=?, decided_at=?, rev=rev+1
                       WHERE id=? AND rev=?""",
                    (ACTIVE, actor["username"], note, now, version_id,
                     version["rev"]))
                self._event(version["entry_id"], version_id, "approved",
                            actor["username"], {"note": note})
                # 同条目的旧生效版本被新版本替代（原始版本仍保留）
                old_active = self.conn.execute(
                    "SELECT id FROM entry_versions WHERE entry_id=? "
                    "AND status=? AND id != ?",
                    (version["entry_id"], ACTIVE, version_id)).fetchall()
                for old in old_active:
                    self._transition(old["id"], SUPERSEDED,
                                     version["entry_id"], actor["username"],
                                     extra={"superseded_by": version_id},
                                     column="superseded_by", ref=version_id)
            else:
                self._set_status(version, REJECTED, actor["username"],
                                 note=note)
                self._event(version["entry_id"], version_id, "rejected",
                            actor["username"], {"note": note})
            return {"version": self.get_version(version_id)}

    def withdraw(self, actor: dict[str, str], version_id: str,
                 reason: str = "") -> dict[str, Any]:
        self._require_role(actor, ("admin", "engineer"), "撤回")
        with transaction(self.conn):
            version = self._load_version_row(version_id)
            if actor["role"] != "admin" and version["submitter"] != actor["username"]:
                raise PermissionDenied("只能撤回自己提交的版本")
            self._assert_status(version, IN_REVIEW)
            self._set_status(version, WITHDRAWN, actor["username"],
                             note=reason)
            self._event(version["entry_id"], version_id, "withdrawn",
                        actor["username"], {"reason": reason})
            return {"version": self.get_version(version_id)}

    def expire(self, actor: dict[str, str], version_id: str,
               reason: str) -> dict[str, Any]:
        """把生效结论标记过期；原因与既往事件共同解释它当时为何被采用。"""
        self._require_role(actor, ("admin", "reviewer"), "标记过期")
        reason = reason.strip()
        if not reason:
            raise ValidationError("标记过期必须填写原因")
        with transaction(self.conn):
            version = self._load_version_row(version_id)
            self._assert_status(version, ACTIVE)
            self._set_status(version, EXPIRED, actor["username"], note=reason)
            self._event(version["entry_id"], version_id, "expired",
                        actor["username"], {"reason": reason})
            return {"version": self.get_version(version_id)}

    def merge(self, actor: dict[str, str], suggestion_id: int | None,
              version_ids: list[str], merged_content: Any,
              reason: str = "") -> dict[str, Any]:
        """执行合并：以复核人身份创建合并后的生效版本，源版本保留为 merged。"""
        self._require_role(actor, ("admin", "reviewer"), "执行合并")
        if suggestion_id is None and len(version_ids) < 2:
            raise ValidationError("合并至少需要两个版本")
        content = VersionContent.from_dict(merged_content)
        reason = reason.strip()
        with transaction(self.conn):
            if suggestion_id is not None:
                sug = self.conn.execute(
                    "SELECT * FROM merge_suggestions WHERE id=?",
                    (suggestion_id,)).fetchone()
                if sug is None:
                    raise NotFound(f"合并建议 {suggestion_id} 不存在")
                if sug["status"] != "open":
                    raise Conflict(f"建议已{sug['status']}，不能重复执行")
                pair = [sug["left_version_id"], sug["right_version_id"]]
            else:
                pair = list(dict.fromkeys(version_ids))
                if len(pair) < 2:
                    raise ValidationError("合并至少需要两个不同版本")

            sources = [self._load_version_row(v) for v in pair]
            for src in sources:
                if src["status"] not in (ACTIVE, IN_REVIEW):
                    raise Conflict(
                        f"版本 {src['id']} 状态为 {src['status']}，不能并入")

            now = self.clock()
            merged_entry_id = "ent_" + content.content_hash[:12]
            if self.conn.execute("SELECT 1 FROM entries WHERE id=?",
                                 (merged_entry_id,)).fetchone():
                raise Conflict("合并后的内容与既有版本完全相同，请直接采用原条目")
            self.conn.execute(
                "INSERT INTO entries(id, created_at) VALUES(?, ?)",
                (merged_entry_id, now))
            merged_version_id = "ver_" + content.content_hash[:16]
            self.conn.execute(
                """INSERT INTO entry_versions
                   (id, entry_id, seq, submitter, status, title, content_json,
                    content_hash, decision_note, reviewer, rev, created_at,
                    decided_at)
                   VALUES(?, ?, 1, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                (merged_version_id, merged_entry_id, actor["username"], ACTIVE,
                 content.title, canonical_json(content.as_dict()),
                 content.content_hash, reason or "由相似条目合并生成",
                 actor["username"], now, now))
            self._event(merged_entry_id, merged_version_id, "submitted",
                        actor["username"], {"via_merge": True})
            self._event(merged_entry_id, merged_version_id, "approved",
                        actor["username"],
                        {"note": reason or "合并即复核",
                         "sources": pair}, )

            for src in sources:
                assert_transition(src["status"], MERGED)
                self.conn.execute(
                    """UPDATE entry_versions SET status=?, merged_into=?,
                           decision_note=?, reviewer=?, decided_at=?,
                           rev=rev+1
                       WHERE id=? AND rev=?""",
                    (MERGED, merged_version_id,
                     f"并入 {merged_version_id}：{reason}".rstrip("："),
                     actor["username"], now, src["id"], src["rev"]))
                self._event(src["entry_id"], src["id"], "merged_away",
                            actor["username"],
                            {"merged_into": merged_version_id,
                             "reason": reason})
                # 同源条目的旧生效版本也一并替代
                sib = self.conn.execute(
                    "SELECT id, entry_id FROM entry_versions "
                    "WHERE entry_id=? AND status=? AND id != ?",
                    (src["entry_id"], ACTIVE, src["id"])).fetchall()
                for s in sib:
                    self._transition(s["id"], SUPERSEDED, s["entry_id"],
                                     actor["username"],
                                     extra={"superseded_by": merged_version_id},
                                     column="superseded_by",
                                     ref=merged_version_id)

            if suggestion_id is not None:
                self.conn.execute(
                    """UPDATE merge_suggestions SET status='accepted',
                           decided_by=?, decided_at=?, reason=?,
                           merged_version_id=? WHERE id=?""",
                    (actor["username"], now, reason, merged_version_id,
                     suggestion_id))
            return {"merged_entry_id": merged_entry_id,
                    "merged_version_id": merged_version_id,
                    "sources": pair,
                    "version": self.get_version(merged_version_id)}

    def dismiss_suggestion(self, actor: dict[str, str], suggestion_id: int,
                           reason: str = "") -> dict[str, Any]:
        self._require_role(actor, ("admin", "reviewer"), "处理合并建议")
        with transaction(self.conn):
            sug = self.conn.execute(
                "SELECT * FROM merge_suggestions WHERE id=?",
                (suggestion_id,)).fetchone()
            if sug is None:
                raise NotFound(f"合并建议 {suggestion_id} 不存在")
            if sug["status"] != "open":
                raise Conflict(f"建议已{sug['status']}")
            self.conn.execute(
                """UPDATE merge_suggestions SET status='dismissed',
                       decided_by=?, decided_at=?, reason=? WHERE id=?""",
                (actor["username"], self.clock(), reason.strip(),
                 suggestion_id))
            return {"id": suggestion_id, "status": "dismissed"}

    # ============ 查询：目录、链路、待审、建议 ============
    def directory(self, *, equipment_model: str = "", process: str = "",
                  keyword: str = "", include_inactive: bool = False,
                  limit: int = 100) -> list[dict[str, Any]]:
        sql = "SELECT v.id FROM entry_versions v WHERE 1=1"
        params: list[Any] = []
        if include_inactive:
            sql += (f" AND v.status IN ({','.join('?' * 4)})")
            params.extend([ACTIVE, EXPIRED, WITHDRAWN, REJECTED])
        else:
            sql += " AND v.status = ?"
            params.append(ACTIVE)
        if equipment_model:
            sql += " AND lower(json_extract(v.content_json, '$.equipment_model')) = ?"
            params.append(equipment_model.strip().casefold())
        if process:
            sql += " AND lower(json_extract(v.content_json, '$.process')) = ?"
            params.append(process.strip().casefold())
        if keyword:
            sql += (" AND (lower(v.title) LIKE ? OR "
                    "lower(json_extract(v.content_json, '$.content')) LIKE ? OR "
                    "lower(json_extract(v.content_json, '$.conclusion')) LIKE ?)")
            like = f"%{keyword.strip().casefold()}%"
            params.extend([like, like, like])
        sql += " ORDER BY v.decided_at DESC, v.id LIMIT ?"
        params.append(min(max(limit, 1), 500))
        rows = self.conn.execute(sql, params).fetchall()
        return [self.get_version(r["id"]) for r in rows]

    def pending(self) -> list[dict[str, Any]]:
        """待审事项直接从持久层读出，服务重启后结果不变。"""
        rows = self.conn.execute(
            """SELECT id FROM entry_versions WHERE status=?
               ORDER BY created_at, id""", (IN_REVIEW,)).fetchall()
        return [self.get_version(r["id"]) for r in rows]

    def suggestions(self, status: str = "open") -> list[dict[str, Any]]:
        if status not in ("open", "accepted", "dismissed", "all"):
            raise ValidationError("status 必须是 open/accepted/dismissed/all")
        sql = "SELECT * FROM merge_suggestions"
        params: list[Any] = []
        if status != "all":
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY score DESC, id"
        return [self._suggestion_dict(r)
                for r in self.conn.execute(sql, params).fetchall()]

    def get_version(self, version_id: str) -> dict[str, Any]:
        row = self._load_version_row(version_id)
        return self._version_dict(row)

    def get_entry(self, entry_id: str) -> dict[str, Any]:
        entry = self.conn.execute(
            "SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
        if entry is None:
            raise NotFound(f"条目 {entry_id} 不存在")
        versions = [
            self._version_dict(r) for r in self.conn.execute(
                "SELECT * FROM entry_versions WHERE entry_id=? ORDER BY seq",
                (entry_id,)).fetchall()
        ]
        return {"id": entry_id, "created_at": entry["created_at"],
                "versions": versions,
                "timeline": self.timeline(entry_id=entry_id)}

    def timeline(self, *, entry_id: str | None = None,
                 version_id: str | None = None) -> list[dict[str, Any]]:
        sql = ("SELECT seq, entry_id, version_id, kind, actor, actor_role, "
               "ts, detail FROM events")
        params: list[Any] = []
        if version_id:
            sql += " WHERE version_id=?"
            params.append(version_id)
        elif entry_id:
            sql += " WHERE entry_id=?"
            params.append(entry_id)
        else:
            raise ValidationError("必须指定 entry_id 或 version_id")
        sql += " ORDER BY seq"
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def version_chain(self, version_id: str) -> dict[str, Any]:
        """一条经验从提交到生效的完整链路。"""
        version = self.get_version(version_id)
        return {"version": version,
                "entry": self.get_entry(version["entry_id"]),
                "timeline": self.timeline(version_id=version_id)}

    def audit_log(self, actor: dict[str, str], limit: int = 100
                  ) -> list[dict[str, Any]]:
        self._require_role(actor, ("admin",), "查看审计日志")
        rows = self.conn.execute(
            "SELECT seq, ts, actor, action, target, detail FROM audit_log "
            "ORDER BY seq DESC LIMIT ?", (min(max(limit, 1), 1000),)).fetchall()
        return [dict(r) for r in rows]

    # ============ 批量导入 ============
    def import_items(self, actor: dict[str, str],
                     items: list[Any]) -> list[dict[str, Any]]:
        """逐条提交，整批在一个事务内：重复内容稳定映射到既有版本。

        某一条内容非法会中止整批并回滚（全部成功或全部不生效），保证重放安全。
        """
        self._require_role(actor, ("admin", "engineer"), "导入经验")
        if not isinstance(items, list) or not items:
            raise ValidationError("导入内容必须是非空列表")
        # 先全部解析，避免脏数据进入事务
        parsed: list[tuple[VersionContent, str]] = []
        for index, raw in enumerate(items):
            if not isinstance(raw, dict):
                raise ValidationError(f"第 {index + 1} 条不是对象")
            content = VersionContent.from_dict(raw.get("content", raw))
            parsed.append((content, str(raw.get("import_id", "") or
                                         f"row-{index + 1}")))
        results: list[dict[str, Any]] = []
        with transaction(self.conn):
            for index, (content, import_id) in enumerate(parsed):
                outcomes = self._submit_locked(
                    content, actor["username"], event_kind="submitted",
                    import_id=import_id)
                results.append({
                    "import_id": import_id,
                    "index": index,
                    "duplicate": outcomes["duplicate"],
                    "version_id": outcomes["version"]["id"],
                    "entry_id": outcomes["version"]["entry_id"],
                    "status": outcomes["version"]["status"],
                })
            self._audit(actor["username"], "experience.import", "",
                        {"count": len(parsed),
                         "duplicates": sum(1 for r in results if r["duplicate"])})
        return results

    # ============ 过期扫描 ============
    def sweep_expired(self, today: str | None = None) -> list[dict[str, Any]]:
        """把 expires_at 已到期的生效版本标记为过期（无需登录，幂等）。

        服务重启后由启动钩子调用，也可由定时器/HTTP 触发；重复执行结果稳定。
        """
        if today is None:
            today = datetime.now(timezone.utc).date().isoformat()
        expired: list[dict[str, Any]] = []
        with transaction(self.conn):
            rows = self.conn.execute(
                "SELECT id, entry_id, expires_at FROM entry_versions "
                "WHERE status=? AND expires_at != '' AND expires_at <= ?",
                (ACTIVE, today)).fetchall()
            for row in rows:
                reason = f"到达失效日期 {row['expires_at']}，自动标记过期"
                self.conn.execute(
                    """UPDATE entry_versions SET status=?, decision_note=?,
                           reviewer='system', decided_at=?, rev=rev+1
                       WHERE id=? AND status=?""",
                    (EXPIRED, reason, self.clock(), row["id"], ACTIVE))
                self._event(row["entry_id"], row["id"], "expired", "system",
                            {"reason": reason, "auto": True})
                expired.append({"version_id": row["id"],
                                "expires_at": row["expires_at"]})
        return expired

    # ============ 内部辅助 ============
    def _load_version_row(self, version_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM entry_versions WHERE id=?", (version_id,)).fetchone()
        if row is None:
            raise NotFound(f"版本 {version_id} 不存在")
        return row

    @staticmethod
    def _assert_status(version: sqlite3.Row, expected: str) -> None:
        if version["status"] != expected:
            raise Conflict(
                f"版本 {version['id']} 当前状态为 {version['status']}，"
                f"需要 {expected}；该操作可能已被其他人完成")

    def _set_status(self, version: sqlite3.Row, new_status: str,
                    actor_name: str, *, note: str = "") -> None:
        assert_transition(version["status"], new_status)
        cur = self.conn.execute(
            """UPDATE entry_versions SET status=?, decision_note=?,
                   reviewer=?, decided_at=?, rev=rev+1
               WHERE id=? AND rev=?""",
            (new_status, note, actor_name, self.clock(), version["id"],
             version["rev"]))
        if cur.rowcount == 0:
            raise Conflict("版本已被并发修改，请刷新后重试")

    def _transition(self, version_id: str, new_status: str, entry_id: str,
                    actor_name: str, *, extra: dict[str, Any] | None = None,
                    column: str = "", ref: str = "") -> None:
        row = self._load_version_row(version_id)
        assert_transition(row["status"], new_status)
        if column:
            self.conn.execute(
                f"UPDATE entry_versions SET status=?, {column}=?, rev=rev+1 "
                "WHERE id=? AND rev=?",
                (new_status, ref, version_id, row["rev"]))
        else:
            self._set_status(row, new_status, actor_name)
        self._event(entry_id, version_id, new_status, actor_name, extra or {})

    def _event(self, entry_id: str, version_id: str, kind: str,
               actor: str, detail: dict[str, Any]) -> None:
        role = actor if actor == "system" else self._role_of(actor)
        self.conn.execute(
            """INSERT INTO events
               (entry_id, version_id, kind, actor, actor_role, ts, detail)
               VALUES(?, ?, ?, ?, ?, ?, ?)""",
            (entry_id, version_id, kind, actor, role, self.clock(),
             _json_dumps(detail)))

    def _role_of(self, username: str) -> str:
        row = self.conn.execute(
            "SELECT role FROM users WHERE username=?", (username,)).fetchone()
        return row["role"] if row else "unknown"

    def _audit(self, actor: str, action: str, target: str,
               detail: dict[str, Any]) -> None:
        self.conn.execute(
            """INSERT INTO audit_log(ts, actor, action, target, detail)
               VALUES(?, ?, ?, ?, ?)""",
            (self.clock(), actor, action, target, _json_dumps(detail)))

    def _version_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "entry_id": row["entry_id"],
            "seq": row["seq"],
            "status": row["status"],
            "submitter": row["submitter"],
            "reviewer": row["reviewer"],
            "title": row["title"],
            "content": json.loads(row["content_json"]),
            "content_hash": row["content_hash"],
            "expires_at": row["expires_at"],
            "decision_note": row["decision_note"],
            "merged_into": row["merged_into"],
            "superseded_by": row["superseded_by"],
            "rev": row["rev"],
            "created_at": row["created_at"],
            "decided_at": row["decided_at"],
        }

    def _suggestion_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "left_version_id": row["left_version_id"],
            "right_version_id": row["right_version_id"],
            "score": row["score"],
            "reasons": json.loads(row["reasons_json"]),
            "status": row["status"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "reason": row["reason"],
            "merged_version_id": row["merged_version_id"],
            "created_at": row["created_at"],
        }
