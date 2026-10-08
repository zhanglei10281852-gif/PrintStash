"""E2E: the recoverable lifecycle of images embedded in markdown documents.

Headline journey through the real document, trash and maintenance routers:
upload → embed → remove (observable pending retirement) → trash → restore
(URL still reads) → audit distinguishes retiring/cross-reference → permanent
delete releases only images no other document references, and never touches an
uploaded-but-unreferenced object.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.modules.storage.storage_backend.runtime import get_backend, init_backend

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000154a24f6f0000000049454e44ae426082"
)


async def _setup_and_login(api, tmp_path) -> dict[str, str]:
    # The local backend only authorizes verified deletes on a real (non-overlay)
    # filesystem with stable inodes; CI mounts a docker volume there. A unique
    # subdirectory keeps repeated runs from inheriting a populated vault root.
    import uuid

    verified_root = Path("/vault-data")
    if verified_root.is_dir():
        root = verified_root / uuid.uuid4().hex
        root.mkdir(parents=True, exist_ok=True)
    else:
        root = tmp_path
    response = await api.post(
        "/api/v1/setup",
        json={
            "username": "owner",
            "password": "Password123",
            "storage_backend": "local",
            "data_dir": str(root / "files"),
            "thumb_dir": str(root / "thumbs"),
        },
    )
    assert response.status_code == 201, response.text
    init_backend()
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _new_document(api, headers: dict[str, str], name: str) -> dict:
    response = await api.post(
        "/api/v1/documents",
        json={"name": name, "body": "", "collection_id": None},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _upload_image(api, headers, document_id: int, data: bytes) -> tuple[str, str]:
    response = await api.post(
        f"/api/v1/documents/{document_id}/images",
        files={"file": ("pic.png", data, "image/png")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    url = response.json()["url"]
    return url, url.rsplit("/", 1)[-1]


class TestDocumentImageLifecycle:
    @pytest.mark.asyncio
    async def test_edit_retire_restore_and_precise_purge(self, api, tmp_path, e2e_db):
        headers = await _setup_and_login(api, tmp_path)
        backend = get_backend()

        owner = await _new_document(api, headers, "Owner")
        referrer = await _new_document(api, headers, "Referrer")
        url_keep, name_keep = await _upload_image(api, headers, owner["id"], b"keep")
        url_gone, name_gone = await _upload_image(
            api, headers, owner["id"], b"going-away"
        )
        _url_orphan, name_orphan = await _upload_image(
            api, headers, owner["id"], b"never-embedded"
        )

        # Embed two images, then remove one: the removal is an observable
        # pending retirement, not a silently orphaned blob.
        response = await api.put(
            f"/api/v1/documents/{owner['id']}",
            json={"body": f"![a]({url_keep}) ![b]({url_gone})"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        response = await api.put(
            f"/api/v1/documents/{owner['id']}",
            json={"body": f"![a]({url_keep})"},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        images = {image["name"]: image for image in response.json()["images"]}
        assert images[name_gone]["state"] == "retiring"
        assert images[name_gone]["removed_at"]
        # The referrer document embeds the image the owner is retiring.
        response = await api.put(
            f"/api/v1/documents/{referrer['id']}",
            json={"body": f"![theirs]({url_gone})"},
            headers=headers,
        )
        assert response.status_code == 200, response.text

        # Trash then restore: the original URL keeps serving its bytes.
        assert (
            await api.delete(
                f"/api/v1/documents/{owner['id']}", headers=headers
            )
        ).status_code == 204
        assert (
            await api.post(
                f"/api/v1/documents/{owner['id']}/restore", headers=headers
            )
        ).status_code == 200
        served = await api.get(url_keep, headers=headers)
        assert served.status_code == 200
        assert served.content == b"keep"

        # The audit classifies the retirement and the cross-document reference.
        started = await api.post(
            "/api/v1/maintenance/audits",
            json={"mode": "quick"},
            headers=headers,
        )
        assert started.status_code == 202, started.text
        from tests.e2e._jobs import settle

        settle()
        run = await api.get(
            f"/api/v1/maintenance/audits/{started.json()['id']}",
            headers=headers,
        )
        assert run.status_code == 200, run.text
        codes = {finding["code"] for finding in run.json()["findings"]}
        assert "embedded_image_retiring" in codes
        assert "embedded_image_cross_reference" in codes
        # The uploaded-but-unreferenced object stays in compatibility limbo
        # but is never deleted by GC or audit.
        assert "embedded_image_unreferenced" in codes

        # Permanent deletion: the owner's own image is released, the image the
        # referrer still embeds is preserved byte-for-byte.
        assert (
            await api.delete(
                f"/api/v1/documents/{owner['id']}", headers=headers
            )
        ).status_code == 204
        purged = await api.delete(
            f"/api/v1/documents/{owner['id']}/permanent",
            headers=headers,
        )
        assert purged.status_code == 204, purged.text

        assert backend.exists(
            backend.document_image_key(owner["id"], name_gone)
        )
        assert not backend.exists(
            backend.document_image_key(owner["id"], name_keep)
        )
        # Uploaded but never embedded: default compatibility keeps it.
        assert backend.exists(
            backend.document_image_key(owner["id"], name_orphan)
        )
        # The owner namespace is gone, so its old URL can no longer serve; the
        # audit's reclaim repair is what makes the bytes readable again. The
        # protection contract is byte survival, which the referrer can restore.
        assert backend.read_bytes(
            backend.document_image_key(owner["id"], name_gone)
        ) == b"going-away"
