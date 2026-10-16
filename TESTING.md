# Testing with real credentials and the live site

**Live status, 2026-10-06.** From one residential IP, logged out, no
proxy, and no key except for the `--scraper-api` row:

| Engine | Run | Result |
|---|---|---|
| Playwright | `--urls-file` (10 Pages) `--posts 3` | 10/10 Pages, 30/30 posts, exit 0, 50s |
| Playwright | `--urls-file` (Nike by username, by profile URL; The New York Times; Shake Shack; a non-existent name; a group URL) `--posts 3` | 3 Page rows (Nike once), 9 posts; the non-existent name `not_found`; the group skipped unrequested; exit 0 |
| Selenium | `--url bbcnews --posts 3` | 1 Page, 3 posts, exit 0 |
| Puppeteer | `--url cnn --posts 3` | 1 Page, 3 posts, exit 0 |
| `--scraper-api`, own pool | `--url derspiegel --posts 3` | the Page and its first post (the other two need a live page), exit 0 |
| Playwright | the canary's three Pages | the canary's own assertions pass |

Coverage on the 10-Page run: category, description, verified badge and
followers 10/10; website 9/10; owner 9/10; phone 4/10; e-mail 4/10;
address 2/10. Post time, text, reactions, comments and shares 30/30. The
reaction count of all 67 posts in the captures matches the count the
page displays.

Recon, same day: the Page by `/profile.php?id=<profile id>` and by
`/<Page id>` redirects to its own address; four names that are no Page
all answered HTTP 200 with "This content isn't available right now";
scrolling a Page four times brought no fourth post.

**Second pass, same day — 16 live scenarios, all with the expected exit
code and rows:** Playwright (a mixed list with a duplicate, a missing
Page and a group URL; CSV; `--max-results 2`; not found → 4; bad input →
2; `--posts 4` → 2), Selenium and Puppeteer direct, Playwright and
Puppeteer through a 2Captcha residential proxy and through the Scraping
Browser API, Selenium refusing the endpoint (2), the Scraper API on its
own pool and through the profile, and a bad key (5). No password in any
log. Two bugs found and fixed on the way:

- **pyppeteer and the Scraper API got the Russian page** on this machine
  ("474 тыс. — подписчики") despite `--lang=en-US`, so `followers` was
  empty. Every engine now opens the address with `locale=en_US`.
- **A second address of a Page already read used up `--max-results`**
  and left a real Page out. The limit now counts distinct Pages.

**Fixed the same day, found by facebook-marketplace-scraper's live runs
in the engine code all three repos share:** pyppeteer's browser cleanup
could hang for 20+ minutes after a proxy closed the connection (now
bounded at 10s, a stuck local Chromium is killed — Puppeteer re-run live
here afterwards: exit 0); a login page served under the asked-for address
(the Scraper API's pool) is now a block, not an unreadable page; an empty
Scraper API answer is now retried as an API error.

**`--exact-likes`, 2026-10-06:** Nike 39,506,221 and BBC News 62,043,912
in a first run, 39,506,247 and 62,043,921 in the run `sample_output.*` is
cut from (`--posts 1 --exact-likes`, Nike and BBC News) — the counts move
between runs
(the Pages show 39M and 62M); a Page with no ads: `likes_exact` empty,
`exact_likes: no_ads`, row kept, exit 0.

**After the pre-release security review, 2026-10-06:** the pyppeteer
engine through the password-protected residential proxy still read
normally (the proxy's own auth challenge answered, nothing else), with no
password in any log; a CSV run records `csv_text_encoding` and
`diff_runs.py` reads it back to the original text.

Not yet run live: `--proxy-file` with several exits, the Docker image,
and any run from a datacentre IP. Each is covered by the offline suite,
but has no live evidence behind it yet.

The quickest real check:

```bash
python3 playwright_scraper.py --url nike --posts 3 --out /tmp/fbp.json
cat /tmp/fbp.json.meta.json        # status: complete, product_count: 4
```

## 1. Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-playwright.txt
playwright install chromium
cp .env.example .env              # only for the paid paths below
python3 env_config.py             # what was picked up, secrets masked
```

## 2. One Page, and what the rows should look like

```bash
python3 playwright_scraper.py --url nike --posts 3 --out /tmp/fbp.json --dump-html
```

- exit 0, `status: complete`, `pages[0].result: ok`, `posts_shown: 3`;
- one `category: page` row with `page_id` 15087023444, `followers` in the
  tens of millions, a website and an owner;
- three `category: post` rows with `posted_at`, `reactions`, `comments`
  and `shares`, `position` 1–3.

Compare against the Page in a browser. `/tmp/fbp_debug_1.html` holds the
page the row was read from.

## 3. The failure answers

```bash
python3 playwright_scraper.py --url zzqx-no-such-page-88123 --out /tmp/nf.json   # exit 4, nothing written
python3 playwright_scraper.py --url https://www.facebook.com/groups/1           # exit 2, nothing requested
python3 playwright_scraper.py --url nike --posts 4                              # exit 2 (0-3)
```

## 4. Puppeteer and Selenium

```bash
pip install -r requirements-puppeteer.txt   # in its own venv
PYPPETEER_EXECUTABLE_PATH=/path/to/chromium python3 puppeteer_scraper.py --url cnn --posts 3

pip install -r requirements-selenium.txt    # in its own venv
python3 selenium_scraper.py --url bbcnews --posts 3
```

Same rows, same exit codes.

## 5. The residential proxy (`--proxy` / `FACEBOOK_PROXY`)

Put `FACEBOOK_PROXY=http://login:password@host:port` in `.env` and run
section 2 again. The log names the exit, never the password. For a pool,
one proxy per line in a file and `--proxy-file proxies.txt --proxy-shuffle`.

## 6. Scraper API mode (`--scraper-api`)

```bash
python3 playwright_scraper.py --scraper-api --url derspiegel --posts 3 --out /tmp/sapi.json
```

Needs `TWOCAPTCHA_KEY`; no browser driver. Expect the Page and one post.
An invalid key must end the run with exit 5 after one call.

## 7. The Scraping Browser API (`--cdp-endpoint`)

```bash
python3 playwright_scraper.py --url nike --posts 3   # with FACEBOOK_CDP_ENDPOINT in .env
```

Selenium refuses a credentialled endpoint up front (exit 2).

## 8. Push to GitHub and let CI do the rest

`tests.yml` runs the offline suite on Python 3.9 and 3.12, builds the
wheel and the Docker image, and runs one `engine-smoke` job per engine.
`canary.yml` reads three live Pages weekly (Mondays) from the bare GitHub runner,
with no key and no proxy (the `FACEBOOK_PROXY` secret is used when set,
never required). It never skips: a block, zero rows, a fetch failure or a
partial run is red. Dispatch it once by hand and read the step results.

## 9. What "done" looks like

Every row in the tables above has a date and a number. Add the open items
from the top of this file to the table as they are run, and update the
README's claims from the same runs.
