#!/usr/bin/env python3
"""page_parser.py — this IS the facebook.com Page site knowledge.

Written 2026-10-06 from live captures of public Facebook Pages fetched
logged out (no account, no cookies) from a residential IP: Nike, CNN, The
New York Times, BBC News, Shake Shack, DER SPIEGEL, McDonald's, Starbucks
and others, the same Page opened by its numeric profile id and by its Page
id, three addresses that are no Page, and one that redirects to a private
person's profile. The trimmed captures are in `tests/fixtures/`.

What a logged-out visitor is actually served:

  - **The Page itself, under HTTP 200**, with a "Log in" dialog drawn over
    it — the data is in the page regardless. The route is
    `comet.fbweb.CometProfilePlusLoggedOutRoute`.
  - **The header** (`profile_header_renderer`): the Page's name, its URL,
    the profile id, the Page id (`delegate_page.id` — the id the Ad
    Library uses), profile picture, cover photo, the verified badge
    (`show_verified_badge_on_profile`) and the social line, which is
    ROUNDED ("39M followers", "24 following"). The exact count is not in
    the page.
  - **The intro card**: typed items (`timeline_context_list_item_type`):
    INTRO_CARD_INFLUENCER_CATEGORY ("Page · Sportswear Store"), _WEBSITE,
    _PROFILE_PHONE, _PROFILE_EMAIL, _ADDRESS, _BUSINESS_HOURS,
    _BUSINESS_PRICE ("Price Range · $"), _RATING ("32% recommend (37,633
    reviews)"), _CONFIRMED_OWNER_LABEL (the legal owner Meta confirmed,
    "NIKE, Inc.") and _OTHER_ACCOUNT (a linked Instagram account). Read
    by TYPE, never by position or by guessing what a string looks like.
  - **The description** (`best_description`, "Just Do It.") and the
    category name, on the Page object.
  - **Three posts at most.** One is embedded in the page; the next two
    arrive in the `ProfileCometTimelineFeedRefetchQuery` response the page
    makes on load. Scrolling brings no more to a logged-out visitor
    (measured: 3 post ids after 4 scrolls). Each carries its EXACT
    reaction, comment and share counts and, for a video, its view count.
  - **An address that is no Page** answers HTTP 200 with
    `comet.fbweb.CometErrorRoute` and "This content isn't available right
    now" — the same for a misspelt name and a removed Page.
  - **`/profile.php?id=<profile id>` and `/<Page id>`** redirect to the
    Page's own address; rows are keyed on the Page id, so all three
    spellings are one row.

Not collected, on purpose: private persons. A profile with no Page behind
it (no `delegate_page`) is reported and skipped, never written — the same
for anything about the people who comment on a Page's posts.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import re
from typing import Any, Iterator, List, Optional
from urllib.parse import parse_qsl, urlparse

from bs4 import BeautifulSoup

from output_writer import Product

log = logging.getLogger("page_parser")

BASE_URL = "https://www.facebook.com"
SOURCE = "facebook.com"
MAX_POSTS = 3  # what a logged-out visitor is shown (measured 2026-10-06)

ROUTE_PAGE = "comet.fbweb.CometProfilePlusLoggedOutRoute"
ROUTE_ERROR = "comet.fbweb.CometErrorRoute"
NOT_FOUND_TEXT = "This content isn't available right now"
FEED_QUERY = "ProfileCometTimelineFeedRefetchQuery"
LOGIN_PATHS = ("/login", "/checkpoint")
BOT_CHALLENGE_MARKERS: tuple = ()  # none seen on any capture

# First path segments that are facebook.com's own routes, never a Page.
_NOT_A_PAGE = frozenset("""
ads groups events marketplace watch gaming login logout recover checkpoint help policies privacy
settings messages notifications friends search hashtag stories reel reels photo photos videos
share sharer dialog plugins pages people places business l.php story.php permalink.php home.php
""".split())
_USERNAME_RE = re.compile(r"^[A-Za-z0-9.\-]{2,80}$")
_ID_RE = re.compile(r"^\d{5,25}$")
_HOSTS = ("facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "fb.com", "www.fb.com")


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #
def normalize_input(text: str) -> Optional[str]:
    """A Page URL, a bare username or a numeric id, as one canonical URL —
    or None for anything that is not a Page address."""
    raw = (text or "").strip()
    if not raw:
        return None
    if "://" not in raw and "/" not in raw and raw.lower().lstrip("@") not in _HOSTS:
        name = raw.lstrip("@")
        if _ID_RE.match(name):
            return f"{BASE_URL}/{name}"
        if _USERNAME_RE.match(name) and name.lower() not in _NOT_A_PAGE and not name.endswith(".php"):
            return f"{BASE_URL}/{name}"
        return None
    if "://" not in raw:
        raw = "https://" + raw
    parts = urlparse(raw)
    if (parts.hostname or "").lower() not in _HOSTS or parts.username or parts.port:
        return None
    segments = [s for s in parts.path.split("/") if s]
    if not segments:
        return None
    if segments[0] == "profile.php":
        pid = dict(parse_qsl(parts.query)).get("id", "")
        return f"{BASE_URL}/profile.php?id={pid}" if _ID_RE.match(pid) else None
    if segments[0] == "people" and len(segments) >= 3 and _ID_RE.match(segments[2]):
        return f"{BASE_URL}/{segments[2]}"
    if segments[0] == "pg" and len(segments) >= 2:
        segments = segments[1:]
    name = segments[0]
    if name.lower() in _NOT_A_PAGE or not _USERNAME_RE.match(name) or name.endswith(".php"):
        return None
    return f"{BASE_URL}/{name}"


def fetch_url(url: str) -> str:
    """The address actually opened: the canonical one plus `locale=en_US`.
    Facebook picks the page language from the browser, not the address —
    measured 2026-10-06, pyppeteer on a Russian-language machine got
    "474 тыс. — подписчики" and "Страница · Ресторан быстрого питания"
    despite `--lang=en-US`, and the Scraper API's browser the same, so the
    rounded counts and the intro card's English labels went unread. The
    parameter makes every engine get the English page; rows still carry
    the canonical address."""
    return url + ("&" if "?" in url else "?") + "locale=en_US"


_LANG_RE = re.compile(r"<html[^>]*\blang=\"([A-Za-z-]+)\"")


def page_language(html: str) -> Optional[str]:
    m = _LANG_RE.search(html or "")
    return m.group(1).lower() if m else None


def refusal_reason(text: str) -> str:
    raw = (text or "").strip()
    host = (urlparse(raw if "://" in raw else "https://" + raw).hostname or "").lower()
    if host and host not in _HOSTS:
        return "not a facebook.com address"
    return "not a Facebook Page address (a Page URL, a username or a numeric id)"


def username_from_url(url: str) -> Optional[str]:
    segments = [s for s in urlparse(url or "").path.split("/") if s]
    if not segments or segments[0] in ("profile.php",) or _ID_RE.match(segments[0]):
        return None
    return segments[0]


# --------------------------------------------------------------------------- #
# Reading a page and a GraphQL response
# --------------------------------------------------------------------------- #
def _find(obj: Any, key: str) -> Iterator[dict]:
    """Every dict (depth-first, document order) that has `key`."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed(list(node.values())))
        elif isinstance(node, list):
            stack.extend(reversed(node))


_WANTED = ("profile_header_renderer", "timeline_context_item", "delegate_page", "timeline_list_feed_units",
           "best_description", "post_id")


def json_blocks(html: str) -> List[Any]:
    out = []
    for script in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/json"):
        raw = script.string or script.get_text()
        if not any(f'"{k}"' in raw for k in _WANTED):
            continue  # ~100 blocks per page; a handful carry the Page
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


def graphql_payloads(body: str) -> List[Any]:
    """One JSON document per line (streamed parts), maybe behind `for (;;);`."""
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if line.startswith("for (;;);"):
            line = line[len("for (;;);"):]
        if not line.startswith("{"):
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def is_feed_response(body: str) -> bool:
    """A captured GraphQL body worth parsing: it carries timeline posts.
    Decided on the BODY, so all three engines agree."""
    return '"post_id"' in (body or "") and '"comet_sections"' in (body or "")


_ROUTE_RE = re.compile(r'"canonicalRouteName":"([^"]+)"')
_TITLE_RE = re.compile(r"<title[^>]*>([^<]{0,120})</title>", re.I)


def page_route(html: str) -> Optional[str]:
    m = _ROUTE_RE.search(html or "")
    return m.group(1) if m else None


def page_title(html: str) -> Optional[str]:
    m = _TITLE_RE.search(html or "")
    return m.group(1).strip() if m else None


def header(blocks: List[Any]) -> Optional[dict]:
    for block in blocks:
        for node in _find(block, "profile_header_renderer"):
            renderer = node.get("profile_header_renderer")
            if isinstance(renderer, dict) and isinstance(renderer.get("user"), dict):
                return renderer
    return None


# A login page announces itself in its canonical link even when the
# address it was fetched under does not change — measured 2026-10-06: the
# Scraper API's own pool was served the login page (canonical
# https://fi-fi.facebook.com/login) for a Marketplace search, and with no
# final URL to go by it was misread as an unknown page rather than a block.
_LOGIN_CANONICAL_RE = re.compile(r'<link[^>]+rel="canonical"[^>]+href="https?://[a-z-]*\.?facebook\.com/login', re.I)


def is_login_page(html: str) -> bool:
    return bool(_LOGIN_CANONICAL_RE.search(html or ""))


def page_state(html: str, *, final_url: Optional[str] = None) -> str:
    """`content` (a Page header in the data), `not_found` (the error route),
    `login` (a login or checkpoint page), `loading` (the Page route with
    no header yet) or `unknown` (not a facebook.com page at all)."""
    route = page_route(html)
    if route == ROUTE_ERROR or (route is None and NOT_FOUND_TEXT in (html or "")):
        return "not_found"
    path = urlparse(final_url or "").path
    if any(path.startswith(p) for p in LOGIN_PATHS) or (route or "").endswith("LoginRoute") or is_login_page(html):
        return "login"
    if '"profile_header_renderer"' in (html or "") and header(json_blocks(html)) is not None:
        return "content"
    if route == ROUTE_PAGE:
        return "loading"
    return "unknown"


# --------------------------------------------------------------------------- #
# Values
# --------------------------------------------------------------------------- #
_SCALE = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}
_COUNT_RE = re.compile(r"^([\d.,]+)\s*([KMB]?)\s+(followers?|following|likes?|people like this)\b", re.I)


def rounded_count(text: Optional[str]) -> Optional[int]:
    """'39M followers' → 39000000, '2.2M' → 2200000, '474K' → 474000,
    '1,234 followers' → 1234. The site rounds; so does this."""
    m = _COUNT_RE.match((text or "").strip())
    if not m:
        return None
    number = m.group(1).replace(",", "")
    try:
        return int(round(float(number) * _SCALE[m.group(2).upper()]))
    except ValueError:
        return None


_RATING_RE = re.compile(r"(\d+)%\s+recommend\s+\(([\d,]+)\s+reviews?\)", re.I)


def _text(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("text")
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _uri(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        image = value.get("image")
        value = value.get("uri") or (image.get("uri") if isinstance(image, dict) else None)
    return value if isinstance(value, str) and value.startswith("http") else None


def _json_list(values: List[Any]) -> Optional[str]:
    return json.dumps(values, ensure_ascii=False) if values else None


def _unwrap_link(url: Optional[str]) -> Optional[str]:
    """The intro card's website is behind Facebook's l.php redirect."""
    if not url:
        return None
    parts = urlparse(url)
    if parts.hostname and parts.hostname.endswith("facebook.com") and parts.path == "/l.php":
        return dict(parse_qsl(parts.query)).get("u") or url
    return url


def intro_items(blocks: List[Any]) -> List[tuple]:
    """(type, text, url) for every intro-card item, each once, in page order."""
    out, seen = [], set()
    for block in blocks:
        for node in _find(block, "timeline_context_item"):
            item = node.get("timeline_context_item")
            if not isinstance(item, dict):
                continue
            kind = item.get("timeline_context_list_item_type")
            context = ((item.get("renderer") or {}).get("context_item") or {}) if isinstance(item.get("renderer"), dict) else {}
            key = node.get("id") or (kind, json.dumps(context, sort_keys=True)[:200])
            if not kind or key in seen:
                continue
            seen.add(key)
            out.append((kind.replace("INTRO_CARD_", ""), _text(context.get("title")), context.get("url")))
    return out


def page_object(blocks: List[Any], page_id: Optional[str]) -> dict:
    """The Page object with `category_name` / `best_description`."""
    for block in blocks:
        for node in _find(block, "delegate_page"):
            page = node.get("delegate_page")
            if isinstance(page, dict) and page.get("id") == page_id and ("category_name" in page or "best_description" in page):
                return page
    return {}


# --------------------------------------------------------------------------- #
# The EXACT like count, from Meta's Ad Library (--exact-likes)
# --------------------------------------------------------------------------- #
# The Page itself shows a logged-out visitor a rounded figure ("39M"). The
# Ad Library, searched for the Page's own ads, carries the Page's EXACT like
# count on every ad it ran (`snapshot.page_like_count`; measured 2026-10-06:
# Nike 39,506,218, BBC News 62,043,915). Only a Page that has run ads has
# one, and a branded-content ad can name ANOTHER Page (one Nike ad carried
# a creator's 1,478), so only the snapshots of this Page's own id count.
AD_LIBRARY_SHELL = "AdLibraryFoundationRoot"
AD_LIBRARY_EMPTY = "No ads match your search criteria"


def ad_library_url(page_id: str) -> str:
    return ("https://www.facebook.com/ads/library/?active_status=all&ad_type=all&country=ALL&media_type=all"
            f"&search_type=page&view_all_page_id={page_id}")


def likes_from_ad_library(html: str, page_id: str) -> tuple:
    """(state, likes): `content` with the most common like count on this
    Page's own ads, `empty` (it has run none), `loading`, or `unknown`."""
    counts: dict = {}
    total = None
    for script in BeautifulSoup(html or "", "html.parser").find_all("script", type="application/json"):
        raw = script.string or script.get_text()
        if "search_results_connection" not in raw:
            continue
        try:
            block = json.loads(raw)
        except ValueError:
            continue
        for node in _find(block, "search_results_connection"):
            conn = node.get("search_results_connection")
            if isinstance(conn, dict) and isinstance(conn.get("count"), int):
                total = conn["count"] if total is None else max(total, conn["count"])
        for node in _find(block, "collated_results"):
            for ad in node.get("collated_results") or []:
                snap = ad.get("snapshot") if isinstance(ad, dict) else None
                likes = snap.get("page_like_count") if isinstance(snap, dict) else None
                if isinstance(likes, int) and not isinstance(likes, bool) and str(snap.get("page_id")) == str(page_id):
                    counts[likes] = counts.get(likes, 0) + 1
    if counts:
        return "content", max(counts, key=lambda k: (counts[k], k))
    if AD_LIBRARY_EMPTY in (html or "") or total == 0:
        return "empty", None
    if AD_LIBRARY_SHELL in (html or ""):
        return "loading", None
    return "unknown", None


# --------------------------------------------------------------------------- #
# Rows
# --------------------------------------------------------------------------- #
def make_sku(page_id: str) -> str:
    return f"facebook-page-{page_id}"


def make_post_sku(post_id: str) -> str:
    return f"facebook-post-{post_id}"


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class NotAPage(Exception):
    """A profile with no Page behind it — a private person's."""


def page_row(html: str, *, scraped_at: Optional[str] = None) -> Product:
    """The Page header + intro card as one row. Raises NotAPage for a
    personal profile, ValueError when the page carries no header."""
    blocks = json_blocks(html)
    hdr = header(blocks)
    if hdr is None:
        raise ValueError("no profile header in this page")
    user = hdr["user"]
    page_id = str((user.get("delegate_page") or {}).get("id") or "") or None
    if not page_id:
        raise NotAPage(user.get("url") or user.get("id"))
    page = page_object(blocks, page_id)
    url = user.get("url") if isinstance(user.get("url"), str) else None
    social = [_text(c.get("text")) for c in (user.get("profile_social_context") or {}).get("content") or [] if isinstance(c, dict)]
    followers_text = next((s for s in social if s and re.search(r"\bfollowers?\b", s)), None)
    following_text = next((s for s in social if s and re.search(r"\bfollowing\b", s)), None)
    likes_text = next((s for s in social if s and re.search(r"\blikes?\b|people like this", s)), None)

    items = intro_items(blocks)

    def first(kind):
        return next(((t, u) for k, t, u in items if k == kind), (None, None))

    category_text = first("INFLUENCER_CATEGORY")[0]
    category = page.get("category_name") if isinstance(page.get("category_name"), str) else None
    if not category and category_text and "·" in category_text:
        category = category_text.split("·", 1)[1].strip()
    website_text, website_url = first("WEBSITE")
    rating_text = first("RATING")[0]
    rating = _RATING_RE.search(rating_text or "")
    price = first("BUSINESS_PRICE")[0]
    if price and "·" in price:
        price = price.split("·", 1)[1].strip()
    verified = re.search(r'"show_verified_badge_on_profile":(true|false)', html or "")
    cover = (user.get("cover_photo") or {}).get("photo") if isinstance(user.get("cover_photo"), dict) else None

    return Product(
        sku=make_sku(page_id),
        source=SOURCE,
        category="page",
        title=user.get("name") if isinstance(user.get("name"), str) else None,
        brand=None, price=None, currency=None, price_source=None,
        product_url=url,
        image_url=_uri(user.get("profilePicLarge")),
        scraped_at=scraped_at or now_iso(),
        page_id=page_id,
        profile_id=str(user["id"]) if user.get("id") else None,
        username=username_from_url(url or ""),
        page_category=category,
        description=_text(page.get("best_description")),
        is_verified=(verified.group(1) == "true") if verified else None,
        followers=rounded_count(followers_text),
        followers_text=followers_text,
        following=rounded_count(following_text),
        likes=rounded_count(likes_text),
        website=_unwrap_link(website_url) or website_text,
        phone=first("PROFILE_PHONE")[0],
        email=first("PROFILE_EMAIL")[0],
        address=first("ADDRESS")[0],
        hours=first("BUSINESS_HOURS")[0],
        price_range=price,
        recommend_pct=int(rating.group(1)) if rating else None,
        review_count=int(rating.group(2).replace(",", "")) if rating else None,
        owner=first("CONFIRMED_OWNER_LABEL")[0],
        other_accounts_json=_json_list([t for k, t, _ in items if k == "OTHER_ACCOUNT" and t]),
        cover_url=_uri(cover.get("image")) if isinstance(cover, dict) else None,
    )


def _outside_comments(obj: Any, key: str) -> Iterator[dict]:
    """Like _find, but never inside a comment: a post carries its comments'
    own reaction counts too (seen on every capture), and the first one in
    document order being the post's own is luck, not structure."""
    stack = [obj]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if key in node:
                yield node
            stack.extend(reversed([v for k, v in node.items() if "comment" not in k.lower()]))
        elif isinstance(node, list):
            stack.extend(reversed(node))


def _count(story: dict, key: str) -> Optional[int]:
    for node in _outside_comments(story, key):
        value = node.get(key)
        if isinstance(value, dict) and isinstance(value.get("count"), int):
            return value["count"]
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def stories(payloads: List[Any]) -> List[dict]:
    """Timeline posts, each once, in the site's order."""
    out, seen = [], set()
    for payload in payloads:
        for node in _find(payload, "post_id"):
            if "comet_sections" not in node or not isinstance(node.get("post_id"), str):
                continue
            if node["post_id"] in seen:
                continue
            seen.add(node["post_id"])
            out.append(node)
    return out


def post_row(story: dict, *, page: Product, position: int, scraped_at: Optional[str] = None) -> Optional[Product]:
    post_id = story.get("post_id")
    if not isinstance(post_id, str) or not post_id.isdigit():
        return None
    message = next((_text(n["message"]) for n in _find(story, "message") if isinstance(n.get("message"), dict)
                    and _text(n["message"])), None)
    comments = None
    for node in _find(story, "comment_rendering_instance"):
        inst = node.get("comment_rendering_instance")
        total = ((inst or {}).get("comments") or {}).get("total_count") if isinstance(inst, dict) else None
        if isinstance(total, int):
            comments = total
            break
    ts = story.get("creation_time")
    posted = (_dt.datetime.fromtimestamp(ts, _dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
              if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0 else None)
    first_line = message.splitlines()[0].strip() if message else None
    return Product(
        sku=make_post_sku(post_id),
        source=SOURCE,
        category="post",
        title=(first_line[:119] + "…") if first_line and len(first_line) > 120 else first_line,
        brand=None, price=None, currency=None, price_source=None,
        product_url=story.get("permalink_url") if isinstance(story.get("permalink_url"), str) else None,
        image_url=None,
        scraped_at=scraped_at or now_iso(),
        page_id=page.page_id,
        username=page.username,
        post_id=post_id,
        posted_at=posted,
        text=message,
        reactions=_count(story, "reaction_count"),
        comments=comments,
        shares=_count(story, "share_count"),
        video_views=_count(story, "video_view_count"),
        position=position,
    )
