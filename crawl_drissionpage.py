"""Crawl https://mitadmissions.org/blogs/ with DrissionPage and write a CSV.

Same fields and same CSV layout as crawl_playwright.py:
Title | Author | Comment Count | Time | Article Content | Images In Article

Usage:
    python crawl_drissionpage.py --max-posts 5
"""

import argparse
import csv
import html
import pathlib
from urllib.parse import urljoin
import re
import sys

WHITESPACE = re.compile(r"\s+")
import time

from DrissionPage import ChromiumOptions, ChromiumPage

from image_rules import keep_largest

BASE_URL = "https://mitadmissions.org/blogs/"
FIELDS = ["Title", "Author", "Comment Count", "Time", "Article Content", "Images In Article"]
COMMENT_PATTERN = re.compile(r"(\d+)\s+Comments?", re.IGNORECASE)


def make_page(headless: bool) -> ChromiumPage:
    options = ChromiumOptions()
    options.auto_port()
    options.headless(headless)
    options.set_argument("--disable-gpu")
    options.set_argument("--no-sandbox")
    return ChromiumPage(options)



EMOJI_HOSTS = ("s.w.org/images/core/emoji", "wordpress.org/images/core/emoji")


def keep_image(url: str) -> bool:
    lowered = url.lower()
    if any(host in lowered for host in EMOJI_HOSTS):
        return False
    return not (lowered.endswith(".svg") and "emoji" in lowered)


def scroll_through(page, pause: float = 0.25) -> None:
    """Trigger lazy-loaded images by scrolling the whole document."""
    page.scroll.to_bottom()
    page.wait(pause)
    page.scroll.to_top()
    page.wait(pause)


def page_url(number: int) -> str:
    return BASE_URL if number == 1 else f"{BASE_URL}page/{number}/"


def collect_links(page, max_pages: int, max_posts: int | None) -> list[str]:
    """Walk the paginated listing until it runs out of *new* articles."""
    links: list[str] = []
    for number in range(1, max_pages + 1):
        page.get(page_url(number))
        page.wait.doc_loaded()
        first = page.ele("css:.post-tease__h__link", timeout=15)
        if not first:
            print(f"page {number}: no posts, stopping", file=sys.stderr)
            break

        fresh = []
        for element in page.eles("css:.post-tease__h__link"):
            href = element.attr("href") or ""
            if "/blogs/entry/" not in href:
                continue
            url = href if href.startswith("http") else f"https://mitadmissions.org{href}"
            if url not in links and url not in fresh:
                fresh.append(url)

        # Last page guard: a "next" link can wrap around to page 1, so trust the
        # absence of new articles rather than the link itself.
        if not fresh:
            print(f"page {number}: no new articles, stopping", file=sys.stderr)
            break

        links.extend(fresh)
        print(f"page {number}: +{len(fresh)} (total {len(links)})", file=sys.stderr)
        if max_posts and len(links) >= max_posts:
            return links[:max_posts]

    return links


def read_comment_count(page, timeout: float = 12.0) -> str:
    """Disqus lives in an iframe; read the "N Comments" text when it renders."""
    try:
        frame = page.get_frame("css:#disqus_thread iframe", timeout=timeout)
        if frame is None:
            return ""
        body = frame.ele("tag:body", timeout=5)
        text = body.text if body else ""
        match = COMMENT_PATTERN.search(text or "")
        if match:
            return match.group(1)
        if re.search(r"no comments|be the first", text or "", re.IGNORECASE):
            return "0"
        return ""
    except Exception:  # noqa: BLE001 - comment count is best effort
        return ""


def scrape_article(page, url: str) -> dict[str, object]:
    page.get(url)
    page.wait.doc_loaded()
    page.ele("css:.article__body", timeout=20)
    scroll_through(page)

    author_element = page.ele("css:.page-topper__title__name", timeout=5)
    author = re.sub(r"^by\s+", "", (author_element.text if author_element else "").strip(), flags=re.I)

    title_element = page.ele("css:.page-topper__title", timeout=5)
    title = (title_element.text if title_element else "").strip()
    if author and author in title:
        title = title.replace(f"by {author}", "").replace(author, "").strip()
    title = re.sub(r"\s*by\s*$", "", title, flags=re.I).strip()

    date_element = page.ele("css:time.page-topper__date", timeout=5)
    published = ""
    if date_element:
        published = date_element.attr("datetime") or date_element.text.strip()

    body_element = page.ele("css:.article__body", timeout=10)
    content = html.unescape(body_element.text.strip()) if body_element else ""
    content = WHITESPACE.sub(" ", content)  # one row per article

    image_sources = keep_largest(
        urljoin(page.url, element.attr("src"))
        for element in page.eles("css:.article__body img")
        if element.attr("src") and keep_image(element.attr("src"))
    )

    return {
        "Title": title,
        "Author": author,
        "Comment Count": read_comment_count(page),
        "Time": published,
        "Article Content": content,
        "Images In Article": "; ".join(sorted(image_sources)),
    }



def download_images(page, urls: str, url: str, folder: pathlib.Path) -> int:
    """Download every image URL into images/<slug>/ using DrissionPage's downloader."""
    if not urls:
        return 0
    slug = url.rstrip("/").split("/")[-1] or "article"
    target = folder / slug
    target.mkdir(parents=True, exist_ok=True)
    saved = 0
    for index, image_url in enumerate((u.strip() for u in urls.split(";") if u.strip()), start=1):
        suffix = pathlib.Path(image_url.split("?")[0]).suffix or ".jpg"
        if len(suffix) > 5:
            suffix = ".jpg"
        try:
            page.download(image_url, str(target), f"{index:02d}{suffix}")
            saved += 1
        except Exception:  # noqa: BLE001
            continue
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-pages", type=int, default=10_000, help="stop after N listing pages")
    parser.add_argument("--max-posts", type=int, default=None)
    parser.add_argument("--out", default="blogs_drissionpage.csv")
    parser.add_argument("--headless", action="store_true", default=True)
    args = parser.parse_args()

    page = make_page(headless=args.headless)
    rows: list[dict[str, object]] = []
    try:
        links = collect_links(page, args.max_pages, args.max_posts)
        print(f"found {len(links)} article(s)", file=sys.stderr)

        for index, url in enumerate(links, start=1):
            started = time.perf_counter()
            try:
                row = scrape_article(page, url)
            except Exception as exc:  # noqa: BLE001
                print(f"[{index}/{len(links)}] FAILED {url}: {exc}", file=sys.stderr)
                continue
            download_images(page, str(row["Images In Article"]), url, pathlib.Path("images"))
            rows.append(row)
            print(
                f"[{index}/{len(links)}] {str(row['Title'])[:40]:40s} "
                f"comments={str(row['Comment Count']) or '-':>3s} "
                f"images={len(str(row['Images In Article']).split(';')) if row['Images In Article'] else 0} "
                f"chars={len(str(row['Article Content']))} "
                f"{time.perf_counter() - started:.1f}s",
                file=sys.stderr,
            )
            time.sleep(0.5)
    finally:
        page.quit()

    with open(args.out, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"wrote {len(rows)} row(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
