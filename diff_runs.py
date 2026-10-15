#!/usr/bin/env python3
"""diff_runs.py — compare two complete runs by SKU (Page id / post id)
and a set of monitored Page and post fields.

Refuses to compare two runs that are not both `status == "complete"` in
their `.meta.json` sidecar: a partial run's un-fetched pages would otherwise
read as delisted products, which is a false signal worse than no diff at
all.

A price difference that comes with a `price_source` difference is reported
as `source_changed`, not `changed` — it says something about how much of
each run's data got DOM-confirmed, not about the site. `--fail-on-change`
ignores `source_changed` rows for exactly that reason.

Added after the 2026-09-30 audit, which diffed one SKU at 9.93 USD from a
"dress" run against 19.93 EUR from a "jeans" run and got a price change:

- Two runs must share their scope — the sidecar `url` (host, path and
  query: market, search term, category) and `sort`. Otherwise the diff is
  refused.
- A currency difference on a SKU is `currency_changed`, never `changed`,
  and is reported even when the number is the same.
- When either run was capped by `--max-results` (sidecar `capped`), a SKU
  missing from the new run is `left_selection` (it fell out of the top N),
  not `removed`.
- A sidecar carrying `output_sha256` must match the file beside it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qsl, urlencode, urlparse


# Followers are ROUNDED by the site ("39M"), so a change there is a change
# of the displayed figure, not a precise delta; it is still the number a
# visitor sees. Post counts (reactions, comments, shares, views) are exact.
MONITOR_FIELDS = ("title", "page_category", "description", "is_verified", "followers", "following", "likes", "likes_exact",
                  "website", "phone", "email", "address", "hours", "price_range", "recommend_pct",
                  "review_count", "owner", "reactions", "comments", "shares", "video_views", "text")
EXTRA_FIELDS = ("followers_text", "other_accounts_json", "cover_url", "image_url")
NUMERIC_FIELDS = {"followers", "following", "likes", "likes_exact", "recommend_pct", "review_count", "reactions", "comments",
                  "shares", "video_views"}
BOOL_FIELDS = {"is_verified"}


def field_value(row, field):
    value = row.get(field)
    if value is None or value == "":
        return None
    if field in NUMERIC_FIELDS and isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return value
    if field in BOOL_FIELDS and isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _load_meta(run_path: str) -> dict:
    meta_path = Path(f"{run_path}.meta.json")
    if not meta_path.exists():
        raise SystemExit(f"error: no sidecar found at {meta_path} — was this run produced by finish_run()?")
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _load_rows(run_path: str) -> List[dict]:
    path = Path(run_path)
    if path.suffix == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix == ".csv":
        import csv
        with path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if _load_meta(run_path).get("csv_text_encoding") == "apostrophe-v1":
            rows = [{key: value[1:] if isinstance(value, str) and value.startswith("'") else value
                     for key, value in row.items()} for row in rows]
        return rows
    raise SystemExit(f"error: unsupported run file extension: {path.suffix}")


def _index_by_sku(rows: List[dict]) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    for row in rows:
        sku = row.get("sku")
        if sku:
            out[sku] = row
    return out


def normalize_scope(url: str) -> str:
    """host + path + sorted query — what makes two runs the same selection."""
    parts = urlparse(url or "")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return f"{parts.netloc.lower()}{parts.path.rstrip('/')}?{query}"


def _url_key(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def diff(old_path: str, new_path: str, *, allow_different_scope: bool = False, fields=None) -> dict:
    fields = list(MONITOR_FIELDS if fields is None else fields)
    if any(name not in MONITOR_FIELDS + EXTRA_FIELDS for name in fields):
        raise ValueError("Unsupported monitor field; choose from " + ", ".join(MONITOR_FIELDS + EXTRA_FIELDS))
    old_meta, new_meta = _load_meta(old_path), _load_meta(new_path)
    for label, meta in (("old", old_meta), ("new", new_meta)):
        if meta.get("status") != "complete":
            raise SystemExit(
                f"error: refusing to diff — {label} run status is "
                f"{meta.get('status')!r}, not 'complete'. A partial run's "
                f"un-fetched pages would read as delisted products."
            )
    for label, path, meta in (("old", old_path, old_meta), ("new", new_path, new_meta)):
        expected = meta.get("output_sha256")
        if expected and hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise SystemExit(f"error: {label} run's sidecar does not describe {path} (output_sha256 mismatch).")
    if not allow_different_scope and normalize_scope(old_meta.get("url", "")) != normalize_scope(new_meta.get("url", "")):
        raise SystemExit(
            f"error: refusing to diff runs of different selections: {old_meta.get('url')!r} vs "
            f"{new_meta.get('url')!r}. Pass --allow-different-scope to compare them anyway."
        )
    if not allow_different_scope and (old_meta.get("sort") or None) != (new_meta.get("sort") or None):
        raise SystemExit(
            f"error: refusing to diff runs with different --sort ({old_meta.get('sort')!r} vs "
            f"{new_meta.get('sort')!r}) — a capped top-N under another ordering is another selection."
        )
    if not allow_different_scope and old_meta.get("selection") != new_meta.get("selection"):
        raise SystemExit("error: refusing to diff different input selections (or legacy metadata without selection).")
    capped = bool(old_meta.get("capped") or new_meta.get("capped"))

    old_rows, new_rows = _index_by_sku(_load_rows(old_path)), _index_by_sku(_load_rows(new_path))
    old_skus, new_skus = set(old_rows), set(new_rows)

    added = sorted(new_skus - old_skus)
    missing = sorted(old_skus - new_skus)
    # A post missing from the new run has usually just been pushed out of
    # the three a Page shows; only a Page (or a post of a Page) whose ADDRESS
    # the new run found gone is really removed. In a capped run (more Pages
    # than --max-results) a missing Page may only have been left out.
    #
    # `not_found_urls` holds the INPUT addresses the new run asked for, which
    # need not be the Page's canonical product_url (a profile.php?id=… or an
    # @Name input). The old sidecar's `pages` maps each input address to the
    # Page it led to, so both are checked, case-insensitively.
    gone = {_url_key(u) for u in (new_meta.get("not_found_urls") or [])}
    old_inputs: Dict[str, set] = {}
    for entry in old_meta.get("pages") or []:
        if isinstance(entry, dict) and entry.get("sku") and entry.get("url"):
            old_inputs.setdefault(entry["sku"], set()).add(_url_key(entry["url"]))

    def _address_gone(sku: str) -> bool:
        addresses = {_url_key(old_rows[sku].get("product_url") or "")} | old_inputs.get(sku, set())
        return bool(addresses & gone)

    removed = [sku for sku in missing if old_rows[sku].get("category") == "page" and not capped and _address_gone(sku)]
    left_selection = [sku for sku in missing if sku not in removed]
    changed, source_changed, currency_changed, field_changes = [], [], [], []

    for sku in sorted(old_skus & new_skus):
        o, n = old_rows[sku], new_rows[sku]
        changes = {}
        for name in fields:
            before, after = field_value(o, name), field_value(n, name)
            if before != after:
                changes[name] = {"old": before, "new": after}
                if name in NUMERIC_FIELDS and isinstance(before, int) and isinstance(after, int):
                    changes[name]["delta"] = after - before
        if changes:
            field_changes.append({"sku": sku, "fields": changes})
        old_price, new_price = o.get("price"), n.get("price")
        if (o.get("currency") or None) != (n.get("currency") or None):
            currency_changed.append({
                "sku": sku, "old_price": old_price, "old_currency": o.get("currency"),
                "new_price": new_price, "new_currency": n.get("currency"), "title": n.get("title"),
            })
            continue
        if old_price == new_price:
            continue
        entry = {"sku": sku, "old_price": old_price, "new_price": new_price, "title": n.get("title")}
        if o.get("price_source") != n.get("price_source"):
            entry["old_price_source"] = o.get("price_source")
            entry["new_price_source"] = n.get("price_source")
            source_changed.append(entry)
        else:
            changed.append(entry)

    return {
        "added": added, "removed": removed, "left_selection": left_selection,
        "field_changes": field_changes, "changed": changed, "source_changed": source_changed, "currency_changed": currency_changed,
        "old_count": len(old_rows), "new_count": len(new_rows), "capped": capped,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Diff two facebook-pages-scraper runs by sku (Page id / post id)")
    p.add_argument("old_run")
    p.add_argument("new_run")
    p.add_argument("--fail-on-change", action="store_true",
                    help="Exit 1 when monitored fields or selection membership change")
    p.add_argument("--json", action="store_true", help="Print the diff as JSON instead of a summary")
    p.add_argument("--allow-different-scope", action="store_true",
                    help="Compare runs whose sidecar URLs (market, query, category) differ")
    p.add_argument("--fields", help="Comma-separated fields to monitor; default: " + ",".join(MONITOR_FIELDS)
                   + "; also available: " + ",".join(EXTRA_FIELDS))
    args = p.parse_args()
    fields = [name.strip() for name in args.fields.split(",") if name.strip()] if args.fields else None
    if fields and any(name not in MONITOR_FIELDS + EXTRA_FIELDS for name in fields):
        p.error("Unknown --fields value; choose from " + ",".join(MONITOR_FIELDS + EXTRA_FIELDS))

    try:
        result = diff(args.old_run, args.new_run, allow_different_scope=args.allow_different_scope, fields=fields)
    except SystemExit as exc:
        # A refused comparison (missing sidecar, a run that did not complete,
        # different selections) is a usage error: exit 2, never 1, which
        # --fail-on-change uses for "something changed".
        if isinstance(exc.code, str):
            message = exc.code[len("error: "):] if exc.code.startswith("error: ") else exc.code
            p.error(message)
        raise

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"{args.old_run} ({result['old_count']}) -> {args.new_run} ({result['new_count']})")
        print(f"  added:          {len(result['added'])}")
        print(f"  removed:        {len(result['removed'])}")
        if result["capped"]:
            print(f"  left_selection: {len(result['left_selection'])} (a capped top-N run: fell out of the selection, not delisted)")
        print(f"  monitored rows changed: {len(result['field_changes'])}")
        for entry in result["field_changes"]:
            print(f"    {entry['sku']}: " + json.dumps(entry["fields"], ensure_ascii=False))
        print(f"  price changed:  {len(result['changed'])}")
        print(f"  source_changed: {len(result['source_changed'])} (ignored by --fail-on-change)")
        print(f"  currency_changed: {len(result['currency_changed'])} (ignored by --fail-on-change: a market change, not a price change)")

    if args.fail_on_change and any(result[key] for key in ("changed", "field_changes", "added", "removed", "left_selection")):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
