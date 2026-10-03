"""Pure progress-coalescing behaviour for external-library scans."""

import pytest

import app.modules.sources.root_binding as source_root_binding
from app.core.config import _overlay
from app.modules.sources.external_library import (
    LibrarySourceError,
    _ScanProgressCoalescer,
    normalize_remote_scan_path,
)


class TestScanProgressCoalescer:
    def test_empty_scan_flushes_only_after_work_appears(self) -> None:
        progress = _ScanProgressCoalescer(total=0, last_flush_at=10.0)

        assert progress.should_flush(0, now=10.0) is False
        assert progress.should_flush(1, now=10.0) is True

    def test_percentage_step_triggers_a_bounded_progress_write(self) -> None:
        progress = _ScanProgressCoalescer(total=100, last_flush_at=10.0)

        assert progress.should_flush(0, now=10.0) is False
        assert progress.should_flush(1, now=10.0) is True
        assert progress.last_percent == 1

    def test_elapsed_interval_triggers_a_bounded_progress_write(self) -> None:
        progress = _ScanProgressCoalescer(total=100, last_flush_at=10.0)

        assert progress.should_flush(1, now=11.0) is True
        assert progress.last_flush_at == 11.0

    def test_final_item_always_flushes_progress(self) -> None:
        progress = _ScanProgressCoalescer(total=3, last_flush_at=10.0)

        assert progress.should_flush(3, now=10.0) is True
        assert progress.last_percent == 100


class TestNormalizeRemoteScanPath:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, None),
            ("", None),
            ("   ", None),
            ("/", None),
            ("functional", "functional"),
            ("functional/", "functional"),
            ("/functional/", "functional"),
            ("a/b/", "a/b"),
            ("nested/functional", "nested/functional"),
        ],
    )
    def test_canonicalizes_subdirectories_under_the_source_prefix(
        self, value: str | None, expected: str | None
    ) -> None:
        assert normalize_remote_scan_path(value) == expected

    @pytest.mark.parametrize("value", ["../outside", "a/../../etc", ".."])
    def test_rejects_a_path_escaping_the_source_prefix(self, value: str) -> None:
        with pytest.raises(LibrarySourceError, match="path_outside_library_root"):
            normalize_remote_scan_path(value)

    @pytest.mark.parametrize("value", ["a//b", "a/./b", "a\\b", "a/b\\c"])
    def test_rejects_a_shape_that_is_not_a_remote_directory(
        self, value: str
    ) -> None:
        with pytest.raises(LibrarySourceError, match="remote_scan_path_invalid"):
            normalize_remote_scan_path(value)


class TestInstallationIdentity:
    def test_invalid_identity_is_never_used_for_root_binding(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(_overlay, "storage_identity", "not-a-valid-identity")

        assert source_root_binding._installation_identity() == ""
