"""Action Planner —— 选择动作类型。参数由规则计算。

★ 核心设计决策（已确认）：**LLM 选动作，规则定参数。**

分工：

    LLM 的职责：从**允许的动作集合**里选一个，并说明理由
    规则的职责：计算所有数值参数（金额），依据政策区间

为什么这样切分：
  - 金额必须可复现、可审计。主管问「为什么是 ¥20」，
    答案应是「政策 §3.1 规定延迟 >72h 补偿 ¥10~¥30，取下沿」，
    而不是「模型觉得差不多」。
  - 参数由规则算，才能写出「任何情况下金额都不超政策上限」的断言。
  - LLM 仍然保留了它擅长的能力：在模糊语境下判断「该回消息还是该给补偿」。

同时 Action Planner **不做风险裁决** —— 那是 Policy Engine 的职责。
Planner 只负责「想做什么」，不负责「能不能做」。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from supportos.agents.llm import LLMClient, LLMError
from supportos.agents.support import GatheredContext
from supportos.domain.enums import ActionType, Intent

SYSTEM_PROMPT = """你是电商售后客服系统的动作规划模块。
你的职责：根据已收集的事实，从**给定的可选动作**中选择最合适的一个。

严格要求：
1. 只能从「可选动作」列表中选择，不得发明新动作
2. 不得给出任何金额或数量 —— 金额由政策规则计算

选择准则（按优先级）：
1. 如果事实显示客户**符合补偿/退款/补发的政策条件**，选择对应的业务动作
   （例如：物流延迟确实超过 72 小时 → 选择 issue_coupon）。
   **不要因为「想先安抚一下」就降级成 send_reply** ——
   补偿资格是客观事实，不因沟通策略而改变。
2. 只有当事实显示**不符合业务动作条件**时，才选择 send_reply。
3. 如果事实显示**不应采取任何业务动作**（需要人工核实），选择 none。

重要提醒：
- 若事实中同时出现「补偿条件成立」与「物流状态异常」，选 none ——
  异常情况必须人工核实，不由 AI 决定补偿。
- 不要因为订单状态与物流状态看起来矛盾就放弃提案；
  矛盾正是 Policy Engine 要裁决的内容，你只需基于已给事实选择动作。

只输出 JSON：
{
  "action": "动作名 或 none",
  "rationale": "选择该动作的理由（30字以内，基于已给事实）"
}"""

# 每个意图允许的动作集合 —— 这是产品规则的体现，不由 LLM 决定
ALLOWED_ACTIONS: dict[Intent, tuple[ActionType, ...]] = {
    Intent.SHIPPING_DELAY: (ActionType.ISSUE_COUPON, ActionType.SEND_REPLY),
    Intent.REFUND_REQUEST: (ActionType.REQUEST_REFUND, ActionType.SEND_REPLY),
    Intent.DAMAGED_ITEM: (
        ActionType.REQUEST_REPLACEMENT,
        ActionType.REQUEST_REFUND,
        ActionType.SEND_REPLY,
    ),
    Intent.UNKNOWN: (),  # 意图不可判定 → 不允许任何动作
}


@dataclass
class PlanProposal:
    """Action Planner 的提案。注意：不含金额。"""

    action: ActionType | None
    """None 表示不提出任何动作（意图不明或事实不足）。"""

    rationale: str = ""
    rejected_reason: str = ""
    """当 LLM 选择了一个不在允许集合里的动作时，记录被拒绝的原因。"""

    llm_raw: dict = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return self.action is None


def _is_policy_question(message: str) -> bool:
    """判断消息是否属于「政策咨询」而非「业务申请」。

    ★ 这是一个真实的产品分类问题，也是一个曾经被写错的地方：
      最初的实现是「带问号就算咨询」，结果「能补发吗？」「能不能部分退款？」
      「赶紧补发」这类明确的业务申请全被归为咨询，
      被走「查询类结案」路径静默关闭，客户诉求被丢弃。

      正确判据：只有当客户**在问规则本身**、且没有提出任何具体诉求时，才算咨询。
      规则判断而非 LLM：这类句式高度确定，规则结果可复现、可测试。
    """
    m = message or ""

    # 提到「规则本身」的词 —— 这是咨询的核心特征
    policy_indicators = (
        "政策",
        "规定",
        "规则",
        "标准",
        "条款",
        "怎样的",
        "是什么",
        "什么条件",
        "咨询一下",
        "了解一下",
        "多久内",
        "多少天内",
    )
    # 提出具体诉求的词 —— 出现任何一个就说明这是业务申请。
    # ★ 这里刻意**不包含**「退款」「退货」「补发」「处理」等名词/动词本身：
    #   它们同样出现在政策咨询里（「退款政策是怎样的」），
    #   列进来会把咨询误判成申请，把一个普通咨询推进审批流程。
    #   真正的判据是「表达行动意愿」的短语。
    request_indicators = (
        "我要",
        "帮我",
        "给我",
        "要求",
        "申请",
        "能不能",
        "可不可以",
        "能否",
        "赶紧",
        "麻烦你",
        "麻烦帮",
    )

    has_policy = any(k in m for k in policy_indicators)
    has_request = any(k in m for k in request_indicators)
    return has_policy and not has_request


class ActionPlanner:
    """动作规划 Agent。"""

    def __init__(self, llm: LLMClient | None = None, use_llm: bool = True):
        self.use_llm = use_llm
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient()
        return self._llm

    # ---------- 主入口 ----------

    def plan(self, intent: Intent, gathered: GatheredContext) -> PlanProposal:
        allowed = ALLOWED_ACTIONS.get(intent, ())

        # 意图不可判定 → 直接不提案，不浪费一次 LLM 调用
        if not allowed:
            return PlanProposal(
                action=None,
                rationale="意图不可判定，Action Planner 不提出任何动作，转人工澄清",
            )

        # 低置信（工具失败 / 循环截断）→ 保守：不提案
        if gathered.is_low_confidence and self._is_hard_low_confidence(gathered):
            return PlanProposal(
                action=None,
                rationale="信息收集不完整（硬性低置信信号），不提出动作",
                rejected_reason="; ".join(gathered.confidence_reasons()),
            )

        # ★ 咨询类消息不得触发业务动作 —— 这条守卫必须**先于路径分派**。
        #   踩过的坑：曾把它放在 `if not use_llm: return` 之后，
        #   结果确定性路径永远走不到守卫，「请问退款政策是怎样的」
        #   被规则版 Planner 提案成退款申请，普通咨询被推进审批流程。
        #   教训：守卫的位置必须覆盖所有分支，放在某个分支之后等于没写。
        if _is_policy_question(gathered.original_message):
            return PlanProposal(
                action=None,
                rationale="客户在咨询政策而非提出业务申请，按查询类工单处理",
            )

        if not self.use_llm:
            return self._deterministic_plan(intent, allowed, gathered)

        try:
            return self._llm_plan(intent, gathered, allowed)
        except LLMError:
            p = self._deterministic_plan(intent, allowed, gathered)
            p.rationale = f"[LLM不可用，规则降级] {p.rationale}"
            return p

    # ---------- LLM 路径 ----------

    def _llm_plan(
        self, intent: Intent, gathered: GatheredContext, allowed: tuple[ActionType, ...]
    ) -> PlanProposal:
        options = "\n".join(f"- {a.value}" for a in allowed)
        user = (
            f"意图：{intent.value}\n"
            f"已收集事实：{gathered.summary}\n"
            f"命中政策：{', '.join(gathered.policy_ids) or '无'}\n"
            f"知识缺口：{gathered.knowledge_gap}\n"
            f"工具失败：{'; '.join(gathered.tool_failures) or '无'}\n\n"
            f"可选动作：\n{options}\n- none（不采取任何业务动作）"
        )

        res = self.llm.complete(
            SYSTEM_PROMPT, user, json_mode=True, temperature=0.0, max_tokens=200
        )
        data = res.json()
        raw_action = str(data.get("action", "none")).strip()

        if raw_action.lower() in ("none", "", "null"):
            return PlanProposal(
                action=None,
                rationale=str(data.get("rationale", "模型判断不需要业务动作"))[:120],
                llm_raw=data,
            )

        # --- 校验：LLM 只能在允许集合内选择 ---
        try:
            chosen = ActionType(raw_action)
        except ValueError:
            return PlanProposal(
                action=None,
                rationale="模型返回了无法识别的动作，保守不提案",
                rejected_reason=f"LLM 返回的动作 {raw_action!r} 不是合法动作类型",
                llm_raw=data,
            )

        if chosen not in allowed:
            return PlanProposal(
                action=None,
                rationale="模型选择的动作不在该意图允许范围内，保守不提案",
                rejected_reason=(
                    f"意图 {intent.value} 不允许动作 {chosen.value}"
                    f"（允许：{', '.join(a.value for a in allowed)}）"
                ),
                llm_raw=data,
            )

        return PlanProposal(
            action=chosen,
            rationale=str(data.get("rationale", ""))[:120],
            llm_raw=data,
        )

    # ---------- 确定性降级路径 ----------

    @staticmethod
    def _deterministic_plan(
        intent: Intent, allowed: tuple[ActionType, ...], gathered: GatheredContext
    ) -> PlanProposal:
        """无 LLM 时的规则版规划。

        ★ 关键：不是「有意图就一定提业务动作」。
          绝大多数工单是「查询类」——客户只是想知道物流到哪了。
          如果对每条物流咨询都提案发优惠券，产品会把大量不需要补偿的工单
          推进审批或拒绝流程，既浪费人力也损害体验。
          因此只有当事实确实指向「需要补偿」时才提案。
        """
        preference = {
            Intent.SHIPPING_DELAY: ActionType.ISSUE_COUPON,
            Intent.REFUND_REQUEST: ActionType.REQUEST_REFUND,
            Intent.DAMAGED_ITEM: ActionType.REQUEST_REPLACEMENT,
        }
        preferred = preference.get(intent)
        if preferred is None or preferred not in allowed:
            return PlanProposal(action=None, rationale="无匹配动作（无 LLM）")

        # 物流延迟：只有确实延迟超过补偿门槛时才提补偿，否则按查询处理
        if preferred is ActionType.ISSUE_COUPON:
            delay = gathered.delay_hours
            if delay is None or delay <= 72:
                return PlanProposal(
                    action=None,
                    rationale=(
                        f"延迟 {delay if delay is not None else '未知'} 小时未达补偿门槛 72h，"
                        "按查询类工单处理，不提出补偿动作"
                    ),
                )

        # ★ 咨询类消息不应触发业务动作（该守卫已上移至 plan()，此处不再重复）。
        return PlanProposal(action=preferred, rationale="规则匹配（无 LLM）")

    # ---------- 低置信判定 ----------

    @staticmethod
    def _is_hard_low_confidence(gathered: GatheredContext) -> bool:
        """硬性低置信信号 —— 只有这些才直接导致不提案。

        ★ 注意区分：
          - 知识缺口（knownledge_gap != NONE）**不是** Planner 该处理的，
            它应由 Policy Engine 裁决为升级，而不是 Planner 私自不提案。
            否则「提案有没有」和「能不能做」的职责就混了。
          - 工具失败与循环截断才属于「信息拿不到」，Planner 无法规划。
        """
        return bool(gathered.tool_failures) or gathered.hit_iteration_cap
