"""权限模型：指定角色复核、职责分离、权限变更即时生效。"""

import unittest

from experience_registry import AuthenticationError, PermissionDeniedError
from helpers import ServiceTestCase


class PermissionTests(ServiceTestCase):
    def test_engineer_cannot_approve(self):
        submitted = self.submit()
        with self.assertRaises(PermissionDeniedError):
            self.service.decide_review(self.eng_wang, submitted["review_task_id"],
                                       decision="approve", rationale="尝试越权")

    def test_viewer_cannot_submit(self):
        with self.assertRaises(PermissionDeniedError):
            self.submit(token=self.viewer)

    def test_submitter_cannot_review_own_version(self):
        # 李工同时具备复核角色时，也不能复核本人提交
        self.service.change_role(self.admin, "eng_li", "reviewer", "grant")
        token = self._login("eng_li", "pass123")
        submitted = self.submit(token=token)
        with self.assertRaises(PermissionDeniedError) as ctx:
            self.service.decide_review(token, submitted["review_task_id"],
                                       decision="approve", rationale="自审")
        self.assertIn("本人", ctx.exception.message)

    def test_role_revocation_takes_effect_immediately(self):
        """权限变化：撤权后同一会话令牌立即失去对应能力。"""
        submitted = self.submit()
        # 撤权前可以查看待审队列
        self.assertEqual(len(self.service.pending_reviews(self.rev_zhao)), 1)
        self.service.change_role(self.admin, "rev_zhao", "reviewer", "revoke")
        with self.assertRaises(PermissionDeniedError):
            self.service.pending_reviews(self.rev_zhao)
        with self.assertRaises(PermissionDeniedError):
            self.service.decide_review(self.rev_zhao, submitted["review_task_id"],
                                       decision="approve", rationale="撤权后尝试复核")
        # 重新授权后恢复
        self.service.change_role(self.admin, "rev_zhao", "reviewer", "grant")
        self.service.decide_review(self.rev_zhao, submitted["review_task_id"],
                                   decision="approve", rationale="恢复授权后复核通过")

    def test_invalid_or_missing_token_is_rejected(self):
        with self.assertRaises(AuthenticationError):
            self.service.search_catalog(None)
        with self.assertRaises(AuthenticationError):
            self.service.search_catalog("tok_not_exists")

    def test_role_changes_are_audited(self):
        self.service.change_role(self.admin, "eng_wang", "reviewer", "grant")
        self.service.change_role(self.admin, "eng_wang", "reviewer", "revoke")
        users = self.service.list_users(self.admin)
        wang = next(u for u in users if u["username"] == "eng_wang")
        self.assertNotIn("reviewer", wang["roles"])


if __name__ == "__main__":
    unittest.main()
