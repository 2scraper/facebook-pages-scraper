#!/usr/bin/env python3
"""output_writer.py — the row model, JSON/CSV writer, dedupe, exit codes,
and run metadata. Carries (almost) no site knowledge: everything here is
part of the family's shared output CONTRACT — diverging from it needs a
written reason, not a per-site tweak. The exit codes,
STATUS_BY_EXIT map, and finish_run() outcome-precedence logic are
IDENTICAL to every prior family member on purpose, including the fix
from a 2026-09-15 audit that found `blocked`/`remote_api_error` were
being silently ignored whenever products were present or --allow-empty
was passed. Only the `Product` dataclass's site-specific tail (below the
family-common fields) differs per repo.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import List, Optional, Sequence

# --------------------------------------------------------------------------- #
# Exit codes — identical across playwright_scraper / selenium_scraper /
# puppeteer_scraper, and identical to every other 2scraper family repo. A
# caller (CI, a cron job, another program) must be able to tell these apart
# without parsing stdout.
# --------------------------------------------------------------------------- #
EXIT_OK = 0
EXIT_CRASH = 1
EXIT_BAD_USAGE = 2
EXIT_BLOCKED = 3
EXIT_ZERO_PRODUCTS = 4
EXIT_REMOTE_API_ERROR = 5
EXIT_PARTIAL = 6

STATUS_BY_EXIT = {
    EXIT_OK: "complete",
    EXIT_CRASH: "crashed",
    EXIT_BAD_USAGE: "bad_usage",
    EXIT_BLOCKED: "blocked",
    EXIT_ZERO_PRODUCTS: "empty",
    EXIT_REMOTE_API_ERROR: "remote_api_error",
    EXIT_PARTIAL: "partial",
}


# --------------------------------------------------------------------------- #
# Row model
# --------------------------------------------------------------------------- #
@dataclass
class Product:
    """Family-common fields first (identical name AND order to every other
    2scraper repo's row, in both JSON and CSV) so `diff_runs.py` and any
    other cross-repo tool keep working unmodified; Page fields at the end.

    Two kinds of row, told apart by `category`:

    - `page`: one public Facebook Page, as a logged-out visitor sees it.
      `sku` is `facebook-page-{page_id}` (the Page id the Ad Library also
      uses, so a Page reached by its username, its profile id or its Page
      id is one row); `title` is its name; `product_url` its address;
      `image_url` its profile picture.
    - `post`: one of the (at most three) posts a logged-out visitor is
      shown, with `--posts`. `sku` is `facebook-post-{post_id}`; `title` the
      first line of its text; `product_url` its permalink.

    `brand` / `price` / `currency` / `price_source` are **not applicable**
    and always `None`.

    Page columns (see page_parser.py for where each comes from):

    - `page_id`, `profile_id`, `username`, `page_category`, `description`,
      `is_verified` (the badge the Page shows).
    - `followers` / `following` / `likes`: the counts the Page shows, which
      Facebook ROUNDS ("39M followers" → 39000000); `followers_text` keeps
      the original. Exact numbers are not served to a logged-out visitor.
    - `likes_exact` (with `--exact-likes`): the Page's EXACT like count,
      read from Meta's Ad Library — only for a Page that has run ads.
    - from the intro card, read by item type: `website`, `phone`, `email`,
      `address`, `hours`, `price_range`, `recommend_pct` and
      `review_count` (from "32% recommend (37,633 reviews)"), `owner` (the
      legal owner Meta confirmed), `other_accounts_json` (a linked
      Instagram account); `cover_url`.

    Post columns: `post_id`, `posted_at` (UTC), `text`, and the EXACT
    `reactions`, `comments`, `shares` and, for a video, `video_views`;
    `page_id` / `username` name the Page; `position` is its place on the
    Page (1 = first shown).
    """

    # --- family-common ---
    sku: Optional[str]
    source: str
    category: Optional[str]
    title: Optional[str]
    brand: Optional[str]
    price: Optional[float]
    currency: Optional[str]
    price_source: Optional[str]
    product_url: Optional[str]
    image_url: Optional[str]
    scraped_at: str

    # --- the Page ---
    page_id: Optional[str] = None
    profile_id: Optional[str] = None
    username: Optional[str] = None
    page_category: Optional[str] = None
    description: Optional[str] = None
    is_verified: Optional[bool] = None
    followers: Optional[int] = None
    followers_text: Optional[str] = None
    following: Optional[int] = None
    likes: Optional[int] = None
    likes_exact: Optional[int] = None
    # --- its intro card ---
    website: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    hours: Optional[str] = None
    price_range: Optional[str] = None
    recommend_pct: Optional[int] = None
    review_count: Optional[int] = None
    owner: Optional[str] = None
    other_accounts_json: Optional[str] = None
    cover_url: Optional[str] = None
    # --- a post ---
    post_id: Optional[str] = None
    posted_at: Optional[str] = None
    text: Optional[str] = None
    reactions: Optional[int] = None
    comments: Optional[int] = None
    shares: Optional[int] = None
    video_views: Optional[int] = None
    position: Optional[int] = None


PRODUCT_FIELD_NAMES: List[str] = [f.name for f in fields(Product)]


# --------------------------------------------------------------------------- #
# Dedupe / merge — "merge in page order, not arrival order": the caller
# passes pages already sorted by page number (or, for a concurrent run,
# sorted before calling this), never in whichever-finished-first order.
# --------------------------------------------------------------------------- #
def sku_key(p: Product) -> str:
    """The identity `merge_pages` dedupes on, and the same one an engine's
    scroll/pagination loop should track to decide "did this page add
    anything NEW" — never just "is this page non-empty" (a page repeating
    an already-seen item, e.g. past the real last batch of results, isn't
    empty but isn't new either)."""
    return p.sku or f"__no_sku__:{p.product_url}"


def merge_pages(pages: Sequence[Sequence[Product]]) -> List[Product]:
    seen: dict = {}
    order: List[str] = []
    for page in pages:
        for p in page:
            key = sku_key(p)
            if key not in seen:
                order.append(key)
            seen[key] = p  # last write for a given sku wins, in page order
    return [seen[k] for k in order]


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
CSV_TEXT_ENCODING = "apostrophe-v1"


def csv_text(value):
    """Escape spreadsheet formulas and literal leading apostrophes reversibly."""
    if isinstance(value, str) and (value.startswith(("'", "\t", "\r", "\n"))
                                  or value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@"))):
        return "'" + value
    return value


def _target_mode(target: Path) -> int:
    """The mode the finished file gets: an existing output keeps its own
    mode (a file someone chmod-ed 0640 stays 0640), a new one gets what a
    plain open() would have given it (0666 minus the umask)."""
    try:
        return stat.S_IMODE(os.stat(target).st_mode)
    except FileNotFoundError:
        umask = os.umask(0)
        os.umask(umask)
        return 0o666 & ~umask


@contextmanager
def _atomic_text(out_path, *, newline=None):
    """Write to a temporary file in the target's own directory, fsync it,
    then os.replace() it over the target: a crash, a kill or a full disk
    halfway through leaves the previous file untouched, never a truncated
    one. mkstemp creates the temporary file 0600, so the mode is set
    explicitly before the rename."""
    target = Path(out_path)
    fd, temporary = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline=newline) as stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, _target_mode(target))
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(products: Sequence[Product], out_path: str) -> int:
    with _atomic_text(out_path) as f:
        json.dump([asdict(p) for p in products], f, ensure_ascii=False, indent=2)
    return 0


def write_csv(products: Sequence[Product], out_path: str) -> int:
    """Returns how many cells csv_text() changed (recorded in the sidecar
    as csv_cells_escaped, so the difference from the JSON is declared)."""
    escaped = 0
    with _atomic_text(out_path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=PRODUCT_FIELD_NAMES)
        writer.writeheader()  # written even for zero rows — a consumer reads
        for p in products:    # an empty table, not a zero-byte file.
            row = {}
            for name, value in asdict(p).items():
                cell = csv_text(value)
                escaped += cell is not value
                row[name] = cell
            writer.writerow(row)
    return escaped


def write_output(products: Sequence[Product], out_path: str, fmt: str) -> int:
    """Writes the rows; returns the number of CSV cells escaped (0 for JSON)."""
    if fmt == "json":
        return write_json(products, out_path)
    if fmt == "csv":
        return write_csv(products, out_path)
    raise ValueError(f"Unsupported format: {fmt!r} (expected 'json' or 'csv')")


# --------------------------------------------------------------------------- #
# Run metadata sidecar
# --------------------------------------------------------------------------- #
def meta_path_for(out_path: str) -> str:
    return f"{out_path}.meta.json"


def write_meta(
    out_path: str,
    *,
    status: str,
    stop_reason: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]] = None,
    product_count: int,
    price_confirmed_pct: Optional[float] = None,
    started_at: float,
    finished_at: Optional[float] = None,
    extra: Optional[dict] = None,
) -> None:
    meta = {
        "status": status,
        "stop_reason": stop_reason,
        "engine": engine,
        "url": url,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "failed_pages": failed_pages or [],
        "product_count": product_count,
        "price_confirmed_pct": price_confirmed_pct,
        "started_at": started_at,
        "finished_at": finished_at or time.time(),
    }
    if extra:
        meta.update(extra)
    with _atomic_text(meta_path_for(out_path)) as stream:
        json.dump(meta, stream, ensure_ascii=False, indent=2)


# --------------------------------------------------------------------------- #
# finish_run — the single place every engine calls to decide exit code,
# whether to write output at all, and whether to write a sidecar. Keeping
# this in one shared function is what stops the three engines' exit-code
# mapping from drifting apart.
# --------------------------------------------------------------------------- #
def finish_run(
    *,
    products: List[Product],
    out_path: str,
    fmt: str,
    engine: str,
    url: str,
    pages_requested: int,
    pages_completed: int,
    failed_pages: Optional[List[int]],
    blocked: bool,
    remote_api_error: bool,
    allow_empty: bool,
    started_at: float,
    price_confirmed_pct: Optional[float] = None,
    extra_meta: Optional[dict] = None,
    rejected_rows: int = 0,
    max_results: Optional[int] = None,
    rate_limited: bool = False,
    total_results: Optional[int] = None,
    incomplete_reason: Optional[str] = None,
    capped: Optional[bool] = None,
) -> int:
    """Decide status/exit code, write output + sidecar (or neither), return
    the process exit code. NEVER writes a sidecar for a failed run, and
    NEVER overwrites a previous good output with an empty one unless the
    caller explicitly passed --allow-empty."""
    failed_pages = failed_pages or []
    partial = bool(failed_pages) or bool(incomplete_reason)
    zero_products = len(products) == 0

    # Outcome precedence — decided ONCE, independent of --allow-empty.
    # `--allow-empty` controls only whether a zero-product result gets
    # WRITTEN as a file (below); it must never launder a blocked or
    # remote-API-error run into a "complete" status just because the
    # caller also passed --allow-empty, and it must never do so just
    # because SOME batches did return results while the run was, in fact,
    # blocked partway through.
    #
    # Rows gathered by a run that did NOT finish cleanly are `partial` (6),
    # with the cause in `stop_reason` (exit 5 and exit 3
    # promise no file; 6 means "some rows, incomplete"). Audit 2026-09-30:
    # this used to return 5 or 3 AND write the rows, so a consumer keyed on
    # the exit code threw away good data. `rejected_rows` counts records
    # the parser refused — the output is then incomplete too.
    if zero_products:
        if remote_api_error:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        elif blocked:
            status, exit_code = "blocked", EXIT_BLOCKED
        elif partial or rejected_rows:
            status, exit_code = "remote_api_error", EXIT_REMOTE_API_ERROR
        else:
            status, exit_code = "empty", EXIT_ZERO_PRODUCTS
        stop_reason = ("rate_limited" if rate_limited and blocked else incomplete_reason
                       or ("failed_pages" if failed_pages and not blocked and not remote_api_error else status))
    elif remote_api_error or blocked or partial or rejected_rows:
        status, exit_code = "partial", EXIT_PARTIAL
        # A throttle is not a block: "blocked" sends the
        # reader to buy a proxy, "rate_limited" to slow down.
        stop_reason = ("remote_api_error" if remote_api_error else "rate_limited" if rate_limited and blocked
                       else "blocked" if blocked else incomplete_reason or ("failed_pages" if partial else "rejected_rows"))
    else:
        status, exit_code = "complete", EXIT_OK
        stop_reason = status

    # The "never overwrite good output with empty" rule: a zero-product
    # outcome (whatever its status above — blocked/remote_api_error/empty
    # all zero out `products`) writes NEITHER file NOR sidecar unless the
    # caller explicitly opted in with --allow-empty. NEVER write a sidecar
    # in the not-written case — a PREVIOUS good <out>.json is left in
    # place untouched, and a stale failure sidecar sitting right beside it
    # would contradict that good data rather than describe it. The
    # engine's own logs carry the diagnostic detail — that's what a
    # failed run's output is FOR, not this file.
    if zero_products and not allow_empty:
        return exit_code

    escaped = write_output(products, out_path, fmt)
    extra = dict(extra_meta or {})
    if fmt == "csv":
        # Housekeeping goes UNDER the caller's keys, never over them.
        extra.setdefault("csv_text_encoding", CSV_TEXT_ENCODING)
        extra.setdefault("csv_cells_escaped", escaped)
    if rejected_rows:
        extra["rejected_rows"] = rejected_rows
    # What diff_runs needs to refuse a meaningless comparison: the cap the
    # run was given (a top-N selection, where "removed" means "fell out of
    # the top N", not "delisted") and a hash binding this sidecar to the
    # exact output file beside it.
    if max_results is not None:
        extra["max_results"] = max_results
        extra["capped"] = len(products) >= max_results if capped is None else capped
    if total_results is not None:
        extra["total_results"] = total_results  # what the site says exists, vs product_count collected
    extra["output_sha256"] = hashlib.sha256(Path(out_path).read_bytes()).hexdigest()
    write_meta(
        out_path, status=status, stop_reason=stop_reason, engine=engine, url=url,
        pages_requested=pages_requested, pages_completed=pages_completed,
        failed_pages=failed_pages, product_count=len(products),
        price_confirmed_pct=price_confirmed_pct, started_at=started_at,
        extra=extra or None,
    )
    return exit_code
