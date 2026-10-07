"""可解释性：过期或被撤回的结论仍能说明当时为何被采用。"""

import unittest

from experience_registry.models import VERSION_EXPIRED, VERSION_WITHDRAWN
from helpers import SAMPLE, ServiceTestCase

RATIONALE = "2026-09-15 现场验证：三班连续加工 50 件无振纹，同意采用"


class ExplainTests(ServiceTestCase):
    def test_withdrawn_version_keeps_adoption_rationale(self):
        submitted = self.submit_and_approve(rationale=RATIONALE)
        version_id = submitted["version"]["id"]
        self.service.withdraw_version(self.rev_zhao, version_id,
                                      reason="新型刀具材质不适用该结论")
        # 已退出检索目录
        self.assertEqual(self.service.search_catalog(self.viewer), [])
        explain = self.service.explain_version(self.viewer, version_id)
        self.assertEqual(explain["version"]["status"], VERSION_WITHDRAWN)
        # 当时为何被采用
        self.assertEqual(explain["adoption"]["reviewed_by"], "rev_zhao")
        self.assertEqual(explain["adoption"]["rationale"], RATIONALE)
        self.assertEqual(explain["adoption"]["evidence_source"], SAMPLE["evidence_source"])
        # 后来为何退出
        self.assertEqual(explain["retirement"]["kind"], "withdrawn")
        self.assertEqual(explain["retirement"]["reason"], "新型刀具材质不适用该结论")

    def test_expired_version_keeps_adoption_rationale(self):
        submitted = self.submit_and_approve(rationale=RATIONALE)
        version_id = submitted["version"]["id"]
        self.service.expire_version(self.rev_zhao, version_id, reason="工艺路线已换代")
        explain = self.service.explain_version(self.viewer, version_id)
        self.assertEqual(explain["version"]["status"], VERSION_EXPIRED)
        self.assertEqual(explain["adoption"]["rationale"], RATIONALE)
        self.assertEqual(explain["retirement"]["kind"], "expired")
        self.assertEqual(explain["retirement"]["reason"], "工艺路线已换代")

    def test_valid_until_triggers_automatic_expiry(self):
        submitted = self.submit(valid_until="2026-01-10")
        self.service.decide_review(self.rev_zhao, submitted["review_task_id"],
                                   decision="approve", rationale=RATIONALE)
        # 有效期内可见
        self.clock.moment = self.clock.moment.replace(month=1, day=10)
        self.assertEqual(len(self.service.search_catalog(self.viewer)), 1)
        # 过期后自动退出目录，且原因可解释
        self.clock.moment = self.clock.moment.replace(day=11)
        self.assertEqual(self.service.search_catalog(self.viewer), [])
        explain = self.service.explain_version(self.viewer, submitted["version"]["id"])
        self.assertEqual(explain["retirement"]["kind"], "expired")
        self.assertIn("2026-01-10", explain["retirement"]["reason"])
        self.assertEqual(explain["adoption"]["rationale"], RATIONALE)

    def test_superseded_version_explains_replacement(self):
        first = self.submit_and_approve(rationale=RATIONALE)
        entry_id = first["entry"]["id"]
        revised = self.service.revise_entry(self.eng_li, entry_id, base_version_no=1, **SAMPLE)
        self.service.decide_review(self.rev_zhao, revised["review_task_id"],
                                   decision="approve", rationale="修订后复验通过")
        explain = self.service.explain_version(self.viewer, first["version"]["id"])
        self.assertEqual(explain["retirement"]["kind"], "superseded")
        self.assertEqual(explain["retirement"]["by_version_no"], 2)
        self.assertEqual(explain["adoption"]["rationale"], RATIONALE)


if __name__ == "__main__":
    unittest.main()
