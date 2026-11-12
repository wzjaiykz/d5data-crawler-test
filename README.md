# d5data-crawler-test

Crawl <https://mitadmissions.org/blogs/> with **both** Playwright and
DrissionPage, and write the same CSV from each implementation:

```
Title | Author | Comment Count | Time | Article Content | Images In Article
```

## Why two implementations

The task asks for both libraries. Writing the same crawler twice is also the
cheapest way to check correctness: if two independent drivers extract the same
data, the selectors and wait strategy are probably right.
`compare.py` does that comparison.

## Requirements

- Python 3.10+
- Chrome or Edge installed (both drivers reuse it)

```bash
pip install -r requirements.txt
playwright install chromium
```

## Run

```bash
# both crawlers (the listing page currently has ~44 posts)
python crawl_playwright.py
python crawl_drissionpage.py

# compare the two outputs
python compare.py blogs_playwright.csv blogs_drissionpage.csv
```

Useful flags:

```bash
python crawl_playwright.py --max-posts 5 --out sample.csv
python crawl_drissionpage.py --max-posts 5 --out sample_dp.csv
python crawl_playwright.py --comment-timeout 6000   # give up on Disqus sooner
```

## How the fields are extracted

| Column | Source |
|---|---|
| Title | `.page-topper__title`, with the nested author link removed |
| Author | `.page-topper__title__name` ("by Nicholas R. '29" → "Nicholas R. '29") |
| Comment Count | the Disqus iframe (`#disqus_thread iframe`), text "N Comments" |
| Time | `time.page-topper__date[datetime]` (ISO-8601) |
| Article Content | `.article__body` visible text |
| Images In Article | unique `src` values of `.article__body img` |

## Details that mattered

1. **Lazy loading.** Images below the fold only load after scrolling, so both
   drivers scroll the document first (`scroll_through` / `settle_page`) and wait
   for the network to go idle before reading the body.
2. **Disqus is a third-party iframe.** The comment count is not in the HTML at
   all; it is read from the iframe and treated as best effort — a blocked or
   slow Disqus yields an empty value instead of failing the row.
3. **DrissionPage needs the `css:` prefix** for these selectors
   (`css:.article__body`); the bare `.article__body` form matched nothing,
   which is why the first run returned empty content.
4. **Polite crawling.** 0.5 s between detail pages, one browser instance reused
   for the whole run.

## Pagination

The listing is paginated (`/blogs/page/N/`, ~492 pages). Both crawlers walk it
until it stops producing new articles and support `--max-pages`:

```bash
python crawl_playwright.py --max-pages 3
python crawl_drissionpage.py --max-pages 3
```

**The trap at the end:** WordPress renders a "next" link that wraps around to
page 1 on the last page, so following the link would loop forever. The stop
condition is therefore "this page yielded no new article URLs" rather than
"there is no next link" — the same guard also catches an HTTP 404/500 page.

Comment threads are **not** paginated on this site: the Disqus embed renders the
whole thread at once. The crawler still clicks a "load more comments" control up
to three times if one ever appears, then reads the count.

## Images

`Images In Article` holds the image URLs, `;`-separated — not a count:

```
https://mitadmissions.org/wp-content/uploads/2026/11/image1-800x533.jpg; ...
```

- Playwright collects candidates from the **network layer**
  (`response.request.resource_type == "image"`, the same view as DevTools' Img
  filter) and uses the DOM to decide which of them belong to the article body.
- DrissionPage reads `.article__body img` and resolves relative srcs with
  `urljoin`.
- Emoji sprites (`s.w.org/...`), avatars, gravatar and Disqus CDN images are
  filtered out.
- Files are also written to `images_playwright/<slug>/01.jpg`, ... Playwright
  reuses the bytes the browser already downloaded because plain `requests` calls
  are blocked by Cloudflare; DrissionPage uses `page.download()`.

## The shortcut: the site exposes a WordPress REST API

Instead of guessing from the DOM, the crawler reads the same JSON the site's own
JavaScript uses:

```bash
curl 'https://mitadmissions.org/wp-json/wp/v2/posts?per_page=1&_embed=1&slug=<slug>'
```

That single request returns title, date, author (`_embedded.author`), the full
rendered content and **every** image URL — 58 for a post whose DOM only showed
48, because lazy images below the fold never load.

So the crawler is a hybrid:

| Field | Source |
|---|---|
| Title, Author, Time, Article Content, Image URLs | WordPress REST API |
| Comment Count | Disqus iframe in the browser (the REST API reports 0 comments) |

Playwright is still the driver: it renders the article, forces each lazy image
into view (`scroll_into_view_if_needed`) so the response listener captures the
bytes, and reads the Disqus frame. Use `--no-rest` to fall back to pure DOM
extraction.

Current coverage on a 58-image post: 58 URLs in the CSV, 46 files captured. The
missing files are duplicate size variants the browser never requests.


### Same image, several sizes

WordPress writes size variants next to the original (image1-400x300.jpg,
image1-800x533.jpg, image1.jpg). image_rules.keep_largest() groups URLs by
their file name, keeps one per upload and prefers the largest pixel area (the
suffix states the size, so no image analysis is involved).

Measured on a real post: 58 URLs in, 58 out - that article references 58
distinct uploads, so there was nothing to collapse. The helper still matters for
posts that embed several sizes of the same picture.

## Getting the image bytes past Cloudflare

Plain HTTP clients (`requests`, `httpx`, even Playwright's own
`context.request.get`) receive **403** from the site's Cloudflare rules, because
those requests do not carry a browser session.

Two browser-native routes are used instead:

| Driver | How the bytes are fetched |
|---|---|
| Playwright | `page.evaluate()` runs `fetch(url, {credentials:'include'})` inside the article's origin, then the body comes back as base64 |
| DrissionPage | `page.download(url, ...)` uses the browser's own download path |

Result on a post with 58 images: **58 URLs, 58 files**. Before this change the
crawler could only keep the images the browser happened to load (46 of 58).

The lesson is not "bypass Cloudflare" but "use the session the browser already
has" - the same session a human visitor uses.

## Stress test scope

The committed CSVs come from a multi-page run, not a single page:

```bash
python crawl_playwright.py --max-pages 3     # ~48 articles
python crawl_drissionpage.py --max-pages 2   # ~32 articles
```

The single-page run that `--max-pages 1` produces is the quick smoke test.

## Cross-check results (16 posts on the listing page)

```
python compare.py blogs_playwright.csv blogs_drissionpage.csv
```

| Field | Agreement |
|---|---|
| Title | 16/16 (used as the join key) |
| Author | 16/16 (100%) |
| Comment Count | 16/16 (100%) |
| Time | 16/16 (100%) |
| Images In Article | 16/16 (100%) |
| Article Content | 13/16 at >= 0.95 similarity |

The three content differences come from one concrete behaviour: **DrissionPage's
`.text` includes image `alt` text** (for example "An arrow pointing right"),
while Playwright's `inner_text()` does not. The same post therefore carries ~300
extra characters under DrissionPage, while the extracted prose itself is
identical.

That is exactly what a second implementation is for: the data is the same, the
text-extraction semantics are not.

## Output sample

```
Title,Author,Comment Count,Time,Article Content,Images In Article
Slinging π’s,Nicholas R. '29,9,2026-11-10T00:00:00-05:00,"The other day I was walking to Next House...",0
Something to believe in,Janet G. '27,20,2026-11-05T00:00:00-05:00,"...",48
```

## Known limitations

- Only the first listing page is crawled; pagination is not followed.
- The comment count depends on Disqus being reachable at crawl time.
- Article Content is visible text (no markup), so formatting such as headings is
  flattened into the same string.
