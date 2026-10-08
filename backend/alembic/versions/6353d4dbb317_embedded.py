"""embedded image reference lifecycle

Adds the durable ledger that connects markdown body references to embedded
raster image ownership:

* ``embedded_image_claims`` — one current reference from a document body or
  collection readme to an image in an owner namespace, including
  cross-document and cross-kind references.
* ``embedded_image_retirements`` — the precise owner-declared removal record
  for an image edited out of a body, kept pending, reclaimable, until the owner
  is permanently deleted without another referrer.

The data backfill populates current claims from every existing body/readme.
Retirements cannot be reconstructed (they record an edit-time removal set), so
they are intentionally left empty; backup/audit already classified such
objects from bodies before this migration.

Revision ID: 6353d4dbb317
Revises: 8298455ff341
Create Date: 2026-10-07 03:18:01.45585858
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "6353d4dbb317"
down_revision: Union[str, Sequence[str], None] = "8298455ff341"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "embedded_image_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("referrer_kind", sa.Text(), nullable=False),
        sa.Column("referrer_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=72), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "owner_kind IN ('document', 'collection')",
            name=op.f("ck_embedded_image_claims_owner_kind_values"),
        ),
        sa.CheckConstraint(
            "referrer_kind IN ('document', 'collection')",
            name=op.f("ck_embedded_image_claims_referrer_kind_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedded_image_claims")),
        sa.UniqueConstraint(
            "owner_kind",
            "owner_id",
            "referrer_kind",
            "referrer_id",
            "name",
            name="uq_embedded_image_claim_reference",
        ),
    )
    with op.batch_alter_table("embedded_image_claims", schema=None) as batch_op:
        batch_op.create_index(
            "ix_embedded_image_claim_owner",
            ["owner_kind", "owner_id", "name"],
            unique=False,
        )
        batch_op.create_index(
            "ix_embedded_image_claim_referrer",
            ["referrer_kind", "referrer_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_claims_last_seen_at"),
            ["last_seen_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_claims_owner_id"),
            ["owner_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_claims_owner_kind"),
            ["owner_kind"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_claims_referrer_id"),
            ["referrer_id"],
            unique=False,
        )

    op.create_table(
        "embedded_image_retirements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_kind", sa.Text(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=72), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("removed_at", sa.DateTime(), nullable=False),
        sa.Column("reclaimed_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "state IN ('pending', 'reclaimed', 'released')",
            name=op.f("ck_embedded_image_retirements_state_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedded_image_retirements")),
    )
    with op.batch_alter_table(
        "embedded_image_retirements", schema=None
    ) as batch_op:
        batch_op.create_index(
            "ix_embedded_image_retirement_owner",
            ["owner_kind", "owner_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_retirements_owner_id"),
            ["owner_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_retirements_removed_at"),
            ["removed_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_embedded_image_retirements_state"),
            ["state"],
            unique=False,
        )
        batch_op.create_index(
            "uq_embedded_image_retirement_pending",
            ["owner_kind", "owner_id", "name"],
            unique=True,
            sqlite_where=sa.text("state = 'pending'"),
            postgresql_where=sa.text("state = 'pending'"),
        )

    _backfill_claims()


def downgrade() -> None:
    with op.batch_alter_table(
        "embedded_image_retirements", schema=None
    ) as batch_op:
        batch_op.drop_index(
            "uq_embedded_image_retirement_pending",
            sqlite_where=sa.text("state = 'pending'"),
            postgresql_where=sa.text("state = 'pending'"),
        )
        batch_op.drop_index(batch_op.f("ix_embedded_image_retirements_state"))
        batch_op.drop_index(batch_op.f("ix_embedded_image_retirements_removed_at"))
        batch_op.drop_index(batch_op.f("ix_embedded_image_retirements_owner_id"))
        batch_op.drop_index("ix_embedded_image_retirement_owner")

    op.drop_table("embedded_image_retirements")
    with op.batch_alter_table("embedded_image_claims", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_embedded_image_claims_referrer_id"))
        batch_op.drop_index(batch_op.f("ix_embedded_image_claims_owner_kind"))
        batch_op.drop_index(batch_op.f("ix_embedded_image_claims_owner_id"))
        batch_op.drop_index(batch_op.f("ix_embedded_image_claims_last_seen_at"))
        batch_op.drop_index("ix_embedded_image_claim_referrer")
        batch_op.drop_index("ix_embedded_image_claim_owner")

    op.drop_table("embedded_image_claims")


def _backfill_claims() -> None:
    """Populate current claims from every body/readme that already embeds one.

    Uses core SQL rather than ORM models: the migrated chain builds some text
    columns (``documents.kind``) with different types than the current models,
    so deserializing a full Document row here can fail on enum lookups.

    Offline (``--sql``) rendering cannot read rows, so it skips the data work;
    the bounded reconciliation pass on first startup converges the same state.
    """
    if op.get_context().as_sql:
        return
    from app.core.time import utcnow
    from app.modules.library.embedded_images import extract_refs

    bind = op.get_bind()
    documents = sa.table(
        "documents",
        sa.column("id", sa.Integer),
        sa.column("body", sa.Text),
    )
    collections = sa.table(
        "collections",
        sa.column("id", sa.Integer),
        sa.column("readme", sa.Text),
    )
    claims = sa.table(
        "embedded_image_claims",
        sa.column("owner_kind", sa.Text),
        sa.column("owner_id", sa.Integer),
        sa.column("referrer_kind", sa.Text),
        sa.column("referrer_id", sa.Integer),
        sa.column("name", sa.String(length=72)),
        sa.column("created_at", sa.DateTime),
        sa.column("last_seen_at", sa.DateTime),
    )
    existing = {
        tuple(row)
        for row in bind.execute(
            sa.text(
                "SELECT owner_kind, owner_id, referrer_kind, referrer_id, name "
                "FROM embedded_image_claims"
            )
        ).all()
    }
    now = utcnow()
    payload: list[dict[str, object]] = []
    referrers = [
        ("document", row.id, row.body)
        for row in bind.execute(sa.select(documents.c.id, documents.c.body)).all()
    ]
    referrers.extend(
        ("collection", row.id, row.readme)
        for row in bind.execute(
            sa.select(collections.c.id, collections.c.readme)
        ).all()
    )
    for referrer_kind, referrer_id, text in referrers:
        for ref in extract_refs(text):
            key = (
                ref.owner_kind.value,
                ref.owner_id,
                referrer_kind,
                referrer_id,
                ref.name,
            )
            if key in existing:
                continue
            existing.add(key)
            payload.append(
                {
                    "owner_kind": ref.owner_kind.value,
                    "owner_id": ref.owner_id,
                    "referrer_kind": referrer_kind,
                    "referrer_id": referrer_id,
                    "name": ref.name,
                    "created_at": now,
                    "last_seen_at": now,
                }
            )
    if payload:
        bind.execute(claims.insert(), payload)
