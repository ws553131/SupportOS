"""权限模型：Agent Permission ≤ Human Permission。

核心公式（PRD 7.1）::

    Effective Agent Permission
      = User Permission          # 发起工单的客服权限
      ∩ Agent Policy             # 系统对 AI 的额外限制（更严）
      ∩ Risk Policy              # 动作本身的风险约束

★ 已确认的产品决策：Agent 权限挂在**发起工单的客服**上。
   审批人不提供额外权限，只提供"临时授权"，且必须有上限与时效。
"""

from __future__ import annotations

from dataclasses import dataclass

from supportos import constants as C


@dataclass(frozen=True)
class Permission:
    """某个角色在某个动作上的有效权限。"""

    role: str
    coupon_max: float
    refund_max: float
    can_replace: bool
    can_escalate: bool
    source: str = "human"
    """human = 人类自身权限；agent = 经 AI 策略收紧后的权限。"""


def human_permission(role: str) -> Permission:
    """人类用户自身的权限。未知角色按最低权限处理（fail-safe）。"""
    limits = C.ROLE_LIMITS.get(role)
    if limits is None:
        # 未知角色 → 不给任何写权限，而不是给默认权限
        return Permission(
            role=role,
            coupon_max=0.0,
            refund_max=0.0,
            can_replace=False,
            can_escalate=False,
        )
    return Permission(
        role=role,
        coupon_max=float(limits["coupon_max"]),
        refund_max=float(limits["refund_max"]),
        can_replace=bool(limits["can_replace"]),
        can_escalate=bool(limits["can_escalate"]),
    )


def effective_agent_permission(role: str) -> Permission:
    """Agent 的有效权限 = 人类权限 ∩ Agent Policy（取更严格者）。"""
    human = human_permission(role)
    return Permission(
        role=role,
        coupon_max=min(human.coupon_max, C.AGENT_COUPON_MAX),
        refund_max=min(human.refund_max, C.AGENT_REFUND_MAX),
        can_replace=human.can_replace,
        can_escalate=False,  # AI 永远不能自行决定升级（升级是人或规则触发的）
        source="agent",
    )


@dataclass
class TemporaryGrant:
    """审批人给予的临时授权（超出 Agent Policy 的部分）。

    约束：
      - 仅对当前 action 有效，不累积
      - 不得超过审批人自身权限
      - 必须写入 Audit Log
    """

    action_id: str
    max_coupon: float = 0.0
    max_refund: float = 0.0
    granted_by: str = ""
    reason: str = ""


def apply_temporary_grant(
    perm: Permission, grant: TemporaryGrant | None, approver_role: str
) -> Permission:
    """把临时授权应用到 Agent 权限上（只放宽，不放宽 can_escalate）。"""
    if grant is None:
        return perm

    approver = human_permission(approver_role)
    # 临时授权的天花板是审批人自身权限
    coupon = min(grant.max_coupon, approver.coupon_max)
    refund = min(grant.max_refund, approver.refund_max)

    return Permission(
        role=perm.role,
        coupon_max=max(perm.coupon_max, coupon),
        refund_max=max(perm.refund_max, refund),
        can_replace=perm.can_replace,
        can_escalate=False,
        source=f"agent+grant({grant.granted_by})",
    )
