# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

## [0.1.0] - 2026-10-06

First release: public Facebook Pages, read logged out. Run live on
2026-10-06 with all three engines and the Scraper API mode (see
`TESTING.md`).

### Added

- Input by Page URL (any facebook.com host, any tab), username,
  `@username` or numeric profile / Page id (`--url`, `--urls-file`); the
  same Page by any of them is one row, keyed on the Page id.
- One row per Page: name, ids, category, description, verified badge,
  followers / following / likes (rounded, as Facebook shows them, with
  the original text), website, phone, e-mail, address, hours, price
  range, recommendation rate and review count, the confirmed legal owner,
  a linked account, profile and cover pictures — the intro card read by
  item type.
- `--posts 0-3`: the posts a logged-out visitor is shown, with exact
  reaction, comment, share and video-view counts; the embedded post and
  the two from the timeline response the page makes on load, read off the
  wire in all three engines.
- "This content isn't available right now" is `not_found` (listed, never
  retried, never a block); a personal profile is `not_a_page` and nothing
  about it is written.
- Every Page is opened with `locale=en_US`: the page language follows the
  browser, and a non-English page's rounded counts and intro labels would
  go unread (measured with pyppeteer on a Russian-language machine).
- `--exact-likes`: each Page's EXACT like count from Meta's Ad Library
  (`likes_exact`), where the Page itself shows only a rounded figure —
  for Pages that have run ads; only the Page's own ads are counted.
- `--scraper-api`: one 2Captcha Scraper API call per Page, no browser; the
  Page and its first post.
- `diff_runs.py` watches Page and post fields; a post pushed out of the
  three shown is `left_selection`, a Page is `removed` only when its
  address is gone.
- Offline suite on fixtures cut from real captures, a failure and
  recovery suite, CI on Python 3.9 and 3.12 with a wheel, Docker and
  per-engine job, and a daily canary that needs a `FACEBOOK_PROXY` secret
  and skips without one.
- Hardening found by live runs on 2026-10-06, shared by all three
  facebook-* repos: pyppeteer's browser cleanup is bounded (it could hang
  for 20+ minutes after a proxy closed the connection), a login page
  served under the asked-for address is a block (exit 3) recognised by
  its canonical link, and an empty Scraper API answer is retried as an
  API error.
- Hardening from a security review before release: an unparseable proxy
  line is reported without echoing it (it can carry a password); the
  pyppeteer engine answers only its OWN proxy's auth challenge, once per
  request, and cancels any other (a site's HTTP auth never sees the
  proxy's credentials); CSV cells that a spreadsheet would run as a
  formula are escaped (`csv_text_encoding: apostrophe-v1`, undone by
  `diff_runs.py`); output files and sidecars are replaced atomically.
