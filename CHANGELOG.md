# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[SemVer](https://semver.org/) as closely as a CLI toolkit can: a patch
release means "fixes", not that every flag and default is frozen — a fix
that changes a default is called out at the top of its entry.

## [Unreleased]

> **Behaviour changes.** (1) A bot challenge that does not clear is now
> reported as blocked (exit 3) whatever HTTP status it came with; under
> HTTP 200, or with Selenium (which has no status), it used to end as a
> fetch error (exit 5). (2) Selenium and pyppeteer now refuse a negative
> `--retries`, `--retry-delay`, `--delay-between-pages`, `--max-solves`
> or `--proxy-block-retries` with exit 2, as Playwright already did for
> most of them; a negative `--retries` used to run no navigation at all.
> `--proxy-block-retries` is now validated in all three engines.
> (3) Rewriting an existing output file keeps that file's mode (a 0640
> file stays 0640); it used to be reset to 0666 minus the umask.
> (4) `diff_runs.py` exits 2, not 1, when it refuses a comparison, so
> `--fail-on-change`'s exit 1 means only "something changed".

### Changed

- The canary no longer skips green without a proxy secret: it reads three
  live Pages from the bare GitHub runner every day and is red on a block,
  zero rows, a fetch failure or a partial run. `FACEBOOK_PROXY` is used
  when set, not required.

### Fixed

- A `FACEBOOK_URL` set in the environment or `.env` no longer replaces a
  `--urls-file` typed on the command line. `.env.example` ships the
  variable commented out.
- `diff_runs.py` watches `likes_exact` (with a delta), and marks a Page
  `removed` when the address it was read from (for example a
  `profile.php?id=` input) is the one the new run found gone, compared
  case-insensitively. The sidecar's `pages` entries now carry the Page's
  `sku` for that join.
- The CSV sidecar records `csv_cells_escaped`, the number of cells the
  formula guard changed, beside `csv_text_encoding`.
- The credential scan no longer exempts `smoke_test.py` (its
  credential-shaped fixtures are assembled from pieces), prints how many
  files it read, and fails if it read almost none.
- Source comments, check labels and workflow prompts state their rules
  in this repository's own words instead of pointing at a file outside
  it; a smoke check keeps it that way. Comments no longer name modules
  that do not exist here.

## [0.1.1] - 2026-10-06

Text only — no change to what is read or written.

### Fixed

- `--max-solves` help: the local solver is disabled, so it caps nothing
  today (the README already said so); the landing pages no longer promise
  a browser-specific fingerprint (`--fp-tags` filters by OS only).
- The exit-code table lists every `stop_reason` a partial run can carry.
- README says what was cross-checked on the 67 captured posts (the reaction count); TESTING names the run sample_output.* comes from and dates both exact-likes measurements; the --scraper-api row no longer sits under 'no key'; the landing page's diff example matches its CSV export.

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
