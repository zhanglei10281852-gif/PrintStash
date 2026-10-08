"""The single source of truth for "which blobs does the database still own?".

Backup manifests and vault audits both need a complete, identical answer to
this question.  The census is deliberately read-only: destructive maintenance
never treats absence from this snapshot as evidence that a discovered file is
safe to delete.  The census used to cover ``File.path`` alone, which made every
Document blob appear unowned.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.db.models import (
    SENTINEL_FILE_HASH,
    ArtifactDerivative,
    Collection,
    Document,
    EmbeddedImageOwnerKind,
    EmbeddedImageRetirement,
    EmbeddedImageRetirementState,
    File,
    ModelSourceCover,
    MultipartModel,
)
from app.modules.library.embedded_images import extract_refs
from app.modules.storage.storage_backend.contracts import StorageBackend
from app.modules.storage.storage_backend.runtime import get_backend


@dataclass(frozen=True)
class OwnedBlob:
    key: str
    resource_type: str
    resource_id: int
    expected_size: int | None = None
    expected_sha256: str | None = None
    display_name: str | None = None


@dataclass(frozen=True)
class EmbeddedReference:
    """One current cross-namespace reference: a referrer body embeds an image
    living in a different owner namespace."""

    owner_kind: EmbeddedImageOwnerKind
    owner_id: int
    name: str
    referrer_kind: EmbeddedImageOwnerKind
    referrer_id: int


@dataclass
class StorageOwnershipSnapshot:
    primary: list[OwnedBlob] = field(default_factory=list)
    external: list[OwnedBlob] = field(default_factory=list)
    derived: list[OwnedBlob] = field(default_factory=list)
    # Images a current body references — by the owner itself or by another
    # document/collection. Removing the reference moves the image to
    # ``retiring``; neither list is derived from storage discovery.
    embedded: list[OwnedBlob] = field(default_factory=list)
    # Images an owner explicitly removed via a precise body edit. They stay
    # owned and recoverable until the precise owner is permanently deleted.
    retiring: list[OwnedBlob] = field(default_factory=list)
    cross_references: list[EmbeddedReference] = field(default_factory=list)
    # Content-hash image names appearing under two or more owner namespaces —
    # physically distinct objects that backup/audit must never conflate.
    shared_names: dict[str, list[tuple[str, int]]] = field(default_factory=dict)
    discovered_keys: set[str] = field(default_factory=set)

    @property
    def claimed_keys(self) -> set[str]:
        return {
            blob.key
            for group in (self.primary, self.derived, self.embedded, self.retiring)
            for blob in group
        }


def _embedded_blob(
    backend: StorageBackend,
    owner_kind: EmbeddedImageOwnerKind,
    owner_id: int,
    name: str,
) -> OwnedBlob:
    if owner_kind is EmbeddedImageOwnerKind.DOCUMENT:
        key = backend.document_image_key(owner_id, name)
        resource_type = "document_image"
    else:
        key = backend.collection_image_key(owner_id, name)
        resource_type = "collection_image"
    return OwnedBlob(
        key=key, resource_type=resource_type, resource_id=owner_id, display_name=name
    )


def _collect_embedded(
    backend: StorageBackend,
    documents: list[Document],
    collections: list[Collection],
) -> tuple[
    list[OwnedBlob],
    list[EmbeddedReference],
    dict[str, list[tuple[str, int]]],
]:
    """Current image references from every body, including cross-namespace ones.

    Returns the distinct owned blobs, the cross-namespace references, and the
    names shared across owner namespaces.
    """
    referrers: tuple[tuple[EmbeddedImageOwnerKind, int, str | None], ...] = tuple(
        (EmbeddedImageOwnerKind.DOCUMENT, int(row.id), row.body)
        for row in documents
        if row.id is not None
    ) + tuple(
        (EmbeddedImageOwnerKind.COLLECTION, int(row.id), row.readme)
        for row in collections
        if row.id is not None
    )
    current: dict[
        tuple[EmbeddedImageOwnerKind, int, str],
        set[tuple[EmbeddedImageOwnerKind, int]],
    ] = {}
    for referrer_kind, referrer_id, text in referrers:
        for ref in extract_refs(text):
            current.setdefault(
                (ref.owner_kind, ref.owner_id, ref.name), set()
            ).add((referrer_kind, referrer_id))

    embedded: list[OwnedBlob] = []
    cross_references: list[EmbeddedReference] = []
    owners_by_name: dict[str, set[tuple[EmbeddedImageOwnerKind, int]]] = {}
    for (owner_kind, owner_id, name), referees in sorted(
        current.items(), key=lambda item: (item[0][0].value, item[0][1], item[0][2])
    ):
        embedded.append(_embedded_blob(backend, owner_kind, owner_id, name))
        owners_by_name.setdefault(name, set()).add((owner_kind, owner_id))
        for referrer_kind, referrer_id in sorted(
            referees, key=lambda item: (item[0].value, item[1])
        ):
            if referrer_kind is not owner_kind or referrer_id != owner_id:
                cross_references.append(
                    EmbeddedReference(
                        owner_kind=owner_kind,
                        owner_id=owner_id,
                        name=name,
                        referrer_kind=referrer_kind,
                        referrer_id=referrer_id,
                    )
                )
    shared_names = {
        name: sorted((kind.value, owner_id) for kind, owner_id in owners)
        for name, owners in owners_by_name.items()
        if len(owners) >= 2
    }
    return embedded, cross_references, shared_names


def ownership_snapshot(
    session: Session, *, discover: bool = True, backend: StorageBackend | None = None
) -> StorageOwnershipSnapshot:
    """Typed census for audit/backup; never used to widen trash deletion."""
    backend = backend or get_backend()
    result = StorageOwnershipSnapshot()

    files = list(session.exec(select(File)).all())
    generation_keys: dict[int, set[str]] = {}
    for derivative in session.exec(select(ArtifactDerivative)).all():
        if derivative.storage_key:
            generation_keys.setdefault(derivative.file_id, set()).add(
                derivative.storage_key
            )
    for row in files:
        if row.id is None:
            continue
        # External print jobs use a database-only placeholder whose path is
        # /dev/null. It is not a vault blob and must never enter backup,
        # restore, or audit ownership sets. Match the reserved hash as well as
        # the path so a real missing vault artifact is still surfaced by
        # backup.stat_size() rather than silently omitted.
        if row.path == "/dev/null" and row.sha256 == SENTINEL_FILE_HASH:
            continue
        blob = OwnedBlob(
            key=row.path,
            resource_type="file",
            resource_id=row.id,
            expected_size=row.size_bytes,
            expected_sha256=row.sha256,
            display_name=row.original_filename,
        )
        (result.external if row.is_external else result.primary).append(blob)
        current_thumbnail = row.thumbnail_path or backend.thumbnail_key(row.id)
        result.derived.append(
            OwnedBlob(
                key=current_thumbnail,
                resource_type="thumbnail",
                resource_id=row.id,
                display_name=row.original_filename,
            )
        )
        if current_thumbnail != backend.thumbnail_key(row.id):
            result.derived.append(
                OwnedBlob(
                    key=backend.thumbnail_key(row.id),
                    resource_type="legacy_webp_thumbnail",
                    resource_id=row.id,
                    display_name=row.original_filename,
                )
            )
        for generation_key in sorted(generation_keys.get(row.id, set())):
            if generation_key == current_thumbnail:
                continue
            result.derived.append(
                OwnedBlob(
                    key=generation_key,
                    resource_type="artifact_derivative",
                    resource_id=row.id,
                    display_name=row.original_filename,
                )
            )
        result.derived.append(
            OwnedBlob(
                key=backend.legacy_thumbnail_key(row.id),
                resource_type="legacy_thumbnail",
                resource_id=row.id,
                display_name=row.original_filename,
            )
        )
        if row.sha256:
            result.derived.append(
                OwnedBlob(
                    key=backend.stl_cache_key(row.sha256),
                    resource_type="stl_cache",
                    resource_id=row.id,
                    display_name=row.original_filename,
                )
            )

    documents = list(session.exec(select(Document)).all())
    for row in documents:
        if row.id is None:
            continue
        if row.filename:
            result.primary.append(
                OwnedBlob(
                    key=backend.document_file_key(row.id, row.filename),
                    resource_type="document",
                    resource_id=row.id,
                    expected_size=row.size_bytes,
                    expected_sha256=row.sha256,
                    display_name=row.filename,
                )
            )
    collections = list(session.exec(select(Collection)).all())
    embedded, cross_references, shared_names = _collect_embedded(
        backend, documents, collections
    )
    result.embedded = embedded
    result.cross_references = cross_references
    result.shared_names = shared_names

    # Pending retirements are still owned: backed up, audited and claimed, so
    # discovery can never call the bytes unowned. A retirement stays visible
    # even when another referrer currently embeds the image — that is exactly
    # the case where purge preserves the bytes and an operator needs to see
    # why. Rows whose owner was hard-deleted are included by deterministic key
    # so post-purge protection evidence stays visible.
    for row in session.exec(
        select(EmbeddedImageRetirement).where(
            EmbeddedImageRetirement.state == EmbeddedImageRetirementState.PENDING
        )
    ).all():
        result.retiring.append(
            _embedded_blob(backend, row.owner_kind, row.owner_id, row.name)
        )

    # Provenance covers are vault-owned blobs even though they are not attached
    # to a File.  Keep them in the census so backup/restore cannot silently lose
    # the private representative image.
    for row in session.exec(select(ModelSourceCover)).all():
        result.primary.append(
            OwnedBlob(
                key=row.storage_key,
                resource_type="model_source_cover",
                resource_id=row.id or 0,
                expected_size=row.size_bytes,
                display_name="source-cover.webp",
            )
        )
    for row in session.exec(select(MultipartModel)).all():
        if row.id is None or row.cover_filename is None:
            continue
        result.primary.append(
            OwnedBlob(
                key=backend.multipart_model_cover_key(row.id, row.cover_filename),
                resource_type="multipart_model_cover",
                resource_id=row.id,
                expected_size=row.cover_size_bytes,
                display_name="multipart-cover.webp",
            )
        )
    if discover:
        result.discovered_keys.update(backend.walk_keys())
        # Local storage keeps derived objects under a separate root. S3's
        # default walk already covers every vault-data prefix.
        if backend.direct_path(backend.thumbnail_key(0)) is not None:
            from pathlib import Path

            result.discovered_keys.update(
                backend.walk_keys(str(Path(backend.thumbnail_key(0)).parent))
            )
    return result


def all_owned_blob_keys(session: Session) -> set[str]:
    """Every storage key a DB row lays claim to, across all owning tables.

    Trashed rows are included on purpose: their bytes must survive until the
    row is hard-deleted, otherwise restoring from trash yields an empty file.

    Includes derived, embedded and retiring keys for backup/audit completeness.
    External paths remain included for compatibility even though their bytes
    are always user-owned.
    """
    snapshot = ownership_snapshot(session, discover=False)
    # Compatibility contract: external File.path values historically appeared
    # here even though trash and backup callers separately avoid deleting them.
    return snapshot.claimed_keys | {blob.key for blob in snapshot.external}
