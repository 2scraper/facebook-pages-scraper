# Facebook Pages Scraper by 2scraper

**Open-source scraper for public Facebook Pages — contacts, followers, owner and the latest posts with exact engagement, no login, three engines, JSON or CSV.**

Give it Page URLs, usernames or ids and get one row per Page: name, category, description, verified badge, followers, website, phone, e-mail, address, opening hours, price range, rating and the legal owner Meta confirmed — plus, if you want them, its latest posts with exact reaction, comment, share and video-view counts.

[**View source on GitHub →**](https://github.com/2scraper/facebook-pages-scraper)

---

## Before you scrape

Facebook's `robots.txt` disallows crawlers, and Meta's terms forbid automated collection without its permission. This tool reads only what a public Page shows any logged-out visitor, never logs in, skips personal profiles and collects nothing about the people who interact with a Page. Whether your use is allowed depends on your jurisdiction and purpose: check first.

## What to expect

Every row comes from the data a Page embeds in its own page, read by type rather than by guessing at text. Measured live on 2026-10-06 from an ordinary residential IP, with no key and no proxy: 10 Pages and their 30 posts in 50 seconds, every Page read. Playwright, Puppeteer and Selenium were all run live, and the browserless Scraper API mode too. Follower counts are rounded by Facebook ("39M") and reported as such; post counts are exact. A logged-out visitor is shown three posts per Page, so that is the limit here too. Details in the [README](https://github.com/2scraper/facebook-pages-scraper#readme).

## What you get

- Free, open-source scraper, one script per engine — **Playwright** (recommended), **Puppeteer** (via pyppeteer) and **Selenium**, all producing the identical output schema and exit codes
- Page URLs, usernames or numeric ids — one at a time or a batch from a file; the same Page by any of them is one row
- Page fields: name, ids, category, description, verified badge, followers / following, website, phone, e-mail, address, hours, price range, recommendation rate and review count, confirmed owner, linked account, profile and cover pictures
- `--exact-likes`: the Page's exact like count (from Meta's Ad Library) where the Page shows only "39M"
- Post fields (`--posts 1-3`): time, text, exact reactions, comments, shares and video views
- JSON and CSV export, with a documented `Product` schema and a `.meta.json` sidecar that lists missing Pages and skipped personal profiles
- Change monitoring: `diff_runs.py` shows what changed on a Page and its posts between two runs
- A browserless mode (`--scraper-api`) that needs no browser driver installed

## 2Captcha products, when you want them

| Product | What it's for |
|---|---|
| **Proxies — 2captcha.com/proxy** (2prx.com is the same product, different name) | Long lists from several addresses: residential exits in `.env` or `--proxy-file`, rotated per Page with per-exit failure tracking |
| **Scraper API — 2captcha.com** | No browser at all: `--scraper-api` fetches each Page from 2Captcha's side, one HTTP call each (the Page and its first post) |
| **Scraping Browser API — 2captcha.com** | A remote browser session over CDP with its own proxy, fingerprint and captcha auto-solve bundled — `--cdp-endpoint` |
| **Browser fingerprints — 2captcha Fingerprint API** | Pin a specific OS/browser/country fingerprint for a locally-launched browser |

## Who this is for

Lead generation from business Pages' public contacts, brand and media monitoring, and competitor research on Pages' posting and engagement. Groups, events, personal profiles and anything behind a login are out of scope.

## Get started

```bash
git clone https://github.com/2scraper/facebook-pages-scraper.git
cd facebook-pages-scraper
pip install -r requirements-playwright.txt && playwright install chromium

python3 playwright_scraper.py --urls-file pages.txt --posts 3 --format csv --out pages.csv
```

Full setup, CLI reference, and configuration details in the [repository README](https://github.com/2scraper/facebook-pages-scraper#readme).

---

**Need it running at scale, with proxies, fingerprints, and captcha solving already configured?**
[Talk to us →](https://2captcha.com/contact) · Proxies by [2captcha.com/proxy](https://2captcha.com/proxy) · Scraping Browser API & captcha solving by [2captcha.com](https://2captcha.com)
