"""HTTP 接口端到端：认证、权限、完整链路、稳定错误格式。"""

import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from experience_registry.httpapi import make_server
from helpers import SAMPLE, ServiceTestCase


class HttpApiTests(ServiceTestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.service, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)

    def _call(self, method, path, body=None, token=None):
        url = f"http://127.0.0.1:{self.port}{urllib.parse.quote(path, safe='/:?=&')}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_chain_over_http(self):
        # 登录
        status, payload = self._call("POST", "/api/tokens",
                                     {"username": "eng_li", "password": "pass123"})
        self.assertEqual(status, 200)
        eng_token = payload["token"]
        status, payload = self._call("POST", "/api/tokens",
                                     {"username": "rev_zhao", "password": "pass123"})
        rev_token = payload["token"]

        # 提交
        status, payload = self._call("POST", "/api/entries", SAMPLE, eng_token)
        self.assertEqual(status, 201)
        entry_id = payload["entry"]["id"]
        task_id = payload["review_task_id"]

        # 未复核前检索不到
        status, payload = self._call("GET", "/api/catalog/search?q=振纹", token=rev_token)
        self.assertEqual(payload["results"], [])

        # 待审队列 -> 复核通过
        status, payload = self._call("GET", "/api/reviews/pending", token=rev_token)
        self.assertEqual([p["id"] for p in payload["pending"]], [task_id])
        status, payload = self._call("POST", f"/api/reviews/{task_id}/decision",
                                     {"decision": "approve", "rationale": "HTTP 复核通过"},
                                     rev_token)
        self.assertEqual(status, 200)

        # 生效后可检索，完整链路可查
        status, payload = self._call("GET", "/api/catalog/search?q=振纹", token=rev_token)
        self.assertEqual(len(payload["results"]), 1)
        status, payload = self._call("GET", f"/api/entries/{entry_id}/history", token=rev_token)
        actions = [e["action"] for e in payload["timeline"]]
        self.assertEqual(actions, ["ENTRY_SUBMITTED", "REVIEW_APPROVED"])

    def test_auth_and_permission_errors_have_stable_shape(self):
        status, payload = self._call("GET", "/api/catalog/search")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "AUTHENTICATION_FAILED")

        status, payload = self._call("POST", "/api/tokens",
                                     {"username": "eng_li", "password": "wrong"})
        self.assertEqual(status, 401)

        status, payload = self._call("POST", "/api/entries", SAMPLE, self.viewer)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "PERMISSION_DENIED")

        status, payload = self._call("GET", "/api/entries/ent_missing/history", token=self.viewer)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "NOT_FOUND")

    def test_permission_change_applies_to_live_token_over_http(self):
        status, payload = self._call("POST", "/api/tokens",
                                     {"username": "rev_zhao", "password": "pass123"})
        rev_token = payload["token"]
        status, _ = self._call("GET", "/api/reviews/pending", token=rev_token)
        self.assertEqual(status, 200)
        self.service.change_role(self.admin, "rev_zhao", "reviewer", "revoke")
        status, payload = self._call("GET", "/api/reviews/pending", token=rev_token)
        self.assertEqual(status, 403)

    def test_import_replay_over_http_is_stable(self):
        body = {"batch_key": "http-batch-1",
                "records": [{"source_ref": "H-1", **SAMPLE}]}
        status, first = self._call("POST", "/api/imports", body, self.eng_li)
        self.assertEqual(status, 200)
        status, second = self._call("POST", "/api/imports", body, self.eng_li)
        self.assertEqual(first, second)
        self.assertEqual(len(first["created"]), 1)


if __name__ == "__main__":
    unittest.main()
