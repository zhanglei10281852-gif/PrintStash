"""The embedded image reference ledger: edits, retirements, reconciliation and
the exact purge decision.

These tests pin the recoverable lifecycle at its owner seam: a removed
reference becomes observable pending retirement, re-referencing reclaims it,
another referrer protects the exact key, and restart reconciliation converges
the ledger from bodies without ever deleting bytes.
"""

from __future__ import annotations

from sqlmodel import Session, select

from app.core.time import utcnow
from app.db.models import (
    EmbeddedImageClaim,
    EmbeddedImageOwnerKind,
    EmbeddedImageRetirement,
    EmbeddedImageRetirementState,
)
from app.modules.library.embedded_images import (
    finalize_owner_purge,
    owner_has_images,
    owner_purge_plan,
    pending_retirements,
    reclaim_cross_reference,
    reconcile_embedded_image_references,
    sync_owner_body,
)
from tests.factories import (
    build_collection,
    build_document,
    build_embedded_image_claim,
    build_embedded_image_retirement,
)

_A = "a" * 64 + ".png"
_B = "b" * 64 + ".png"
_C = "c" * 64 + ".png"


def _body(*urls: str) -> str:
    return " ".join(f"![x]({url})" for url in urls)


def _doc_url(document_id: int, name: str) -> str:
    return f"/api/v1/documents/{document_id}/images/{name}"


def _col_url(collection_id: int, name: str) -> str:
    return f"/api/v1/collections/{collection_id}/images/{name}"


def _claim_keys(session: Session) -> set[tuple[str, int, str, int, str]]:
    return {
        (
            row.owner_kind.value,
            row.owner_id,
            row.referrer_kind.value,
            row.referrer_id,
            row.name,
        )
        for row in session.exec(select(EmbeddedImageClaim)).all()
    }


class TestSyncOwnerBody:
    def test_records_self_and_cross_references_from_a_new_body(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")

        change = sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A), _doc_url(doc.id + 1000, _B)),
        )
        db_session.commit()

        assert change.current == (_A,)
        assert change.retired == ()
        assert _claim_keys(db_session) == {
            ("document", doc.id, "document", doc.id, _A),
            ("document", doc.id + 1000, "document", doc.id, _B),
        }

    def test_records_the_exact_removed_set_as_pending_retirement(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A), _doc_url(doc.id, _B)),
        )
        db_session.commit()

        change = sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _B)),
        )
        db_session.commit()

        assert change.retired == (_A,)
        rows = pending_retirements(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id)
        assert [row.name for row in rows] == [_A]
        assert rows[0].state is EmbeddedImageRetirementState.PENDING
        assert _claim_keys(db_session) == {
            ("document", doc.id, "document", doc.id, _B),
        }

    def test_a_duplicate_edit_changes_nothing(self, db_session: Session) -> None:
        doc = build_document(db_session, body="")
        body = _body(_doc_url(doc.id, _A))
        sync_owner_body(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, body)
        db_session.commit()

        change = sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, body
        )
        db_session.commit()

        assert change.current == (_A,)
        assert change.retired == ()
        assert change.claims_added == 0
        assert change.claims_removed == 0
        assert pending_retirements(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id) == []

    def test_reclaims_a_pending_retirement_when_referenced_again(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        sync_owner_body(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, _body()
        )
        db_session.commit()
        assert len(
            pending_retirements(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id)
        ) == 1

        change = sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        db_session.commit()

        assert change.reclaimed == (_A,)
        rows = db_session.exec(select(EmbeddedImageRetirement)).all()
        assert len(rows) == 1
        assert rows[0].state is EmbeddedImageRetirementState.RECLAIMED
        assert rows[0].reclaimed_at is not None
        assert pending_retirements(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id) == []

    def test_keeps_another_referrers_claim_when_the_owner_removes_its_own(
        self, db_session: Session
    ) -> None:
        owner = build_document(db_session, "owner", body="")
        other = build_document(db_session, "other", body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            owner.id,
            _body(_doc_url(owner.id, _A)),
        )
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            other.id,
            _body(_doc_url(owner.id, _A)),
        )
        sync_owner_body(db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id, "")
        db_session.commit()

        assert ("document", owner.id, "document", other.id, _A) in _claim_keys(
            db_session
        )
        assert [
            row.name
            for row in pending_retirements(
                db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id
            )
        ] == [_A]


class TestOwnerPurgePlan:
    def test_releases_current_and_retired_names_without_other_referrers(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _B)),
        )
        db_session.commit()

        plan = owner_purge_plan(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _B), _doc_url(doc.id, _C)),
        )

        assert _B in plan.release
        assert _C in plan.release
        assert _A in plan.release
        assert plan.protected == ()

    def test_protects_a_name_another_referrer_still_claims(
        self, db_session: Session
    ) -> None:
        owner = build_document(db_session, "owner", body="")
        other = build_document(db_session, "other", body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            other.id,
            _body(_doc_url(owner.id, _A)),
        )

        plan = owner_purge_plan(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            owner.id,
            _body(_doc_url(owner.id, _A), _doc_url(owner.id, _B)),
        )

        assert plan.protected == (_A,)
        assert plan.release == (_B,)

    def test_isolates_document_and_collection_namespaces(self, db_session: Session) -> None:
        doc = build_document(db_session, "doc", body="")
        col = build_collection(db_session, name="col", slug="col", path="col")
        # Both the document itself and a collection in the other namespace
        # reference the document's image: the cross-kind claim protects it,
        # and the collection has nothing releasable in its own namespace.
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.COLLECTION,
            col.id,
            _body(_doc_url(doc.id, _A)),
        )
        db_session.commit()

        doc_plan = owner_purge_plan(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, _body(_doc_url(doc.id, _A))
        )
        col_plan = owner_purge_plan(
            db_session, EmbeddedImageOwnerKind.COLLECTION, col.id, _body(_doc_url(doc.id, _A))
        )

        assert doc_plan.release == ()
        assert doc_plan.protected == (_A,)
        assert col_plan.release == ()
        assert col_plan.protected == ()

    def test_owner_has_images_reflects_claims_and_retirements(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        assert not owner_has_images(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id)

        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        assert owner_has_images(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id)

        sync_owner_body(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, "")
        db_session.commit()
        assert owner_has_images(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id)


class TestFinalizeOwnerPurge:
    def test_marks_retirements_released_and_drops_only_their_claims(
        self, db_session: Session
    ) -> None:
        owner = build_document(db_session, "owner", body="")
        other = build_document(db_session, "other", body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            owner.id,
            _body(_doc_url(owner.id, _A)),
        )
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            other.id,
            _body(_doc_url(owner.id, _B)),
        )
        sync_owner_body(db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id, "")
        db_session.commit()

        finalize_owner_purge(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id, [_A]
        )
        db_session.commit()

        retirement = db_session.exec(
            select(EmbeddedImageRetirement).where(
                EmbeddedImageRetirement.name == _A
            )
        ).one()
        assert retirement.state is EmbeddedImageRetirementState.RELEASED
        assert retirement.released_at is not None
        # The protected image keeps the other referrer's claim as evidence.
        remaining = _claim_keys(db_session)
        assert remaining == {
            ("document", owner.id, "document", other.id, _B),
        }

    def test_keeps_a_cross_referrers_claim_on_a_released_name(
        self, db_session: Session
    ) -> None:
        # The cleanup/re-reference interleave under read-committed isolation:
        # another referrer's claim lands between the purge decision and the
        # ledger settlement. Finalize must not delete it — the delete-intent
        # fence is what re-checks the claim before releasing the bytes.
        owner = build_document(db_session, "owner", body="")
        late = build_document(db_session, "late-referrer", body="")
        build_embedded_image_retirement(
            db_session,
            owner_kind=EmbeddedImageOwnerKind.DOCUMENT,
            owner_id=owner.id,
            name=_A,
        )
        build_embedded_image_claim(
            db_session,
            owner_kind=EmbeddedImageOwnerKind.DOCUMENT,
            owner_id=owner.id,
            referrer_kind=EmbeddedImageOwnerKind.DOCUMENT,
            referrer_id=late.id,
            name=_A,
        )
        db_session.commit()

        finalize_owner_purge(
            db_session, EmbeddedImageOwnerKind.DOCUMENT, owner.id, [_A]
        )
        db_session.commit()

        assert _claim_keys(db_session) == {
            ("document", owner.id, "document", late.id, _A),
        }
        retirement = db_session.exec(
            select(EmbeddedImageRetirement).where(
                EmbeddedImageRetirement.name == _A
            )
        ).one()
        assert retirement.state is EmbeddedImageRetirementState.RELEASED


class TestReconcile:
    def test_adds_claims_for_a_body_changed_without_the_sync_seam(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body=_body(_doc_url(5, _A)))

        stats = reconcile_embedded_image_references(db_session)
        db_session.commit()

        assert stats.claims_added == 1
        assert ("document", 5, "document", doc.id, _A) in _claim_keys(db_session)

    def test_removes_stale_claims_after_a_body_removed_a_reference(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        db_session.commit()
        doc.body = ""
        db_session.add(doc)
        db_session.commit()

        stats = reconcile_embedded_image_references(db_session)
        db_session.commit()

        assert stats.claims_removed == 1
        assert _claim_keys(db_session) == set()

    def test_reclaims_a_retirement_when_the_body_carries_the_reference_again(
        self, db_session: Session
    ) -> None:
        doc = build_document(db_session, body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            doc.id,
            _body(_doc_url(doc.id, _A)),
        )
        sync_owner_body(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id, "")
        db_session.commit()
        doc.body = _body(_doc_url(doc.id, _A))
        db_session.add(doc)
        db_session.commit()

        stats = reconcile_embedded_image_references(db_session)
        db_session.commit()

        assert stats.retirements_reclaimed == 1
        assert pending_retirements(db_session, EmbeddedImageOwnerKind.DOCUMENT, doc.id) == []

    def test_drops_claims_of_a_hard_deleted_referrer(self, db_session: Session) -> None:
        owner = build_document(db_session, "owner", body="")
        gone = build_document(db_session, "gone", body="")
        sync_owner_body(
            db_session,
            EmbeddedImageOwnerKind.DOCUMENT,
            gone.id,
            _body(_doc_url(owner.id, _A)),
        )
        db_session.commit()
        db_session.delete(gone)
        db_session.commit()

        reconcile_embedded_image_references(db_session)
        db_session.commit()

        assert _claim_keys(db_session) == set()

    def test_is_idempotent_on_a_second_pass(self, db_session: Session) -> None:
        build_document(db_session, body=_body(_doc_url(1, _A), _col_url(2, _B)))
        build_collection(db_session, name="c", slug="c", path="c")

        first = reconcile_embedded_image_references(db_session)
        db_session.commit()
        second = reconcile_embedded_image_references(db_session)
        db_session.commit()

        assert (
            second.claims_added,
            second.claims_removed,
            second.retirements_reclaimed,
        ) == (0, 0, 0)
        assert first.referrers_scanned >= 1

    def test_single_pass_reaches_marker_rows_after_stale_claim_rows_leave(
        self, db_session: Session
    ) -> None:
        # Converging a stale-claim-only row drops it from the candidate set.
        # Keyset paging must still reach every later marker row in this same
        # pass; an offset page would skip them and miss the cross-namespace
        # claim the purge decision is about to need.
        stale = [
            build_document(db_session, f"stale-{index}", body="no image marker")
            for index in range(9)
        ]
        for doc in stale:
            build_embedded_image_claim(
                db_session,
                owner_kind=EmbeddedImageOwnerKind.DOCUMENT,
                owner_id=900000 + doc.id,
                referrer_id=doc.id,
                name="f" * 64 + ".png",
            )
        marker = build_document(db_session, "marker", body=_body(_doc_url(555001, _A)))
        db_session.commit()

        reconcile_embedded_image_references(db_session, page_size=4)
        db_session.commit()

        assert ("document", 555001, "document", marker.id, _A) in _claim_keys(
            db_session
        )


class TestReclaimCrossReference:
    def test_leaves_a_trashed_collection_repair_unresolved(
        self, db_session: Session
    ) -> None:
        collection = build_collection(db_session, name="Guides", slug="guides", path="g")
        collection.deleted_at = utcnow()
        db_session.add(collection)
        db_session.commit()

        resolved = reclaim_cross_reference(
            db_session,
            owner_kind=EmbeddedImageOwnerKind.DOCUMENT,
            owner_id=71,
            referrer_kind=EmbeddedImageOwnerKind.COLLECTION,
            referrer_id=collection.id,
            name=_A,
        )

        assert resolved is False
