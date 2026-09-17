"""SupportOS 领域枚举。

所有跨模块共享的取值集合都定义在这里，避免字符串散落各处。
"""

from __future__ import annotations

from enum import Enum


class Intent(str, Enum):
    """V0.1 冻结三类意图。

    注意：投诉不单独作为 Intent，而是作为 sentiment + SLA 违约的风险修饰符。
    理由：『延迟五天的投诉』同时匹配 SHIPPING_DELAY 与 COMPLAINT，
    分类边界模糊，更适合作为叠加信号处理。
    """

    SHIPPING_DELAY = "SHIPPING_DELAY"      # 物流延迟
    DAMAGED_ITEM = "DAMAGED_ITEM"          # 商品破损
    REFUND_REQUEST = "REFUND_REQUEST"      # 退款申请
    UNKNOWN = "UNKNOWN"                    # 无法归类 → 触发升级


class Priority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    URGENT = "URGENT"


class Sentiment(str, Enum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    ANGRY = "ANGRY"


class RiskLevel(str, Enum):
    """PRD 第 11 节冻结的三级风险模型。"""

    GREEN = "GREEN"      # 只读，自动执行
    YELLOW = "YELLOW"    # 有副作用，人工审批
    RED = "RED"          # 不可逆/大额，强制升级


class Decision(str, Enum):
    """Policy Engine 的四种输出。"""

    AUTO = "AUTO"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    ESCALATE = "ESCALATE"
    DENY = "DENY"


class ActionType(str, Enum):
    """V0.1 冻结的全部动作类型。"""

    # --- Read Tools（GREEN）---
    GET_CUSTOMER = "get_customer"
    GET_ORDER = "get_order"
    GET_SHIPPING = "get_shipping"
    SEARCH_KNOWLEDGE = "search_knowledge"

    # --- Write Tools（需裁决）---
    ISSUE_COUPON = "issue_coupon"
    REQUEST_REFUND = "request_refund"
    REQUEST_REPLACEMENT = "request_replacement"
    SEND_REPLY = "send_reply"

    # --- 明确禁止（DENY）---
    REFUND_MONEY = "refund_money"
    BAN_ACCOUNT = "ban_account"
    DELETE_TICKET = "delete_ticket"
    MODIFY_AUDIT_LOG = "modify_audit_log"


class TicketState(str, Enum):
    """PRD 第 16 节冻结的 9 状态机。"""

    NEW = "NEW"
    TRIAGING = "TRIAGING"
    RESEARCHING = "RESEARCHING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    EXECUTING = "EXECUTING"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    CLOSED = "CLOSED"


class OrderStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PAID = "PAID"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    REFUNDED = "REFUNDED"
    CLOSED = "CLOSED"


class ShippingStatus(str, Enum):
    IN_TRANSIT = "IN_TRANSIT"
    DELIVERED = "DELIVERED"
    DELAYED = "DELAYED"
    EXCEPTION = "EXCEPTION"      # 运输异常
    LOST = "LOST"
    RETURNED = "RETURNED"


class CustomerTier(str, Enum):
    NORMAL = "NORMAL"
    VIP = "VIP"
    SVIP = "SVIP"


class KnowledgeGap(str, Enum):
    """知识缺口标记。Agent 找不到依据时禁止编造规则。"""

    NONE = "NONE"
    NO_POLICY_FOUND = "NO_POLICY_FOUND"
    POLICY_EXPIRED = "POLICY_EXPIRED"
    RULE_CONFLICT = "RULE_CONFLICT"
    INSUFFICIENT = "INSUFFICIENT"
    """命中了通用政策，但未覆盖当前具体情况（如海外订单、定制商品）。
    政策存在不等于政策够用 —— 这是真实客服里最常见的一类缺口。"""
