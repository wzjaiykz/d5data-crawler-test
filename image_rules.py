"""Group WordPress image variants by file name and keep the largest one.

WordPress appends size suffixes to the same upload:

    2026/11/image1-400x300.jpg
    2026/11/image1-800x533.jpg
    2026/11/image1-scaled.jpg
    2026/11/image1.jpg          <- the original

The rendered content can reference several of them; they are one picture, so the
crawler keeps a single URL: the one with the largest pixel area (the original wins
ties). No image analysis needed - the URL states the size.
"""

import re
from urllib.parse import urlparse

SIZE_SUFFIX = re.compile(r"-(?:scaled|\d+x\d+|e\d+|rotated|cropped)(?=\.[A-Za-z0-9]+$)")
EDITED_SUFFIX = re.compile(r"-e\d{6,}(?=\.[A-Za-z0-9]+$)")
DIMENSIONS = re.compile(r"-(\d+)x(\d+)(?=\.[A-Za-z0-9]+$)")


def base_key(url: str) -> str:
    """The upload identity, independent of the generated size suffix."""
    path = urlparse(url).path
    path = EDITED_SUFFIX.sub("", path)
    path = SIZE_SUFFIX.sub("", path)
    return path


def area(url: str) -> int:
    """Pixel area when the URL encodes it; originals count as the largest."""
    match = DIMENSIONS.search(urlparse(url).path)
    if match:
        return int(match.group(1)) * int(match.group(2))
    # No size suffix means the uploaded original; -scaled still ranks below it.
    return 10**9 + 1


def keep_largest(urls) -> list[str]:
    """One URL per underlying upload, ordered by first appearance."""
    best: dict[str, str] = {}
    order: list[str] = []
    for url in urls:
        if not url:
            continue
        key = base_key(url)
        if key not in best:
            best[key] = url
            order.append(key)
            continue
        if area(url) > area(best[key]):
            best[key] = url
    return [best[key] for key in order]
