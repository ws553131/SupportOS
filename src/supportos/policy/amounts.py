"""金额与参数计算 —— 业务规则的单一真相源。

★ 核心设计决策（已确认）：**LLM 选动作，规则定参数。**

为什么金额绝不能交给 LLM 生成：
  1. **可复现**：同样的 case 必须算出同样的金额。LLM 有随机性，今天 ¥20 明天 ¥25，
     这在财务口径上是不可接受的。
  2. **可审计**：主管问「为什么是 ¥20」时，答案必须是「政策 3.1 规定延迟 >72h
     补偿 ¥10~¥30，取中位数」，而不是「模型觉得差不多」。
  3. **可测试**：参数由规则计算，才能写出「任何情况下金额都不超政策上限」的断言。
     LLM 自由生成金额时，这条断言根本无法成立。

因此 Action Planner 的职责被收窄为「从允许的动作集合里选一个」，
所有数值参数由本模块计算。
"""

from __future__ import annotations

from dataclasses import dataclass

from supportos import constants as C
from supportos.domain.enums import CustomerTier, Intent
from supportos.domain.models import CaseContext, PolicyDecision
from supportos.tools.knowledge import KnowledgeBase, PolicySection


@dataclass(frozen=True)
class AmountComputation:
    amount: float
    basis: str
    """计算依据的人类可读说明 —— 会进入 Decision Evidence 与审计日志。"""

    policy_cite: str = ""
    clamped: bool = False
    """是否因为权限/政策上限而被截断。被截断必须留痕。"""


def _delay_tier(delay_hours: float) -> str:
    if delay_hours > 168:
        return "SEVERE"      # > 7 天
    if delay_hours > 72:
        return "MODERATE"    # > 3 天
    return "NONE"


def compute_coupon_amount(
    ctx: CaseContext, kb: KnowledgeBase | None = None, perm_coupon_max: float | None = None
) -> AmountComputation:
    """按政策计算物流延迟补偿金额。

    规则来源：SHIPPING_DELAY_POLICY_V3 §3.1 / §3.2
      - 延迟 > 72h：¥10~¥30
      - 延迟 > 168h：¥30~¥50（超过 ¥30 部分需主管审批）
    取值策略：取区间下沿（保守），而不是中位数或上限 ——
    补偿金额「够表达歉意」即可，不需要给满。
    """
    hours = ctx.delay_hours() or 0.0
    tier = _delay_tier(hours)

    if tier == "SEVERE":
        raw, basis = 30.0, f"延迟 {hours:.0f}h > 168h，政策 §3.2 区间 ¥30~¥50，取区间下沿 ¥30"
        cite = "SHIPPING_DELAY_POLICY_V3 §3.2"
    elif tier == "MODERATE":
        raw, basis = 10.0, f"延迟 {hours:.0f}h > 72h，政策 §3.1 区间 ¥10~¥30，取区间下沿 ¥10"
        cite = "SHIPPING_DELAY_POLICY_V3 §3.1"
    else:
        raw, basis = 0.0, f"延迟仅 {hours:.0f}h，未达补偿门槛 72h，金额为 0"
        cite = "SHIPPING_DELAY_POLICY_V3 §3.5"

    # --- 硬性上限：无论政策怎么写，都不得突破 Agent 策略上限 ---
    limit = perm_coupon_max if perm_coupon_max is not None else C.AGENT_COUPON_MAX
    if raw > limit:
        return AmountComputation(
            amount=limit,
            basis=f"{basis}；但受 Agent 优惠券上限约束，截断为 ¥{limit:g}",
            policy_cite=cite,
            clamped=True,
        )

    # --- VIP 可上浮，但同样不得突破上限 ---
    if (
        ctx.customer.tier in (CustomerTier.VIP, CustomerTier.SVIP)
        and tier == "MODERATE"
        and raw < C.POLICY_MAX_COUPON
    ):
        boosted = min(C.POLICY_MAX_COUPON, limit)
        return AmountComputation(
            amount=boosted,
            basis=f"{basis}；客户为 {ctx.customer.tier.value}，按 §3.3 上浮至 ¥{boosted:g}",
            policy_cite=cite,
        )

    return AmountComputation(amount=raw, basis=basis, policy_cite=cite)


def compute_refund_amount(ctx: CaseContext) -> AmountComputation:
    """退款金额 = 订单金额（V0.1 不支持部分退款）。

    部分退款涉及优惠分摊、组合购拆分等复杂规则，
    而这些场景已登记为知识缺口（GAP-003），因此 V0.1 只做全额退款。
    """
    if ctx.order is None:
        return AmountComputation(amount=0.0, basis="缺少订单信息，无法计算退款金额")
    return AmountComputation(
        amount=float(ctx.order.amount),
        basis=f"订单 #{ctx.order.order_id} 全额退款 ¥{ctx.order.amount:g}"
        "（V0.1 不支持部分退款，相关场景已登记为知识缺口）",
        policy_cite="REFUND_POLICY_V4 §4.2",
    )


def compute_params(
    intent: Intent,
    ctx: CaseContext,
    kb: KnowledgeBase | None = None,
    perm_coupon_max: float | None = None,
) -> tuple[dict, AmountComputation | None]:
    """按意图计算动作参数。返回 (params, 金额计算说明)。"""
    from supportos.domain.enums import ActionType

    if intent is Intent.SHIPPING_DELAY:
        comp = compute_coupon_amount(ctx, kb, perm_coupon_max)
        return {"amount": comp.amount}, comp
    if intent is Intent.REFUND_REQUEST:
        comp = compute_refund_amount(ctx)
        return {"amount": comp.amount}, comp
    if intent is Intent.DAMAGED_ITEM:
        return {}, None
    return {}, None


def validate_amount_against_policy(
    amount: float, sections: list[PolicySection]
) -> tuple[bool, str]:
    """校验一个金额是否落在所有相关政策允许的区间内。

    供评测使用：即使金额由规则计算，也必须能通过政策区间校验 ——
    否则说明 amount 计算逻辑与政策文件不一致。
    """
    if amount <= 0:
        return False, f"金额 {amount} 非正数"
    bounds = [
        s.params for s in sections if s.action == "offer_coupon" and "max" in s.params
    ]
    if not bounds:
        return True, "无金额区间约束可校验"
    max_allowed = max(float(b["max"]) for b in bounds)
    if amount > max_allowed:
        return False, f"金额 ¥{amount:g} 超出政策允许的最大值 ¥{max_allowed:g}"
    return True, f"金额 ¥{amount:g} 在政策允许区间内（最大 ¥{max_allowed:g}）"
