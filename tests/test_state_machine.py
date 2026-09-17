"""状态机单元测试。

核心命题：**Agent 不能绕过 Policy Engine 自行执行。**
"""

from __future__ import annotations

import pytest

from supportos.domain.enums import Decision, TicketState as S
from supportos.orchestrator.state_machine import (
    ALLOWED,
    FORBIDDEN_RATIONALE,
    IllegalTransition,
    TicketStateMachine,
)


def sm(state: S = S.NEW) -> TicketStateMachine:
    return TicketStateMachine(ticket_id="T001", state=state)


class TestLegalTransitions:
    def test_happy_path_auto(self):
        """低风险：NEW→TRIAGING→RESEARCHING→PLANNING→EXECUTING→RESOLVED→CLOSED"""
        m = sm()
        m.transition(S.TRIAGING)
        m.transition(S.RESEARCHING)
        m.transition(S.PLANNING)
        m.transition(S.EXECUTING, actor="PolicyEngine")
        m.transition(S.RESOLVED)
        m.transition(S.CLOSED)
        assert m.state is S.CLOSED
        assert len(m.history) == 6

    def test_happy_path_with_approval(self):
        """中风险：PLANNING→WAITING_APPROVAL→EXECUTING→RESOLVED"""
        m = sm(S.PLANNING)
        m.transition(S.WAITING_APPROVAL)
        m.transition(S.EXECUTING, actor="supervisor")
        m.transition(S.RESOLVED)
        assert m.state is S.RESOLVED

    def test_escalation_path(self):
        """高风险：PLANNING→ESCALATED→RESOLVED→CLOSED"""
        m = sm(S.PLANNING)
        m.transition(S.ESCALATED)
        m.transition(S.RESOLVED)
        m.transition(S.CLOSED)
        assert m.state is S.CLOSED

    def test_closed_is_terminal(self):
        assert ALLOWED[S.CLOSED] == frozenset()


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "src,dst",
        [
            (S.PLANNING, S.RESOLVED),
            (S.PLANNING, S.CLOSED),
            (S.NEW, S.EXECUTING),
            (S.RESEARCHING, S.EXECUTING),
            (S.WAITING_APPROVAL, S.CLOSED),
            (S.ESCALATED, S.EXECUTING),
            (S.RESOLVED, S.EXECUTING),
            (S.CLOSED, S.EXECUTING),
        ],
    )
    def test_forbidden_transitions_raise(self, src, dst):
        m = sm(src)
        with pytest.raises(IllegalTransition):
            m.transition(dst)
        # 失败后状态不得改变
        assert m.state is src

    def test_no_execution_without_policy_or_approval(self):
        """★ 安全红线：PLANNING 不能直接跳到 EXECUTING 而绕过裁决。

        注意：状态机允许 PLANNING→EXECUTING（因为 AUTO 裁决后会这样走），
        但 assert_no_bypass 会校验「进入 EXECUTING 前是否有过裁决/审批记录」。
        """
        m = sm(S.PLANNING)
        m.transition(S.EXECUTING)  # 无裁决记录
        with pytest.raises(AssertionError, match="越权执行"):
            m.assert_no_bypass()

    def test_bypass_check_passes_when_policy_engine_acted(self):
        m = sm(S.PLANNING)
        m.transition(S.EXECUTING, actor="PolicyEngine")
        m.assert_no_bypass()  # 不应抛错

    def test_illegal_transition_carries_rationale(self):
        m = sm(S.ESCALATED)
        with pytest.raises(IllegalTransition) as ei:
            m.transition(S.EXECUTING)
        assert "Agent 不能从升级态自行恢复执行" == FORBIDDEN_RATIONALE[(S.ESCALATED, S.EXECUTING)]
        assert ei.value.src is S.ESCALATED


class TestDecisionRouting:
    def test_auto_routes_to_executing(self):
        m = sm(S.PLANNING)
        m.route_by_decision(Decision.AUTO)
        assert m.state is S.EXECUTING

    def test_approval_routes_to_waiting_approval(self):
        m = sm(S.PLANNING)
        m.route_by_decision(Decision.APPROVAL_REQUIRED)
        assert m.state is S.WAITING_APPROVAL

    def test_escalate_routes_to_escalated(self):
        m = sm(S.PLANNING)
        m.route_by_decision(Decision.ESCALATE)
        assert m.state is S.ESCALATED

    def test_deny_routes_to_escalated_not_closed(self):
        """★ DENY 也落到 ESCALATED —— 被拒绝的动作仍需人工给客户答复。

        直接关闭会让客户问题无人处理，与「超权限应升级」的设计一致。
        """
        m = sm(S.PLANNING)
        m.route_by_decision(Decision.DENY)
        assert m.state is S.ESCALATED


class TestHumanGates:
    def test_approve_moves_to_executing(self):
        m = sm(S.WAITING_APPROVAL)
        m.approve("supervisor_zhang")
        assert m.state is S.EXECUTING
        assert m.history[-1].actor == "supervisor_zhang"

    def test_reject_escalates_not_closes(self):
        """驳回不等于关闭 —— 客户问题仍需被处理。"""
        m = sm(S.WAITING_APPROVAL)
        m.reject("supervisor_zhang")
        assert m.state is S.ESCALATED

    def test_cannot_approve_from_new(self):
        m = sm(S.NEW)
        with pytest.raises(IllegalTransition):
            m.approve("supervisor")
