"""What a share token is worth, decided in one place.

`resolve_share_access` is the gate every public handler passes through, and its
job is to collapse *every* token failure into the same 404: a token that never
existed, one that was revoked, and one that expired all look identical from
outside. Anything that distinguishes them turns the public surface into an
oracle for what the vault contains.

The other half is identity. A model share (no pins) follows the live model, as
it always has. A revision share names exact Artifacts via ShareLinkPin rows:
ids are validated against *that model's live* files at creation, and from then
on the pin is the identity — new uploads get new rows, trashed pinned files
stay served, and a missing row is a stable "unavailable" result rather than a
fallback onto the current revision.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.core.errors import ErrorKind, OperationError
from app.core.time import ensure_utc, utcnow
from app.db.models import File, FileType, Model, ShareLink, ShareLinkPin
from app.modules.identity import share
from tests.factories import build_file, build_model


def _model(db_session: Session, slug: str, **fields) -> Model:
    row = build_model(
        db_session, name=slug.title(), slug=slug, hash=f"{slug:h<64}"[:64], **fields
    )
    db_session.refresh(row)
    return row


def _file(
    db_session: Session,
    model: Model,
    name: str,
    *,
    ftype: FileType = FileType.GCODE,
    **fields,
) -> File:
    used = len(db_session.exec(select(File).where(File.model_id == model.id)).all())
    row = build_file(
        db_session,
        model,
        path=f"{name}",
        filename=name,
        file_type=ftype,
        version=used + 1,  # (model_id, version) is unique
        size_bytes=10,
        sha256=f"{name:a<64}"[:64],
        **fields,
    )
    db_session.refresh(row)
    return row


def _gcode(db_session: Session, model: Model, name: str, **fields) -> File:
    return _file(db_session, model, name, ftype=FileType.GCODE, **fields)


def _create(db_session: Session, model: Model, **overrides):
    """`create_share` with the router's defaults filled in."""
    kwargs: dict = {
        "model_id": model.id,
        "expires_in_days": 7,
        "allow_download": False,
        "created_by": None,
    }
    kwargs.update(overrides)
    return share.create_share(db_session, **kwargs)


def _access(db_session: Session, raw_token: str) -> share.ShareAccess:
    return share.resolve_share_access(db_session, raw_token)


class TestCreateShare:
    def test_returns_a_token_that_is_not_stored(self, db_session: Session) -> None:
        model = _model(db_session, "minted")

        link, raw_token = _create(db_session, model)

        assert raw_token
        # Only the hash is persisted, so a database leak does not hand over live links.
        assert raw_token not in (link.token_hash or "")

    @pytest.mark.parametrize(
        ("requested", "expected_days"),
        [
            pytest.param(0, 1, id="below-min-clamps-to-one-day"),
            pytest.param(400, 365, id="above-max-clamps-to-a-year"),
            pytest.param(7, 7, id="in-range-kept"),
        ],
    )
    def test_clamps_the_lifetime(
        self, db_session: Session, requested: int, expected_days: int
    ) -> None:
        model = _model(db_session, f"lifetime{requested}")

        link, _ = _create(db_session, model, expires_in_days=requested)

        assert link.expires_at is not None
        # SQLite hands the column back naive; compare on the same footing.
        actual = (ensure_utc(link.expires_at) - utcnow()).days
        assert actual in (expected_days - 1, expected_days)

    def test_pins_the_named_gcode_revision_with_its_snapshot(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "scoped")
        chosen = _gcode(db_session, model, "chosen.gcode", recommended=True)
        _gcode(db_session, model, "other.gcode")

        link, _ = _create(db_session, model, revision_file_ids=[chosen.id])

        pins = share.list_pins(db_session, int(link.id))
        assert [pin.file_id for pin in pins] == [chosen.id]
        pin = pins[0]
        assert pin.sha256 == chosen.sha256
        assert pin.file_type is FileType.GCODE
        assert pin.original_filename == "chosen.gcode"
        assert pin.file_version == chosen.version
        assert pin.revision_number == 1
        assert pin.is_recommended is True

    def test_pins_an_stl_revision(self, db_session: Session) -> None:
        model = _model(db_session, "stl-pinned")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)

        link, _ = _create(db_session, model, revision_file_ids=[mesh.id])

        pins = share.list_pins(db_session, int(link.id))
        assert [pin.file_id for pin in pins] == [mesh.id]
        assert pins[0].file_type is FileType.STL
        assert pins[0].revision_number is None

    def test_ignores_a_repeated_revision_id(self, db_session: Session) -> None:
        model = _model(db_session, "deduped")
        chosen = _gcode(db_session, model, "chosen.gcode")

        link, _ = _create(db_session, model, revision_file_ids=[chosen.id, chosen.id])

        assert [pin.file_id for pin in share.list_pins(db_session, int(link.id))] == [
            chosen.id
        ]

    def test_refuses_an_empty_revision_selection(self, db_session: Session) -> None:
        model = _model(db_session, "empty-selection")

        with pytest.raises(OperationError) as raised:
            _create(db_session, model, revision_file_ids=[])

        assert raised.value.kind is ErrorKind.INVALID
        assert raised.value.detail == "no_revisions_selected"

    def test_refuses_a_revision_from_another_model(self, db_session: Session) -> None:
        model = _model(db_session, "mine")
        other = _model(db_session, "theirs")
        stranger = _gcode(db_session, other, "stranger.gcode")

        with pytest.raises(OperationError) as raised:
            _create(db_session, model, revision_file_ids=[stranger.id])

        assert raised.value.kind is ErrorKind.INVALID
        assert raised.value.detail == "invalid_revision_file_id"

    def test_refuses_a_trashed_revision(self, db_session: Session) -> None:
        model = _model(db_session, "trashed-revision")
        gone = _gcode(db_session, model, "gone.gcode", deleted_at=utcnow())

        with pytest.raises(OperationError) as raised:
            _create(db_session, model, revision_file_ids=[gone.id])

        assert raised.value.detail == "invalid_revision_file_id"

    def test_refuses_an_unpinnable_file_type(self, db_session: Session) -> None:
        model = _model(db_session, "dxf-revision")
        drawing = _file(db_session, model, "part.dxf", ftype=FileType.DXF)

        with pytest.raises(OperationError) as raised:
            _create(db_session, model, revision_file_ids=[drawing.id])

        assert raised.value.detail == "invalid_revision_file_id"

    def test_snapshots_the_model_cover_for_a_revision_share(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "cover-snapshot")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)
        model.thumbnail_file_id = mesh.id
        db_session.add(model)
        db_session.commit()

        link, _ = _create(db_session, model, revision_file_ids=[mesh.id])

        assert link.cover_file_id == mesh.id
        assert link.cover_thumbnail_path is None

    def test_leaves_a_model_share_without_pins_or_cover(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "plain-model")

        link, _ = _create(db_session, model)

        assert share.list_pins(db_session, int(link.id)) == ()
        assert link.cover_file_id is None
        assert link.cover_thumbnail_path is None

    def test_retries_a_token_hash_collision(
        self, db_session: Session, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        model = _model(db_session, "collision")
        _link, first_token = _create(db_session, model)
        replacement = "second-unguessable-token-value"
        tokens = iter([first_token, replacement])
        monkeypatch.setattr(
            share.secrets, "token_urlsafe", lambda _bytes: next(tokens)
        )

        link, raw_token = _create(db_session, model)

        assert raw_token == replacement
        assert link.token_hash != _link.token_hash


class TestResolveShareAccess:
    def test_returns_the_link_for_a_live_token(self, db_session: Session) -> None:
        model = _model(db_session, "live")
        _, raw_token = _create(db_session, model)

        assert _access(db_session, raw_token).link.model_id == model.id

    @pytest.mark.parametrize(
        "token", [pytest.param("", id="empty"), pytest.param("nope", id="unknown")]
    )
    def test_rejects_a_token_that_was_never_real(
        self, db_session: Session, token: str
    ) -> None:
        with pytest.raises(OperationError) as raised:
            share.resolve_share_access(db_session, token)

        assert raised.value.kind is ErrorKind.NOT_FOUND
        assert raised.value.detail == "not_found"

    def test_rejects_a_revoked_token(self, db_session: Session) -> None:
        model = _model(db_session, "revoked")
        link, raw_token = _create(db_session, model)
        share.revoke_share(db_session, link)

        with pytest.raises(OperationError) as raised:
            share.resolve_share_access(db_session, raw_token)

        assert raised.value.detail == "not_found"

    def test_rejects_an_expired_token(self, db_session: Session) -> None:
        model = _model(db_session, "expired")
        link, raw_token = _create(db_session, model)
        link.expires_at = utcnow() - timedelta(days=1)
        db_session.add(link)
        db_session.commit()

        with pytest.raises(OperationError) as raised:
            share.resolve_share_access(db_session, raw_token)

        assert raised.value.detail == "not_found"

    def test_rejects_a_model_share_whose_model_was_trashed(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "trashed-model")
        _, raw_token = _create(db_session, model)
        model.deleted_at = utcnow()
        db_session.add(model)
        db_session.commit()

        with pytest.raises(OperationError) as raised:
            share.resolve_share_access(db_session, raw_token)

        # Same 404 as an unknown token: the public surface is not an oracle.
        assert raised.value.detail == "not_found"

    def test_keeps_a_revision_share_alive_while_its_model_is_trashed(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "held-model")
        gcode = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(db_session, model, revision_file_ids=[gcode.id])
        model.deleted_at = utcnow()
        db_session.add(model)
        db_session.commit()

        access = _access(db_session, raw_token)

        assert access.is_revision_share
        assert [pin.file_id for pin in access.pins] == [gcode.id]


class TestAuthorizeSharedFile:
    def test_returns_a_file_of_the_shared_model(self, db_session: Session) -> None:
        model = _model(db_session, "reachable")
        gcode = _gcode(db_session, model, "reachable.gcode")
        _, raw_token = _create(db_session, model)

        access = _access(db_session, raw_token)
        assert share.authorize_shared_file(db_session, access, gcode.id).id == gcode.id

    def test_refuses_a_file_of_another_model(self, db_session: Session) -> None:
        model = _model(db_session, "mine2")
        other = _model(db_session, "theirs2")
        stranger = _gcode(db_session, other, "stranger2.gcode")
        _, raw_token = _create(db_session, model)

        access = _access(db_session, raw_token)
        with pytest.raises(OperationError) as raised:
            share.authorize_shared_file(db_session, access, stranger.id)

        assert raised.value.kind is ErrorKind.NOT_FOUND

    def test_refuses_a_trashed_file_on_a_model_share(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "trashed-file-model")
        gone = _gcode(db_session, model, "gone.gcode", deleted_at=utcnow())
        _, raw_token = _create(db_session, model)

        access = _access(db_session, raw_token)
        with pytest.raises(OperationError) as raised:
            share.authorize_shared_file(db_session, access, gone.id)

        assert raised.value.kind is ErrorKind.NOT_FOUND

    def test_serves_a_trashed_file_pinned_by_a_revision_share(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "trashed-file-pin")
        pinned = _gcode(db_session, model, "pinned.gcode")
        _, raw_token = _create(
            db_session, model, revision_file_ids=[pinned.id]
        )
        pinned.deleted_at = utcnow()
        db_session.add(pinned)
        db_session.commit()

        access = _access(db_session, raw_token)
        authorized = share.authorize_shared_file(db_session, access, pinned.id)

        assert authorized.id == pinned.id

    def test_refuses_a_new_revision_the_link_does_not_name(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "new-revision")
        chosen = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(db_session, model, revision_file_ids=[chosen.id])
        newer = _gcode(db_session, model, "rev2.gcode")

        access = _access(db_session, raw_token)
        with pytest.raises(OperationError) as raised:
            share.authorize_shared_file(db_session, access, newer.id)

        assert raised.value.kind is ErrorKind.NOT_FOUND

    def test_refuses_a_pinned_file_through_the_wrong_route(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "wrong-route")
        gcode = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(db_session, model, revision_file_ids=[gcode.id])

        access = _access(db_session, raw_token)
        with pytest.raises(OperationError) as raised:
            share.authorize_shared_file(
                db_session,
                access,
                gcode.id,
                file_types=share.MESH_TYPES,
            )

        assert raised.value.kind is ErrorKind.NOT_FOUND

    def test_reports_a_missing_pinned_row_as_gone(self, db_session: Session) -> None:
        # The foreign key makes a pin without its File unreachable through normal
        # writes; drive the service with the orphan identity directly to pin the
        # stable failure result for that impossible-in-production state.
        model = _model(db_session, "vanished-row")
        link, _ = _create(db_session, model)
        orphan = ShareLinkPin(
            share_link_id=int(link.id),
            file_id=9_999_999,
            sha256="a" * 64,
            file_type=FileType.GCODE,
            original_filename="rev1.gcode",
            file_version=1,
            revision_number=1,
        )
        access = share.ShareAccess(link=link, pins=(orphan,))

        with pytest.raises(OperationError) as raised:
            share.authorize_shared_file(db_session, access, 9_999_999)

        assert raised.value.kind is ErrorKind.GONE
        assert raised.value.detail == "shared_file_unavailable"


class TestPublicDetail:
    def test_model_share_follows_new_live_revisions(self, db_session: Session) -> None:
        model = _model(db_session, "live-model-detail")
        first = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(db_session, model)
        _gcode(db_session, model, "rev2.gcode")

        detail = share.public_detail(db_session, _access(db_session, raw_token))

        assert {row.id for row in detail.files} == {first.id + 1, first.id}

    def test_revision_share_hides_revisions_created_afterwards(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "pinned-detail")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)
        chosen = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(
            db_session, model, revision_file_ids=[mesh.id, chosen.id]
        )
        _gcode(db_session, model, "rev2.gcode")

        detail = share.public_detail(db_session, _access(db_session, raw_token))

        assert {row.id for row in detail.files} == {mesh.id, chosen.id}

    def test_revision_share_keeps_a_trashed_pinned_revision_visible(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "trashed-pin-detail")
        pinned = _gcode(db_session, model, "rev1.gcode")
        _, raw_token = _create(db_session, model, revision_file_ids=[pinned.id])
        pinned.deleted_at = utcnow()
        db_session.add(pinned)
        db_session.commit()

        detail = share.public_detail(db_session, _access(db_session, raw_token))

        assert [row.id for row in detail.files] == [pinned.id]
        assert detail.files[0].is_available is True

    def test_revision_number_snapshot_survives_another_revision_being_trashed(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "renumber")
        first = _gcode(db_session, model, "rev1.gcode")
        second = _gcode(db_session, model, "rev2.gcode", recommended=True)
        _, raw_token = _create(db_session, model, revision_file_ids=[second.id])
        first.deleted_at = utcnow()
        db_session.add(first)
        db_session.commit()

        detail = share.public_detail(db_session, _access(db_session, raw_token))

        shared = next(row for row in detail.files if row.id == second.id)
        assert shared.gcode_revision_number == 2

    def test_missing_pinned_row_is_a_stable_unavailable_entry(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "tombstone-detail")
        link, _ = _create(db_session, model)
        orphan = ShareLinkPin(
            share_link_id=int(link.id),
            file_id=9_999_998,
            sha256="b" * 64,
            file_type=FileType.GCODE,
            original_filename="lost.gcode",
            file_version=4,
            revision_number=4,
        )

        detail = share.public_detail(
            db_session, share.ShareAccess(link=link, pins=(orphan,))
        )

        assert len(detail.files) == 1
        entry = detail.files[0]
        assert entry.id == 9_999_998
        assert entry.is_available is False
        assert entry.original_filename == "lost.gcode"
        assert entry.gcode_revision_number == 4


class TestSharedThumbnailTarget:
    def test_model_share_follows_the_live_cover(self, db_session: Session) -> None:
        model = _model(db_session, "live-cover")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)
        model.thumbnail_file_id = mesh.id
        db_session.add(model)
        db_session.commit()
        _, raw_token = _create(db_session, model)

        file_id, path = share.shared_thumbnail_target(
            db_session, _access(db_session, raw_token)
        )

        assert file_id == mesh.id
        assert path is None

    def test_revision_share_keeps_its_cover_snapshot(self, db_session: Session) -> None:
        model = _model(db_session, "pinned-cover")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)
        model.thumbnail_file_id = mesh.id
        db_session.add(model)
        db_session.commit()
        _, raw_token = _create(db_session, model, revision_file_ids=[mesh.id])
        model.thumbnail_file_id = None
        model.thumbnail_path = None
        db_session.add(model)
        db_session.commit()

        access = _access(db_session, raw_token)

        file_id, path = share.shared_thumbnail_target(db_session, access)
        assert file_id == mesh.id
        assert share.public_detail(db_session, access).has_thumbnail is True

    def test_revision_share_without_a_cover_at_creation_stays_empty(
        self, db_session: Session
    ) -> None:
        model = _model(db_session, "born-without-cover")
        mesh = _file(db_session, model, "part.stl", ftype=FileType.STL)
        _, raw_token = _create(db_session, model, revision_file_ids=[mesh.id])
        # A later upload producing a cover must not drift onto the old share.
        newer = _file(db_session, model, "part2.stl", ftype=FileType.STL)
        model.thumbnail_file_id = newer.id
        db_session.add(model)
        db_session.commit()

        access = _access(db_session, raw_token)

        file_id, path = share.shared_thumbnail_target(db_session, access)
        assert (file_id, path) == (None, None)
        assert share.public_detail(db_session, access).has_thumbnail is False


class TestShareHolds:
    def test_active_revision_pin_holds_its_file(self, db_session: Session) -> None:
        model = _model(db_session, "held")
        pinned = _gcode(db_session, model, "rev1.gcode")
        _create(db_session, model, revision_file_ids=[pinned.id])

        assert share.file_is_held(db_session, pinned.id) is True
        assert share.model_is_held(db_session, model.id) is True

    def test_model_share_holds_nothing(self, db_session: Session) -> None:
        model = _model(db_session, "unheld")
        gcode = _gcode(db_session, model, "rev1.gcode")
        _create(db_session, model)

        assert share.file_is_held(db_session, gcode.id) is False
        assert share.model_is_held(db_session, model.id) is False

    def test_revoked_link_releases_its_hold(self, db_session: Session) -> None:
        model = _model(db_session, "revoked-hold")
        pinned = _gcode(db_session, model, "rev1.gcode")
        link, _ = _create(db_session, model, revision_file_ids=[pinned.id])

        share.revoke_share(db_session, link)

        assert share.file_is_held(db_session, pinned.id) is False

    def test_expired_link_releases_its_hold(self, db_session: Session) -> None:
        model = _model(db_session, "expired-hold")
        pinned = _gcode(db_session, model, "rev1.gcode")
        link, _ = _create(db_session, model, revision_file_ids=[pinned.id])
        link.expires_at = utcnow() - timedelta(days=1)
        db_session.add(link)
        db_session.commit()

        assert share.file_is_held(db_session, pinned.id) is False

    def test_releasing_pins_keeps_active_ones(self, db_session: Session) -> None:
        model = _model(db_session, "partial-release")
        active_target = _gcode(db_session, model, "active.gcode")
        expired_target = _gcode(db_session, model, "expired.gcode")
        _create(db_session, model, revision_file_ids=[active_target.id])
        expired_link, _ = _create(
            db_session, model, revision_file_ids=[expired_target.id]
        )
        expired_link.expires_at = utcnow() - timedelta(days=1)
        db_session.add(expired_link)
        db_session.commit()

        share.release_inactive_pins(db_session, expired_target.id)

        remaining = db_session.exec(select(ShareLinkPin)).all()
        assert [pin.file_id for pin in remaining] == [active_target.id]


class TestRecordAccess:
    def test_counts_each_view(self, db_session: Session) -> None:
        model = _model(db_session, "counted-service")
        link, _ = _create(db_session, model)

        share.record_access(db_session, link)
        share.record_access(db_session, link)

        assert db_session.get(ShareLink, link.id).access_count == 2


class TestRevokeShare:
    def test_stamps_the_revocation(self, db_session: Session) -> None:
        model = _model(db_session, "revocable-service")
        link, _ = _create(db_session, model)

        revoked = share.revoke_share(db_session, link)

        assert revoked.revoked_at is not None
