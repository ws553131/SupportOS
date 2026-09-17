"""Policy Engine —— SupportOS 的核心安全边界。

设计原则（PRD 第 7 节）：
  1. LLM 只负责「想做什么」，规则系统负责「能不能做」
  2. Proposal Agent ≠ Approval Authority（AI 不能审批自己的动作）
  3. 不确定时优先 ESCALATE，而不是硬着头皮执行
  4. 不依赖 LLM 自报置信度，只使用可观测信号

关键实现决策（值得写进 PRD）：
  ★ 不把规则写死在 if-else 里，而是「数据驱动」：
    动作规格表 ACTIONS 声明每个动作的阈值来源、风险等级与必要条件，
    引擎只做匹配与裁决。新增动作 = 加一条数据，不改引擎逻辑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from supportos import constants as C
from supportos.domain.enums import (
    ActionType,
    Decision,
    Intent,
    KnowledgeGap,
    OrderStatus,
    RiskLevel,
    ShippingStatus,
)
from supportos.domain.models import CaseContext, Evidence, PolicyDecision, ProposedAction
from supportos.policy.permissions import Permission, effective_agent_permission

# ============================================================
# 谓词（Predicate）—— 引擎的原子判断单元
# ============================================================

Predicate = Callable[[CaseContext, ProposedAction, Permission], tuple[bool, str]]
"""返回 (是否命中, 人类可读的原因)。"""


def _p_amount_positive(ctx: CaseContext, a: ProposedAction, p: Permission) -> tuple[bool, str]:
    amt = a.amount
    if amt is None:
        return True, "动作缺少 amount 参数"
    if amt <= 0:
        return True, f"金额非法：¥{amt:g}（必须大于 0）"
    return False, ""


def _p_over_agent_coupon_limit(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    amt = a.amount
    if amt is None:
        return False, ""
    if amt > p.coupon_max:
        return True, f"金额 ¥{amt:g} 超出 Agent 优惠券上限 ¥{p.coupon_max:g}"
    return False, ""


def _p_over_user_coupon_limit(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    from supportos.policy.permissions import human_permission

    amt = a.amount
    human = human_permission(p.role)
    if amt is not None and amt > human.coupon_max:
        return True, f"金额 ¥{amt:g} 超出客服 {p.role} 的权限上限 ¥{human.coupon_max:g}"
    return False, ""


def _p_coupon_frequency_exceeded(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    n = ctx.customer.coupon_count_7d
    if n >= C.AGENT_CUSTOMER_COUPON_COUNT_7D_MAX:
        return True, f"该客户 7 日内已领取 {n} 次优惠券，超过上限 {C.AGENT_CUSTOMER_COUPON_COUNT_7D_MAX} 次"
    return False, ""


def _p_daily_coupon_total_exceeded(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    amt = a.amount or 0.0
    projected = ctx.coupons_issued_today + amt
    if projected > C.AGENT_DAILY_COUPON_TOTAL_MAX:
        return True, f"今日累计优惠券将达到 ¥{projected:g}，超过单日上限 ¥{C.AGENT_DAILY_COUPON_TOTAL_MAX:g}"
    return False, ""


def _p_delay_below_sla(ctx: CaseContext, a: ProposedAction, p: Permission) -> tuple[bool, str]:
    """延迟补偿的必要条件：延迟必须超过 SLA 阈值。"""
    hours = ctx.delay_hours()
    if hours is None:
        return True, "无法获取物流延迟信息，不能判定是否符合补偿条件"
    if hours <= C.SLA_DELAY_HOURS:
        return True, f"延迟仅 {hours:.1f} 小时，未达到补偿门槛 {C.SLA_DELAY_HOURS} 小时"
    return False, ""


def _p_refund_over_approval_line(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    amt = a.amount
    if amt is not None and amt > C.REFUND_APPROVAL_MAX:
        return True, f"退款 ¥{amt:g} 超过审批线 ¥{C.REFUND_APPROVAL_MAX:g}"
    return False, ""


def _p_refund_over_user_limit(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    """退款超出当前客服权限。

    ★ 这是「授权不足」而非「违规」，因此归入 escalate_if 而非 deny_if。
      理由文案要可执行：主管需要知道「不是你批不了，而是发起人权限不够」。
    """
    from supportos.policy.permissions import human_permission

    amt = a.amount
    if amt is None or ctx.order is None:
        return False, ""
    human = human_permission(p.role)
    if amt > human.refund_max:
        return True, (
            f"退款 ¥{amt:g} 超出客服 {p.role} 的审批权限 ¥{human.refund_max:g}"
            f"（该客服可自行审批的上限），需升级至更高权限角色处理"
        )
    return False, ""


def _p_order_not_refundable(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    if ctx.order is None:
        return True, "缺少订单信息"
    if ctx.order.status in (OrderStatus.REFUNDED, OrderStatus.CLOSED, OrderStatus.PENDING_PAYMENT):
        return True, f"订单状态为 {ctx.order.status.value}，不满足退款前置条件"
    return False, ""


def _p_beyond_after_sale_window(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    days = ctx.days_since_delivery()
    if days is None:
        return True, "无法确认签收时间，不能判定是否在售后期限内"
    if days > C.AFTER_SALE_WINDOW_DAYS:
        return True, f"已签收 {days} 天，超出 {C.AFTER_SALE_WINDOW_DAYS} 天售后期限"
    return False, ""


def _p_refund_history_abnormal(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    n = ctx.customer.refund_count_12m
    if n >= C.REFUND_HISTORY_COUNT_12M_MAX:
        return True, f"该客户 12 个月内退款 {n} 次，达到异常阈值 {C.REFUND_HISTORY_COUNT_12M_MAX} 次"
    return False, ""


def _p_no_damage_evidence(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    if ctx.order is not None and not ctx.order.has_damage_evidence:
        return True, "缺少商品破损证据，无法认定责任"
    return False, ""


def _p_high_value_item(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    if ctx.order is not None and ctx.order.amount > C.AGENT_REPLACEMENT_VALUE_MAX:
        return True, f"商品价值 ¥{ctx.order.amount:g} 超过自动补发上限 ¥{C.AGENT_REPLACEMENT_VALUE_MAX:g}"
    return False, ""


def _p_has_commitment(
    ctx: CaseContext, a: ProposedAction, p: Permission
) -> tuple[bool, str]:
    """回复中是否含承诺性表述。

    ★ 实现说明：这里使用「结构化承诺」而非自然语言判断。
      Action Planner 必须把承诺抽取成结构化字段（amount / days / action），
      引擎再判断这些承诺是否在政策范围内。
      这是「LLM 抽取 + 规则裁决」的典型应用，避免让 LLM 自己判断自己。
    """
    commitments = a.params.get("commitments") or []
    if not commitments:
        return False, ""
    return True, f"回复包含 {len(commitments)} 项承诺性表述，需人工确认"


# ============================================================
# 动作规格表（数据驱动）
# ============================================================


@dataclass(frozen=True)
class ActionSpec:
    """一个动作的裁决规格。

    escalate_if 优先于 approve_if 判断 —— 安全优先（PRD 第 20 节的优先级冻结）。
    """

    risk: RiskLevel
    approve_if: tuple[Predicate, ...] = ()
    escalate_if: tuple[Predicate, ...] = ()
    deny_if: tuple[Predicate, ...] = ()
    require_policy: bool = False
    """是否需要知识库政策支撑（无政策 → KNOWLEDGE_GAP → 升级）。"""

    needs_approver_role: str | None = "SUPERVISOR"


ACTIONS: dict[ActionType, ActionSpec] = {
    # ---------- Read Tools：GREEN，自动执行 ----------
    ActionType.GET_CUSTOMER: ActionSpec(risk=RiskLevel.GREEN),
    ActionType.GET_ORDER: ActionSpec(risk=RiskLevel.GREEN),
    ActionType.GET_SHIPPING: ActionSpec(risk=RiskLevel.GREEN),
    ActionType.SEARCH_KNOWLEDGE: ActionSpec(risk=RiskLevel.GREEN),
    # ---------- 优惠券：YELLOW ----------
    ActionType.ISSUE_COUPON: ActionSpec(
        risk=RiskLevel.YELLOW,
        deny_if=(
            _p_amount_positive,
            _p_over_user_coupon_limit,
            _p_delay_below_sla,
        ),
        escalate_if=(
            _p_coupon_frequency_exceeded,
            _p_daily_coupon_total_exceeded,
        ),
        approve_if=(_p_over_agent_coupon_limit,),
        require_policy=True,
    ),
    # ---------- 退款：YELLOW → RED ----------
    # ★ 关键设计：「超出当前客服权限」属于 ESCALATE 而非 DENY。
    #   理由：这不是违规，而是授权不足。DENY 会让工单死掉、客户问题无人解决；
    #   ESCALATE 则把决策权交给有权限的主管 —— 符合 PRD 第 20 节的优先级。
    ActionType.REQUEST_REFUND: ActionSpec(
        risk=RiskLevel.YELLOW,
        deny_if=(
            _p_amount_positive,
            _p_order_not_refundable,
            _p_beyond_after_sale_window,
        ),
        escalate_if=(
            _p_refund_over_approval_line,
            _p_refund_over_user_limit,
            _p_refund_history_abnormal,
        ),
        require_policy=True,
    ),
    # ---------- 补发：YELLOW → RED ----------
    ActionType.REQUEST_REPLACEMENT: ActionSpec(
        risk=RiskLevel.YELLOW,
        deny_if=(_p_beyond_after_sale_window,),
        escalate_if=(_p_no_damage_evidence, _p_high_value_item),
        approve_if=(),
        require_policy=True,
    ),
    # ---------- 回复客户：GREEN → YELLOW ----------
    ActionType.SEND_REPLY: ActionSpec(
        risk=RiskLevel.GREEN,
        approve_if=(_p_has_commitment,),
    ),
    # ---------- 明确禁止 ----------
    ActionType.REFUND_MONEY: ActionSpec(risk=RiskLevel.RED),
    ActionType.BAN_ACCOUNT: ActionSpec(risk=RiskLevel.RED),
    ActionType.DELETE_TICKET: ActionSpec(risk=RiskLevel.RED),
    ActionType.MODIFY_AUDIT_LOG: ActionSpec(risk=RiskLevel.RED),
}

FORBIDDEN_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.REFUND_MONEY,
        ActionType.BAN_ACCOUNT,
        ActionType.DELETE_TICKET,
        ActionType.MODIFY_AUDIT_LOG,
    }
)

SIDE_EFFECT_FREE_ACTIONS: frozenset[ActionType] = frozenset(
    {
        ActionType.GET_CUSTOMER,
        ActionType.GET_ORDER,
        ActionType.GET_SHIPPING,
        ActionType.SEARCH_KNOWLEDGE,
    }
)
"""只读动作集合。即使意图不可判定也允许执行 ——
查订单、查物流、查政策本身不会对客户产生任何影响，
而且这些信息恰恰是人工判断所必需的。

★ send_reply 刻意**不在此列**：它是发往客户的对外动作。
  意图不可判定时若自动发出回复，工单就会被自动关闭、无人介入。
  这条防线在引擎层（本处）与编排层（_finalize_without_action）各设一道，
  因为 W2 的教训是：只在一层设防，换个动作路径就会被绕过。"""


# ============================================================
# 引擎
# ============================================================


@dataclass
class CaseMeta:
    """裁决所需的会话元信息。"""

    requester_id: str = ""
    """发起工单的客服 ID。Agent 权限继承此人。"""

    requester_role: str = "AGENT_L1"
    approver_role: str = "SUPERVISOR"
    now: datetime | None = None


@dataclass
class PolicyEngine:
    """确定性策略引擎。

    不涉及任何 LLM 调用 —— 这是刻意的设计：
    裁决必须可复现、可测试、可审计。
    """

    meta: CaseMeta = field(default_factory=CaseMeta)

    # ---------- 对外主入口 ----------

    def evaluate(self, action: ProposedAction, ctx: CaseContext) -> PolicyDecision:
        """裁决单个动作。"""
        perm = effective_agent_permission(self.meta.requester_role)

        # --- 0. 自审批检查（PRD 7.3）---
        if action.proposed_by and action.proposed_by == self.meta.requester_id:
            pass  # 提案人与执行人是同一人是允许的；禁止的是「审批」自己

        if str(action.action) == "approve_own_action":
            return self._deny("AI 不允许审批自己的动作", rule_id="FORBID_SELF_APPROVAL")

        # --- 1. 明确禁止的动作 ---
        if action.action in FORBIDDEN_ACTIONS:
            return self._deny(
                f"动作 {action.action.value} 在 V0.1 中明确禁止，任何角色不可执行",
                rule_id="FORBIDDEN_ACTION",
                risk=RiskLevel.RED,
            )

        spec = ACTIONS.get(action.action)
        if spec is None:
            return self._escalate(
                f"未知动作类型 {action.action}，无法裁决，保守处理",
                rule_id="UNKNOWN_ACTION",
            )

        # --- 2. 必备前置信息 ---
        if ctx.order is None and action.action in (
            ActionType.ISSUE_COUPON,
            ActionType.REQUEST_REFUND,
            ActionType.REQUEST_REPLACEMENT,
        ):
            return self._escalate("缺少订单信息，无法完成风险裁决", rule_id="MISSING_ORDER")

        # --- 2.1 意图不可判定时，禁止自动执行任何有副作用的动作 ---
        # ★ 这是端到端验证中发现并修复的真实漏洞：
        #   消息只表达情绪、未说明业务问题时，Triage 会返回 UNKNOWN。
        #   若此时仍自动发出「请补充具体问题」的回复，工单会被自动关闭，
        #   而这类消息恰恰是「情绪对抗 + 试图套取补偿」的高危场景。
        #   正确行为：意图不可判定 → 一律进入人工。
        #   注意只读工具（get_order 等）不受限制 —— 查信息本身不会造成伤害。
        if (
            ctx.ticket.intent is Intent.UNKNOWN
            and action.action not in SIDE_EFFECT_FREE_ACTIONS
        ):
            return self._escalate(
                "意图无法判定（UNKNOWN），系统不应在未知客户诉求的情况下自动动作，转人工澄清",
                rule_id="ESCALATE:INTENT_UNKNOWN",
                risk=RiskLevel.RED,
            )

        # --- 2.2 情境性硬升级信号 ---
        # ★ 与「情绪信号」区别对待：
        #   丢件 / 物流异常是**案件本身的客观事实**，与 Agent 打算做什么动作无关。
        #   无论动作是发券、退款还是回复，都必须先转人工核实。
        #   反例教训：这些信号曾经只在非 GREEN 动作上检查，
        #   导致「丢件 + 只回复」绕过了检查被自动执行。
        risk = self._check_case_risk_signals(ctx)
        if risk is not None:
            return self._escalate(risk, rule_id="ESCALATE:CASE_SIGNAL", risk=RiskLevel.RED)

        # --- 3. 知识缺口（PRD 第 14 节 / 7.5）---
        if spec.require_policy:
            gap = self._check_knowledge(ctx)
            if gap is not None:
                return self._escalate(
                    gap.reason, rule_id="KNOWLEDGE_GAP", risk=RiskLevel.RED
                )
        elif ctx.knowledge_gap in (KnowledgeGap.INSUFFICIENT, KnowledgeGap.RULE_CONFLICT):
            # ★ 即使动作本身不要求政策（如 send_reply），
            #   只要当前 case 存在知识缺口，就不允许自动发出正式回复 ——
            #   否则等于在「不知道政策怎么规定」的情况下自动答复客户。
            return self._escalate(
                f"当前 case 存在知识缺口（{ctx.knowledge_gap.value}），"
                "不得自动发出正式回复，转人工确认后再答复",
                rule_id="ESCALATE:KNOWLEDGE_GAP_ON_REPLY",
                risk=RiskLevel.RED,
            )

        # --- 4. DENY（硬性违规，安全优先于一切）---
        for pred in spec.deny_if:
            hit, reason = pred(ctx, action, perm)
            if hit:
                return self._deny(reason, rule_id=f"DENY:{pred.__name__}")

        # --- 5. ESCALATE（高风险，绝不自动执行）---
        # 收集全部命中的升级理由，而不是首个即返回 ——
        # 升级理由要「完整且可执行」，主管才能判断该找谁、能不能批。
        esc_reasons: list[str] = []
        esc_rule = ""
        for pred in spec.escalate_if:
            hit, reason = pred(ctx, action, perm)
            if hit:
                esc_reasons.append(reason)
                esc_rule = esc_rule or f"ESCALATE:{pred.__name__}"
        if esc_reasons:
            return self._escalate("；".join(esc_reasons), rule_id=esc_rule, risk=RiskLevel.RED)

        # --- 6. 情绪与对抗性升级（可观测信号，非 LLM 自报）---
        # 注意：只读动作（GREEN）不因情绪阻断 —— 客户生气时更需要 Agent
        # 自动查订单、查物流、查政策。情绪只应阻断「有副作用的写操作」。
        if spec.risk is not RiskLevel.GREEN:
            esc = self._check_escalation_signals(ctx)
            if esc is not None:
                return self._escalate(esc, rule_id="ESCALATE:SIGNAL", risk=RiskLevel.RED)

        # --- 7. APPROVAL（需人工审批）---
        approval_reasons: list[str] = []
        for pred in spec.approve_if:
            hit, reason = pred(ctx, action, perm)
            if hit:
                approval_reasons.append(reason)

        if spec.risk is RiskLevel.YELLOW and not approval_reasons:
            approval_reasons.append(f"动作 {action.action.value} 属于需审批的中风险操作")

        if approval_reasons:
            return self._approval(
                action,
                ctx,
                approval_reasons,
                rule_id="APPROVAL_REQUIRED",
                risk=RiskLevel.YELLOW,
            )

        # --- 8. AUTO ---
        return PolicyDecision(
            decision=Decision.AUTO,
            risk=spec.risk,
            reason=f"动作 {action.action.value} 属于低风险只读/无副作用操作，可自动执行",
            rule_id="AUTO",
            evidence=self._build_evidence(action, ctx, RiskLevel.GREEN),
        )

    def evaluate_plan(
        self, actions: list[ProposedAction], ctx: CaseContext
    ) -> list[PolicyDecision]:
        """裁决整份方案。

        额外检查：同一方案内重复动作 → 保守升级（防止重复发券）。
        """
        seen: set[str] = set()
        out: list[PolicyDecision] = []
        for a in actions:
            key = f"{a.action.value}:{a.amount}"
            dup = key in seen
            seen.add(key)
            d = self.evaluate(a, ctx)
            if dup and d.decision in (Decision.AUTO, Decision.APPROVAL_REQUIRED):
                d = self._escalate(
                    f"同一方案内出现重复动作 {a.action.value}（金额 {a.amount}），可能存在重复执行风险",
                    rule_id="DUPLICATE_ACTION",
                    risk=RiskLevel.RED,
                )
            out.append(d)
        return out

    # ---------- 审批后重新校验（★ 已确认的产品决策）----------

    def revalidate(
        self, action: ProposedAction, ctx_before: CaseContext, ctx_now: CaseContext
    ) -> PolicyDecision:
        """审批通过后、执行前，用最新上下文重新裁决。

        理由：主管可能在几小时后才批准，期间订单状态、物流、客户历史都可能变化。
        『批准时成立』不等于『执行时成立』。
        """
        original = self.evaluate(action, ctx_before)
        current = self.evaluate(action, ctx_now)

        if current.decision is original.decision:
            current.rule_id = f"REVALIDATE_OK({current.rule_id})"
            current.reason = f"重新校验通过（状态未发生实质变化）：{current.reason}"
            return current

        # 决策发生降级（AUTO/APPROVAL → ESCALATE/DENY）→ 不执行
        return self._escalate(
            "审批通过后重新校验失败：上下文已变化，"
            f"原裁决 {original.decision.value} → 现裁决 {current.decision.value}。"
            f"原因：{current.reason}",
            rule_id="REVALIDATE_FAILED",
            risk=RiskLevel.RED,
        )

    # ---------- 内部辅助 ----------

    def _check_knowledge(self, ctx: CaseContext) -> _Gap | None:
        """知识缺口检查。

        ★ 关键产品原则：政策「存在」不等于政策「够用」。
          海外订单、定制商品这类情况会命中通用退款政策，
          但通用政策并未覆盖它们 —— 此时必须升级，不得套用通用政策。
        """
        if ctx.knowledge_gap is KnowledgeGap.RULE_CONFLICT:
            return _Gap(
                "检索到互相冲突的政策条款，无法确定适用规则，转人工裁决",
                ctx.knowledge_gap_detail,
            )
        if ctx.knowledge_gap is KnowledgeGap.POLICY_EXPIRED:
            return _Gap(
                "命中的政策已过期或尚未生效，不能作为裁决依据",
                ctx.knowledge_gap_detail,
            )
        if ctx.knowledge_gap is KnowledgeGap.INSUFFICIENT:
            return _Gap(
                "现有政策未覆盖该具体情况（KNOWLEDGE_GAP），"
                "按产品原则不套用通用政策、不编造规则，转人工处理",
                ctx.knowledge_gap_detail,
            )
        if not ctx.policy_ids:
            return _Gap(
                "知识库中未找到可支撑该动作的有效政策（KNOWLEDGE_GAP），"
                "按产品原则不编造规则，转人工处理",
                ctx.knowledge_gap_detail,
            )
        return None

    def _check_case_risk_signals(self, ctx: CaseContext) -> str | None:
        """情境性硬升级信号 —— 与「Agent 打算做什么动作」无关。

        ★ 这类信号描述的是**案件本身的客观事实**，因此必须在任何动作裁决之前拦截，
          不受动作风险等级影响。曾经把它们和情绪信号混在一起、只在非 GREEN
          动作上检查，导致「丢件 + 只回一条消息」被自动执行。
        """
        # 运输异常 / 丢件：必须人工核实后才能决策
        if ctx.shipment is not None and ctx.shipment.status in (
            ShippingStatus.EXCEPTION,
            ShippingStatus.LOST,
        ):
            return (
                f"物流状态为 {ctx.shipment.status.value}，"
                "需人工核实实际情况后再决策，不得自动处理"
            )

        # 严重 SLA 违约 + 高价值客户 + 历史工单 → 主管介入
        hours = ctx.delay_hours()
        if (
            hours is not None
            and hours > C.SLA_SEVERE_DELAY_HOURS
            and ctx.has_high_value_customer()
            and ctx.customer.previous_ticket_count >= 2
        ):
            return (
                f"客户为 {ctx.customer.tier.value} 且延迟 {hours:.0f} 小时、"
                f"历史工单 {ctx.customer.previous_ticket_count} 次，升级主管处理"
            )

        return None

    def _check_escalation_signals(self, ctx: CaseContext) -> str | None:
        """情绪性升级信号。刻意不使用 LLM 自报置信度。

        只对**有副作用的动作**生效 —— 客户生气时更需要 Agent 自动查订单、
        查物流、查政策，阻断只读动作是帮倒忙。
        """
        from supportos.domain.enums import Sentiment

        text = ctx.ticket.message or ""
        angry = ctx.ticket.sentiment is Sentiment.ANGRY
        hit_kw = [kw for kw in C.ESCALATION_TRIGGER_KEYWORDS if kw in text]
        if angry and hit_kw:
            return (
                f"客户情绪严重升级且提及敏感渠道（{', '.join(hit_kw)}），"
                "按产品原则强制升级人工处理"
            )
        return None

    def _build_evidence(
        self, action: ProposedAction, ctx: CaseContext, risk: RiskLevel
    ) -> Evidence:
        ev = Evidence(recommendation=f"{action.action.value} {action.params}", risk_level=risk)
        ev.business_rules = [f"政策：{pid}" for pid in ctx.policy_ids] or ["无政策引用"]
        ev.customer_context = [
            f"客户等级：{ctx.customer.tier.value}",
            f"12 个月退款次数：{ctx.customer.refund_count_12m}",
            f"7 日内优惠券次数：{ctx.customer.coupon_count_7d}",
        ]
        if ctx.order is not None:
            ev.supporting_data.append(f"订单金额：¥{ctx.order.amount:g}")
            ev.supporting_data.append(f"订单状态：{ctx.order.status.value}")
        hours = ctx.delay_hours()
        if hours is not None:
            ev.supporting_data.append(f"物流延迟：{hours:.1f} 小时")
        return ev

    def _deny(
        self, reason: str, rule_id: str, risk: RiskLevel | None = None
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.DENY,
            risk=risk,
            reason=reason,
            rule_id=rule_id,
            blocked_reasons=[reason],
        )

    def _escalate(
        self, reason: str, rule_id: str, risk: RiskLevel | None = None
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.ESCALATE,
            risk=risk or RiskLevel.RED,
            reason=reason,
            rule_id=rule_id,
            requires_approval_by="HUMAN_AGENT",
        )

    def _approval(
        self,
        action: ProposedAction,
        ctx: CaseContext,
        reasons: list[str],
        rule_id: str,
        risk: RiskLevel,
    ) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.APPROVAL_REQUIRED,
            risk=risk,
            reason="；".join(reasons),
            rule_id=rule_id,
            requires_approval_by=self.meta.approver_role,
            evidence=self._build_evidence(action, ctx, risk),
            blocked_reasons=reasons,
        )


@dataclass
class _Gap:
    reason: str
    detail: str = ""
