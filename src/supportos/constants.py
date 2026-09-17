"""SupportOS 全局常量与阈值。

★ 所有金额/天数阈值集中在此处，方便评审与调整。
   修改阈值只改这个文件，不要在业务逻辑里写魔法数字。
"""

from __future__ import annotations

# ============================================================
# 角色权限（人类）
# ============================================================

ROLE_LIMITS: dict[str, dict[str, float | bool]] = {
    "AGENT_L1": {
        "coupon_max": 50.0,
        "refund_max": 200.0,
        "can_replace": True,
        "can_escalate": False,
    },
    "AGENT_L2": {
        "coupon_max": 100.0,
        "refund_max": 500.0,
        "can_replace": True,
        "can_escalate": True,
    },
    "SUPERVISOR": {
        "coupon_max": 200.0,
        "refund_max": 2000.0,
        "can_replace": True,
        "can_escalate": True,
    },
}


# ============================================================
# Agent Policy —— AI 特有的额外限制（独立于人类权限）
# ============================================================

AGENT_COUPON_MAX = 30.0
"""AI 单笔优惠券上限。即使客服有 ¥50 权限，AI 也只给到 ¥30。"""

AGENT_REFUND_MAX = 0.0
"""AI 不可直接退款，只能 request_refund。"""

AGENT_DAILY_COUPON_TOTAL_MAX = 500.0
"""单日累计优惠券上限，防止批量滥用。"""

AGENT_CUSTOMER_COUPON_COUNT_7D_MAX = 2
"""同一客户 7 日内优惠券次数上限。超过则升级。"""

AGENT_CUSTOMER_COUPON_COUNT_7D_SOFT = 1
"""7 日内已发 1 次时，第 2 次需标记为超额审批。"""

AGENT_REPLACEMENT_VALUE_MAX = 500.0
"""AI 可建议补发的商品价值上限，超过则升级。"""


# ============================================================
# 风险业务阈值
# ============================================================

POLICY_MIN_COUPON = 10.0
"""延迟补偿的最低优惠券金额（政策规定 ¥10~¥30）。"""

POLICY_MAX_COUPON = 30.0
"""延迟补偿的最高优惠券金额。"""

# ★ 已确认：退款 YELLOW/RED 分界线 = ¥200
REFUND_APPROVAL_MAX = 200.0
"""退款金额 <= ¥200 走审批；> ¥200 强制升级。"""

REFUND_HISTORY_COUNT_12M_MAX = 3
"""12 个月退款次数 >= 3 视为异常，强制升级。"""

SLA_DELAY_HOURS = 72
"""延迟超过 72 小时，方可提供补偿。"""

SLA_SEVERE_DELAY_HOURS = 72
"""严重 SLA 违约阈值（结合 VIP/高价值客户触发升级）。"""

AFTER_SALE_WINDOW_DAYS = 15
"""售后期限（天）。超期不予受理。"""


# ============================================================
# 阈值比较约定
# ============================================================

# ★ 已确认：所有阈值边界统一使用 <= （"最多 ¥30" 含 ¥30）
# 判断"是否超限"时一律用 amount > limit
BOUNDARY_INCLUSIVE = True


# ============================================================
# 升级触发词（可观测信号，不依赖 LLM 自报置信度）
# ============================================================

ESCALATION_TRIGGER_KEYWORDS: tuple[str, ...] = (
    "投诉",
    "消协",
    "12315",
    "起诉",
    "律师",
    "曝光",
    "媒体",
    "微博",
    "黑猫",
    "工商",
)

SENTIMENT_ANGRY = "ANGRY"
