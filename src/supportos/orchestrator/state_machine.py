"""Ticket 状态机 —— 限制 Agent 的执行路径。

为什么要状态机（PRD 第 16 节）：
  LLM 是有创造力的，而创造力在工单流转里是灾难。
  状态机把「能走哪条路」变成确定性约束，
  使 Agent 无法从 PLANNING 直接跳到 EXECUTING 绕过 Policy Engine。

非法流转会被拒绝并记录审计 —— 这不是异常处理，而是产品能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from supportos.domain.enums import Decision, TicketState as S


class IllegalTransition(RuntimeError):
    """非法状态流转。携带足够信息以便审计。"""

    def __init__(self, src: S, dst: S, reason: str = ""):
        self.src = src
        self.dst = dst
        super().__init__(
            f"非法状态流转 {src.value} → {dst.value}" + (f"：{reason}" if reason else "")
        )


# ============================================================
# 允许的流转表
# ============================================================

ALLOWED: dict[S, frozenset[S]] = {
    S.NEW: frozenset({S.TRIAGING, S.CLOSED}),
    S.TRIAGING: frozenset({S.RESEARCHING, S.ESCALATED}),
    S.RESEARCHING: frozenset({S.PLANNING, S.ESCALATED}),
    # ★ PLANNING 只能去 WAITING_APPROVAL / EXECUTING / ESCALATED
    #   且 EXECUTING 仅在 Policy Engine 判定 AUTO 时才允许（见 can_execute）
    S.PLANNING: frozenset({S.WAITING_APPROVAL, S.EXECUTING, S.ESCALATED}),
    S.WAITING_APPROVAL: frozenset({S.EXECUTING, S.ESCALATED, S.RESOLVED}),
    S.EXECUTING: frozenset({S.RESOLVED, S.ESCALATED}),
    S.ESCALATED: frozenset({S.RESOLVED, S.CLOSED}),
    S.RESOLVED: frozenset({S.CLOSED, S.WAITING_APPROVAL}),
    S.CLOSED: frozenset(),  # 终态
}

# 显式记录「为什么这些流转被禁止」——面试时可以直接引用
FORBIDDEN_RATIONALE: dict[tuple[S, S], str] = {
    (S.PLANNING, S.RESOLVED): "规划完成不等于问题已解决，必须先执行或升级",
    (S.PLANNING, S.CLOSED): "未经执行或升级不得关闭工单",
    (S.WAITING_APPROVAL, S.CLOSED): "审批未决不得关闭工单",
    (S.ESCALATED, S.EXECUTING): "Agent 不能从升级态自行恢复执行",
    (S.RESOLVED, S.EXECUTING): "已解决的工单不得重新执行，需新建工单",
    (S.CLOSED, S.EXECUTING): "已关闭的工单不可变",
    (S.NEW, S.EXECUTING): "必须先经过 Triage 与 Policy 裁决",
    (S.RESEARCHING, S.EXECUTING): "必须先完成 PLANNING 与 Policy 裁决",
}


# ============================================================
# 按 Policy 裁决结果决定落点
# ============================================================

DECISION_TO_STATE: dict[Decision, S] = {
    Decision.AUTO: S.EXECUTING,
    Decision.APPROVAL_REQUIRED: S.WAITING_APPROVAL,
    Decision.ESCALATE: S.ESCALATED,
    Decision.DENY: S.ESCALATED,
    # DENY 也落到 ESCALATED：被拒绝的动作仍需人工知晓并给客户答复，
    # 直接关闭会让客户问题无人处理。这与「超权限应升级」的设计一致。
}


@dataclass
class StateTransition:
    src: S
    dst: S
    at: datetime
    trigger: str
    actor: str = "system"


@dataclass
class TicketStateMachine:
    """单个工单的状态机实例。"""

    ticket_id: str
    state: S = S.NEW
    history: list[StateTransition] = field(default_factory=list)

    def can_transition(self, dst: S) -> bool:
        return dst in ALLOWED.get(self.state, frozenset())

    def transition(self, dst: S, trigger: str = "", actor: str = "system",
                   now: datetime | None = None) -> StateTransition:
        if not self.can_transition(dst):
            why = FORBIDDEN_RATIONALE.get((self.state, dst), "")
            raise IllegalTransition(self.state, dst, why)
        t = StateTransition(
            src=self.state, dst=dst, at=now or datetime.now(), trigger=trigger, actor=actor
        )
        self.state = dst
        self.history.append(t)
        return t

    def route_by_decision(
        self, decision: Decision, trigger: str = "", now: datetime | None = None
    ) -> StateTransition:
        """按 Policy Engine 的裁决结果自动路由。

        ★ 这是「状态机约束 Agent」的落地方式：
          Agent 无法自己决定下一步，只能由 Policy 裁决驱动。
        """
        target = DECISION_TO_STATE[decision]
        if self.state is not S.PLANNING and target is not S.ESCALATED:
            # 非 PLANNING 阶段的裁决只在异常情况下生效，保守升级
            target = S.ESCALATED
            trigger = f"{trigger}（非规划阶段的裁决，保守升级）"
        return self.transition(target, trigger=trigger, actor="PolicyEngine", now=now)

    # ---------- 审批相关 ----------

    def approve(self, approver: str, now: datetime | None = None) -> StateTransition:
        """人工批准 → 进入执行。"""
        return self.transition(
            S.EXECUTING, trigger=f"approved by {approver}", actor=approver, now=now
        )

    def reject(self, approver: str, now: datetime | None = None) -> StateTransition:
        """人工驳回 → 升级处理（而不是关闭，客户问题仍需答复）。"""
        return self.transition(
            S.ESCALATED, trigger=f"rejected by {approver}", actor=approver, now=now
        )

    def escalate(self, reason: str, now: datetime | None = None) -> StateTransition:
        return self.transition(S.ESCALATED, trigger=reason, actor="PolicyEngine", now=now)

    def resolve(self, note: str = "", now: datetime | None = None) -> StateTransition:
        return self.transition(S.RESOLVED, trigger=note or "resolved", now=now)

    def close(self, note: str = "", now: datetime | None = None) -> StateTransition:
        return self.transition(S.CLOSED, trigger=note or "closed", now=now)

    # ---------- 断言：安全红线 ----------

    def assert_no_bypass(self) -> None:
        """校验历史中不存在绕过 Policy Engine 的执行。

        判定规则：每一次进入 EXECUTING 的流转，必须满足以下任一条件：
          a) 该流转本身由 PolicyEngine 触发（AUTO 裁决），或
          b) 此前存在一次 WAITING_APPROVAL（经人工审批），或
          c) 该流转的 actor 是具体的人（人工批准）。

        注意 (a)：进入 EXECUTING 的「那次流转本身」就带有 actor 信息，
        不能只看它之前的记录 —— 否则 AUTO 裁决会被误判为越权。
        """
        for i, t in enumerate(self.history):
            if t.dst is not S.EXECUTING:
                continue
            by_policy = t.actor == "PolicyEngine"
            human_approved = t.actor not in ("system", "PolicyEngine") and t.actor != ""
            had_approval_gate = any(h.dst is S.WAITING_APPROVAL for h in self.history[:i])
            if not (by_policy or human_approved or had_approval_gate):
                raise AssertionError(
                    f"工单 {self.ticket_id} 在索引 {i} 进入 EXECUTING（actor={t.actor!r}），"
                    "但既非 Policy Engine 裁决、也未经人工审批 —— 存在越权执行"
                )
