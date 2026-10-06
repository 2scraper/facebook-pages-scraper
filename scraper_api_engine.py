#!/usr/bin/env python3
"""scraper_api_engine.py — `--scraper-api`: fetch through 2Captcha's
Scraper API instead of driving a browser.

A Page's header, intro card and first post are embedded in the page
itself (see page_parser.py), so one Scraper API call per Page returns
them with no browser on this machine. The second and third post arrive
only in a request the live page makes, so `--posts` gives at most one
post per Page in this mode. It plugs into the same `page_flow` loop as
the browser engines — `goto` is the Scraper API call, `content` returns
the HTML it brought back — so parsing, exit codes and the sidecar are
unchanged.

It needs `TWOCAPTCHA_KEY` (the Scraper API's own auth). When
`FACEBOOK_CDP_ENDPOINT` is set, each call is routed through that Scraping
Browser profile (`cdpurl`) and so leaves from its exit; otherwise it uses
the Scraper API's own pool.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import page_flow
from output_writer import EXIT_BAD_USAGE
from proxy_pool import redact_credentials
from scraper_api_client import TwoCaptchaAuthError, TwoCaptchaClient, TwoCaptchaError

log = logging.getLogger("scraper_api_engine")

ENGINE_NAME = "scraper_api"
SCRAPE_TIMEOUT_S = 60  # the Scraper API's own wait for the target (its limit is 1-120s)


class _ScraperApiSession:
    """page_flow.PageSession with no page: `goto` is one Scraper API call
    and `content` is the HTML it returned."""

    def __init__(self, engine: "ScraperApiEngine"):
        self.engine = engine
        self.html = ""
        self.url = ""

    async def goto(self, url: str) -> Optional[int]:
        self.url = url
        status, self.html = await self.engine.fetch_html(url)
        return status

    async def content(self) -> str:
        return self.html

    async def current_url(self) -> str:
        return self.url

    async def wait(self, seconds: float) -> None:
        return None  # the HTML is final: waiting cannot paint anything more



    async def take_responses(self) -> list:
        return []

    async def close(self) -> None:
        return None


class ScraperApiEngine:
    """page_flow.Engine over the Scraper API. `last_remote_error` says
    whether the most recent call failed on the Scraper API's side (not the
    site's), so a run that read nothing for that reason ends as
    remote_api_error (exit 5), not empty."""

    name = ENGINE_NAME
    readiness_s = 0.0

    def __init__(self, client: TwoCaptchaClient, cdp_url: Optional[str]):
        self.client, self.cdp_url = client, cdp_url
        self.last_remote_error = False
        self.fatal: Optional[str] = None  # a bad key or no balance: every later call would fail the same way

    async def open(self, proxy) -> _ScraperApiSession:
        return _ScraperApiSession(self)

    async def sleep(self, seconds: float) -> None:
        if self.fatal is None:  # no point waiting out a refused key
            await asyncio.sleep(seconds)

    async def solve_captcha(self, session, *, html: str, url: str):
        return None  # nothing local to deliver a token to

    async def fetch_html(self, url: str) -> tuple:
        """(target status, HTML). A Scraper API failure raises, like a
        browser's failed navigation, so page_flow retries and reports it."""
        if self.fatal is not None:
            self.last_remote_error = True
            raise TwoCaptchaError(self.fatal)
        try:
            result = await asyncio.to_thread(
                self.client.scrape_url, url, data_format="raw", timeout=SCRAPE_TIMEOUT_S, cdp_url=self.cdp_url,
            )
        except TwoCaptchaError as exc:
            message = redact_credentials(str(exc))
            if isinstance(exc, TwoCaptchaAuthError) or "insufficient" in message:
                self.fatal = message
                log.error("%s — stopping Scraper API calls for this run.", message)
            self.last_remote_error = True
            raise TwoCaptchaError(message) from None
        if not result.body and not result.target_status:
            # Measured 2026-10-06: the Scraper API answered once with neither a
            # page nor a target status. That is the service failing, not the
            # site answering: raised like any other API error, so page_flow
            # retries it and a run that reads nothing ends as remote_api_error.
            self.last_remote_error = True
            raise TwoCaptchaError("Scraper API returned an empty answer (no page, no target status)")
        self.last_remote_error = False
        return int(result.target_status or 0) or None, result.body or ""


async def run(args, *, urls, started_at: float) -> int:
    """The whole run in `--scraper-api` mode; the caller has already
    validated the selection and set `args.out`."""
    if not args.twocaptcha_key:
        log.error("--scraper-api needs TWOCAPTCHA_KEY (the Scraper API's own auth) in .env.")
        return EXIT_BAD_USAGE
    if args.proxy or args.proxy_file:
        log.warning("Ignoring --proxy: --scraper-api fetches from 2Captcha's side.")
    if args.fingerprint:
        log.warning("Ignoring --fingerprint: --scraper-api fetches from 2Captcha's side.")
    client = TwoCaptchaClient(args.twocaptcha_key, api_base=args.captcha_api)
    engine = ScraperApiEngine(client, args.cdp_endpoint or None)
    return await page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=started_at)
