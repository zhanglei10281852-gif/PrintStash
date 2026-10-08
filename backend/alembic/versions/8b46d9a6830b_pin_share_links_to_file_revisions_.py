"""pin share links to file revisions [confirmed data-dropping, not a rename: share_links.selected_file_ids_json]

Revision shares used a JSON list of live G-code ids. That identity drifted: a
soft-deleted revision vanished from the share and trash reclamation could
delete its bytes, while there was nowhere to record a logical hold or a cover
snapshot. This migration introduces the durable ``share_link_pins`` identity and
the share's cover columns, then converts existing revision links:

* every still-live selected G-code id becomes a pin with its snapshot;
* the link's live non-G-code Artifacts become pins too — under the old
  semantics they were the always-visible companions of the selected revision;
* the model's current cover is snapshotted so the public thumbnail cannot
  drift after the upgrade either.

Ids that no longer resolve to a live Artifact of the link's model are dropped:
the old JSON path could never have served them. Links without a JSON selection
stay model shares (no pins, no cover) and keep following the live model.

Revision ID: 8b46d9a6830b
Revises: 8298455ff341
Create Date: 2026-10-07 09:34:54.218684

"""
import json
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8b46d9a6830b"
down_revision: Union[str, Sequence[str], None] = "8298455ff341"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MESH_FILE_TYPES = ("stl", "3mf", "obj", "step", "dxf")


def _backfill_revision_shares(connection: sa.Connection) -> None:
    """Convert legacy JSON selections to pin rows and snapshot each cover."""
    links = connection.execute(
        sa.text(
            "SELECT id, model_id, created_at, selected_file_ids_json "
            "FROM share_links WHERE selected_file_ids_json IS NOT NULL"
        )
    ).all()
    for link_id, model_id, created_at, raw_json in links:
        try:
            parsed = json.loads(raw_json)
        except (TypeError, ValueError):
            continue
        if not isinstance(parsed, list):
            continue
        selected_ids = [int(item) for item in parsed if isinstance(item, int)]

        files = connection.execute(
            sa.text(
                "SELECT id, sha256, file_type, original_filename, version, "
                "is_recommended, deleted_at FROM files WHERE model_id = :model_id"
            ),
            {"model_id": model_id},
        ).all()
        live_files = [row for row in files if row.deleted_at is None]
        # The legacy File column stores enum names (''GCODE''); pin columns use
        # enum values (''gcode'') like every EnumText column.
        gcode_rank = {
            row.id: rank
            for rank, row in enumerate(
                sorted(
                    (row for row in live_files if row.file_type.lower() == "gcode"),
                    key=lambda row: row.version,
                ),
                start=1,
            )
        }
        chosen = [
            row
            for row in sorted(
                live_files, key=lambda row: (row.file_type.lower(), row.version)
            )
            if row.id in selected_ids or row.file_type.lower() in _MESH_FILE_TYPES
        ]
        for row in chosen:
            connection.execute(
                sa.text(
                    "INSERT INTO share_link_pins "
                    "(share_link_id, file_id, sha256, file_type, original_filename, "
                    "file_version, revision_number, is_recommended, created_at) "
                    "VALUES (:link_id, :file_id, :sha256, :file_type, :filename, "
                    ":version, :revision_number, :recommended, :created_at)"
                ),
                {
                    "link_id": link_id,
                    "file_id": row.id,
                    "sha256": row.sha256,
                    "file_type": row.file_type.lower(),
                    "filename": row.original_filename,
                    "version": row.version,
                    "revision_number": gcode_rank.get(row.id),
                    "recommended": bool(row.is_recommended),
                    "created_at": created_at,
                },
            )

        cover = connection.execute(
            sa.text(
                "SELECT thumbnail_file_id, thumbnail_path FROM models WHERE id = :id"
            ),
            {"id": model_id},
        ).first()
        if cover is None:
            continue
        cover_file_id, cover_path = cover
        if cover_file_id is not None:
            exists = connection.execute(
                sa.text("SELECT 1 FROM files WHERE id = :id"),
                {"id": cover_file_id},
            ).first()
            if exists is not None:
                connection.execute(
                    sa.text(
                        "UPDATE share_links SET cover_file_id = :file_id "
                        "WHERE id = :link_id"
                    ),
                    {"file_id": cover_file_id, "link_id": link_id},
                )
                continue
        if cover_path:
            connection.execute(
                sa.text(
                    "UPDATE share_links SET cover_thumbnail_path = :path "
                    "WHERE id = :link_id"
                ),
                {"path": cover_path, "link_id": link_id},
            )


def _restore_json_selections(connection: sa.Connection) -> None:
    """Reverse the backfill: old code only stored selected G-code ids."""
    links = connection.execute(
        sa.text(
            "SELECT DISTINCT share_link_id FROM share_link_pins"
        )
    ).all()
    for (link_id,) in links:
        gcode_ids = [
            row.file_id
            for row in connection.execute(
                sa.text(
                    "SELECT file_id FROM share_link_pins "
                    "WHERE share_link_id = :link_id AND file_type = 'gcode' "
                    "ORDER BY file_version ASC"
                ),
                {"link_id": link_id},
            ).all()
        ]
        connection.execute(
            sa.text(
                "UPDATE share_links SET selected_file_ids_json = :payload "
                "WHERE id = :link_id"
            ),
            {"payload": json.dumps(gcode_ids), "link_id": link_id},
        )


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "share_link_pins",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("share_link_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("file_type", sa.Text(), nullable=False),
        sa.Column("original_filename", sa.String(length=512), nullable=False),
        sa.Column("file_version", sa.Integer(), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=True),
        sa.Column("is_recommended", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "file_type IN ('stl', '3mf', 'gcode', 'obj', 'step', 'dxf')",
            name=op.f("ck_share_link_pins_file_type_values"),
        ),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_share_link_pins_file_id_files"),
        ),
        sa.ForeignKeyConstraint(
            ["share_link_id"],
            ["share_links.id"],
            name=op.f("fk_share_link_pins_share_link_id_share_links"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_share_link_pins")),
        sa.UniqueConstraint(
            "share_link_id", "file_id", name="uq_share_link_pins_link_file"
        ),
    )
    with op.batch_alter_table("share_link_pins", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_share_link_pins_file_id"), ["file_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_share_link_pins_share_link_id"),
            ["share_link_id"],
            unique=False,
        )

    # Keep the legacy column for the data migration; it is dropped afterwards.
    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.add_column(sa.Column("cover_file_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("cover_thumbnail_path", sa.String(length=2048), nullable=True)
        )
        batch_op.create_check_constraint(
            batch_op.f("ck_share_links_cover_identity"),
            "cover_file_id IS NULL OR cover_thumbnail_path IS NULL",
        )
        batch_op.create_index(
            batch_op.f("ix_share_links_cover_file_id"),
            ["cover_file_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_share_links_cover_file_id_files"),
            "files",
            ["cover_file_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if not op.get_context().as_sql:
        _backfill_revision_shares(op.get_bind())

    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.drop_column("selected_file_ids_json")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.add_column(sa.Column("selected_file_ids_json", sa.TEXT(), nullable=True))

    if not op.get_context().as_sql:
        _restore_json_selections(op.get_bind())

    with op.batch_alter_table("share_links", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_share_links_cover_file_id_files"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_share_links_cover_file_id"))
        batch_op.drop_constraint(
            batch_op.f("ck_share_links_cover_identity"), type_="check"
        )
        batch_op.drop_column("cover_thumbnail_path")
        batch_op.drop_column("cover_file_id")

    with op.batch_alter_table("share_link_pins", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_share_link_pins_share_link_id"))
        batch_op.drop_index(batch_op.f("ix_share_link_pins_file_id"))

    op.drop_table("share_link_pins")
