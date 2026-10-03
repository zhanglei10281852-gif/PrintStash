"""Read-only, bounded content seam for mounted and remote library sources."""

from __future__ import annotations

import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Iterator

from sqlmodel import Session, select

from app.db.models import (
    ExternalLibrary,
    File,
    LibrarySourceKind,
    StorageConnection,
    StorageConnectionPurpose,
)
from app.db.session import get_session_factory
from app.modules.storage.remote_deadline import paced_sleep, remote_budget
from app.modules.storage.remote_io import RemoteIO
from app.modules.storage.remote_io_adapters import remote_io_for
from app.modules.storage.storage_backend.contracts import StorageConfigurationError
from app.modules.storage.storage_connections import (
    StorageConnectionConfigError,
    load_connection_config,
)
from app.modules.storage.storage_ownership import provider_ref_for_backend
from app.modules.storage.storage_providers import resolve_transport

from .contracts import (
    LibrarySource,
    LibrarySourceError,
    SourceContent,
    SourceEntry,
    SourcePage,
)

# Failures at the connection seam (row missing/disabled, purpose withdrawn,
# generation rotated) that a scan treats as binding failures rather than
# transient provider failures.
BINDING_FAILURE_CODES = frozenset(
    {
        "storage_connection_disabled",
        "storage_connection_missing",
        "storage_connection_not_library",
        "storage_connection_kind_invalid",
        "storage_connection_invalid",
    }
)

# Provider status text that proves the configured credentials were rejected.
# Matched across the whole exception chain because adapters deliberately
# surface only a generic message and keep the response in the chained cause.
_CREDENTIAL_REJECTION_MARKERS = re.compile(
    r"(?:"
    r"(?<![0-9])40[13](?![0-9])"  # an HTTP 401/403 status standing alone
    r"|forbidden"
    r"|unauthorized"
    r"|access denied"
    r"|permission denied"
    r"|authentication failed"
    r")",
    re.IGNORECASE,
)


class ConnectionBindingError(LibrarySourceError):
    """A scan's bound connection generation no longer matches the database."""


@dataclass(frozen=True)
class RemoteScanBinding:
    """The connection generation one scan is allowed to observe through."""

    connection_id: int
    revision: str
    target_ref: str


def credentials_rejected_reason(exc: BaseException) -> str | None:
    """Classify a provider failure as a credential rejection, if proven so.

    Walks the cause/context chain looking for an HTTP/auth status marker. The
    generic adapter messages never match, so a timeout or reset keeps its
    ordinary failure handling while a 401/403 becomes an explicit, recoverable
    scan state. Provider text is never returned, so response details cannot leak
    into the recorded reason.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if _CREDENTIAL_REJECTION_MARKERS.search(str(current)):
            return "remote_scan_credentials_rejected"
        current = current.__cause__ or current.__context__
    return None


def _require_observation(expected: SourceEntry | None, actual: SourceEntry) -> None:
    if expected is not None and (
        expected.key != actual.key
        or expected.size != actual.size
        or (
            expected.modified_at is not None
            and timestamp(expected.modified_at) != timestamp(actual.modified_at)
        )
        or (expected.etag is not None and expected.etag != actual.etag)
        or (
            expected.version_id is not None and expected.version_id != actual.version_id
        )
    ):
        raise LibrarySourceError("library_source_changed")


def _safe_key(value: str) -> str:
    stripped = value.strip("/")
    if not stripped:
        raise LibrarySourceError("library_source_key_invalid")
    key = PurePosixPath(stripped).as_posix()
    if not key or any(part in {"", ".", ".."} for part in PurePosixPath(key).parts):
        raise LibrarySourceError("library_source_key_invalid")
    return key


class RemoteLibrarySource:
    def __init__(
        self,
        backend: RemoteIO,
        *,
        max_metadata_ops_per_second: int | None = None,
        max_bytes_per_second: int | None = None,
    ) -> None:
        self.backend = backend
        self.max_metadata_ops_per_second = max_metadata_ops_per_second
        self.max_bytes_per_second = max_bytes_per_second

    def _pace_metadata(self, started: float, operations: int) -> None:
        if not self.max_metadata_ops_per_second:
            return
        target_elapsed = operations / self.max_metadata_ops_per_second
        remaining = target_elapsed - (time.monotonic() - started)
        if remaining > 0:
            paced_sleep(remaining)

    def probe(self) -> int:
        with remote_budget(deadline=time.monotonic() + 30):
            with self.backend.iter_directory("") as entries:
                return int(next(entries, None) is not None)

    def list_page(self, prefix: str, *, cursor: str | None, limit: int) -> SourcePage:
        from app.modules.sources.remote_discovery import inventory_page

        started = time.monotonic()
        with remote_budget(deadline=started + 900):
            return inventory_page(
                self.backend,
                prefix,
                cursor=cursor,
                limit=limit,
                pace=lambda operations: self._pace_metadata(started, operations),
            )

    @contextmanager
    def materialize(
        self, key: str, *, expected: SourceEntry | None = None
    ) -> Iterator[SourceContent]:
        with remote_budget(deadline=time.monotonic() + 900):
            safe_key = _safe_key(key)
            provider_key = self.backend.source_key(safe_key)
            before = self.backend.object_info(provider_key)
            if before is None:
                raise LibrarySourceError("library_source_missing")
            observation = SourceEntry(
                safe_key,
                before.size,
                before.modified_at,
                before.etag,
                before.version_id,
            )
            _require_observation(expected, observation)
            fd, raw = tempfile.mkstemp(suffix=Path(safe_key).suffix)
            path = Path(raw)
            written = 0
            started = time.monotonic()
            try:
                with (
                    open(fd, "wb", closefd=True) as output,
                    self.backend.open_reader(provider_key, expected=before) as reader,
                ):
                    while chunk := reader.read(
                        min(1024 * 1024, before.size - written + 1)
                    ):
                        written += len(chunk)
                        if written > before.size:
                            raise LibrarySourceError("library_source_size_mismatch")
                        output.write(chunk)
                        if self.max_bytes_per_second:
                            target_elapsed = written / self.max_bytes_per_second
                            remaining = target_elapsed - (time.monotonic() - started)
                            if remaining > 0:
                                paced_sleep(remaining)
                if written != before.size:
                    raise LibrarySourceError("library_source_size_mismatch")
                after = self.backend.object_info(provider_key)
                if (
                    after is None
                    or after.size != before.size
                    or after.etag != before.etag
                    or after.version_id != before.version_id
                    or timestamp(after.modified_at) != timestamp(before.modified_at)
                ):
                    raise LibrarySourceError("library_source_changed")
                yield SourceContent(path, observation)
            finally:
                path.unlink(missing_ok=True)


def source_from_connection(
    connection: StorageConnection, *, scan_limits: bool = False
) -> LibrarySource:
    if not connection.enabled:
        raise LibrarySourceError("storage_connection_disabled")
    if not connection.purpose.allows(StorageConnectionPurpose.LIBRARY):
        raise LibrarySourceError("storage_connection_not_library")
    if connection.kind == LibrarySourceKind.MOUNTED:
        raise LibrarySourceError("storage_connection_kind_invalid")
    try:
        parsed = load_connection_config(connection)
        return RemoteLibrarySource(
            remote_io_for(resolve_transport(parsed)),
            max_metadata_ops_per_second=4 if scan_limits else None,
            max_bytes_per_second=8 * 1024 * 1024 if scan_limits else None,
        )
    except (StorageConnectionConfigError, StorageConfigurationError) as exc:
        raise LibrarySourceError("storage_connection_invalid") from exc


def source_for_library(library: ExternalLibrary) -> LibrarySource:
    if (
        library.source_kind == LibrarySourceKind.MOUNTED
        or library.connection_id is None
    ):
        raise LibrarySourceError("library_source_is_mounted")
    with get_session_factory().scoped_session() as session:
        connection = session.get(StorageConnection, library.connection_id)
        if connection is None:
            raise LibrarySourceError("storage_connection_missing")
        # Materialize the decrypted values before the session closes.
        connection.config_json = str(connection.config_json)
        connection.secret_json = str(connection.secret_json)
        return source_from_connection(connection, scan_limits=True)


def target_ref_for_source(source: LibrarySource) -> str:
    """Credential-free destination identity behind a resolved source."""
    backend = getattr(source, "backend", None)
    if backend is None:
        raise LibrarySourceError("storage_connection_invalid")
    return provider_ref_for_backend(backend)


def binding_for(
    session: Session, library: ExternalLibrary, source: LibrarySource
) -> RemoteScanBinding | None:
    """Bind the resolved source to the connection row the scan transaction sees.

    Returns None only when the library has no persisted connection and the
    source was supplied through a replaced ``source_for_library`` seam (the
    mounted-source guard and test doubles); production remote libraries always
    have a connection and therefore a binding.
    """
    if library.connection_id is None:
        return None
    connection = session.get(StorageConnection, library.connection_id)
    if connection is None:
        raise ConnectionBindingError("storage_connection_missing")
    if not connection.enabled:
        raise ConnectionBindingError("storage_connection_disabled")
    if not connection.purpose.allows(StorageConnectionPurpose.LIBRARY):
        raise ConnectionBindingError("storage_connection_not_library")
    if connection.id is None:
        raise ConnectionBindingError("storage_connection_missing")
    return RemoteScanBinding(
        connection_id=connection.id,
        revision=connection.revision,
        target_ref=target_ref_for_source(source),
    )


def assert_binding_current(
    session: Session,
    binding: RemoteScanBinding,
    *,
    verify_target: bool = False,
) -> None:
    """Fail closed unless *binding* still matches the current connection row.

    The cheap revision/enabled/purpose check runs at every page and observation
    commit. ``verify_target`` additionally rebuilds the resolved destination and
    compares its credential-free identity, catching an out-of-band configuration
    change that did not bump the revision; it runs at page and convergence
    boundaries.
    """
    row = session.execute(
        select(
            StorageConnection.enabled,
            StorageConnection.revision,
            StorageConnection.purpose,
        ).where(StorageConnection.id == binding.connection_id)
    ).first()
    if row is None:
        raise ConnectionBindingError("storage_connection_missing")
    enabled, revision, purpose = row
    if not enabled:
        raise ConnectionBindingError("storage_connection_disabled")
    if not purpose.allows(StorageConnectionPurpose.LIBRARY):
        raise ConnectionBindingError("storage_connection_not_library")
    if revision != binding.revision:
        raise ConnectionBindingError("storage_connection_revision_changed")
    if verify_target:
        # Resolve through a transient profile so this read never dirties (and
        # re-encrypts) the persisted row on every boundary check.
        profile_row = session.execute(
            select(
                StorageConnection.kind,
                StorageConnection.config_json,
                StorageConnection.secret_json,
            ).where(StorageConnection.id == binding.connection_id)
        ).first()
        if profile_row is None:
            raise ConnectionBindingError("storage_connection_missing")
        profile = StorageConnection(
            id=binding.connection_id,
            name="",
            kind=profile_row.kind,
            config_json=str(profile_row.config_json),
            secret_json=str(profile_row.secret_json),
            enabled=True,
        )
        source = source_from_connection(profile, scan_limits=True)
        if target_ref_for_source(source) != binding.target_ref:
            raise ConnectionBindingError("storage_connection_target_changed")


def source_for_file(file: File) -> tuple[LibrarySource, str]:
    if file.external_library_id is None or not file.source_key:
        raise LibrarySourceError("remote_file_source_missing")
    with get_session_factory().scoped_session() as session:
        library = session.get(ExternalLibrary, file.external_library_id)
        if library is None:
            raise LibrarySourceError("remote_file_source_missing")
        connection = (
            session.get(StorageConnection, library.connection_id)
            if library.connection_id is not None
            else None
        )
        if connection is None:
            raise LibrarySourceError("storage_connection_missing")
        connection.config_json = str(connection.config_json)
        connection.secret_json = str(connection.secret_json)
        source = source_from_connection(connection)
        if (
            file.source_target_ref is not None
            and target_ref_for_source(source) != file.source_target_ref
        ):
            # The current connection serves a different destination than the
            # one the scan confirmed for this row. Refuse before any byte is
            # fetched; the row stays recoverable if the target is restored.
            raise LibrarySourceError("remote_file_source_target_changed")
        return source, file.source_key


def timestamp(value: datetime | None) -> float | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()
