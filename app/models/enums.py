from __future__ import annotations

import enum


class Role(str, enum.Enum):
    SALES = "sales"
    ACCOUNTS = "accounts"
    ADMIN = "admin"

    @property
    def label(self) -> str:
        return {"sales": "Sales", "accounts": "Accounts in-charge", "admin": "Admin"}[self.value]


class SOType(str, enum.Enum):
    SALES_ORDER = "sales_order"          # own manufactured material
    TRADING_SALES_ORDER = "trading_sales_order"  # trading

    @property
    def label(self) -> str:
        return {
            "sales_order": "Sales Order (own manufactured material)",
            "trading_sales_order": "Trading Sales Order (trading)",
        }[self.value]

    @property
    def short(self) -> str:
        return "Sales Order" if self is SOType.SALES_ORDER else "Trading Sales Order"

    @property
    def number_prefix(self) -> str:
        return "SO" if self is SOType.SALES_ORDER else "TSO"


class SOStatus(str, enum.Enum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    REJECTED = "rejected"
    APPROVED = "approved"

    @property
    def label(self) -> str:
        return {
            "draft": "Draft",
            "pending_approval": "Awaiting approval",
            "rejected": "Sent back",
            "approved": "Approved",
        }[self.value]


class TallyStatus(str, enum.Enum):
    NOT_QUEUED = "not_queued"
    QUEUED = "queued"
    SYNCED = "synced"
    FAILED = "failed"

    @property
    def label(self) -> str:
        return {
            "not_queued": "Not sent",
            "queued": "Queued for Tally",
            "synced": "In Tally",
            "failed": "Tally failed",
        }[self.value]


class TaxMode(str, enum.Enum):
    CGST_SGST = "cgst_sgst"
    IGST = "igst"

    @property
    def label(self) -> str:
        return "CGST + SGST (same state)" if self is TaxMode.CGST_SGST else "IGST (other state)"


class OutboxStatus(str, enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SENT = "sent"
    FAILED = "failed"   # will be retried
    DEAD = "dead"       # retries exhausted, needs a human


class EmailStatus(str, enum.Enum):
    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"   # will be retried
    DEAD = "dead"       # retries exhausted, needs a human

    @property
    def label(self) -> str:
        return {"pending": "Waiting to send", "sending": "Sending", "sent": "Sent", "failed": "Failed, will retry", "dead": "Gave up"}[self.value]
