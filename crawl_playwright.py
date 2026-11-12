"""Crawl https://mitadmissions.org/blogs/ with Playwright and write a CSV.

Fields: Title | Author | Comment Count | Time | Article Content | Images In Article

Images are collected from the network layer (what DevTools shows under the Img
filter) instead of scanning every <img> in the DOM: only real image responses
are kept, lazy-loaded ones included, and the body is reused for downloading.

Usage:
    python crawl_playwright.py --max-pages 2 --max-posts 5
    python crawl_playwright.py                  # every listing page
"""

import argparse
import base64
import csv
import html
import pathlib
import re
import sys
import time
import urllib.parse
from dataclasses import asdict, dataclass

import requests
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from image_rules import keep_largest

BASE_URL = "https://mitadmissions.org/blogs/"
FIELDS = ["Title", "Author", "Comment Count", "Time", "Article Content", "Images In Article"]
COMMENT_PATTERN = re.compile(r"(\d+)\s+Comments?", re.IGNORECASE)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
WHITESPACE = re.compile(r"\s+")

# Emoji sprites and avatars are served as images but are not article content.
IGNORED_IMAGE_PARTS = (
    "s.w.org/images/core/emoji",
    "wordpress.org/images/core/emoji",
    "c.disquscdn.com",
    "disquscdn.com",
    "disqus.com",
    "/avatar",
    "gravatar",
    "spinner",
    "emoji",
)


@dataclass
class Article:
    title: str
    author: str
    comment_count: str
    published: str
    content: str
    images: str


def page_url(number: int) -> str:
    return BASE_URL if number == 1 else f"{BASE_URL}page/{number}/"


def keep_image(url: str) -> bool:
    lowered = url.lower()
    return not any(part in lowered for part in IGNORED_IMAGE_PARTS)


class ImageCollector:
    """Record image responses (Playwright equivalent of the DevTools Img filter)."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.bodies: dict[str, bytes] = {}

    def reset(self) -> None:
        self.urls.clear()
        self.bodies.clear()

    def handler(self, response) -> None:
        try:
            if response.request.resource_type != "image" or not response.ok:
                return
            url = response.url
            if not keep_image(url):
                return
            if url not in self.urls:
                self.urls.append(url)
            # Plain HTTP clients are blocked by Cloudflare, so keep the bytes the
            # browser already received. Only first-party images are worth the memory.
            if "mitadmissions.org" in url and url not in self.bodies:
                self.bodies[url] = response.body()
        except Exception:  # noqa: BLE001 - a broken image must not abort the crawl
            return


def settle_page(page, pause_ms: int = 400) -> None:
    """Scroll through the page so lazy images and embeds actually load."""
    previous = -1
    for _ in range(14):  # lazy content keeps growing the document; stop when it settles
        height = page.evaluate("document.body.scrollHeight")
        page.evaluate(f"window.scrollTo(0, {height})")
        page.wait_for_timeout(200)
        if height == previous:
            break
        previous = height
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(pause_ms)


def collect_links(page, max_pages: int, max_posts: int | None) -> list[str]:
    """Walk the paginated listing until it runs out of *new* articles."""
    links: list[str] = []
    for number in range(1, max_pages + 1):
        response = page.goto(page_url(number), wait_until="domcontentloaded", timeout=60_000)
        if response is not None and response.status >= 400:
            print(f"page {number}: HTTP {response.status}, stopping", file=sys.stderr)
            break

        try:
            page.wait_for_selector(".post-tease__h__link", timeout=20_000)
        except PlaywrightTimeout:
            print(f"page {number}: no posts, stopping", file=sys.stderr)
            break

        hrefs = page.eval_on_selector_all(
            ".post-tease__h__link", "nodes => nodes.map(n => n.getAttribute('href'))"
        )
        fresh = []
        for href in hrefs:
            if not href or "/blogs/entry/" not in href:
                continue
            url = href if href.startswith("http") else f"https://mitadmissions.org{href}"
            if url not in links and url not in fresh:
                fresh.append(url)

        # Last page guard: WordPress can render a "next" link that wraps to page 1,
        # so stop as soon as a page yields nothing new instead of trusting the link.
        if not fresh:
            print(f"page {number}: no new articles, stopping", file=sys.stderr)
            break

        links.extend(fresh)
        print(f"page {number}: +{len(fresh)} (total {len(links)})", file=sys.stderr)
        if max_posts and len(links) >= max_posts:
            return links[:max_posts]

    return links


def read_comment_count(page, timeout_ms: int = 10_000) -> str:
    """Disqus renders in an iframe; expand "load more" first, then read the count."""
    try:
        page.wait_for_selector("#disqus_thread iframe", timeout=timeout_ms)
        handle = page.query_selector("#disqus_thread iframe")
        frame = handle.content_frame() if handle else None
        if frame is None:
            return ""

        for _ in range(3):  # a thread may paginate behind "Load more comments"
            try:
                more = frame.query_selector("text=/load more|more comments/i")
                if not more:
                    break
                more.click()
                page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                break

        text = frame.inner_text("body")
        match = COMMENT_PATTERN.search(text)
        if match:
            return match.group(1)
        if re.search(r"no comments|be the first", text, re.IGNORECASE):
            return "0"
    except (PlaywrightTimeout, Exception):  # noqa: BLE001 - best effort
        return ""
    return ""



def rest_post(slug: str) -> dict:
    """Fetch one post from the WordPress REST API (no browser, complete content)."""
    url = (
        f"https://mitadmissions.org/wp-json/wp/v2/posts?per_page=1&_embed=1&slug={slug}"
    )
    try:
        response = requests.get(url, timeout=20, headers={"User-Agent": USER_AGENT})
        if response.status_code != 200:
            return {}
        payload = response.json()
        return payload[0] if payload else {}
    except (requests.RequestException, ValueError):
        return {}


def rest_images(post: dict) -> str:
    """Every image referenced by the rendered content, in document order."""
    html = (post.get("content") or {}).get("rendered") or ""
    sources = re.findall(r'<img[^>]+src="([^"]+)"', html)
    cleaned = [src for src in sources if keep_image(src)]
    return "; ".join(dict.fromkeys(cleaned))



def scrape_article(
    page, collector: ImageCollector, url: str, comment_timeout_ms: int, rest_mode: bool = True
) -> Article:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    collector.reset()
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_selector(".article__body", timeout=30_000)
    settle_page(page)
    # Lazy images only fetch when they approach the viewport; walk them so the
    # response listener captures every one of them.
    for element in page.query_selector_all(".article__body img"):
        try:
            element.scroll_into_view_if_needed(timeout=1_500)
        except Exception:  # noqa: BLE001
            continue
    page.wait_for_timeout(600)

    title = page.evaluate(
        """() => {
            const node = document.querySelector('.page-topper__title');
            if (!node) return '';
            const clone = node.cloneNode(true);
            clone.querySelectorAll('.page-topper__title__name').forEach(n => n.remove());
            return clone.textContent.replace(/\\s+/g, ' ').trim();
        }"""
    )

    author_raw = page.inner_text(".page-topper__title__name").strip()
    author = html.unescape(re.sub(r"^by\s+", "", author_raw, flags=re.IGNORECASE))

    published = page.get_attribute("time.page-topper__date", "datetime") or ""
    if not published:
        published = page.inner_text("time.page-topper__date").strip()

    content = WHITESPACE.sub(" ", html.unescape(page.inner_text(".article__body")).strip())

    # The DOM is the source of truth for *which* images belong to the article;
    # the network layer only supplies their bytes for the download step.
    dom_sources = page.eval_on_selector_all(
        ".article__body img",
        "nodes => nodes.map(n => n.currentSrc || n.src).filter(Boolean)",
    )
    urls = keep_largest(sorted({src for src in dom_sources if keep_image(src)}))

    if rest_mode:
        post = rest_post(slug)
        if post:
            rest_urls = rest_images(post)
            if rest_urls:
                urls = keep_largest(rest_urls.split("; "))
                title = re.sub("<[^>]+>", "", post["title"]["rendered"]).strip() or title
                published = post.get("date") or published
                author_node = (post.get("_embedded") or {}).get("author") or [{}]
                author = author_node[0].get("name") or author

    return Article(
        title=title,
        author=author,
        comment_count=read_comment_count(page, comment_timeout_ms),
        published=published,
        content=content,
        images="; ".join(sorted(urls)),
    )


FETCH_JS = """async (url) => {
    const response = await fetch(url, { credentials: 'include' });
    if (!response.ok) return null;
    const buffer = new Uint8Array(await response.arrayBuffer());
    let binary = '';
    const chunk = 0x8000;
    for (let i = 0; i < buffer.length; i += chunk) {
        binary += String.fromCharCode.apply(null, buffer.subarray(i, i + chunk));
    }
    return btoa(binary);
}"""


def save_images(page, urls: str, article_url: str, root: pathlib.Path) -> int:
    """Fetch each image from inside the page so the request carries the browser session."""
    items = [u.strip() for u in urls.split(";") if u.strip()]
    if not items:
        return 0
    slug = article_url.rstrip("/").rsplit("/", 1)[-1] or "article"
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)

    saved = 0
    for index, image_url in enumerate(items, 1):
        try:
            encoded = page.evaluate(FETCH_JS, image_url)
            if not encoded:
                continue
            body = base64.b64decode(encoded)
            if not body:
                continue
            suffix = pathlib.Path(urllib.parse.urlparse(image_url).path).suffix or ".jpg"
            (folder / f"{index:02d}{suffix}").write_bytes(body)
            saved += 1
        except Exception:  # noqa: BLE001 - a single broken image must not stop the run
            continue
    return saved


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
    parser.add_argument("--max-pages", type=int, default=10_000, help="stop after N listing pages")
    parser.add_argument("--max-posts", type=int, default=None)
    parser.add_argument("--out", default="blogs_playwright.csv")
    parser.add_argument("--image-dir", default="images_playwright")
    parser.add_argument("--comment-timeout", type=int, default=8_000)
    parser.add_argument(
        "--no-rest", action="store_true", help="ignore the WordPress REST API"
    )
    args = parser.parse_args()

    collector = ImageCollector()
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
        page.on("response", collector.handler)

        links = collect_links(page, args.max_pages, args.max_posts)
        print(f"total {len(links)} article(s)", file=sys.stderr)

        articles: list[Article] = []
        for index, url in enumerate(links, 1):
            started = time.perf_counter()
            try:
                article = scrape_article(
                    page, collector, url, args.comment_timeout, rest_mode=not args.no_rest
                )
            except Exception as exc:  # noqa: BLE001 - one bad page must not stop the run
                print(f"[{index}/{len(links)}] FAILED {url}: {exc}", file=sys.stderr)
                continue
            saved = save_images(page, article.images, url, pathlib.Path(args.image_dir))
            articles.append(article)
            print(
                f"[{index}/{len(links)}] {article.title[:36]:36s} "
                f"comments={article.comment_count or '-':>3s} "
                f"images={saved:>2d} chars={len(article.content):>6d} "
                f"{time.perf_counter() - started:.1f}s",
                file=sys.stderr,
            )
            time.sleep(0.4)

        browser.close()

    write_csv(articles, args.out)
    print(f"wrote {len(articles)} row(s) -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
