#!/usr/bin/env python3
"""puppeteer_scraper.py — pyppeteer engine, parity copy of
playwright_scraper.py (same flags, same exit codes and status semantics —
see output_writer.finish_run). Not the primary engine (Playwright is); kept
for parity, and because it — like Playwright, unlike Selenium — CAN open an
authenticated `ws://login:pass@host:port` CDP session, so it is the second
engine able to use the Scraping Browser API's own `Captcha.setAutoSolve`.

Named and shaped like every sibling repo's own `puppeteer_scraper.py`
(Python + pyppeteer, not a separate Node.js file) — this repo follows that
family convention for the same reason: one shared output contract and one
set of family modules across all three engines.

pyppeteer itself is effectively unmaintained (its own README points at
Playwright) — this file exists for parity/completeness, not as a
recommendation to prefer it.

Chromium binary: sourced from `PYPPETEER_EXECUTABLE_PATH` /
`PUPPETEER_EXECUTABLE_PATH` if set (handy for reusing an existing
Playwright/system Chromium instead of pyppeteer's own bundled download),
otherwise pyppeteer's own default.

Same input and the same shared loop as playwright_scraper.py
(`page_flow.run`); this file only provides the pyppeteer page session.
Over `--cdp-endpoint` one connection serves the
whole run and is DISCONNECTED at the end, never closed — `close()` on a
connected pyppeteer browser ends the remote Browser API session itself.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from urllib.parse import urlparse
from typing import Optional

try:
    from pyppeteer import connect as pyppeteer_connect
    from pyppeteer import launch as pyppeteer_launch
    from pyppeteer.errors import NetworkError, PageError, TimeoutError as PyppeteerTimeoutError
except ImportError as _IMPORT_ERROR:  # pragma: no cover — exercised by smoke_test's no-engine path
    pyppeteer_launch = None
    pyppeteer_connect = None
    NetworkError = PageError = PyppeteerTimeoutError = Exception
    _PYPPETEER_IMPORT_ERROR = _IMPORT_ERROR
else:
    _PYPPETEER_IMPORT_ERROR = None

import env_config
import page_flow
import scraper_api_engine
import scraper_api_client
import page_parser as pp
from output_writer import EXIT_BAD_USAGE, EXIT_CRASH
from fingerprint_client import fetch_fingerprint, refuse_if_cdp, user_agent_from
from proxy_pool import Proxy, ProxyPool, ProxyParseError, load_proxies, redact_credentials
from scraper_api_client import TwoCaptchaClient

ENGINE_NAME = "puppeteer"
NAV_TIMEOUT_MS = 45_000
READINESS_WAIT_S = 1.5  # see playwright_scraper.READINESS_WAIT_MS
CDP_CONNECT_TIMEOUT_S = 60
GRAPHQL_PATH = "/api/graphql"

log = logging.getLogger("puppeteer_scraper")

_CHROMIUM_EXECUTABLE = os.environ.get("PYPPETEER_EXECUTABLE_PATH") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")


def _positive_int(value: str) -> int:
    ivalue = int(value)
    if ivalue < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer (got {value!r})")
    return ivalue


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Facebook Pages scraper (public Pages, logged out) — pyppeteer (Puppeteer) engine",
        epilog="Credentials belong in .env / FACEBOOK_PROXY / TWOCAPTCHA_KEY — never on this command line.",
    )
    page_flow.add_page_arguments(p)
    p.add_argument("--max-results", type=_positive_int, default=100, help="Pages to read in total (posts not counted); the rest of the list is left out and the run marked capped")
    p.add_argument("--delay-between-pages", type=float, default=2.0)
    p.add_argument("--format", choices=["json", "csv"], default="json")
    p.add_argument("--out", default=None)
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--retry-delay", type=float, default=3.0)
    p.add_argument("--proxy", default=None)
    p.add_argument("--proxy-file", default=None)
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=3)
    p.add_argument("--twocaptcha-key", default=None)
    p.add_argument("--captcha-api", default=None, help="Override the 2Captcha API base URL (testing only)")
    p.add_argument("--solve-captcha", choices=["off", "when-blocked", "always"], default="when-blocked")
    p.add_argument("--max-solves", type=int, default=8, help="Cap on paid 2Captcha solves for the whole run, recorded as solves_spent — kept for the local solver, which is disabled: nothing is solved or billed locally today (the Scraping Browser API auto-solves on its own)")
    p.add_argument("--min-score", type=float, default=0.3, help="Minimum acceptable reCAPTCHA v3 score (2Captcha's minScore task field)")
    p.add_argument("--fingerprint", action="store_true", help="Fetch and apply a 2Captcha Fingerprint API profile's user agent (ignored with --cdp-endpoint — see fingerprint_client.refuse_if_cdp)")
    p.add_argument("--fp-tags", default=None, help="Fingerprint API OS filter — one of Windows, Linux, Android (no browser names, no lists)")
    p.add_argument("--fp-country", default=None, help="Fingerprint API filter, e.g. 'us'")
    p.add_argument("--cdp-endpoint", default=None)
    p.add_argument("--scraper-api", action="store_true", help="Fetch through 2Captcha's Scraper API instead of driving this browser (needs TWOCAPTCHA_KEY; routed through FACEBOOK_CDP_ENDPOINT's Scraping Browser profile when one is set; the Page and the one post it embeds — the other two arrive only in a live page)")
    p.add_argument("--allow-empty", action="store_true")
    p.add_argument("--dump-html", action="store_true")
    p.add_argument("--headless", dest="headless", action="store_true", default=True)
    p.add_argument("--headful", dest="headless", action="store_false")
    return p


def _default_out(fmt: str) -> str:
    return f"facebook_pages.{fmt}"


_resolve_urls = page_flow.resolve_urls  # the one implementation is page_flow's


async def _launch(*, headless: bool, proxy: Optional[Proxy], cdp_endpoint: Optional[str]):
    if cdp_endpoint:
        # pyppeteer's connect() has no timeout of its own and never resolves a
        # refused handshake; connect_with_retry bounds each try, retries a
        # locked profile and names an expired one (CLAUDE.md §26).
        return await scraper_api_client.connect_with_retry(
            lambda: pyppeteer_connect(browserWSEndpoint=cdp_endpoint, defaultViewport=None),
            redact=redact_credentials, log=log,
        )
    args = ["--no-sandbox", "--disable-dev-shm-usage", "--lang=en-US"]  # see selenium_scraper: titles follow the browser language
    if proxy is not None:
        args.append(proxy.pyppeteer_launch_arg())
    kwargs = dict(headless=headless, args=args)
    if _CHROMIUM_EXECUTABLE:
        kwargs["executablePath"] = _CHROMIUM_EXECUTABLE
    return await pyppeteer_launch(**kwargs)


async def _authenticate_if_needed(page, proxy: Optional[Proxy]) -> None:
    """Answer the proxy's auth challenge over the CDP `Fetch` domain.

    pyppeteer's own `page.authenticate()` turns on
    `Network.setRequestInterception`, which current Chromium no longer has:
    measured 2026-10-04 with Chrome for Testing, every proxied URL failed
    with "'Network.setRequestInterception' wasn't found" before a byte was
    fetched. `Fetch` is that method's replacement and exists in every
    Chromium since 74. Credentials travel in the CDP message, never argv."""
    auth = proxy.pyppeteer_auth_dict() if proxy is not None else None
    if not auth:
        return
    client = page._client

    async def _continue(event):
        try:
            await client.send("Fetch.continueRequest", {"requestId": event["requestId"]})
        except Exception as exc:  # noqa: BLE001 — a request cancelled meanwhile is not an error
            log.debug("Fetch.continueRequest failed: %s", redact_credentials(str(exc)))

    answered = set()

    async def _answer(event):
        challenge = event.get("authChallenge") or {}
        try:
            origin = urlparse(challenge.get("origin", ""))
            trusted = (challenge.get("source") == "Proxy"
                       and origin.hostname == proxy.host.lower()
                       and (origin.port or (443 if origin.scheme == "https" else 80)) == proxy.port)
        except ValueError:
            trusted = False
        request_id = event["requestId"]
        response = {"response": "CancelAuth"}
        if trusted and request_id not in answered:
            answered.add(request_id)
            response = {"response": "ProvideCredentials", **auth}
        try:
            await client.send("Fetch.continueWithAuth", {
                "requestId": event["requestId"],
                "authChallengeResponse": response,
            })
        except Exception as exc:  # noqa: BLE001
            log.debug("Fetch.continueWithAuth failed: %s", redact_credentials(str(exc)))

    client.on("Fetch.requestPaused", lambda event: asyncio.ensure_future(_continue(event)))
    client.on("Fetch.authRequired", lambda event: asyncio.ensure_future(_answer(event)))
    await client.send("Fetch.enable", {"handleAuthRequests": True, "patterns": [{"urlPattern": "*"}]})


async def _enable_scraping_browser_auto_solve(page) -> None:
    try:
        client = await page.target.createCDPSession()
        client.on("Captcha.detected", lambda *_: log.info("[Scraping Browser API] captcha detected"))
        client.on("Captcha.solveFinished", lambda *_: log.info("[Scraping Browser API] captcha solved"))
        client.on("Captcha.solveFailed", lambda *_: log.warning("[Scraping Browser API] captcha solve failed"))
        await client.send("Captcha.setAutoSolve", {"autoSolve": True, "options": [{"type": "*"}]})
    except Exception as exc:  # noqa: BLE001 — optional enhancement, never fatal
        log.warning("Captcha.setAutoSolve unavailable on this CDP session (continuing without it): %s", exc)


async def _maybe_solve_captcha(*, html: str, url: str, client: Optional[TwoCaptchaClient], policy: str, min_score: float = 0.3) -> Optional[dict]:
    if policy == "off" or client is None:
        return None
    log.warning("Local captcha solving is disabled: token delivery is not implemented; no paid task created. "
                "Use the Scraping Browser CDP auto-solve integration.")
    return {"action": "unsupported_delivery"}


# pyppeteer's own close can wait forever: measured 2026-10-06, after a
# proxy closed the connection mid-navigation the run hung in browser
# cleanup for 20+ minutes after the page had already been reported failed.
# Every cleanup step is bounded, and a local Chromium that will not close
# is killed.
CLOSE_TIMEOUT_S = 10


async def _bounded(coro, what: str) -> bool:
    try:
        await asyncio.wait_for(coro, timeout=CLOSE_TIMEOUT_S)
        return True
    except asyncio.TimeoutError:
        log.warning("%s did not finish in %ds — moving on.", what, CLOSE_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — cleanup only
        log.debug("%s failed: %s", what, exc)
    return False


async def _release(browser, *, remote: bool) -> None:
    if remote:
        await _bounded(browser.disconnect(), "Disconnecting from the remote browser")
        return
    if not await _bounded(browser.close(), "Closing the browser"):
        process = getattr(browser, "process", None)
        if process is not None:
            try:
                process.kill()
            except Exception as exc:  # noqa: BLE001 — cleanup only
                log.debug("killing the browser process failed: %s", exc)


async def _open_page(browser, *, proxy: Optional[Proxy], user_agent: Optional[str], autosolve: bool):
    page = await browser.newPage()
    if user_agent:
        await page.setUserAgent(user_agent)
    await _authenticate_if_needed(page, proxy)
    if autosolve:
        await _enable_scraping_browser_auto_solve(page)
    return page


class _PyppeteerSession:
    """page_flow.PageSession over one pyppeteer page; a local session owns
    its browser (one per Page, on that Page's proxy). GraphQL responses
    that carry timeline posts are kept as they arrive, for
    `take_responses()`."""

    def __init__(self, page, browser, *, owns_browser: bool):
        self.page, self.browser, self.owns_browser = page, browser, owns_browser
        self._bodies: list = []
        self._pending: set = set()
        page.on("response", self._on_response)

    def _on_response(self, response) -> None:
        if GRAPHQL_PATH not in response.url:
            return
        task = asyncio.ensure_future(self._read(response))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _read(self, response) -> None:
        try:
            body = await response.text()
        except Exception as exc:  # noqa: BLE001 — a body gone with a navigation is not an error
            log.debug("Could not read a GraphQL response body: %s", exc)
            return
        if pp.is_feed_response(body):
            self._bodies.append(body)

    async def goto(self, url: str) -> Optional[int]:
        response = await self.page.goto(url, {"waitUntil": "domcontentloaded", "timeout": NAV_TIMEOUT_MS})
        await asyncio.sleep(READINESS_WAIT_S)
        return response.status if response is not None else None

    async def content(self) -> str:
        return await self.page.content()

    async def current_url(self) -> str:
        return self.page.url

    async def wait(self, seconds: float) -> None:
        await asyncio.sleep(seconds)



    async def take_responses(self) -> list:
        if self._pending:
            await asyncio.wait(list(self._pending), timeout=10)
        bodies, self._bodies = self._bodies, []
        return bodies

    async def close(self) -> None:
        await _bounded(self.page.close(), "Closing the page")
        if self.owns_browser:
            await _release(self.browser, remote=False)


class _PyppeteerEngine:
    """page_flow.Engine for pyppeteer: over CDP one connection for the
    run (disconnected at the end, never closed)."""

    name = ENGINE_NAME
    readiness_s = READINESS_WAIT_S

    def __init__(self, args: argparse.Namespace, *, remote_browser, autosolve: bool, user_agent: Optional[str], client):
        self.args, self.remote_browser, self.autosolve, self.user_agent, self.client = args, remote_browser, autosolve, user_agent, client

    async def open(self, proxy) -> _PyppeteerSession:
        browser = self.remote_browser or await _launch(headless=self.args.headless, proxy=proxy, cdp_endpoint=None)
        page = await _open_page(browser, proxy=proxy, user_agent=self.user_agent, autosolve=self.autosolve)
        return _PyppeteerSession(page, browser, owns_browser=self.remote_browser is None)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return await _maybe_solve_captcha(html=html, url=url, client=self.client, policy=self.args.solve_captcha,
                                          min_score=self.args.min_score)


_CLOSED_TARGET_NOISE = ("Target closed", "No session with given id")


def _quiet_target_closed(loop, context) -> None:
    """pyppeteer leaves a detachFromTarget/sendMessageToTarget future
    failing with "Target closed" or "No session with given id" behind a
    page.close() — logged as an ERROR although nothing went wrong (seen on
    every live run). Everything else still reaches the default handler."""
    exc = context.get("exception")
    if isinstance(exc, NetworkError) and any(m in str(exc) for m in _CLOSED_TARGET_NOISE):
        return
    # A refused CDP handshake leaves pyppeteer's own connect task failing
    # after connect_with_retry moved on (CLAUDE.md §26): silence that shape.
    if exc is not None and type(exc).__name__ in ("InvalidStatusCode", "InvalidStatus", "InvalidHandshake", "AbortHandshake"):
        return
    loop.default_exception_handler(context)


async def run(args: argparse.Namespace) -> int:
    started_at = time.time()
    asyncio.get_running_loop().set_exception_handler(_quiet_target_closed)
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    try:
        urls, skipped = _resolve_urls(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    stop = page_flow.validate_common(args, urls=urls, skipped=skipped, print_err=lambda m: print(m, file=sys.stderr))
    if stop is not None:
        return stop
    if args.scraper_api:
        args.out = args.out or _default_out(args.format)
        return await scraper_api_engine.run(args, urls=urls, started_at=started_at)
    if pyppeteer_launch is None:
        print(f"Error: pyppeteer is not installed ({_PYPPETEER_IMPORT_ERROR}). "
              f"pip install -r requirements-puppeteer.txt", file=sys.stderr)
        return EXIT_CRASH
    args.out = args.out or _default_out(args.format)

    try:
        proxies = load_proxies(args.proxy, args.proxy_file)
    except ProxyParseError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_BAD_USAGE
    proxy_pool = ProxyPool(proxies, shuffle=args.proxy_shuffle, block_retries=args.proxy_block_retries) if proxies else None
    if args.cdp_endpoint and proxy_pool is not None:
        log.warning("Ignoring --proxy: a --cdp-endpoint session already carries its own exit IP.")
        proxy_pool = None

    client = None
    if args.twocaptcha_key and (args.solve_captcha != "off" or args.fingerprint):
        client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    user_agent = None
    if args.fingerprint and not refuse_if_cdp(args.cdp_endpoint):
        if client is None:
            log.warning("--fingerprint requested but no --twocaptcha-key/TWOCAPTCHA_KEY set — continuing without one.")
        else:
            profile = fetch_fingerprint(client, tags=args.fp_tags, country=args.fp_country)
            if profile:
                user_agent = user_agent_from(profile)
                if user_agent:
                    log.info("Fingerprint applied: user agent %s", user_agent)

    remote_browser = None
    try:
        if args.cdp_endpoint:
            try:
                remote_browser = await _launch(headless=args.headless, proxy=None, cdp_endpoint=args.cdp_endpoint)
            except RuntimeError as exc:
                log.error("CDP connection failed — treating as remote_api_error, not a crash: %s", exc)
                return page_flow.finish(args, products=[], blocked=False, remote_api_error=True, engine_name=ENGINE_NAME,
                                        urls=urls, started_at=started_at,
                                        pages_completed=0, failed_pages=[])
        engine = _PyppeteerEngine(args, remote_browser=remote_browser,
                                  autosolve=bool(args.cdp_endpoint) and args.solve_captcha != "off",
                                  user_agent=user_agent, client=client)
        return await page_flow.run(engine, args, urls=urls,
                                   proxy_pool=proxy_pool, client=client, started_at=started_at)
    except Exception:
        log.exception("Unhandled error — this is a crash, not a normal blocked/empty run")
        return EXIT_CRASH
    finally:
        if remote_browser is not None:
            await _release(remote_browser, remote=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = build_arg_parser().parse_args()
    args = env_config.apply_env(args)
    try:
        # asyncio.run(), not get_event_loop(): the latter raises in any
        # process that already ran and closed a loop (shein-scraper hit it).
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return EXIT_CRASH


if __name__ == "__main__":
    sys.exit(main())
