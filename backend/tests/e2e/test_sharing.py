"""E2E: public share links — create, anonymous read, revoke, revision pins.

A share link must let an unauthenticated visitor read a model, and revoking it
must immediately deny further access.

A revision share names the exact Artifact selected at creation: newer revisions
never appear on the link, a soft-deleted pinned revision keeps serving through
trash, repeated GC passes leave it alone while the link is active, and the
token only dies once the link is revoked.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest

from app.core.time import utcnow
from app.modules.library.trash import gc_soft_deleted
from tests.factories import build_file, build_model


def _seed_model(session) -> int:
    model = build_model(
        session, name="Shared Bracket", slug="shared-bracket", hash="c" * 64
    )
    return model.id


def _write(tmp_path, name: str, body: bytes):
    path = tmp_path / name
    path.write_bytes(body)
    return path


class TestShareLink:
    @pytest.mark.asyncio
    async def test_share_link_grants_then_revokes_anonymous_access(
        self, api, superuser_headers, e2e_db
    ):
        model_id = _seed_model(e2e_db)

        created = await api.post(
            f"/api/v1/models/{model_id}/shares",
            json={"expires_in_days": 7, "allow_download": False},
            headers=superuser_headers,
        )
        assert created.status_code == 200, created.text
        payload = created.json()
        token = payload["token"]
        share_id = payload["id"]

        # Anonymous visitor (no auth header) can read the shared model.
        public = await api.get(f"/api/v1/share/{token}")
        assert public.status_code == 200, public.text
        assert public.json()["name"] == "Shared Bracket"

        # A bogus token is rejected.
        assert (await api.get("/api/v1/share/not-a-real-token")).status_code in (
            403,
            404,
        )

        # Revoke -> the same token no longer resolves.
        revoked = await api.delete(
            f"/api/v1/shares/{share_id}", headers=superuser_headers
        )
        assert revoked.status_code == 200, revoked.text
        after = await api.get(f"/api/v1/share/{token}")
        assert after.status_code in (403, 404, 410), after.text

        # Revoked links drop out of the owner's management list (the row is kept so
        # the token above stays permanently dead).
        listed = await api.get(
            f"/api/v1/models/{model_id}/shares", headers=superuser_headers
        )
        assert listed.status_code == 200, listed.text
        assert all(link["id"] != share_id for link in listed.json())

    @pytest.mark.asyncio
    async def test_revision_share_pins_the_selected_revision_through_trash_and_gc(
        self, api, superuser_headers, e2e_db, tmp_path
    ):
        model = build_model(
            e2e_db,
            name="Pinned Bracket",
            slug="pinned-bracket",
            hash="d" * 64,
        )
        first_body = b"G1 X1\n"
        first = build_file(
            e2e_db,
            model,
            filename="rev1.gcode",
            path=str(_write(tmp_path, "rev1.gcode", first_body)),
            size_bytes=len(first_body),
            sha256=hashlib.sha256(first_body).hexdigest(),
            file_type="gcode",
            version=1,
        )

        created = await api.post(
            f"/api/v1/models/{model.id}/shares",
            json={
                "expires_in_days": 7,
                "allow_download": True,
                "revision_file_ids": [first.id],
            },
            headers=superuser_headers,
        )
        assert created.status_code == 200, created.text
        token = created.json()["token"]

        # A revision uploaded afterwards must not appear on the share.
        second_body = b"G1 X2\n"
        second = build_file(
            e2e_db,
            model,
            filename="rev2.gcode",
            path=str(_write(tmp_path, "rev2.gcode", second_body)),
            size_bytes=len(second_body),
            sha256=hashlib.sha256(second_body).hexdigest(),
            file_type="gcode",
            version=2,
        )

        detail = await api.get(f"/api/v1/share/{token}")
        assert detail.status_code == 200, detail.text
        assert [row["id"] for row in detail.json()["files"]] == [first.id]

        served = await api.get(f"/api/v1/share/{token}/files/{first.id}/gcode")
        assert served.status_code == 200, served.text
        assert served.content == first_body
        drifted = await api.get(f"/api/v1/share/{token}/files/{second.id}/gcode")
        assert drifted.status_code == 404, drifted.text

        # Trashing the pinned revision keeps it on the share.
        first.deleted_at = utcnow() - timedelta(days=2)
        e2e_db.add(first)
        e2e_db.commit()

        detail = await api.get(f"/api/v1/share/{token}")
        assert detail.status_code == 200, detail.text
        pinned_row = detail.json()["files"][0]
        assert pinned_row["id"] == first.id
        assert pinned_row["is_available"] is True
        served = await api.get(f"/api/v1/share/{token}/files/{first.id}/gcode")
        assert served.status_code == 200, served.text
        assert served.content == first_body

        # Repeated GC passes with zero retention neither reap the held row nor
        # error: the hold is evaluated straight from the catalog, so a process
        # restart between passes cannot change the decision.
        first_pass = gc_soft_deleted(retention_days=0)
        second_pass = gc_soft_deleted(retention_days=0)
        assert first_pass["rows"] == 0
        assert second_pass["rows"] == 0
        e2e_db.expire_all()
        assert e2e_db.get(type(first), first.id) is not None
        assert (
            await api.get(f"/api/v1/share/{token}/files/{first.id}/gcode")
        ).status_code == 200

        # Revocation is the release: the token itself dies immediately. The
        # subsequent GC reclaim is covered at the integration tier.
        revoked = await api.delete(
            f"/api/v1/shares/{created.json()['id']}", headers=superuser_headers
        )
        assert revoked.status_code == 200, revoked.text
        assert (await api.get(f"/api/v1/share/{token}")).status_code in (
            403,
            404,
            410,
        )
