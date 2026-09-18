"""Support Agent —— 用 Function Calling 收集解决问题所需的业务上下文。

职责边界（重要）：
  本 Agent **只读**，不做任何决策、不提出任何动作。
  它的输出是一份结构化的 Case Context，供 Policy Engine 裁决使用。

设计要点：
  1. **工具循环有硬上限** —— 防止 Agent 陷入无限调用（真实生产事故高发点）
  2. **工具失败是可观测信号** —— 不静默忽略，失败会计入置信度判断
  3. **不依赖 LLM 自报置信度** —— 用「工具失败数 + 政策命中数」推导
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from supportos.agents.llm import LLMClient, LLMError
from supportos.domain.enums import Intent
from supportos.tools.registry import ToolError, ToolRegistry

SYSTEM_PROMPT = """你是电商售后客服系统的信息收集模块。

你的唯一职责是**收集解决问题所需的事实**，不做判断、不提方案、不承诺任何补偿。

可用工具：
- get_customer: 查询客户信息（等级、历史、退款次数）
- get_order: 查询订单（金额、状态、签收时间、破损证据）
- get_shipping: 查询物流状态与预计到达时间
- search_knowledge: 检索客服政策

工作方式：
1. 先查询订单与物流，确认客观事实
2. 查询客户信息，了解客户等级与历史
3. 检索相关政策条款
4. 信息足够后，用一句话总结关键事实，然后停止

禁止事项：
- 不要提出任何补偿方案或金额
- 不要向客户承诺任何事
- 不要在信息不足时猜测
- 不要在总结中给出处理建议"""


@dataclass
class GatheredContext:
    """Support Agent 收集到的业务上下文。"""

    order_id: str | None = None
    customer_id: str | None = None
    summary: str = ""
    policy_ids: list[str] = field(default_factory=list)
    knowledge_gap: str = "NONE"
    knowledge_gap_detail: str = ""

    tool_calls: int = 0
    tool_failures: list[str] = field(default_factory=list)
    hit_iteration_cap: bool = False
    """是否因为达到工具循环上限而终止 —— 这本身是低置信信号。"""

    executed_reads: list[str] = field(default_factory=list)
    """实际执行过的只读工具名。用于在 Agent 未提出业务动作时，
    忠实地把「已经查过的信息」记录为工单动作，而不是硬塞一个补偿动作。"""

    delay_hours: float | None = None
    """物流延迟小时数。由 Support Agent 在查询物流时一并算出 ——
    避免下游再去解析自然语言摘要。"""

    original_message: str = ""
    """客户原始消息。供 Planner 做「咨询 vs 申请」判断。"""

    @property
    def primary_read(self) -> str | None:
        """优先选一个最有信息量的只读动作用于记录。"""
        for cand in ("get_shipping", "get_order", "get_customer", "search_knowledge"):
            if cand in self.executed_reads:
                return cand
        return self.executed_reads[0] if self.executed_reads else None

    @property
    def is_low_confidence(self) -> bool:
        """可观测的置信度判断（不使用 LLM 自报置信度）。

        触发条件：工具调用失败、循环被截断、或政策检索为空。
        """
        return (
            bool(self.tool_failures)
            or self.hit_iteration_cap
            or self.knowledge_gap != "NONE"
            or not self.policy_ids
        )

    def confidence_reasons(self) -> list[str]:
        out: list[str] = []
        if self.tool_failures:
            out.append(f"工具调用失败 {len(self.tool_failures)} 次：{'; '.join(self.tool_failures)}")
        if self.hit_iteration_cap:
            out.append("达到工具调用循环上限，信息可能不完整")
        if self.knowledge_gap != "NONE":
            out.append(f"知识缺口：{self.knowledge_gap}")
        if not self.policy_ids:
            out.append("未检索到适用政策")
        return out


class SupportAgent:
    """只读的信息收集 Agent。"""

    MAX_TOOL_ITERATIONS = 6
    """硬上限。超过即终止并把信息不完整记为低置信信号。"""

    def __init__(
        self,
        registry: ToolRegistry,
        llm: LLMClient | None = None,
        use_llm: bool = True,
        now=None,
    ):
        self.registry = registry
        self.use_llm = use_llm
        self._llm = llm
        from datetime import datetime as _dt

        self.now = now or _dt(2026, 9, 17, 10, 0, 0)

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    def _delay_hours(self, shipment) -> float:
        """按统一时间基准计算延迟小时数。

        ★ 时间基准必须来自一处（now），否则测试与生产会算出不同结果。
        """
        from datetime import datetime as _dt

        est = _dt.combine(shipment.estimated_arrival, _dt.min.time())
        return max(0.0, (self.now - est).total_seconds() / 3600.0)

    # ---------- 主入口 ----------

    def gather(        self,
        message: str,
        intent: Intent,
        order_id: str | None = None,
        customer_id: str | None = None,
    ) -> GatheredContext:
        if not self.use_llm:
            return self._deterministic_gather(intent, order_id, customer_id, message)
        try:
            return self._llm_gather(message, intent, order_id, customer_id)
        except LLMError:
            g = self._deterministic_gather(intent, order_id, customer_id, message)
            g.tool_failures.append("LLM 不可用，已降级为确定性收集")
            return g

    # ---------- LLM 路径 ----------

    def _llm_gather(
        self, message: str, intent: Intent, order_id: str | None, customer_id: str | None
    ) -> GatheredContext:
        hints = []
        if order_id:
            hints.append(f"订单号：{order_id}")
        if customer_id:
            hints.append(f"客户号：{customer_id}")
        hints.append(f"意图：{intent.value}")
        hints.append(f"客户消息：{message}")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(hints)},
        ]

        out = GatheredContext(
            order_id=order_id, customer_id=customer_id, original_message=message
        )
        tools = ToolRegistry.tool_schemas()
        # 收集阶段只暴露只读工具 —— 写工具在能力层面就不给这个 Agent
        read_only_names = {
            "get_customer",
            "get_order",
            "get_shipping",
            "search_knowledge",
        }
        tools = [t for t in tools if t["function"]["name"] in read_only_names]
        # search_knowledge 不通过 registry 的 schema 走（它不在 ActionType 里），
        # 这里单独补上

        for _ in range(self.MAX_TOOL_ITERATIONS):
            resp = self.llm.complete_with_tools(messages, tools)
            msg = resp.choices[0].message
            tool_calls = getattr(msg, "tool_calls", None)

            if not tool_calls:
                out.summary = (msg.content or "").strip()
                return out

            messages.append(
                {
                    "role": "assistant",
                    "content": msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in tool_calls
                    ],
                }
            )

            for tc in tool_calls:
                out.tool_calls += 1
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result, error = self._invoke(name, args, out)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": json.dumps(
                            {"ok": error is None, "result": result, "error": error},
                            ensure_ascii=False,
                            default=str,
                        )[:4000],
                    }
                )

        # 达到上限：信息可能不完整
        out.hit_iteration_cap = True
        out.summary = "（达到工具调用上限，信息收集未完成）"
        return out

    def _invoke(self, name: str, args: dict, out: GatheredContext):
        """执行工具调用，并回填关键上下文。"""
        try:
            if name == "get_customer":
                c = self.registry.get_customer(args["customer_id"])
                out.customer_id = c.customer_id
                out.executed_reads.append("get_customer")
                return c.model_dump(mode="json"), None

            if name == "get_order":
                # ★ 必须传归属人 —— 否则 Tool 层的归属校验形同虚设，
                #   客户只要在消息里报出别人的订单号就能被查到。
                #   B-17 对抗案例正是通过这条路径暴露了该缺陷。
                o = self.registry.get_order(
                    args["order_id"], requester_customer_id=out.customer_id
                )
                out.order_id = o.order_id
                out.executed_reads.append("get_order")
                return o.model_dump(mode="json"), None

            if name == "get_shipping":
                s = self.registry.get_shipping(
                    args["order_id"], requester_customer_id=out.customer_id
                )
                out.executed_reads.append("get_shipping")
                out.delay_hours = self._delay_hours(s)
                return s.model_dump(mode="json"), None

            if name == "search_knowledge":
                payload = self.registry.search_knowledge(
                    args.get("query", ""), args.get("intent")
                )
                out.executed_reads.append("search_knowledge")
                out.policy_ids = sorted(
                    set(out.policy_ids) | set(payload.get("policy_ids", []))
                )
                gap = payload.get("knowledge_gap", "NONE")
                # 知识缺口优先级：INSUFFICIENT / POLICY_EXPIRED 等比 NONE 更重要
                if gap != "NONE":
                    out.knowledge_gap = gap
                    out.knowledge_gap_detail = payload.get("gap_detail", "")
                return payload, None

            return None, f"未知工具：{name}"

        except ToolError as e:
            out.tool_failures.append(f"{name}: {e}")
            return None, str(e)
        except KeyError as e:
            out.tool_failures.append(f"{name}: 缺少参数 {e}")
            return None, f"缺少必需参数：{e}"
        except Exception as e:  # noqa: BLE001
            out.tool_failures.append(f"{name}: {type(e).__name__}: {e}")
            return None, f"{type(e).__name__}: {e}"

    # ---------- 确定性降级路径 ----------

    def _deterministic_gather(
        self,
        intent: Intent,
        order_id: str | None,
        customer_id: str | None,
        message: str = "",
    ) -> GatheredContext:
        """LLM 不可用时的确定性收集。行为可预测，便于测试。"""
        out = GatheredContext(
            order_id=order_id, customer_id=customer_id, original_message=message
        )
        facts: list[str] = []

        if customer_id:
            try:
                c = self.registry.get_customer(customer_id)
                out.executed_reads.append("get_customer")
                facts.append(
                    f"客户{c.customer_id}（{c.tier.value}，历史工单{c.previous_ticket_count}次，"
                    f"12月退款{c.refund_count_12m}次）"
                )
            except ToolError as e:
                out.tool_failures.append(f"get_customer: {e}")

        if order_id:
            try:
                o = self.registry.get_order(order_id, requester_customer_id=customer_id)
                out.executed_reads.append("get_order")
                facts.append(f"订单{o.order_id} 金额¥{o.amount:g} 状态{o.status.value}")
            except ToolError as e:
                out.tool_failures.append(f"get_order: {e}")
            try:
                s = self.registry.get_shipping(order_id, requester_customer_id=customer_id)
                out.executed_reads.append("get_shipping")
                out.delay_hours = self._delay_hours(s)
                facts.append(
                    f"物流{s.status.value} 预计{s.estimated_arrival.isoformat()}"
                )
            except ToolError as e:
                out.tool_failures.append(f"get_shipping: {e}")

        # ★ 必须传入客户原始消息 —— 知识缺口的识别依赖消息内容
        #   （例如「海外订单」「定制」「套装」等模式）。
        #   曾经这里传的是空字符串，导致「政策未覆盖该情况」在无 LLM 路径下
        #   完全无法被识别，知识缺口案例被静默结案。
        payload = self.registry.search_knowledge(message, intent.value)
        out.executed_reads.append("search_knowledge")
        out.policy_ids = payload.get("policy_ids", [])
        if payload.get("knowledge_gap", "NONE") != "NONE":
            out.knowledge_gap = payload["knowledge_gap"]
            out.knowledge_gap_detail = payload.get("gap_detail", "")
        facts.append(f"命中政策 {len(out.policy_ids)} 条")

        out.summary = "；".join(facts)
        return out
