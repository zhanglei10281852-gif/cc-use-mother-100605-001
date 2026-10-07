"""幂等性：重复导入、重复裁决、重复提交同一来源记录都得到稳定结果。"""

import unittest

from experience_registry import ConflictError
from helpers import SAMPLE, ServiceTestCase


def _record(ref, **overrides):
    record = {"source_ref": ref, **SAMPLE}
    record.update(overrides)
    return record


class IdempotencyTests(ServiceTestCase):
    def test_same_batch_replay_returns_identical_result(self):
        records = [_record("ERP-001"),
                   _record("ERP-002", process_step="粗车")]
        first = self.service.import_entries(self.eng_li, batch_key="batch-1", records=records)
        second = self.service.import_entries(self.eng_li, batch_key="batch-1", records=records)
        self.assertEqual(first, second)
        self.assertEqual(len(first["created"]), 2)
        # 重放不产生新数据
        self.assertEqual(len(self.service.list_entries(self.viewer)), 2)
        self.assertEqual(len(self.service.pending_reviews(self.rev_zhao)), 2)

    def test_overlapping_records_across_batches_are_deduplicated(self):
        self.service.import_entries(self.eng_li, batch_key="batch-1",
                                    records=[_record("ERP-001")])
        result = self.service.import_entries(
            self.eng_li, batch_key="batch-2",
            records=[_record("ERP-001"), _record("ERP-003")])
        self.assertEqual(len(result["created"]), 1)
        self.assertEqual(len(result["duplicates"]), 1)
        self.assertEqual(result["duplicates"][0]["source_ref"], "ERP-001")
        self.assertEqual(len(self.service.list_entries(self.viewer)), 2)

    def test_import_errors_are_reported_without_blocking_valid_records(self):
        bad = {"source_ref": "ERP-404", "equipment_model": "", "process_step": "精车",
               "applicable_conditions": "x", "evidence_source": "y", "content": "z"}
        result = self.service.import_entries(self.eng_li, batch_key="batch-err",
                                             records=[bad, _record("ERP-405")])
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(len(result["created"]), 1)

    def test_duplicate_source_ref_submit_is_conflict(self):
        self.submit(source_ref="ERP-001")
        with self.assertRaises(ConflictError) as ctx:
            self.submit(source_ref="ERP-001")
        self.assertEqual(ctx.exception.code, "DUPLICATE_SOURCE_REF")

    def test_double_approval_is_stable_conflict(self):
        submitted = self.submit()
        task_id = submitted["review_task_id"]
        self.service.decide_review(self.rev_zhao, task_id, decision="approve", rationale="通过")
        with self.assertRaises(ConflictError) as ctx:
            self.service.decide_review(self.rev_zhao, task_id, decision="approve", rationale="重复")
        self.assertEqual(ctx.exception.code, "REVIEW_ALREADY_DECIDED")
        self.assertEqual(ctx.exception.detail["decided_by"], "rev_zhao")


if __name__ == "__main__":
    unittest.main()
