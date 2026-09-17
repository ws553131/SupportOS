"""Tool 层单元测试。

核心命题：**Agent 在能力层面就无法直接动钱（没有 refund_money）。**
"""

from __future__ import annotations

import pytest

from supportos.domain.enums import ActionType
from supportos.tools.registry import ToolError, ToolRegistry


def reg() -> ToolRegistry:
    return ToolRegistry()


class TestReadTools:
    def test_get_customer(self):
        c = reg().get_customer("C0001")
        assert c.customer_id == "C0001"
        assert c.tier is not None

    def test_get_customer_missing_raises(self):
        with pytest.raises(ToolError, match="不存在"):
            reg().get_customer("C9999")

    def test_get_order(self):
        o = reg().get_order("A1001")
        assert o.order_id == "A1001"
        assert o.amount > 0

    def test_get_shipping(self):
        s = reg().get_shipping("A1001")
        assert s.order_id == "A1001"

    def test_ownership_check_blocks_cross_customer_access(self):
        """★ 归属校验：防止 Agent 被诱导查询他人订单。"""
        r = reg()
        order = r.get_order("A1001")
        with pytest.raises(ToolError, match="越权访问"):
            r.get_order("A1001", requester_customer_id="C9999")
        # 越权尝试必须被记录
        assert any(not c.ok and c.error == "越权访问" for c in r.call_log)

    def test_ownership_check_passes_for_owner(self):
        r = reg()
        order = r.get_order("A1001")
        got = r.get_order("A1001", requester_customer_id=order.customer_id)
        assert got.order_id == order.order_id


class TestWriteTools:
    def test_issue_coupon_records_side_effect(self):
        r = reg()
        rec = r.issue_coupon("C0001", 20.0, ticket_id="T1")
        assert rec["amount"] == 20.0
        assert len(r.coupons_issued) == 1

    def test_issue_coupon_rejects_non_positive(self):
        with pytest.raises(ToolError, match="非法"):
            reg().issue_coupon("C0001", 0)
        with pytest.raises(ToolError, match="非法"):
            reg().issue_coupon("C0001", -20)

    def test_request_refund_does_not_actually_refund(self):
        """★ 核心安全设计：request_refund 只创建待授权请求，不执行退款。"""
        r = reg()
        rec = r.request_refund("A1001", 150.0, ticket_id="T1")
        assert rec["status"] == "PENDING_AUTHORIZATION"
        assert len(r.refund_requests) == 1

    def test_request_refund_cannot_exceed_order_amount(self):
        r = reg()
        order = r.get_order("A1001")
        with pytest.raises(ToolError, match="超过订单金额"):
            r.request_refund("A1001", order.amount + 100)

    def test_request_replacement(self):
        r = reg()
        rec = r.request_replacement("A1001", ticket_id="T1")
        assert rec["status"] == "PENDING_AUTHORIZATION"

    def test_snapshot(self):
        r = reg()
        r.issue_coupon("C0001", 20.0)
        r.request_refund("A1001", 150.0)
        snap = r.snapshot()
        assert len(snap["coupons_issued"]) == 1
        assert len(snap["refund_requests"]) == 1


class TestToolSchemaIsMinimal:
    def test_schema_has_expected_tool_count(self):
        """4 个只读工具（含知识检索）+ 3 个写工具 = 7。

        ★ 数量本身就是设计约束：每多一个工具，
          Agent 的越权面就多一分，prompt 也更容易选错。
        """
        schemas = ToolRegistry.tool_schemas()
        assert len(schemas) == 7
        names = {t["function"]["name"] for t in schemas}
        assert names == {
            "get_customer",
            "get_order",
            "get_shipping",
            "search_knowledge",
            "issue_coupon",
            "request_refund",
            "request_replacement",
        }

    def test_schema_excludes_dangerous_capabilities(self):
        """★ 最外层能力收口：LLM 看不到的工具，它就无法调用。"""
        names = {t["function"]["name"] for t in ToolRegistry.tool_schemas()}
        for forbidden in (
            ActionType.REFUND_MONEY.value,
            ActionType.BAN_ACCOUNT.value,
            ActionType.DELETE_TICKET.value,
            ActionType.MODIFY_AUDIT_LOG.value,
        ):
            assert forbidden not in names, f"危险能力 {forbidden} 不应暴露给 LLM"

    def test_schema_has_refund_request_not_refund_money(self):
        names = {t["function"]["name"] for t in ToolRegistry.tool_schemas()}
        assert ActionType.REQUEST_REFUND.value in names
        assert ActionType.REFUND_MONEY.value not in names
