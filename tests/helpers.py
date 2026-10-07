"""测试公共基座：临时数据库、固定时钟、预置角色用户。"""

from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest

from experience_registry import KnowledgeService


class FakeClock:
    def __init__(self, start=datetime(2026, 1, 1, 8, 0, tzinfo=timezone.utc)):
        self.moment = start

    def __call__(self):
        return self.moment

    def advance(self, **kwargs):
        self.moment += timedelta(**kwargs)


SAMPLE = {
    "equipment_model": "CA6140",
    "process_step": "精车",
    "applicable_conditions": "加工 45# 钢轴类件，切削液正常供给",
    "evidence_source": "2025-12 三车间现场验证记录 SV-2025-118",
    "content": "精车 45# 钢长轴出现振纹时，先将主轴卡死检查径向间隙，"
               "若轴承发热超过 70 度则更换轴承，恢复后重新对刀。",
}


class ServiceTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = os.path.join(self.tmp.name, "kb.db")
        self.clock = FakeClock()
        self.service = KnowledgeService(self.db_path, clock=self.clock)
        self.service.bootstrap_admin("admin", "admin123")
        self.admin = self._login("admin", "admin123")
        # 预置三种角色用户
        self.service.create_user(self.admin, username="eng_li", password="pass123",
                                 display_name="李工", roles=["engineer", "viewer"])
        self.service.create_user(self.admin, username="eng_wang", password="pass123",
                                 display_name="王工", roles=["engineer", "viewer"])
        self.service.create_user(self.admin, username="rev_zhao", password="pass123",
                                 display_name="赵师傅", roles=["reviewer", "viewer"])
        self.service.create_user(self.admin, username="ops_guest", password="pass123",
                                 display_name="运维", roles=["viewer"])
        self.eng_li = self._login("eng_li", "pass123")
        self.eng_wang = self._login("eng_wang", "pass123")
        self.rev_zhao = self._login("rev_zhao", "pass123")
        self.viewer = self._login("ops_guest", "pass123")

    def _login(self, username, password):
        return self.service.authenticate(username, password)["token"]

    def submit(self, token=None, **overrides):
        fields = dict(SAMPLE)
        fields.update(overrides)
        return self.service.submit_entry(token or self.eng_li, **fields)

    def submit_and_approve(self, token=None, rationale="现场验证通过，同意入库"):
        submitted = self.submit(token)
        task_id = submitted["review_task_id"]
        self.service.decide_review(self.rev_zhao, task_id, decision="approve", rationale=rationale)
        return submitted
