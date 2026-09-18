"""消融实验变体的单元测试。

核心命题：**消融实验本身必须是可信的。**
如果变体引擎没有真正改变系统行为，那对比表就是假的。
因此这里验证每个变体确实造成了预期的行为差异。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from supportos.domain.enums import (
    ActionType,
    CustomerTier,
    Decision,
    Intent,
    OrderStatus,
    Sentiment,
    ShippingStatus,
)
from supportos.domain.models import (
    CaseContext,
    Customer,
    Order,
    ProposedAction,
    Shipment,
    Ticket,
)
from supportos.evals.variants import (
    VARIANTS,
    NoApprovalEngine,
    NullPolicyEngine,
    get_variant,
)
from supportos.policy.engine import CaseMeta, PolicyEngine

NOW = datetime(2026, 9, 17, 14, 0, 0)


def ctx(**kw) -> CaseContext:
    """默认：延迟 130h（超过 72h 补偿门槛）、订单可退的常规场景。"""
    base = dict(
        ticket=Ticket(
            ticket_id="T001",
            customer_id="C001",
            order_id="A1001",
            message="东西晚五天了",
            intent=Intent.SHIPPING_DELAY,
            sentiment=Sentiment.NEUTRAL,
        ),
        customer=Customer(customer_id="C001", name="Alice", tier=CustomerTier.NORMAL),
        order=Order(
            order_id="A1001",
            customer_id="C001",
            amount=299.0,
            status=OrderStatus.DELIVERED,
            created_at=NOW - timedelta(days=10),
            delivered_at=NOW - timedelta(days=2),
        ),
        shipment=Shipment(
            order_id="A1001",
            status=ShippingStatus.DELAYED,
            estimated_arrival=date(2026, 9, 12),
            last_update=NOW - timedelta(hours=6),
        ),
        policy_ids=["SHIPPING_DELAY_POLICY_V3"],
        now=NOW,
    )
    base.update(kw)
    return CaseContext(**base)


def coupon(amount: float = 20) -> ProposedAction:
    return ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": amount})


class TestVariantRegistry:
    def test_three_variants_defined(self):
        assert set(VARIANTS) == {"v0", "v1", "v2"}

    def test_v0_disables_resolve_blocker_others_enable(self):
        """V0 是纯 LLM 基线，本就没有治理层，不应给它装上守卫。"""
        assert VARIANTS["v0"].enforce_resolve_blocker is False
        assert VARIANTS["v1"].enforce_resolve_blocker is True
        assert VARIANTS["v2"].enforce_resolve_blocker is True

    def test_unknown_variant_raises(self):
        with pytest.raises(KeyError):
            get_variant("v99")


class TestNullEngine:
    """V0：无治理 —— 必须对一切动作放行，否则对比表失真。"""

    def test_allows_write_action_that_should_be_gated(self):
        e = NullPolicyEngine(CaseMeta(requester_role="AGENT_L1"))
        d = e.evaluate(coupon(20), ctx())
        assert d.decision is Decision.AUTO

    def test_allows_even_forbidden_action(self):
        """连明确禁止的动作都放行 —— 这才是真正的「无治理」。"""
        e = NullPolicyEngine(CaseMeta())
        d = e.evaluate(ProposedAction(action=ActionType.REFUND_MONEY), ctx())
        assert d.decision is Decision.AUTO

    def test_provides_no_risk_signals(self):
        e = NullPolicyEngine(CaseMeta())
        assert e._check_case_risk_signals(ctx()) is None  # noqa: SLF001
        assert e._check_escalation_signals(ctx()) is None  # noqa: SLF001


class TestNoApprovalEngine:
    """V1：有策略裁决，但审批被自动放行。"""

    def test_approval_required_becomes_auto(self):
        inner = PolicyEngine(meta=CaseMeta(requester_role="AGENT_L1"))
        # 先确认完整引擎确实要求审批
        assert inner.evaluate(coupon(20), ctx()).decision is Decision.APPROVAL_REQUIRED

        e = NoApprovalEngine(CaseMeta(requester_role="AGENT_L1"))
        assert e.evaluate(coupon(20), ctx()).decision is Decision.AUTO

    def test_escalate_is_not_downgraded(self):
        """★ V1 与 V0 的区别正在于此：策略裁决仍然有效。

        只有 APPROVAL_REQUIRED 被自动放行，ESCALATE / DENY 必须保持。
        """
        e = NoApprovalEngine(CaseMeta(requester_role="AGENT_L1"))
        # 物流异常 → 完整引擎判 ESCALATE
        c = ctx(shipment=Shipment(
            order_id="A1001",
            status=ShippingStatus.EXCEPTION,
            estimated_arrival=date(2026, 9, 12),
            last_update=NOW,
        ))
        assert e.evaluate(coupon(20), c).decision is Decision.ESCALATE

    def test_write_action_policy_still_blocks(self):
        """V1 也保留了动作级策略（超上限拒绝等）。"""
        e = NoApprovalEngine(CaseMeta(requester_role="AGENT_L1"))
        assert e.evaluate(coupon(200), ctx()).decision is Decision.DENY

    def test_delegates_risk_signals(self):
        e = NoApprovalEngine(CaseMeta())
        c = ctx(shipment=Shipment(
            order_id="A1001",
            status=ShippingStatus.LOST,
            estimated_arrival=date(2026, 9, 12),
            last_update=NOW,
        ))
        assert e._check_case_risk_signals(c) is not None  # noqa: SLF001


class TestVariantsProduceDifferentBehaviour:
    """★ 最重要的验证：三个变体在同一输入上必须产生不同结果。

    如果它们行为一致，消融实验的对比表就没有意义。
    """

    def test_three_engines_differ_on_gated_action(self):
        c = ctx()
        a = coupon(20)
        v0 = NullPolicyEngine(CaseMeta(requester_role="AGENT_L1")).evaluate(a, c)
        v1 = NoApprovalEngine(CaseMeta(requester_role="AGENT_L1")).evaluate(a, c)
        v2 = PolicyEngine(meta=CaseMeta(requester_role="AGENT_L1")).evaluate(a, c)

        assert v0.decision is Decision.AUTO
        assert v1.decision is Decision.AUTO
        assert v2.decision is Decision.APPROVAL_REQUIRED
        # V1 与 V0 在裁决记录上必须可区分（V1 保留了原始裁决痕迹）
        assert "APPROVED" in v1.rule_id
        assert v0.rule_id == "NULL_ENGINE"
