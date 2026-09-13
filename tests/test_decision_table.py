from __future__ import annotations

import unittest

from ai_disk_assistant.ai_advisor import _validate_unit_evidence
from ai_disk_assistant.models import Advice
from ai_disk_assistant.safety import advice_rank, decide_unit_advice


def ai_advice(level: str = "建议删除", evidence: list[str] | None = None) -> Advice:
    return Advice(
        recommend_delete=level == "建议删除",
        purpose="日志文件",
        advice_level=level,
        reason="日志残留，可清理。",
        source="ai",
        evidence=evidence if evidence is not None else ["suffix=.log"],
    )


def guard(level: str, source: str = "local-guard") -> Advice:
    return Advice(
        recommend_delete=False,
        purpose="系统文件" if level == "不建议删除" else "未知用途",
        advice_level=level,
        reason="本地规则结论。",
        source=source,
    )


class DecisionTableTests(unittest.TestCase):
    """决策表真值表：最终建议 = min(AI 等级, 守卫上限, 证据闸门)。

    AI 是主判断者：守卫为 None（守卫放行的未知类型）时 AI 有完整判断权，
    包括"建议删除"；用户内容/受保护目录由守卫上限压住，AI 无权越级。
    """

    def test_ai_delete_passes_when_guard_silent(self) -> None:
        final = decide_unit_advice(None, ai_advice(), evidence_ok=True)
        self.assertEqual(final.advice_level, "建议删除")
        self.assertTrue(final.recommend_delete)
        self.assertEqual(final.source, "unit-hybrid-ai")

    def test_missing_evidence_forces_manual_confirm(self) -> None:
        final = decide_unit_advice(None, ai_advice(evidence=[]), evidence_ok=False)
        self.assertEqual(final.advice_level, "人工确认")
        self.assertFalse(final.recommend_delete)
        self.assertEqual(final.source, "unit-fallback")

    def test_wrong_evidence_fails_validation(self) -> None:
        payload = {"suffix": ".log", "file_count": 2}
        self.assertFalse(_validate_unit_evidence(payload, ["suffix=.tmp"]))
        self.assertFalse(_validate_unit_evidence(payload, ["file_count=999"]))
        self.assertFalse(_validate_unit_evidence(payload, ["unknown_field=1"]))
        self.assertFalse(_validate_unit_evidence(payload, ["no_separator"]))
        self.assertFalse(_validate_unit_evidence(payload, []))
        self.assertTrue(_validate_unit_evidence(payload, ["suffix=.log", "file_count=2"]))

    def test_guard_never_delete_cannot_be_upgraded(self) -> None:
        final = decide_unit_advice(guard("不建议删除"), ai_advice(), evidence_ok=True)
        self.assertEqual(final.advice_level, "不建议删除")
        self.assertFalse(final.recommend_delete)
        self.assertEqual(final.purpose, "系统文件")

    def test_guard_manual_confirm_caps_at_caution(self) -> None:
        final = decide_unit_advice(guard("人工确认"), ai_advice(), evidence_ok=True)
        self.assertEqual(final.advice_level, "谨慎删除")
        self.assertFalse(final.recommend_delete)

    def test_guard_caution_caps_at_caution(self) -> None:
        final = decide_unit_advice(guard("谨慎删除", source="local-rule"), ai_advice(), evidence_ok=True)
        self.assertEqual(final.advice_level, "谨慎删除")
        self.assertEqual(final.source, "unit-hybrid-guarded")

    def test_ai_more_conservative_than_guard_wins(self) -> None:
        final = decide_unit_advice(guard("人工确认"), ai_advice("不建议删除"), evidence_ok=True)
        self.assertEqual(final.advice_level, "不建议删除")

    def test_local_rule_delete_guard_allows_ai_downgrade(self) -> None:
        # 本地直判"建议删除"（AI 不可用时的结论）不绑架 AI：AI 可降为谨慎删除。
        final = decide_unit_advice(guard("建议删除", source="local-rule"), ai_advice("谨慎删除"), evidence_ok=True)
        self.assertEqual(final.advice_level, "谨慎删除")
        self.assertFalse(final.recommend_delete)

    def test_local_rule_delete_guard_and_ai_agree(self) -> None:
        final = decide_unit_advice(guard("建议删除", source="local-rule"), ai_advice(), evidence_ok=True)
        self.assertEqual(final.advice_level, "建议删除")
        self.assertTrue(final.recommend_delete)

    def test_ai_caution_passes_through(self) -> None:
        final = decide_unit_advice(None, ai_advice("谨慎删除"), evidence_ok=True)
        self.assertEqual(final.advice_level, "谨慎删除")
        self.assertEqual(final.source, "unit-hybrid-ai")

    def test_rank_is_total_order(self) -> None:
        self.assertLess(advice_rank("不建议删除"), advice_rank("人工确认"))
        self.assertLess(advice_rank("人工确认"), advice_rank("谨慎删除"))
        self.assertLess(advice_rank("谨慎删除"), advice_rank("建议删除"))
        # 未知等级回落到保守档。
        self.assertEqual(advice_rank("不存在的等级"), advice_rank("人工确认"))


if __name__ == "__main__":
    unittest.main()
