"""Remote scans bind one storage connection generation and fail closed when it changes.

A paginated scan must never mix evidence from two connection generations: files
listed through the connection captured at scan start, deletions concluded after
an administrator disabled or rotated it, and reads later served from a different
target. These cases drive the binding, checkpoint and read seams with egress
stood in for a fake S3-compatible backend.
"""

from __future__ import annotations

import hashlib
import io
import json
import secrets
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from sqlmodel import select

from app.db.models import (
    ExternalLibrary,
    ExternalLibraryCheckpoint,
    ExternalLibraryObservation,
    ExternalLibraryScanStatus,
    File,
    LibrarySourceKind,
    StorageConnection,
    StorageConnectionPurpose,
)
from app.db.session import get_session_factory
from app.modules.sources import external_library, library_source
from app.modules.sources.library_source import LibrarySourceError
from app.modules.storage.remote_io import RemoteEntry
from app.modules.storage.storage_backend.contracts import (
    StorageConfigurationError,
    StorageObjectInfo,
)
from app.modules.storage.storage_connections import parse_connection_config
from app.modules.storage.storage_ownership import provider_ref_for_backend
from app.modules.storage.storage_providers import resolve_transport
from tests.factories import build_file, build_model, build_storage_connection
from tests.paths import FIXTURES_DIR

_ENDPOINT = "https://minio.example.test"
_ROOT = "library-root"
_CONFIG_A = {
    "provider": "s3_self_hosted",
    "bucket": "print-library-a",
    "endpoint_url": _ENDPOINT,
    "region": "us-east-1",
    "addressing_style": "path",
    "root": _ROOT,
}
_CONFIG_B = {**_CONFIG_A, "bucket": "print-library-b"}
_SECRETS = {"access_key": "ACCESS-KEY", "secret_key": "SECRET-KEY"}


def _gcode(marker: str) -> bytes:
    return FIXTURES_DIR.joinpath("sample.gcode").read_bytes() + (
        f"\n; binding-marker {marker}\n".encode()
    )


# All fixtures share a length, so this budget admits exactly one file per slice.
_ONE_FILE_BUDGET = len(_gcode("a")) * 2 - 1


@dataclass
class _RemoteState:
    """Shared, target-keyed object stand-in for the remote S3 service."""

    objects: dict[str, dict[str, bytes]] = field(default_factory=dict)
    list_failures_left: int = 0
    reject_credentials: bool = False
    flip_revision_on_materialize: int | None = None
    list_calls: int = 0
    materialize_calls: int = 0

    def revision_connection(self, connection_id: int) -> None:
        with get_session_factory().scoped_session() as session:
            connection = session.get(StorageConnection, connection_id)
            assert connection is not None
            connection.revision = secrets.token_hex(16)
            session.add(connection)
            session.commit()


class _FakeRemote:
    def __init__(self, spec, state: _RemoteState, connection_id: int) -> None:
        self._spec = spec
        self.backend_name = spec.provider
        self.provider_id = spec.provider
        self.transport = spec.kind.value
        self.namespace = spec.namespace
        self._state = state
        self._connection_id = connection_id

    @property
    def _target_ref(self) -> str:
        return provider_ref_for_backend(self)

    @property
    def _objects(self) -> dict[str, bytes]:
        return self._state.objects.setdefault(self._target_ref, {})

    def source_key(self, relative: str) -> str:
        return f"{self.namespace}/{relative.lstrip('/')}"

    def _relative(self, key: str) -> str:
        return key.removeprefix(f"{self.namespace}/")

    def object_info(self, key: str):
        relative = self._relative(key)
        data = self._objects.get(relative)
        if data is None:
            return None
        return StorageObjectInfo(
            size=len(data),
            etag=hashlib.sha256(data).hexdigest(),
            version_id="v1",
        )

    @contextmanager
    def open_reader(self, key: str, *, expected=None) -> BinaryIO:
        self._state.materialize_calls += 1
        if (
            self._state.flip_revision_on_materialize is not None
            and self._state.materialize_calls
            >= self._state.flip_revision_on_materialize
        ):
            self._state.revision_connection(self._connection_id)
        data = self._objects[self._relative(key)]
        yield io.BufferedReader(io.BytesIO(data))

    @contextmanager
    def iter_directory(self, relative: str):
        self._state.list_calls += 1
        if self._state.reject_credentials:
            cause = PermissionError("403 Forbidden")
            raise StorageConfigurationError("remote_storage_list_failed") from cause
        if self._state.list_failures_left:
            self._state.list_failures_left -= 1
            raise RuntimeError("transient reset by peer")

        def observations():
            prefix = relative.strip("/")
            depth = 0 if not prefix else len(prefix.split("/"))
            seen_dirs: set[str] = set()
            for key, data in sorted(self._objects.items()):
                parts = key.split("/")
                if prefix and not key.startswith(prefix + "/"):
                    continue
                if len(parts) == depth + 1:
                    yield RemoteEntry(
                        key=key,
                        size=len(data),
                        is_dir=False,
                        etag=hashlib.sha256(data).hexdigest(),
                        version_id="v1",
                    )
                elif len(parts) > depth + 1:
                    directory = "/".join(parts[: depth + 1])
                    if directory not in seen_dirs:
                        seen_dirs.add(directory)
                        yield RemoteEntry(directory, 0, True)

        yield observations()


@pytest.fixture
def bound_scan(db_session, monkeypatch):
    state = _RemoteState()
    connection = build_storage_connection(
        db_session,
        purpose=StorageConnectionPurpose.LIBRARY,
        config_json=json.dumps(_CONFIG_A),
        secret_json=json.dumps(_SECRETS),
    )
    library = ExternalLibrary(
        name=f"bound-{secrets.token_hex(4)}",
        root_path="",
        source_kind=LibrarySourceKind.S3,
        connection_id=connection.id,
        source_prefix="models",
        watch_mode="off",
    )
    db_session.add(library)
    db_session.commit()
    db_session.refresh(library)
    db_session.refresh(connection)
    connection_id = connection.id

    def remote_io_for(spec, *, operator=None):
        return _FakeRemote(spec, state, connection_id)

    monkeypatch.setattr(library_source, "remote_io_for", remote_io_for)

    def ref_for(config: dict) -> str:
        spec = resolve_transport(
            parse_connection_config(LibrarySourceKind.S3, config, _SECRETS)
        )
        return provider_ref_for_backend(_FakeRemote(spec, state, connection_id))

    target_a = ref_for(_CONFIG_A)
    target_b = ref_for(_CONFIG_B)
    state.objects[target_a] = {
        "models/a.gcode": _gcode("a"),
        "models/b.gcode": _gcode("b"),
    }
    state.objects[target_b] = {}
    initial_revision = connection.revision
    return SimpleNamespace(
        session=db_session,
        state=state,
        connection=connection,
        library=library,
        revision=initial_revision,
        target_a=target_a,
        target_b=target_b,
    )


def _checkpoint(session, library: ExternalLibrary) -> ExternalLibraryCheckpoint:
    return session.exec(
        select(ExternalLibraryCheckpoint).where(
            ExternalLibraryCheckpoint.library_id == library.id
        )
    ).one()


def _clear_backoff(session, library: ExternalLibrary) -> None:
    checkpoint = _checkpoint(session, library)
    checkpoint.backoff_until = None
    session.add(checkpoint)
    session.commit()


def _live_files(session, library: ExternalLibrary) -> list[File]:
    session.expire_all()
    return list(
        session.exec(
            select(File).where(
                File.external_library_id == library.id,
                File.deleted_at.is_(None),
            )
        ).all()
    )


def _indexed_file(session, library: ExternalLibrary, key: str, target_ref: str) -> File:
    model = build_model(session, name=f"model-{Path(key).stem}-{secrets.token_hex(2)}")
    return build_file(
        session,
        model,
        filename=Path(key).name,
        external=True,
        external_library_id=library.id,
        source_key=key,
        path=f"source://{library.connection_id}/{key}",
        source_target_ref=target_ref,
    )


def _retarget_connection(
    session, connection: StorageConnection, config: dict
) -> str:
    connection.config_json = json.dumps(config)
    connection.secret_json = json.dumps(_SECRETS)
    connection.revision = secrets.token_hex(16)
    session.add(connection)
    session.commit()
    session.refresh(connection)
    return connection.revision


def _flip_after_resumed_page(monkeypatch, ctx) -> None:
    """Rotate the connection while a resumed (cursored) page is in flight."""
    original = library_source.RemoteLibrarySource.list_page

    def rotated(self, prefix, *, cursor, limit):
        page = original(self, prefix, cursor=cursor, limit=limit)
        if cursor is not None:
            ctx.state.revision_connection(ctx.connection.id)
        return page

    monkeypatch.setattr(library_source.RemoteLibrarySource, "list_page", rotated)


class TestScanConnectionBinding:
    def test_stamps_every_slice_with_connection_identity_generation_and_target(
        self, bound_scan, monkeypatch
    ):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)

        first = external_library.scan_remote_library(ctx.library.id)
        second = external_library.scan_remote_library(ctx.library.id)

        assert first["complete"] is False, first
        assert second["complete"] is True, second
        assert second["removed"] == 0
        checkpoint = _checkpoint(ctx.session, ctx.library)
        assert checkpoint.connection_id == ctx.connection.id
        assert checkpoint.connection_revision == ctx.revision
        assert checkpoint.connection_target_ref == ctx.target_a
        rows = _live_files(ctx.session, ctx.library)
        assert {row.source_key for row in rows} == {
            "models/a.gcode",
            "models/b.gcode",
        }
        assert {row.source_target_ref for row in rows} == {ctx.target_a}

    def test_same_target_credential_rotation_rebinds_and_resumes_the_epoch(
        self, bound_scan, monkeypatch
    ):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)
        first = external_library.scan_remote_library(ctx.library.id)
        assert first["complete"] is False
        epoch_before = _checkpoint(ctx.session, ctx.library).epoch

        rotated = secrets.token_hex(16)
        ctx.connection.revision = rotated
        ctx.session.add(ctx.connection)
        ctx.session.commit()

        second = external_library.scan_remote_library(ctx.library.id)

        assert second["error"] is None, second
        assert second["complete"] is True
        checkpoint = _checkpoint(ctx.session, ctx.library)
        assert checkpoint.epoch == epoch_before
        assert checkpoint.connection_revision == rotated
        assert checkpoint.connection_target_ref == ctx.target_a

    def test_target_generation_change_rebuilds_the_epoch_without_index_loss(
        self, bound_scan, monkeypatch
    ):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)
        first = external_library.scan_remote_library(ctx.library.id)
        assert first["complete"] is False
        epoch_before = _checkpoint(ctx.session, ctx.library).epoch
        ctx.state.objects[ctx.target_b] = {"models/c.gcode": _gcode("c")}
        _retarget_connection(ctx.session, ctx.connection, _CONFIG_B)

        second = external_library.scan_remote_library(ctx.library.id)

        assert second["complete"] is True, second
        assert second["removed"] == 0
        checkpoint = _checkpoint(ctx.session, ctx.library)
        assert checkpoint.epoch != epoch_before
        assert checkpoint.connection_target_ref == ctx.target_b
        rows = _live_files(ctx.session, ctx.library)
        assert {row.source_key for row in rows} == {
            "models/a.gcode",
            "models/c.gcode",
        }
        observations = ctx.session.exec(
            select(ExternalLibraryObservation).where(
                ExternalLibraryObservation.checkpoint_id == checkpoint.id
            )
        ).all()
        observed_rows = [
            ctx.session.get(File, observation.file_id) for observation in observations
        ]
        assert {row.source_key for row in observed_rows} == {"models/c.gcode"}

    def test_restarted_worker_resumes_a_bound_checkpoint(self, bound_scan, monkeypatch):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)
        external_library.scan_remote_library(ctx.library.id)
        epoch_before = _checkpoint(ctx.session, ctx.library).epoch
        ctx.session.expire_all()

        resumed = external_library.scan_remote_library(ctx.library.id)

        assert resumed["complete"] is True, resumed
        assert _checkpoint(ctx.session, ctx.library).epoch == epoch_before

    def test_failed_page_retries_within_the_same_epoch(self, bound_scan):
        ctx = bound_scan
        ctx.state.list_failures_left = 1

        failed = external_library.scan_remote_library(ctx.library.id)
        assert failed["error"] == "transient reset by peer"
        epoch_after_failure = _checkpoint(ctx.session, ctx.library).epoch
        _clear_backoff(ctx.session, ctx.library)

        completed = external_library.scan_remote_library(ctx.library.id)

        assert completed["complete"] is True, completed
        assert _checkpoint(ctx.session, ctx.library).epoch == epoch_after_failure
        assert len(_live_files(ctx.session, ctx.library)) == 2


class TestScanConnectionGuards:
    def test_disabled_connection_between_pages_stops_without_publishing(
        self, bound_scan, monkeypatch
    ):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)
        external_library.scan_remote_library(ctx.library.id)

        ctx.connection.enabled = False
        ctx.session.add(ctx.connection)
        ctx.session.commit()

        result = external_library.scan_remote_library(ctx.library.id)
        ctx.session.refresh(ctx.library)

        assert result["error"] == "storage_connection_disabled"
        assert ctx.library.last_scan_status == ExternalLibraryScanStatus.ERROR
        assert _checkpoint(ctx.session, ctx.library).backoff_until is not None
        assert {row.source_key for row in _live_files(ctx.session, ctx.library)} == {
            "models/a.gcode"
        }
        assert _checkpoint(ctx.session, ctx.library).complete is False

    def test_revision_change_during_a_resumed_page_keeps_its_checkpoint(
        self, bound_scan, monkeypatch
    ):
        ctx = bound_scan
        monkeypatch.setattr(external_library, "_REMOTE_SLICE_MAX_BYTES", _ONE_FILE_BUDGET)
        external_library.scan_remote_library(ctx.library.id)
        epoch_before = _checkpoint(ctx.session, ctx.library).epoch
        _flip_after_resumed_page(monkeypatch, ctx)

        result = external_library.scan_remote_library(ctx.library.id)
        ctx.session.refresh(ctx.library)

        assert result["error"] == "storage_connection_revision_changed"
        assert ctx.library.last_scan_status == ExternalLibraryScanStatus.PARTIAL
        checkpoint = _checkpoint(ctx.session, ctx.library)
        assert checkpoint.backoff_until is None
        assert checkpoint.complete is False
        assert checkpoint.epoch == epoch_before
        assert checkpoint.connection_revision == ctx.revision
        assert {row.source_key for row in _live_files(ctx.session, ctx.library)} == {
            "models/a.gcode"
        }

    def test_revision_change_before_second_observation_holds_the_page_boundary(
        self, bound_scan
    ):
        ctx = bound_scan
        ctx.state.flip_revision_on_materialize = 2

        result = external_library.scan_remote_library(ctx.library.id)
        ctx.session.refresh(ctx.library)

        assert result["error"] == "storage_connection_revision_changed"
        assert ctx.library.last_scan_status == ExternalLibraryScanStatus.PARTIAL
        assert {row.source_key for row in _live_files(ctx.session, ctx.library)} == {
            "models/a.gcode"
        }
        assert _checkpoint(ctx.session, ctx.library).complete is False

    def test_stale_binding_never_converges_into_removals(self, bound_scan, monkeypatch):
        ctx = bound_scan
        retained = _indexed_file(ctx.session, ctx.library, "models/old.gcode", ctx.target_a)
        ctx.state.flip_revision_on_materialize = 1
        monkeypatch.setattr(external_library, "_REMOTE_PAGE_LIMIT", 1000)

        result = external_library.scan_remote_library(ctx.library.id)
        ctx.session.refresh(retained)

        assert result["error"] == "storage_connection_revision_changed"
        assert result["removed"] == 0
        assert retained.deleted_at is None
        assert _checkpoint(ctx.session, ctx.library).complete is False

    def test_rejected_credentials_preserve_the_checkpoint_without_removals(
        self, bound_scan
    ):
        ctx = bound_scan
        ctx.state.reject_credentials = True

        result = external_library.scan_remote_library(ctx.library.id)
        ctx.session.refresh(ctx.library)

        assert result["error"] == "remote_scan_credentials_rejected"
        assert ctx.library.last_scan_status == ExternalLibraryScanStatus.ERROR
        assert _checkpoint(ctx.session, ctx.library).backoff_until is not None
        assert _live_files(ctx.session, ctx.library) == []

    def test_target_rebuild_check_detects_out_of_band_config_change(self, bound_scan):
        ctx = bound_scan
        binding = library_source.RemoteScanBinding(
            connection_id=ctx.connection.id,
            revision=ctx.revision,
            target_ref=ctx.target_a,
        )
        _retarget_connection(ctx.session, ctx.connection, _CONFIG_B)
        ctx.connection.revision = ctx.revision
        ctx.session.add(ctx.connection)
        ctx.session.commit()

        with pytest.raises(
            library_source.ConnectionBindingError,
            match="storage_connection_target_changed",
        ):
            library_source.assert_binding_current(
                ctx.session, binding, verify_target=True
            )


class TestRemoteReadConsistency:
    def test_reads_match_the_target_confirmed_by_the_scan(self, bound_scan):
        ctx = bound_scan
        completed = external_library.scan_remote_library(ctx.library.id)
        assert completed.get("error") is None, completed
        assert completed["complete"] is True, completed
        row = next(
            row
            for row in _live_files(ctx.session, ctx.library)
            if row.source_key == "models/a.gcode"
        )

        source, key = library_source.source_for_file(row)
        with source.materialize(
            key, expected=library_source.SourceEntry(key, row.size_bytes)
        ) as content:
            assert hashlib.sha256(content.path.read_bytes()).hexdigest() == row.sha256

    def test_reads_from_a_foreign_target_fail_closed(self, bound_scan):
        ctx = bound_scan
        external_library.scan_remote_library(ctx.library.id)
        row = next(
            row
            for row in _live_files(ctx.session, ctx.library)
            if row.source_key == "models/a.gcode"
        )
        _retarget_connection(ctx.session, ctx.connection, _CONFIG_B)

        with pytest.raises(
            LibrarySourceError, match="remote_file_source_target_changed"
        ):
            library_source.source_for_file(row)

    def test_reads_refuse_a_disabled_connection(self, bound_scan):
        ctx = bound_scan
        external_library.scan_remote_library(ctx.library.id)
        row = next(
            row
            for row in _live_files(ctx.session, ctx.library)
            if row.source_key == "models/a.gcode"
        )
        ctx.connection.enabled = False
        ctx.session.add(ctx.connection)
        ctx.session.commit()

        with pytest.raises(LibrarySourceError, match="storage_connection_disabled"):
            library_source.source_for_file(row)

    def test_legacy_rows_without_target_ref_stay_readable(self, bound_scan):
        ctx = bound_scan
        external_library.scan_remote_library(ctx.library.id)
        row = next(
            row
            for row in _live_files(ctx.session, ctx.library)
            if row.source_key == "models/a.gcode"
        )
        row.source_target_ref = None
        ctx.session.add(row)
        ctx.session.commit()

        source, key = library_source.source_for_file(row)
        with source.materialize(key, expected=None) as content:
            assert content.path.read_bytes() == _gcode("a")
