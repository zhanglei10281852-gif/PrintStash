"""Actors, credentials, resource permissions, browser pairing and share grants."""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlmodel import Field

from app.core.time import utcnow
from app.db.encrypted import EncryptedText
from app.db.enum_columns import EnumText, enum_check

from .base import SQLModel
from .types import CaptureProvider, CollectionRole, FileType, PrinterRole


class CollectionPermission(SQLModel, table=True):
    __tablename__ = "collection_permissions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "collection_id",
            name="uq_collection_permissions_user_collection",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    collection_id: int = Field(foreign_key="collections.id", index=True)
    role: CollectionRole = Field(index=True)

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class PrinterPermission(SQLModel, table=True):
    __tablename__ = "printer_permissions"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "printer_id",
            name="uq_printer_permissions_user_printer",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    printer_id: int = Field(foreign_key="printers.id", index=True)
    role: PrinterRole = Field(index=True)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class User(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("oidc_issuer", "oidc_subject", name="uq_users_oidc_identity"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(max_length=128, unique=True, index=True)
    email: Optional[str] = Field(default=None, max_length=255)
    hashed_password: str = Field(max_length=255)
    is_superuser: bool = Field(default=False)
    is_active: bool = Field(default=True)
    # Incrementing this value invalidates every access token issued for user.
    # Persisted in DB so logout survives API restarts and multiple processes.
    auth_version: int = Field(default=0)
    # External identity is additive: local users keep these null and local login
    # remains available even when OIDC is configured.
    oidc_issuer: Optional[str] = Field(default=None, max_length=512, index=True)
    oidc_subject: Optional[str] = Field(default=None, max_length=255, index=True)
    oidc_managed: bool = Field(
        default=False,
        sa_column=Column(Boolean, nullable=False, server_default="0", index=True),
    )

    deleted_at: Optional[datetime] = Field(default=None, index=True)
    deleted_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RefreshToken(SQLModel, table=True):
    __tablename__ = "refresh_tokens"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    token_hash: str = Field(max_length=64, unique=True, index=True)
    expires_at: datetime = Field(index=True)
    revoked: bool = Field(default=False, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    revoked_at: Optional[datetime] = None


class ApiKey(SQLModel, table=True):
    __tablename__ = "api_keys"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(default="Programmatic access", max_length=128)
    key_hash: str = Field(max_length=64, unique=True, index=True)
    prefix: str = Field(max_length=16, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = Field(default=None, index=True)


class ProviderConnection(SQLModel, table=True):
    """One encrypted external-provider credential set owned by a user."""

    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", name="uq_provider_connection_user_provider"
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    provider: CaptureProvider = Field(
        sa_column=Column(String(32), nullable=False, index=True)
    )
    access_token: Optional[str] = Field(
        default=None, sa_column=Column(EncryptedText(), nullable=True)
    )
    refresh_token: Optional[str] = Field(
        default=None, sa_column=Column(EncryptedText(), nullable=True)
    )
    credential_secret: Optional[str] = Field(
        default=None, sa_column=Column(EncryptedText(), nullable=True)
    )
    token_expires_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ProviderOAuthState(SQLModel, table=True):
    __tablename__ = "provider_oauth_states"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    provider: CaptureProvider = Field(sa_column=Column(String(32), nullable=False))
    state_hash: str = Field(max_length=64, unique=True, index=True)
    redirect_uri: str = Field(max_length=2048)
    expires_at: datetime = Field(index=True)
    used_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utcnow)


class BrowserPairingCode(SQLModel, table=True):
    __tablename__ = "browser_pairing_codes"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    code_hash: str = Field(max_length=64, unique=True, index=True)
    expires_at: datetime = Field(index=True)
    used_at: Optional[datetime] = Field(default=None, index=True)
    attempts: int = Field(default=0)
    created_at: datetime = Field(default_factory=utcnow)


class BrowserDevice(SQLModel, table=True):
    __tablename__ = "browser_devices"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_browser_device_user_name"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    name: str = Field(max_length=128)
    credential_hash: str = Field(max_length=64, unique=True, index=True)
    created_at: datetime = Field(default_factory=utcnow)
    last_used_at: Optional[datetime] = Field(default=None)
    revoked_at: Optional[datetime] = Field(default=None, index=True)


class ShareLink(SQLModel, table=True):
    """A public, expiring, read-only capability to view a single Model.

    Possession of the (unguessable) token grants access to exactly one model —
    never the rest of the vault, never any mutation. Only the SHA-256 of the
    token is stored; the raw token is shown once at creation.

    A link without any ShareLinkPin rows is a *model* share: it follows the
    live model. A link with pins is a *revision* share: the pins are the exact
    Artifacts the link shows and the logical hold that keeps their bytes out of
    trash reclamation while the link is active. ``cover_file_id`` /
    ``cover_thumbnail_path`` snapshot the cover for a revision share so the
    thumbnail cannot drift either; exactly one of the two may be set.
    """

    __tablename__ = "share_links"
    __table_args__ = (
        CheckConstraint(
            "cover_file_id IS NULL OR cover_thumbnail_path IS NULL",
            name="cover_identity",
        ),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    model_id: int = Field(foreign_key="models.id", index=True)
    token_hash: str = Field(max_length=64, unique=True, index=True)

    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = Field(default=None, index=True)
    # When false, the public viewer can render the model but not download the
    # original source files (a tessellated mesh is still served for viewing).
    allow_download: bool = Field(default=False)
    # Revision-share cover snapshot (null for model shares). At most one of the
    # two is set; a pinned file id follows the file's thumbnail identity, while
    # a path carries a pre-thumbnail-file legacy cover.
    cover_file_id: Optional[int] = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("files.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
    )
    cover_thumbnail_path: Optional[str] = Field(default=None, max_length=2048)
    access_count: int = Field(default=0)

    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=utcnow)


class ShareLinkPin(SQLModel, table=True):
    """One pinned Artifact identity of a revision share, and its logical hold.

    The File row id is the non-drifting identity: a newly uploaded revision is
    a new row, so a pin can never silently follow the current live revision.
    The snapshot columns are the evidence captured at creation — they let the
    share render a stable "no longer available" entry if the row itself is ever
    missing, and keep the revision number/recommended marker stable while other
    revisions are trashed or restored. An active link's pin also marks the
    Artifact as held: trash reclamation skips held files and releases them only
    once the link expires or is revoked.
    """

    __tablename__ = "share_link_pins"
    __table_args__ = (
        UniqueConstraint(
            "share_link_id", "file_id", name="uq_share_link_pins_link_file"
        ),
        enum_check("file_type", FileType),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    share_link_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("share_links.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    # Restrict on purpose: purge deletes a held File only through the share
    # module, which clears pins of links that have expired or been revoked.
    file_id: int = Field(foreign_key="files.id", index=True)
    sha256: str = Field(max_length=64)
    file_type: FileType = Field(
        sa_column=Column(EnumText(FileType), nullable=False)
    )
    original_filename: str = Field(max_length=512)
    file_version: int
    # G-code revision numbers are derived from version order; snapshot the value
    # at pin time so trashing another revision cannot renumber what the customer
    # sees. Null for non-G-code pins.
    revision_number: Optional[int] = Field(default=None)
    is_recommended: bool = Field(default=False)
    created_at: datetime = Field(default_factory=utcnow)
