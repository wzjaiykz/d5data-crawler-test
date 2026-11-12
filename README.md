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
