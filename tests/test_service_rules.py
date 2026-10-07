from experience_registry.errors import Conflict, PermissionDenied
from experience_registry.models import ACTIVE, IN_REVIEW, MERGED
from tests.support import ServiceCase, sample_content


class SimilarityMergeTests(ServiceCase):
    def _two_parallel_records(self):
        # 两个班组用不同术语记录同一条经验
        a = sample_content()
        b = sample_content(
            title="CW6163B 主轴窜动的判断方法",
            content="将百分表吸在床头箱端面，表头顶在主轴端孔内，"
                    "手动撬动主轴，读数超过0.02mm即判定主轴窜动过大。",
            conclusion="主轴窜动超过0.02mm需更换止推轴承并复测。",
            tags=["主轴", "百分表"])
        v1 = self.svc.submit(self.engineer, a)["version"]["id"]
        v2 = self.svc.submit(self.engineer, b)["version"]["id"]
        return v1, v2

    def test_similar_terms_create_merge_suggestion(self):
        v1, v2 = self._two_parallel_records()
        suggestions = self.svc.suggestions()
        self.assertEqual(len(suggestions), 1)
        sug = suggestions[0]
        self.assertEqual(sorted([sug["left_version_id"],
                                 sug["right_version_id"]]),
                         sorted([v1, v2]))
        self.assertGreaterEqual(sug["score"], 0.34)
        self.assertTrue(sug["reasons"])

    def test_merge_creates_active_version_and_preserves_originals(self):
        v1, v2 = self._two_parallel_records()
        sug_id = self.svc.suggestions()[0]["id"]
        merged_content = sample_content(
            title="CW6163B 主轴轴向位移（窜动）判断方法",
            conclusion="轴向位移超过0.02mm需更换止推轴承并复测；"
                       "测量前确认百分表座吸合牢固。",
            evidence_sources=[
                {"kind": "field_verification",
                 "reference": "验证批次2026-09-A17"},
                {"kind": "work_order", "reference": "WO-20261003-22"},
            ])
        with self.assertRaises(PermissionDenied):
            self.svc.merge(self.engineer, sug_id, [], merged_content)
        out = self.svc.merge(self.reviewer, sug_id, [], merged_content,
                             "合并两班组术语并补充工单证据")
        mv = out["merged_version_id"]
        self.assertEqual(self.svc.get_version(mv)["status"], ACTIVE)
        self.assertEqual(self.svc.get_version(v1)["status"], MERGED)
        self.assertEqual(self.svc.get_version(v2)["status"], MERGED)
        self.assertEqual(self.svc.get_version(v1)["merged_into"], mv)
        # 原始版本仍可读取，链路解释去向
        self.assertIn("0.02mm",
                      self.svc.get_version(v1)["content"]["conclusion"])
        chain = self.svc.version_chain(v1)
        self.assertEqual([e["kind"] for e in chain["timeline"]],
                         ["submitted", "suggestion_created", "merged_away"])
        # 建议已结案，不能重复执行
        with self.assertRaises(Conflict):
            self.svc.merge(self.reviewer, sug_id, [], merged_content)
        # 目录只有合并后的版本
        self.assertEqual([d["id"] for d in self.svc.directory()], [mv])

    def test_dismiss_suggestion(self):
        self._two_parallel_records()
        sug_id = self.svc.suggestions()[0]["id"]
        self.svc.dismiss_suggestion(self.reviewer, sug_id, "适用工序不同")
        self.assertEqual(self.svc.suggestions("all")[0]["status"], "dismissed")
        with self.assertRaises(Conflict):
            self.svc.dismiss_suggestion(self.reviewer, sug_id)

    def test_unrelated_records_get_no_suggestion(self):
        self.svc.submit(self.engineer, sample_content())
        other = sample_content(
            title="CB-B25 液压泵流量不足处理", kind="process_knowhow",
            equipment_model="CB-B25", process="液压系统检修",
            conditions=["系统压力低于4MPa"],
            content="流量不足时检查溢流阀开度与吸油滤网堵塞情况，"
                    "必要时更换油泵并排气。",
            conclusion="清洗滤网、调整溢流阀后流量恢复额定值。",
            evidence_sources=[{"kind": "work_order",
                               "reference": "WO-20260820-07"}],
            tags=["液压"])
        self.svc.submit(self.engineer, other)
        self.assertEqual(self.svc.suggestions(), [])


class ExpiryTests(ServiceCase):
    def test_manual_expire_keeps_explanation(self):
        v, _ = self.submit_and_approve()
        self.svc.expire(self.reviewer, v, "2026年新机型改用激光干涉测量")
        got = self.svc.get_version(v)
        self.assertEqual(got["status"], "expired")
        self.assertTrue(got["decision_note"])
        self.assertNotIn(v, [d["id"] for d in self.svc.directory()])
        # 默认目录不含过期项；显式查询仍能取回
        self.assertIn(v, [d["id"] for d in
                          self.svc.directory(include_inactive=True)])

    def test_sweep_marks_due_versions_idempotently(self):
        out = self.svc.submit(
            self.engineer, sample_content(
                conclusion="临时工艺：冬季预热15分钟（暂行结论PPQ）"),
            expires_at="2026-09-30")
        v = out["version"]["id"]
        self.svc.review(self.reviewer, v, True)
        swept = self.svc.sweep_expired("2026-10-01")
        self.assertEqual([s["version_id"] for s in swept], [v])
        # 再次扫描结果稳定为空
        self.assertEqual(self.svc.sweep_expired("2026-10-01"), [])
        kinds = [e["kind"] for e in self.svc.timeline(version_id=v)]
        self.assertEqual(kinds, ["submitted", "approved", "expired"])

    def test_sweep_keeps_not_yet_due(self):
        out = self.svc.submit(self.engineer, sample_content(
            conclusion="未来才到期的结论ZZZ"), expires_at="2026-12-31")
        v = out["version"]["id"]
        self.svc.review(self.reviewer, v, True)
        self.assertEqual(self.svc.sweep_expired("2026-10-01"), [])
        self.assertEqual(self.svc.get_version(v)["status"], ACTIVE)


class RbacTests(ServiceCase):
    def test_role_change_takes_effect_immediately(self):
        self.svc.create_user(self.admin, "zhang", "engineer", "token-zhang")
        zhang = self.svc.authenticate_token("token-zhang")
        self.svc.submit(zhang, sample_content())
        # 工程师不能复核
        from experience_registry.errors import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.svc.review(zhang, self.svc.pending()[0]["id"], True)
        self.svc.change_user_role(self.admin, "zhang", "reviewer")
        # 无需重新登录：角色实时读取
        self.svc.review(zhang, self.svc.pending()[0]["id"], True, "已授权")
        self.assertEqual(self.svc.pending(), [])

    def test_deactivated_token_stops_working(self):
        self.svc.create_user(self.admin, "li", "engineer", "token-li")
        self.svc.set_user_active(self.admin, "li", False)
        from experience_registry.errors import AuthError
        with self.assertRaises(AuthError):
            self.svc.authenticate_token("token-li")
        # 已持有的身份也无法继续操作
        self.svc.set_user_active(self.admin, "li", True)
        li = self.svc.authenticate_token("token-li")
        self.svc.set_user_active(self.admin, "li", False)
        from experience_registry.errors import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.svc.submit(li, sample_content(title="停用后再试QQQ"))

    def test_only_admin_manages_users(self):
        self.svc.create_user(self.admin, "tmpeng", "engineer", "token-tmpeng")
        self.svc.change_user_role(self.admin, "tmpeng", "reviewer")
        with self.assertRaises(PermissionDenied):
            self.svc.create_user(self.reviewer, "x", "engineer", "t")
        audit = self.svc.audit_log(self.admin)
        self.assertTrue(any(a["action"] == "user.role_change" for a in audit))
