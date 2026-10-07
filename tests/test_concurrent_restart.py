"""并发修订、服务重启持久性测试。"""
import sqlite3
import threading

from experience_registry.db import connect, transaction
from experience_registry.errors import Conflict
from experience_registry.models import ACTIVE
from experience_registry.service import RegistryService
from tests.support import ServiceCase, sample_content


class ConcurrencyTests(ServiceCase):
    def test_concurrent_approve_has_single_winner(self):
        # 先造两个待审版本（不同条目）
        ids = []
        for i in range(2):
            ids.append(self.svc.submit(
                self.engineer,
                sample_content(title=f"并发复核条目{i}",
                               conclusion=f"结论内容并行{i}XYZ"))["version"]["id"])

        results: list[str] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def approve(version_id: str):
            conn = connect(self.db_path)
            try:
                svc = RegistryService(conn)
                actor = svc.authenticate_token("token-reviewer")
                barrier.wait()
                svc.review(actor, version_id, True, "并行复核")
                results.append(version_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=approve, args=(vid,))
                   for vid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 不同版本互不冲突，二者都成功
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), sorted(ids))

    def test_concurrent_duplicate_submit_creates_one_version(self):
        outcomes = []

        def submit():
            conn = connect(self.db_path)
            try:
                svc = RegistryService(conn)
                actor = svc.authenticate_token("token-engineer")
                out = svc.submit(actor, sample_content())
                outcomes.append(out["version"]["id"])
            finally:
                conn.close()

        threads = [threading.Thread(target=submit) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # BEGIN IMMEDIATE 串行化 + content_hash 唯一：只有一个版本
        self.assertEqual(len(set(outcomes)), 1)
        self.assertEqual(len(self.svc.pending()), 1)

    def test_concurrent_revision_single_pending_version(self):
        _, entry_id = self.submit_and_approve()
        errors = []

        def revise(i: int):
            conn = connect(self.db_path)
            try:
                svc = RegistryService(conn)
                actor = svc.authenticate_token("token-engineer")
                svc.revise(actor, entry_id, sample_content(
                    conclusion=f"并发修订阈值0.01{i}mm"))
            except Conflict as exc:
                errors.append(exc)
            except sqlite3.OperationalError as exc:
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=revise, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # 恰好一个修订成功，其余得到冲突或锁忙，绝不会出现两个待审版本
        pending = self.svc.pending()
        self.assertEqual(len(pending), 1)
        self.assertGreaterEqual(len(errors), 1)


class RestartPersistenceTests(ServiceCase):
    def test_pending_and_chain_survive_restart(self):
        v = self.svc.submit(self.engineer, sample_content())["version"]["id"]
        entry_id = self.svc.get_version(v)["entry_id"]
        self.reopen()
        # 待审事项不丢失
        pending = self.svc.pending()
        self.assertEqual([p["id"] for p in pending], [v])
        self.svc.review(self.reviewer, v, True, "重启后继续复核")
        self.reopen()
        self.assertEqual(self.svc.get_version(v)["status"], ACTIVE)
        chain = self.svc.version_chain(v)
        self.assertEqual([e["kind"] for e in chain["timeline"]],
                         ["submitted", "approved"])
        self.assertTrue(chain["entry"]["versions"])

    def test_suggestions_and_roles_survive_restart(self):
        a = sample_content()
        b = sample_content(
            title="CW6163B 主轴窜动的判断方法",
            conclusion="主轴窜动超过0.02mm需更换止推轴承并复测。")
        self.svc.submit(self.engineer, a)
        self.svc.submit(self.engineer, b)
        self.svc.create_user(self.admin, "wang", "reviewer", "token-wang")
        self.reopen()
        self.assertEqual(len(self.svc.suggestions()), 1)
        wang = self.svc.authenticate_token("token-wang")
        self.assertEqual(wang["role"], "reviewer")
        users = {u["username"]: u for u in self.svc.list_users(self.admin)}
        self.assertEqual(users["wang"]["active"], 1)

    def test_expired_swept_after_restart(self):
        out = self.svc.submit(self.engineer, sample_content(
            conclusion="重启后应自动过期的暂行结论RRR"),
            expires_at="2026-09-01")
        v = out["version"]["id"]
        self.svc.review(self.reviewer, v, True)
        self.reopen()
        # 模拟 HTTP 启动钩子
        swept = self.svc.sweep_expired("2026-10-07")
        self.assertEqual([s["version_id"] for s in swept], [v])
        self.assertEqual(self.svc.get_version(v)["status"], "expired")

    def test_wal_recovers_after_uncommitted_transaction(self):
        # 另一连接开启写事务但未提交即被强制关闭，已提交数据仍完好
        v = self.svc.submit(self.engineer, sample_content())["version"]["id"]
        rogue = connect(self.db_path)
        rogue.execute("BEGIN IMMEDIATE")
        rogue.execute(
            "UPDATE entry_versions SET status='active' WHERE id=?", (v,))
        # 不 COMMIT，直接丢弃连接（模拟进程被 kill -9）
        del rogue
        self.reopen()
        self.assertEqual(self.svc.get_version(v)["status"], "in_review")
