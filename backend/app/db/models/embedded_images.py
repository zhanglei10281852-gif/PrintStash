"""Durable ledger connecting markdown body references to embedded image bytes.

A markdown body (``Document.body`` or ``Collection.readme``) can embed raster
images served from an owner namespace (``/documents/<id>/images/<name>`` or
``/collections/<id>/images/<name>``). Parsing the body at read time used to be
the only record of that reference, so an editor who removed an image silently
left an unowned blob, while a document that embedded another document's image
could have its bytes deleted out from under it.

Two tables make that reference lifecycle explicit and recoverable:

``embedded_image_claims``
    One *current* reference from a referrer body to one image in an owner
    namespace. Claims are reconciled in the same transaction as the body they
    describe, so a failed or lost edit leaves old and new references both
    reconvergeable. An image referenced by any other referrer cannot be
    released when its owner is purged.

``embedded_image_retirements``
    The owner's exact removal record. Editing a self-reference out of the body
    creates a ``pending`` row — observable, backed up, and reclaimed when the
    reference returns. Only the precise owner's permanent deletion, with no
    other referrer claim, moves it to ``released`` and authorizes physical
    deletion.

Neither table carries a foreign key. Owners are hard-deleted rows, but ledger
rows must survive that deletion as evidence while a durable storage delete
intent — or another document's reference — can still act on the exact image.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Index, UniqueConstraint, text
from sqlmodel import Field

from app.core.time import utcnow
from app.db.enum_columns import EnumText, enum_check

from .base import SQLModel
from .types import EmbeddedImageOwnerKind, EmbeddedImageRetirementState


class EmbeddedImageClaim(SQLModel, table=True):
    """One current body reference: referrer body -> image in owner namespace."""

    __tablename__ = "embedded_image_claims"
    __table_args__ = (
        UniqueConstraint(
            "owner_kind",
            "owner_id",
            "referrer_kind",
            "referrer_id",
            "name",
            name="uq_embedded_image_claim_reference",
        ),
        Index(
            "ix_embedded_image_claim_owner",
            "owner_kind",
            "owner_id",
            "name",
        ),
        Index(
            "ix_embedded_image_claim_referrer",
            "referrer_kind",
            "referrer_id",
        ),
        enum_check("owner_kind", EmbeddedImageOwnerKind),
        enum_check("referrer_kind", EmbeddedImageOwnerKind),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_kind: EmbeddedImageOwnerKind = Field(
        sa_column=Column(EnumText(EmbeddedImageOwnerKind), nullable=False, index=True)
    )
    owner_id: int = Field(index=True)
    referrer_kind: EmbeddedImageOwnerKind = Field(
        sa_column=Column(EnumText(EmbeddedImageOwnerKind), nullable=False)
    )
    referrer_id: int = Field(index=True)
    # ``<sha256>.<ext>`` — 64 hex chars, a dot, at most 4 ("jpeg").
    name: str = Field(max_length=72)
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime = Field(default_factory=utcnow, index=True)


class EmbeddedImageRetirement(SQLModel, table=True):
    """One owner-declared removal of one of its own embedded images."""

    __tablename__ = "embedded_image_retirements"
    __table_args__ = (
        Index(
            "uq_embedded_image_retirement_pending",
            "owner_kind",
            "owner_id",
            "name",
            unique=True,
            sqlite_where=text("state = 'pending'"),
            postgresql_where=text("state = 'pending'"),
        ),
        Index(
            "ix_embedded_image_retirement_owner",
            "owner_kind",
            "owner_id",
        ),
        enum_check("state", EmbeddedImageRetirementState),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    owner_kind: EmbeddedImageOwnerKind = Field(
        sa_column=Column(EnumText(EmbeddedImageOwnerKind), nullable=False)
    )
    owner_id: int = Field(index=True)
    name: str = Field(max_length=72)
    state: EmbeddedImageRetirementState = Field(
        default=EmbeddedImageRetirementState.PENDING,
        sa_column=Column(
            EnumText(EmbeddedImageRetirementState), nullable=False, index=True
        ),
    )
    removed_at: datetime = Field(default_factory=utcnow, index=True)
    reclaimed_at: Optional[datetime] = None
    released_at: Optional[datetime] = None
