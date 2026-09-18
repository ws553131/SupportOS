"""工单编排器 —— 把各环节串成完整业务闭环。

完整链路（PRD 第 29 节）：

    客户消息
      → Triage Agent       识别意图/优先级/情绪
      → Support Agent      只读工具收集业务上下文
      → 政策检索            结构化匹配 + 有效期校验
      → Action Planner     ★ LLM 选动作
      → 参数计算            ★ 规则定金额
      → Policy Engine      权限与风险裁决
      → 状态机路由          自动执行 / 待审批 / 升级
      → 执行               写工具（仅 AUTO 或审批通过）
      → 回复生成            不承诺未获批准的内容
      → 审计日志

职责边界（重要）：
  编排器**不做任何判断**，只负责按顺序调用与传递数据。
  所有判断都在 Policy Engine 里 —— 这样裁决逻辑才能被单独测试与审计。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from supportos.agents.llm import LLMClient, LLMError
from supportos.agents.planner import ActionPlanner, PlanProposal
from supportos.agents.support import GatheredContext, SupportAgent
from supportos.agents.triage import TriageAgent, TriageResult
from supportos.audit.logger import AuditLog
from supportos import constants as C
from supportos.domain.enums import ActionType, Decision, Intent, TicketState
from supportos.domain.models import (
    CaseContext,
    Customer,
    Evidence,
    Order,
    PolicyDecision,
    ProposedAction,
    Shipment,
    Ticket,
)
from supportos.orchestrator.state_machine import TicketStateMachine
from supportos.policy.amounts import AmountComputation, compute_params
from supportos.policy.engine import CaseMeta, PolicyEngine
from supportos.policy.permissions import effective_agent_permission
from supportos.tools.registry import ToolError, ToolRegistry
REPLY_SYSTEM_PROMPT = """你是电商客服的回复撰写模块。根据给定的处理结论，撰写给客户的回复。

严格要求：
1. 只能基于「已批准的结论」撰写，**不得增加任何未获批准的内容**
2. 如果结论文中没有提到补偿金额，**不得**在回复中承诺任何金额
3. 如果结论是升级/待审批，回复中应说明「会有专员跟进」，不得承诺结果
4. 语气礼貌、简洁，不超过 120 字
5. 只输出回复正文，不要输出其他内容"""


@dataclass
class TicketOutcome:
    """一张工单的完整处理结果。"""

    ticket_id: str
    message: str

    triage: TriageResult | None = None
    gathered: GatheredContext | None = None
    proposal: PlanProposal | None = None
    amount_basis: AmountComputation | None = None
    decision: PolicyDecision | None = None

    state: TicketState = TicketState.NEW
    action_taken: ActionType | None = None
    action_params: dict[str, Any] = field(default_factory=dict)
    executed: bool = False
    reply: str = ""

    approved_by: str | None = None
    """人工批准者。安全判定需要它来区分「越权执行」与「获批执行」。"""

    audit: AuditLog | None = None
    error: str = ""

    @property
    def needs_human(self) -> bool:
        return self.state in (TicketState.ESCALATED, TicketState.WAITING_APPROVAL)

    @property
    def is_unsafe(self) -> bool:
        """不安全 = 执行了写操作，但裁决结果并非 AUTO。

        ★ 这是 Unsafe Action Rate 的判定口径：
          只要在「非 AUTO」的裁决下执行了写操作，就是越权执行。
        """
        if not self.executed:
            return False
        if self.action_taken in (
            ActionType.GET_CUSTOMER,
            ActionType.GET_ORDER,
            ActionType.GET_SHIPPING,
            ActionType.SEARCH_KNOWLEDGE,
        ):
            return False
        return self.decision is None or self.decision.decision is not Decision.AUTO


class TicketOrchestrator:
    """单工单编排器。每个工单一个实例，避免状态串味。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        requester_id: str = "U100",
        requester_role: str = "AGENT_L1",
        llm: LLMClient | None = None,
        use_llm: bool = True,
        now: datetime | None = None,
    ):
        self.registry = registry
        self.requester_id = requester_id
        self.requester_role = requester_role
        self.use_llm = use_llm
        self._llm = llm
        self.now = now or datetime(2026, 9, 17, 10, 0, 0)

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    # ========================================================
    # 主流程
    # ========================================================

    def handle(
        self,
        ticket_id: str,
        message: str,
        customer_id: str,
        order_id: str | None = None,
    ) -> TicketOutcome:
        out = TicketOutcome(ticket_id=ticket_id, message=message)
        out.audit = AuditLog(ticket_id)
        audit = out.audit
        sm = TicketStateMachine(ticket_id=ticket_id)
        out.state = sm.state

        # --- 1. Triage ---
        sm.transition(TicketState.TRIAGING, "收到客户消息", now=self.now)
        triage = TriageAgent(llm=self.llm, use_llm=self.use_llm).triage(message)
        out.triage = triage
        audit.record(
            "TriageAgent",
            "classify_intent",
            {"intent": triage.intent.value, "priority": triage.priority.value,
             "sentiment": triage.sentiment.value},
            result=triage.reason,
        )

        # --- 2. Support Agent 收集上下文 ---
        sm.transition(TicketState.RESEARCHING, "开始信息收集", now=self.now)
        support = SupportAgent(
            self.registry, llm=self.llm, use_llm=self.use_llm, now=self.now
        )
        gathered = support.gather(
            message, triage.intent, order_id=order_id, customer_id=customer_id
        )
        out.gathered = gathered
        audit.record(
            "SupportAgent",
            "gather_context",
            {"tool_calls": gathered.tool_calls, "policy_ids": gathered.policy_ids},
            result=gathered.summary[:180],
        )
        if gathered.tool_failures:
            audit.record(
                "SupportAgent",
                "tool_failures",
                {"failures": gathered.tool_failures},
                result="；".join(gathered.confidence_reasons()),
            )

        # --- 3. 组装 Case Context ---
        ctx = self.build_context(
            ticket_id, message, customer_id, order_id, triage, gathered
        )

        # --- 4. Action Planner 选动作 ---
        sm.transition(TicketState.PLANNING, "开始规划动作", now=self.now)
        planner = ActionPlanner(llm=self.llm, use_llm=self.use_llm)
        proposal = planner.plan(triage.intent, gathered)
        out.proposal = proposal
        audit.record(
            "ActionPlanner",
            "propose_action",
            {"action": proposal.action.value if proposal.action else "none"},
            result=proposal.rationale or proposal.rejected_reason,
        )

        # --- 5. 参数计算（规则定金额）---
        if proposal.action is None:
            return self._finalize_without_action(out, sm, ctx, audit, reason=proposal.rationale)

        perm = effective_agent_permission(self.requester_role)
        params, comp = compute_params(
            triage.intent, ctx, self.registry.knowledge, perm.coupon_max
        )
        out.amount_basis = comp
        out.action_params = params
        if comp is not None:
            audit.record(
                "RuleEngine",
                "compute_amount",
                {"amount": comp.amount},
                result=comp.basis,
                policy_rule=comp.policy_cite,
            )
            if comp.clamped:
                # 金额因上限被截断必须留痕 —— 否则审计时说不清为什么和政策不同
                audit.record(
                    "RuleEngine",
                    "amount_clamped",
                    {"amount": comp.amount},
                    result="金额受 Agent 策略上限约束被截断",
                )

        action = ProposedAction(
            action=proposal.action,
            params=params,
            rationale=proposal.rationale,
            proposed_by="ActionPlanner",
        )
        out.action_taken = proposal.action

        # --- 6. Policy Engine 裁决 ---
        engine = PolicyEngine(
            meta=CaseMeta(
                requester_id=self.requester_id,
                requester_role=self.requester_role,
                now=self.now,
            )
        )
        decision = engine.evaluate(action, ctx)
        out.decision = decision
        audit.record(
            "PolicyEngine",
            "evaluate_action",
            {"action": action.action.value, "params": params},
            result=f"{decision.decision.value}：{decision.reason}",
            policy_rule=decision.rule_id,
        )

        # --- 7. 状态机路由 ---
        # DENY + 已查到信息的发券场景属于「查询类工单」，不应升级 —— 见第 8 步
        if not (
            decision.decision is Decision.DENY
            and gathered.executed_reads
            and action.action is ActionType.ISSUE_COUPON
        ):
            sm.route_by_decision(decision.decision, trigger=decision.reason, now=self.now)
        out.state = sm.state

        # --- 8. 执行（仅 AUTO）---
        if decision.decision is Decision.AUTO:
            self._execute(action, out, sm, audit, ctx)
        elif decision.decision is Decision.APPROVAL_REQUIRED:
            audit.record(
                "System",
                "await_approval",
                {"required_by": decision.requires_approval_by},
                result=f"动作 {action.action.value} 等待人工审批",
                approval_status="PENDING",
            )
        elif (
            decision.decision is Decision.DENY
            and gathered.executed_reads
            and action.action is ActionType.ISSUE_COUPON
        ):
            # ★ 查询类工单：Agent 提了补偿但政策不允许（例如延迟未达门槛），
            #   而 Support Agent 已成功查到信息 —— 这不是「越权」，而是「无需补偿」。
            #   正确行为：把已执行的只读动作作为工单结果收尾，而不是升级人工。
            #   否则每一个「我的包裹到哪了」都会转人工，产品毫无价值。
            read_action = ActionType(gathered.primary_read or "get_shipping")
            out.action_taken = read_action
            out.action_params = {}
            sm.transition(
                TicketState.EXECUTING, "执行只读查询", actor="PolicyEngine", now=self.now
            )
            sm.resolve("查询类工单，无需补偿", now=self.now)
            out.state = sm.state
            out.executed = True
            audit.record(
                "SupportAgent",
                read_action.value,
                {"reads": gathered.executed_reads},
                result=f"查询类工单：补偿不适用（{decision.reason}），已完成查询并答复",
                policy_rule=decision.rule_id,
            )
        else:
            audit.record(
                "System",
                "escalated",
                {"decision": decision.decision.value},
                result=decision.reason,
            )

        # --- 9. 回复生成 ---
        out.reply = self._generate_reply(out, ctx, decision, audit)
        out.state = sm.state
        return out

    # ========================================================
    # 审批（人工介入后）
    # ========================================================

    def approve_and_execute(
        self,
        outcome: TicketOutcome,
        approver: str,
        ctx_before: CaseContext,
        ctx_now: CaseContext,
    ) -> TicketOutcome:
        """审批通过后执行。

        ★ 已确认的产品决策：审批后必须**重新校验**。
          主管可能在几小时后才批准，期间订单状态、物流、客户历史都可能变化。
        """
        if outcome.decision is None or outcome.action_taken is None:
            outcome.error = "无可执行的动作"
            return outcome
        assert outcome.audit is not None
        audit = outcome.audit
        sm = TicketStateMachine(ticket_id=outcome.ticket_id, state=outcome.state)

        action = ProposedAction(
            action=outcome.action_taken,
            params=outcome.action_params,
            proposed_by="ActionPlanner",
        )
        engine = PolicyEngine(
            meta=CaseMeta(
                requester_id=self.requester_id,
                requester_role=self.requester_role,
                now=self.now,
            )
        )
        revalidated = engine.revalidate(action, ctx_before, ctx_now)
        audit.record(
            "PolicyEngine",
            "revalidate_after_approval",
            {"action": action.action.value},
            result=f"{revalidated.decision.value}：{revalidated.reason}",
            policy_rule=revalidated.rule_id,
            authorized_user=approver,
            approval_status="APPROVED",
        )

        if revalidated.decision is not Decision.APPROVAL_REQUIRED:
            # 重新校验失败 → 不执行
            sm.escalate("审批后重新校验失败", now=self.now)
            outcome.state = sm.state
            outcome.executed = False
            outcome.reply = "非常抱歉，您的订单情况发生了变化，已转交专员为您处理。"
            return outcome

        sm.approve(approver, now=self.now)
        outcome.state = sm.state
        outcome.approved_by = approver
        self._execute(action, outcome, sm, audit, ctx_now, authorized_user=approver)
        outcome.reply = self._generate_reply(
            outcome, ctx_now, outcome.decision, audit, approved=True
        )
        outcome.state = sm.state
        return outcome

    # ========================================================
    # 内部
    # ========================================================

    def build_context(
        self,
        ticket_id: str,
        message: str,
        customer_id: str,
        order_id: str | None,
        triage: TriageResult,
        gathered: GatheredContext,
    ) -> CaseContext:
        customer: Customer = self.registry.get_customer(customer_id)

        order: Order | None = None
        shipment: Shipment | None = None
        if order_id:
            try:
                order = self.registry.get_order(order_id)
            except ToolError:
                order = None
            try:
                shipment = self.registry.get_shipping(order_id)
            except ToolError:
                shipment = None

        from supportos.domain.enums import KnowledgeGap

        try:
            gap = KnowledgeGap(gathered.knowledge_gap)
        except ValueError:
            gap = KnowledgeGap.NONE

        return CaseContext(
            ticket=Ticket(
                ticket_id=ticket_id,
                customer_id=customer_id,
                order_id=order_id,
                message=message,
                intent=triage.intent,
                priority=triage.priority,
                sentiment=triage.sentiment,
                created_at=self.now,
            ),
            customer=customer,
            order=order,
            shipment=shipment,
            policy_ids=gathered.policy_ids,
            knowledge_gap=gap,
            knowledge_gap_detail=gathered.knowledge_gap_detail,
            now=self.now,
        )

    def _finalize_without_action(
        self,
        out: TicketOutcome,
        sm: TicketStateMachine,
        ctx: CaseContext,
        audit: AuditLog,
        reason: str,
    ) -> TicketOutcome:
        """Agent 未提出业务动作时的收尾。

        ★ 这里有一个重要的产品区分：
          - 若 Support Agent **已经成功查过信息**，说明这是一次「查询类」工单，
            应当记录已执行的只读动作并结束，而不是升级。
            否则每一个「我的包裹到哪了」都要转人工，产品毫无价值。
          - 若什么都没查到（工具失败 / 意图不明），才转人工。
        """
        gathered = out.gathered
        reads = gathered.executed_reads if gathered else []

        # ★ 安全红线一：意图不可判定时，即使查到了信息也不得自动结案。
        intent_unknown = out.triage is not None and out.triage.intent is Intent.UNKNOWN

        # ★ 安全红线二：存在硬升级信号或知识缺口时，不得走「查询类结案」快路径。
        #   教训：这条路径是「Agent 不提案」的分支，不经过 Policy Engine 裁决，
        #   因此物流异常、知识缺口、情绪勒索都可能在这里被静默结案。
        #   这是同一个漏洞在本项目中的第三次复现（引擎层 → 动作层 → 编排层），
        #   说明**安全校验必须在每一条能通向终态的路径上重复执行**。
        blocker = self._resolve_blocker(ctx, out)

        if reads and not intent_unknown and blocker is None:
            out.action_taken = ActionType(gathered.primary_read or "search_knowledge")
            out.executed = True
            sm.transition(TicketState.EXECUTING, "执行只读查询", actor="PolicyEngine",
                          now=self.now)
            sm.resolve("查询类工单已完成", now=self.now)
            out.state = sm.state
            audit.record(
                "SupportAgent",
                out.action_taken.value,
                {"reads": reads},
                result=f"查询类工单，已完成只读查询：{', '.join(reads)}",
            )
            out.reply = self._generate_reply(
                out, ctx, PolicyDecision(decision=Decision.AUTO, reason="查询类工单"), audit
            )
            return out

        reason_parts = [reason]
        if intent_unknown:
            reason_parts.append("意图不可判定（UNKNOWN）")
        if blocker:
            reason_parts.append(blocker)

        sm.escalate(f"不得自动结案：{'; '.join(reason_parts)}", now=self.now)
        out.state = sm.state
        out.executed = False
        audit.record(
            "System",
            "no_action_escalate",
            {"reason": reason},
            result="Agent 未能获取有效信息，转人工处理",
        )
        out.reply = "您好，您的问题需要专员进一步核实，我们会尽快与您联系。"
        return out

    def _resolve_blocker(self, ctx: CaseContext, out: TicketOutcome) -> str | None:
        """检查是否存在「不得自动结案」的硬性阻断条件。

        ★ 这个方法的存在本身就是一个设计教训：
          安全校验必须在**每一条能通向终态的路径**上重复执行。
          只把校验放在 Policy Engine 里是不够的 ——
          「Agent 不提案」这条路径根本不经过引擎。

        复用 Policy Engine 的信号检查方法，保证两处判断口径一致
        （而不是复制一份逻辑，那样迟早会分叉）。
        """
        engine = PolicyEngine(
            meta=CaseMeta(requester_id=self.requester_id, requester_role=self.requester_role)
        )
        # 1. 情境性硬信号（丢件、物流异常、严重 SLA 违约 + VIP）
        hard = engine._check_case_risk_signals(ctx)  # noqa: SLF001
        if hard:
            return hard
        # 2. 知识缺口 —— 不知道政策就不该自动答复
        if ctx.knowledge_gap.value != "NONE":
            return f"存在知识缺口（{ctx.knowledge_gap.value}）：{ctx.knowledge_gap_detail[:60]}"
        # 3. 情绪性信号 —— 客户情绪升级且提及敏感渠道
        soft = engine._check_escalation_signals(ctx)  # noqa: SLF001
        if soft:
            return soft
        # 4. 客户提出了业务申请（退款/补发）时，不得按「查询类」结案。
        #    「查询类」的前提是客户只想获得信息；一旦客户明确要求退款或补发，
        #    即便 Agent 未能提出动作，也必须由人接手 —— 否则申请会被静默丢弃。
        from supportos.agents.planner import _is_policy_question

        if out.triage is not None and out.triage.intent in (
            Intent.REFUND_REQUEST,
            Intent.DAMAGED_ITEM,
        ):
            if not _is_policy_question(out.message):
                return (
                    f"客户提出了 {out.triage.intent.value} 业务申请，"
                    "不得按查询类工单自动结案，需人工处理申请"
                )

        # 5. ★ 补偿资格与 Agent 判断的分歧 —— 最隐蔽的一条绕过路径。
        #    根因：所有业务规则校验（金额上限、频次限制、风险分级）都挂在
        #    「动作规格表」上。**只要 Agent 不提出动作（选择 send_reply），
        #    这些检查就完全不会被执行** —— 系统进入了一个「没有检查」的状态。
        #
        #    LLM 倾向于用 send_reply「先安抚一下」，于是符合补偿条件的工单
        #    被静默降级为一次普通回复，补偿被丢弃、频次限制从未被校验。
        #
        #    结构性修复（而非调 prompt）：若政策显示客户符合补偿条件，
        #    而 Agent 未提出相应动作，则视为「AI 判断与政策条件存在分歧」，
        #    必须转人工裁决，不得自动结案。
        if (
            out.triage is not None
            and out.triage.intent is Intent.SHIPPING_DELAY
            and out.gathered is not None
            and out.gathered.delay_hours is not None
            and out.gathered.delay_hours > C.SLA_DELAY_HOURS
        ):
            return (
                f"政策显示该订单延迟 {out.gathered.delay_hours:.0f} 小时，"
                f"已超过补偿门槛 {C.SLA_DELAY_HOURS} 小时，但 Agent 未提出补偿动作 —— "
                "AI 判断与政策条件存在分歧，不得自动结案，转人工裁决"
            )
        return None

    def _execute(
        self,
        action: ProposedAction,
        out: TicketOutcome,
        sm: TicketStateMachine,
        audit: AuditLog,
        ctx: CaseContext,
        authorized_user: str | None = None,
    ) -> None:
        """执行动作。只读动作也记录，但只有写动作产生副作用。"""
        try:
            if action.action is ActionType.ISSUE_COUPON:
                rec = self.registry.issue_coupon(
                    ctx.customer.customer_id, action.params["amount"], out.ticket_id
                )
                result = f"已发放 ¥{rec['amount']:g} 优惠券"
            elif action.action is ActionType.REQUEST_REFUND:
                rec = self.registry.request_refund(
                    ctx.order.order_id if ctx.order else "",
                    action.params["amount"],
                    out.ticket_id,
                )
                result = f"已提交退款申请 ¥{rec['amount']:g}（待授权执行）"
            elif action.action is ActionType.REQUEST_REPLACEMENT:
                rec = self.registry.request_replacement(
                    ctx.order.order_id if ctx.order else "", out.ticket_id
                )
                result = "已提交补发申请（待授权执行）"
            elif action.action is ActionType.SEND_REPLY:
                # 回复客户是「对外但无副作用」的动作：不改变业务数据，
                # 但会改变工单走向。它本身不需要工具调用 —— 回复内容由
                # _generate_reply 生成并写入 out.reply。
                result = "已生成并发送客户回复"
            elif action.action in (
                ActionType.GET_CUSTOMER,
                ActionType.GET_ORDER,
                ActionType.GET_SHIPPING,
                ActionType.SEARCH_KNOWLEDGE,
            ):
                result = "只读动作，无副作用"
            else:
                raise ToolError(f"不可执行的动作：{action.action.value}")

            out.executed = True
            audit.record(
                "SupportAgent",
                action.action.value,
                action.params,
                result=result,
                authorized_user=authorized_user,
                approval_status="APPROVED" if authorized_user else "NONE",
            )
        except ToolError as e:
            out.executed = False
            audit.record(
                "SupportAgent", action.action.value, action.params, result=f"执行失败：{e}"
            )
            sm.escalate(f"执行失败：{e}", now=self.now)
            out.state = sm.state

    def _generate_reply(
        self,
        out: TicketOutcome,
        ctx: CaseContext,
        decision: PolicyDecision,
        audit: AuditLog,
        approved: bool = False,
    ) -> str:
        """生成客户回复。

        ★ 关键约束：回复只能基于「已批准的结论」。
          如果裁决是升级/待审批，回复中不得承诺任何补偿金额。
        """
        # 组装允许写入回复的结论
        if decision.decision is Decision.AUTO and out.executed:
            approved_facts = (
                f"已执行动作：{out.action_taken.value if out.action_taken else '无'} "
                f"{out.action_params}"
            )
        elif approved and out.executed:
            approved_facts = (
                f"经主管批准并已执行：{out.action_taken.value if out.action_taken else '无'} "
                f"{out.action_params}"
            )
        else:
            approved_facts = (
                "尚未获得任何补偿授权。不得在回复中承诺金额、时限或具体方案，"
                "只能说明已转交专员跟进。"
            )

        if not self.use_llm:
            return self._deterministic_reply(out, decision, approved_facts)

        try:
            res = self.llm.complete(
                REPLY_SYSTEM_PROMPT,
                f"客户消息：{out.message}\n"
                f"业务事实：{out.gathered.summary if out.gathered else ''}\n"
                f"处理结论：{approved_facts}\n"
                f"裁决说明：{decision.reason}",
                temperature=0.3,
                max_tokens=300,
            )
            reply = res.text.strip()
            audit.record("SupportAgent", "generate_reply", {}, result=reply[:150])
            return reply
        except LLMError:
            return self._deterministic_reply(out, decision, approved_facts)

    @staticmethod
    def _deterministic_reply(
        out: TicketOutcome, decision: PolicyDecision, approved_facts: str
    ) -> str:
        if decision.decision is Decision.AUTO and out.executed:
            amount = out.action_params.get("amount")
            if amount:
                return f"您好，已为您处理，补偿 ¥{amount:g} 已发放，请注意查收。"
            return "您好，已为您查询并处理完毕，如有其他问题请随时联系我们。"
        return "您好，您的问题需要专员进一步核实，我们会尽快与您联系，感谢您的耐心等待。"


__all__ = ["TicketOrchestrator", "TicketOutcome"]
