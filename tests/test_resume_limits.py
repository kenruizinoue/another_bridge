"""Per-attachment caps on the resume body (ticket 2)."""

import pytest
from pydantic import ValidationError

from routers.sessions import MAX_IMAGE_B64_CHARS, ResumeRequest


def test_an_image_under_its_cap_is_accepted() -> None:
    body = ResumeRequest(images=[{"media_type": "image/jpeg", "data": "a" * 1000}])
    assert len(body.images) == 1


def test_an_image_over_its_cap_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ResumeRequest(images=[{"media_type": "image/jpeg", "data": "a" * (MAX_IMAGE_B64_CHARS + 1)}])
