"""Builders for the embedded image reference ledger.

Claims and retirements are normally produced by body edits (see
`app.modules.library.embedded_images.sync_owner_body`); these builders exist
for tests that arrange a specific ledger state directly — cross-references,
reconciliation divergence, and purge protection.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.db.models import (
    EmbeddedImageClaim,
    EmbeddedImageOwnerKind,
    EmbeddedImageRetirement,
    EmbeddedImageRetirementState,
)
from tests.factories._support import save


def build_embedded_image_claim(
    session: Session,
    *,
    owner_kind: EmbeddedImageOwnerKind = EmbeddedImageOwnerKind.DOCUMENT,
    owner_id: int,
    referrer_kind: EmbeddedImageOwnerKind | None = None,
    referrer_id: int | None = None,
    name: str = "a" * 64 + ".png",
    **overrides: Any,
) -> EmbeddedImageClaim:
    """One current body reference to one image in an owner namespace."""
    return save(
        session,
        EmbeddedImageClaim(
            owner_kind=owner_kind,
            owner_id=owner_id,
            referrer_kind=referrer_kind or owner_kind,
            referrer_id=referrer_id if referrer_id is not None else owner_id,
            name=name,
            **overrides,
        ),
    )


def build_embedded_image_retirement(
    session: Session,
    *,
    owner_kind: EmbeddedImageOwnerKind = EmbeddedImageOwnerKind.DOCUMENT,
    owner_id: int,
    name: str = "b" * 64 + ".png",
    state: EmbeddedImageRetirementState = EmbeddedImageRetirementState.PENDING,
    **overrides: Any,
) -> EmbeddedImageRetirement:
    """One owner-declared image removal record."""
    return save(
        session,
        EmbeddedImageRetirement(
            owner_kind=owner_kind,
            owner_id=owner_id,
            name=name,
            state=state,
            **overrides,
        ),
    )
