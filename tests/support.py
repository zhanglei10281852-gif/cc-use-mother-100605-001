"""共享测试夹具。"""
import tempfile
import unittest
from pathlib import Path

from experience_registry.db import connect, initialize
from experience_registry.service import RegistryService, utc_now


def sample_content(**overrides):
    content = {
        "title": "CW6163B 主轴轴向位移的判断方法",
        "kind": "fault_judgement",
        "equipment_model": "CW6163B",
        "process": "主轴装配",
        "conditions": ["空载低速", "环境温度20-30℃"],
        "content": "将百分表吸在主轴箱端面，表头顶在主轴端孔内，"
                   "手动撬动主轴，读数超过0.02mm即判定轴向位移过大。",
        "conclusion": "轴向位移超过0.02mm需更换止推轴承并复测。",
        "evidence_sources": [
            {"kind": "field_verification", "reference": "验证批次2026-09-A17",
             "verified_at": "2026-09-12", "note": "三车间2号机组复测两次"},
        ],
        "tags": ["主轴", "百分表"],
    }
    content.update(overrides)
    return content


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "registry.db")
        initialize(self.db_path, utc_now)
        self.conn = connect(self.db_path)
        self.svc = RegistryService(self.conn)
        self.admin = self.svc.authenticate_token("token-admin")
        self.engineer = self.svc.authenticate_token("token-engineer")
        self.reviewer = self.svc.authenticate_token("token-reviewer")

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def reopen(self):
        """模拟服务重启：关闭旧连接，打开新连接。"""
        self.conn.close()
        self.conn = connect(self.db_path)
        self.svc = RegistryService(self.conn)
        self.admin = self.svc.authenticate_token("token-admin")
        self.engineer = self.svc.authenticate_token("token-engineer")
        self.reviewer = self.svc.authenticate_token("token-reviewer")

    def submit_and_approve(self, actor=None, content=None, **kwargs):
        actor = actor or self.engineer
        out = self.svc.submit(actor, content or sample_content(), **kwargs)
        self.assertFalse(out["duplicate"])
        version_id = out["version"]["id"]
        entry_id = out["version"]["entry_id"]
        self.svc.review(self.reviewer, version_id, True, "现场记录齐全")
        return version_id, entry_id
