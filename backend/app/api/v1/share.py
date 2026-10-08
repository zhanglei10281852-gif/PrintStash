"""Share-link endpoints.

Two routers with very different trust levels:

* ``router`` (prefix ``/share``) — **unauthenticated**, **GET-only**. The entire
  public surface. Every handler resolves the token, re-scopes file access to the
  shared model, and is rate-limited per IP.
* ``admin_router`` — authenticated management (create/list/revoke), guarded by
  the model's collection RBAC.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlmodel import Session

from app.api.v1.files import serve_artifact, stl_response, thumbnail_response
from app.core.ratelimit import rate_limit
from app.core.security import require_user
from app.db.models import CollectionRole, FileType, Model, ShareLink, User
from app.db.session import get_session
from app.modules.identity import rbac, share
from app.modules.storage.artifact_delivery import DeliveryPurpose
from app.schemas.share import ShareLinkCreate, ShareLinkCreated, ShareLinkRead


# ---------------------------------------------------------------------------
# Public router — unauthenticated, GET only. Rate-limited per IP as
# defense-in-depth against token enumeration.
# ---------------------------------------------------------------------------

router = APIRouter(
    prefix="/share",
    tags=["share"],
    dependencies=[Depends(rate_limit(120, 60.0))],
)


@router.get("/{token}", summary="Public read-only view of a shared model")
def get_shared_model(token: str, session: Session = Depends(get_session)):
    access = share.resolve_share_access(session, token)
    share.record_access(session, access.link)
    return share.public_detail(session, access)


@router.get("/{token}/thumbnail", summary="Thumbnail of a shared model")
def get_shared_thumbnail(
    token: str,
    request: Request,
    session: Session = Depends(get_session),
):
    access = share.resolve_share_access(session, token)
    cover_file_id, cover_path = share.shared_thumbnail_target(session, access)
    if cover_file_id is None and not cover_path:
        raise HTTPException(status_code=404, detail="not_found")
    return thumbnail_response(
        cover_file_id,
        request,
        thumbnail_path=cover_path,
        purpose=DeliveryPurpose.PUBLIC_SHARE,
    )


@router.get(
    "/{token}/files/{file_id}/stl",
    summary="Serve a shared mesh file as STL for the public viewer",
)
def get_shared_stl(
    token: str,
    file_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    access = share.resolve_share_access(session, token)
    f = share.authorize_shared_file(
        session, access, file_id, file_types=share.MESH_TYPES
    )
    return stl_response(session, f, request, DeliveryPurpose.PUBLIC_SHARE)


@router.get(
    "/{token}/files/{file_id}/download",
    summary="Download a shared original file (only when the link allows it)",
)
def download_shared_file(
    token: str,
    request: Request,
    file_id: int,
    session: Session = Depends(get_session),
):
    access = share.resolve_share_access(session, token)
    f = share.authorize_shared_file(session, access, file_id)
    if not access.link.allow_download:
        raise HTTPException(status_code=403, detail="download_disabled")
    return serve_artifact(
        f, request, f.original_filename, purpose=DeliveryPurpose.PUBLIC_SHARE
    )


@router.get(
    "/{token}/files/{file_id}/gcode",
    summary="Serve a shared G-code file for the public toolpath preview",
)
def get_shared_gcode(
    token: str,
    request: Request,
    file_id: int,
    session: Session = Depends(get_session),
):
    access = share.resolve_share_access(session, token)
    f = share.authorize_shared_file(
        session, access, file_id, file_types=frozenset({FileType.GCODE})
    )
    if not access.link.allow_download:
        raise HTTPException(status_code=403, detail="download_disabled")
    return serve_artifact(
        f, request, f.original_filename, "text/plain", DeliveryPurpose.PUBLIC_SHARE
    )


# ---------------------------------------------------------------------------
# Admin router — authenticated share management.
# ---------------------------------------------------------------------------

admin_router = APIRouter(tags=["share"])


def _require_model(
    session: Session, user: User, model_id: int, role: CollectionRole
) -> Model:
    model = session.get(Model, model_id)
    if model is None or model.deleted_at is not None:
        raise HTTPException(status_code=404, detail="model_not_found")
    rbac.require_model_collection_role(session, user, model.collection_id, role)
    return model


@admin_router.post(
    "/models/{model_id}/shares",
    response_model=ShareLinkCreated,
    summary="Create a public share link for a model",
)
def create_model_share(
    model_id: int,
    body: ShareLinkCreate,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ShareLinkCreated:
    _require_model(session, current_user, model_id, CollectionRole.EDIT)
    link, raw_token = share.create_share(
        session,
        model_id=model_id,
        expires_in_days=body.expires_in_days,
        allow_download=body.allow_download,
        revision_file_ids=body.revision_file_ids,
        created_by=current_user.id,
    )
    read = share.to_read(link, share.list_pins(session, int(link.id)))
    return ShareLinkCreated(
        **read.model_dump(), token=raw_token, url=f"/share/{raw_token}"
    )


@admin_router.get(
    "/models/{model_id}/shares",
    response_model=list[ShareLinkRead],
    summary="List a model's share links",
)
def list_model_shares(
    model_id: int,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> list[ShareLinkRead]:
    _require_model(session, current_user, model_id, CollectionRole.VIEW)
    links = share.list_shares(session, model_id)
    pins_by_link = share.list_pins_for_links(session, links)
    return [
        share.to_read(link, pins_by_link.get(int(link.id), ())) for link in links
    ]


@admin_router.delete(
    "/shares/{share_id}",
    response_model=ShareLinkRead,
    summary="Revoke a share link",
)
def revoke_model_share(
    share_id: int,
    current_user: User = Depends(require_user),
    session: Session = Depends(get_session),
) -> ShareLinkRead:
    link = session.get(ShareLink, share_id)
    if link is None:
        raise HTTPException(status_code=404, detail="share_not_found")
    _require_model(session, current_user, link.model_id, CollectionRole.EDIT)
    return share.to_read(
        share.revoke_share(session, link),
        share.list_pins(session, int(link.id)),
    )


@router.get(
    "/{token}/files/{file_id}/toolpath",
    summary="Toolpath only for download-authorized shares",
)
def get_shared_toolpath(
    token: str,
    file_id: int,
    session: Session = Depends(get_session),
):
    from app.api.toolpath_response import toolpath_response

    access = share.resolve_share_access(session, token)
    file = share.authorize_shared_file(
        session, access, file_id, file_types=frozenset({FileType.GCODE})
    )
    if not access.link.allow_download:
        raise HTTPException(status_code=403, detail="download_disabled")
    return toolpath_response(session, file)
