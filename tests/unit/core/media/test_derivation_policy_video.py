"""
Motet - Video derivation eligibility tests (ADR-0118)
"""

from motet.core.media.derivation_policy import get_eligible_derivations


def test_video_content_type_eligible():
    assert "video" in get_eligible_derivations("video/mp4", "user_upload")


def test_image_not_video_eligible():
    eligible = get_eligible_derivations("image/png", "user_upload")
    assert "video" not in eligible
    assert "image" in eligible
