"""Policy Engine 单元测试。

★ 这是 W1 最重要的产出。

面试话术：「我的 Policy Engine 有 N 个单元测试，覆盖全部风险分支与权限边界。」
这句话的分量远超「我写了个权限模块」。

测试组织原则：
  - 每个风险等级（GREEN/YELLOW/RED）独立测试
  - 权限边界必须测『刚好等于阈值』和『刚好超过阈值』两侧
  - 安全相关的失败必须显式断言（不能只断言 decision 相等）
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
    RiskLevel,
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
from supportos.policy.permissions import effective_agent_permission, human_permission

NOW = datetime(2026, 9, 17, 14, 0, 0)
DAY = 24 * 3600.0


# ============================================================
# 测试夹具
# ============================================================


def make_customer(**kw) -> Customer:
    base = dict(
        customer_id="C001",
        name="Alice",
        tier=CustomerTier.NORMAL,
        order_count=3,
        lifetime_value=1200.0,
        refund_count_12m=0,
        coupon_count_7d=0,
        previous_ticket_count=0,
    )
    base.update(kw)
    return Customer(**base)


def make_order(**kw) -> Order:
    base = dict(
        order_id="A1001",
        customer_id="C001",
        amount=299.0,
        status=OrderStatus.DELIVERED,
        created_at=NOW - timedelta(days=10),
        delivered_at=NOW - timedelta(days=2),
        item_name="蓝牙耳机",
        has_damage_evidence=False,
    )
    base.update(kw)
    return Order(**base)


def make_shipment(**kw) -> Shipment:
    """默认：预计 9/12 到达，当前 9/17 → 延迟 120 小时（超过 72h 门槛）。"""
    base = dict(
        order_id="A1001",
        status=ShippingStatus.DELAYED,
        estimated_arrival=date(2026, 9, 12),
        last_update=NOW - timedelta(hours=6),
        carrier="SF",
    )
    base.update(kw)
    return Shipment(**base)


def make_ctx(**kw) -> CaseContext:
    base = dict(
        ticket=Ticket(
            ticket_id="T001",
            customer_id="C001",
            order_id="A1001",
            message="我的订单为什么还没到？",
            intent=Intent.SHIPPING_DELAY,
            sentiment=Sentiment.NEUTRAL,
        ),
        customer=make_customer(),
        order=make_order(),
        shipment=make_shipment(),
        policy_ids=["SHIPPING_DELAY_POLICY_V3"],
        now=NOW,
        coupons_issued_today=0.0,
    )
    base.update(kw)
    return CaseContext(**base)


def engine(role: str = "AGENT_L1") -> PolicyEngine:
    return PolicyEngine(meta=CaseMeta(requester_id="U100", requester_role=role))


# ============================================================
# 1. 权限模型
# ============================================================


class TestPermissionModel:
    def test_agent_coupon_capped_below_human(self):
        """Agent 的优惠券上限必须低于人类（¥30 < ¥50）。"""
        human = human_permission("AGENT_L1")
        agent = effective_agent_permission("AGENT_L1")
        assert human.coupon_max == 50.0
        assert agent.coupon_max == 30.0
        assert agent.coupon_max < human.coupon_max

    def test_agent_cannot_refund_directly(self):
        """Agent 的退款上限必须为 0 —— 只能 request_refund，不能 refund_money。"""
        assert effective_agent_permission("AGENT_L1").refund_max == 0.0
        assert effective_agent_permission("SUPERVISOR").refund_max == 0.0

    def test_agent_can_never_escalate_on_its_own(self):
        """AI 永远不能自行决定升级。"""
        assert effective_agent_permission("SUPERVISOR").can_escalate is False

    def test_unknown_role_gets_no_write_permission(self):
        """未知角色 fail-safe：不给任何写权限，而不是给默认权限。"""
        p = human_permission("HACKER_ROLE")
        assert p.coupon_max == 0.0
        assert p.refund_max == 0.0
        assert p.can_replace is False


# ============================================================
# 2. GREEN：只读动作自动执行
# ============================================================


class TestReadTools:
    @pytest.mark.parametrize(
        "action",
        [
            ActionType.GET_CUSTOMER,
            ActionType.GET_ORDER,
            ActionType.GET_SHIPPING,
            ActionType.SEARCH_KNOWLEDGE,
        ],
    )
    def test_read_tools_auto_execute(self, action):
        d = engine().evaluate(ProposedAction(action=action), make_ctx())
        assert d.decision is Decision.AUTO
        assert d.risk is RiskLevel.GREEN

    def test_read_tool_not_blocked_by_angry_customer(self):
        """客户生气时更需要 Agent 自动查信息，只读动作不应被情绪阻断。"""
        ctx = make_ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="我要投诉到消协！",
                sentiment=Sentiment.ANGRY,
            )
        )
        d = engine().evaluate(ProposedAction(action=ActionType.GET_ORDER), ctx)
        assert d.decision is Decision.AUTO


# ============================================================
# 3. 优惠券 issue_coupon
# ============================================================


class TestIssueCoupon:
    def test_within_agent_limit_requires_approval(self):
        """¥30 恰好等于 Agent 上限 → 需审批（边界含等号）。"""
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 30}), make_ctx()
        )
        assert d.decision is Decision.APPROVAL_REQUIRED
        assert d.risk is RiskLevel.YELLOW

    def test_over_agent_limit_denied(self):
        """¥60 超出 Agent 上限 ¥30 → 拒绝。"""
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 60}), make_ctx()
        )
        assert d.decision is Decision.DENY

    def test_over_human_permission_denied(self):
        """¥200 超出客服 L1 的 ¥50 权限 → 拒绝。"""
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 200}), make_ctx()
        )
        assert d.decision is Decision.DENY

    def test_negative_amount_denied(self):
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": -50}), make_ctx()
        )
        assert d.decision is Decision.DENY

    def test_delay_below_sla_denied(self):
        """延迟仅 24 小时，未达 72 小时补偿门槛 → 拒绝（不能滥发补偿）。"""
        ctx = make_ctx(
            shipment=make_shipment(estimated_arrival=date(2026, 9, 16)),
            now=datetime(2026, 9, 17, 0, 0, 0),
        )
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.DENY
        assert "未达到补偿门槛" in d.reason

    def test_frequency_exceeded_escalates(self):
        """该客户 7 日内已领 2 次 → 强制升级（防止重复补偿）。"""
        ctx = make_ctx(customer=make_customer(coupon_count_7d=2))
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.ESCALATE
        assert d.risk is RiskLevel.RED

    def test_daily_total_exceeded_escalates(self):
        """今日已发 ¥490，再发 ¥20 将超单日上限 ¥500 → 升级。"""
        ctx = make_ctx(coupons_issued_today=490.0)
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.ESCALATE

    def test_no_policy_knowledge_gap_escalates(self):
        """知识库无政策支撑 → 禁止编造规则 → 升级。"""
        ctx = make_ctx(policy_ids=[])
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "KNOWLEDGE_GAP"


# ============================================================
# 4. 退款 request_refund
# ============================================================


class TestRequestRefund:
    def test_small_refund_requires_approval(self):
        """¥200 恰好等于审批线 → 审批（★ 已确认阈值 ¥200，含等号）。"""
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 200}), make_ctx()
        )
        assert d.decision is Decision.APPROVAL_REQUIRED
        assert d.risk is RiskLevel.YELLOW

    def test_above_approval_line_escalates(self):
        """¥299 超过审批线 → 强制升级（PRD 12.4 的例子）。"""
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 299}), make_ctx()
        )
        assert d.decision is Decision.ESCALATE
        assert d.risk is RiskLevel.RED

    def test_above_user_limit_escalates(self):
        """¥300 超出 L1 客服 ¥200 退款权限 → 升级而非拒绝。

        ★ 关键设计：这不是「违规」，而是「授权不足」。
          DENY 会让工单死掉、客户问题无人解决；ESCALATE 把决策权交给主管。
          —— 这直接体现 PRD 第 20 节的优先级（安全 > 自动化率）。
        """
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 300}), make_ctx()
        )
        assert d.decision is Decision.ESCALATE
        assert "权限" in d.reason

    def test_abnormal_refund_history_escalates(self):
        """12 个月退款 3 次 → 异常，强制升级。"""
        ctx = make_ctx(customer=make_customer(refund_count_12m=3))
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 150}), ctx
        )
        assert d.decision is Decision.ESCALATE
        assert "异常阈值" in d.reason

    def test_already_refunded_order_denied(self):
        ctx = make_ctx(order=make_order(status=OrderStatus.REFUNDED))
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 100}), ctx
        )
        assert d.decision is Decision.DENY

    def test_beyond_after_sale_window_denied(self):
        """签收 20 天，超过 15 天售后期限 → 拒绝。"""
        ctx = make_ctx(order=make_order(delivered_at=NOW - timedelta(days=20)))
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 100}), ctx
        )
        assert d.decision is Decision.DENY
        assert "售后期限" in d.reason

    def test_no_policy_escalates(self):
        ctx = make_ctx(policy_ids=[])
        d = engine().evaluate(
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 100}), ctx
        )
        assert d.decision is Decision.ESCALATE


# ============================================================
# 5. 补发 request_replacement
# ============================================================


class TestRequestReplacement:
    def test_with_evidence_requires_approval(self):
        ctx = make_ctx(order=make_order(has_damage_evidence=True, amount=299.0))
        d = engine().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx)
        assert d.decision is Decision.APPROVAL_REQUIRED

    def test_without_evidence_escalates(self):
        ctx = make_ctx(order=make_order(has_damage_evidence=False))
        d = engine().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx)
        assert d.decision is Decision.ESCALATE
        assert "证据" in d.reason

    def test_high_value_item_escalates(self):
        """商品价值 ¥800 超过自动补发上限 ¥500 → 升级。"""
        ctx = make_ctx(order=make_order(has_damage_evidence=True, amount=800.0))
        d = engine().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx)
        assert d.decision is Decision.ESCALATE


# ============================================================
# 6. 回复客户 send_reply
# ============================================================


class TestSendReply:
    def test_plain_reply_auto(self):
        """不含承诺的回复可自动发送。"""
        d = engine().evaluate(
            ProposedAction(
                action=ActionType.SEND_REPLY,
                params={"text": "您的包裹正在运输中，预计明日到达。"},
            ),
            make_ctx(),
        )
        assert d.decision is Decision.AUTO

    def test_reply_with_commitment_requires_approval(self):
        """含承诺性表述 → 需人工确认。

        ★ 设计说明：承诺由 Action Planner 抽取成结构化字段，
          引擎只判断『是否存在承诺』，不让 LLM 自己判断自己对不对。
        """
        d = engine().evaluate(
            ProposedAction(
                action=ActionType.SEND_REPLY,
                params={
                    "text": "我们为您补偿 ¥20 优惠券。",
                    "commitments": [{"type": "coupon", "amount": 20}],
                },
            ),
            make_ctx(),
        )
        assert d.decision is Decision.APPROVAL_REQUIRED


# ============================================================
# 7. 明确禁止的动作
# ============================================================


class TestForbiddenActions:
    @pytest.mark.parametrize(
        "action",
        [
            ActionType.REFUND_MONEY,
            ActionType.BAN_ACCOUNT,
            ActionType.DELETE_TICKET,
            ActionType.MODIFY_AUDIT_LOG,
        ],
    )
    def test_forbidden_actions_always_denied(self, action):
        """即使是主管发起，这些动作也必须被拒绝。"""
        for role in ("AGENT_L1", "AGENT_L2", "SUPERVISOR"):
            d = engine(role).evaluate(ProposedAction(action=action), make_ctx())
            assert d.decision is Decision.DENY, f"{action} 在 {role} 下未被拒绝"
            assert "明确禁止" in d.reason


# ============================================================
# 8. 升级信号（可观测，不依赖 LLM 自报置信度）
# ============================================================


class TestEscalationSignals:
    def test_angry_with_legal_threat_escalates(self):
        """情绪激动 + 提及投诉渠道 → 强制升级。

        这是对抗性用例：客户用情绪压力试图获取超额补偿。
        """
        ctx = make_ctx(
            ticket=Ticket(
                ticket_id="T001",
                customer_id="C001",
                message="都延迟五天了！你们再不补偿我就投诉到消协！",
                sentiment=Sentiment.ANGRY,
            )
        )
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 30}), ctx
        )
        assert d.decision is Decision.ESCALATE
        assert "敏感渠道" in d.reason

    def test_shipping_exception_escalates(self):
        ctx = make_ctx(shipment=make_shipment(status=ShippingStatus.EXCEPTION))
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.ESCALATE

    def test_lost_package_escalates(self):
        ctx = make_ctx(shipment=make_shipment(status=ShippingStatus.LOST))
        d = engine().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx)
        assert d.decision is Decision.ESCALATE

    def test_missing_shipment_info_denied(self):
        """缺少物流信息时无法判定补偿条件，不能默认放行。"""
        ctx = make_ctx(shipment=None)
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.DENY


# ============================================================
# 9. 方案级裁决
# ============================================================


class TestEvaluatePlan:
    def test_duplicate_action_escalates(self):
        """同一方案内重复发券 → 升级（防止重复执行）。"""
        actions = [
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}),
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}),
        ]
        decisions = engine().evaluate_plan(actions, make_ctx())
        assert decisions[0].decision is Decision.APPROVAL_REQUIRED
        assert decisions[1].decision is Decision.ESCALATE
        assert decisions[1].rule_id == "DUPLICATE_ACTION"


# ============================================================
# 10. 审批后重新校验（★ 已确认的产品决策）
# ============================================================


class TestRevalidation:
    def test_revalidate_ok_when_context_unchanged(self):
        """上下文未变 → 重新校验通过，可执行。"""
        action = ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20})
        ctx = make_ctx()
        d = engine().revalidate(action, ctx, make_ctx())
        assert d.decision is Decision.APPROVAL_REQUIRED
        assert "REVALIDATE_OK" in d.rule_id

    def test_revalidate_fails_when_order_already_refunded(self):
        """★ 关键场景：主管批准后，订单已被其他渠道退款 → 必须中止执行。"""
        action = ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 150})
        before = make_ctx()
        after = make_ctx(order=make_order(status=OrderStatus.REFUNDED))

        d = engine().revalidate(action, before, after)
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "REVALIDATE_FAILED"
        assert "上下文已变化" in d.reason

    def test_revalidate_fails_when_customer_got_another_coupon(self):
        """批准期间客户又从别的客服领了券 → 重新校验应拦截。"""
        action = ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20})
        before = make_ctx(customer=make_customer(coupon_count_7d=0))
        after = make_ctx(customer=make_customer(coupon_count_7d=2))

        d = engine().revalidate(action, before, after)
        assert d.decision is Decision.ESCALATE
        assert d.rule_id == "REVALIDATE_FAILED"


# ============================================================
# 11. 安全红线总检查
# ============================================================


class TestSafetyInvariants:
    def test_no_write_action_is_ever_AUTO_without_policy(self):
        """红线：任何写操作在无政策支撑时都不得自动执行。"""
        write_actions = [
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}),
            ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": 100}),
            ProposedAction(action=ActionType.REQUEST_REPLACEMENT),
        ]
        ctx = make_ctx(policy_ids=[], knowledge_gap=KnowledgeGap.NO_POLICY_FOUND)
        for a in write_actions:
            d = engine().evaluate(a, ctx)
            assert d.decision is not Decision.AUTO, f"{a.action} 在无政策时被自动执行"
            assert d.decision is not Decision.APPROVAL_REQUIRED

    def test_escalate_is_always_red_risk(self):
        """红线：强制升级的动作必须标记为高风险。"""
        ctx = make_ctx(policy_ids=[])
        d = engine().evaluate(
            ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": 20}), ctx
        )
        assert d.decision is Decision.ESCALATE
        assert d.risk is RiskLevel.RED
