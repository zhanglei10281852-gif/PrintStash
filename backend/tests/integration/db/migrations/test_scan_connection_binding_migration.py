"""Remote scan binding columns are additive and backfill a connection revision."""

from __future__ import annotations

import re
from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from tests.paths import ALEMBIC_DIR, ALEMBIC_INI

REVISION = "61ec49c68499"
PREVIOUS = "a1d9da54fb03"


def _config(db_path: Path) -> Config:
    config = Config(str(ALEMBIC_INI))
    config.set_main_option("script_location", str(ALEMBIC_DIR))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return config


class TestScanConnectionBindingMigration:
    def test_upgrade_backfills_revision_and_adds_binding_columns(
        self, tmp_path: Path
    ) -> None:
        db_path = tmp_path / "scan-connection-binding.sqlite"
        config = _config(db_path)
        command.upgrade(config, PREVIOUS)
        engine = create_engine(f"sqlite:///{db_path}")
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO storage_connections (
                            name, kind, config_json, secret_json, enabled,
                            created_at, updated_at
                        ) VALUES (
                            'Existing remote', 'S3', '{}', '{}', 1,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                        )
                        """
                    )
                )

            command.upgrade(config, REVISION)

            with engine.connect() as connection:
                revision = connection.execute(
                    text(
                        "SELECT revision FROM storage_connections "
                        "WHERE name = 'Existing remote'"
                    )
                ).scalar_one()
                assert re.fullmatch(r"[0-9a-f]{32}", revision)
                checkpoint_columns = {
                    column["name"]
                    for column in inspect(engine).get_columns(
                        "external_library_checkpoints"
                    )
                }
                assert {
                    "connection_id",
                    "connection_revision",
                    "connection_target_ref",
                } <= checkpoint_columns
                file_columns = {
                    column["name"]
                    for column in inspect(engine).get_columns("files")
                }
                assert "source_target_ref" in file_columns

            command.downgrade(config, PREVIOUS)

            assert "revision" not in {
                column["name"]
                for column in inspect(engine).get_columns("storage_connections")
            }
            with engine.connect() as connection:
                assert (
                    connection.execute(
                        text(
                            "SELECT kind FROM storage_connections "
                            "WHERE name = 'Existing remote'"
                        )
                    ).scalar_one()
                    == "S3"
                )
        finally:
            engine.dispose()
