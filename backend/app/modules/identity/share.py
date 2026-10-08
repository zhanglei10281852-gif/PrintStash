"""Public share-link service.

The trust boundary: a share token is a bearer capability for *one* model. Every
public request funnels through :func:`resolve_share_access`, which returns the
link and its resolved pins only when the token exists, is not revoked and has
not expired — and raises a uniform 404 otherwise so tokens/models cannot be
probed for existence.

Two link shapes, never mixed:

* **Model share** (no pins) — the legacy live-model semantics. The detail
  follows the model's live Artifacts and the model must stay live; a new
  revision or cover is meant to appear.
* **Revision share** (one or more pins) — the pins are the exact Artifact
  identities selected at creation. The detail, thumbnail, STL, G-code,
  toolpath and download routes all authorize against the same resolved
  :class:`ShareAccess`, and never fall back to the current live file. Pinned
  Artifacts stay served while trashed, their bytes are held out of trash
  reclamation for the link's lifetime, and a pin whose row is gone reports a
  stable "unavailable" result instead of drifting onto another revision.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, delete, select

from app.core.errors import ErrorKind, OperationError
from app.core.time import utcnow
from app.db.models import (
    File,
    FileType,
    Metadata,
    Model,
    ShareLink,
    ShareLinkPin,
)
from app.db.scopes import live
from app.modules.identity.auth import _as_utc, _token_hash
from app.modules.storage.storage_ownership import UnsafeStorageDeleteError
from app.schemas.share import PublicFileRead, PublicModelRead, ShareLinkRead

_TOKEN_BYTES = 32
_TOKEN_COLLISION_RETRIES = 3

# The public mesh viewer serves these Artifact types; a revision share can pin
# any of them or a G-code revision. DXF sources never had a public route.
MESH_TYPES = frozenset({FileType.STL, FileType.THREE_MF, FileType.OBJ, FileType.STEP})
PINNABLE_TYPES = MESH_TYPES | {FileType.GCODE}


class ShareHoldError(UnsafeStorageDeleteError):
    """An active revision share holds an Artifact a purge tried to reclaim."""

    def __init__(self) -> None:
        super().__init__("share_hold_active")


@dataclass(frozen=True)
class ShareAccess:
    """The one authorization every public share route resolves through."""

    link: ShareLink
    pins: tuple[ShareLinkPin, ...]

    @property
    def is_revision_share(self) -> bool:
        return bool(self.pins)

    def pin_for(self, file_id: int) -> ShareLinkPin | None:
        return next((pin for pin in self.pins if pin.file_id == file_id), None)


def is_active(link: ShareLink) -> bool:
    # expires_at comes back naive from SQLite; normalise before comparing.
    return link.revoked_at is None and _as_utc(link.expires_at) > utcnow()


def active_hold_file_ids_select():
    """File ids currently held by active revision shares (a SQL subquery).

    Callers embed this as a subquery — never bind the id list into SQL — so the
    predicate stays one statement at any library size.
    """
    return (
        select(ShareLinkPin.file_id)
        .join(
            ShareLink, ShareLinkPin.share_link_id == ShareLink.id  # type: ignore[arg-type]
        )
        .where(
            ShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
            ShareLink.expires_at > utcnow(),
        )
    )


def file_is_held(session: Session, file_id: int) -> bool:
    return (
        session.exec(
            select(ShareLinkPin.id)
            .join(
                ShareLink, ShareLinkPin.share_link_id == ShareLink.id  # type: ignore[arg-type]
            )
            .where(
                ShareLinkPin.file_id == file_id,
                ShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
                ShareLink.expires_at > utcnow(),
            )
            .limit(1)
        ).first()
        is not None
    )


def model_is_held(session: Session, model_id: int) -> bool:
    return (
        session.exec(
            select(ShareLinkPin.id)
            .join(
                ShareLink, ShareLinkPin.share_link_id == ShareLink.id  # type: ignore[arg-type]
            )
            .join(
                File, ShareLinkPin.file_id == File.id  # type: ignore[arg-type]
            )
            .where(
                File.model_id == model_id,
                ShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
                ShareLink.expires_at > utcnow(),
            )
            .limit(1)
        ).first()
        is not None
    )


def release_inactive_pins(session: Session, file_id: int) -> None:
    """Drop pins whose links have expired or been revoked before a File purge.

    Pins of links still active must never reach here — :func:`file_is_held`
    guards the purge — so the file's RESTRICT foreign key only keeps genuinely
    active holds.
    """
    active_link_ids = select(ShareLink.id).where(
        ShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
        ShareLink.expires_at > utcnow(),
    )
    session.exec(
        delete(ShareLinkPin)
        .where(ShareLinkPin.file_id == file_id)
        .where(col(ShareLinkPin.share_link_id).not_in(active_link_ids))
    )


def _pin_ids(pins: tuple[ShareLinkPin, ...]) -> list[int]:
    return [pin.file_id for pin in pins]


def to_read(link: ShareLink, pins: tuple[ShareLinkPin, ...] = ()) -> ShareLinkRead:
    return ShareLinkRead(
        id=link.id,  # type: ignore[arg-type]
        model_id=link.model_id,
        expires_at=link.expires_at,
        revoked_at=link.revoked_at,
        allow_download=link.allow_download,
        revision_file_ids=_pin_ids(pins),
        access_count=link.access_count,
        created_at=link.created_at,
        is_active=is_active(link),
    )


def _gcode_revision_numbers(
    session: Session, model_id: int, *, include_trashed: bool
) -> dict[int, int]:
    query = (
        select(File)
        .where(File.model_id == model_id)
        .where(File.file_type == FileType.GCODE)
        .order_by(File.version.asc())  # type: ignore[attr-defined]
    )
    if not include_trashed:
        query = query.where(live(File))
    rows = session.exec(query).all()
    return {f.id: i for i, f in enumerate(rows, start=1) if f.id is not None}


def _snapshot_pins(
    session: Session,
    link_id: int,
    model_id: int,
    files: list[File],
) -> list[ShareLinkPin]:
    revision_numbers = _gcode_revision_numbers(session, model_id, include_trashed=False)
    return [
        ShareLinkPin(
            share_link_id=link_id,
            file_id=file_row.id,  # type: ignore[arg-type]
            sha256=file_row.sha256,
            file_type=file_row.file_type,
            original_filename=file_row.original_filename,
            file_version=file_row.version,
            revision_number=(
                revision_numbers.get(file_row.id)
                if file_row.file_type is FileType.GCODE
                else None
            ),
            is_recommended=file_row.is_recommended,
        )
        for file_row in files
    ]


def _cover_snapshot(session: Session, model: Model) -> tuple[int | None, str | None]:
    if model.thumbnail_file_id is not None:
        cover = session.get(File, model.thumbnail_file_id)
        if cover is not None:
            return int(model.thumbnail_file_id), None
    if model.thumbnail_path:
        return None, model.thumbnail_path
    return None, None


def create_share(
    session: Session,
    *,
    model_id: int,
    expires_in_days: int,
    allow_download: bool,
    revision_file_ids: list[int] | None = None,
    created_by: int | None,
) -> tuple[ShareLink, str]:
    """Create a share link, returning (row, raw_token). Only the hash is stored.

    ``revision_file_ids=None`` mints a model share; a non-empty list of live
    Artifact ids (G-code revisions and/or mesh sources) mints a revision share
    whose pins are the exact, non-drifting identity of what the customer sees.
    """
    raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    days = max(1, min(expires_in_days, 365))

    selected_files: list[File] = []
    cover_file_id: int | None = None
    cover_thumbnail_path: str | None = None
    if revision_file_ids is not None:
        requested = list(dict.fromkeys(revision_file_ids))
        if not requested:
            raise OperationError("no_revisions_selected", kind=ErrorKind.INVALID)
        selected_files = list(
            session.exec(
                select(File)
                .where(File.model_id == model_id)
                .where(live(File))
                .where(
                    File.file_type.in_(tuple(PINNABLE_TYPES))  # type: ignore[attr-defined]
                )
                .where(File.id.in_(requested))  # type: ignore[attr-defined]
            ).all()
        )
        if {f.id for f in selected_files} != set(requested):
            raise OperationError("invalid_revision_file_id", kind=ErrorKind.INVALID)
        selected_files.sort(key=lambda f: requested.index(f.id))

    expires_at = utcnow() + timedelta(days=days)
    for _attempt in range(_TOKEN_COLLISION_RETRIES):
        link = ShareLink(
            model_id=model_id,
            token_hash=_token_hash(raw_token),
            expires_at=expires_at,
            allow_download=allow_download,
            created_by=created_by,
        )
        if revision_file_ids is not None:
            model = session.get(Model, model_id)
            assert model is not None  # the router requires a live model
            cover_file_id, cover_thumbnail_path = _cover_snapshot(session, model)
            link.cover_file_id = cover_file_id
            link.cover_thumbnail_path = cover_thumbnail_path
        session.add(link)
        try:
            # Flush the link on its own so a token-hash collision can be retried
            # with a fresh token; pin foreign-key failures surface afterwards.
            session.flush()
            if revision_file_ids is not None:
                session.add_all(
                    _snapshot_pins(session, int(link.id), model_id, selected_files)
                )
            session.commit()
            break
        except IntegrityError as exc:
            session.rollback()
            if _attempt + 1 >= _TOKEN_COLLISION_RETRIES:
                raise OperationError(
                    "share_creation_failed", kind=ErrorKind.CONFLICT
                ) from exc
            raw_token = secrets.token_urlsafe(_TOKEN_BYTES)
    session.refresh(link)
    return link, raw_token


def list_shares(session: Session, model_id: int) -> list[ShareLink]:
    # Revoked links are hidden from the management list. The row is kept (not
    # hard-deleted) so the token stays permanently invalid — the public lookup
    # still finds it, sees it's revoked, and returns the uniform 404.
    return list(
        session.exec(
            select(ShareLink)
            .where(
                ShareLink.model_id == model_id,
                ShareLink.revoked_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(ShareLink.created_at.desc())  # type: ignore[attr-defined]
        ).all()
    )


def list_pins(session: Session, link_id: int) -> tuple[ShareLinkPin, ...]:
    return tuple(
        session.exec(
            select(ShareLinkPin)
            .where(ShareLinkPin.share_link_id == link_id)
            .order_by(ShareLinkPin.id.asc())  # type: ignore[attr-defined]
        ).all()
    )


def list_pins_for_links(
    session: Session, links: list[ShareLink]
) -> dict[int, tuple[ShareLinkPin, ...]]:
    if not links:
        return {}
    rows = session.exec(
        select(ShareLinkPin)
        .where(
            ShareLinkPin.share_link_id.in_(  # type: ignore[attr-defined]
                [link.id for link in links]
            )
        )
        .order_by(ShareLinkPin.id.asc())  # type: ignore[attr-defined]
    ).all()
    grouped: dict[int, list[ShareLinkPin]] = {}
    for row in rows:
        grouped.setdefault(row.share_link_id, []).append(row)
    return {link_id: tuple(pins) for link_id, pins in grouped.items()}


def revoke_share(session: Session, link: ShareLink) -> ShareLink:
    if link.revoked_at is None:
        link.revoked_at = utcnow()
        session.add(link)
        session.commit()
        session.refresh(link)
    return link


def resolve_share(session: Session, token: str) -> ShareLink:
    """Return the active link for *token*, or raise a uniform 404.

    Model shares additionally require the model to stay live, as before.
    Revision shares resolve while the model is trashed: their pins are a
    logical hold that keeps the selected Artifacts addressable.
    """
    if not token:
        raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
    link = session.exec(
        select(ShareLink).where(ShareLink.token_hash == _token_hash(token))
    ).first()
    if link is None or not is_active(link):
        raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
    return link


def resolve_share_access(session: Session, token: str) -> ShareAccess:
    """Resolve token + pins once; the single gate of every public route."""
    link = resolve_share(session, token)
    pins = list_pins(session, int(link.id))
    model = session.get(Model, link.model_id)
    if model is None:
        raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
    if not pins and model.deleted_at is not None:
        # Model shares keep their legacy live-model semantics.
        raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
    return ShareAccess(link=link, pins=pins)


def record_access(session: Session, link: ShareLink) -> None:
    link.access_count += 1
    session.add(link)
    session.commit()


def _public_file_row(
    f: File,
    md: Metadata | None,
    *,
    revision_number: int | None,
    is_recommended: bool | None = None,
) -> PublicFileRead:
    return PublicFileRead(
        id=f.id,  # type: ignore[arg-type]
        original_filename=f.original_filename,
        file_type=f.file_type.value,
        size_bytes=f.size_bytes,
        version=f.version,
        is_available=True,
        gcode_revision_number=revision_number,
        revision_label=f.revision_label,
        revision_status=f.revision_status,
        revision_notes=f.revision_notes,
        is_recommended=is_recommended
        if is_recommended is not None
        else f.is_recommended,
        bbox_x_mm=md.bbox_x_mm if md else None,
        bbox_y_mm=md.bbox_y_mm if md else None,
        bbox_z_mm=md.bbox_z_mm if md else None,
        triangle_count=md.triangle_count if md else None,
        slicer_name=md.slicer_name if md else None,
        slicer_version=md.slicer_version if md else None,
        printer_model=md.printer_model if md else None,
        nozzle_diameter_mm=md.nozzle_diameter_mm if md else None,
        layer_height_mm=md.layer_height_mm if md else None,
        first_layer_height_mm=md.first_layer_height_mm if md else None,
        infill_percent=md.infill_percent if md else None,
        wall_loops=md.wall_loops if md else None,
        support_material=md.support_material if md else None,
        nozzle_temperature_c=md.nozzle_temperature_c if md else None,
        bed_temperature_c=md.bed_temperature_c if md else None,
        estimated_time_s=md.estimated_time_s if md else None,
        filament_weight_g=md.filament_weight_g if md else None,
        filament_length_mm=md.filament_length_mm if md else None,
        filament_cost=md.filament_cost if md else None,
        material_type=md.material_type if md else None,
        material_brand=md.material_brand if md else None,
    )


def _unavailable_pin(pin: ShareLinkPin) -> PublicFileRead:
    """Stable tombstone when the pinned Artifact row no longer exists."""
    return PublicFileRead(
        id=pin.file_id,
        original_filename=pin.original_filename,
        file_type=pin.file_type.value,
        size_bytes=0,
        version=pin.file_version,
        is_available=False,
        gcode_revision_number=pin.revision_number,
        revision_label=None,
        revision_status=None,
        revision_notes=None,
        is_recommended=pin.is_recommended,
    )


def _model_share_detail(session: Session, link: ShareLink) -> PublicModelRead:
    model = session.get(Model, link.model_id)
    assert model is not None  # resolve_share already validated liveness
    gcode_revision_numbers = _gcode_revision_numbers(
        session, link.model_id, include_trashed=False
    )
    rows = session.exec(
        select(File, Metadata)
        .where(File.model_id == link.model_id)
        .where(live(File))
        .outerjoin(
            Metadata, Metadata.file_id == File.id  # type: ignore[arg-type]
        )
        .order_by(File.version.asc())  # type: ignore[attr-defined]
    ).all()
    files = [
        _public_file_row(f, md, revision_number=gcode_revision_numbers.get(f.id))
        for f, md in rows
    ]
    return PublicModelRead(
        name=model.name,
        description=model.description,
        has_thumbnail=bool(model.thumbnail_file_id or model.thumbnail_path),
        allow_download=link.allow_download,
        files=files,
    )


def _revision_share_detail(session: Session, access: ShareAccess) -> PublicModelRead:
    model = session.get(Model, access.link.model_id)
    assert model is not None  # resolve_share_access requires the row
    pin_ids = [pin.file_id for pin in access.pins]
    rows = {
        f.id: (f, md)
        for f, md in session.exec(
            select(File, Metadata)
            .where(File.id.in_(pin_ids))  # type: ignore[attr-defined]
            .outerjoin(
                Metadata, Metadata.file_id == File.id  # type: ignore[arg-type]
            )
        ).all()
    }
    files: list[PublicFileRead] = []
    for pin in access.pins:
        row = rows.get(pin.file_id)
        if row is None:
            files.append(_unavailable_pin(pin))
            continue
        f, md = row
        files.append(
            _public_file_row(
                f,
                md,
                revision_number=pin.revision_number,
                is_recommended=pin.is_recommended,
            )
        )
    return PublicModelRead(
        name=model.name,
        description=model.description,
        has_thumbnail=bool(
            access.link.cover_file_id or access.link.cover_thumbnail_path
        ),
        allow_download=access.link.allow_download,
        files=files,
    )


def public_detail(session: Session, access: ShareAccess) -> PublicModelRead:
    """Build the read-only projection the link grants."""
    if access.is_revision_share:
        return _revision_share_detail(session, access)
    return _model_share_detail(session, access.link)


def authorize_shared_file(
    session: Session,
    access: ShareAccess,
    file_id: int,
    *,
    file_types: frozenset[FileType] | None = None,
) -> File:
    """Return *file_id* only when this link grants it — the single file gate.

    Raises a uniform 404 for ids the link does not name (so file ids cannot be
    enumerated), and a stable 410 when a pinned Artifact's row is no longer
    recoverable, instead of ever falling through to another revision.
    """
    link = access.link
    if access.is_revision_share:
        pin = access.pin_for(file_id)
        if pin is None:
            raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
        if file_types is not None and pin.file_type not in file_types:
            raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
        f = session.get(File, file_id)
        if f is None:
            raise OperationError("shared_file_unavailable", kind=ErrorKind.GONE)
        return f
    f = session.get(File, file_id)
    if (
        f is None
        or f.deleted_at is not None
        or f.model_id != link.model_id
        or (file_types is not None and f.file_type not in file_types)
    ):
        raise OperationError("not_found", kind=ErrorKind.NOT_FOUND)
    return f


def share_file_or_404(session: Session, link: ShareLink, file_id: int) -> File:
    """Compatibility seam for callers holding a link without loaded pins."""
    return authorize_shared_file(
        session, ShareAccess(link=link, pins=list_pins(session, int(link.id))), file_id
    )


def shared_thumbnail_target(
    session: Session, access: ShareAccess
) -> tuple[int | None, str | None]:
    """Resolve the cover identity every thumbnail request authorizes through.

    Model shares follow the live model cover; revision shares follow their
    immutable cover snapshot (which may be empty forever once snapshotted so).
    """
    link = access.link
    if access.is_revision_share:
        return link.cover_file_id, link.cover_thumbnail_path
    model = session.get(Model, link.model_id)
    assert model is not None  # resolve_share already validated liveness
    return model.thumbnail_file_id, model.thumbnail_path
