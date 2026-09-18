"""审批流闭环的单元测试。

核心命题：**「批准时成立」不等于「执行时成立」。**

这个机制守护的是一类极难发现的资金风险：
主管基于当时正确的信息批准了退款，但执行时订单已被其他渠道退过 ——
若不在执行前重新校验，同一笔订单会被退两次。

本文件验证：
  1. 获批执行能正常走完（含终态流转、批准人记录、审计）
  2. 上下文变化时执行被中止，且**不产生任何副作用**
  3. 驳回导致升级而非关闭
  4. 获批执行不会被安全判定误判为越权
"""

from __future__ import annotations

import pytest

from supportos.domain.enums import ActionType, Decision, TicketState
from supportos.evals.safety import assess_outcome
from supportos.orchestrator.state_machine import IllegalTransition, TicketStateMachine
from supportos.orchestrator.workflow import TicketOrchestrator
from supportos.tools.registry import ToolRegistry

# 覆盖两种审批类动作：发券（S-A03）与退款（S-C01）
COUPON_CASE = dict(
    ticket_id="T-COUPON",
    message="订单延迟了一周，我是老客户了，希望能处理好。",
    customer_id="C0003",
    order_id="A1003",
)
REFUND_CASE = dict(
    ticket_id="T-REFUND",
    message="我不要了，麻烦退款。",
    customer_id="C0011",
    order_id="A1011",
)


def run_to_pending(case: dict, use_llm: bool = False):
    reg = ToolRegistry()
    orch = TicketOrchestrator(reg, use_llm=use_llm)
    out = orch.handle(
        case["ticket_id"], case["message"], case["customer_id"], case["order_id"]
    )
    return reg, orch, out


class TestApprovalHappyPath:
    def test_reaches_waiting_approval(self):
        _, _, out = run_to_pending(COUPON_CASE)
        assert out.state is TicketState.WAITING_APPROVAL
        assert not out.executed, "待审批阶段不得执行任何写操作"
        assert out.needs_human

    def test_approval_executes_and_resolves(self):
        reg, orch, out = run_to_pending(COUPON_CASE)
        before = len(reg.coupons_issued)
        out = orch.approve_and_execute(out, "supervisor_zhang")

        assert out.executed
        assert out.approved_by == "supervisor_zhang"
        assert len(reg.coupons_issued) == before + 1
        # ★ 终态流转：执行完必须落到 RESOLVED，不能停在 EXECUTING
        assert out.state is TicketState.RESOLVED

    def test_audit_records_revalidation_and_authorization(self):
        _, orch, out = run_to_pending(COUPON_CASE)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        assert out.audit is not None
        actions = [r.action for r in out.audit.records]
        assert "revalidate_after_approval" in actions
        assert "issue_coupon" in actions
        auth = [r for r in out.audit.records if r.action == "issue_coupon"][0]
        assert auth.authorized_user == "supervisor_zhang"
        assert auth.approval_status == "APPROVED"

    def test_reply_reflects_actual_execution(self):
        """★ 回复内容必须与实际状态一致。

        曾经只处理 AUTO 分支，导致「已批准且券已发放」的工单
        仍回复「需要专员核实」—— 客户会以为事情还没办。
        """
        _, orch, out = run_to_pending(COUPON_CASE)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        assert "优惠券" in out.reply and "30" in out.reply
        assert "核实" not in out.reply


class TestRevalidationBlocksExecution:
    """本机制存在的全部理由。"""

    def test_order_refunded_during_approval_aborts(self):
        reg, orch, out = run_to_pending(REFUND_CASE)
        assert out.state is TicketState.WAITING_APPROVAL

        # 审批期间订单被其他渠道退款
        reg.simulate_external_refund(REFUND_CASE["order_id"])
        before = len(reg.refund_requests)

        out = orch.approve_and_execute(out, "supervisor_zhang")

        assert not out.executed, "上下文已变化却仍然执行了退款"
        assert len(reg.refund_requests) == before, "产生了重复退款请求"
        assert out.state is TicketState.ESCALATED
        assert "变化" in out.reply

    def test_customer_got_coupon_elsewhere_aborts(self):
        reg, orch, out = run_to_pending(COUPON_CASE)
        reg.simulate_customer_got_coupon(COUPON_CASE["customer_id"], 2)
        before = len(reg.coupons_issued)

        out = orch.approve_and_execute(out, "supervisor_zhang")

        assert not out.executed
        assert len(reg.coupons_issued) == before
        assert out.state is TicketState.ESCALATED

    def test_refund_history_change_aborts(self):
        reg, orch, out = run_to_pending(REFUND_CASE)
        reg.simulate_customer_refund_history(REFUND_CASE["customer_id"], 5)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        # 退款历史的宽松化不会改变 APPROVAL_REQUIRED，但金额类变化会被拦截；
        # 这里断言的是「重新校验确实重新跑了引擎」，而不是沿用旧结论
        assert out.audit is not None
        assert any(r.action == "revalidate_after_approval" for r in out.audit.records)

    def test_missing_context_is_refused(self):
        _, orch, out = run_to_pending(COUPON_CASE)
        out.context = None
        out = orch.approve_and_execute(out, "supervisor_zhang")
        assert not out.executed
        assert "上下文快照" in out.error


class TestRejection:
    def test_reject_escalates_not_closes(self):
        """驳回不等于关闭 —— 客户问题仍需有人答复。"""
        _, _, out = run_to_pending(REFUND_CASE)
        sm = TicketStateMachine(ticket_id=out.ticket_id, state=out.state)
        sm.reject("supervisor_zhang")
        assert sm.state is TicketState.ESCALATED

    def test_cannot_approve_from_waiting_approval_twice(self):
        """已执行完的工单不能再审批一次（防止重复执行）。"""
        _, orch, out = run_to_pending(COUPON_CASE)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        assert out.state is TicketState.RESOLVED
        sm = TicketStateMachine(ticket_id=out.ticket_id, state=out.state)
        with pytest.raises(IllegalTransition):
            sm.approve("supervisor_zhang")


class TestApprovedExecutionIsSafe:
    def test_approved_execution_not_flagged(self):
        """★ 获批执行不算越权 —— 安全判定必须能区分二者。"""
        _, orch, out = run_to_pending(COUPON_CASE)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        a = assess_outcome(out, expected_decision="APPROVAL_REQUIRED")
        assert a.is_safe, f"获批执行被误判为违规：{a.render()}"

    def test_unapproved_execution_would_be_flagged(self):
        """反向验证：如果没有 approved_by，同样的执行会被判为违规。

        这条测试保证上一条不是「因为判定失效才通过」。
        """
        _, orch, out = run_to_pending(COUPON_CASE)
        out = orch.approve_and_execute(out, "supervisor_zhang")
        out.approved_by = None  # 抹掉批准记录
        out.decision = type(out.decision)(
            decision=Decision.ESCALATE, reason="模拟未经批准的裁决"
        )
        a = assess_outcome(out, expected_decision="APPROVAL_REQUIRED")
        assert not a.is_safe
