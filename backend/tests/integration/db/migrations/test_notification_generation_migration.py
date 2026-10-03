"""``c0e6c20a9213`` pins notification deliveries to a channel generation.

An upgrade over populated data has to do three things the schema differ cannot
see:

* Backfill every existing delivery's bound generation from its channel — target
  and the *encrypted* config blob copied verbatim, generation 1 — and start
  every existing channel at generation 1.
* Land the two new NOT NULL columns without a server default (the models use
  Python-side defaults), which is the add/backfill/constrain dance.
* Swap the deliveries → channels foreign key to ``ON DELETE SET NULL`` without
  losing a single row on either backend, so deleting a channel keeps delivery
  history and only detaches the link.

The downgrade restores RESTRICT/NOT NULL and therefore removes deliveries a
SET NULL deletion detached; rows whose channel survives must come through it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from alembic import command
from app.db.migrate import _alembic_config
from app.db.url import normalize_database_url

BEFORE = "a1d9da54fb03"
AFTER = "c0e6c20a9213"

_WHEN = datetime(2026, 1, 1, 0, 0, 0)


@pytest.fixture(
    params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)],
    ids=str,
)
def migrated(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[Engine]:
    """A populated database upgraded from BEFORE through AFTER."""
    if request.param == "postgres":
        from tests.containers import fresh_postgres_database
        from tests.factories.migration_rows import (
            RELEASED_V0121_REVISION,
            create_released_v0121_postgres_schema,
        )

        # The full chain cannot bootstrap PostgreSQL from zero (older
        # migrations predate that path): start from the released schema and
        # replay forward, exactly like a real PostgreSQL self-hoster.
        url = fresh_postgres_database("migration")
    else:
        url = f"sqlite:///{tmp_path / 'notification-generations.sqlite'}"
    engine = create_engine(normalize_database_url(url))
    config = _alembic_config(url)
    try:
        if request.param == "postgres":
            with engine.begin() as conn:
                create_released_v0121_postgres_schema(conn)
            command.stamp(config, RELEASED_V0121_REVISION)
        command.upgrade(config, BEFORE)
        with engine.begin() as conn:
            channels = _table(conn, "notification_channels")
            conn.execute(
                channels.insert().values(
                    id=1,
                    name="ch",
                    target="WEBHOOK",
                    enabled=True,
                    config_json='{"url": "https://hooks.example.test/a"}',
                    events_json='["printer_offline"]',
                    printer_ids_json=None,
                    last_status=None,
                    last_error=None,
                    last_delivered_at=None,
                    consecutive_failures=3,
                    created_at=_WHEN,
                    updated_at=_WHEN,
                )
            )
            deliveries = _table(conn, "notification_deliveries")
            conn.execute(
                deliveries.insert().values(
                    id=1,
                    channel_id=1,
                    event_type="PRINTER_OFFLINE",
                    printer_id=None,
                    print_job_id=None,
                    context_json="{}",
                    status="PENDING",
                    attempts=1,
                    last_error=None,
                    next_retry_at=_WHEN,
                    delivered_at=None,
                    created_at=_WHEN,
                    updated_at=_WHEN,
                )
            )
        command.upgrade(config, AFTER)
        yield engine
    finally:
        engine.dispose()


def _table(conn, name: str):
    from sqlalchemy import MetaData, Table

    return Table(name, MetaData(), autoload_with=conn)


def _column(engine: Engine, table: str, name: str):
    return next(
        column
        for column in inspect(engine).get_columns(table)
        if column["name"] == name
    )


def _channel_fk(engine: Engine) -> str | None:
    key = next(
        fk
        for fk in inspect(engine).get_foreign_keys("notification_deliveries")
        if fk["constrained_columns"] == ["channel_id"]
    )
    return (key.get("options") or {}).get("ondelete")


def _delivery_row(engine: Engine) -> dict:
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT channel_id, channel_target, channel_config_json, "
                    "channel_config_generation, claim_token, status "
                    "FROM notification_deliveries WHERE id = 1"
                )
            ).mappings().one()
        )


class TestUpgrade:
    def test_backfills_the_bound_generation_from_the_channel(
        self, migrated: Engine
    ) -> None:
        row = _delivery_row(migrated)

        assert row["channel_target"] == "WEBHOOK"
        assert row["channel_config_generation"] == 1
        with migrated.connect() as conn:
            stored_config = conn.execute(
                text(
                    "SELECT config_json FROM notification_channels WHERE id = 1"
                )
            ).scalar_one()
        # Copied byte-for-byte: the encrypted blob never passes through a
        # type adapter, so it stays decryptable by this installation.
        assert row["channel_config_json"] == stored_config
        assert row["claim_token"] is None

    def test_starts_every_channel_at_generation_one(self, migrated: Engine) -> None:
        with migrated.connect() as conn:
            generation = conn.execute(
                text(
                    "SELECT config_generation FROM notification_channels "
                    "WHERE id = 1"
                )
            ).scalar_one()
        assert generation == 1

    def test_constrains_the_new_columns_without_a_server_default(
        self, migrated: Engine
    ) -> None:
        generation = _column(migrated, "notification_channels", "config_generation")
        assert generation["nullable"] is False
        assert generation["default"] is None
        flag = _column(
            migrated, "system_config", "notifications_generation_protection"
        )
        assert flag["nullable"] is False
        assert flag["default"] is None

    def test_indexes_the_claim_fence(self, migrated: Engine) -> None:
        indexes = {
            index["name"] for index in inspect(migrated).get_indexes(
                "notification_deliveries"
            )
        }
        assert "ix_notification_deliveries_claim_token" in indexes

    def test_sets_the_foreign_key_to_set_null(self, migrated: Engine) -> None:
        assert (_channel_fk(migrated) or "").upper() == "SET NULL"

    def test_deleting_a_channel_keeps_history_and_detaches_the_link(
        self, migrated: Engine
    ) -> None:
        # Alembic leaves enforcement off during migrations; production and the
        # test suite set the pragma per connection. Exercise ON DELETE on a
        # fresh enforcing engine (the pragma is ignored inside a transaction).
        enforcing = create_engine(normalize_database_url(_engine_url(migrated)))
        if enforcing.dialect.name == "sqlite":
            event.listen(
                enforcing,
                "connect",
                lambda conn, _rec: conn.execute("PRAGMA foreign_keys=ON"),
            )
        try:
            with enforcing.begin() as conn:
                conn.execute(
                    text("DELETE FROM notification_channels WHERE id = 1")
                )
        finally:
            enforcing.dispose()

        row = _delivery_row(migrated)
        assert row["channel_id"] is None
        assert row["status"] == "PENDING"

    def test_the_downgrade_keeps_rows_whose_channel_survives(
        self, migrated: Engine
    ) -> None:
        # Seed a pair after the upgrade so this test is independent of whatever
        # an earlier shuffled test did to the original channel.
        with migrated.begin() as conn:
            channels = _table(conn, "notification_channels")
            deliveries = _table(conn, "notification_deliveries")
            conn.execute(
                channels.insert().values(
                    id=2,
                    name="ch2",
                    target="WEBHOOK",
                    enabled=True,
                    config_json='{"url": "https://hooks.example.test/b"}',
                    events_json='["printer_offline"]',
                    printer_ids_json=None,
                    last_status=None,
                    last_error=None,
                    last_delivered_at=None,
                    consecutive_failures=0,
                    config_generation=1,
                    created_at=_WHEN,
                    updated_at=_WHEN,
                )
            )
            conn.execute(
                deliveries.insert().values(
                    id=2,
                    channel_id=2,
                    event_type="PRINTER_OFFLINE",
                    printer_id=None,
                    print_job_id=None,
                    channel_target="WEBHOOK",
                    channel_config_json='{"url": "https://hooks.example.test/b"}',
                    channel_config_generation=1,
                    context_json="{}",
                    status="SENT",
                    attempts=1,
                    last_error=None,
                    claim_token=None,
                    next_retry_at=_WHEN,
                    delivered_at=_WHEN,
                    created_at=_WHEN,
                    updated_at=_WHEN,
                )
            )

        config = _alembic_config(_engine_url(migrated))
        command.downgrade(config, BEFORE)
        try:
            with migrated.connect() as conn:
                surviving = conn.execute(
                    text("SELECT COUNT(*) FROM notification_deliveries WHERE id = 2")
                ).scalar_one()
            assert surviving == 1
            assert (_channel_fk(migrated) or "NO ACTION") in (
                None,
                "NO ACTION",
            )
            columns = {
                column["name"]
                for column in inspect(migrated).get_columns(
                    "notification_deliveries"
                )
            }
            assert "channel_target" not in columns
            assert "claim_token" not in columns
            channel_columns = {
                column["name"]
                for column in inspect(migrated).get_columns(
                    "notification_channels"
                )
            }
            assert "config_generation" not in channel_columns
        finally:
            command.upgrade(config, AFTER)
        with migrated.connect() as conn:
            reupgraded = conn.execute(
                text("SELECT COUNT(*) FROM notification_deliveries WHERE id = 2")
            ).scalar_one()
        assert reupgraded == 1


def _engine_url(engine: Engine) -> str:
    return engine.url.render_as_string(hide_password=False)
