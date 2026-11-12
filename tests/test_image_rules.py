"""Unit tests for the image variant grouping (no network required)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from image_rules import base_key, keep_largest  # noqa: E402


def test_size_suffixes_share_one_key():
    urls = [
        "https://example.org/a/image1-400x300.jpg",
        "https://example.org/a/image1-800x533.jpg",
        "https://example.org/a/image1.jpg",
    ]
    keys = {base_key(url) for url in urls}
    assert len(keys) == 1
    assert keys.pop() == "/a/image1.jpg"


def test_keep_largest_prefers_the_original():
    urls = [
        "https://example.org/a/image1-400x300.jpg",
        "https://example.org/a/image1.jpg",
        "https://example.org/a/image1-800x533.jpg",
    ]
    assert keep_largest(urls) == ["https://example.org/a/image1.jpg"]


def test_keep_largest_prefers_the_bigger_size_when_no_original():
    urls = [
        "https://example.org/a/pic-400x300.jpg",
        "https://example.org/a/pic-800x533.jpg",
    ]
    assert keep_largest(urls) == ["https://example.org/a/pic-800x533.jpg"]


def test_distinct_uploads_are_kept():
    urls = [
        "https://example.org/a/image1-400x300.jpg",
        "https://example.org/a/image2-400x300.jpg",
    ]
    assert len(keep_largest(urls)) == 2
