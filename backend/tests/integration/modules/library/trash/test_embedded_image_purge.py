"""Purge-time release rules for embedded document/collection images.

Physical release belongs to the precise owner's permanent deletion — and even
then only when no other referrer still claims the exact key. These tests pin
that boundary through the same GC path hourly trash collection uses.
"""

from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, select

from app.core.time import utcnow
from app.db.models import (
    EmbeddedImageOwnerKind,
    EmbeddedImageRetirement,
    EmbeddedImageRetirementState,
)
from app.modules.library.embedded_images import sync_owner_body
from app.modules.library.trash import (
    gc_soft_deleted,
    hard_delete_collection,
)
from tests.factories import (
    build_collection,
    build_document,
    store_owned_bytes,
)

_A = "a" * 64 + ".png"
_B = "b" * 64 + ".png"
_C = "c" * 64 + ".png"


def _doc_url(document_id: int, name: str) -> str:
    return f"/api/v1/documents/{document_id}/images/{name}"


def _col_url(collection_id: int, name: str) -> str:
    return f"/api/v1/collections/{collection_id}/images/{name}"


def _body(*urls: str) -> str:
    return " ".join(f"![x]({url})" for url in urls)


def _image(session: Session, storage, key: str, data: bytes) -> None:
    store_owned_bytes(
        session,
        storage,
        key,
        data,
        object_kind="document_image" if "/document-images/" in key else "collection_image",
    )


class TestDocumentImagePurge:
    def test_gc_releases_current_and_retiring_images_but_keeps_unreferenced(
        self, db_session: Session, storage
    ) -> None:
        doc = build_document(db_session, "doc", body="", deleted_at=utcnow())
        key_a = storage.document_image_key(doc.id, _A)
        key_b = storage.document_image_key(doc.id, _B)
        key_c = storage.document_image_key(doc.id, _C)
        _image(db_session, storage, key_a, b"a")
        _image(db_session, storage, key_b, b"b")
        _image(db_session, storage, key_c, b"c")
        body_two = _body(_doc_url(doc.id, _A), _doc_url(doc.id, _B))
        body_one = _body(_doc_url(doc.id, _A))
        doc.body = body_two
        db_session.add(doc)
        sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, body_two
        )
        doc.body = body_one
        db_session.add(doc)
        sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, body_one
        )
        db_session.commit()

        gc_soft_deleted(retention_days=0)

        assert not Path(key_a).exists()
        assert not Path(key_b).exists()
        # Never embedded — default compatibility leaves discovered objects alone.
        assert Path(key_c).exists()
        db_session.expire_all()
        retirement = db_session.exec(
            select(EmbeddedImageRetirement).where(
                EmbeddedImageRetirement.name == _B
            )
        ).one()
        assert retirement.state is EmbeddedImageRetirementState.RELEASED

    def test_gc_preserves_an_image_another_live_document_still_references(
        self, db_session: Session, storage
    ) -> None:
        owner = build_document(db_session, "owner", body="", deleted_at=utcnow())
        owner_id = owner.id
        other = build_document(db_session, "referrer", body="")
        key = storage.document_image_key(owner.id, _A)
        _image(db_session, storage, key, b"shared")
        owner_body = _body(_doc_url(owner.id, _A))
        other_body = _body(_doc_url(owner.id, _A))
        owner.body = owner_body
        other.body = other_body
        db_session.add(owner)
        db_session.add(other)
        sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id, owner_body
        )
        sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, other.id, other_body
        )
        db_session.commit()

        gc_soft_deleted(retention_days=0)

        assert Path(key).exists()
        db_session.expire_all()
        assert db_session.get(type(owner), owner_id) is None

class TestCollectionImagePurge:
    def test_hard_delete_references_are_scoped_to_the_collection_namespace(
        self, db_session: Session, storage
    ) -> None:
        collection = build_collection(
            db_session, name="Guides", slug="guides", path="guides"
        )
        document = build_document(db_session, "guide-doc", body="")
        own_key = storage.collection_image_key(collection.id, _A)
        cross_key = storage.collection_image_key(collection.id, _B)
        orphan_key = storage.collection_image_key(collection.id, _C)
        _image(db_session, storage, own_key, b"own")
        _image(db_session, storage, cross_key, b"cross")
        _image(db_session, storage, orphan_key, b"orphan")
        readme_two = _body(_col_url(collection.id, _A), _col_url(collection.id, _B))
        readme_one = _body(_col_url(collection.id, _B))
        doc_body = _body(_col_url(collection.id, _B))
        document.body = doc_body
        db_session.add(document)
        collection.readme = readme_two
        db_session.add(collection)
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.COLLECTION,
            collection.id,
            readme_two,
        )
        # A document embeds one collection readme image: cross-kind protection.
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            document.id,
            doc_body,
        )
        # The first readme image is edited out before the collection is purged.
        collection.readme = readme_one
        db_session.add(collection)
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.COLLECTION,
            collection.id,
            readme_one,
        )
        collection.deleted_at = utcnow()
        db_session.add(collection)
        db_session.commit()

        hard_delete_collection(db_session, collection)
        db_session.commit()

        from app.modules.storage.storage_deletion import process_storage_delete_intents

        process_storage_delete_intents()

        assert not Path(own_key).exists()
        assert Path(cross_key).exists()
        assert Path(orphan_key).exists()
