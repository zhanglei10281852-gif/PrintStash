"""Active revision shares hold exact Artifact identities out of trash reclamation.

A pin is a *logical* hold: the pinned row and its owned bytes survive the
hourly GC while the link is active, even though the Artifact (and its model)
are trashed. The hold is scoped to the pinned identity — unrelated expired
Artifacts still reclaim in the same pass — and expiry or revocation is the only
thing that releases it. Repeated GC passes must make the same decision.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlmodel import Session, select

from app.core.time import utcnow
from app.db.models import File, Model, ShareLinkPin
from app.modules.identity import share
from app.modules.library.trash import gc_soft_deleted, hard_delete_file
from tests.factories import build_model, build_stored_file


def _pin_share(db_session: Session, file_id: int, model_id: int):
    return share.create_share(
        db_session,
        model_id=model_id,
        expires_in_days=7,
        allow_download=True,
        revision_file_ids=[file_id],
        created_by=None,
    )


def _expire(model_or_file) -> None:
    model_or_file.deleted_at = utcnow() - timedelta(days=2)


class TestHourlyGcRespectsHolds:
    def test_keeps_a_held_file_but_reclaims_an_unrelated_one(
        self, db_session: Session, storage
    ) -> None:
        model = build_model(db_session, "hold-file", slug="hold-file")
        held = build_stored_file(db_session, storage, model, filename="held.stl")
        other = build_stored_file(
            db_session, storage, model, filename="other.stl"
        )
        held_id, held_path = held.id, held.path
        other_id, other_path = other.id, other.path
        _pin_share(db_session, held_id, model.id)
        for row in (held, other):
            _expire(row)
            db_session.add(row)
        db_session.commit()

        result = gc_soft_deleted(retention_days=0)

        assert result["rows"] == 1
        db_session.expire_all()
        assert db_session.get(File, held_id) is not None
        assert Path(held_path).exists()
        assert db_session.get(File, other_id) is None
        assert not Path(other_path).exists()

    def test_a_second_pass_is_idempotent(self, db_session: Session, storage) -> None:
        model = build_model(db_session, "hold-repeat", slug="hold-repeat")
        held = build_stored_file(
            db_session, storage, model, filename="held.stl"
        )
        held_id, held_path = held.id, held.path
        _pin_share(db_session, held_id, model.id)
        _expire(held)
        db_session.add(held)
        db_session.commit()

        first = gc_soft_deleted(retention_days=0)
        second = gc_soft_deleted(retention_days=0)

        assert first["rows"] == 0
        assert second["rows"] == 0
        db_session.expire_all()
        assert db_session.get(File, held_id) is not None
        assert Path(held_path).exists()

    def test_revocation_releases_the_hold(
        self, db_session: Session, storage
    ) -> None:
        model = build_model(db_session, "hold-release", slug="hold-release")
        held = build_stored_file(
            db_session, storage, model, filename="held.stl"
        )
        held_id, held_path = held.id, held.path
        link, _ = _pin_share(db_session, held_id, model.id)
        _expire(held)
        db_session.add(held)
        db_session.commit()
        assert gc_soft_deleted(retention_days=0)["rows"] == 0

        share.revoke_share(db_session, link)
        result = gc_soft_deleted(retention_days=0)

        assert result["rows"] == 1
        db_session.expire_all()
        assert db_session.get(File, held_id) is None
        assert not Path(held_path).exists()

    def test_expiry_releases_the_hold(
        self, db_session: Session, storage
    ) -> None:
        model = build_model(db_session, "hold-expiry", slug="hold-expiry")
        held = build_stored_file(
            db_session, storage, model, filename="held.stl"
        )
        held_id = held.id
        link, _ = _pin_share(db_session, held_id, model.id)
        _expire(held)
        link.expires_at = utcnow() - timedelta(days=1)
        db_session.add(held)
        db_session.add(link)
        db_session.commit()

        result = gc_soft_deleted(retention_days=0)

        assert result["rows"] == 1
        db_session.expire_all()
        assert db_session.get(File, held_id) is None

    def test_keeps_a_held_model_but_purges_an_unrelated_expired_one(
        self, db_session: Session, storage
    ) -> None:
        held_model = build_model(db_session, "hold-model", slug="hold-model")
        held_file = build_stored_file(
            db_session, storage, held_model, filename="held-model.stl"
        )
        held_model_id, held_file_id = held_model.id, held_file.id
        held_path = held_file.path
        _pin_share(db_session, held_file_id, held_model_id)
        other_model = build_model(
            db_session, "hold-other-model", slug="hold-other-model"
        )
        other_file = build_stored_file(
            db_session, storage, other_model, filename="other-model.stl"
        )
        other_model_id, other_file_id = other_model.id, other_file.id
        _expire(held_model)
        _expire(other_model)
        db_session.add_all([held_model, other_model])
        db_session.commit()

        result = gc_soft_deleted(retention_days=0)

        assert result["rows"] == 1
        db_session.expire_all()
        assert db_session.get(Model, held_model_id) is not None
        assert db_session.get(File, held_file_id) is not None
        assert Path(held_path).exists()
        assert db_session.get(Model, other_model_id) is None
        assert db_session.get(File, other_file_id) is None


class TestHardDeleteGuards:
    def test_refuses_to_purge_an_active_held_file(
        self, db_session: Session, storage
    ) -> None:
        model = build_model(db_session, "guard-file", slug="guard-file")
        held = build_stored_file(
            db_session, storage, model, filename="held.stl"
        )
        held_id, held_path = held.id, held.path
        _pin_share(db_session, held_id, model.id)
        _expire(held)
        db_session.add(held)
        db_session.commit()

        with pytest.raises(share.ShareHoldError):
            hard_delete_file(db_session, held, confirm_storage_risk=True)

        db_session.rollback()
        db_session.expire_all()
        assert db_session.get(File, held_id) is not None
        assert Path(held_path).exists()

    def test_purges_a_file_held_only_by_an_expired_link(
        self, db_session: Session, storage
    ) -> None:
        model = build_model(db_session, "guard-expired", slug="guard-expired")
        held = build_stored_file(
            db_session, storage, model, filename="held.stl"
        )
        held_id = held.id
        link, _ = _pin_share(db_session, held_id, model.id)
        _expire(held)
        link.expires_at = utcnow() - timedelta(days=1)
        db_session.add(held)
        db_session.add(link)
        db_session.commit()

        hard_delete_file(db_session, held, confirm_storage_risk=True)
        db_session.commit()

        assert db_session.get(File, held_id) is None
        assert (
            db_session.exec(
                select(ShareLinkPin).where(ShareLinkPin.file_id == held_id)
            ).first()
            is None
        )
