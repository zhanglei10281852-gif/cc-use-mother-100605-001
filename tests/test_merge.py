"""相似条目识别与合并：不同班组术语归一、合并建议、原始版本保留。"""

import unittest

from experience_registry import ConflictError
from experience_registry.models import ENTRY_MERGED
from helpers import SAMPLE, ServiceTestCase


class MergeTests(ServiceTestCase):
    def _submit_variant_with_different_terms(self):
        """二班用"主轴抱死"记录同一经验（一班记作"主轴卡死"）。"""
        return self.submit(
            token=self.eng_wang,
            equipment_model="ca6140 ",
            content=SAMPLE["content"].replace("主轴卡死", "主轴抱死"),
            terms=["二班记录", "主轴抱死"],
        )

    def test_similar_entry_with_different_terminology_triggers_suggestion(self):
        first = self.submit()
        second = self._submit_variant_with_different_terms()
        suggestions = second["merge_suggestions"]
        self.assertEqual(len(suggestions), 1)
        suggestion = suggestions[0]
        self.assertEqual(suggestion["status"], "OPEN")
        self.assertGreaterEqual(suggestion["score"], 0.55)
        pair = {suggestion["entry_id_a"], suggestion["entry_id_b"]}
        self.assertEqual(pair, {first["entry"]["id"], second["entry"]["id"]})
        # 术语归一后内容维度应高度相似
        self.assertGreater(suggestion["reasons"]["content"], 0.8)

    def test_dissimilar_entry_does_not_trigger_suggestion(self):
        self.submit()
        other = self.submit(
            equipment_model="M1432",
            process_step="外圆磨",
            applicable_conditions="砂轮修整后精磨",
            evidence_source="磨床班组记录",
            content="磨削烧伤时降低进给量并检查砂轮硬度。",
        )
        self.assertEqual(other["merge_suggestions"], [])

    def test_suggestion_is_not_duplicated_for_same_pair(self):
        first = self.submit()
        self._submit_variant_with_different_terms()
        # 修订第一条触发重扫，同一对条目仍只保留一条建议
        self.service.revise_entry(self.eng_li, first["entry"]["id"], base_version_no=1, **SAMPLE)
        open_suggestions = self.service.list_merge_suggestions(self.rev_zhao, status="OPEN")
        self.assertEqual(len(open_suggestions), 1)

    def test_accept_merge_preserves_originals_and_updates_catalog(self):
        first = self.submit_and_approve()
        second = self._submit_variant_with_different_terms()
        suggestion = second["merge_suggestions"][0]
        canonical_id = first["entry"]["id"]
        merged_id = second["entry"]["id"]
        decided = self.service.accept_merge(self.rev_zhao, suggestion["id"],
                                            canonical_entry_id=canonical_id,
                                            note="同一经验，保留一班版本为主")
        self.assertEqual(decided["status"], "ACCEPTED")
        # 被合并条目退出目录，主条目仍在
        results = self.service.search_catalog(self.viewer)
        self.assertEqual([r["entry_id"] for r in results], [canonical_id])
        merged_entry = self.service.get_entry(self.viewer, merged_id)
        self.assertEqual(merged_entry["status"], ENTRY_MERGED)
        self.assertEqual(merged_entry["canonical_entry_id"], canonical_id)
        # 原始版本完整保留，链路可查
        history = self.service.entry_history(self.viewer, merged_id)
        self.assertEqual(len(history["versions"]), 1)
        actions = [e["action"] for e in history["timeline"]]
        self.assertIn("MERGE_ACCEPTED", actions)
        # 已处理的建议不能重复裁决
        with self.assertRaises(ConflictError) as ctx:
            self.service.accept_merge(self.rev_zhao, suggestion["id"],
                                      canonical_entry_id=canonical_id)
        self.assertEqual(ctx.exception.code, "MERGE_ALREADY_DECIDED")

    def test_dismissed_suggestion_does_not_reopen(self):
        first = self.submit()
        second = self._submit_variant_with_different_terms()
        suggestion = second["merge_suggestions"][0]
        self.service.dismiss_merge(self.rev_zhao, suggestion["id"],
                                   reason="适用设备批次不同，保留两条")
        # 修订触发重扫，已驳回的条目对不会重新生成建议
        self.service.revise_entry(self.eng_li, first["entry"]["id"], base_version_no=1, **SAMPLE)
        self.assertEqual(self.service.list_merge_suggestions(self.rev_zhao, status="OPEN"), [])
        dismissed = self.service.list_merge_suggestions(self.rev_zhao, status="DISMISSED")
        self.assertEqual(len(dismissed), 1)

    def test_admin_can_extend_terminology_aliases(self):
        self.service.add_term_alias(self.admin, alias="让刀", canonical="刀具让刀")
        aliases = self.service.list_term_aliases(self.viewer)
        self.assertEqual(aliases["让刀"], "刀具让刀")


if __name__ == "__main__":
    unittest.main()
