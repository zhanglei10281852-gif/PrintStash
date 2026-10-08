"""The embedded-image ledger migration backfills current body references."""

from __future__ import annotations

import io
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic import command
from app.db import migrate

PREVIOUS = "8298455ff341"
REVISION = "6353d4dbb317"
_NAME = "a" * 64 + ".png"


@pytest.fixture
def upgraded_engine(tmp_path):
    db_file = tmp_path / "vault.sqlite"
    url = f"sqlite:///{db_file}"
    engine = create_engine(url)
    config = migrate._alembic_config(url)
    command.upgrade(config, PREVIOUS)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO documents (name, kind, created_at, updated_at) "
                 "VALUES ('doc-a', 'markdown', :now, :now)"),
            {"now": now},
        )
        connection.execute(
            text(
                "INSERT INTO collections (name, slug, path, created_at) "
                "VALUES ('Guides', 'guides', 'guides', :now)"
            ),
            {"now": now},
        )
        document_id = connection.execute(
            text("SELECT id FROM documents WHERE name = 'doc-a'")
        ).scalar_one()
        collection_id = connection.execute(
            text("SELECT id FROM collections WHERE slug = 'guides'")
        ).scalar_one()
        connection.execute(
            text("UPDATE documents SET body = :body WHERE id = :id"),
            {
                "id": document_id,
                "body": (
                    f"![x](/api/v1/documents/{document_id}/images/{_NAME}) "
                    f"![y](/api/v1/collections/{collection_id}/images/{_NAME})"
                ),
            },
        )
        connection.execute(
            text("UPDATE collections SET readme = :readme WHERE id = :id"),
            {
                "id": collection_id,
                "readme": f"![x](/api/v1/collections/{collection_id}/images/{_NAME})",
            },
        )
    command.upgrade(config, REVISION)
    try:
        yield engine, document_id, collection_id
    finally:
        engine.dispose()


class TestEmbeddedImageLifecycleMigration:
    def test_backfills_claims_from_every_existing_body(
        self, upgraded_engine
    ) -> None:
        engine, document_id, collection_id = upgraded_engine
        with engine.connect() as connection:
            rows = set(
                connection.execute(
                    text(
                        "SELECT owner_kind, owner_id, referrer_kind, referrer_id, name "
                        "FROM embedded_image_claims"
                    )
                ).all()
            )

        assert rows == {
            ("document", document_id, "document", document_id, _NAME),
            ("collection", collection_id, "document", document_id, _NAME),
            ("collection", collection_id, "collection", collection_id, _NAME),
        }

    def test_leaves_retirements_empty(self, upgraded_engine) -> None:
        engine, _document_id, _collection_id = upgraded_engine
        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT COUNT(*) FROM embedded_image_retirements")
            ).scalar_one()

        assert count == 0

    def test_creates_the_pending_retirement_partial_index(
        self, upgraded_engine
    ) -> None:
        engine, _document_id, _collection_id = upgraded_engine
        with engine.connect() as connection:
            definition = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE name = 'uq_embedded_image_retirement_pending'"
                )
            ).scalar_one()

        assert "UNIQUE INDEX" in definition
        assert "state = 'pending'" in definition
        inspector = inspect(engine)
        checks = inspector.get_check_constraints("embedded_image_retirements")
        assert any(
            check["name"] == "ck_embedded_image_retirements_state_values"
            for check in checks
        )

    def test_renders_offline_ddl_without_row_reads(self) -> None:
        output = io.StringIO()
        config = migrate._alembic_config("sqlite:///unused.sqlite")
        config.output_buffer = output
        command.upgrade(config, f"{PREVIOUS}:{REVISION}", sql=True)

        ddl = output.getvalue()
        assert "embedded_image_claims" in ddl
        assert "embedded_image_retirements" in ddl
        # The data backfill cannot read rows offline and is skipped.
        assert "INSERT" not in ddl
