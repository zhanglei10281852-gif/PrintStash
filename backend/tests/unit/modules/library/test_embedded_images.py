"""Pure reference parsing for embedded markdown images.

The parser is the one seam every lifecycle decision shares — ownership census,
body-edit sync, purge protection and reconciliation — so the exact reference
shapes it recognizes are pinned here, without a database.
"""

from __future__ import annotations

import pytest

from app.db.models import EmbeddedImageOwnerKind
from app.modules.library.embedded_images import (
    SERVABLE_IMAGE_NAME_RE,
    EmbeddedImageRef,
    extract_refs,
)

_HASH = "a" * 64


def _ref(kind: EmbeddedImageOwnerKind, owner_id: int, name: str) -> EmbeddedImageRef:
    return EmbeddedImageRef(owner_kind=kind, owner_id=owner_id, name=name)


class TestExtractRefs:
    def test_parses_nothing_from_empty_bodies(self) -> None:
        assert extract_refs(None) == frozenset()
        assert extract_refs("") == frozenset()
        assert extract_refs("no images here") == frozenset()

    @pytest.mark.parametrize(
        "url",
        [
            f"/api/v1/documents/7/images/{_HASH}.png",
            f"/documents/7/images/{_HASH}.png",
        ],
    )
    def test_parses_a_self_document_reference_with_or_without_prefix(
        self, url: str
    ) -> None:
        refs = extract_refs(f"![pic]({url})")

        assert refs == frozenset({_ref(EmbeddedImageOwnerKind.DOCUMENT, 7, f"{_HASH}.png")})

    def test_parses_a_collection_reference(self) -> None:
        refs = extract_refs(f"![pic](/api/v1/collections/3/images/{_HASH}.jpeg)")

        assert refs == frozenset(
            {_ref(EmbeddedImageOwnerKind.COLLECTION, 3, f"{_HASH}.jpeg")}
        )

    def test_keeps_a_reference_to_another_documents_namespace(self) -> None:
        body = f"see [theirs](/documents/42/images/{_HASH}.gif) for the diagram"

        refs = extract_refs(body)

        assert refs == frozenset(
            {_ref(EmbeddedImageOwnerKind.DOCUMENT, 42, f"{_HASH}.gif")}
        )

    def test_parses_document_and_collection_references_in_one_body(self) -> None:
        body = (
            f"![a](/api/v1/documents/1/images/{_HASH}.png)"
            f"![b](/api/v1/collections/2/images/{_HASH}.webp)"
        )

        refs = extract_refs(body)

        assert refs == frozenset(
            {
                _ref(EmbeddedImageOwnerKind.DOCUMENT, 1, f"{_HASH}.png"),
                _ref(EmbeddedImageOwnerKind.COLLECTION, 2, f"{_HASH}.webp"),
            }
        )

    def test_deduplicates_repeated_references_to_one_image(self) -> None:
        body = (
            f"![a](/documents/9/images/{_HASH}.png) "
            f"![a again](/documents/9/images/{_HASH}.png)"
        )

        refs = extract_refs(body)

        assert refs == frozenset(
            {_ref(EmbeddedImageOwnerKind.DOCUMENT, 9, f"{_HASH}.png")}
        )

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/documents/7/files/manual.pdf",
            f"/documents/images/{_HASH}.png",
            f"/documents/x/images/{_HASH}.png",
        ],
    )
    def test_ignores_paths_that_are_not_embedded_image_urls(self, path: str) -> None:
        assert extract_refs(f"[x]({path})") == frozenset()

    def test_keeps_an_unservable_extension_as_a_reference_but_not_an_owner_key(
        self,
    ) -> None:
        # The census recognizes any image-path mention so missing non-hash
        # objects surface as findings; release decisions additionally require
        # the servable hash name shape.
        refs = extract_refs(f"[x](/api/v1/documents/7/images/{_HASH}.svg)")

        assert refs == frozenset(
            {_ref(EmbeddedImageOwnerKind.DOCUMENT, 7, f"{_HASH}.svg")}
        )
        assert SERVABLE_IMAGE_NAME_RE.match(f"{_HASH}.svg") is None


class TestServableImageName:
    @pytest.mark.parametrize("ext", ["png", "jpg", "jpeg", "gif", "webp"])
    def test_accepts_every_served_extension(self, ext: str) -> None:
        assert SERVABLE_IMAGE_NAME_RE.match(f"{_HASH}.{ext}")

    @pytest.mark.parametrize("name", ["mine.png", f"{'a' * 63}.png", f"{_HASH}.svg"])
    def test_rejects_names_the_upload_route_cannot_create(self, name: str) -> None:
        assert SERVABLE_IMAGE_NAME_RE.match(name) is None
