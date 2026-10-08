"""Revision shares migrate from JSON selections to durable pin identities.

The upgrade must give an existing revision link the exact identity the new
schema serves: pins for every still-live selected G-code and for the live mesh
Artifacts that used to be its always-visible companions, plus a cover snapshot.
Unresolvable selected ids (which the old code could never serve) are dropped,
and model shares (no JSON selection) stay model shares.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.sql import text

from alembic import command
from app.db.migrate import _alembic_config
from app.db.url import normalize_database_url
from tests.factories.migration_rows import seed_schema_row

PREVIOUS = "8298455ff341"
REVISION = "8b46d9a6830b"
_NOW = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _seed_legacy_world(connection) -> None:
    seed_schema_row(
        connection,
        "models",
        id=1,
        name="Shared",
        slug="shared",
        hash="a" * 64,
        thumbnail_file_id=1,
    )
    seed_schema_row(
        connection,
        "files",
        id=1,
        model_id=1,
        original_filename="part.stl",
        file_type="STL",
        path="/library/part.stl",
        version=1,
        size_bytes=3,
        sha256="1" * 64,
    )
    seed_schema_row(
        connection,
        "files",
        id=2,
        model_id=1,
        original_filename="rev1.gcode",
        file_type="GCODE",
        path="/library/rev1.gcode",
        version=2,
        size_bytes=5,
        sha256="2" * 64,
    )
    seed_schema_row(
        connection,
        "files",
        id=3,
        model_id=1,
        original_filename="rev2.gcode",
        file_type="GCODE",
        path="/library/rev2.gcode",
        version=3,
        size_bytes=7,
        sha256="3" * 64,
    )
    for link_id, payload in ((1, "[2]"), (2, None), (3, "[999]")):
        seed_schema_row(
            connection,
            "share_links",
            id=link_id,
            model_id=1,
            token_hash=f"hash-{link_id}",
            expires_at=_NOW,
            allow_download=True,
            access_count=0,
            created_at=_NOW,
            selected_file_ids_json=payload,
        )


def _exercise_upgrade(url: str) -> None:
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        command.upgrade(config, PREVIOUS)
        with engine.begin() as connection:
            _seed_legacy_world(connection)

        command.upgrade(config, REVISION)

        with engine.connect() as connection:
            columns = {
                row["name"]
                for row in inspect(engine).get_columns("share_links")
            }
            assert "selected_file_ids_json" not in columns

            pins = connection.execute(
                text(
                    "SELECT share_link_id, file_id, file_type, revision_number "
                    "FROM share_link_pins ORDER BY share_link_id, file_type, file_version"
                )
            ).all()
            # Link 1: the selected G-code (rev 1) plus the live mesh companion.
            # Link 3's selected id is unresolvable, but under the old semantics
            # a scoped link still showed every live mesh, so that companion is
            # preserved as a pin too.
            assert pins == [
                (1, 2, "gcode", 1),
                (1, 1, "stl", None),
                (3, 1, "stl", None),
            ]
            covers = connection.execute(
                text(
                    "SELECT id, cover_file_id, cover_thumbnail_path "
                    "FROM share_links ORDER BY id"
                )
            ).all()
            assert covers == [
                (1, 1, None),
                (2, None, None),
                (3, 1, None),
            ]

        command.downgrade(config, PREVIOUS)
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, selected_file_ids_json FROM share_links "
                    "ORDER BY id"
                )
            ).all()
            # The reverse migration restores selected G-code ids; mesh
            # companions were implicit under the old semantics, and link 3's
            # unresolvable id could not be reconstructed from its mesh pin.
            assert rows == [(1, "[2]"), (2, None), (3, "[]")]

        command.upgrade(config, REVISION)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT COUNT(*) FROM share_link_pins")).scalar() == 3
    finally:
        engine.dispose()


class TestShareLinkPinsMigration:
    def test_sqlite_backfill_creates_pins_and_cover(self, tmp_path) -> None:
        _exercise_upgrade(f"sqlite:///{tmp_path / 'share-pins-upgrade.sqlite'}")

    @pytest.mark.postgres
    def test_postgres_backfill_creates_pins_and_cover(self) -> None:
        from tests.containers import postgres_url

        root_url = normalize_database_url(postgres_url())
        database = f"share_pins_{uuid4().hex}"
        admin = create_engine(root_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        isolated_url = (
            make_url(root_url)
            .set(database=database)
            .render_as_string(hide_password=False)
        )
        try:
            _exercise_upgrade(isolated_url)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            admin.dispose()
