"""An existing Artifact survives PostgreSQL's native-enum to text upgrade."""

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import Session

from alembic import command
from app.db.migrate import _alembic_config
from app.db.models import FileType
from app.db.url import normalize_database_url
from tests.factories import build_model
from tests.factories.migration_rows import (
    RELEASED_V0121_REVISION,
    create_released_v0121_postgres_schema,
    seed_schema_row,
)

PREDECESSOR = "a23d2e57ff0c"
REVISION = "6f27f2e6090a"


def _seed_file(
    connection,
    *,
    model_id: int,
    path: str,
    filename: str,
    file_type: FileType,
    version: int,
    size: int,
    sha: str,
) -> None:
    # Seed against the historical schema with a reflected raw insert: the
    # current ORM mapper always emits columns later migrations introduced.
    seed_schema_row(
        connection,
        "files",
        model_id=model_id,
        path=path,
        original_filename=filename,
        file_type=file_type.value,
        version=version,
        size_bytes=size,
        sha256=sha,
    )


def _exercise_upgrade(url: str, *, postgres: bool) -> None:
    config = _alembic_config(url)
    engine = create_engine(url)
    try:
        if postgres:
            with engine.begin() as connection:
                create_released_v0121_postgres_schema(connection)
            command.stamp(config, RELEASED_V0121_REVISION)
        command.upgrade(config, PREDECESSOR)
        with Session(engine) as session:
            model = build_model(
                session, "Existing", slug="dxf-migration-existing", hash="a" * 64
            )
            session.commit()
            _seed_file(
                session.connection(),
                model_id=model.id,
                path="/library/existing.stl",
                filename="existing.stl",
                file_type=FileType.STL,
                version=1,
                size=3,
                sha="b" * 64,
            )
            session.commit()

        command.upgrade(config, REVISION)

        with engine.begin() as connection:
            existing = connection.execute(
                text(
                    "SELECT file_type, sha256, model_id FROM files "
                    "WHERE original_filename = 'existing.stl'"
                )
            ).one()
            assert existing[0] == FileType.STL.value
            assert existing[1] == "b" * 64
            _seed_file(
                connection,
                model_id=existing[2],
                path="/library/drawing.dxf",
                filename="drawing.dxf",
                file_type=FileType.DXF,
                version=2,
                size=12,
                sha="c" * 64,
            )
            drawing = connection.execute(
                text(
                    "SELECT file_type FROM files WHERE original_filename = 'drawing.dxf'"
                )
            ).one()
            assert drawing[0] == FileType.DXF.value

        indexes = {row["name"] for row in inspect(engine).get_indexes("files")}
        assert "uq_files_live_recommended_gcode_text" in indexes
        with pytest.raises(RuntimeError, match="DXF Artifacts"):
            command.downgrade(config, "-1")
        with engine.begin() as connection:
            assert {
                row[0]
                for row in connection.execute(
                    text("SELECT original_filename FROM files")
                ).all()
            } == {"existing.stl", "drawing.dxf"}
            drawing_id = connection.execute(
                text(
                    "SELECT id FROM files WHERE original_filename = 'drawing.dxf'"
                )
            ).scalar_one()
        # This database intentionally stops at the historical DXF revision.
        # Delete revision-local rows without asking the current Metadata mapper
        # to select columns introduced by later migrations.
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM metadata WHERE file_id = :file_id"),
                {"file_id": drawing_id},
            )
            connection.execute(
                text("DELETE FROM files WHERE id = :file_id"),
                {"file_id": drawing_id},
            )

        command.downgrade(config, PREDECESSOR)
        with engine.connect() as connection:
            assert [
                row[0]
                for row in connection.execute(
                    text("SELECT original_filename FROM files")
                ).all()
            ] == ["existing.stl"]
        assert "uq_files_live_recommended_gcode" in {
            row["name"] for row in inspect(engine).get_indexes("files")
        }
    finally:
        engine.dispose()


class TestDxfFiletypeMigration:
    def test_sqlite_upgrade_preserves_existing_artifact(self, tmp_path) -> None:
        _exercise_upgrade(
            f"sqlite:///{tmp_path / 'dxf-upgrade.sqlite'}", postgres=False
        )

    @pytest.mark.postgres
    def test_postgres_upgrade_preserves_existing_artifact(self) -> None:
        from tests.containers import postgres_url

        root_url = normalize_database_url(postgres_url())
        database = f"dxf_upgrade_{uuid4().hex}"
        admin = create_engine(root_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
        isolated_url = (
            make_url(root_url)
            .set(database=database)
            .render_as_string(hide_password=False)
        )
        try:
            _exercise_upgrade(isolated_url, postgres=True)
        finally:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'DROP DATABASE "{database}" WITH (FORCE)')
            admin.dispose()
