import json

from experience_registry.errors import (AuthError, Conflict, NotFound,
                                         PermissionDenied, ValidationError)
from experience_registry.models import (ACTIVE, EXPIRED, IN_REVIEW, MERGED,
                                        REJECTED, SUPERSEDED, WITHDRAWN)
from tests.support import ServiceCase, sample_content


class SubmitReviewTests(ServiceCase):
    def test_submit_then_approve_enters_directory(self):
        out = self.svc.submit(self.engineer, sample_content())
        self.assertFalse(out["duplicate"])
        v = out["version"]
        self.assertEqual(v["status"], IN_REVIEW)
        self.assertEqual(len(v["content"]["evidence_sources"]), 1)
        with self.assertRaises(PermissionDenied):
            self.svc.review(self.engineer, v["id"], True)
        self.svc.review(self.reviewer, v["id"], True, "证据可追溯")
        self.assertEqual(self.svc.get_version(v["id"])["status"], ACTIVE)
        directory = self.svc.directory(equipment_model="cw6163b")
        self.assertEqual([d["id"] for d in directory], [v["id"]])
        # 待审版本不进目录
        self.svc.submit(self.engineer, sample_content(
            title="另一条待审经验", conclusion="完全不同的结论内容XYZ"))
        self.assertEqual(len(self.svc.directory()), 1)

    def test_reject_requires_reason_and_keeps_explanation(self):
        v = self.svc.submit(self.engineer, sample_content())["version"]
        with self.assertRaises(ValidationError):
            self.svc.review(self.reviewer, v["id"], False)
        self.svc.review(self.reviewer, v["id"], False, "缺少现场验证记录")
        got = self.svc.get_version(v["id"])
        self.assertEqual(got["status"], REJECTED)
        self.assertEqual(got["decision_note"], "缺少现场验证记录")
        self.assertEqual(len(got["content"]["evidence_sources"]), 1)
        kinds = [e["kind"] for e in self.svc.timeline(version_id=v["id"])]
        self.assertEqual(kinds, ["submitted", "rejected"])

    def test_content_validation(self):
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(title=" "))
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(
                kind="unsupported_kind"))
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(conditions=[]))
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(
                evidence_sources=[{"kind": "field_verification"}]))
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(
                evidence_sources=[{"kind": "rumor", "reference": "x"}]))
        with self.assertRaises(ValidationError):
            self.svc.submit(self.engineer, sample_content(unexpected=1))

    def test_withdraw_only_by_submitter(self):
        v = self.svc.submit(self.engineer, sample_content())["version"]
        other = self.svc.authenticate_token("token-admin")
        self.svc.withdraw(self.engineer, v["id"], "班组要求补充证据")
        self.assertEqual(self.svc.get_version(v["id"])["status"], WITHDRAWN)
        v2 = self.svc.submit(other, sample_content(
            title="管理员提交的一条", conclusion="结论ABC123"))["version"]
        with self.assertRaises(PermissionDenied):
            self.svc.withdraw(self.engineer, v2["id"])

    def test_auth_required(self):
        with self.assertRaises(AuthError):
            self.svc.authenticate_token("")
        with self.assertRaises(AuthError):
            self.svc.authenticate_token("wrong-token")


class RevisionTests(ServiceCase):
    def test_revision_chain_supersedes_old_and_keeps_original(self):
        v1, entry_id = self.submit_and_approve()
        revised = sample_content(
            conclusion="轴向位移超过0.03mm需更换止推轴承，0.02-0.03mm加密观察。")
        out = self.svc.revise(self.engineer, entry_id, revised)
        v2 = out["version"]["id"]
        self.assertNotEqual(v1, v2)
        # 旧版本仍生效，直到新版本复核通过
        self.assertEqual(self.svc.get_version(v1)["status"], ACTIVE)
        self.assertEqual(len(self.svc.pending()), 1)
        self.svc.review(self.reviewer, v2, True, "依据10月二次现场验证")
        self.assertEqual(self.svc.get_version(v1)["status"], SUPERSEDED)
        self.assertEqual(self.svc.get_version(v1)["superseded_by"], v2)
        self.assertEqual(self.svc.get_version(v2)["status"], ACTIVE)
        # 原始版本内容与事件都还在，可解释当时为何采用
        old = self.svc.get_version(v1)
        self.assertIn("0.02mm", old["content"]["conclusion"])
        entry = self.svc.get_entry(entry_id)
        self.assertEqual([v["seq"] for v in entry["versions"]], [1, 2])
        chain_kinds = [e["kind"] for e in entry["timeline"]]
        self.assertEqual(
            chain_kinds,
            ["submitted", "approved", "revision_submitted", "approved",
             SUPERSEDED])

    def test_concurrent_revision_blocked_while_pending(self):
        v1, entry_id = self.submit_and_approve()
        self.svc.revise(self.engineer, entry_id, sample_content(
            conclusion="待审修订一，阈值0.025mm"))
        with self.assertRaises(Conflict):
            self.svc.revise(self.engineer, entry_id, sample_content(
                conclusion="待审修订二，阈值0.018mm"))

    def test_revise_missing_entry(self):
        with self.assertRaises(NotFound):
            self.svc.revise(self.engineer, "ent_missing", sample_content())


class DuplicateImportTests(ServiceCase):
    def _payloads(self):
        a = sample_content()
        # b 与 a 语义完全相同：空白、标签顺序、大小写不同
        b = json.loads(json.dumps(a, ensure_ascii=False))
        b["tags"] = list(reversed(b["tags"]))
        b["equipment_model"] = " cw6163b "
        b["content"] = "  " + a["content"].replace("，", "，  ") + "  "
        b["conditions"] = list(reversed(a["conditions"]))
        c = sample_content(title="液压泵异响排查", equipment_model="CB-B25",
                           process="液压系统检修",
                           content="油泵异响先查吸油滤网与联轴器对中。",
                           conclusion="清洗滤网并重新对中后异响消失。",
                           tags=["液压"])
        return a, b, c

    def test_duplicate_submit_returns_existing_version(self):
        a, b, _ = self._payloads()
        first = self.svc.submit(self.engineer, a)["version"]
        again = self.svc.submit(self.engineer, b)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["version"]["id"], first["id"])

    def test_import_batch_is_idempotent_and_replay_safe(self):
        a, b, c = self._payloads()
        results1 = self.svc.import_items(self.engineer, [
            {"import_id": "r1", "content": a},
            {"import_id": "r2", "content": b},
            {"import_id": "r3", "content": c},
        ])
        self.assertEqual([r["duplicate"] for r in results1],
                         [False, True, False])
        ids = {r["import_id"]: r["version_id"] for r in results1}
        # 整批重放：全部映射到既有版本，不产生新数据
        results2 = self.svc.import_items(self.engineer, [
            {"import_id": "r1", "content": a},
            {"import_id": "r2", "content": b},
            {"import_id": "r3", "content": c},
        ])
        self.assertTrue(all(r["duplicate"] for r in results2))
        self.assertEqual([r["version_id"] for r in results2],
                         [ids["r1"], ids["r2"], ids["r3"]])
        # 待审队列只有两条（a/b 归一为同一条，c 一条），重放不产生第三条
        self.assertEqual(len(self.svc.pending()), 2)

    def test_import_bad_record_rolls_back_whole_batch(self):
        good = sample_content()
        before = len(self.svc.pending())
        with self.assertRaises(ValidationError):
            self.svc.import_items(self.engineer, [
                good,
                sample_content(title=""),  # 非法
            ])
        self.assertEqual(len(self.svc.pending()), before)

    def test_same_content_after_revision_still_dedups(self):
        # 不同条目下提交完全相同的内容也应稳定去重（content_hash 全局唯一）
        a = sample_content()
        first = self.svc.submit(self.engineer, a)["version"]
        second = self.svc.submit(self.admin, a)
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["version"]["entry_id"], first["entry_id"])
