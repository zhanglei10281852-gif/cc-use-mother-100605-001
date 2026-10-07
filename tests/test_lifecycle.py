"""条目全生命周期：提交 -> 复核 -> 生效 -> 修订取代 -> 检索可见性。"""

import unittest

from experience_registry import ConflictError, ValidationError
from experience_registry.models import (
    VERSION_APPROVED,
    VERSION_PENDING,
    VERSION_REJECTED,
    VERSION_SUPERSEDED,
)
from helpers import SAMPLE, ServiceTestCase


class LifecycleTests(ServiceTestCase):
    def test_submit_creates_pending_version_and_review_task(self):
        result = self.submit()
        self.assertEqual(result["version"]["status"], VERSION_PENDING)
        self.assertEqual(result["version"]["version_no"], 1)
        self.assertEqual(result["entry"]["label"], "KB-0001")
        pending = self.service.pending_reviews(self.rev_zhao)
        self.assertEqual([p["id"] for p in pending], [result["review_task_id"]])

    def test_unapproved_version_is_not_searchable(self):
        self.submit()
        self.assertEqual(self.service.search_catalog(self.viewer, q="振纹"), [])

    def test_approve_publishes_to_catalog(self):
        submitted = self.submit_and_approve()
        results = self.service.search_catalog(self.viewer, q="振纹")
        self.assertEqual([r["id"] for r in results], [submitted["version"]["id"]])
        # 复核依据随版本保存
        self.assertEqual(results[0]["review_rationale"], "现场验证通过，同意入库")

    def test_reject_keeps_entry_out_of_catalog(self):
        submitted = self.submit()
        self.service.decide_review(self.rev_zhao, submitted["review_task_id"],
                                   decision="reject", rationale="缺少现场验证记录")
        self.assertEqual(self.service.search_catalog(self.viewer), [])
        history = self.service.entry_history(self.viewer, submitted["entry"]["id"])
        self.assertEqual(history["versions"][0]["status"], VERSION_REJECTED)

    def test_revision_supersedes_previous_approved_version(self):
        first = self.submit_and_approve()
        entry_id = first["entry"]["id"]
        revised = self.service.revise_entry(
            self.eng_li, entry_id, base_version_no=1,
            **{**SAMPLE, "content": "补充：更换轴承后需跑合 30 分钟再精车。"})
        self.service.decide_review(self.rev_zhao, revised["review_task_id"],
                                   decision="approve", rationale="修订内容经复验有效")
        history = self.service.entry_history(self.viewer, entry_id)
        statuses = [v["status"] for v in history["versions"]]
        self.assertEqual(statuses, [VERSION_SUPERSEDED, VERSION_APPROVED])
        results = self.service.search_catalog(self.viewer, q="跑合")
        self.assertEqual([r["version_no"] for r in results], [2])

    def test_revision_with_stale_base_is_rejected(self):
        self.submit_and_approve()
        entry_id = self.service.list_entries(self.viewer)[0]["id"]
        self.service.revise_entry(self.eng_li, entry_id, base_version_no=1, **SAMPLE)
        with self.assertRaises(ConflictError) as ctx:
            self.service.revise_entry(self.eng_wang, entry_id, base_version_no=1, **SAMPLE)
        self.assertEqual(ctx.exception.code, "VERSION_CONFLICT")
        self.assertEqual(ctx.exception.detail["latest_version_no"], 2)

    def test_required_fields_are_validated(self):
        with self.assertRaises(ValidationError):
            self.submit(evidence_source="")
        with self.assertRaises(ValidationError):
            self.submit(valid_until="2026/01/01")

    def test_full_chain_is_traceable(self):
        submitted = self.submit_and_approve()
        history = self.service.entry_history(self.viewer, submitted["entry"]["id"])
        actions = [e["action"] for e in history["timeline"]]
        self.assertIn("ENTRY_SUBMITTED", actions)
        self.assertIn("REVIEW_APPROVED", actions)
        approved = next(e for e in history["timeline"] if e["action"] == "REVIEW_APPROVED")
        self.assertEqual(approved["actor"], "rev_zhao")
        self.assertEqual(approved["detail"]["rationale"], "现场验证通过，同意入库")


if __name__ == "__main__":
    unittest.main()
