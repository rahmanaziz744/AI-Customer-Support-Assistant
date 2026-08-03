"""Domain enums.

Stored as VARCHAR with a CHECK constraint (`native_enum=False`) rather than a
Postgres ENUM type, so adding a value is an ordinary migration instead of an
ALTER TYPE dance.
"""

from enum import StrEnum


class TicketStatus(StrEnum):
    NEW = "NEW"
    PROCESSING = "PROCESSING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    ESCALATED = "ESCALATED"
    RESOLVED = "RESOLVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class TicketCategory(StrEnum):
    REFUND_REQUEST = "REFUND_REQUEST"
    REPLACEMENT_REQUEST = "REPLACEMENT_REQUEST"
    ORDER_STATUS = "ORDER_STATUS"
    SHIPPING_ISSUE = "SHIPPING_ISSUE"
    BILLING = "BILLING"
    TECHNICAL_SUPPORT = "TECHNICAL_SUPPORT"
    ACCOUNT = "ACCOUNT"
    COMPLAINT = "COMPLAINT"
    GENERAL_INQUIRY = "GENERAL_INQUIRY"


class Sentiment(StrEnum):
    POSITIVE = "POSITIVE"
    NEUTRAL = "NEUTRAL"
    NEGATIVE = "NEGATIVE"
    VERY_NEGATIVE = "VERY_NEGATIVE"


class Channel(StrEnum):
    EMAIL = "EMAIL"
    WEB = "WEB"
    CHAT = "CHAT"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    COMPLETED = "COMPLETED"
    ESCALATED = "ESCALATED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class OrderStatus(StrEnum):
    PLACED = "PLACED"
    SHIPPED = "SHIPPED"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    RETURNED = "RETURNED"


class ActionType(StrEnum):
    REFUND = "REFUND"
    REPLACEMENT = "REPLACEMENT"


class ActionStatus(StrEnum):
    PROPOSED = "PROPOSED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
