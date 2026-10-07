"""服务重启：待审事项、令牌、合并建议在重启后保持稳定。"""

import unittest

from experience_registry import KnowledgeService, PermissionDeniedError
from helpers import SAMPLE, ServiceTestCase


class RestartTests(ServiceTestCase):
    def _reopen(self):
        self.service = KnowledgeService(self.db_path, clock=self.clock)

    def test_pending_reviews_survive_restart(self):
        first = self.submit(content="经验一：主轴卡死的处置")
        second = self.submit(process_step="粗车", content="经验二：粗车余量控制")
        self._reopen()
        pending = self.service.pending_reviews(self.rev_zhao)
        self.assertEqual(
            {p["id"] for p in pending},
            {first["review_task_id"], second["review_task_id"]},
        )
        # 重启后仍可完成复核并生效
        self.service.decide_review(self.rev_zhao, first["review_task_id"],
                                   decision="approve", rationale="重启后复核")
        results = self.service.search_catalog(self.viewer, q="主轴")
        self.assertEqual(len(results), 1)

    def test_tokens_survive_restart(self):
        self._reopen()
        # 重启前签发的令牌仍然有效，且权限实时校验
        self.assertEqual(self.service.search_catalog(self.eng_li), [])
        with self.assertRaises(PermissionDeniedError):
            self.service.pending_reviews(self.eng_li)

    def test_merge_suggestions_survive_restart(self):
        self.submit()
        self.submit(content=SAMPLE["content"].replace("主轴卡死", "主轴抱死"),
                    terms=["一班记录"])
        before = self.service.list_merge_suggestions(self.rev_zhao, status="OPEN")
        self.assertEqual(len(before), 1)
        self._reopen()
        after = self.service.list_merge_suggestions(self.rev_zhao, status="OPEN")
        self.assertEqual([s["id"] for s in before], [s["id"] for s in after])


if __name__ == "__main__":
    unittest.main()
