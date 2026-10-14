# facebook-pages-scraper

![release](https://img.shields.io/github/v/release/2scraper/facebook-pages-scraper?sort=semver)
![tests](https://github.com/2scraper/facebook-pages-scraper/actions/workflows/tests.yml/badge.svg)
![canary](https://github.com/2scraper/facebook-pages-scraper/actions/workflows/canary.yml/badge.svg)
![python](https://img.shields.io/badge/python-3.9%2B-blue)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-informational)
![no login](https://img.shields.io/badge/runs%20without-an%20account-success)

**Scrape public Facebook Pages into clean JSON or CSV, without logging
in.** Give it a Page URL, a username or a numeric id, or a file of them;
get one row per Page — name, category, description, verified badge,
followers, website, phone, e-mail, address, opening hours, price range,
rating and the legal owner Meta confirmed — and, with `--posts`, its
latest posts with their EXACT reaction, comment, share and video-view
counts.

- **No account, no cookies, no key.** It reads what Facebook shows any
  logged-out visitor: the data a Page embeds in its own page.
- **Measured live on 2026-10-06** from an ordinary residential IP with no
  key and no proxy: 10 Pages and their 30 posts in 50 seconds, every Page
  read. All three engines and the browserless Scraper API mode were run
  live.
- **Honest results.** A missing Page, a block or a partial run says so in
  its exit code and a `.meta.json` file next to the output. A run that
  finds nothing never overwrites your last good data.
- **Three browser engines** (Playwright, Puppeteer, Selenium) running one
  shared fetch loop, a browserless **Scraper API** mode, rotating proxies,
  2Captcha's **Scraping Browser API** over CDP, and a run-to-run diff tool.

Companion repos: [facebook-ads-scraper](https://github.com/2scraper/facebook-ads-scraper)
for a Page's ads in Meta's Ad Library (with the Page's EXACT like count),
and [facebook-marketplace-scraper](https://github.com/2scraper/facebook-marketplace-scraper).

## Quick start

```bash
git clone https://github.com/2scraper/facebook-pages-scraper.git
cd facebook-pages-scraper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium

python3 playwright_scraper.py --url nike --posts 3
```

Results land in `facebook_pages.json`, with
`facebook_pages.json.meta.json` beside it. No `.env` is needed for this.

**When you need more than that:** for long lists, a specific exit
country, or no browser on your machine, put a 2Captcha residential proxy
or key in `.env` (`cp .env.example .env`, `FACEBOOK_PROXY=...`,
`TWOCAPTCHA_KEY=...`). `python3 env_config.py` shows what was picked up,
without printing secrets.

## Examples

```bash
# one Page, by username, URL or numeric id — all three give the same row
python3 playwright_scraper.py --url nike
python3 playwright_scraper.py --url https://www.facebook.com/nytimes
python3 playwright_scraper.py --url "https://www.facebook.com/profile.php?id=100044541544829"

# a list (one per line, # comments allowed), each Page with its 3 latest posts, as CSV
python3 playwright_scraper.py --urls-file pages.txt --posts 3 --format csv

# the same with Puppeteer, or Selenium
python3 puppeteer_scraper.py --url bbcnews --posts 3
python3 selenium_scraper.py --url bbcnews --posts 3

# without a browser: 2Captcha's Scraper API (needs TWOCAPTCHA_KEY) — the Page and its first post
python3 playwright_scraper.py --scraper-api --url derspiegel --posts 1

# compare two runs
python3 diff_runs.py monday.json tuesday.json
```

## Sample output

Rows of `sample_output.json`, cut from a live run with `--posts 1
--exact-likes` on 2026-10-06 (media URLs shortened here). A Page:

```json
{
  "sku": "facebook-page-15087023444",
  "source": "facebook.com",
  "category": "page",
  "title": "Nike",
  "brand": null,
  "price": null,
  "currency": null,
  "price_source": null,
  "product_url": "https://www.facebook.com/nike",
  "image_url": "https://scontent.fakx3-1.fna.fbcdn.net/v/t39.30808-1/2849640…",
  "scraped_at": "2026-10-06T12:44:31Z",
  "page_id": "15087023444",
  "profile_id": "100044541544829",
  "username": "nike",
  "page_category": "Sportswear Store",
  "description": "Just Do It.",
  "is_verified": true,
  "followers": 39000000,
  "followers_text": "39M followers",
  "following": 24,
  "likes": null,
  "likes_exact": 39506247,
  "website": "http://nike.com/",
  "phone": "+972 54-292-7777",
  "email": null,
  "address": null,
  "hours": null,
  "price_range": null,
  "recommend_pct": null,
  "review_count": null,
  "owner": "NIKE, Inc.",
  "other_accounts_json": null,
  "cover_url": "https://scontent.fakx3-1.fna.fbcdn.net/v/t39.30808-6/2852112…",
  "post_id": null,
  "posted_at": null,
  "text": null,
  "reactions": null,
  "comments": null,
  "shares": null,
  "video_views": null,
  "position": null
}
```

And its post (the Page columns are empty on a post row, except
`page_id` and `username`):

```json
{
  "sku": "facebook-post-1393461115481927",
  "category": "post",
  "title": "Leave your limits at the surface. #JustDoIt",
  "product_url": "https://www.facebook.com/reel/2166091230582141/",
  "page_id": "15087023444",
  "username": "nike",
  "post_id": "1393461115481927",
  "posted_at": "2025-09-15T16:01:59Z",
  "text": "Leave your limits at the surface. #JustDoIt",
  "reactions": 8200,
  "comments": 2644,
  "shares": 1157,
  "video_views": 496072,
  "position": 1
}
```

What the columns mean:

| Column | |
|---|---|
| `sku` | `facebook-page-{page_id}` / `facebook-post-{post_id}`. A Page reached by its username, its profile id or its Page id is one row |
| `page_id` | the Page's id — the same id the Ad Library uses (`advertiser_page_id` in facebook-ads-scraper) |
| `profile_id`, `username` | the Page's profile id and its address name (none when it has only a numeric address) |
| `page_category`, `description`, `is_verified` | as the Page shows them; `is_verified` is the blue badge |
| `followers`, `following`, `likes` | **rounded by Facebook** ("39M followers" → 39000000); `followers_text` keeps the original. The exact figure is not shown on the Page to a logged-out visitor |
| `likes_exact` | with `--exact-likes`: the Page's **exact** like count, read from Meta's Ad Library (see below) — only for a Page that has run ads |
| `website`, `phone`, `email`, `address`, `hours`, `price_range` | from the Page's intro card, each read by its item type; the website is taken out of Facebook's `l.php` redirect |
| `recommend_pct`, `review_count` | from "32% recommend (37,633 reviews)" |
| `owner` | the legal owner Meta confirmed for the Page ("NIKE, Inc.") |
| `other_accounts_json` | a linked account the intro card names (an Instagram handle) |
| `post_id`, `posted_at`, `text` | a post's id, time (UTC) and full text; `title` is its first line (at most 120 characters) |
| `reactions`, `comments`, `shares`, `video_views` | **exact** counts as Facebook sends them (the reaction count checked against the displayed count on all 67 captured posts) |
| `position` | 1 = the first post the Page shows |

Media URLs are signed CDN links and stop working after some days.

## How a Page is read

1. Open the Page logged out. Facebook answers HTTP 200 and draws a "Log
   in" dialog over the page; the Page's data is in the page underneath.
2. Wait for the data (`profile_header_renderer`), up to 20 seconds.
3. Read the header (name, ids, followers, verified badge, pictures), the
   Page object (category, description) and the intro card. The intro
   card's items carry their type (`INTRO_CARD_WEBSITE`,
   `INTRO_CARD_PROFILE_PHONE`, `INTRO_CARD_ADDRESS`, …), so a phone number
   is never mistaken for a company name.
4. With `--posts`: the page embeds its first post and asks for the next
   two on load (`ProfileCometTimelineFeedRefetchQuery`). The engine reads
   that response off the wire, waiting up to 15 seconds for it.

**Three posts is all a logged-out visitor gets.** Scrolling the Page
brought no more (4 scrolls, still 3 post ids, measured 2026-10-06), so
`--posts` takes 0–3.

An address that is no Page — a misspelling, a removed Page — is answered
with HTTP 200 and "This content isn't available right now". The tool
reports it as **not found** (listed in the sidecar's `not_found_urls`),
never as a block, and does not retry it.

## Exact like counts (`--exact-likes`)

A Page shows a logged-out visitor a rounded figure ("39M followers").
Meta's Ad Library carries the Page's EXACT like count on every ad it has
run, so with `--exact-likes` the tool opens the library's search for the
Page's own id (one more page load per Page) and writes that number to
`likes_exact`. Measured 2026-10-06: Nike 39,506,247, BBC News 62,043,921.
Only the Page's own ads count — a branded-content ad can name another
Page (one Nike ad carried a creator's 1,478). A Page that has never run
ads has no such number: `likes_exact` stays empty and the sidecar says
`exact_likes: no_ads`. A failed lookup never fails the Page.

## Private persons are skipped

This tool reads Pages — businesses, brands, media, public figures'
Pages. An address that leads to a person's own profile (no Page behind
it) is reported as `not_a_page` in the sidecar and **nothing about it is
written**. Nothing about the people who comment on or react to a Page's
posts is collected either; only the counts.

## Captchas

None, so far. No page in the live runs of 2026-10-05 and 2026-10-06
(Playwright, Puppeteer and Selenium, direct and through a residential
proxy, and the Scraper API) carried a captcha widget, iframe or
challenge. The generic captcha detection stays on, so a real one would
be reported as blocked (exit 3) rather than read as data, whatever HTTP
status it came with.

## Volume and blocks

From one residential IP on 2026-10-05 and 2026-10-06, more than 40 Page
loads over the two days read normally, 10 of them back to back at the default
2-second pace. We have not found where the limit is. If Facebook starts
refusing an address, the tool reports it as **blocked** (a login page, or
a page that is not facebook.com's under 401/403/429), never as a missing
Page. Without a proxy pool it stops after 3 blocked answers in a row and
reports the rest as `not_attempted`. To read more: rotate residential
exits with `--proxy-file`, and keep `--delay-between-pages` at 2 seconds
or more.

**A datacentre address has not been measured yet.** The daily canary
runs from a GitHub runner only when a proxy secret is configured.

## Scraper API mode

With `--scraper-api` no browser is driven: each Page is one 2Captcha
Scraper API call (`TWOCAPTCHA_KEY` in `.env`), routed through the
Scraping Browser profile in `FACEBOOK_CDP_ENDPOINT` when one is set. The
Page's own data is all there; of its posts, only the **first** (the one
the page embeds) — the other two arrive only in a request a live page
makes. Measured on 2026-10-06 on the Scraper API's own pool: DER SPIEGEL
and its first post, exit 0. A refused key or an empty balance stops the
run at once with exit 5.

## Run results and exit codes

Every run writes `<out>` and `<out>.meta.json`: status, `stop_reason`,
per address what happened (`ok`, `not_found`, `not_a_page`, a failure)
and how many posts it showed, `not_found_urls`, `not_a_page_urls`,
failed addresses with reasons, whether `--max-results` capped the list,
solves spent, and a hash of the output file. A run that collects nothing
writes neither, so it never replaces your previous good file.

| Exit | Meaning |
|---|---|
| `0` | complete (not-found and personal-profile addresses are listed, not failures) |
| `6` | partial: rows were written, but the run did not finish cleanly — `stop_reason` says why (`blocked`, `not_painted`, `failed_pages`, `rejected_rows`, `parse_error`, `remote_api_error`, `proxy_pool_exhausted`) |
| `3` | blocked, no rows |
| `4` | no rows: no address was a Page |
| `5` | nothing could be read (fetch, parse or Scraper API failure); no rows |
| `2` | bad usage, including input where every line was skipped |
| `1` | crash (a bug; please report it) |

**CSV and safe writes.** In a CSV export, text that a spreadsheet would
run as a formula (starting with `=`, `+`, `-` or `@`, or with a literal
leading apostrophe) gets a leading `'`; the sidecar records this
(`csv_text_encoding: apostrophe-v1`, and `csv_cells_escaped` with how
many cells were changed) and `diff_runs.py` restores the original text
before comparing. A phone number written `+1 …` is one of them: in the
CSV it reads `'+1 …`. JSON is unchanged. Every output file and
sidecar is written to a temporary file first and swapped in only once
complete, so a failed write never leaves a half-written file over the
previous good one.

## Monitoring changes

```bash
# what changed between two runs, as JSON
python3 diff_runs.py monday.json tuesday.json --json

# only some fields, and exit 1 if anything changed (for cron or CI)
python3 diff_runs.py monday.json tuesday.json --fields followers,website,phone,reactions --fail-on-change
```

`diff_runs.py` matches rows by `sku` and lists, for each, the old and
new value of every watched field — the Page's name, category,
description, badge, follower/following/like counts, contacts, hours,
price range, rating and owner, the exact like count, and each post's text and counts — with a
`delta` for numbers. Remember that follower counts are rounded by
Facebook: "39M" to "40M" is a change of the displayed figure.

A post missing from the next run has usually just been pushed out of the
three a Page shows, so it is `left_selection`, not `removed`. A Page is
`removed` only when the new run found its address gone (not found) —
the address the Page was read from, or the one the new run was given
for it. The
diff refuses runs of different inputs, `--posts` or `--max-results`, a
run that did not complete, and a `.meta.json` that does not match its
file, with exit 2.

## Options

Same flags for all three engines. Credentials go in `.env`
(`TWOCAPTCHA_KEY`, `FACEBOOK_PROXY`, `FACEBOOK_CDP_ENDPOINT`), never on
the command line.

| Option | Default | |
|---|---|---|
| `--url` / `--urls-file` | `FACEBOOK_URL` (for `--url` only) | what to read: a Page URL (any tab — `/about` is read as the Page), a username, `@username` or a numeric id; a file has one per line. A typed `--urls-file` always wins over `FACEBOOK_URL` |
| `--posts` | 0 | also write each Page's latest posts, 0–3 |
| `--exact-likes` | off | also read each Page's exact like count from the Ad Library (one more page load per Page) |
| `--max-results` | 100 | Pages to read in total (posts not counted); the rest of the list is left out and the run marked `capped` |
| `--delay-between-pages` | 2s | pause between Pages |
| `--format` / `--out` | json / `facebook_pages.<format>` | output format and path |
| `--proxy` / `--proxy-file` / `--proxy-shuffle` | `FACEBOOK_PROXY` | one proxy or a rotating pool, for a local browser |
| `--proxy-block-retries` | 3 | blocked answers before that proxy is dropped from the pool |
| `--scraper-api` | off | no browser: fetch each Page through 2Captcha's Scraper API (needs `TWOCAPTCHA_KEY`) — the Page and its first post |
| `--cdp-endpoint` | `FACEBOOK_CDP_ENDPOINT` | connect to a Scraping Browser API profile instead of launching a browser |
| `--solve-captcha` | when-blocked | `off` disables the Browser API's own captcha auto-solve; there is no local solver |
| `--max-solves` / `--min-score` | 8 / 0.3 | kept for the local solver, which is disabled; they change nothing today |
| `--retries` / `--retry-delay` | 2 / 3s | navigation retries per Page (extra attempts: `0` means one navigation). Negative numbers for these, `--delay-between-pages`, `--max-solves` and `--proxy-block-retries` are refused with exit 2 by every engine |
| `--fingerprint` / `--fp-tags` / `--fp-country` | off | apply a 2Captcha Fingerprint API user agent (local browsers only) |
| `--dump-html` | off | save each Page's HTML next to the output, for debugging |
| `--allow-empty` | off | write an output file even when nothing was found |
| `--headless` / `--headful` | headless | show the browser window |

An input that is no Page address (a group, an event, a post, Marketplace,
another site) is logged and skipped, never requested. Run any engine
with `--help` for the full list.

## Engines

- **Playwright** (`playwright_scraper.py`) is the recommended engine.
- **Puppeteer** (`puppeteer_scraper.py`, via pyppeteer) supports the same
  modes. pyppeteer itself is no longer maintained, and its own proxy login
  no longer works on current Chromium, so this engine answers the proxy's
  password prompt itself (over CDP `Fetch`).
- **Selenium** (`selenium_scraper.py`) runs a local Chrome only and reads
  the posts' response from Chrome's performance log. chromedriver cannot
  authenticate a Scraping Browser endpoint (the run exits 2 before
  fetching; use `--scraper-api` instead), and its `--proxy-server` cannot
  use a proxy password (the credentials are stripped, with a warning; a
  proxy that needs its password then answers HTTP 407 and the run says
  so — allow your IP in the proxy's settings instead).

Install one engine per virtualenv (`requirements-playwright.txt`,
`requirements-puppeteer.txt`, `requirements-selenium.txt`). Their
dependencies conflict with each other.

All three engines share one fetch loop (`page_flow.py`), so they agree on
results, exit codes and when money is spent. Docker:
`docker build -t facebook-pages-scraper .` gives an image with Playwright
and Chromium.

## Known limitations

- **Follower counts are rounded** by Facebook for a logged-out visitor
  ("39M"); the tool does not pretend otherwise. The exact LIKE count is
  available with `--exact-likes` for Pages that have run ads; an exact
  follower count is not.
- **Three posts per Page**, in the order the Page shows them.
- **No comments, reviews or the people behind them**, no photos and
  videos tab, no events, no About-tab-only fields beyond what the intro
  card shows.
- **Groups, events and personal profiles are out of scope.**

## Is this allowed?

Facebook's `robots.txt` disallows crawlers, and Meta's terms forbid
automated collection without its permission. This tool reads only what
a public Page shows any logged-out visitor, does not log in, skips
personal profiles and collects nothing about the people who interact
with a Page. Whether your use is permitted depends on your jurisdiction
and purpose — check before you run it.

## Development

```bash
python3 smoke_test.py            # offline checks, no network, no engine needed
python3 .github/ci_checks.py     # credential scan
python3 -m unittest discover -s tests -p 'test_*.py'  # failure and recovery scenarios
```

Parser and flow checks run on fixtures cut from real captures in
`tests/fixtures/`. CI runs the offline suite on Python 3.9 and 3.12,
installs the built wheel outside the checkout, builds the Docker image
and launches Chromium in it, and runs each engine in its own virtualenv.
`TESTING.md` describes live testing; `CHANGELOG.md` has the history.

## Licence

MIT, see `LICENSE`.
