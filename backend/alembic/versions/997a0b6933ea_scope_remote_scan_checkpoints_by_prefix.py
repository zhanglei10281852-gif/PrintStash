"""scope remote scan checkpoints by prefix

Adds ``external_library_checkpoints.scan_prefix`` and replaces the one-row-per-
library unique constraint with one row per (library, scanned prefix). This is
what lets a subdirectory scan keep its own resumable epoch, cursor and
observations independently of the whole-library ``source_prefix`` epoch.

Historical checkpoint rows were all whole-library epochs, so they are backfilled
from their library's ``source_prefix`` before the column is constrained NOT NULL.

Revision ID: 997a0b6933ea
Revises: a1d9da54fb03
Create Date: 2026-10-03 12:23:19.742503

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "997a0b6933ea"
down_revision: Union[str, Sequence[str], None] = "a1d9da54fb03"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the nullable prefix, backfill it, then constrain and re-key."""
    op.add_column(
        "external_library_checkpoints",
        sa.Column("scan_prefix", sa.Text(), nullable=True),
    )
    # Every pre-feature epoch enumerated the library's configured source_prefix.
    # The correlated scalar subquery is valid on both SQLite and PostgreSQL;
    # CASCADE on the library foreign key means no orphan checkpoint can survive
    # to receive NULL.
    op.execute(
        "UPDATE external_library_checkpoints SET scan_prefix = ("
        " SELECT source_prefix FROM external_libraries"
        " WHERE external_libraries.id ="
        " external_library_checkpoints.library_id)"
    )
    with op.batch_alter_table("external_library_checkpoints", schema=None) as batch_op:
        batch_op.alter_column("scan_prefix", existing_type=sa.Text(), nullable=False)
        batch_op.drop_constraint("uq_external_library_checkpoint", type_="unique")
        batch_op.create_unique_constraint(
            "uq_external_library_checkpoint_prefix",
            ["library_id", "scan_prefix"],
        )


def downgrade() -> None:
    """Restore the single-checkpoint-per-library shape and drop the prefix.

    A prefix-scoped installation may hold several live checkpoint rows per
    library, which the old single-row constraint cannot represent. Keep the
    newest epoch per library and remove the rest explicitly (the observations
    FK is CASCADE on PostgreSQL, but Alembic runs SQLite migrations with foreign
    keys off, so children are deleted here for both dialects).
    """
    op.execute(
        "DELETE FROM external_library_observations WHERE checkpoint_id IN ("
        " SELECT id FROM external_library_checkpoints WHERE id NOT IN ("
        "  SELECT max(id) FROM external_library_checkpoints GROUP BY library_id))"
    )
    op.execute(
        "DELETE FROM external_library_checkpoints WHERE id NOT IN ("
        " SELECT max(id) FROM external_library_checkpoints GROUP BY library_id)"
    )
    with op.batch_alter_table("external_library_checkpoints", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_external_library_checkpoint_prefix", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_external_library_checkpoint", ["library_id"]
        )
        batch_op.drop_column("scan_prefix")
