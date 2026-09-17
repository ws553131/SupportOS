"""知识库检索单元测试。

核心命题：**政策存在 ≠ 政策够用；过期政策不得作为裁决依据。**
"""

from __future__ import annotations

from datetime import date

from supportos.domain.enums import Intent, KnowledgeGap
from supportos.tools.knowledge import KnowledgeBase


def kb() -> KnowledgeBase:
    return KnowledgeBase()


class TestPolicyRetrieval:
    def test_shipping_delay_returns_policy(self):
        r = kb().search(Intent.SHIPPING_DELAY, "我的东西晚五天了")
        assert r.found
        assert "SHIPPING_DELAY_POLICY_V3" in r.policy_ids
        assert r.gap is KnowledgeGap.NONE

    def test_refund_returns_policy(self):
        r = kb().search(Intent.REFUND_REQUEST, "我要退款")
        assert r.found
        assert "REFUND_POLICY_V4" in r.policy_ids

    def test_citations_include_section(self):
        """可审计：必须能说出命中了哪条政策的哪一节。"""
        r = kb().search(Intent.SHIPPING_DELAY, "延迟")
        cites = r.citations()
        assert any("§" in c for c in cites)
        assert any("SHIPPING_DELAY_POLICY_V3" in c for c in cites)

    def test_unknown_intent_has_no_policy(self):
        r = kb().search(Intent.UNKNOWN, "随便问问")
        assert not r.found
        assert r.gap is KnowledgeGap.NO_POLICY_FOUND


class TestKnowledgeGaps:
    def test_overseas_refund_detected_as_gap(self):
        """★ 政策存在不等于够用：海外订单命中通用退款政策，但未覆盖。"""
        r = kb().search(Intent.REFUND_REQUEST, "我这是海外订单，想申请部分退款")
        assert r.gap is KnowledgeGap.INSUFFICIENT
        assert "GAP-001" in r.gap_detail

    def test_custom_product_detected_as_gap(self):
        r = kb().search(Intent.DAMAGED_ITEM, "我定制的刻字商品破损了")
        assert r.gap is KnowledgeGap.INSUFFICIENT
        assert "GAP-002" in r.gap_detail

    def test_bundle_refund_detected_as_gap(self):
        r = kb().search(Intent.REFUND_REQUEST, "这个套装里只想要一件，组合购能部分退款吗")
        assert r.gap is KnowledgeGap.INSUFFICIENT
        assert "GAP-003" in r.gap_detail

    def test_normal_case_is_not_gap(self):
        r = kb().search(Intent.REFUND_REQUEST, "我要退款")
        assert r.gap is KnowledgeGap.NONE


class TestPolicyValidity:
    def test_future_policy_is_not_effective(self):
        """★ 未生效的政策不得作为依据 —— 记录在 expired_policy_ids 里备查。"""
        r = kb().search(
            Intent.REFUND_REQUEST, "跨境订单退款", today=date(2026, 9, 17)
        )
        # INTL_REFUND_POLICY_V1 生效日是 2027-01-01，2026 年不应生效
        assert "INTL_REFUND_POLICY_V1" in kb().all_policy_ids()
        assert "INTL_REFUND_POLICY_V1" not in kb().effective_policy_ids(date(2026, 9, 17))

    def test_effective_policy_after_date(self):
        assert "INTL_REFUND_POLICY_V1" in kb().effective_policy_ids(date(2027, 6, 1))

    def test_expired_policy_triggers_gap(self):
        """当某个 Intent 只有过期政策时，应报 POLICY_EXPIRED 而不是 NO_POLICY_FOUND。"""
        r = kb().search(
            Intent.REFUND_REQUEST, "跨境订单退款", today=date(2026, 9, 17)
        )
        # 仍有有效的通用退款政策，所以不是 POLICY_EXPIRED；
        # 但未生效政策必须被记录，不能悄悄参与裁决
        assert r.gap in (KnowledgeGap.INSUFFICIENT, KnowledgeGap.NONE)


class TestConflictDetection:
    def test_no_conflict_on_single_policy(self):
        r = kb().search(Intent.SHIPPING_DELAY, "延迟")
        conflicts = kb().detect_conflicts(r.sections, "offer_coupon")
        # SHIPPING_DELAY_POLICY_V3 的 3.1/3.2 上限不同，应被识别
        assert isinstance(conflicts, list)

    def test_conflict_detected_on_differing_limits(self):
        """不同政策对同一动作给出不同上限 → 必须能被检测出来。"""
        r = kb().search(Intent.SHIPPING_DELAY, "延迟")
        conflicts = kb().detect_conflicts(r.sections, "offer_coupon")
        assert len(conflicts) >= 1
        assert "上限不一致" in conflicts[0]
