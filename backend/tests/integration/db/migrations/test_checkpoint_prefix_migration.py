"""Prefix-scoped scan checkpoints backfill and re-key on upgrade.

Historical checkpoint rows were whole-library epochs; the migration must
backfill ``scan_prefix`` from each library's ``source_prefix`` before making it
NOT NULL and replacing the per-library unique constraint with a per-(library,
prefix) one. The downgrade collapses a multi-prefix installation back to one
checkpoint row per library without losing its newest epoch.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.db.migrate import _alembic_config
from tests.factories.migration_rows import seed_schema_row

PREVIOUS = "a1d9da54fb03"
HEAD = "997a0b6933ea"


def _seed_library_with_checkpoint(connection) -> None:
    seed_schema_row(
        connection,
        "external_libraries",
        id=1,
        name="Remote source",
        root_path="",
        source_kind="S3",
        source_prefix="models",
        collection_mode="MIRROR",
        watch_mode="OFF",
        last_scan_status="OK",
    )
    seed_schema_row(
        connection,
        "external_library_checkpoints",
        id=1,
        library_id=1,
        epoch="historical-epoch",
        observed_keys_json="[]",
        complete=False,
    )


def _assert_upgrade_shape(engine) -> None:
    with engine.connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT scan_prefix FROM external_library_checkpoints WHERE id = 1"
                )
            ).scalar_one()
            == "models"
        )
        # Explicit ids: a batch rebuild copies rows without advancing the new
        # table's sequence, so an autoincrement id is untrustworthy here.
        connection.execute(
            text(
                "INSERT INTO external_library_checkpoints"
                " (id, library_id, scan_prefix, epoch, observed_keys_json, complete,"
                "  metadata_ops, bytes_read, started_at, updated_at)"
                " VALUES (2, 1, 'models/sub', 'prefix-epoch', '[]', FALSE, 0, 0,"
                " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
            )
        )
        connection.commit()
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO external_library_checkpoints"
                    " (id, library_id, scan_prefix, epoch, observed_keys_json,"
                    "  complete, metadata_ops, bytes_read, started_at, updated_at)"
                    " VALUES (3, 1, 'models/sub', 'duplicate-epoch', '[]', FALSE,"
                    " 0, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
                )
            )
        connection.rollback()


class TestCheckpointPrefixUpgrade:
    def test_scopes_historical_checkpoints_by_prefix_on_upgrade(self, tmp_path):
        url = f"sqlite:///{tmp_path / 'checkpoint-prefix.sqlite'}"
        config = _alembic_config(url)
        command.upgrade(config, PREVIOUS)
        engine = create_engine(url)
        try:
            with engine.begin() as connection:
                _seed_library_with_checkpoint(connection)
            command.upgrade(config, HEAD)
            _assert_upgrade_shape(engine)
            with engine.connect() as connection:
                columns = {
                    column["name"]: column
                    for column in inspect(connection).get_columns(
                        "external_library_checkpoints"
                    )
                }
                assert columns["scan_prefix"]["nullable"] is False
            command.downgrade(config, PREVIOUS)
            with engine.connect() as connection:
                assert "scan_prefix" not in {
                    column["name"]
                    for column in inspect(connection).get_columns(
                        "external_library_checkpoints"
                    )
                }
                remaining = connection.execute(
                    text("SELECT library_id, epoch FROM external_library_checkpoints")
                ).all()
                assert remaining == [(1, "prefix-epoch")]
            command.upgrade(config, HEAD)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT scan_prefix FROM external_library_checkpoints"
                            " WHERE epoch = 'prefix-epoch'"
                        )
                    ).scalar_one()
                    == "models"
                )
        finally:
            engine.dispose()


class TestPostgresCheckpointPrefixUpgrade:
    @pytest.mark.postgres
    def test_postgres_scopes_historical_checkpoints_on_upgrade(self):
        from app.db.migrate import run_migrations
        from app.db.url import normalize_database_url
        from tests.containers import postgres_url
        from tests.factories.migration_rows import (
            RELEASED_V0121_REVISION,
            create_released_v0121_postgres_schema,
        )

        url = postgres_url()
        engine = create_engine(normalize_database_url(url))
        config = _alembic_config(url)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA public CASCADE")
                connection.exec_driver_sql("CREATE SCHEMA public")
                # The full chain is SQLite-path-supported; PostgreSQL coverage
                # adopts a released installation and upgrades through it.
                create_released_v0121_postgres_schema(connection)
            command.stamp(config, RELEASED_V0121_REVISION)
            command.upgrade(config, PREVIOUS)
            with engine.begin() as connection:
                _seed_library_with_checkpoint(connection)
            command.upgrade(config, HEAD)
            _assert_upgrade_shape(engine)
            command.downgrade(config, PREVIOUS)
            command.upgrade(config, HEAD)
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT scan_prefix FROM external_library_checkpoints"
                            " WHERE epoch = 'prefix-epoch'"
                        )
                    ).scalar_one()
                    == "models"
                )
        finally:
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP SCHEMA public CASCADE")
                connection.exec_driver_sql("CREATE SCHEMA public")
            run_migrations(url)
            engine.dispose()
