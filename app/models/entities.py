"""All ORM entities.

Conventions
-----------
* Money is ``Numeric(14, 2)``, quantity ``Numeric(14, 3)``; never floats.
* Business entities carry ``SoftDeleteMixin``; the audit log and outbox are append-only.
* Sales orders snapshot party / item / ledger names and addresses at creation, so later
  master-data edits never silently rewrite history.
* ``SalesOrder.version`` provides optimistic locking.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SoftDeleteMixin, TimestampMixin, utcnow
from app.models.enums import EmailStatus, OutboxStatus, Role, SOStatus, SOType, TallyStatus, TaxMode


def _enum(e: type) -> Enum:
    return Enum(e, native_enum=False, length=32, values_callable=lambda x: [m.value for m in x], validate_strings=True)


# --------------------------------------------------------------------------- users
class User(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[Role] = mapped_column(_enum(Role), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    failed_login_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime())
    password_changed_at: Mapped[datetime | None] = mapped_column(DateTime())
    # Plants (regions) a sales person may raise orders for. Only enforced for the sales role.
    plants: Mapped[list["Plant"]] = relationship(secondary="user_plants", lazy="selectin", order_by="Plant.name")

    @property
    def can_sign_in(self) -> bool:
        return self.is_active and self.deleted_at is None


class UserPlant(Base):
    """Which plants a user is allowed to sell from."""

    __tablename__ = "user_plants"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    plant_id: Mapped[int] = mapped_column(ForeignKey("plants.id", ondelete="CASCADE"), primary_key=True)


class UserSession(Base):
    """Server-side session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime())
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))

    user: Mapped[User] = relationship(lazy="joined")


# --------------------------------------------------------------------------- master data
class Party(Base, TimestampMixin, SoftDeleteMixin):
    """Customer (Tally 'Party A/C')."""

    __tablename__ = "parties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)  # must equal the Tally ledger name
    gstin: Mapped[str | None] = mapped_column(String(15))
    default_payment_term: Mapped[str | None] = mapped_column(String(200))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    address_line2: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state_code: Mapped[str | None] = mapped_column(String(2))   # GST state code, e.g. "27"
    pincode: Mapped[str | None] = mapped_column(String(6))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Item(Base, TimestampMixin, SoftDeleteMixin):
    """Stock item. `name` must equal the Tally stock item name."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    hsn_code: Mapped[str | None] = mapped_column(String(8))
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False, default=Decimal("18.00"))
    unit: Mapped[str] = mapped_column(String(10), nullable=False, default="MT")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SalesLedger(Base, TimestampMixin, SoftDeleteMixin):
    """Tally sales ledger, e.g. 'Sales GST'."""

    __tablename__ = "sales_ledgers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Plant(Base, TimestampMixin, SoftDeleteMixin):
    """A company plant / location. Its GST state decides CGST+SGST (same state as the customer) or IGST."""

    __tablename__ = "plants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(500), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, onupdate=utcnow, nullable=False)
    updated_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class NumberSequence(Base):
    """Gap-free counters, locked with SELECT ... FOR UPDATE when allocating."""

    __tablename__ = "number_sequences"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


# --------------------------------------------------------------------------- sales orders
class SalesOrder(Base, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "sales_orders"
    __table_args__ = (
        CheckConstraint("subtotal >= 0", name="subtotal_non_negative"),
        Index("ix_sales_orders_status_created", "status", "created_at"),
        Index("ix_sales_orders_creator_status", "created_by_id", "status"),
        Index("ix_sales_orders_party_po", "party_id", "customer_po_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    so_number: Mapped[str | None] = mapped_column(String(32), unique=True)  # allocated on first submit
    so_type: Mapped[SOType] = mapped_column(_enum(SOType), nullable=False)
    order_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    party_id: Mapped[int] = mapped_column(ForeignKey("parties.id", ondelete="RESTRICT"), nullable=False)
    party_name: Mapped[str] = mapped_column(String(200), nullable=False)
    party_gstin: Mapped[str | None] = mapped_column(String(15))
    payment_term: Mapped[str] = mapped_column(String(200), nullable=False)
    customer_po_number: Mapped[str] = mapped_column(String(60), nullable=False)

    billing_address: Mapped[str] = mapped_column(Text, nullable=False)
    billing_state_code: Mapped[str] = mapped_column(String(2), nullable=False)
    shipping_address: Mapped[str] = mapped_column(Text, nullable=False)
    shipping_state_code: Mapped[str | None] = mapped_column(String(2))

    # Plant that supplies the order (nullable only for rows created before plants existed).
    plant_id: Mapped[int | None] = mapped_column(ForeignKey("plants.id", ondelete="RESTRICT"))
    plant_name: Mapped[str | None] = mapped_column(String(200))
    plant_state_code: Mapped[str | None] = mapped_column(String(2))

    sales_ledger_id: Mapped[int] = mapped_column(ForeignKey("sales_ledgers.id", ondelete="RESTRICT"), nullable=False)
    sales_ledger_name: Mapped[str] = mapped_column(String(200), nullable=False)

    narration: Mapped[str] = mapped_column(Text, nullable=False)   # generated by the server from the items, never typed
    remarks: Mapped[str] = mapped_column(String(500), nullable=False, default="")  # typed by sales; pushed to Tally after the narration

    tax_mode: Mapped[TaxMode] = mapped_column(_enum(TaxMode), nullable=False)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    cgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    sgst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    igst_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False, default=0)

    status: Mapped[SOStatus] = mapped_column(_enum(SOStatus), nullable=False, default=SOStatus.DRAFT, index=True)
    tally_status: Mapped[TallyStatus] = mapped_column(_enum(TallyStatus), nullable=False, default=TallyStatus.NOT_QUEUED)
    tally_reference: Mapped[str | None] = mapped_column(String(100))

    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime())
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime())
    review_remarks: Mapped[str | None] = mapped_column(String(500))

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __mapper_args__ = {"version_id_col": version}

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id], lazy="joined")
    reviewed_by: Mapped[User | None] = relationship(foreign_keys=[reviewed_by_id], lazy="joined")
    items: Mapped[list["SalesOrderItem"]] = relationship(
        back_populates="order", cascade="all, delete-orphan", order_by="SalesOrderItem.line_no", lazy="selectin"
    )

    # The queue row for this order (set once it is approved). Read-only here; the queue service owns it.
    outbox: Mapped["TallyOutbox | None"] = relationship(
        "TallyOutbox", primaryjoin="SalesOrder.id == TallyOutbox.order_id", foreign_keys="TallyOutbox.order_id",
        uselist=False, lazy="selectin", viewonly=True,
    )

    @property
    def is_editable(self) -> bool:
        return self.status in (SOStatus.DRAFT, SOStatus.REJECTED)


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "line_no", name="uq_so_items_order_line"),
        CheckConstraint("quantity > 0", name="qty_positive"),
        CheckConstraint("rate > 0", name="rate_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="CASCADE"), nullable=False, index=True)
    line_no: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="RESTRICT"), nullable=False)
    item_name: Mapped[str] = mapped_column(String(200), nullable=False)
    hsn_code: Mapped[str | None] = mapped_column(String(8))
    unit: Mapped[str] = mapped_column(String(10), nullable=False, default="MT")
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    gst_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    order: Mapped[SalesOrder] = relationship(back_populates="items")


# --------------------------------------------------------------------------- audit + outbox
class AuditLog(Base):
    """Append-only trail. Application code only ever INSERTs here."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False, index=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), index=True)
    actor_email: Mapped[str | None] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(String(45))
    request_id: Mapped[str | None] = mapped_column(String(36))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class TallyOutbox(Base, TimestampMixin):
    """Transactional outbox: written in the same transaction as the approval."""

    __tablename__ = "tally_outbox"
    __table_args__ = (Index("ix_outbox_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("sales_orders.id", ondelete="RESTRICT"), nullable=False, unique=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[OutboxStatus] = mapped_column(_enum(OutboxStatus), nullable=False, default=OutboxStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_error: Mapped[str | None] = mapped_column(String(1000))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime())
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime())   # first time a push was tried
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime())    # most recent push try
    response_reference: Mapped[str | None] = mapped_column(String(100))

    order: Mapped[SalesOrder] = relationship(lazy="joined")


# --------------------------------------------------------------------------- notifications + email
class Notification(Base):
    """One message in a person's inbox. Soft-deleted by the owner; old read ones are purged by housekeeping."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_inbox", "user_id", "read_at", "created_at"),
        UniqueConstraint("user_id", "dedupe_key", name="uq_notifications_dedupe"),   # NULL keys never collide
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(10), nullable=False, default="info")   # info | success | warning | danger
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    url: Mapped[str | None] = mapped_column(String(300))
    entity_type: Mapped[str | None] = mapped_column(String(48))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    dedupe_key: Mapped[str | None] = mapped_column(String(80))
    email_id: Mapped[int | None] = mapped_column(ForeignKey("email_queue.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime())

    email: Mapped["QueuedEmail | None"] = relationship(lazy="joined")


class NotificationPref(Base):
    """Per person, per category: show in the app and/or send by email. Security notices cannot be switched off."""

    __tablename__ = "notification_prefs"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    category: Mapped[str] = mapped_column(String(24), primary_key=True)
    in_app: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    email: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, onupdate=utcnow, nullable=False)


class QueuedEmail(Base):
    """Every email the system sends. Written in the same transaction as the event, delivered by a worker."""

    __tablename__ = "email_queue"
    __table_args__ = (Index("ix_email_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    to_email: Mapped[str] = mapped_column(String(254), nullable=False, index=True)
    to_name: Mapped[str | None] = mapped_column(String(120))
    subject: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    html_body: Mapped[str | None] = mapped_column(Text)
    text_body: Mapped[str | None] = mapped_column(Text)
    sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)   # body holds a secret link: wiped once done
    status: Mapped[EmailStatus] = mapped_column(_enum(EmailStatus), nullable=False, default=EmailStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False, index=True)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime())
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime())
    last_error: Mapped[str | None] = mapped_column(String(500))
    message_id: Mapped[str | None] = mapped_column(String(160))
    requested_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    requested_by: Mapped["User | None"] = relationship(foreign_keys=[requested_by_id], lazy="joined")


class PasswordReset(Base):
    """A forgot-password link. Only the SHA-256 of the token is stored; it works once and expires quickly."""

    __tablename__ = "password_resets"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(), default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime())
    requested_by_admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(255))
