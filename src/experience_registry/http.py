"""HTTP 接口：仅依赖标准库 http.server，REST/JSON。

每个工作线程使用独立的 SQLite 连接（WAL 允许多读单写）；
写事务用 BEGIN IMMEDIATE 串行化，冲突返回 409，调用方可安全重试。
"""
from __future__ import annotations

import json
import re
import threading
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .db import connect, initialize
from .errors import RegistryError
from .service import RegistryService, utc_now

MAX_BODY_BYTES = 2 * 1024 * 1024


class _Store:
    """每线程一个连接；线程随请求结束而关闭连接。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        initialize(db_path, utc_now)

    def service(self) -> RegistryService:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.db_path)
            self._local.conn = conn
        return RegistryService(conn)


def _match(path: str, pattern: str) -> dict[str, str] | None:
    rx = re.sub(r"\{(\w+)\}", r"(?P<\1>[^/]+)", pattern)
    m = re.fullmatch(rx, path)
    return m.groupdict() if m else None


# (method, path-template, handler-name-without do_)
ROUTES: list[tuple[str, str, str]] = [
    ("GET", "/v1/health", "health"),
    ("POST", "/v1/entries", "submit"),
    ("POST", "/v1/entries/{entry_id}/revisions", "revise"),
    ("GET", "/v1/entries/{entry_id}", "get_entry"),
    ("GET", "/v1/pending", "pending"),
    ("GET", "/v1/directory", "directory"),
    ("GET", "/v1/versions/{version_id}", "get_version"),
    ("GET", "/v1/versions/{version_id}/chain", "chain"),
    ("POST", "/v1/versions/{version_id}/approve", "approve"),
    ("POST", "/v1/versions/{version_id}/reject", "reject"),
    ("POST", "/v1/versions/{version_id}/withdraw", "withdraw"),
    ("POST", "/v1/versions/{version_id}/expire", "expire"),
    ("GET", "/v1/suggestions", "suggestions"),
    ("POST", "/v1/suggestions/{suggestion_id}/merge", "merge_suggestion"),
    ("POST", "/v1/suggestions/{suggestion_id}/dismiss", "dismiss"),
    ("POST", "/v1/merges", "merge"),
    ("POST", "/v1/imports", "import_items"),
    ("GET", "/v1/synonyms", "synonyms"),
    ("POST", "/v1/admin/synonyms", "add_synonym"),
    ("GET", "/v1/admin/users", "list_users"),
    ("POST", "/v1/admin/users", "create_user"),
    ("PATCH", "/v1/admin/users/{username}", "update_user"),
    ("GET", "/v1/admin/audit", "audit"),
    ("POST", "/v1/admin/sweep-expired", "sweep_expired"),
]


class Handler(BaseHTTPRequestHandler):
    server_version = "ExperienceRegistry/0.1"

    # ---- 框架辅助 ----
    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise RegistryError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegistryError(f"请求体不是合法 JSON：{exc}") from exc
        if not isinstance(data, dict):
            raise RegistryError("请求体必须是 JSON 对象")
        return data

    def _actor(self) -> dict[str, str]:
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.startswith("Bearer ") else ""
        return self.store.service().authenticate_token(token)

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    # ---- 路由 ----
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PATCH(self) -> None:
        self._dispatch("PATCH")

    def _dispatch(self, method: str) -> None:
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            for route_method, pattern, name in ROUTES:
                if route_method != method:
                    continue
                kwargs = _match(parts.path, pattern)
                if kwargs is None:
                    continue
                if name == "health":
                    self._send_json({"status": "ok"})
                    return
                body = self._read_json() if method in ("POST", "PATCH") else {}
                actor = self._actor()
                result = getattr(self, f"do_{name}")(
                    actor, body, query, kwargs)
                self._send_json(result, 200)
                return
            self._send_json(
                {"error": {"code": "not_found", "message": "接口不存在"}}, 404)
        except RegistryError as exc:
            self._send_json(
                {"error": {"code": exc.code, "message": str(exc)}},
                exc.status_code)
        except (sqlite3.OperationalError,) as exc:
            # 写锁竞争等瞬时问题：明确的 503，客户端可重试
            self._send_json(
                {"error": {"code": "busy", "message": f"数据库繁忙：{exc}"}},
                503)

    @property
    def store(self) -> _Store:
        return self.server.store  # type: ignore[attr-defined]

    def svc(self) -> RegistryService:
        return self.store.service()

    # ---- 业务端点 ----
    @staticmethod
    def _unpack(body: dict) -> Any:
        # 支持两种请求体：直接给内容对象，或 {"content": <内容对象>, ...} 信封。
        # 内容本身也有一个文本字段叫 content，故只有嵌套对象才视为信封。
        nested = body.get("content")
        return nested if isinstance(nested, dict) else body

    def do_submit(self, actor, body, query, kwargs):
        return self.svc().submit(
            actor, self._unpack(body),
            expires_at=str(body.get("expires_at", "")))

    def do_revise(self, actor, body, query, kwargs):
        return self.svc().revise(actor, kwargs["entry_id"],
                                 self._unpack(body),
                                 expires_at=str(body.get("expires_at", "")))

    def do_get_entry(self, actor, body, query, kwargs):
        return self.svc().get_entry(kwargs["entry_id"])

    def do_pending(self, actor, body, query, kwargs):
        return {"items": self.svc().pending()}

    def do_directory(self, actor, body, query, kwargs):
        return {"items": self.svc().directory(
            equipment_model=query.get("equipment_model", ""),
            process=query.get("process", ""),
            keyword=query.get("keyword", ""),
            include_inactive=query.get("include_inactive") in ("1", "true"),
            limit=int(query.get("limit", "100")))}

    def do_get_version(self, actor, body, query, kwargs):
        return self.svc().get_version(kwargs["version_id"])

    def do_chain(self, actor, body, query, kwargs):
        return self.svc().version_chain(kwargs["version_id"])

    def do_approve(self, actor, body, query, kwargs):
        return self.svc().review(
            actor, kwargs["version_id"], True, str(body.get("note", "")))

    def do_reject(self, actor, body, query, kwargs):
        return self.svc().review(
            actor, kwargs["version_id"], False, str(body.get("note", "")))

    def do_withdraw(self, actor, body, query, kwargs):
        return self.svc().withdraw(actor, kwargs["version_id"],
                                   str(body.get("reason", "")))

    def do_expire(self, actor, body, query, kwargs):
        return self.svc().expire(actor, kwargs["version_id"],
                                 str(body.get("reason", "")))

    def do_suggestions(self, actor, body, query, kwargs):
        return {"items": self.svc().suggestions(
            query.get("status", "open"))}

    def do_merge_suggestion(self, actor, body, query, kwargs):
        nested = body.get("content")
        if not isinstance(nested, dict):
            raise RegistryError("合并必须在 content 中给出合并后的经验内容对象")
        return self.svc().merge(
            actor, int(kwargs["suggestion_id"]), [], nested,
            str(body.get("reason", "")))

    def do_dismiss(self, actor, body, query, kwargs):
        return self.svc().dismiss_suggestion(
            actor, int(kwargs["suggestion_id"]), str(body.get("reason", "")))

    def do_merge(self, actor, body, query, kwargs):
        ids = body.get("version_ids")
        if not isinstance(ids, list) or not ids:
            raise RegistryError("version_ids 必须是非空列表")
        return self.svc().merge(
            actor, None, [str(v) for v in ids], self._unpack(body),
            str(body.get("reason", "")))

    def do_import_items(self, actor, body, query, kwargs):
        items = body.get("items")
        if not isinstance(items, list):
            raise RegistryError("items 必须是列表")
        return {"results": self.svc().import_items(actor, items)}

    def do_synonyms(self, actor, body, query, kwargs):
        return {"items": self.svc().list_synonyms()}

    def do_add_synonym(self, actor, body, query, kwargs):
        words = body.get("words")
        if not isinstance(words, list):
            raise RegistryError("words 必须是列表")
        return self.svc().add_synonym_group(actor, [str(w) for w in words])

    def do_list_users(self, actor, body, query, kwargs):
        return {"items": self.svc().list_users(actor)}

    def do_create_user(self, actor, body, query, kwargs):
        return self.svc().create_user(
            actor, str(body.get("username", "")), str(body.get("role", "")),
            str(body.get("token", "")))

    def do_update_user(self, actor, body, query, kwargs):
        username = kwargs["username"]
        if "role" in body:
            return self.svc().change_user_role(
                actor, username, str(body["role"]))
        if "active" in body:
            return self.svc().set_user_active(
                actor, username, bool(body["active"]))
        raise RegistryError("需要提供 role 或 active 字段")

    def do_audit(self, actor, body, query, kwargs):
        return {"items": self.svc().audit_log(
            actor, int(query.get("limit", "100")))}

    def do_sweep_expired(self, actor, body, query, kwargs):
        return {"expired": self.svc().sweep_expired(
            body.get("today") or None)}


def build_server(host: str, port: int, db_path: str) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), Handler)
    server.store = _Store(db_path)  # type: ignore[attr-defined]
    # 启动时先扫描一次过期项，重启后到期结论自动失效且留下事件
    try:
        server.store.service().sweep_expired()
    except sqlite3.Error:
        pass
    return server


def serve(host: str = "127.0.0.1", port: int = 8080,
          db_path: str = "data/registry.db") -> None:
    server = build_server(host, port, db_path)
    print(f"经验知识库服务已启动：http://{host}:{port}  数据库：{db_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
