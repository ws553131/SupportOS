"""Triage Agent —— 识别客户问题的意图、优先级与情绪。

★ 核心设计原则：
  1. **LLM 只做分类，不做裁决**。它输出意图标签，不决定能不能执行。
  2. **输出必须可校验**。任何落在枚举之外的值都视为 LLM 失败，
     保守降级为 UNKNOWN → 触发升级，而不是当成某个默认意图。
  3. **不采集 LLM 自报置信度**。改由可观测信号推导（见 TriageSignal）。

为什么不做「多轮自我反思」：分类任务上，反思带来的收益远小于
延迟与成本上升，且会让结果不可复现。V0.1 保持单轮确定性分类。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from supportos.agents.llm import LLMClient, LLMError
from supportos.domain.enums import Intent, Priority, Sentiment

SYSTEM_PROMPT = """你是电商售后客服系统的意图识别模块。你的唯一职责是分析客户消息并输出结构化标签。

必须严格输出 JSON，字段如下：
{
  "intent": "SHIPPING_DELAY | DAMAGED_ITEM | REFUND_REQUEST | UNKNOWN",
  "priority": "LOW | MEDIUM | HIGH | URGENT",
  "sentiment": "POSITIVE | NEUTRAL | NEGATIVE | ANGRY",
  "reason": "一句话说明判断依据（20字以内）"
}

意图定义：
- SHIPPING_DELAY：物流延迟、包裹未到、物流异常、丢件
- DAMAGED_ITEM：商品破损、损坏、质量问题、要求补发
- REFUND_REQUEST：要求退款、退货、取消订单
- UNKNOWN：无法归入以上三类，或需要人工判断

优先级判定：
- URGENT：提及法律、消协、起诉、媒体曝光
- HIGH：情绪激烈、VIP客户、延迟超过 3 天、金额较大
- MEDIUM：常规售后问题
- LOW：咨询类问题

情绪判定：
- ANGRY：明显愤怒、威胁、强烈不满
- NEGATIVE：不满但克制
- NEUTRAL：中性陈述
- POSITIVE：表达感谢或满意

注意：只输出 JSON，不要输出任何其他内容。不要输出你的置信度。"""


@dataclass
class TriageResult:
    intent: Intent
    priority: Priority
    sentiment: Sentiment
    reason: str = ""
    raw: dict = field(default_factory=dict)

    # --- 可观测信号（替代 LLM 自报置信度）---
    parse_ok: bool = True
    """LLM 输出是否通过枚举校验。False 表示 LLM 返回了非法值。"""

    @property
    def is_confident(self) -> bool:
        """V0.1 的置信度只由「解析是否成功 + 意图是否可判定」决定。

        ★ 刻意不使用 LLM 自报的 confidence 字段：
          实测中模型自报置信度与实际正确率几乎无关，
          而且会给出一个无法审计的数字。可观测信号才可信。
        """
        return self.parse_ok and self.intent is not Intent.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "intent": self.intent.value,
            "priority": self.priority.value,
            "sentiment": self.sentiment.value,
            "reason": self.reason,
            "parse_ok": self.parse_ok,
        }


class TriageAgent:
    """意图识别 Agent。"""

    def __init__(self, llm: LLMClient | None = None, use_llm: bool = True):
        self.use_llm = use_llm
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    # ---------- 主入口 ----------

    def triage(self, message: str, customer_tier: str = "NORMAL") -> TriageResult:
        if not self.use_llm:
            return self._rule_based(message)
        try:
            return self._llm_triage(message, customer_tier)
        except LLMError:
            # LLM 不可用不应导致工单卡死 —— 降级为规则分类
            r = self._rule_based(message)
            r.reason = f"[LLM不可用，降级规则分类] {r.reason}"
            return r

    # ---------- LLM 路径 ----------

    def _llm_triage(self, message: str, customer_tier: str) -> TriageResult:
        user = f"客户等级：{customer_tier}\n客户消息：{message}"
        res = self.llm.complete(
            SYSTEM_PROMPT, user, json_mode=True, temperature=0.0, max_tokens=200
        )
        data = res.json()

        intent, ok_i = _safe_enum(data.get("intent"), Intent, Intent.UNKNOWN)
        priority, ok_p = _safe_enum(data.get("priority"), Priority, Priority.MEDIUM)
        sentiment, ok_s = _safe_enum(data.get("sentiment"), Sentiment, Sentiment.NEUTRAL)
        parse_ok = ok_i and ok_p and ok_s

        return TriageResult(
            intent=intent,
            priority=priority,
            sentiment=sentiment,
            reason=str(data.get("reason", ""))[:120],
            raw=data,
            parse_ok=parse_ok,
        )

    # ---------- 规则降级路径 ----------

    @staticmethod
    def _rule_based(message: str) -> TriageResult:
        """LLM 不可用时的保守降级。

        宁可判不出意图（UNKNOWN → 升级人工），也不要猜错。
        """
        m = message or ""
        if any(k in m for k in ("退款", "退钱", "退货", "取消订单")):
            intent = Intent.REFUND_REQUEST
        elif any(k in m for k in ("破损", "坏了", "损坏", "碎了", "补发")):
            intent = Intent.DAMAGED_ITEM
        elif any(k in m for k in ("物流", "快递", "没到", "延迟", "发货", "包裹")):
            intent = Intent.SHIPPING_DELAY
        else:
            intent = Intent.UNKNOWN

        angry = any(k in m for k in ("投诉", "曝光", "消协", "起诉", "太差", "垃圾"))
        sentiment = Sentiment.ANGRY if angry else Sentiment.NEUTRAL
        priority = Priority.HIGH if angry else Priority.MEDIUM

        return TriageResult(
            intent=intent,
            priority=priority,
            sentiment=sentiment,
            reason="规则匹配（无 LLM）",
            parse_ok=True,
        )


def _safe_enum(value, enum_cls, fallback):
    """把 LLM 返回的字符串安全转换为枚举。

    ★ 返回 (值, 是否合法)。非法值不会静默变成 fallback 后当作正常结果 ——
      调用方必须知道 LLM 返回了不可用的东西。
    """
    if value is None:
        return fallback, False
    try:
        return enum_cls(str(value).strip().upper()), True
    except ValueError:
        return fallback, False
