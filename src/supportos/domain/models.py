"""SupportOS 领域模型。

Policy Engine 的输入输出全部由这些模型定义。
使用 Pydantic 以获得类型校验 —— 非法参数在进入引擎前就会被拦截。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from supportos.domain.enums import (
    ActionType,
    CustomerTier,
    Decision,
    Intent,
    KnowledgeGap,
    OrderStatus,
    Priority,
    RiskLevel,
    Sentiment,
    ShippingStatus,
    TicketState,
)


# ============================================================
# 业务实体
# ============================================================


class Customer(BaseModel):
    customer_id: str
    name: str
    tier: CustomerTier = CustomerTier.NORMAL
    order_count: int = 0
    lifetime_value: float = 0.0
    refund_count_12m: int = 0
    """过去 12 个月退款次数。>= 3 触发异常升级。"""

    coupon_count_7d: int = 0
    """过去 7 日已领取优惠券次数。>= 2 触发升级。"""

    previous_ticket_count: int = 0


class Order(BaseModel):
    order_id: str
    customer_id: str
    amount: float
    status: OrderStatus
    created_at: datetime
    delivered_at: datetime | None = None
    item_name: str = ""
    has_damage_evidence: bool = False
    """是否有破损证据（图片等）。无证据的补发请求需升级。"""


class Shipment(BaseModel):
    order_id: str
    status: ShippingStatus
    estimated_arrival: date
    last_update: datetime
    carrier: str = ""


class Ticket(BaseModel):
    ticket_id: str
    customer_id: str
    order_id: str | None = None
    message: str
    state: TicketState = TicketState.NEW
    intent: Intent | None = None
    priority: Priority | None = None
    sentiment: Sentiment | None = None
    created_at: datetime = Field(default_factory=datetime.now)


# ============================================================
# Case Context —— Agent 收集到的业务上下文
# ============================================================


class CaseContext(BaseModel):
    """Support Agent 汇总的完整业务上下文，供 Policy Engine 裁决。"""

    ticket: Ticket
    customer: Customer
    order: Order | None = None
    shipment: Shipment | None = None

    # --- 知识库检索结果 ---
    policy_ids: list[str] = Field(default_factory=list)
    """命中的有效政策 ID。为空 → KNOWLEDGE_GAP → 升级。"""

    knowledge_gap: KnowledgeGap = KnowledgeGap.NONE
    knowledge_gap_detail: str = ""

    # --- 时间基准（可注入，便于测试）---
    now: datetime = Field(default_factory=datetime.now)

    # --- 会话内统计（用于额度与频次控制）---
    coupons_issued_today: float = 0.0
    """该客服今日累计发放优惠券金额。"""

    def delay_hours(self) -> float | None:
        """物流延迟小时数。无法计算时返回 None。"""
        if self.shipment is None:
            return None
        est = datetime.combine(self.shipment.estimated_arrival, datetime.min.time())
        return max(0.0, (self.now - est).total_seconds() / 3600.0)

    def days_since_delivery(self) -> int | None:
        """签收后经过的天数。用于售后期限判断。"""
        if self.order is None or self.order.delivered_at is None:
            return None
        return (self.now - self.order.delivered_at).days

    def has_high_value_customer(self) -> bool:
        return self.customer.tier in (CustomerTier.VIP, CustomerTier.SVIP)


# ============================================================
# 动作与裁决
# ============================================================


class ProposedAction(BaseModel):
    """Action Planner 提出的候选动作。注意：这只是『提案』，不等于执行。"""

    action: ActionType
    params: dict[str, Any] = Field(default_factory=dict)
    rationale: str = ""
    proposed_by: str = "ActionPlanner"

    @property
    def amount(self) -> float | None:
        v = self.params.get("amount")
        return float(v) if isinstance(v, (int, float)) else None


class Evidence(BaseModel):
    """Decision Evidence —— PRD 第 17 节。

    展示给用户的是结构化依据，而不是模型内部推理过程。
    """

    recommendation: str = ""
    business_rules: list[str] = Field(default_factory=list)
    """引用的政策 ID 与条款。"""

    customer_context: list[str] = Field(default_factory=list)
    supporting_data: list[str] = Field(default_factory=list)
    risk_level: RiskLevel | None = None


class PolicyDecision(BaseModel):
    """Policy Engine 的裁决结果。"""

    decision: Decision
    risk: RiskLevel | None = None
    reason: str = ""
    rule_id: str = ""
    """命中的规则标识，便于审计与回归测试。"""

    requires_approval_by: str | None = None
    """需要何种角色审批。"""

    evidence: Evidence = Field(default_factory=Evidence)

    # --- 用于展示"被拦截的原因" ---
    blocked_reasons: list[str] = Field(default_factory=list)

    @property
    def is_safe(self) -> bool:
        """只有 AUTO 才算是『AI 自行执行』。其余都需要人参与。"""
        return self.decision is Decision.AUTO


# ============================================================
# 审计
# ============================================================


class AuditEntry(BaseModel):
    """PRD 第 12.5 节：所有 Agent 行为必须留痕。"""

    timestamp: datetime = Field(default_factory=datetime.now)
    ticket_id: str
    actor: str
    """TriageAgent / SupportAgent / ActionPlanner / PolicyEngine / 人类用户名。"""

    action: str
    params: dict[str, Any] = Field(default_factory=dict)
    result: str = ""
    authorized_user: str | None = None
    approval_status: Literal["NONE", "PENDING", "APPROVED", "REJECTED", "MODIFIED"] = "NONE"
