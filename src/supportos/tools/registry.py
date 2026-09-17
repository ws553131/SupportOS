"""SupportOS Tool 层 —— Agent 可以调用的全部业务能力。

★ 关键设计：V0.1 只开放 6 个 Tool（4 读 2 写）。

最核心的一点：**没有 refund_money()，只有 request_refund()**。
  这意味着 Agent 在能力层面就「无法」直接退款 ——
  它只能提出请求，真正的退款由获得授权的人执行。

这是「纵深防御」的第一层：即使 Policy Engine 被绕过，
Agent 手里也没有能直接动钱的工具。安全不能只靠一层检查。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from supportos.domain.enums import ActionType
from supportos.domain.models import Customer, Order, Shipment

DEFAULT_DATASET = Path(__file__).resolve().parents[3] / "data" / "mock" / "dataset.json"


class ToolError(RuntimeError):
    """Tool 执行失败。会作为可观测信号参与 Policy 裁决（而不是静默失败）。"""


@dataclass
class ToolCall:
    """一次 Tool 调用记录，用于审计。"""

    name: str
    params: dict[str, Any]
    ok: bool
    result: Any = None
    error: str = ""
    at: datetime = field(default_factory=datetime.now)


@dataclass
class ToolRegistry:
    """Mock 业务系统的工具集合。

    V0.1 使用 JSON 数据集模拟真实业务系统；
    接真实系统时只需替换本类的内部实现，Tool 接口保持不变。
    """

    dataset_path: Path = DEFAULT_DATASET
    _customers: dict[str, Customer] = field(default_factory=dict, init=False)
    _orders: dict[str, Order] = field(default_factory=dict, init=False)
    _shipments: dict[str, Shipment] = field(default_factory=dict, init=False)
    call_log: list[ToolCall] = field(default_factory=list, init=False)

    # 写操作产生的副作用记录（供审计与测试断言）
    coupons_issued: list[dict] = field(default_factory=list, init=False)
    refund_requests: list[dict] = field(default_factory=list, init=False)
    replacement_requests: list[dict] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        raw = json.loads(Path(self.dataset_path).read_text(encoding="utf-8"))
        for c in raw["customers"]:
            self._customers[c["customer_id"]] = Customer(**c)
        for o in raw["orders"]:
            self._orders[o["order_id"]] = Order(**o)
        for s in raw["shipments"]:
            self._shipments[s["order_id"]] = Shipment(**s)

    # ========================================================
    # Read Tools（GREEN，低风险）
    # ========================================================

    def get_customer(self, customer_id: str) -> Customer:
        c = self._customers.get(customer_id)
        if c is None:
            self._log("get_customer", {"customer_id": customer_id}, False, error="客户不存在")
            raise ToolError(f"客户 {customer_id} 不存在")
        self._log("get_customer", {"customer_id": customer_id}, True, c)
        return c

    def get_order(self, order_id: str, requester_customer_id: str | None = None) -> Order:
        """查询订单。

        ★ 归属校验：如果传入了 requester_customer_id，则订单必须属于该客户。
          否则 Agent 可能被诱导去查别人的订单 —— 这是真实存在的越权漏洞。
        """
        o = self._orders.get(order_id)
        if o is None:
            self._log("get_order", {"order_id": order_id}, False, error="订单不存在")
            raise ToolError(f"订单 {order_id} 不存在")

        if requester_customer_id and o.customer_id != requester_customer_id:
            self._log(
                "get_order",
                {"order_id": order_id, "requester": requester_customer_id},
                False,
                error="越权访问",
            )
            raise ToolError(
                f"越权访问：订单 {order_id} 不属于客户 {requester_customer_id}"
            )

        self._log("get_order", {"order_id": order_id}, True, o)
        return o

    def get_shipping(self, order_id: str) -> Shipment:
        s = self._shipments.get(order_id)
        if s is None:
            self._log("get_shipping", {"order_id": order_id}, False, error="无物流记录")
            raise ToolError(f"订单 {order_id} 没有物流记录")
        self._log("get_shipping", {"order_id": order_id}, True, s)
        return s

    # ========================================================
    # Write Tools（必须经 Policy Engine 裁决后才可调用）
    # ========================================================

    def issue_coupon(self, customer_id: str, amount: float, ticket_id: str = "") -> dict:
        """发放优惠券。

        ⚠️ 调用方（编排层）必须先获得 PolicyDecision == AUTO 或人工审批通过。
           本方法自身不做策略判断 —— 职责分离，避免两处逻辑不一致。
        """
        if amount <= 0:
            raise ToolError(f"优惠券金额非法：{amount}")
        rec = {
            "customer_id": customer_id,
            "amount": amount,
            "ticket_id": ticket_id,
            "at": datetime.now().isoformat(),
        }
        self.coupons_issued.append(rec)
        self._log("issue_coupon", rec, True, rec)
        return rec

    def request_refund(self, order_id: str, amount: float, ticket_id: str = "") -> dict:
        """提出退款请求。

        ★ 注意这不是 refund_money() —— 它只创建一条待授权的退款申请，
          真正退款需要人工授权后由外部系统执行。
          Agent 在能力层面就无法直接动钱。
        """
        if amount <= 0:
            raise ToolError(f"退款金额非法：{amount}")
        order = self._orders.get(order_id)
        if order is None:
            raise ToolError(f"订单 {order_id} 不存在")
        if amount > order.amount:
            raise ToolError(
                f"退款金额 ¥{amount:g} 超过订单金额 ¥{order.amount:g}"
            )
        rec = {
            "order_id": order_id,
            "amount": amount,
            "ticket_id": ticket_id,
            "status": "PENDING_AUTHORIZATION",
            "at": datetime.now().isoformat(),
        }
        self.refund_requests.append(rec)
        self._log("request_refund", rec, True, rec)
        return rec

    def request_replacement(self, order_id: str, ticket_id: str = "") -> dict:
        """提出补发请求（同样需授权）。"""
        order = self._orders.get(order_id)
        if order is None:
            raise ToolError(f"订单 {order_id} 不存在")
        rec = {
            "order_id": order_id,
            "ticket_id": ticket_id,
            "status": "PENDING_AUTHORIZATION",
            "at": datetime.now().isoformat(),
        }
        self.replacement_requests.append(rec)
        self._log("request_replacement", rec, True, rec)
        return rec

    # ========================================================
    # 辅助
    # ========================================================

    def _log(self, name: str, params: dict, ok: bool, result: Any = None, error: str = "") -> None:
        self.call_log.append(
            ToolCall(name=name, params=params, ok=ok, result=result, error=error)
        )

    def snapshot(self) -> dict:
        """写操作副作用快照，供评测断言使用。"""
        return {
            "coupons_issued": list(self.coupons_issued),
            "refund_requests": list(self.refund_requests),
            "replacement_requests": list(self.replacement_requests),
            "tool_calls": len(self.call_log),
        }

    # ---------- Tool Schema（供 LLM Function Calling 使用）----------

    @staticmethod
    def tool_schemas() -> list[dict]:
        """OpenAI 兼容的 Tool 定义。

        ★ 注意：schema 中**不存在** refund_money / ban_account 等能力。
          LLM 看不到的工具，它就无法调用 —— 这是最外层的能力收口。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": ActionType.GET_CUSTOMER.value,
                    "description": "查询客户基本信息（等级、历史、退款次数）",
                    "parameters": {
                        "type": "object",
                        "properties": {"customer_id": {"type": "string"}},
                        "required": ["customer_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": ActionType.GET_ORDER.value,
                    "description": "查询订单详情（金额、状态、是否已签收、破损证据）",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": ActionType.GET_SHIPPING.value,
                    "description": "查询订单物流状态与预计到达时间",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": ActionType.ISSUE_COUPON.value,
                    "description": "为客户发放优惠券（需经策略裁决与人工审批）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "amount": {"type": "number", "description": "优惠券金额"},
                        },
                        "required": ["customer_id", "amount"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": ActionType.REQUEST_REFUND.value,
                    "description": "提出退款请求（不会直接退款，需人工授权）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {"type": "string"},
                            "amount": {"type": "number"},
                        },
                        "required": ["order_id", "amount"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": ActionType.REQUEST_REPLACEMENT.value,
                    "description": "提出补发请求（需人工授权）",
                    "parameters": {
                        "type": "object",
                        "properties": {"order_id": {"type": "string"}},
                        "required": ["order_id"],
                    },
                },
            },
        ]
