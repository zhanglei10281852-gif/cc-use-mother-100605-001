"""并发：并行修订与并行复核都有确定性的唯一胜者。"""

import threading
import unittest

from experience_registry import ConflictError
from helpers import SAMPLE, ServiceTestCase


class ConcurrencyTests(ServiceTestCase):
    def _run_parallel(self, fn, count=2):
        barrier = threading.Barrier(count)
        outcomes = []

        def worker(index):
            barrier.wait(timeout=5)
            try:
                fn(index)
                outcomes.append("ok")
            except ConflictError as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return outcomes

    def test_concurrent_revisions_have_single_winner(self):
        submitted = self.submit_and_approve()
        entry_id = submitted["entry"]["id"]
        tokens = [self.eng_li, self.eng_wang]

        def revise(i):
            self.service.revise_entry(tokens[i], entry_id, base_version_no=1,
                                      **{**SAMPLE, "content": f"并发修订 {i}"})

        outcomes = self._run_parallel(revise)
        self.assertEqual(sorted(outcomes), ["VERSION_CONFLICT", "ok"])
        history = self.service.entry_history(self.viewer, entry_id)
        self.assertEqual(len(history["versions"]), 2)

    def test_concurrent_approvals_have_single_winner(self):
        submitted = self.submit()
        task_id = submitted["review_task_id"]
        self.service.create_user(self.admin, username="rev_qian", password="pass123",
                                 roles=["reviewer"])
        tokens = [self.rev_zhao, self._login("rev_qian", "pass123")]

        def approve(i):
            self.service.decide_review(tokens[i], task_id, decision="approve",
                                       rationale=f"复核员 {i} 通过")

        outcomes = self._run_parallel(approve)
        self.assertEqual(sorted(outcomes), ["REVIEW_ALREADY_DECIDED", "ok"])
        # 目录中仍是唯一生效版本
        results = self.service.search_catalog(self.viewer)
        self.assertEqual(len(results), 1)

    def test_concurrent_imports_of_same_batch_have_single_result(self):
        records = [{"source_ref": "ERP-100", **SAMPLE}]

        def do_import(_):
            self.service.import_entries(self.eng_li, batch_key="batch-race", records=records)

        outcomes = self._run_parallel(do_import)
        # 一个批次首次写入，另一个重放同一结果；两种实现路径都算成功
        self.assertEqual(outcomes, ["ok", "ok"])
        self.assertEqual(len(self.service.list_entries(self.viewer)), 1)


if __name__ == "__main__":
    unittest.main()
