"""notification delivery config generations

Revision ID: c0e6c20a9213
Revises: a1d9da54fb03
Create Date: 2026-10-03 12:50:31.919606

Binds outboxed notification deliveries to the channel generation they were
created in. Adds:

* ``notification_channels.config_generation`` (starts at 1) and the
  ``system_config.notifications_generation_protection`` master switch
  (default on).
* ``notification_deliveries.channel_target`` / ``channel_config_json`` /
  ``channel_config_generation`` — the enqueue-time snapshot a delivery keeps
  following through sends, backoff, stuck-send reclamation and restarts — and
  ``claim_token``, the single-writer fence for concurrent/duplicate claims.
* Swaps the deliveries → channels foreign key to ``ON DELETE SET NULL`` so a
  channel can be deleted while delivery history remains; in-flight rows are
  closed by the application and the snapshot keeps the row's identity.

Data work the differ cannot see: existing deliveries are backfilled from their
channel (target, encrypted config copied verbatim, generation 1), existing
channels start at generation 1, and the settings row gets the default-on
flag. The two NOT NULL columns follow add/backfill/constrain so no server
default is left in the table definition — the models carry Python-side
defaults, and the chain ends at the same shape a fresh install gets. A channel
cannot have been deleted before this revision because the old key was
RESTRICT, so no orphaned pending rows exist to repair.

The downgrade restores RESTRICT and NOT NULL: delivery rows detached by a
channel deletion under SET NULL have no parent to point at, so they are removed
before the constraint is recreated.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "c0e6c20a9213"
down_revision: Union[str, Sequence[str], None] = "a1d9da54fb03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FK_NAME = "fk_notification_deliveries_channel_id_notification_channels"


def _reflected_table(bind, name: str, fk_column: str | None = None) -> sa.Table:
    """Reflect a table for ``copy_from``, optionally naming one foreign key."""
    table = sa.Table(name, sa.MetaData(), autoload_with=bind)
    if fk_column is not None:
        for constraint in table.foreign_key_constraints:
            if constraint.column_keys == [fk_column]:
                constraint.name = _FK_NAME
                break
        else:
            raise RuntimeError(f"{name}.{fk_column} foreign key is missing")
    return table


def _upgrade_deliveries(bind) -> None:
    """Add the snapshot/fence columns and swap the channel FK to SET NULL."""
    if bind.dialect.name == "sqlite":
        table = _reflected_table(bind, "notification_deliveries", "channel_id")
        with op.batch_alter_table(
            "notification_deliveries", copy_from=table
        ) as batch:
            batch.add_column(
                sa.Column(
                    "channel_target",
                    sa.Enum(
                        "WEBHOOK",
                        "DISCORD",
                        "TELEGRAM",
                        "NTFY",
                        name="notificationtarget",
                    ),
                    nullable=True,
                )
            )
            batch.add_column(
                sa.Column("channel_config_json", sa.Text(), nullable=True)
            )
            batch.add_column(
                sa.Column(
                    "channel_config_generation", sa.Integer(), nullable=True
                )
            )
            batch.add_column(
                sa.Column("claim_token", sa.String(length=64), nullable=True)
            )
            batch.alter_column(
                "channel_id", existing_type=sa.INTEGER(), nullable=True
            )
            batch.create_index(
                batch.f("ix_notification_deliveries_claim_token"),
                ["claim_token"],
                unique=False,
            )
            batch.drop_constraint(_FK_NAME, type_="foreignkey")
            batch.create_foreign_key(
                _FK_NAME,
                "notification_channels",
                ["channel_id"],
                ["id"],
                ondelete="SET NULL",
            )
        return

    # PostgreSQL path. The delivery FK on databases upgraded through the older
    # chain carries its system-assigned name, so find it by shape instead of
    # assuming the metadata naming convention filled it in.
    op.add_column(
        "notification_deliveries",
        sa.Column(
            "channel_target",
            sa.Enum(
                "WEBHOOK",
                "DISCORD",
                "TELEGRAM",
                "NTFY",
                name="notificationtarget",
                native_enum=True,
            ),
            nullable=True,
        ),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("channel_config_json", sa.Text(), nullable=True),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("channel_config_generation", sa.Integer(), nullable=True),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("claim_token", sa.String(length=64), nullable=True),
    )
    op.alter_column(
        "notification_deliveries",
        "channel_id",
        existing_type=sa.INTEGER(),
        nullable=True,
    )
    op.create_index(
        "ix_notification_deliveries_claim_token",
        "notification_deliveries",
        ["claim_token"],
        unique=False,
    )
    existing_delivery_fk = next(
        (
            fk
            for fk in sa.inspect(bind).get_foreign_keys("notification_deliveries")
            if fk.get("constrained_columns") == ["channel_id"]
            and fk.get("referred_table") == "notification_channels"
        ),
        None,
    )
    delivery_fk_name = (
        str(existing_delivery_fk.get("name"))
        if existing_delivery_fk and existing_delivery_fk.get("name")
        else _FK_NAME
    )
    op.drop_constraint(
        delivery_fk_name, "notification_deliveries", type_="foreignkey"
    )
    op.create_foreign_key(
        _FK_NAME,
        "notification_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
        ondelete="SET NULL",
    )


def _downgrade_deliveries(bind) -> None:
    """Restore the RESTRICT/NOT NULL key and drop the new columns."""
    if bind.dialect.name == "sqlite":
        table = _reflected_table(bind, "notification_deliveries", "channel_id")
        with op.batch_alter_table(
            "notification_deliveries", copy_from=table
        ) as batch:
            batch.drop_constraint(_FK_NAME, type_="foreignkey")
            batch.create_foreign_key(
                _FK_NAME,
                "notification_channels",
                ["channel_id"],
                ["id"],
            )
            batch.drop_index(batch.f("ix_notification_deliveries_claim_token"))
            batch.alter_column(
                "channel_id", existing_type=sa.INTEGER(), nullable=False
            )
            batch.drop_column("claim_token")
            batch.drop_column("channel_config_generation")
            batch.drop_column("channel_config_json")
            batch.drop_column("channel_target")
        return

    op.drop_constraint(
        _FK_NAME, "notification_deliveries", type_="foreignkey"
    )
    op.create_foreign_key(
        _FK_NAME,
        "notification_deliveries",
        "notification_channels",
        ["channel_id"],
        ["id"],
    )
    op.drop_index(
        "ix_notification_deliveries_claim_token",
        table_name="notification_deliveries",
    )
    op.alter_column(
        "notification_deliveries",
        "channel_id",
        existing_type=sa.INTEGER(),
        nullable=False,
    )
    op.drop_column("notification_deliveries", "claim_token")
    op.drop_column("notification_deliveries", "channel_config_generation")
    op.drop_column("notification_deliveries", "channel_config_json")
    op.drop_column("notification_deliveries", "channel_target")


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    _upgrade_deliveries(bind)

    # Backfill the bound generation from each delivery's channel. Correlated
    # subqueries (no UPDATE ... FROM) so the statement shape stays identical on
    # SQLite and PostgreSQL. ``config_json`` is copied as stored — ciphertext
    # encrypted with this installation's key — so it never passes through a
    # type adapter. On PostgreSQL the new column is the native enum type, so
    # the varchar copied from the old table is explicitly cast to it.
    target_expr = (
        "(SELECT target FROM notification_channels "
        "WHERE notification_channels.id "
        "= notification_deliveries.channel_id)"
    )
    if not is_sqlite:
        target_expr = f"CAST({target_expr} AS notificationtarget)"
    bind.execute(
        sa.text(
            f"""
            UPDATE notification_deliveries
               SET channel_target = {target_expr},
                   channel_config_json = (
                       SELECT config_json FROM notification_channels
                        WHERE notification_channels.id
                              = notification_deliveries.channel_id
                   ),
                   channel_config_generation = 1
             WHERE channel_id IS NOT NULL
               AND channel_target IS NULL
            """
        )
    )

    # --- notification_channels.config_generation: add, backfill, constrain ---
    op.add_column(
        "notification_channels",
        sa.Column("config_generation", sa.Integer(), nullable=True),
    )
    bind.execute(sa.text("UPDATE notification_channels SET config_generation = 1"))
    if is_sqlite:
        with op.batch_alter_table("notification_channels") as batch:
            batch.alter_column(
                "config_generation",
                existing_type=sa.INTEGER(),
                nullable=False,
            )
    else:
        op.alter_column(
            "notification_channels",
            "config_generation",
            existing_type=sa.INTEGER(),
            nullable=False,
        )

    # --- system_config flag: add (nullable), backfill, constrain -----------
    op.add_column(
        "system_config",
        sa.Column(
            "notifications_generation_protection", sa.Boolean(), nullable=True
        ),
    )
    # A bound boolean (not the integer literal 1) so PostgreSQL's strict
    # boolean cast accepts the same statement SQLite accepts loosely.
    bind.execute(
        sa.text(
            "UPDATE system_config SET notifications_generation_protection = :on"
        ),
        {"on": True},
    )
    if is_sqlite:
        with op.batch_alter_table("system_config") as batch:
            batch.alter_column(
                "notifications_generation_protection",
                existing_type=sa.BOOLEAN(),
                nullable=False,
            )
    else:
        op.alter_column(
            "system_config",
            "notifications_generation_protection",
            existing_type=sa.BOOLEAN(),
            nullable=False,
        )


def downgrade() -> None:
    bind = op.get_bind()

    # SET NULL could have detached rows that NOT NULL/RESTRICT cannot represent.
    # Channel deletion also terminalized their in-flight state, so the only
    # surviving rows are delivery history whose parent no longer exists.
    bind.execute(
        sa.text(
            "DELETE FROM notification_deliveries WHERE channel_id IS NULL"
        )
    )

    op.drop_column("system_config", "notifications_generation_protection")
    op.drop_column("notification_channels", "config_generation")
    _downgrade_deliveries(bind)
    # The PostgreSQL enum type is shared with notification_channels.target, so
    # it is intentionally not dropped here.
