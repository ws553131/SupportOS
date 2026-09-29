"""策略实验台 —— 亲手试出 Policy Engine 的判断逻辑。

★ 这个脚本是给「学习」用的，不是给演示用的。

  它的设计目标：让你在**不读代码**的前提下，靠反复试验建立直觉 ——
    "如果金额是 200 会怎样？"
    "如果客户 7 天内已经领过 2 次券呢？"
    "如果物流是丢件呢？"

  关键设计：它**直接调用引擎里真实的谓词函数**（`ACTIONS[action].deny_if` 等），
  而不是复制一份判断逻辑。复制逻辑会与引擎分叉 ——
  那时实验台教给你的就是错的。

用法：
  python scripts/policy_playground.py            # 菜单模式
  python scripts/policy_playground.py --list     # 列出所有可试的场景
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from supportos.domain.enums import (  # noqa: E402
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
from supportos.domain.models import (  # noqa: E402
    CaseContext,
    Customer,
    Order,
    ProposedAction,
    Shipment,
    Ticket,
)
from supportos.policy.engine import ACTIONS, CaseMeta, PolicyEngine  # noqa: E402
from supportos.policy.permissions import effective_agent_permission  # noqa: E402

NOW = datetime(2026, 9, 17, 14, 0, 0)

DECISION_CN = {
    Decision.AUTO: ("✅ AUTO", "自动执行 —— AI 自己就能做"),
    Decision.APPROVAL_REQUIRED: ("👤 APPROVAL_REQUIRED", "需审批 —— AI 想好了，等人点头"),
    Decision.ESCALATE: ("🚨 ESCALATE", "升级 —— 交给人来决定"),
    Decision.DENY: ("⛔ DENY", "拒绝 —— 这个动作不合规"),
}

ACTION_CN = {
    ActionType.ISSUE_COUPON: "发优惠券",
    ActionType.REQUEST_REFUND: "退款申请",
    ActionType.REQUEST_REPLACEMENT: "补发商品",
    ActionType.SEND_REPLY: "回复客户",
    ActionType.GET_ORDER: "查订单（只读）",
    ActionType.GET_CUSTOMER: "查客户（只读）",
    ActionType.GET_SHIPPING: "查物流（只读）",
    ActionType.SEARCH_KNOWLEDGE: "查政策（只读）",
    ActionType.REFUND_MONEY: "直接退款（禁止）",
}


# ============================================================
# 场景预设：让新手不用自己编数据
# ============================================================

PRESETS: list[dict] = [
    {
        "key": "1",
        "name": "普通发券（合规范围内）",
        "desc": "延迟 120 小时、金额 ¥20、普通客户",
        "action": ActionType.ISSUE_COUPON,
        "amount": 20.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 120,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "2",
        "name": "★ 客户狮子大开口：要 ¥200 券",
        "desc": "同样的延迟，但金额被抬到 ¥200",
        "action": ActionType.ISSUE_COUPON,
        "amount": 200.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 120,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "3",
        "name": "★ 物流丢件（客户只是想查）",
        "desc": "动作只是只读的查物流，但物流状态是丢件",
        "action": ActionType.GET_SHIPPING,
        "amount": None,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 300,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.LOST,
        "order_status": OrderStatus.SHIPPED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "4",
        "name": "★ 退款 ¥299（超过审批线）",
        "desc": "审批线是 ¥200，¥299 会怎样？",
        "action": ActionType.REQUEST_REFUND,
        "amount": 299.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 0,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELIVERED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "5",
        "name": "★ 资深客服（权限 ¥500）处理 ¥300 退款",
        "desc": "对比场景 4：这次客服有权批，但金额仍超政策审批线 ¥200",
        "action": ActionType.REQUEST_REFUND,
        "amount": 300.0,
        "role": "AGENT_L2",
        "tier": CustomerTier.NORMAL,
        "delay_hours": 0,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELIVERED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "6",
        "name": "★ 客户 7 天内已领 2 次券，又来要",
        "desc": "频次超限 —— 防重复补偿",
        "action": ActionType.ISSUE_COUPON,
        "amount": 20.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 120,
        "coupon_7d": 2,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "7",
        "name": "★ 延迟只有 1 天，客户要补偿",
        "desc": "没到 72 小时门槛",
        "action": ActionType.ISSUE_COUPON,
        "amount": 20.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 24,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "8",
        "name": "★ 破损补发但没有证据",
        "desc": "没有照片，不能认定责任",
        "action": ActionType.REQUEST_REPLACEMENT,
        "amount": None,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 0,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELIVERED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
    },
    {
        "key": "9",
        "name": "★ 政策查不到（知识缺口）",
        "desc": "政策库没有可支撑该动作的条款",
        "action": ActionType.ISSUE_COUPON,
        "amount": 20.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 120,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.NEUTRAL,
        "no_policy": True,
    },
    {
        "key": "10",
        "name": "★ 客户情绪激动 + 提及投诉渠道",
        "desc": "虽然是合规的发券，但该转人工",
        "action": ActionType.ISSUE_COUPON,
        "amount": 20.0,
        "tier": CustomerTier.NORMAL,
        "delay_hours": 120,
        "coupon_7d": 0,
        "refund_12m": 0,
        "shipping": ShippingStatus.DELAYED,
        "order_status": OrderStatus.DELIVERED,
        "evidence": False,
        "sentiment": Sentiment.ANGRY,
        "message": "延迟五天了！再不补偿我就投诉到消协！",
    },
]


# ============================================================


def build_context(p: dict, message: str = "") -> CaseContext:
    delivered_at = NOW - timedelta(days=2)
    order = Order(
        order_id="A9001",
        customer_id="C9001",
        amount=p.get("order_amount") or max(p.get("amount") or 200.0, 200.0),
        status=p["order_status"],
        created_at=NOW - timedelta(days=10),
        delivered_at=delivered_at,
        has_damage_evidence=p["evidence"],
    )
    est = (NOW - timedelta(hours=p["delay_hours"])).date()
    shipment = Shipment(
        order_id="A9001",
        status=p["shipping"],
        estimated_arrival=est,
        last_update=NOW - timedelta(hours=6),
    )
    customer = Customer(
        customer_id="C9001",
        name="测试客户",
        tier=p["tier"],
        refund_count_12m=p["refund_12m"],
        coupon_count_7d=p["coupon_7d"],
        previous_ticket_count=0,
    )
    return CaseContext(
        ticket=Ticket(
            ticket_id="T-PLAY",
            customer_id="C9001",
            order_id="A9001",
            message=message or p.get("message", "测试消息"),
            intent=Intent.SHIPPING_DELAY,
            sentiment=p.get("sentiment", Sentiment.NEUTRAL),
        ),
        customer=customer,
        order=order,
        shipment=shipment,
        policy_ids=[] if p.get("no_policy") else ["SHIPPING_DELAY_POLICY_V3"],
        knowledge_gap=KnowledgeGap.NO_POLICY_FOUND if p.get("no_policy") else KnowledgeGap.NONE,
        now=NOW,
    )


def show_checks(action: ProposedAction, ctx: CaseContext, role: str = "AGENT_L1") -> None:
    """逐项展示引擎会检查的谓词 —— 用的是引擎里真实的谓词函数。"""
    perm = effective_agent_permission(role)
    spec = ACTIONS.get(action.action)
    if spec is None:
        print("  （该动作没有规格表）")
        return

    print()
    print("  引擎会依次检查这些条件（用的是引擎里真实的判断函数）：")
    print("  " + "-" * 84)

    groups = [
        ("deny_if    （命中就拒绝）", spec.deny_if),
        ("escalate_if（命中就升级）", spec.escalate_if),
        ("approve_if （命中就需审批）", spec.approve_if),
    ]
    for title, preds in groups:
        if not preds:
            continue
        print(f"  {title}")
        for pred in preds:
            try:
                hit, reason = pred(ctx, action, perm)
            except Exception as e:  # noqa: BLE001
                hit, reason = False, f"(无法判断: {e})"
            mark = "✗ 命中" if hit else "✓ 通过"
            name = pred.__name__.replace("_p_", "")
            print(f"      {mark}  {name:<34} {reason if hit else ''}")
        print()


def run_preset(p: dict) -> None:
    print()
    print("=" * 88)
    print(f"场景：{p['name']}")
    print(f"      {p['desc']}")
    print("=" * 88)

    ctx = build_context(p)
    role = p.get("role", "AGENT_L1")
    params = {}
    if p.get("amount") is not None:
        params["amount"] = p["amount"]
    action = ProposedAction(action=p["action"], params=params, proposed_by="ActionPlanner")

    # --- 输入概览 ---
    print()
    print("  输入：")
    print(f"      发起客服    {role}"
          + ("（资深，可自行审批 ¥500 以内的退款）" if role == "AGENT_L2" else "（一线，退款权限 ¥200）"))
    print(f"      动作        {p['action'].value}（{ACTION_CN.get(p['action'], '')}）")
    if p.get("amount") is not None:
        print(f"      金额        ¥{p['amount']:g}")
    print(f"      客户等级    {p['tier'].value}")
    print(f"      延迟小时    {p['delay_hours']} 小时")
    print(f"      物流状态    {p['shipping'].value}")
    print(f"      订单状态    {p['order_status'].value}")
    print(f"      7日领券次数 {p['coupon_7d']}")
    print(f"      破损证据    {'有' if p['evidence'] else '无'}")
    print(f"      客户情绪    {p['sentiment'].value}")
    if p.get("no_policy"):
        print("      政策库      空（无政策支撑）")
    if p.get("message"):
        print(f"      客户原话    {p['message']}")

    # --- 逐项检查 ---
    show_checks(action, ctx, role)

    # --- 最终裁决 ---
    engine = PolicyEngine(meta=CaseMeta(requester_id="U100", requester_role=role))
    d = engine.evaluate(action, ctx)

    label, explain = DECISION_CN[d.decision]
    print("  最终裁决：")
    print("  " + "-" * 84)
    print(f"      {label}    —— {explain}")
    print(f"      风险等级    {d.risk.value if d.risk else '-'}")
    print(f"      命中规则    {d.rule_id}")
    print(f"      理由        {d.reason}")
    if d.requires_approval_by:
        print(f"      需谁处理    {d.requires_approval_by}")
    print()


def interactive() -> None:
    print()
    print("=" * 88)
    print("SupportOS 策略实验台")
    print("=" * 88)
    print()
    print("  目的：亲手试出「什么情况下引擎会说不行」。")
    print("  选一个场景，看引擎的完整判断过程。")
    print()
    for p in PRESETS:
        print(f"    [{p['key']:>2}] {p['name']}")
        print(f"         {p['desc']}")
    print()
    print("    [ q] 退出")
    print()

    while True:
        try:
            choice = input("  请输入编号：").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if choice in ("q", "quit", "exit", ""):
            return
        match = next((p for p in PRESETS if p["key"] == choice), None)
        if match is None:
            print("  ✗ 没有这个编号，请重试。")
            continue
        run_preset(match)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="列出所有场景")
    ap.add_argument("--preset", help="直接运行某个场景编号")
    args = ap.parse_args()

    if args.list:
        for p in PRESETS:
            print(f"[{p['key']:>2}] {p['name']}  —— {p['desc']}")
        return 0
    if args.preset:
        match = next((p for p in PRESETS if p["key"] == args.preset), None)
        if match is None:
            print(f"未找到场景 {args.preset}")
            return 1
        run_preset(match)
        return 0

    interactive()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
