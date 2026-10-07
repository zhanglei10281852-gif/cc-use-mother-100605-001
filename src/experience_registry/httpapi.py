"""HTTP 接口：基于标准库 http.server 的 JSON API。

统一约定：
- 除 /api/health 与 /api/tokens 外都需要 `Authorization: Bearer <token>`；
- 错误响应格式稳定：{"error": {"code", "message", "detail"}}；
- 权限在每次请求时实时校验，角色变更立即生效。
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
from urllib.parse import parse_qs, urlparse

from .errors import KnowledgeError, ValidationError


class ApiApp:
    """路由与请求处理，与具体 socket 解耦，便于测试。"""

    def __init__(self, service):
        self.service = service
        self.routes = [
            ("GET", r"^/api/health$", self._health),
            ("POST", r"^/api/tokens$", self._login),
            ("GET", r"^/api/users$", self._users),
            ("POST", r"^/api/users$", self._create_user),
            ("POST", r"^/api/users/([^/]+)/roles$", self._change_role),
            ("POST", r"^/api/entries$", self._submit),
            ("GET", r"^/api/entries$", self._list_entries),
            ("GET", r"^/api/entries/([^/]+)$", self._get_entry),
            ("GET", r"^/api/entries/([^/]+)/history$", self._history),
            ("POST", r"^/api/entries/([^/]+)/revisions$", self._revise),
            ("GET", r"^/api/reviews/pending$", self._pending),
            ("POST", r"^/api/reviews/([^/]+)/decision$", self._decide),
            ("POST", r"^/api/versions/([^/]+)/withdraw$", self._withdraw),
            ("POST", r"^/api/versions/([^/]+)/expire$", self._expire),
            ("GET", r"^/api/versions/([^/]+)/explain$", self._explain),
            ("GET", r"^/api/catalog/search$", self._search),
            ("GET", r"^/api/merges$", self._merges),
            ("POST", r"^/api/merges/([^/]+)/accept$", self._merge_accept),
            ("POST", r"^/api/merges/([^/]+)/dismiss$", self._merge_dismiss),
            ("POST", r"^/api/imports$", self._import),
            ("POST", r"^/api/maintenance/sweep-expired$", self._sweep),
            ("GET", r"^/api/terms/aliases$", self._aliases),
            ("POST", r"^/api/terms/aliases$", self._add_alias),
        ]

    def handle(self, method, raw_path, headers, body):
        path = urlparse(raw_path).path
        query = {k: v[0] for k, v in parse_qs(urlparse(raw_path).query).items()}
        try:
            for route_method, pattern, func in self.routes:
                if route_method != method:
                    continue
                match = re.match(pattern, path)
                if match:
                    token = self._token(headers)
                    status, payload = func(token, match, query, body or {})
                    return status, payload
            return 404, {"error": {"code": "NOT_FOUND", "message": f"路径不存在：{method} {path}", "detail": {}}}
        except KnowledgeError as exc:
            return exc.http_status, {"error": exc.to_dict()}
        except Exception as exc:  # noqa: BLE001 - 兜底，保证错误格式稳定
            return 500, {"error": {"code": "INTERNAL_ERROR", "message": str(exc), "detail": {}}}

    @staticmethod
    def _token(headers):
        auth = headers.get("Authorization") or headers.get("authorization") or ""
        if auth.startswith("Bearer "):
            return auth[len("Bearer "):].strip()
        return None

    # ------------------------------------------------------------------
    def _health(self, token, match, query, body):
        return 200, {"status": "ok"}

    def _login(self, token, match, query, body):
        return 200, self.service.authenticate(body.get("username"), body.get("password"))

    def _users(self, token, match, query, body):
        return 200, {"users": self.service.list_users(token)}

    def _create_user(self, token, match, query, body):
        result = self.service.create_user(
            token, username=body.get("username"), password=body.get("password"),
            display_name=body.get("display_name"), roles=body.get("roles") or [])
        return 201, result

    def _change_role(self, token, match, query, body):
        return 200, self.service.change_role(
            token, match.group(1), body.get("role"), body.get("action"))

    @staticmethod
    def _entry_fields(body):
        return {
            "equipment_model": body.get("equipment_model"),
            "process_step": body.get("process_step"),
            "applicable_conditions": body.get("applicable_conditions"),
            "evidence_source": body.get("evidence_source"),
            "content": body.get("content"),
            "terms": body.get("terms"),
            "valid_until": body.get("valid_until"),
        }

    def _submit(self, token, match, query, body):
        result = self.service.submit_entry(token, source_ref=body.get("source_ref"),
                                           **self._entry_fields(body))
        return 201, result

    def _list_entries(self, token, match, query, body):
        return 200, {"entries": self.service.list_entries(token, status=query.get("status"))}

    def _get_entry(self, token, match, query, body):
        return 200, self.service.get_entry(token, match.group(1))

    def _history(self, token, match, query, body):
        return 200, self.service.entry_history(token, match.group(1))

    def _revise(self, token, match, query, body):
        base = body.get("base_version_no")
        if not isinstance(base, int):
            raise ValidationError("base_version_no 必须是整数")
        result = self.service.revise_entry(token, match.group(1), base_version_no=base,
                                           **self._entry_fields(body))
        return 201, result

    def _pending(self, token, match, query, body):
        return 200, {"pending": self.service.pending_reviews(token)}

    def _decide(self, token, match, query, body):
        return 200, self.service.decide_review(
            token, match.group(1), decision=body.get("decision"), rationale=body.get("rationale"))

    def _withdraw(self, token, match, query, body):
        return 200, self.service.withdraw_version(token, match.group(1), reason=body.get("reason"))

    def _expire(self, token, match, query, body):
        return 200, self.service.expire_version(token, match.group(1), reason=body.get("reason"))

    def _explain(self, token, match, query, body):
        return 200, self.service.explain_version(token, match.group(1))

    def _search(self, token, match, query, body):
        return 200, {"results": self.service.search_catalog(
            token, q=query.get("q"), equipment_model=query.get("equipment_model"),
            process_step=query.get("process_step"))}

    def _merges(self, token, match, query, body):
        return 200, {"merge_suggestions": self.service.list_merge_suggestions(
            token, status=query.get("status"))}

    def _merge_accept(self, token, match, query, body):
        return 200, self.service.accept_merge(
            token, match.group(1), canonical_entry_id=body.get("canonical_entry_id"),
            note=body.get("note"))

    def _merge_dismiss(self, token, match, query, body):
        return 200, self.service.dismiss_merge(token, match.group(1), reason=body.get("reason"))

    def _import(self, token, match, query, body):
        return 200, self.service.import_entries(
            token, batch_key=body.get("batch_key"), records=body.get("records") or [])

    def _sweep(self, token, match, query, body):
        return 200, self.service.sweep_expired(token)

    def _aliases(self, token, match, query, body):
        return 200, {"aliases": self.service.list_term_aliases(token)}

    def _add_alias(self, token, match, query, body):
        return 200, self.service.add_term_alias(
            token, alias=body.get("alias"), canonical=body.get("canonical"))


class _Handler(BaseHTTPRequestHandler):
    server_version = "KnowledgeBase/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默访问日志，保持 CLI/测试输出干净
        pass

    def _handle(self, method):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            self._send(400, {"error": {"code": "INVALID_JSON", "message": "请求体不是合法 JSON", "detail": {}}})
            return
        status, payload = self.server.api_app.handle(method, self.path, self.headers, body)
        self._send(status, payload)

    def _send(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def make_server(service, host="127.0.0.1", port=8000):
    server = ThreadingHTTPServer((host, port), _Handler)
    server.api_app = ApiApp(service)
    return server
