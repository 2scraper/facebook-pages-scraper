#!/usr/bin/env python3
"""page_flow.py — what one Facebook Page address MEANS, decided once for
all engines (CLAUDE.md §1: three copies of this triage would drift, and the
drift would be silent — one engine reporting exit 3 where its twin reports
0 on the same page).

facebook.com answers a logged-out visitor's Page address these ways, all
seen live on 2026-10-06 (see page_parser.py):

  1. the Page, under HTTP 200 with a login dialog drawn over it → `content`;
  2. the Page route with no header yet → `loading`: WAIT, bounded, never
     retry (a retry re-rolls a page that was about to paint — CLAUDE.md §18);
  3. "This content isn't available right now" (`CometErrorRoute`, also
     HTTP 200) → `not_found`: a complete answer about that address, no
     row, never retried and never called a block;
  4. a login or checkpoint page → `blocked` (not seen on Pages; handled so
     that it cannot pass as "not found");
  5. anything else (Chromium's own error page, an interstitial) →
     `blocked` under 401/403/429, a `fetch_error` otherwise.

A Page whose address leads to a private person's profile (no Page behind
it) is `not_a_page`: reported and skipped, never written.

Posts: the page embeds one and fetches two more on load
(`ProfileCometTimelineFeedRefetchQuery`); with `--posts N` the engine
captures that response and up to N (at most 3) are written after the
Page's row. Nothing is scrolled — scrolling brings a logged-out visitor
no more.

Triage is driver-independent; the shared async flow below consumes NAMED
engine operations — goto, content, current_url, wait, take_responses,
close — and no JavaScript crosses this boundary.
"""
from __future__ import annotations

import logging as _logging
from dataclasses import dataclass, field
from pathlib import Path as _Path
from typing import List, Optional
from typing import Protocol as _Protocol

import page_parser as pp
from captcha_solver import detect_from_html
from output_writer import Product
from output_writer import finish_run as _finish_run
from proxy_pool import is_proxy_dead_error as _is_proxy_dead_error, redact_credentials as _redact

_log = _logging.getLogger("page_flow")

BLOCKING_STATUSES = (401, 403, 429)
# With one exit and no pool to rotate through, a block is per IP: every later
# Page gets the same answer. After this many in a row the run stops and
# reports the rest as not attempted, keeping what it has.
STOP_AFTER_CONSECUTIVE_BLOCKS = 3
PAINT_WAIT_S = 20
UNKNOWN_WAIT_S = 8
POLL_S = 1.0
# The feed request leaves on load; measured 2026-10-06 its answer was in
# within 2-6s of the header on every capture. 15s is generous for a slow
# exit and short enough that a Page with no posts does not hang the run.
FEED_WAIT_S = 15
# --exact-likes opens the Page's own Ad Library search. Its results are
# streamed in after the library's shell — measured on facebook-ads-scraper:
# up to ~10s on a slow load — so the wait is bounded at 25s.
AD_LIBRARY_WAIT_S = 25
CHALLENGE_WAIT_S = 15


def is_challenge(html: str) -> bool:
    """A bot-challenge marker on a page that is not facebook.com's own
    Page or error route."""
    return pp.page_route(html) is None and detect_from_html(html or "", pp.BOT_CHALLENGE_MARKERS)


@dataclass
class PageOutcome:
    url: str
    rows: List[Product] = field(default_factory=list)
    state: str = "unknown"          # content | not_found | login | loading | unknown
    blocked: bool = False
    failure: Optional[str] = None   # fetch_error | not_painted | parse_error | proxy_pool_exhausted | remote_api_error | not_attempted | over_limit
    not_found: bool = False
    not_a_page: bool = False
    posts_found: int = 0
    exact_likes: Optional[str] = None  # ok | no_ads | failed (with --exact-likes)
    rejected: int = 0


# --------------------------------------------------------------------------- #
# The engine boundary (CLAUDE.md §26)
# --------------------------------------------------------------------------- #
class PageSession(_Protocol):
    async def goto(self, url: str) -> Optional[int]: ...  # HTTP status or None; raises on failure
    async def content(self) -> str: ...
    async def current_url(self) -> str: ...
    async def wait(self, seconds: float) -> None: ...
    async def take_responses(self) -> List[str]: ...  # post-carrying GraphQL bodies since the last call
    async def close(self) -> None: ...


class Engine(_Protocol):
    name: str
    readiness_s: float

    async def open(self, proxy) -> PageSession: ...
    async def sleep(self, seconds: float) -> None: ...
    async def solve_captcha(self, session: PageSession, *, html: str, url: str) -> Optional[dict]: ...


class SolveBudget:
    """One cap on PAID captcha solves for the whole run (CLAUDE.md §23: a
    per-page limit nothing sums is a bill). `limit=0` means never pay."""

    def __init__(self, limit: int):
        self.limit = max(0, int(limit))
        self.spent = 0

    def remaining(self) -> int:
        return max(0, self.limit - self.spent)

    def spend(self) -> None:
        self.spent += 1


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def resolve_urls(args) -> tuple:
    """(Page URLs, skipped). `--url` wins over `--urls-file`; each line is a
    Page URL, a username or a numeric id. Anything else is logged and
    skipped, never fetched; duplicates are fetched once."""
    if args.url:
        candidates = [args.url]
    elif args.urls_file:
        try:
            lines = _Path(args.urls_file).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"could not read --urls-file {args.urls_file!r}: {exc}")
        candidates = [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]
    else:
        return [], 0
    urls, skipped = [], 0
    for text in candidates:
        url = pp.normalize_input(text)
        if url is None:
            _log.warning("Skipping %s — %s.", text, pp.refusal_reason(text))
            skipped += 1
            continue
        if url not in urls:
            urls.append(url)
    return urls, skipped


def _dump_path(out_path: str, index: int) -> str:
    return f"{_Path(out_path).with_suffix('')}_debug_{index}.html"


async def _solve_within_budget(engine: Engine, session: PageSession, args, *, html: str, url: str) -> Optional[dict]:
    budget = getattr(args, "_solve_budget", None)
    if budget is not None and budget.remaining() == 0:
        _log.warning("Captcha solving skipped: the run's solve budget is spent (--max-solves %d).", budget.limit)
        return None
    result = await engine.solve_captcha(session, html=html, url=url)
    if budget is not None and result and result.get("action") in ("solved", "warning_solver_error"):
        budget.spend()  # a task was created and billed, whatever came back
    return result


# --------------------------------------------------------------------------- #
# One Page
# --------------------------------------------------------------------------- #
async def _wait_for_paint(session: PageSession) -> tuple:
    """(state, html) once the page is no longer `loading` (bounded), with a
    shorter grace for a page that is not recognisable yet at all."""
    html = await session.content()
    state = pp.page_state(html, final_url=await session.current_url())
    waited = 0.0
    while (state == "loading" and waited < PAINT_WAIT_S) or (state == "unknown" and waited < UNKNOWN_WAIT_S):
        await session.wait(POLL_S)
        waited += POLL_S
        try:
            html = await session.content()
        except Exception:  # noqa: BLE001 — mid-navigation
            continue
        state = pp.page_state(html, final_url=await session.current_url())
    if waited and state == "content":
        _log.info("Page painted after %.0fs.", waited)
    return state, html


async def _feed_bodies(session: PageSession, wanted: int, embedded: int) -> List[str]:
    """The feed response(s), waiting (bounded) until enough posts are in or
    FEED_WAIT_S passes."""
    bodies: List[str] = []
    waited = 0.0
    while True:
        try:
            bodies.extend(await session.take_responses())
        except Exception as exc:  # noqa: BLE001 — the posts are extra; the Page already read must not be lost
            _log.warning("The timeline response could not be read (%s) — writing the Page with the posts it embeds.",
                         _redact(str(exc)))
            return bodies
        have = embedded + sum(1 for b in bodies for _ in pp.stories(pp.graphql_payloads(b)))
        if have >= wanted or waited >= FEED_WAIT_S:
            return bodies
        await session.wait(POLL_S)
        waited += POLL_S


def _rows(html: str, bodies: List[str], posts: int, outcome: PageOutcome) -> None:
    page = pp.page_row(html)
    outcome.rows.append(page)
    if posts <= 0:
        return
    payloads = pp.json_blocks(html)
    for body in bodies:
        payloads.extend(pp.graphql_payloads(body))
    found = pp.stories(payloads)
    outcome.posts_found = len(found)
    for story in found[:posts]:
        try:
            row = pp.post_row(story, page=page, position=len(outcome.rows))
        except Exception as exc:  # noqa: BLE001 — one malformed post must not cost the Page
            _log.warning("A post on %s could not be read (%s) — skipped and counted as rejected.", outcome.url, exc)
            row = None
        if row is None:
            outcome.rejected += 1
        else:
            outcome.rows.append(row)


async def _exact_likes(engine: Engine, args, outcome: PageOutcome, proxy_pool) -> None:
    """Fill the Page row's likes_exact from the Ad Library (one more page
    load). Never fails the Page: a miss is recorded, the row is kept."""
    page = outcome.rows[0]
    proxy = proxy_pool.next() if proxy_pool is not None else None
    if proxy_pool is not None and proxy is None:
        outcome.exact_likes = "failed"
        return
    try:
        session = await engine.open(proxy)
    except Exception as exc:  # noqa: BLE001
        _log.warning("%s: could not open the Ad Library for its exact like count: %s", outcome.url, _redact(str(exc)))
        outcome.exact_likes = "failed"
        return
    try:
        await engine.sleep(args.delay_between_pages)
        await session.goto(pp.fetch_url(pp.ad_library_url(page.page_id)))
        state, likes, waited = "loading", None, 0.0
        while True:
            state, likes = pp.likes_from_ad_library(await session.content(), page.page_id)
            if state in ("content", "empty") or waited >= AD_LIBRARY_WAIT_S:
                break
            await session.wait(POLL_S)
            waited += POLL_S
        if state == "content":
            page.likes_exact, outcome.exact_likes = likes, "ok"
        elif state == "empty":
            outcome.exact_likes = "no_ads"
            _log.info("%s: no exact like count — the Page has run no ads, so the Ad Library has none.", outcome.url)
        else:
            outcome.exact_likes = "failed"
            _log.warning("%s: the Ad Library answered without results (%s) — likes_exact left empty.", outcome.url, state)
    except Exception as exc:  # noqa: BLE001
        outcome.exact_likes = "failed"
        _log.warning("%s: the Ad Library lookup failed (%s) — likes_exact left empty.", outcome.url, _redact(str(exc)))
    finally:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Session cleanup failed: %s", _redact(str(exc)))


async def fetch_page(engine: Engine, args, url: str, index: int, proxy_pool) -> PageOutcome:
    """One Page address: open it, wait for it to paint, read the header,
    the intro card and (with --posts) the posts. Every failure is a typed
    outcome, never a raise."""
    outcome = PageOutcome(url=url)
    proxy = proxy_pool.next() if proxy_pool is not None else None
    if proxy_pool is not None and proxy is None:
        outcome.failure = "proxy_pool_exhausted"
        return outcome
    _log.info("Opening %s (proxy: %s)", url, proxy.masked() if proxy else "(none — direct, or the --cdp-endpoint session's own exit)")
    try:
        session = await engine.open(proxy)
    except Exception as exc:
        _log.error("Browser connection failed — treating this Page as failed, not a crash: %s", _redact(str(exc)))
        outcome.failure = "fetch_error"
        return outcome
    try:
        last_error, status = None, None
        for attempt in range(args.retries + 1):
            try:
                status = await session.goto(pp.fetch_url(url))
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 — every remote call must be bounded and reported
                last_error = _redact(str(exc)).splitlines()[0] if str(exc) else type(exc).__name__
                if proxy_pool is not None and proxy is not None and _is_proxy_dead_error(last_error):
                    proxy_pool.report_failure(proxy, dead=True)
                    _log.warning("Proxy reported dead: %s", last_error)
                else:
                    _log.warning("Navigation attempt %d/%d for %s failed: %s", attempt + 1, args.retries + 1, url, last_error)
                if getattr(engine, "fatal", None):
                    break  # a refused key or an empty balance: every retry would fail the same way
                if attempt < args.retries:
                    await engine.sleep(args.retry_delay)
        if last_error is not None:
            _log.error("%s permanently failed to load: %s", url, last_error)
            outcome.failure = "remote_api_error" if getattr(engine, "last_remote_error", False) else "fetch_error"
            return outcome

        state, html = await _wait_for_paint(session)
        if state == "unknown":
            waited = 0.0
            while is_challenge(html) and waited < CHALLENGE_WAIT_S:
                await session.wait(1)
                waited += 1
                html = await session.content()
            if is_challenge(html) and args.solve_captcha != "off":
                result = await _solve_within_budget(engine, session, args, html=html, url=url)
                if result and result.get("action") == "solved":
                    await session.wait(engine.readiness_s)
            state, html = await _wait_for_paint(session)
        outcome.state = state

        if state == "content" and (pp.page_language(html) or "en").split("-")[0] != "en":
            _log.warning("%s: served in %r despite locale=en_US — the rounded counts and the intro card's labels "
                         "may not be read.", url, pp.page_language(html))
        if state == "content":
            embedded = len(pp.stories(pp.json_blocks(html)))
            bodies = await _feed_bodies(session, min(args.posts, pp.MAX_POSTS), embedded) if args.posts else []
            try:
                _rows(html, bodies, args.posts, outcome)
            except pp.NotAPage:
                outcome.not_a_page = True
                _log.warning("%s: this address leads to a personal profile, not a Page — skipped, nothing written "
                             "about it.", url)
            except Exception as exc:  # noqa: BLE001
                outcome.failure = "parse_error"
                _log.warning("%s: the Page could not be read (%s).", url, exc)
            if outcome.rows and args.posts and outcome.posts_found < min(args.posts, pp.MAX_POSTS):
                _log.info("%s: %d of the %d posts asked for were shown.", url, min(outcome.posts_found, args.posts),
                          min(args.posts, pp.MAX_POSTS))
        elif state == "not_found":
            outcome.not_found = True
            _log.warning("%s: \"This content isn't available right now\" — no such Page, or it is removed or "
                         "restricted. Not a block; nothing written for it.", url)
        elif state == "login":
            outcome.blocked = True
            _log.warning("%s: facebook.com sent this visitor to a login page — blocked, not missing. "
                         "Slow down (--delay-between-pages) or use another exit (--proxy-file).", url)
        elif state == "loading":
            outcome.failure = "not_painted"
            _log.warning("%s: the Page loaded but its data never arrived in %ds — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, PAINT_WAIT_S)
        elif status == 407:
            outcome.failure = "fetch_error"
            _log.warning("%s: the proxy refused this request — HTTP 407, proxy authentication required. "
                         "Selenium cannot send a proxy password: allow this machine's IP in the proxy's settings "
                         "(IP whitelist), or use playwright_scraper.py / puppeteer_scraper.py.", url)
        elif status in BLOCKING_STATUSES:
            outcome.blocked = True
            _log.warning("%s: HTTP %s and not a facebook.com page (%d bytes, title %r) — blocked, not missing.",
                         url, status, len(html or ""), pp.page_title(html))
        else:
            outcome.failure = "fetch_error"
            _log.warning("%s: HTTP %s, not a facebook.com page (%d bytes, title %r) — nothing read. "
                         "Re-run with --dump-html to inspect it.", url, status or "-", len(html or ""), pp.page_title(html))
        if args.dump_html:
            _Path(_dump_path(args.out, index)).write_text(html, encoding="utf-8")

        if proxy_pool is not None and proxy is not None:
            if outcome.blocked:
                proxy_pool.report_failure(proxy, dead=True)  # a block is a property of the EXIT (CLAUDE.md §8)
            else:
                proxy_pool.report_success(proxy)
        if getattr(engine, "last_remote_error", False) and not outcome.rows:
            outcome.failure = "remote_api_error"
        if outcome.rows:
            _log.info("%s: %s, %d post(s)", url, outcome.rows[0].title, len(outcome.rows) - 1)
        return outcome
    except Exception as exc:  # noqa: BLE001
        _log.warning("Page failed for %s: %s", url, _redact(str(exc)))
        outcome.failure = outcome.failure or "fetch_error"
        return outcome
    finally:
        try:
            await session.close()
        except Exception as exc:  # noqa: BLE001
            _log.warning("Session cleanup failed: %s", _redact(str(exc)))


# --------------------------------------------------------------------------- #
# The ONE run loop
# --------------------------------------------------------------------------- #
async def run(engine: Engine, args, *, urls: List[str], proxy_pool, client, started_at: float) -> int:
    """Every Page in input order, one browser session each, up to
    --max-results Pages."""
    outcomes: List[PageOutcome] = []
    consecutive_blocks = 0
    pages_read: set = set()  # distinct Pages: the same Page by another address does not use up the limit
    for i, url in enumerate(urls, 1):
        if len(pages_read) >= args.max_results:
            outcomes.extend(PageOutcome(url=u, failure="over_limit") for u in urls[i - 1:])
            _log.info("--max-results %d reached — %d Page address(es) not opened.", args.max_results, len(urls) - i + 1)
            break
        outcome = await fetch_page(engine, args, url, i, proxy_pool)
        if getattr(args, "exact_likes", False) and outcome.rows and outcome.rows[0].category == "page":
            await _exact_likes(engine, args, outcome, proxy_pool)
        outcomes.append(outcome)
        pages_read.update(r.sku for r in outcome.rows if r.category == "page")
        consecutive_blocks = consecutive_blocks + 1 if outcome.blocked else 0
        stop = (outcome.failure == "proxy_pool_exhausted" or getattr(engine, "fatal", None)
                or (proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS))
        if stop and i < len(urls):
            if proxy_pool is None and consecutive_blocks >= STOP_AFTER_CONSECUTIVE_BLOCKS:
                _log.error("%d blocked answers in a row from the same exit — stopping instead of asking again. "
                           "Rerun later, or with --proxy-file to rotate exits.", consecutive_blocks)
            outcomes.extend(PageOutcome(url=u, failure="not_attempted") for u in urls[i:])
            break
        if i < len(urls):
            await engine.sleep(args.delay_between_pages)
    return finish_pages(args, outcomes, engine_name=engine.name, urls=urls, started_at=started_at)


def finish_pages(args, outcomes: List[PageOutcome], *, engine_name: str, urls: List[str], started_at: float) -> int:
    products: List[Product] = []
    seen = set()
    failed_pages, failures = [], []
    capped = False
    for index, o in enumerate(outcomes, 1):
        for row in o.rows:
            if row.sku not in seen:  # one Page reached by two addresses is one row, the first one's
                seen.add(row.sku)
                products.append(row)
        if o.failure == "over_limit":
            capped = True  # left out because --max-results was reached, not because the run broke
            continue
        if o.failure or o.blocked:
            failed_pages.append(index)
            failures.append({"url": o.url, "reason": o.failure or "blocked"})
    pages = [{"url": o.url, "state": o.state, "rows": len(o.rows), "posts_shown": o.posts_found, "exact_likes": o.exact_likes,
              "result": ("not_found" if o.not_found else "not_a_page" if o.not_a_page else o.failure
                         or ("blocked" if o.blocked else "ok"))} for o in outcomes]
    return finish(args, products=products, blocked=any(o.blocked for o in outcomes),
                  remote_api_error=any(o.failure == "remote_api_error" for o in outcomes), engine_name=engine_name,
                  urls=urls, started_at=started_at,
                  pages_completed=sum(1 for o in outcomes if not (o.failure or o.blocked)),
                  pages_requested=sum(1 for o in outcomes if o.failure != "over_limit"),
                  failed_pages=failed_pages, failures=failures, capped=capped, pages=pages,
                  not_found=[o.url for o in outcomes if o.not_found],
                  not_a_page=[o.url for o in outcomes if o.not_a_page],
                  rejected_rows=sum(o.rejected for o in outcomes))


_INCOMPLETE_REASONS = ("not_painted", "parse_error", "proxy_pool_exhausted")


def finish(args, *, products: List[Product], blocked: bool, remote_api_error: bool, engine_name: str,
           urls: List[str], started_at: float, pages_completed: int, failed_pages: List[int],
           pages_requested: Optional[int] = None, failures=None, capped: bool = False, pages=None, not_found=None, not_a_page=None,
           rejected_rows: int = 0) -> int:
    budget = getattr(args, "_solve_budget", None)
    selection = {"mode": "pages", "urls": urls, "posts": args.posts, "exact_likes": bool(getattr(args, "exact_likes", False)),
                 "max_results": args.max_results}
    extra = {"solves_spent": budget.spent if budget is not None else 0, "selection": selection,
             "failed_urls": failures or [], "pages": pages or [], "not_found_urls": not_found or [],
             "not_a_page_urls": not_a_page or []}
    return _finish_run(
        products=products, out_path=args.out, fmt=args.format, engine=engine_name, url=urls[0] if urls else "",
        pages_requested=len(urls) if pages_requested is None else pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages or None, blocked=blocked, remote_api_error=remote_api_error,
        allow_empty=args.allow_empty, started_at=started_at, price_confirmed_pct=None,
        max_results=args.max_results, rejected_rows=rejected_rows,
        incomplete_reason=next((f["reason"] for f in (failures or []) if f["reason"] in _INCOMPLETE_REASONS), None),
        capped=capped, extra_meta=extra,
    )


def validate_common(args, *, urls, skipped, print_err) -> Optional[int]:
    """The usage checks every engine makes before launching anything;
    returns an exit code to stop with, or None to go on."""
    from output_writer import EXIT_BAD_USAGE
    if not urls:
        if skipped:
            print_err(f"Error: none of the inputs is a Facebook Page address ({skipped} skipped) — nothing left to fetch")
        else:
            print_err("Error: provide --url or --urls-file (a Page URL, a username or a numeric id)")
        return EXIT_BAD_USAGE
    if args.format not in ("json", "csv"):
        print_err(f"Error: unsupported --format {args.format!r}")
        return EXIT_BAD_USAGE
    # Checked before any browser starts: the file is written only at the end,
    # so a missing directory would otherwise crash after the whole run.
    out_dir = _Path(args.out).parent if getattr(args, "out", None) else _Path(".")
    if not out_dir.is_dir():
        print_err(f"Error: the --out directory {str(out_dir)!r} does not exist")
        return EXIT_BAD_USAGE
    return None


def _posts_count(value: str) -> int:
    import argparse
    n = int(value)
    if not 0 <= n <= pp.MAX_POSTS:
        raise argparse.ArgumentTypeError(f"must be 0-{pp.MAX_POSTS}: a logged-out visitor is shown {pp.MAX_POSTS} posts (got {value!r})")
    return n


def add_page_arguments(p) -> None:
    """The input flags, identical on every engine."""
    p.add_argument("--url", default=None, help="One Page: its URL, username or numeric id (or set FACEBOOK_URL) — overrides --urls-file")
    p.add_argument("--urls-file", default=None, help="A file with one Page URL, username or numeric id per line (# comments allowed)")
    p.add_argument("--exact-likes", action="store_true", help="Also read each Page's EXACT like count from Meta's Ad Library (one more page load per Page; only Pages that have run ads have one)")
    p.add_argument("--posts", type=_posts_count, default=0, help=f"Also write each Page's latest posts, 0-{pp.MAX_POSTS} (all a logged-out visitor is shown)")
