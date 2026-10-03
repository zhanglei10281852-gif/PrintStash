"""Notification channels and their recorded delivery outcomes."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Column,
    ForeignKey,
    Integer,
    Text,
)
from sqlmodel import Field

from app.core.time import utcnow
from app.db.encrypted import EncryptedText

from .base import SQLModel
from .types import NotificationDeliveryStatus, NotificationEventType, NotificationTarget


class NotificationChannel(SQLModel, table=True):
    """A configured outbound notification target (superuser-managed).

    Secret-bearing fields live in ``config_json`` (e.g. Telegram bot token,
    webhook URLs); the API masks them on read, mirroring the S3/MakerWorld
    secret handling. ``events_json`` and ``printer_ids_json`` hold the
    per-event and per-printer subscription filters as JSON arrays of strings;
    a null/empty ``printer_ids_json`` means "all printers".
    """

    __tablename__ = "notification_channels"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=128)
    target: NotificationTarget = Field(index=True)
    enabled: bool = Field(default=True, index=True)

    # Target-specific connection config (url, bot_token, chat_id, topic, ...).
    config_json: str = Field(
        default="{}", sa_column=Column(EncryptedText(), nullable=False)
    )
    # JSON array of NotificationEventType values this channel subscribes to.
    events_json: str = Field(default="[]", sa_column=Column(Text))
    # JSON array of printer ids; null = all printers.
    printer_ids_json: Optional[str] = Field(
        default=None, sa_column=Column(Text, nullable=True)
    )

    # Visible "last notification" status surfaced in the UI.
    last_status: Optional[str] = Field(default=None, max_length=16)
    last_error: Optional[str] = Field(default=None, max_length=1024)
    last_delivered_at: Optional[datetime] = Field(default=None)

    # Circuit breaker: consecutive permanently-failed deliveries. Reset to 0 on
    # any success; once it crosses the threshold the channel is auto-disabled so
    # a dead endpoint stops generating failures for every event.
    consecutive_failures: int = Field(default=0)

    # Configuration generation. Every material routing/authentication edit (and
    # every re-enable) starts a new generation: deliveries already in the outbox
    # keep delivering to the target/config they were enqueued with, and only
    # same-generation outcomes may touch last_status/consecutive_failures, so a
    # new generation always begins from independent health state.
    config_generation: int = Field(default=1)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class NotificationDelivery(SQLModel, table=True):
    """Outbox / work-queue row: one per (event x matching channel).

    Rows are inserted in the *same transaction* as the print-job/printer-status
    change that produced the event (transactional outbox), so events are never
    lost and — because emission is edge-triggered — never duplicated. A
    background dispatcher polls ``status in (pending)`` with
    ``next_retry_at <= now`` and delivers them with exponential backoff.

    ``context_json`` snapshots the rendered event context at enqueue time so a
    later channel-config edit can't retroactively change a queued payload. The
    ``channel_*`` snapshot columns do the same for *where and how* the event is
    delivered — target, encrypted connection config and the channel's
    configuration generation — so an admin edit, disable or delete can never
    redirect an outboxed event: its whole lifecycle (send, 429/503 backoff,
    stuck-send reclamation, process restart) continues along the generation it
    was enqueued under. ``channel_id`` is SET NULL on channel deletion; the
    snapshot keeps the row's identity and an in-flight row is closed with an
    observable failure instead of being silently redirected.

    ``claim_token`` fences the send claim: the conditional claim rotates it per
    claim, so concurrent claims and duplicate reclamation of a stale SENDING row
    cannot produce a second external send, and a result arriving with a stale
    token (from an attempt that lost the claim) is discarded.
    """

    __tablename__ = "notification_deliveries"

    id: Optional[int] = Field(default=None, primary_key=True)
    channel_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("notification_channels.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    event_type: NotificationEventType = Field(index=True)
    printer_id: Optional[int] = Field(
        default=None, foreign_key="printers.id", index=True
    )
    print_job_id: Optional[int] = Field(
        default=None, foreign_key="print_jobs.id", index=True
    )

    # Bound channel generation: target + encrypted config as they were when the
    # event entered the outbox, and the generation they belong to. NULL only for
    # rows predating the migration that could not be backfilled (their channel
    # was already gone), which fail observably rather than following live config.
    channel_target: Optional[NotificationTarget] = Field(default=None)
    channel_config_json: Optional[str] = Field(
        default=None, sa_column=Column(EncryptedText(), nullable=True)
    )
    channel_config_generation: Optional[int] = Field(default=None)

    context_json: str = Field(default="{}", sa_column=Column(Text))

    status: NotificationDeliveryStatus = Field(
        default=NotificationDeliveryStatus.PENDING, index=True
    )
    attempts: int = Field(default=0)
    last_error: Optional[str] = Field(default=None, max_length=1024)
    next_retry_at: datetime = Field(default_factory=utcnow, index=True)
    delivered_at: Optional[datetime] = Field(default=None)

    # Opaque fence token of the claim currently allowed to send/record. NULL
    # when no claim is active (PENDING due later, or a terminal row).
    claim_token: Optional[str] = Field(default=None, max_length=64, index=True)

    created_at: datetime = Field(default_factory=utcnow, index=True)
    updated_at: datetime = Field(default_factory=utcnow)
