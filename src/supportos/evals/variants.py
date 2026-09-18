"""消融实验的版本变体 —— 用于回答「SupportOS 到底解决了什么」。

★ 这是整个作品集里最重要的一个实验：

  前面所有章节都在论证「Policy Engine 是必要的」，但那都是**推理**。
  本模块让推理变成**可测量的对照实验**：

    V0 纯 LLM      ：LLM 提案 → 直接执行。无策略裁决、无审批、无守卫。
    V1 +Policy     ：加入 Policy Engine 裁决，但审批要求被自动放行（无人审批环节）。
    V2 +审批 Gate  ：完整系统（当前实现）。

  通过对比三者在同一批 46 个案例上的表现，可以量化出：
    - Policy Engine 单独贡献了多少安全性
    - 人工审批 Gate 又额外贡献了多少
    - 以及每一层治理付出的自动化率代价

★ 判定口径的关键设计：
  为了跨版本可比，安全违规必须基于**结果**判定（"本应人工介入的案例是否被执行/结案"），
  而不是基于「引擎裁决是否为 AUTO」。
  否则 V0 因为根本没有引擎，会显示为 100% 合规 —— 一个荒谬的结论。
  对应 `ViolationType.UNGATED_WRITE_EXECUTION`。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from supportos.domain.enums import Decision
from supportos.domain.models import CaseContext, PolicyDecision, ProposedAction
from supportos.policy.engine import CaseMeta, PolicyEngine


# ============================================================
# 引擎变体
# ============================================================


class NullPolicyEngine:
    """V0 基线：**无治理**。一律放行。

    它代表「直接让 LLM 处理工单」的产品形态 —— 很多团队的第一版就是这样。
    接口与 PolicyEngine 对齐，以便编排器无差别调用。
    """

    name = "V0_null"

    def __init__(self, meta: CaseMeta | None = None):
        self.meta = meta or CaseMeta()

    def evaluate(self, action: ProposedAction, ctx: CaseContext) -> PolicyDecision:
        return PolicyDecision(
            decision=Decision.AUTO,
            reason="无治理基线：不做任何风险检查，直接放行",
            rule_id="NULL_ENGINE",
        )

    def revalidate(
        self, action: ProposedAction, before: CaseContext, now: CaseContext
    ) -> PolicyDecision:
        return self.evaluate(action, now)

    # 编排器的守卫会调用这两个方法取信号；V0 刻意不提供任何信号
    def _check_case_risk_signals(self, ctx: CaseContext) -> str | None:
        return None

    def _check_escalation_signals(self, ctx: CaseContext) -> str | None:
        return None


class NoApprovalEngine:
    """V1：有策略裁决，但**审批环节被自动放行**。

    代表「加了规则校验但没做人工门禁」的产品形态。
    它与 V2 的差异恰好等于「人工审批 Gate」的贡献。
    """

    name = "V1_no_approval"

    def __init__(self, meta: CaseMeta | None = None):
        self._inner = PolicyEngine(meta=meta or CaseMeta())

    @property
    def meta(self) -> CaseMeta:
        return self._inner.meta

    def evaluate(self, action: ProposedAction, ctx: CaseContext) -> PolicyDecision:
        d = self._inner.evaluate(action, ctx)
        if d.decision is Decision.APPROVAL_REQUIRED:
            # 审批要求被自动满足 —— 这正是本变体要模拟的缺陷
            return d.model_copy(
                update={
                    "decision": Decision.AUTO,
                    "rule_id": f"AUTO_APPROVED({d.rule_id})",
                    "reason": f"[审批被自动放行] {d.reason}",
                    "requires_approval_by": None,
                }
            )
        return d

    def revalidate(
        self, action: ProposedAction, before: CaseContext, now: CaseContext
    ) -> PolicyDecision:
        d = self._inner.revalidate(action, before, now)
        if d.decision is Decision.APPROVAL_REQUIRED:
            return d.model_copy(update={"decision": Decision.AUTO})
        return d

    def _check_case_risk_signals(self, ctx: CaseContext) -> str | None:
        return self._inner._check_case_risk_signals(ctx)  # noqa: SLF001

    def _check_escalation_signals(self, ctx: CaseContext) -> str | None:
        return self._inner._check_escalation_signals(ctx)  # noqa: SLF001


# ============================================================
# 变体配置
# ============================================================


@dataclass(frozen=True)
class Variant:
    key: str
    name: str
    description: str
    engine_factory: Callable[[CaseMeta], object]
    enforce_resolve_blocker: bool
    """是否启用「不得自动结案」的守卫。

    V0 关闭（纯 LLM 版本没有治理层，LLM 不提案就结案）；
    V1/V2 开启。这样 V0 与 V1 的差异主要来自 Policy Engine，
    V1 与 V2 的差异主要来自审批 Gate。
    """


VARIANTS: dict[str, Variant] = {
    "v0": Variant(
        key="v0",
        name="V0 纯 LLM",
        description="LLM 提案后直接执行：无策略裁决、无审批、无守卫",
        engine_factory=lambda meta: NullPolicyEngine(meta),
        enforce_resolve_blocker=False,
    ),
    "v1": Variant(
        key="v1",
        name="V1 +Policy Engine",
        description="加入确定性策略裁决，但审批要求被自动放行（无人工门禁）",
        engine_factory=lambda meta: NoApprovalEngine(meta),
        enforce_resolve_blocker=True,
    ),
    "v2": Variant(
        key="v2",
        name="V2 +审批 Gate（完整系统）",
        description="策略裁决 + 人工审批 + 全链路审计（当前实现）",
        engine_factory=lambda meta: PolicyEngine(meta=meta),
        enforce_resolve_blocker=True,
    ),
}


def get_variant(key: str) -> Variant:
    if key not in VARIANTS:
        raise KeyError(f"未知变体 {key}，可选：{', '.join(VARIANTS)}")
    return VARIANTS[key]
