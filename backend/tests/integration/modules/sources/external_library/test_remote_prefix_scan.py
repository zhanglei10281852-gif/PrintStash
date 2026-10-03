"""A remote subdirectory scan is prefix-scoped evidence, never a full snapshot.

Discovery, checkpoints, observations, indexing, tombstones and absence all stay
inside the selected subdirectory: its progress resumes the same prefix across
slices, failures and restarts, while siblings outside the prefix can never be
judged missing from it. These cases pin that boundary against the real scanner,
the real SQLite database and a fake remote source standing in only for egress.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.db.models import (
    ExternalLibrary,
    ExternalLibraryCheckpoint,
    ExternalLibraryScanStatus,
    ExternalLibraryTombstone,
    File,
    LibrarySourceKind,
)
from app.modules.sources import external_library
from app.modules.sources.external_library import LibrarySourceError
from app.modules.sources.library_source import SourceContent, SourceEntry, SourcePage
from tests.factories import build_file, build_model
from tests.paths import FIXTURES_DIR

SAMPLE = FIXTURES_DIR / "sample.gcode"


class _Source:
    """Scripts ``list_page`` per prefix; ``materialize`` reads the real fixture."""

    def __init__(
        self,
        pages: dict[str, list[SourcePage]] | None = None,
        *,
        fail_once_prefix: str | None = None,
    ) -> None:
        self.pages = pages or {}
        self.fail_once_prefix = fail_once_prefix
        self.prefixes: list[str] = []
        self.cursors: list[str | None] = []
        self.materialize_count = 0

    def list_page(self, prefix: str, *, cursor: str | None, limit: int) -> SourcePage:
        assert limit == 1000
        self.prefixes.append(prefix)
        self.cursors.append(cursor)
        if self.fail_once_prefix == prefix:
            self.fail_once_prefix = None
            raise RuntimeError("provider unavailable")
        queue = self.pages.get(prefix, [])
        if queue:
            return queue.pop(0)
        return SourcePage((), None, True, metadata_ops=0)

    @contextmanager
    def materialize(self, key: str, *, expected=None):
        del expected
        self.materialize_count += 1
        entry = SourceEntry(key, SAMPLE.stat().st_size)
        yield SourceContent(SAMPLE, entry)


def _remote_library(make_external_library) -> ExternalLibrary:
    return make_external_library(
        "",
        root_identity=None,
        source_kind=LibrarySourceKind.S3,
        source_prefix="models",
        watch_mode="off",
    )


def _indexed(session: Session, library: ExternalLibrary, key: str) -> File:
    model = build_model(session, name=f"model-{key}")
    return build_file(
        session,
        model,
        filename=Path(key).name,
        external=True,
        external_library_id=library.id,
        source_key=key,
        path=f"source://1/{key}",
    )


def _entry(key: str) -> SourceEntry:
    return SourceEntry(key, SAMPLE.stat().st_size)


def _checkpoints(session: Session, library: ExternalLibrary):
    return session.exec(
        select(ExternalLibraryCheckpoint).where(
            ExternalLibraryCheckpoint.library_id == library.id
        )
    ).all()


class TestRemotePrefixScope:
    def test_enumerates_only_the_requested_prefix_with_prefix_scope(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = _Source(
            {"models/sub": [SourcePage((_entry("models/sub/a.gcode"),), None, True, 1)]}
        )
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert source.prefixes == ["models/sub"]
        assert result["scope"] == "prefix"
        assert result["scan_prefix"] == "models/sub"
        assert result["complete"] is True
        assert result["added"] == 1
        checkpoint = _checkpoints(db_session, library)[0]
        assert checkpoint.scan_prefix == "models/sub"
        assert checkpoint.complete is True
        row = db_session.exec(
            select(File).where(File.external_library_id == library.id)
        ).one()
        assert row.source_key == "models/sub/a.gcode"

    def test_scan_without_subdir_reports_full_scope_at_the_source_prefix(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = _Source(
            {"models": [SourcePage((_entry("models/a.gcode"),), None, True, 1)]}
        )
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id)

        db_session.refresh(library)
        assert source.prefixes == ["models"]
        assert result["scope"] == "full"
        assert result["scan_prefix"] == "models"
        assert result["complete"] is True
        assert library.last_scan_status == ExternalLibraryScanStatus.OK

    def test_completion_removes_only_files_missing_inside_the_prefix(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        gone = _indexed(db_session, library, "models/sub/gone.gcode")
        sibling = _indexed(db_session, library, "models/other/keep.gcode")
        lookalike = _indexed(db_session, library, "models/submarine/deep.gcode")
        source = _Source(
            {
                "models/sub": [
                    SourcePage((_entry("models/sub/present.gcode"),), None, True, 1)
                ]
            }
        )
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert result["added"] == 1
        assert result["removed"] == 1
        assert db_session.get(File, gone.id).deleted_at is not None
        assert db_session.get(File, sibling.id).deleted_at is None
        assert db_session.get(File, lookalike.id).deleted_at is None

    def test_empty_prefix_completes_without_touching_a_string_prefix_sibling(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        lookalike = _indexed(db_session, library, "models/submarine/deep.gcode")
        source = _Source({"models/sub": [SourcePage((), None, True, 1)]})
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert result["removed"] == 0
        assert result["error"] is None
        assert db_session.get(File, lookalike.id).deleted_at is None

    def test_empty_epoch_over_files_in_scope_is_a_mass_removal_block(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        in_scope = _indexed(db_session, library, "models/sub/precious.gcode")
        source = _Source({"models/sub": [SourcePage((), None, True, 1)]})
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert result["error"] == "remote_mass_removal_blocked"
        assert result["removed"] == 0
        assert db_session.get(File, in_scope.id).deleted_at is None

    def test_tombstone_inside_the_prefix_still_suppresses_reimport(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        key = "models/sub/trashed.gcode"
        db_session.add(
            ExternalLibraryTombstone(
                library_id=library.id, source_key=key, reason="user_trashed"
            )
        )
        db_session.commit()
        source = _Source({"models/sub": [SourcePage((_entry(key),), None, True, 1)]})
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")

        assert result["skipped"] == 1
        assert (
            db_session.exec(
                select(File).where(File.external_library_id == library.id)
            ).all()
            == []
        )


class TestRemotePrefixResume:
    def _two_page_source(self) -> _Source:
        return _Source(
            {
                "models/sub": [
                    SourcePage(
                        (_entry("models/sub/a.gcode"),),
                        "page-2",
                        False,
                        1,
                        entry_cursors=("one",),
                    ),
                    SourcePage((_entry("models/sub/b.gcode"),), None, True, 0),
                ]
            }
        )

    def test_marks_an_incomplete_prefix_slice_partial(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = self._two_page_source()
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        first = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert first["complete"] is False
        assert first["scope"] == "prefix"
        assert _checkpoints(db_session, library)[0].complete is False
        db_session.refresh(library)
        assert library.last_scan_status == ExternalLibraryScanStatus.PARTIAL

    def test_repeating_the_prefix_continues_its_epoch_from_the_cursor(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = self._two_page_source()
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        external_library.scan_remote_library(library.id, relative_path="sub")
        db_session.expire_all()
        epoch = _checkpoints(db_session, library)[0].epoch
        second = external_library.scan_remote_library(library.id, relative_path="sub")

        db_session.expire_all()
        assert source.prefixes == ["models/sub", "models/sub"]
        assert source.cursors == [None, "page-2"]
        assert second["complete"] is True
        checkpoints = _checkpoints(db_session, library)
        assert len(checkpoints) == 1
        assert checkpoints[0].epoch == epoch
        assert checkpoints[0].complete is True

    def test_a_pathless_continuation_finishes_the_interrupted_prefix(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = self._two_page_source()
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        external_library.scan_remote_library(library.id, relative_path="sub")
        resumed = external_library.scan_remote_library(library.id)

        assert source.prefixes == ["models/sub", "models/sub"]
        assert resumed["scope"] == "prefix"
        assert resumed["scan_prefix"] == "models/sub"
        assert resumed["complete"] is True

    def test_distinct_subdirectories_keep_independent_epochs(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = _Source(
            {
                "models/a": [
                    SourcePage((_entry("models/a/a.gcode"),), "a-page-2", False, 1)
                ],
                "models/b": [SourcePage((_entry("models/b/b.gcode"),), None, True, 1)],
            }
        )
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        incomplete = external_library.scan_remote_library(library.id, relative_path="a")
        complete = external_library.scan_remote_library(library.id, relative_path="b")

        assert incomplete["complete"] is False
        assert complete["complete"] is True
        rows = {row.scan_prefix: row for row in _checkpoints(db_session, library)}
        assert set(rows) == {"models/a", "models/b"}
        assert rows["models/a"].complete is False
        assert rows["models/a"].cursor == "a-page-2"
        assert rows["models/b"].complete is True
        assert rows["models/a"].epoch != rows["models/b"].epoch

    def test_provider_failure_parks_backoff_on_the_prefix_checkpoint(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = _Source(fail_once_prefix="models/sub")
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        result = external_library.scan_remote_library(library.id, relative_path="sub")
        held = external_library.scan_remote_library(library.id, relative_path="sub")

        checkpoint = _checkpoints(db_session, library)[0]
        assert result["error"] == "provider unavailable"
        assert checkpoint.scan_prefix == "models/sub"
        assert checkpoint.backoff_until is not None
        assert "backoff_until" in held
        assert source.prefixes == ["models/sub"]

    def test_a_pathless_scan_falls_back_to_full_scope_when_prefix_is_backed_off(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        source = _Source(
            {
                "models/sub": [],
                "models": [SourcePage((_entry("models/root.gcode"),), None, True, 1)],
            },
            fail_once_prefix="models/sub",
        )
        monkeypatch.setattr(external_library, "source_for_library", lambda _lib: source)

        external_library.scan_remote_library(library.id, relative_path="sub")
        result = external_library.scan_remote_library(library.id)

        assert source.prefixes == ["models/sub", "models"]
        assert result["scope"] == "full"
        assert result["scan_prefix"] == "models"
        assert result["complete"] is True

    def test_engine_rejects_a_relative_path_escaping_source_prefix(
        self,
        db_session: Session,
        monkeypatch: pytest.MonkeyPatch,
        make_external_library,
    ) -> None:
        library = _remote_library(make_external_library)
        monkeypatch.setattr(
            external_library,
            "source_for_library",
            lambda _lib: _Source(),
        )

        with pytest.raises(LibrarySourceError, match="path_outside_library_root"):
            external_library.scan_remote_library(
                library.id, relative_path="../elsewhere"
            )
