from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.db.models import CollectionRole, DocumentKind


class EmbeddedImageState(BaseModel):
    """One image in the owner namespace with its lifecycle state.

    ``current`` images are referenced by the document body; ``retiring`` ones
    were removed by a precise edit and remain stored and reclaimable until the
    document is permanently deleted.
    """

    name: str
    state: Literal["current", "retiring"]
    removed_at: Optional[datetime] = None


class DocumentListItem(BaseModel):
    id: int
    name: str
    kind: DocumentKind
    collection: Optional[str] = None
    collection_id: Optional[int] = None
    multipart_model_id: Optional[int] = None
    filename: Optional[str] = None
    effective_role: Optional[CollectionRole] = None
    updated_at: datetime


class DocumentRead(DocumentListItem):
    body: Optional[str] = None  # markdown content (None for binary docs)
    images: list[EmbeddedImageState] = Field(default_factory=list)


class DocumentCreate(BaseModel):
    """Create a markdown document. Binary docs come in via the upload endpoint."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    collection_id: Optional[int] = None
    multipart_model_id: Optional[int] = None
    body: Optional[str] = Field(default=None, max_length=1_000_000)


class DocumentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    body: Optional[str] = Field(default=None, max_length=1_000_000)


class DocumentImageUpload(BaseModel):
    url: str
