"""Policy Engine 快速自查脚本（不依赖 pytest）。

用途：在 pytest 未就绪时快速验证核心决策逻辑是否符合预期。
运行：python scripts/smoke_check.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from supportos.domain.enums import (  # noqa: E402
    ActionType,
    CustomerTier,
    Decision,
    Intent,
    KnowledgeGap,
    OrderStatus,
    Sentiment,
    ShippingStatus,
)
from supportos.domain.models import (  # noqa: E402
    CaseContext,
    Customer,
    Order,
    ProposedAction,
    Shipment,
    Ticket,
)
from supportos.policy.engine import CaseMeta, PolicyEngine  # noqa: E402
from supportos.policy.permissions import (  # noqa: E402
    effective_agent_permission,
    human_permission,
)

NOW = datetime(2026, 9, 17, 14, 0, 0)

results: list[tuple[bool, str, str, str]] = []


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    results.append((ok, name, str(actual), str(expected)))


def customer(**kw) -> Customer:
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


def order(**kw) -> Order:
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


def shipment(**kw) -> Shipment:
    base = dict(
        order_id="A1001",
        status=ShippingStatus.DELAYED,
        estimated_arrival=date(2026, 9, 12),
        last_update=NOW - timedelta(hours=6),
        carrier="SF",
    )
    base.update(kw)
    return Shipment(**base)


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
        customer=customer(),
        order=order(),
        shipment=shipment(),
        policy_ids=["SHIPPING_DELAY_POLICY_V3"],
        now=NOW,
        coupons_issued_today=0.0,
    )
    base.update(kw)
    return CaseContext(**base)


def eng(role: str = "AGENT_L1") -> PolicyEngine:
    return PolicyEngine(meta=CaseMeta(requester_id="U100", requester_role=role))


def coupon(amount: float) -> ProposedAction:
    return ProposedAction(action=ActionType.ISSUE_COUPON, params={"amount": amount})


def refund(amount: float) -> ProposedAction:
    return ProposedAction(action=ActionType.REQUEST_REFUND, params={"amount": amount})


# ---------- 权限模型 ----------
check("Agent 优惠券上限=¥30", effective_agent_permission("AGENT_L1").coupon_max, 30.0)
check("人类客服优惠券上限=¥50", human_permission("AGENT_L1").coupon_max, 50.0)
check("Agent 退款上限=0", effective_agent_permission("SUPERVISOR").refund_max, 0.0)
check("Agent 不能自行升级", effective_agent_permission("SUPERVISOR").can_escalate, False)
check("未知角色优惠券=0", human_permission("HACKER").coupon_max, 0.0)

# ---------- 只读动作 ----------
check("查订单→AUTO", eng().evaluate(ProposedAction(action=ActionType.GET_ORDER), ctx()).decision, Decision.AUTO)

# ---------- 优惠券 ----------
check("发券¥20→审批", eng().evaluate(coupon(20), ctx()).decision, Decision.APPROVAL_REQUIRED)
check("发券¥30(边界)→审批", eng().evaluate(coupon(30), ctx()).decision, Decision.APPROVAL_REQUIRED)
check("发券¥60→拒绝", eng().evaluate(coupon(60), ctx()).decision, Decision.DENY)
check("发券¥200→拒绝", eng().evaluate(coupon(200), ctx()).decision, Decision.DENY)
check("发券-50→拒绝", eng().evaluate(coupon(-50), ctx()).decision, Decision.DENY)
check(
    "延迟24h发券→拒绝",
    eng().evaluate(coupon(20), ctx(shipment=shipment(estimated_arrival=date(2026, 9, 16)), now=datetime(2026, 9, 17))).decision,
    Decision.DENY,
)
check("7日领2次→升级", eng().evaluate(coupon(20), ctx(customer=customer(coupon_count_7d=2))).decision, Decision.ESCALATE)
check("单日超¥500→升级", eng().evaluate(coupon(20), ctx(coupons_issued_today=490.0)).decision, Decision.ESCALATE)
check("无政策→升级", eng().evaluate(coupon(20), ctx(policy_ids=[])).decision, Decision.ESCALATE)

# ---------- 退款 ----------
check("退款¥200(边界)→审批", eng().evaluate(refund(200), ctx()).decision, Decision.APPROVAL_REQUIRED)
check("退款¥299→升级", eng().evaluate(refund(299), ctx()).decision, Decision.ESCALATE)
# ★ 设计变更：「超出客服权限」= 授权不足，应升级给主管，而不是拒绝客户
check("退款¥300超权限→升级", eng().evaluate(refund(300), ctx()).decision, Decision.ESCALATE)
check("12月退3次→升级", eng().evaluate(refund(150), ctx(customer=customer(refund_count_12m=3))).decision, Decision.ESCALATE)
check("已退款订单→拒绝", eng().evaluate(refund(100), ctx(order=order(status=OrderStatus.REFUNDED))).decision, Decision.DENY)
check(
    "签收20天→拒绝",
    eng().evaluate(refund(100), ctx(order=order(delivered_at=NOW - timedelta(days=20)))).decision,
    Decision.DENY,
)

# ---------- 补发 ----------
check(
    "有证据补发→审批",
    eng().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx(order=order(has_damage_evidence=True))).decision,
    Decision.APPROVAL_REQUIRED,
)
check(
    "无证据补发→升级",
    eng().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx(order=order(has_damage_evidence=False))).decision,
    Decision.ESCALATE,
)
check(
    "¥800高价值补发→升级",
    eng().evaluate(ProposedAction(action=ActionType.REQUEST_REPLACEMENT), ctx(order=order(has_damage_evidence=True, amount=800.0))).decision,
    Decision.ESCALATE,
)

# ---------- 回复 ----------
check(
    "普通回复→AUTO",
    eng().evaluate(ProposedAction(action=ActionType.SEND_REPLY, params={"text": "包裹运输中"}), ctx()).decision,
    Decision.AUTO,
)
check(
    "含承诺回复→审批",
    eng().evaluate(
        ProposedAction(action=ActionType.SEND_REPLY, params={"text": "补偿¥20", "commitments": [{"type": "coupon", "amount": 20}]}),
        ctx(),
    ).decision,
    Decision.APPROVAL_REQUIRED,
)

# ---------- 禁止动作 ----------
for act in (ActionType.REFUND_MONEY, ActionType.BAN_ACCOUNT, ActionType.DELETE_TICKET, ActionType.MODIFY_AUDIT_LOG):
    check(f"禁止动作 {act.value}→拒绝", eng("SUPERVISOR").evaluate(ProposedAction(action=act), ctx()).decision, Decision.DENY)

# ---------- 升级信号 ----------
angry = ctx(
    ticket=Ticket(ticket_id="T001", customer_id="C001", message="延迟五天！再不补偿我就投诉到消协！", sentiment=Sentiment.ANGRY)
)
check("愤怒+投诉威胁→升级", eng().evaluate(coupon(30), angry).decision, Decision.ESCALATE)
check("物流异常→升级", eng().evaluate(coupon(20), ctx(shipment=shipment(status=ShippingStatus.EXCEPTION))).decision, Decision.ESCALATE)
check("缺物流信息发券→拒绝", eng().evaluate(coupon(20), ctx(shipment=None)).decision, Decision.DENY)
check("查订单不被情绪阻断→AUTO", eng().evaluate(ProposedAction(action=ActionType.GET_ORDER), angry).decision, Decision.AUTO)

# ---------- 方案级重复 ----------
plan = [coupon(20), coupon(20)]
dec = eng().evaluate_plan(plan, ctx())
check("重复发券第1条→审批", dec[0].decision, Decision.APPROVAL_REQUIRED)
check("重复发券第2条→升级", dec[1].decision, Decision.ESCALATE)

# ---------- 审批后重新校验 ----------
check("重新校验(未变)→通过", eng().revalidate(coupon(20), ctx(), ctx()).decision, Decision.APPROVAL_REQUIRED)
check(
    "重新校验(订单已退款)→中止",
    eng().revalidate(refund(150), ctx(), ctx(order=order(status=OrderStatus.REFUNDED))).decision,
    Decision.ESCALATE,
)
check(
    "重新校验(又领券)→中止",
    eng().revalidate(coupon(20), ctx(), ctx(customer=customer(coupon_count_7d=2))).decision,
    Decision.ESCALATE,
)

# ---------- 安全红线 ----------
nopolicy = ctx(policy_ids=[], knowledge_gap=KnowledgeGap.NO_POLICY_FOUND)
for a in (coupon(20), refund(100), ProposedAction(action=ActionType.REQUEST_REPLACEMENT)):
    d = eng().evaluate(a, nopolicy)
    check(f"红线:无政策时 {a.action.value} 非 AUTO", d.decision is not Decision.AUTO, True)

# ---------- 输出 ----------
passed = sum(1 for ok, *_ in results if ok)
total = len(results)
print("=" * 62)
print(f"Policy Engine 自查结果： {passed}/{total} 通过")
print("=" * 62)
for ok, name, actual, expect in results:
    if not ok:
        print(f"  [FAIL] {name}\n         实际={actual}  期望={expect}")
if passed == total:
    print("  全部通过 ✓")
print("=" * 62)
sys.exit(0 if passed == total else 1)
