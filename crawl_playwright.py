"""Crawl https://mitadmissions.org/blogs/ with Playwright and write a CSV.

Fields: Title | Author | Comment Count | Time | Article Content | Images In Article

Usage:
    python crawl_playwright.py --max-posts 5
    python crawl_playwright.py                 # all posts on the listing page
"""

import argparse
import csv
import re
import sys
import time
from dataclasses import asdict, dataclass

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://mitadmissions.org/blogs/"
FIELDS = ["Title", "Author", "Comment Count", "Time", "Article Content", "Images In Article"]
COMMENT_PATTERN = re.compile(r"(\d+)\s+Comments?", re.IGNORECASE)


@dataclass
class Article:
    title: str
    author: str
    comment_count: str
    published: str
    content: str
    images: int



def settle_page(page, pause_ms: int = 800) -> None:
    """Scroll through the page so lazy images and embeds actually load."""
    height = page.evaluate("document.body.scrollHeight")
    step = max(400, height // 8)
    for y in range(0, height + step, step):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)
    page.evaluate("window.scrollTo(0, 0)")
    try:
        page.wait_for_load_state("networkidle", timeout=8_000)
    except PlaywrightTimeout:
        pass
    page.wait_for_timeout(pause_ms)


def collect_links(page, limit: int | None) -> list[str]:
    """Read the listing page and return article URLs (deduplicated, in page order)."""
    page.goto(BASE_URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(".post-tease__h__link", timeout=30_000)

    hrefs = page.eval_on_selector_all(
        ".post-tease__h__link", "nodes => nodes.map(n => n.getAttribute('href'))"
    )
    links: list[str] = []
    for href in hrefs:
        if not href or "/blogs/entry/" not in href:
            continue
        url = href if href.startswith("http") else f"https://mitadmissions.org{href}"
        if url not in links:
            links.append(url)
    return links[:limit] if limit else links


def read_comment_count(page, timeout_ms: int = 12_000) -> str:
    """Disqus renders inside an iframe; wait for it and read "N Comments".

    Falls back to an empty string when Disqus is blocked or the post has no thread.
    """
    try:
        page.wait_for_selector("#disqus_thread iframe", timeout=timeout_ms)
        handle = page.query_selector("#disqus_thread iframe")
        frame = handle.content_frame() if handle else None
        if frame is None:
            return ""
        text = frame.inner_text("body")
        match = COMMENT_PATTERN.search(text)
        if match:
            return match.group(1)
        if re.search(r"no comments|be the first", text, re.IGNORECASE):
            return "0"
    except (PlaywrightTimeout, Exception):  # noqa: BLE001 - comment count is best effort
        return ""
    return ""


def scrape_article(page, url: str, comment_timeout_ms: int) -> Article:
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(".article__body", timeout=30_000)
    settle_page(page)

    title = page.evaluate(
        """() => {
            const node = document.querySelector('.page-topper__title');
            if (!node) return '';
            const clone = node.cloneNode(true);
            clone.querySelectorAll('.page-topper__title__name').forEach(n => n.remove());
            return clone.textContent.replace(/\\s+/g, ' ').trim();
        }"""
    )
    # The author is rendered as "by Nicholas R. '29" inside the title link.
    author_raw = page.inner_text(".page-topper__title__name").strip()
    author = re.sub(r"^by\s+", "", author_raw, flags=re.IGNORECASE)

    published = page.get_attribute("time.page-topper__date", "datetime") or ""
    if not published:
        published = page.inner_text("time.page-topper__date").strip()

    content = page.inner_text(".article__body").strip()

    image_srcs = page.eval_on_selector_all(
        ".article__body img", "nodes => nodes.map(n => n.getAttribute('src') || '')"
    )
    images = len({src for src in image_srcs if src})

    comment_count = read_comment_count(page, comment_timeout_ms)
    return Article(
        title=title,
        author=author,
        comment_count=comment_count,
        published=published,
        content=content,
        images=images,
    )


def write_csv(articles: list[Article], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        for article in articles:
            row = asdict(article)
            writer.writerow(
                {
                    "Title": row["title"],
                    "Author": row["author"],
                    "Comment Count": row["comment_count"],
                    "Time": row["published"],
                    "Article Content": row["content"],
                    "Images In Article": row["images"],
                }
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-posts", type=int, default=None, help="limit the number of posts")
    parser.add_argument("--out", default="blogs_playwright.csv")
    parser.add_argument(
        "--comment-timeout", type=int, default=12_000, help="ms to wait for the Disqus frame"
    )
    args = parser.parse_args()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        links = collect_links(page, args.max_posts)
        print(f"found {len(links)} article(s)", file=sys.stderr)

        articles: list[Article] = []
        for index, url in enumerate(links, start=1):
            started = time.perf_counter()
            try:
                article = scrape_article(page, url, args.comment_timeout)
            except Exception as exc:  # noqa: BLE001 - keep going on a single failure
                print(f"[{index}/{len(links)}] FAILED {url}: {exc}", file=sys.stderr)
                continue
            articles.append(article)
            print(
                f"[{index}/{len(links)}] {article.title[:40]:40s} "
                f"comments={article.comment_count or '-':>3s} "
                f"images={article.images} "
                f"chars={len(article.content)} "
                f"{time.perf_counter() - started:.1f}s",
                file=sys.stderr,
            )
            time.sleep(0.5)  # be polite to the site

        browser.close()

    write_csv(articles, args.out)
    print(f"wrote {len(articles)} row(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
