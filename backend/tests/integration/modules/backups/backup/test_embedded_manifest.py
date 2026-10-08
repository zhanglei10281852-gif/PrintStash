"""Backup manifest classification of embedded images.

Current references, pending retirements and same-named objects in different
owner namespaces are distinct recovery classes in the archive manifest.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

from app.db.models import Document, DocumentKind, EmbeddedImageOwnerKind
from app.modules.backups.backup import creation as backup_creation
from app.modules.library.embedded_images import sync_owner_body
from app.modules.storage.storage_backend.runtime import get_backend
from tests.factories import store_owned_bytes
from tests.integration._backup_harness import BackupEnv


def _seed_embedded_images(session) -> tuple[int, int, int]:
    shared = "f" * 64 + ".png"
    retired = "g" * 64 + ".png"
    doc_a = Document(name="a", kind=DocumentKind.MARKDOWN)
    doc_b = Document(name="b", kind=DocumentKind.MARKDOWN)
    doc_c = Document(name="c", kind=DocumentKind.MARKDOWN)
    session.add(doc_a)
    session.add(doc_b)
    session.add(doc_c)
    session.commit()
    session.refresh(doc_a)
    session.refresh(doc_b)
    session.refresh(doc_c)
    doc_a.body = f"![x](/api/v1/documents/{doc_a.id}/images/{shared})"
    doc_b.body = f"![x](/api/v1/documents/{doc_b.id}/images/{shared})"
    session.add(doc_a)
    session.add(doc_b)
    sync_owner_body(session, EmbeddedImageOwnerKind.DOCUMENT, doc_a.id, doc_a.body)
    sync_owner_body(session, EmbeddedImageOwnerKind.DOCUMENT, doc_b.id, doc_b.body)
    retired_body = f"![x](/api/v1/documents/{doc_c.id}/images/{retired})"
    sync_owner_body(session, EmbeddedImageOwnerKind.DOCUMENT, doc_c.id, retired_body)
    sync_owner_body(session, EmbeddedImageOwnerKind.DOCUMENT, doc_c.id, "")
    backend = get_backend()
    store_owned_bytes(
        session,
        backend,
        backend.document_image_key(doc_a.id, shared),
        b"a-bytes",
        object_kind="document_image",
    )
    store_owned_bytes(
        session,
        backend,
        backend.document_image_key(doc_b.id, shared),
        b"b-bytes",
        object_kind="document_image",
    )
    store_owned_bytes(
        session,
        backend,
        backend.document_image_key(doc_c.id, retired),
        b"c-bytes",
        object_kind="document_image",
    )
    session.commit()
    return doc_a.id, doc_b.id, doc_c.id


def _manifest(archive: Path) -> dict:
    with tarfile.open(archive, "r:gz") as tar:
        manifest_file = tar.extractfile("manifest.json")
        assert manifest_file is not None
        return json.loads(manifest_file.read())


class TestEmbeddedImageManifest:
    def test_distinguishes_current_retiring_and_shared_namespace_objects(
        self, backup_env: BackupEnv
    ) -> None:
        with backup_env.new_session() as session:
            owner_a, owner_b, owner_c = _seed_embedded_images(session)

        meta = backup_creation.create_backup()
        manifest = _manifest(Path(meta.path))

        image_entries = [
            entry
            for entry in manifest["files"]
            if str(entry.get("role", "")).startswith("embedded")
        ]
        assert {entry["role"] for entry in image_entries} == {
            "embedded_current",
            "embedded_retiring",
        }
        current = [e for e in image_entries if e["role"] == "embedded_current"]
        retiring = [e for e in image_entries if e["role"] == "embedded_retiring"]
        assert len(current) == 2
        assert len(retiring) == 1
        # Same content-hash name owned by two documents is two distinct blobs.
        assert len({entry["key"] for entry in current}) == 2
        assert {entry["image_owner_id"] for entry in current} == {owner_a, owner_b}
        assert all(e["image_owner_kind"] == "document" for e in image_entries)
        assert retiring[0]["image_owner_id"] == owner_c

    def test_backup_fails_when_a_retiring_image_is_missing(
        self, backup_env: BackupEnv
    ) -> None:
        import pytest
        from sqlmodel import select

        with backup_env.new_session() as session:
            _seed_embedded_images(session)
            backend = get_backend()
            retired = "g" * 64 + ".png"
            doc_c = session.exec(
                select(Document).order_by(Document.id.desc())
            ).first()
            assert doc_c is not None
            Path(backend.document_image_key(doc_c.id, retired)).unlink()

        with pytest.raises(FileNotFoundError):
            backup_creation.create_backup()
