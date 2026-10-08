"""Recoverable lifecycle for images embedded in markdown bodies.

Body references (``Document.body`` / ``Collection.readme``), physical image
ownership, trash/restore, backup manifests, vault audit and garbage collection
meet here. The rules this module enforces:

* A successful body edit records its *exact* self-reference removal set as
  observable ``pending`` retirements, in the same transaction as the body.
* Claims are reconvergeable: a lost edit, a duplicate request or a restart can
  recompute every claim from the current bodies; old and new references both
  converge. Reconciliation reclaims and deletes ledger rows — never bytes.
* An image stays owned by its document/collection while that owner is trashed.
* Physical release is authorized only when the precise owner is permanently
  deleted and no other referrer still claims the image.
* An image that was uploaded but never referenced has no claim and no
  retirement, preserving the historical "never delete discovered objects"
  compatibility behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, cast

from sqlalchemy import ColumnElement, delete
from sqlalchemy import exists as sa_exists
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.time import utcnow
from app.db.models import (
    Collection,
    Document,
    EmbeddedImageClaim,
    EmbeddedImageOwnerKind,
    EmbeddedImageRetirement,
    EmbeddedImageRetirementState,
)

# The image upload routes only ever publish ``<sha256>.<ext>`` names. The
# reference patterns keep the storage census' permissive name class (a relative
# mention or an old, non-servable name still counts as a reference for
# missing-image detection); lifecycle transitions only release names this
# pattern proves the routes could have created.
_IMAGE_PATH = r"/images/([^\s)\]?#]+)"
DOCUMENT_IMAGE_RE = re.compile(rf"(?:/api/v1)?/documents/(\d+){_IMAGE_PATH}")
COLLECTION_IMAGE_RE = re.compile(rf"(?:/api/v1)?/collections/(\d+){_IMAGE_PATH}")
SERVABLE_IMAGE_NAME_RE = re.compile(r"[0-9a-f]{64}\.(?:png|jpe?g|gif|webp)$")

_RECONCILE_PAGE = 500


@dataclass(frozen=True)
class EmbeddedImageRef:
    """One image reference parsed from a referrer body."""

    owner_kind: EmbeddedImageOwnerKind
    owner_id: int
    name: str

    @property
    def claim_key(self) -> tuple[str, int, str]:
        return self.owner_kind.value, self.owner_id, self.name


@dataclass(frozen=True)
class EmbeddedImageChange:
    """Observable result of syncing one owner body with its reference ledger."""

    current: tuple[str, ...]
    retired: tuple[str, ...]
    reclaimed: tuple[str, ...]
    claims_added: int
    claims_removed: int


@dataclass(frozen=True)
class EmbeddedImagePurgePlan:
    """Names the precise owner may release, and names other referrers protect."""

    release: tuple[str, ...]
    protected: tuple[str, ...]


@dataclass(frozen=True)
class ReconcileStats:
    referrers_scanned: int
    claims_added: int
    claims_removed: int
    retirements_reclaimed: int

def extract_refs(text: str | None) -> frozenset[EmbeddedImageRef]:
    """Every document/collection image reference in one markdown body."""
    if not text:
        return frozenset()
    refs = {
        EmbeddedImageRef(EmbeddedImageOwnerKind.DOCUMENT, int(doc_id), name)
        for doc_id, name in DOCUMENT_IMAGE_RE.findall(text)
    }
    refs.update(
        EmbeddedImageRef(EmbeddedImageOwnerKind.COLLECTION, int(col_id), name)
        for col_id, name in COLLECTION_IMAGE_RE.findall(text)
    )
    return frozenset(refs)


def _is_self(ref: EmbeddedImageRef, kind: EmbeddedImageOwnerKind, owner_id: int) -> bool:
    return ref.owner_kind is kind and ref.owner_id == owner_id


def _self_names(refs: Iterable[EmbeddedImageRef], kind: EmbeddedImageOwnerKind, owner_id: int) -> set[str]:
    return {ref.name for ref in refs if _is_self(ref, kind, owner_id)}


def _existing_claims(
    session: Session, kind: EmbeddedImageOwnerKind, referrer_id: int
) -> list[EmbeddedImageClaim]:
    return list(
        session.exec(
            select(EmbeddedImageClaim).where(
                EmbeddedImageClaim.referrer_kind == kind,
                EmbeddedImageClaim.referrer_id == referrer_id,
            )
        ).all()
    )


def _record_retirement(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    owner_id: int,
    name: str,
    *,
    now: datetime,
) -> None:
    """Open one pending retirement unless one is already open.

    The insert runs in a savepoint so a duplicate request racing on another
    connection (PostgreSQL read-committed) hits the partial unique index
    inside a converging no-op rather than failing the caller's edit.
    """
    existing = session.exec(
        select(EmbeddedImageRetirement.id).where(
            EmbeddedImageRetirement.owner_kind == kind,
            EmbeddedImageRetirement.owner_id == owner_id,
            EmbeddedImageRetirement.name == name,
            EmbeddedImageRetirement.state == EmbeddedImageRetirementState.PENDING,
        )
    ).first()
    if existing is not None:
        return
    try:
        with session.begin_nested():
            session.add(
                EmbeddedImageRetirement(
                    owner_kind=kind,
                    owner_id=owner_id,
                    name=name,
                    state=EmbeddedImageRetirementState.PENDING,
                    removed_at=now,
                )
            )
    except IntegrityError:
        # The concurrent duplicate request won the same pending row: the
        # post-savepoint state is exactly what this call wanted.
        return


def _reclaim_retirements(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    owner_id: int,
    names: Iterable[str],
    *,
    now: datetime,
) -> set[str]:
    """Reclaim every pending retirement for a name the body references again."""
    reclaimed: set[str] = set()
    for name in names:
        pending = session.exec(
            select(EmbeddedImageRetirement).where(
                EmbeddedImageRetirement.owner_kind == kind,
                EmbeddedImageRetirement.owner_id == owner_id,
                EmbeddedImageRetirement.name == name,
                EmbeddedImageRetirement.state == EmbeddedImageRetirementState.PENDING,
            )
        ).first()
        if pending is None:
            continue
        pending.state = EmbeddedImageRetirementState.RECLAIMED
        pending.reclaimed_at = now
        session.add(pending)
        reclaimed.add(name)
    return reclaimed


def sync_referrer(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    referrer_id: int,
    text: str | None,
    *,
    now: datetime | None = None,
) -> tuple[frozenset[EmbeddedImageRef], int, int]:
    """Converge one referrer's claims with its current body. Caller commits.

    Returns ``(desired_refs, added_count, removed_count)``. Never touches
    retirements or storage bytes: a stale claim deletion is ledger-only.
    """
    now = now or utcnow()
    desired = extract_refs(text)
    desired_keys = {ref.claim_key for ref in desired}
    existing = _existing_claims(session, kind, referrer_id)
    removed = 0
    for claim in existing:
        if (claim.owner_kind.value, claim.owner_id, claim.name) not in desired_keys:
            session.delete(claim)
            removed += 1
    present = {
        (claim.owner_kind, claim.owner_id, claim.name) for claim in existing
    }
    added = 0
    for ref in desired:
        if (ref.owner_kind, ref.owner_id, ref.name) in present:
            continue
        try:
            with session.begin_nested():
                session.add(
                    EmbeddedImageClaim(
                        owner_kind=ref.owner_kind,
                        owner_id=ref.owner_id,
                        referrer_kind=kind,
                        referrer_id=referrer_id,
                        name=ref.name,
                        last_seen_at=now,
                    )
                )
        except IntegrityError:
            # A duplicate request racing on another connection already
            # recorded this exact claim; the converged state is unchanged.
            continue
        added += 1
    return desired, added, removed


def sync_owner_body(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    owner_id: int,
    text: str | None,
    *,
    now: datetime | None = None,
) -> EmbeddedImageChange:
    """Record the exact reference change of one successful owner body edit.

    Runs entirely inside the caller's transaction: a failed body update rolls
    claims and retirements back together with the body. Self-references removed
    here become pending retirements; any self-reference the body carries again
    reclaims a pending retirement (the operation is idempotent convergence).
    """
    now = now or utcnow()
    previous_self = {
        claim.name
        for claim in _existing_claims(session, kind, owner_id)
        if claim.owner_kind is kind and claim.owner_id == owner_id
    }
    desired, added, removed = sync_referrer(session, kind, owner_id, text, now=now)
    current_self = _self_names(desired, kind, owner_id)
    retired = previous_self - current_self
    for name in sorted(retired):
        _record_retirement(session, kind, owner_id, name, now=now)
    reclaimed = _reclaim_retirements(session, kind, owner_id, current_self, now=now)
    return EmbeddedImageChange(
        current=tuple(sorted(current_self)),
        retired=tuple(sorted(retired)),
        reclaimed=tuple(sorted(reclaimed)),
        claims_added=added,
        claims_removed=removed,
    )


def pending_retirements(
    session: Session, kind: EmbeddedImageOwnerKind, owner_id: int
) -> list[EmbeddedImageRetirement]:
    return list(
        session.exec(
            select(EmbeddedImageRetirement)
            .where(
                EmbeddedImageRetirement.owner_kind == kind,
                EmbeddedImageRetirement.owner_id == owner_id,
                EmbeddedImageRetirement.state == EmbeddedImageRetirementState.PENDING,
            )
            .order_by(
                EmbeddedImageRetirement.removed_at.asc(),  # type: ignore[attr-defined]
                EmbeddedImageRetirement.id.asc(),  # type: ignore[attr-defined]
            )
        ).all()
    )


def _self_claim_names(
    session: Session, kind: EmbeddedImageOwnerKind, owner_id: int
) -> set[str]:
    return set(
        session.exec(
            select(EmbeddedImageClaim.name).where(
                EmbeddedImageClaim.owner_kind == kind,
                EmbeddedImageClaim.owner_id == owner_id,
                EmbeddedImageClaim.referrer_kind == kind,
                EmbeddedImageClaim.referrer_id == owner_id,
            )
        ).all()
    )


def _other_referrer_names(
    session: Session, kind: EmbeddedImageOwnerKind, owner_id: int
) -> set[str]:
    """Names in this owner's namespace claimed by a *different* referrer."""
    return set(
        session.exec(
            select(EmbeddedImageClaim.name).where(
                EmbeddedImageClaim.owner_kind == kind,
                EmbeddedImageClaim.owner_id == owner_id,
                (EmbeddedImageClaim.referrer_kind != kind)
                | (EmbeddedImageClaim.referrer_id != owner_id),
            )
        ).all()
    )


def owner_purge_plan(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    owner_id: int,
    text: str | None,
) -> EmbeddedImagePurgePlan:
    """Exact release decision for an owner about to be permanently deleted.

    The owner's own claims are first reconverged from its final body. Only
    servable names it currently references or explicitly retired can release,
    and any name another referrer still claims is protected.
    """
    sync_owner_body(session, kind, owner_id, text)
    candidates = _self_claim_names(session, kind, owner_id) | {
        row.name for row in pending_retirements(session, kind, owner_id)
    }
    candidates = {name for name in candidates if SERVABLE_IMAGE_NAME_RE.match(name)}
    protected = _other_referrer_names(session, kind, owner_id) & candidates
    return EmbeddedImagePurgePlan(
        release=tuple(sorted(candidates - protected)),
        protected=tuple(sorted(protected)),
    )


def owner_has_images(
    session: Session, kind: EmbeddedImageOwnerKind, owner_id: int
) -> bool:
    """Whether this owner currently references or has retired owned images."""
    current = session.exec(
        select(EmbeddedImageClaim.id).where(
            EmbeddedImageClaim.owner_kind == kind,
            EmbeddedImageClaim.owner_id == owner_id,
            EmbeddedImageClaim.referrer_kind == kind,
            EmbeddedImageClaim.referrer_id == owner_id,
        )
    ).first()
    if current is not None:
        return True
    pending = session.exec(
        select(EmbeddedImageRetirement.id).where(
            EmbeddedImageRetirement.owner_kind == kind,
            EmbeddedImageRetirement.owner_id == owner_id,
            EmbeddedImageRetirement.state == EmbeddedImageRetirementState.PENDING,
        )
    ).first()
    return pending is not None


def finalize_owner_purge(
    session: Session,
    kind: EmbeddedImageOwnerKind,
    owner_id: int,
    released_names: Iterable[str],
    *,
    now: datetime | None = None,
) -> None:
    """Settle the ledger after purge enqueued exact delete authorizations.

    Released retirements become ``released`` evidence and the deleted owner's
    own referrer claims are removed. Another referrer's claim on a released
    name is deliberately kept: under read-committed isolation it can land
    between the purge decision and this settlement, and it is the durable
    evidence the delete-intent fence re-checks before releasing the bytes.
    Such a surviving claim either blocks the physical delete (bytes preserved
    and observably blocked) or explains a later missing-image finding; it is
    never deleted on the purged owner's authority alone.
    """
    now = now or utcnow()
    released = set(released_names)
    for name in released:
        for pending in pending_retirements(session, kind, owner_id):
            if pending.name != name:
                continue
            pending.state = EmbeddedImageRetirementState.RELEASED
            pending.released_at = now
            session.add(pending)
    session.exec(
        delete(EmbeddedImageClaim).where(
            cast(
                "ColumnElement[bool]",
                EmbeddedImageClaim.referrer_kind == kind,
            ),
            cast(
                "ColumnElement[bool]",
                EmbeddedImageClaim.referrer_id == owner_id,
            ),
        )
    )


def _delete_claims_of_missing_referrers(session: Session) -> int:
    """Drop claims whose referrer row was hard-deleted. A vanished referrer
    has no body left that could read the image; protected cross-reference
    evidence is the surviving owner rows."""
    removed = 0
    result = session.exec(
        delete(EmbeddedImageClaim).where(
            cast(
                "ColumnElement[bool]",
                EmbeddedImageClaim.referrer_kind == EmbeddedImageOwnerKind.DOCUMENT,
            ),
            cast(
                "ColumnElement[bool]",
                ~sa_exists().where(
                    cast(
                        "ColumnElement[bool]",
                        Document.id == EmbeddedImageClaim.referrer_id,
                    )
                ),
            ),
        )
    )
    removed += int(result.rowcount or 0)
    result = session.exec(
        delete(EmbeddedImageClaim).where(
            cast(
                "ColumnElement[bool]",
                EmbeddedImageClaim.referrer_kind == EmbeddedImageOwnerKind.COLLECTION,
            ),
            cast(
                "ColumnElement[bool]",
                ~sa_exists().where(
                    cast(
                        "ColumnElement[bool]",
                        Collection.id == EmbeddedImageClaim.referrer_id,
                    )
                ),
            ),
        )
    )
    removed += int(result.rowcount or 0)
    return removed


def _body_text(kind: EmbeddedImageOwnerKind, row) -> str | None:
    return row.body if kind is EmbeddedImageOwnerKind.DOCUMENT else row.readme


def reconcile_embedded_image_references(
    session: Session,
    *,
    page_size: int = _RECONCILE_PAGE,
) -> ReconcileStats:
    """Restart-safe convergence between bodies and the reference ledger.

    Only referrers that can possibly need convergence are scanned: rows whose
    text carries an ``/images/`` marker or rows that already own a claim. The
    set is exact — a row without a marker and without a claim has an empty
    reference set — so the pass stays bounded without a change timestamp,
    which collections do not have. Claims of hard-deleted referrers are
    removed afterwards. The pass never invents retirements and never deletes
    bytes: bodies are the source of truth for claims; explicit edits own
    retirements.
    """
    now = utcnow()
    scanned = added = removed = reclaimed = 0

    def page(
        kind: EmbeddedImageOwnerKind, model: Any, text_column: Any
    ) -> None:
        # Keyset (id > last_id), never OFFSET: converging a row can delete its
        # stale claims and so drop it out of the candidate set mid-pass; an
        # offset page would then skip the rows that shifted forward, leaving a
        # new cross-namespace claim invisible to that pass's purge decision.
        nonlocal scanned, added, removed, reclaimed
        has_claim = sa_exists().where(
            cast(
                "ColumnElement[bool]",
                EmbeddedImageClaim.referrer_kind == kind,
            ),
            EmbeddedImageClaim.referrer_id == model.id,
        )
        candidate = cast(
            ColumnElement[bool], text_column.like("%/images/%") | has_claim
        )
        last_id = 0
        while True:
            rows = session.exec(
                select(model)
                .where(candidate, cast(ColumnElement[bool], model.id > last_id))
                .order_by(model.id.asc())
                .limit(page_size)
            ).all()
            if not rows:
                return
            for row in rows:
                change = sync_owner_body(
                    session, kind, int(row.id), _body_text(kind, row), now=now
                )
                scanned += 1
                added += change.claims_added
                removed += change.claims_removed
                reclaimed += len(change.reclaimed)
            last_id = int(rows[-1].id)
            if len(rows) < page_size:
                return

    page(EmbeddedImageOwnerKind.DOCUMENT, Document, Document.body)
    page(EmbeddedImageOwnerKind.COLLECTION, Collection, Collection.readme)
    removed += _delete_claims_of_missing_referrers(session)
    return ReconcileStats(
        referrers_scanned=scanned,
        claims_added=added,
        claims_removed=removed,
        retirements_reclaimed=reclaimed,
    )


def _owner_path(kind: EmbeddedImageOwnerKind, owner_id: int, name: str) -> str:
    plural = "documents" if kind is EmbeddedImageOwnerKind.DOCUMENT else "collections"
    return f"/api/v1/{plural}/{owner_id}/images/{name}"


def reclaim_cross_reference(
    session: Session,
    *,
    owner_kind: EmbeddedImageOwnerKind,
    owner_id: int,
    referrer_kind: EmbeddedImageOwnerKind,
    referrer_id: int,
    name: str,
) -> bool:
    """Repair action: copy a cross-namespace image into the referrer's own
    namespace and rewrite its body to the self-owned URL.

    Returns False (leaving the audit finding open) when the referrer is gone
    or trashed, the body no longer carries the reference, or the source bytes
    cannot be read and published. Names are content hashes, so a destination
    that already holds identical owned bytes is reused without copying.
    """
    from app.db.projections import content_changed
    from app.modules.storage.storage_backend.runtime import get_backend
    from app.modules.storage.storage_ownership import (
        adopt_publication,
        matching_creation_receipt,
        prepare_bytes,
    )

    if not SERVABLE_IMAGE_NAME_RE.match(name):
        return False
    if referrer_kind is EmbeddedImageOwnerKind.DOCUMENT:
        referrer = session.exec(
            select(Document).where(
                Document.id == referrer_id,
                Document.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
    else:
        referrer = session.exec(
            select(Collection).where(
                Collection.id == referrer_id,
                Collection.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        ).first()
    if referrer is None:
        return False
    text = referrer.body if isinstance(referrer, Document) else referrer.readme
    text = text or ""

    owner_plural = "documents" if owner_kind is EmbeddedImageOwnerKind.DOCUMENT else "collections"
    old_pattern = re.compile(
        rf"(?:/api/v1)?/{owner_plural}/{owner_id}/images/{re.escape(name)}"
    )
    self_url = _owner_path(referrer_kind, referrer_id, name)
    new_text, count = old_pattern.subn(self_url, text)
    if count == 0:
        return False

    backend = get_backend()
    source_key = (
        backend.document_image_key(owner_id, name)
        if owner_kind is EmbeddedImageOwnerKind.DOCUMENT
        else backend.collection_image_key(owner_id, name)
    )
    target_key = (
        backend.document_image_key(referrer_id, name)
        if referrer_kind is EmbeddedImageOwnerKind.DOCUMENT
        else backend.collection_image_key(referrer_id, name)
    )
    if source_key != target_key and matching_creation_receipt(
        session, backend, target_key
    ) is None:
        if not backend.exists(source_key):
            return False
        data = backend.read_bytes(source_key)
        candidate = prepare_bytes(
            session,
            backend,
            target_key,
            data,
            object_kind=(
                "document_image"
                if referrer_kind is EmbeddedImageOwnerKind.DOCUMENT
                else "collection_image"
            ),
        )
        adopt_publication(session, candidate)

    if isinstance(referrer, Document):
        referrer.body = new_text
    else:
        referrer.readme = new_text
    referrer.updated_at = utcnow()
    session.add(referrer)
    sync_owner_body(session, referrer_kind, referrer_id, new_text)
    content_changed(
        session,
        "document" if referrer_kind is EmbeddedImageOwnerKind.DOCUMENT else "collection",
        [referrer_id],
    )
    return True
