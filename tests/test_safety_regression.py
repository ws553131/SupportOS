"""安全回归测试 —— 固化端到端验证中发现并修复的三个真实漏洞。

★ 这个文件的存在本身就是作品集的一部分：
  它证明我不是「写完就完」，而是通过端到端验证发现了真实的越权路径，
  并把每个修复都固化成了回归测试，防止重构时漏洞复现。

三个漏洞都发生在「Agent 认为自己在做无害的事」的场景：
  1. 意图不可判定时自动回复 → 工单被自动关闭，无需人工介入
  2. 知识缺口时自动回复 → 在不知道政策的情况下答复客户
  3. 丢件 + 只回复 → 绕过物流异常检查
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from supportos.domain.enums import (
    ActionType,
    CustomerTier,
    Decision,
    Intent,
    KnowledgeGap,
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
from supportos.policy.engine import CaseMeta, PolicyEngine
from supportos.tools.registry import ToolError, ToolRegistry

NOW = datetime(2026, 9, 17, 14, 0, 0)


def ctx(**kw) -> CaseContext:
    base = dict(
        ticket=Ticket(
            ticket_id="T001",
            customer_id="C001",
            order_id="A1001",
            message="我的订单为什么还没到？",
            intent=Intent.SHIPPING_DELAY,
            sentiment=Sentiment.NEUTRAL,
        ),
        customer=Customer(
            customer_id="C001", name="Alice", tier=CustomerTier.NORMAL
        ),
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


def eng(role: str = "AGENT_L1") -> PolicyEngine:
    return PolicyEngine(meta=CaseMeta(requester_id="U100", requester_role=role))


# ============================================================
# 漏洞 1：意图不可判定时，不得自动执行有副作用的动作
# ============================================================


class TestIntentUnknownGuard:
    """发现场景：客户只表达情绪、未说明业务问题，Triage 返回 UNKNOWN。

    原行为：系统自动发出「请补充具体问题」的回复，工单自动关闭，无人介入。
    风险：这类消息恰恰是「情绪对抗 + 试图套取补偿」的高危场景。
    """

    def test_unknown_intent_blocks_send_reply(self):
        """★ 回归点：UNKNOWN + send_reply 必须升级，而不是 AUTO。"""
        c = ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="太差劲了，我要发微博曝光你们！",
                intent=Intent.UNKNOWN,
                sentiment=Sentiment.ANGRY,
            )
        )
        d = eng().evaluate(
            ProposedAction(action=ActionType.SEND_REPLY, params={"text": "请补充具体问题"}), c
        )
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "ESCALATE:INTENT_UNKNOWN"

    def test_unknown_intent_blocks_all_write_actions(self):
        for action in (
            ActionType.SEND_REPLY,
            ActionType.ISSUE_COUPON,
            ActionType.REQUEST_REFUND,
            ActionType.REQUEST_REPLACEMENT,
        ):
            c = ctx(
                ticket=Ticket(
                    ticket_id="T001",
                    customer_id="C001",
                    message="我不管，你们必须给我补偿！",
                    intent=Intent.UNKNOWN,
                    sentiment=Sentiment.ANGRY,
                )
            )
            params = {"amount": 20} if action is ActionType.ISSUE_COUPON else {}
            d = eng().evaluate(ProposedAction(action=action, params=params), c)
            assert d.decision is Decision.ESCALATE, f"{action} 在意图未知时未被拦截"

    @pytest.mark.parametrize(
        "action",
        [
            ActionType.GET_CUSTOMER,
            ActionType.GET_ORDER,
            ActionType.GET_SHIPPING,
            ActionType.SEARCH_KNOWLEDGE,
        ],
    )
    def test_unknown_intent_still_allows_read_tools(self, action):
        """只读动作不受限制 —— 查信息本身不会造成伤害，且是人工判断所必需。"""
        c = ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="随便看看",
                intent=Intent.UNKNOWN,
                sentiment=Sentiment.NEUTRAL,
            )
        )
        d = eng().evaluate(ProposedAction(action=action), c)
        assert d.decision is Decision.AUTO, f"只读动作 {action} 不应被阻断"


# ============================================================
# 漏洞 2：存在知识缺口时，不得自动发出正式回复
# ============================================================


class TestKnowledgeGapOnReply:
    """发现场景：海外订单命中通用退款政策，但政策未覆盖该情况。

    原行为：send_reply 不要求政策，于是在「不知道政策怎么规定」时自动答复客户。
    """

    def test_insufficient_knowledge_blocks_auto_reply(self):
        c = ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="海外订单想部分退款",
                intent=Intent.REFUND_REQUEST,
                sentiment=Sentiment.NEUTRAL,
            ),
            knowledge_gap=KnowledgeGap.INSUFFICIENT,
            knowledge_gap_detail="[GAP-001] 海外订单退款规则缺失",
        )
        d = eng().evaluate(
            ProposedAction(action=ActionType.SEND_REPLY, params={"text": "已为您处理"}), c
        )
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "ESCALATE:KNOWLEDGE_GAP_ON_REPLY"

    def test_rule_conflict_blocks_auto_reply(self):
        c = ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="补偿多少？",
                intent=Intent.SHIPPING_DELAY,
                sentiment=Sentiment.NEUTRAL,
            ),
            knowledge_gap=KnowledgeGap.RULE_CONFLICT,
            knowledge_gap_detail="两条政策上限不一致",
        )
        d = eng().evaluate(
            ProposedAction(action=ActionType.SEND_REPLY, params={"text": "补偿您 ¥30"}), c
        )
        assert d.decision is Decision.ESCALATE

    def test_reply_still_auto_without_gap(self):
        """无知识缺口时，普通回复仍应自动执行（否则产品毫无价值）。"""
        d = eng().evaluate(
            ProposedAction(
                action=ActionType.SEND_REPLY, params={"text": "您的包裹正在运输中"}
            ),
            ctx(),
        )
        assert d.decision is Decision.AUTO


# ============================================================
# 漏洞 3：情境性硬信号必须独立于动作类型
# ============================================================


class TestCaseSignalGuard:
    """发现场景：丢件工单，Agent 只打算回一条消息。

    原行为：升级信号只在非 GREEN 动作上检查 → send_reply 是 GREEN → 绕过检查 → AUTO。
    根因：把「案件客观事实」和「客户情绪」两类信号混为一谈。
    """

    @pytest.mark.parametrize(
        "status", [ShippingStatus.EXCEPTION, ShippingStatus.LOST]
    )
    @pytest.mark.parametrize(
        "action,params",
        [
            (ActionType.SEND_REPLY, {"text": "我们会尽快处理"}),
            (ActionType.ISSUE_COUPON, {"amount": 20}),
        ],
    )
    def test_abnormal_shipping_escalates_regardless_of_action(self, status, action, params):
        """★ 回归点：物流异常/丢件时，任何动作都必须转人工。"""
        c = ctx(
            shipment=Shipment(
                order_id="A1001",
                status=status,
                estimated_arrival=date(2026, 9, 12),
                last_update=NOW,
            )
        )
        d = eng().evaluate(ProposedAction(action=action, params=params), c)
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "ESCALATE:CASE_SIGNAL"

    def test_normal_shipping_reply_still_auto(self):
        """对照组：物流正常时回复仍可自动执行。"""
        c = ctx(
            shipment=Shipment(
                order_id="A1001",
                status=ShippingStatus.IN_TRANSIT,
                estimated_arrival=date(2026, 9, 20),
                last_update=NOW,
            )
        )
        d = eng().evaluate(
            ProposedAction(action=ActionType.SEND_REPLY, params={"text": "运输中"}), c
        )
        assert d.decision is Decision.AUTO

    def test_angry_customer_does_not_block_read_tools(self):
        """对照：情绪信号只拦写操作，不拦只读动作（否则客户越生气系统越没用）。"""
        c = ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="我要投诉到消协！",
                intent=Intent.SHIPPING_DELAY,
                sentiment=Sentiment.ANGRY,
            )
        )
        assert eng().evaluate(ProposedAction(action=ActionType.GET_ORDER), c).decision is Decision.AUTO
        assert (
            eng().evaluate(
                ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), c
            ).decision
            is Decision.ESCALATE
        )


# ============================================================
# 漏洞 4：Tool 层归属校验在编排路径中从未生效
# ============================================================


class TestCrossCustomerDataAccess:
    """发现场景（对抗案例 B-17）：客户消息里报出别人的订单号。

    原行为：`SupportAgent._invoke` 调用 get_order/get_shipping 时
            没有传 requester_customer_id，于是 Tool 层写好的归属校验
            在真实编排路径中从未生效 —— LLM 直接按消息里说的订单号查到了他人数据。
    另：get_shipping 本身也没有归属校验参数，形成「查不到订单但能查到物流」的越权读路径。

    ★ 教训：校验「写了」不等于「生效了」。
      必须验证每一条真实调用路径都传入了校验所需的参数。
    """

    def test_get_shipping_enforces_ownership(self):
        r = ToolRegistry()
        order = r.get_order("A1001")
        with pytest.raises(ToolError, match="越权访问"):
            r.get_shipping("A1001", requester_customer_id="C9999")
        # 归属人自己查应当成功
        s = r.get_shipping("A1001", requester_customer_id=order.customer_id)
        assert s.order_id == "A1001"

    def test_support_agent_cannot_read_other_customer_order(self):
        """Agent 必须带着工单归属人去查，否则越权读取他人订单。"""
        r = ToolRegistry()
        from supportos.agents.support import SupportAgent

        agent = SupportAgent(r, use_llm=False)
        # C0001 的工单，但试图查询 A1002（属于 C0002）
        g = agent.gather("帮我查 A1002", Intent.SHIPPING_DELAY, "A1002", "C0001")
        assert any("越权" in f for f in g.tool_failures), (
            f"Agent 未触发归属校验，tool_failures={g.tool_failures}"
        )

    def test_support_agent_can_read_own_order(self):
        r = ToolRegistry()
        from supportos.agents.support import SupportAgent

        order = r.get_order("A1001")
        agent = SupportAgent(r, use_llm=False)
        g = agent.gather(
            "查我的物流", Intent.SHIPPING_DELAY, "A1001", order.customer_id
        )
        assert not any("越权" in f for f in g.tool_failures)
        assert "get_shipping" in g.executed_reads
