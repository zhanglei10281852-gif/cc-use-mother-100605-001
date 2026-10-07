"""HTTP 端到端测试：真实 socket + ThreadingHTTPServer。"""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from experience_registry.http import build_server
from tests.support import sample_content


def _request(base, method, path, token=None, body=None):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db_path = str(Path(cls.tmp.name) / "registry.db")
        cls.server = build_server("127.0.0.1", 0, cls.db_path)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def test_01_full_lifecycle_over_http(self):
        # 无令牌 401
        status, err = _request(self.base, "GET", "/v1/pending")
        self.assertEqual(status, 401)
        self.assertEqual(err["error"]["code"], "unauthorized")

        # 工程师提交
        status, out = _request(
            self.base, "POST", "/v1/entries",
            token="token-engineer", body=sample_content())
        self.assertEqual(status, 200)
        self.assertFalse(out["duplicate"])
        version_id = out["version"]["id"]
        entry_id = out["version"]["entry_id"]

        # 工程师不能复核 -> 403
        status, _ = _request(
            self.base, "POST", f"/v1/versions/{version_id}/approve",
            token="token-engineer", body={})
        self.assertEqual(status, 403)

        # 复核通过 -> 目录可查
        status, _ = _request(
            self.base, "POST", f"/v1/versions/{version_id}/approve",
            token="token-reviewer", body={"note": "证据齐全"})
        self.assertEqual(status, 200)
        status, out = _request(
            self.base, "GET",
            "/v1/directory?equipment_model=CW6163B",
            token="token-engineer")
        self.assertEqual(status, 200)
        self.assertEqual([v["id"] for v in out["items"]], [version_id])

        # 完整链路
        status, chain = _request(
            self.base, "GET", f"/v1/versions/{version_id}/chain",
            token="token-engineer")
        self.assertEqual(status, 200)
        self.assertEqual([e["kind"] for e in chain["timeline"]],
                         ["submitted", "approved"])

        # 相似条目产生合并建议
        status, _ = _request(
            self.base, "POST", "/v1/entries",
            token="token-engineer", body=sample_content(
                title="CW6163B 主轴窜动的判断方法",
                conclusion="主轴窜动超过0.02mm需更换止推轴承并复测。"))
        self.assertEqual(status, 200)
        status, out = _request(
            self.base, "GET", "/v1/suggestions", token="token-reviewer")
        self.assertEqual(status, 200)
        self.assertEqual(len(out["items"]), 1)
        sug_id = out["items"][0]["id"]

        # 按建议合并
        merged = sample_content(
            title="CW6163B 主轴轴向位移（窜动）判断方法",
            conclusion="轴向位移超过0.02mm需更换止推轴承并复测。",
            evidence_sources=[
                {"kind": "field_verification",
                 "reference": "验证批次2026-09-A17"},
                {"kind": "work_order", "reference": "WO-20261003-22"}])
        status, out = _request(
            self.base, "POST", f"/v1/suggestions/{sug_id}/merge",
            token="token-reviewer",
            body={"content": merged, "reason": "统一术语并补充工单"})
        self.assertEqual(status, 200)
        merged_id = out["merged_version_id"]

        # 旧版本显示去向，目录只留合并版本
        status, old = _request(
            self.base, "GET", f"/v1/versions/{version_id}",
            token="token-engineer")
        self.assertEqual(old["status"], "merged")
        self.assertEqual(old["merged_into"], merged_id)
        status, out = _request(
            self.base, "GET", "/v1/directory", token="token-engineer")
        self.assertEqual([v["id"] for v in out["items"]], [merged_id])

    def test_02_duplicate_import_is_stable(self):
        payload = {"items": [
            {"import_id": "http-1", "content": sample_content(
                title="HTTP 导入专用条目", equipment_model="XYZ-100",
                process="调试工序", conclusion="结论编号HTTP-DEDUP-1")},
            {"import_id": "http-2", "content": sample_content(
                title="HTTP 导入专用条目", equipment_model="xyz-100",
                process=" 调试工序 ", conclusion="结论编号HTTP-DEDUP-1")},
        ]}
        status, out1 = _request(
            self.base, "POST", "/v1/imports",
            token="token-engineer", body=payload)
        self.assertEqual(status, 200)
        self.assertEqual([r["duplicate"] for r in out1["results"]],
                         [False, True])
        status, out2 = _request(
            self.base, "POST", "/v1/imports",
            token="token-engineer", body=payload)
        self.assertEqual(
            [r["version_id"] for r in out2["results"]],
            [r["version_id"] for r in out1["results"]])

    def test_03_role_change_then_forbidden_becomes_allowed(self):
        status, _ = _request(
            self.base, "POST", "/v1/admin/users",
            token="token-admin",
            body={"username": "zhao", "role": "engineer",
                  "token": "token-zhao"})
        self.assertEqual(status, 200)
        # 提交一条供复核
        status, out = _request(
            self.base, "POST", "/v1/entries",
            token="token-zhao", body=sample_content(
                title="赵工提交条目", equipment_model="ZK-1",
                process="角色切换验证工序", conclusion="角色切换验证结论R1"))
        self.assertEqual(status, 200)
        vid = out["version"]["id"]
        status, _ = _request(
            self.base, "POST", f"/v1/versions/{vid}/approve",
            token="token-zhao", body={})
        self.assertEqual(status, 403)
        status, _ = _request(
            self.base, "PATCH", "/v1/admin/users/zhao",
            token="token-admin", body={"role": "reviewer"})
        self.assertEqual(status, 200)
        status, _ = _request(
            self.base, "POST", f"/v1/versions/{vid}/approve",
            token="token-zhao", body={"note": "角色已切换，复核通过"})
        self.assertEqual(status, 200)

    def test_04_validation_errors_are_400_and_404(self):
        status, out = _request(
            self.base, "POST", "/v1/entries",
            token="token-engineer", body={"title": "缺少必填字段"})
        self.assertEqual(status, 400)
        self.assertEqual(out["error"]["code"], "validation_error")
        status, out = _request(
            self.base, "GET", "/v1/versions/ver_does_not_exist",
            token="token-engineer")
        self.assertEqual(status, 404)
        status, out = _request(self.base, "GET", "/v1/health", token=None)
        self.assertEqual(status, 200)
