"""领域模型与枚举。"""

from supportos.domain.enums import (
    ActionType,
    CustomerTier,
    Decision,
    Intent,
    KnowledgeGap,
    OrderStatus,
    Priority,
    RiskLevel,
    Sentiment,
    ShippingStatus,
    TicketState,
)
from supportos.domain.models import (
    AuditEntry,
    CaseContext,
    Customer,
    Evidence,
    Order,
    PolicyDecision,
    ProposedAction,
    Shipment,
    Ticket,
)

__all__ = [
    "ActionType",
    "CustomerTier",
    "Decision",
    "Intent",
    "KnowledgeGap",
    "OrderStatus",
    "Priority",
    "RiskLevel",
    "Sentiment",
    "ShippingStatus",
    "TicketState",
    "AuditEntry",
    "CaseContext",
    "Customer",
    "Evidence",
    "Order",
    "PolicyDecision",
    "ProposedAction",
    "Shipment",
    "Ticket",
]
