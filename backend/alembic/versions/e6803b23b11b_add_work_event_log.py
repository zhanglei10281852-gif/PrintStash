"""Durable, expirable cursor log for realtime work events.

Adds ``work_events``: one monotonically numbered row per notice in its
authorization scope, written in the transaction of the Job or derivative
change it describes. A reconnecting ``/events/ws`` replays ``seq`` values
above its cursor from here instead of waiting for polling; rows past the
retention window are pruned, after which the cursor returns an explicit
resync. Holds ids and a hint only, never protected data.

Revision ID: e6803b23b11b
Revises: 8298455ff341
Create Date: 2026-10-07 10:39:24.838660

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e6803b23b11b'
down_revision: Union[str, Sequence[str], None] = '8298455ff341'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('work_events',
    sa.Column('seq', sa.BigInteger().with_variant(sa.Integer(), 'sqlite'), autoincrement=True, nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('scope', sa.Text(), nullable=False),
    sa.Column('scope_key', sa.Integer(), nullable=True),
    sa.Column('payload_json', sa.Text(), nullable=False),
    sa.CheckConstraint("(scope IN ('job_owner', 'model')) = (scope_key IS NOT NULL)", name=op.f('ck_work_events_scope_key_matches_scope')),
    sa.CheckConstraint("scope IN ('job_owner', 'job_admin', 'model', 'policy')", name=op.f('ck_work_events_scope_values')),
    sa.CheckConstraint('scope_key IS NULL OR scope_key > 0', name=op.f('ck_work_events_scope_key_positive')),
    sa.PrimaryKeyConstraint('seq', name=op.f('pk_work_events'))
    )
    with op.batch_alter_table('work_events', schema=None) as batch_op:
        batch_op.create_index('ix_work_events_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_work_events_scope_key_seq', ['scope', 'scope_key', 'seq'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('work_events', schema=None) as batch_op:
        batch_op.drop_index('ix_work_events_scope_key_seq')
        batch_op.drop_index('ix_work_events_created_at')

    op.drop_table('work_events')
