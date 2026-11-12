"""Compare the two crawler outputs.

Usage:
    python compare.py blogs_playwright.csv blogs_drissionpage.csv

Reports row counts, title overlap and per-field agreement (whitespace-normalised).
"""

import csv
import difflib
import re
import sys

# Article titles contain emoji; a GBK console would crash while printing them.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

WHITESPACE = re.compile(r"\s+")
FIELDS = ["Author", "Comment Count", "Time", "Article Content", "Images In Article"]


# Typographic variants differ between the two drivers' text extraction.
TRANSLATE = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "-", "\u00a0": " ", "\u03c0": "pi",
    }
)


def url_set(value: str) -> set[str]:
    return {part.strip() for part in value.split(";") if part.strip()}


def normalise(value: str) -> str:
    return WHITESPACE.sub(" ", (value or "").translate(TRANSLATE).strip())


def title_key(value: str) -> str:
    """Join key: emoji are stripped because the two drivers disagree on them.

    Playwright keeps the emoji from the DOM text, DrissionPage sometimes drops
    them, so the same article would otherwise look like two different rows.
    """
    without_emoji = "".join(ch for ch in normalise(value) if ord(ch) < 0x2190)
    return WHITESPACE.sub(" ", without_emoji).strip()


def load(path: str) -> dict[str, dict[str, str]]:
    with open(path, encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    return {title_key(row["Title"]): row for row in rows}


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2

    left = load(sys.argv[1])
    right = load(sys.argv[2])

    print(f"{sys.argv[1]:28s} {len(left)} rows")
    print(f"{sys.argv[2]:28s} {len(right)} rows")

    common = sorted(set(left) & set(right))
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))

    print(f"\ncommon titles : {len(common)}")
    print(f"only in left  : {len(only_left)}")
    print(f"only in right : {len(only_right)}")
    for title in only_left[:5]:
        print(f"  left-only : {title[:70]}")
    for title in only_right[:5]:
        print(f"  right-only: {title[:70]}")

    print("\nfield agreement on common titles:")
    for field in FIELDS:
        same = 0
        diffs: list[tuple[str, str, str]] = []
        for title in common:
            a = normalise(left[title][field])
            b = normalise(right[title][field])
            if field == "Article Content":
                # Two driver stacks differ in whitespace and hidden text, so compare
                # the strings rather than their lengths.
                ratio = difflib.SequenceMatcher(None, a, b).ratio()
                if ratio >= 0.95:
                    same += 1
                else:
                    diffs.append((title, f"{ratio:.3f} {a}", b))
            elif field == "Images In Article":
                if url_set(a) == url_set(b):
                    same += 1
                else:
                    diffs.append((title, a, b))
            elif a == b:
                same += 1
            else:
                diffs.append((title, a, b))
        share = same / len(common) if common else 0
        print(f"  {field:20s} {same}/{len(common)}  ({share:.0%})")
        for title, a, b in diffs[:2]:
            print(f"    - {title[:50]}: left={a[:40]!r} right={b[:40]!r}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
