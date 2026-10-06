#!/usr/bin/env python3
"""smoke_test.py — one file of plain functions with inline/synthetic-
fixture checks. No pytest, no conftest. `tests/test_smoke.py` wraps this as
a single pytest entry point so `pytest` also works, without a second copy
of the checks.

**What the fixtures are**: every fixture in `tests/fixtures/` comes from a
REAL logged-out capture of a public facebook.com Page — Nike, BBC News, DER
SPIEGEL (2026-10-06), McDonald's (2026-10-05), and the answer for an
address that is no Page — plus the timeline response that brings a Page's
second and third post. They are NOT verbatim: each is rebuilt from the
page's own JSON keeping only the paths page_parser.py reads, with their
real values, and dropping session tokens, comments and the people who
wrote them (see each file's header). Values are asserted against what
those captures hold, not just that a column is populated (CLAUDE.md §10).

Run directly: `python3 smoke_test.py`
"""
from __future__ import annotations

import asyncio
import inspect as _inspect
import json
import re
import tempfile
from pathlib import Path

import page_parser as pp
import captcha_solver
import diff_runs
import env_config
import output_writer
import page_flow
import proxy_pool
import puppeteer_scraper
import scraper_api_client
import selenium_scraper

try:
    import playwright_scraper
except Exception as exc:  # pragma: no cover — this import itself must never fail
    raise AssertionError(f"playwright_scraper must import cleanly even without playwright installed: {exc}") from exc

ROOT = Path(__file__).parent

RESULTS = []  # (name, ok, detail)


def check(name):
    """Runs the decorated function IMMEDIATELY (at module-load time) and
    records the outcome — same pattern as every other family member's
    smoke_test.py; every check function is named `_` because only RESULTS
    is ever read, nothing looks a check up by name."""
    def decorator(fn):
        try:
            fn()
            RESULTS.append((name, True, ""))
        except AssertionError as exc:
            RESULTS.append((name, False, str(exc)))
        except Exception as exc:  # a check that crashes is still a failure, not an uncaught traceback
            RESULTS.append((name, False, f"{type(exc).__name__}: {exc}"))
        except SystemExit as exc:  # a CLI helper exiting inside a check would otherwise end the whole suite silently
            RESULTS.append((name, False, f"SystemExit: {exc}"))
        return fn
    return decorator


def asyncio_run_maybe(mod, args):
    """playwright_scraper.run()/puppeteer_scraper.run() are coroutines;
    selenium_scraper.run() is plain sync."""
    result = mod.run(args)
    if _inspect.iscoroutine(result):
        return asyncio.run(result)
    return result


_ENGINES = (playwright_scraper, selenium_scraper, puppeteer_scraper)
_FIX = ROOT / "tests" / "fixtures"


def _fx(name):
    path = next(_FIX.glob(f"fb_page_{name}_2026100?.*"))
    return path.read_text(encoding="utf-8")


_PAGES = ("nike", "bbcnews", "spiegel", "mcdonalds", "not_found")
_NIKE_URL = "https://www.facebook.com/nike"
_NIKE_POSTS = ["1393461115481927", "1392071868954185", "1388501259311246"]
_SAPI_ARGV = ["--url", "nike"]
_CHROMIUM_ERROR = ('<html><head><title>www.facebook.com</title></head><body><div id="main-message">'
                   '<h1>This site can’t be reached</h1><div class="error-code">ERR_TIMED_OUT</div></div></body></html>')


# --------------------------------------------------------------------------- #
# Engine import/CLI hygiene (CLAUDE.md §6)
# --------------------------------------------------------------------------- #
@check("engines import cleanly regardless of installed drivers")
def _():
    for mod in _ENGINES:
        assert hasattr(mod, "build_arg_parser")
        assert hasattr(mod, "run")


@check("each engine imports its driver at MODULE level, guarded by try/except ImportError (an ast walk, not a substring: a driver imported inside a function makes the no-driver skip meaningless — CLAUDE.md §10)")
def _():
    import ast as _ast
    drivers = {"playwright_scraper": "playwright", "selenium_scraper": "selenium", "puppeteer_scraper": "pyppeteer"}
    for mod in _ENGINES:
        tree = _ast.parse((ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8"))
        guarded = [n for n in tree.body if isinstance(n, _ast.Try)
                   and any(isinstance(h.type, _ast.Name) and h.type.id == "ImportError" for h in n.handlers)
                   and any(isinstance(s, _ast.ImportFrom) and (s.module or "").split(".")[0] == drivers[mod.__name__]
                           or isinstance(s, _ast.Import) and any(a.name.split(".")[0] == drivers[mod.__name__] for a in s.names)
                           for s in n.body)]
        assert guarded, f"{mod.__name__}: no module-level guarded import of {drivers[mod.__name__]}"


@check("no module uses a name it never imports, defines or assigns (compileall proves a file PARSES, not that its names RESOLVE — CLAUDE.md §10)")
def _():
    import ast as _ast
    import builtins
    for path in sorted(ROOT.glob("*.py")):
        tree = _ast.parse(path.read_text(encoding="utf-8"))
        bound = set(dir(builtins)) | {"__file__", "__name__", "__doc__"}
        for node in _ast.walk(tree):
            if isinstance(node, (_ast.Import, _ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
            elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
                bound.add(node.name)
                a = node.args if not isinstance(node, _ast.ClassDef) else None
                if a is not None:
                    bound |= {x.arg for x in a.args + a.kwonlyargs + a.posonlyargs}
                    bound |= {x.arg for x in (a.vararg, a.kwarg) if x is not None}
            elif isinstance(node, _ast.Name) and isinstance(node.ctx, (_ast.Store, _ast.Del)):
                bound.add(node.id)
            elif isinstance(node, _ast.arg):
                bound.add(node.arg)
            elif isinstance(node, _ast.ExceptHandler) and node.name:
                bound.add(node.name)
            elif isinstance(node, _ast.alias):
                bound.add((node.asname or node.name).split(".")[0])
        used = {n.id for n in _ast.walk(tree) if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
        assert not (used - bound), f"{path.name}: unresolved names {sorted(used - bound)}"


@check("no forbidden overclaiming wording in any shipped .py/.md/.yml file")
def _():
    # Built from pieces so this file can be scanned too (CLAUDE.md §22: the
    # check used to exempt its own file, where the phrases sat verbatim).
    anti = "anti" + "detect"
    banned = (
        "cloud" + " browser", anti + " browser", "2scraper " + anti + " browser",
        "gate." + "2prx.com", "--" + anti, anti + "_local_api",
    )
    exempt_names = {"CLAUDE.md"}
    venvs = {p.parent for p in ROOT.rglob("pyvenv.cfg")}
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in (".py", ".md", ".html", ".toml", ".cfg", ".yml", ".yaml"):
            continue
        if path.name in exempt_names or path.name.startswith("2scraper"):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or any(v in path.parents for v in venvs):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for phrase in banned:
            assert phrase not in text, f"{path.relative_to(ROOT)}: contains banned phrase {phrase!r}"


@check("nothing shipped still names the repo this one was bootstrapped from (facebook-ads-scraper's module, flags, output names or site)")
def _():
    # Mentions of the companion repo (facebook-ads-scraper, the Ad Library) are
    # legitimate cross-references; what must not survive is its CODE.
    leftovers = ("ad_parser", "run_state", "INSTAGRAM_", "instagram_results", "facebook_ads.", "search_url(",
                 "--query", "--region", "--active-status")
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in (".py", ".md", ".html", ".toml", ".yml", ".txt", ".example", ""):
            continue
        if ".git" in path.parts or "__pycache__" in path.parts or "fixtures" in path.parts or path.name == "smoke_test.py":
            continue
        if any(part.endswith(".egg-info") or part in ("build", "dist") or part.startswith(".venv") for part in path.parts):
            continue  # build artefacts, git-ignored and never shipped
        text = path.read_text(encoding="utf-8", errors="ignore")
        for word in leftovers:
            assert word not in text, f"{path.relative_to(ROOT)}: still says {word!r}"


@check("all three engines expose the identical --flag set (CLAUDE.md §4)")
def _():
    def flag_set(mod):
        return {opt for a in mod.build_arg_parser()._actions for opt in a.option_strings if opt.startswith("--")}

    pw, se, pu = flag_set(playwright_scraper), flag_set(selenium_scraper), flag_set(puppeteer_scraper)
    all_engines = pw | se | pu
    for name, flags in (("playwright_scraper", pw), ("selenium_scraper", se), ("puppeteer_scraper", pu)):
        missing = all_engines - flags
        assert not missing, f"{name} is missing {sorted(missing)} that (an)other engine(s) define — flag sets have drifted apart"


@check("all three engines share the same default output filename stem and the same defaults")
def _():
    for mod in _ENGINES:
        assert mod._default_out("json") == "facebook_pages.json"
        a = mod.build_arg_parser().parse_args(["--url", "nike"])
        assert (a.max_solves, a.delay_between_pages, a.retries, a.max_results, a.posts) == (8, 2.0, 2, 100, 0), mod.__name__


@check("every top-level module is in the Dockerfile COPY and pyproject py-modules (a module left out breaks the image on every run — CLAUDE.md §16)")
def _():
    if not (ROOT / "Dockerfile").exists() and not (ROOT / "pyproject.toml").exists():
        return  # the Docker image's own copy of this suite ships neither (CLAUDE.md §22)
    modules = sorted(pth.stem for pth in ROOT.glob("*.py"))
    docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    listed = set(re.findall(r'"([a-z_]+)"', pyproject.split("py-modules", 1)[1].split("]", 1)[0]))
    for mod in modules:
        assert f"{mod}.py" in docker, f"{mod}.py missing from the Dockerfile COPY"
        assert mod in listed, f"{mod} missing from pyproject py-modules"
    assert listed <= set(modules), f"pyproject lists modules that do not exist: {sorted(listed - set(modules))}"


@check("BOT_CHALLENGE_MARKERS is empty, and the generic markers match NONE of the real captures (CLAUDE.md §18: count a marker on a good page first)")
def _():
    assert pp.BOT_CHALLENGE_MARKERS == (), "no captcha vendor appeared on any Page capture"
    for name in _PAGES:
        html = _fx(name)
        assert not captcha_solver.detect_from_html(html, pp.BOT_CHALLENGE_MARKERS), f"{name}: a generic marker fires on a real Page"
        assert not page_flow.is_challenge(html), name


# --------------------------------------------------------------------------- #
# output_writer — exit codes / precedence / dedupe (CLAUDE.md §9)
# --------------------------------------------------------------------------- #
@check("exit codes and STATUS_BY_EXIT match the family contract exactly")
def _():
    expected = {0: "complete", 1: "crashed", 2: "bad_usage", 3: "blocked", 4: "empty", 5: "remote_api_error", 6: "partial"}
    assert output_writer.STATUS_BY_EXIT == expected


def _mk_product(sku, price=None, **kw):
    defaults = dict(
        sku=sku, source="facebook.com", category="page", title="An example Page",
        brand=None, price=price, currency=None, price_source=None,
        product_url=f"https://www.facebook.com/{sku}",
        image_url=None, scraped_at="2026-10-06T00:00:00Z",
        page_id=sku, username=sku, followers=1000,
    )
    defaults.update(kw)
    return output_writer.Product(**defaults)


@check("finish_run: rows gathered by a run that did not finish are PARTIAL (6) with the cause in stop_reason — never 5/3 with a file (CLAUDE.md §25; audit 2026-09-30 got exit 5 AND a written file). Rewrites the old pinned 'exit 5 with products' position deliberately.")
def _():
    cases = (
        (dict(blocked=True, remote_api_error=True), "remote_api_error"),
        (dict(blocked=False, remote_api_error=True), "remote_api_error"),
        (dict(blocked=True, remote_api_error=False), "blocked"),
        (dict(blocked=False, remote_api_error=False, failed_pages=[3]), "failed_pages"),
        (dict(blocked=False, remote_api_error=False, rejected_rows=2), "rejected_rows"),
    )
    for kw, reason in cases:
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            kw = {"failed_pages": None, **kw}
            code = output_writer.finish_run(
                products=[_mk_product("1")], out_path=out, fmt="json", engine="test", url="u",
                pages_requested=3, pages_completed=2, allow_empty=False, started_at=0.0, **kw,
            )
            assert code == output_writer.EXIT_PARTIAL, (kw, code)
            assert Path(out).exists(), "already-collected products must still be written out"
            meta = json.loads(Path(f"{out}.meta.json").read_text())
            assert meta["status"] == "partial" and meta["stop_reason"] == reason, (kw, meta)
            if reason == "rejected_rows":
                assert meta["rejected_rows"] == 2
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "z.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=0,
            failed_pages=None, blocked=False, remote_api_error=True, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_REMOTE_API_ERROR and not Path(out).exists(), "5 promises no file"

@check("finish_run precedence: blocked+zero-products respects --allow-empty for WHETHER to write, never for the STATUS")
def _():
    with tempfile.TemporaryDirectory() as td:
        out_a = str(Path(td) / "a.json")
        code = output_writer.finish_run(
            products=[], out_path=out_a, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=True, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert Path(out_a).exists(), "--allow-empty means a zero-product outcome DOES get written"
        meta = json.loads(Path(f"{out_a}.meta.json").read_text())
        assert meta["status"] == "blocked", "--allow-empty must never launder this into 'complete'"

        out_b = str(Path(td) / "b.json")
        code = output_writer.finish_run(
            products=[], out_path=out_b, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=[2],
            blocked=True, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_BLOCKED
        assert not Path(out_b).exists(), "without --allow-empty, a zero-product outcome writes nothing"


@check("finish_run: zero products without --allow-empty writes neither file nor sidecar")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_ZERO_PRODUCTS
        assert not Path(out).exists()
        assert not Path(f"{out}.meta.json").exists()


@check("finish_run: partial (failed pages, some products) writes output and reports EXIT_PARTIAL")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_PARTIAL
        assert Path(out).exists()
        meta = json.loads(Path(f"{out}.meta.json").read_text())
        assert meta["status"] == "partial"


@check("finish_run: a clean run with products writes output and reports EXIT_OK/complete")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.json")
        code = output_writer.finish_run(
            products=[_mk_product("a"), _mk_product("b")], out_path=out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        assert code == output_writer.EXIT_OK
        data = json.loads(Path(out).read_text())
        assert len(data) == 2


@check("merge_pages dedupes by sku, last-write-wins, in fetch order not arrival order")
def _():
    batch1 = [_mk_product("a", title="A v1"), _mk_product("b", title="B")]
    batch2 = [_mk_product("a", title="A v2"), _mk_product("c", title="C")]  # "a" edited between runs
    merged = output_writer.merge_pages([batch1, batch2])
    skus = [p.sku for p in merged]
    assert skus == ["a", "b", "c"], f"expected batch-order with new items appended, got {skus}"
    a = next(p for p in merged if p.sku == "a")
    assert a.title == "A v2", "later batch's value must win for a repeated sku"


@check("write_csv writes a header even for zero rows")
def _():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "out.csv")
        output_writer.write_csv([], out)
        text = Path(out).read_text()
        assert text.strip() != ""
        assert "sku" in text.splitlines()[0]


# --------------------------------------------------------------------------- #
# proxy_pool — parsing, redaction, dead-marking (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("proxy_pool rejects a malformed proxy string with ProxyParseError")
def _():
    try:
        proxy_pool.load_proxies("not a proxy!!", None)
        raise AssertionError("expected ProxyParseError")
    except proxy_pool.ProxyParseError:
        pass


@check("proxy_pool parses a credentialed proxy and masks it in logs")
def _():
    proxies = proxy_pool.load_proxies("http://user:secretpass@host.example:8080", None)
    assert len(proxies) == 1
    p = proxies[0]
    assert p.has_auth
    masked = p.masked()
    assert "secretpass" not in masked
    assert "host.example" in masked


@check("proxy_pool.redact_credentials strips login:password out of an arbitrary string")
def _():
    raw = "connect failed: ws://myuser:mysecret@cb.2captcha.com:9222 (5 attempts)"
    redacted = proxy_pool.redact_credentials(raw)
    assert "mysecret" not in redacted
    assert "myuser" not in redacted


# --------------------------------------------------------------------------- #
# captcha_solver — generic + widget-specific detection (family-shared)
# --------------------------------------------------------------------------- #
@check("captcha_solver.detect_from_html finds generic bot-challenge markers")
def _():
    assert captcha_solver.detect_from_html("<html>please complete the g-recaptcha below</html>")
    assert not captcha_solver.detect_from_html("<html><body>ordinary page, no widgets</body></html>")


@check("captcha_solver.identify_widget extracts a Turnstile sitekey")
def _():
    html = '<div class="cf-turnstile" data-sitekey="0x4AAA_example"></div>'
    signal = captcha_solver.identify_widget(html)
    assert signal is not None
    assert signal.captcha_type == captcha_solver.CaptchaType.CLOUDFLARE_TURNSTILE
    assert signal.sitekey == "0x4AAA_example"


# --------------------------------------------------------------------------- #
# env_config — FACEBOOK_* keys, placeholder detection, precedence
# --------------------------------------------------------------------------- #
@check("env_config.ENV_KEYS matches .env.example exactly, in both directions")
def _():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    documented = {line.split("=", 1)[0] for line in example.splitlines() if "=" in line and not line.startswith("#")}
    assert documented == set(env_config.ENV_KEYS), (documented, set(env_config.ENV_KEYS))


@check("env_config uses FACEBOOK_ prefixed keys, not a leftover key of the repo this was bootstrapped from")
def _():
    for key in env_config.ENV_KEYS:
        assert key == "TWOCAPTCHA_KEY" or key.startswith("FACEBOOK_"), f"unexpected env key {key!r}"


@check("env_config._is_placeholder treats a braced {...} fragment as unset")
def _():
    assert env_config._is_placeholder("")
    assert env_config._is_placeholder(None)
    assert env_config._is_placeholder("{login}-zone-scraping_browser:{password}@cb.2captcha.com")
    assert not env_config._is_placeholder("a-real-looking-value-123")


@check("env_config.apply_env never overrides an explicitly-set CLI flag")
def _():
    import os as _os
    ns = __import__("argparse").Namespace(proxy="http://explicit:pass@host:1")
    _os.environ["FACEBOOK_PROXY"] = "http://from-env:pass@host:2"
    try:
        env_config.apply_env(ns, dotenv_path="/nonexistent/.env")
        assert ns.proxy == "http://explicit:pass@host:1"
    finally:
        del _os.environ["FACEBOOK_PROXY"]


# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# page_parser — inputs
# --------------------------------------------------------------------------- #
@check("normalize_input: a Page URL (any facebook.com host, with or without scheme, a trailing tab such as /about, a query), a bare username, @username and a numeric id become ONE canonical address; facebook.com's own routes, groups, posts, other hosts and credentials in the URL are refused")
def _():
    for text in ("nike", "@nike", "https://www.facebook.com/nike", "facebook.com/nike/", "https://m.facebook.com/nike?ref=x",
                 "https://web.facebook.com/nike/about", "https://www.facebook.com/pg/nike/posts/", "fb.com/nike"):
        assert pp.normalize_input(text) == _NIKE_URL, text
    assert pp.normalize_input("15087023444") == "https://www.facebook.com/15087023444"
    assert pp.normalize_input("https://www.facebook.com/profile.php?id=100044541544829&sk=about") == \
        "https://www.facebook.com/profile.php?id=100044541544829"
    assert pp.normalize_input("https://www.facebook.com/people/Some-Name/100044541544829/") == "https://www.facebook.com/100044541544829"
    for bad in ("https://www.facebook.com/groups/123", "https://www.facebook.com/ads/library/?q=nike", "https://www.facebook.com/marketplace",
                "https://www.facebook.com/", "https://www.facebook.com/profile.php", "https://www.facebook.com/watch/?v=1",
                "https://example.com/nike", "https://user:pw@www.facebook.com/nike", "https://www.facebook.com:8080/nike",
                "", "not a page!", "https://www.facebook.com/story.php?story_fbid=1", "login"):
        assert pp.normalize_input(bad) is None, bad
    assert "not a facebook.com" in pp.refusal_reason("https://example.com/nike")
    assert pp.username_from_url(_NIKE_URL) == "nike" and pp.username_from_url("https://www.facebook.com/15087023444") is None


# --------------------------------------------------------------------------- #
# page_parser — the REAL captures
# --------------------------------------------------------------------------- #
@check("page_state on every REAL capture: a Page = content; the no-Page answer (CometErrorRoute, HTTP 200!) = not_found, never a block; the Page route without a header = loading; a login redirect = login; Chromium's own error page = unknown")
def _():
    for name in ("nike", "bbcnews", "spiegel", "mcdonalds"):
        assert pp.page_state(_fx(name)) == "content", name
        assert pp.page_route(_fx(name)) == pp.ROUTE_PAGE, name
    assert pp.page_state(_fx("not_found")) == "not_found" and pp.page_route(_fx("not_found")) == pp.ROUTE_ERROR
    shell = '<script type="application/json">{"canonicalRouteName":"%s"}</script>' % pp.ROUTE_PAGE
    assert pp.page_state(shell) == "loading"
    assert pp.page_state("<html><title>Log in</title></html>", final_url="https://www.facebook.com/login/?next=x") == "login"
    assert pp.page_state(_CHROMIUM_ERROR) == "unknown"
    assert pp.page_title(_fx("nike")) == "Nike | Facebook"


def _page(name):
    return pp.page_row(_fx(name), scraped_at="2026-10-06T00:00:00Z")


@check("page_row on the REAL Nike capture: values, not coverage (CLAUDE.md §10) — keyed on the Page id, rounded followers kept as text AND as a number, the intro card read by item TYPE, the website out of Facebook's l.php redirect")
def _():
    r = _page("nike")
    assert (r.sku, r.source, r.category, r.title, r.product_url) == (
        "facebook-page-15087023444", "facebook.com", "page", "Nike", _NIKE_URL)
    assert (r.brand, r.price, r.currency, r.price_source) == (None, None, None, None)
    assert (r.page_id, r.profile_id, r.username) == ("15087023444", "100044541544829", "nike")
    assert (r.page_category, r.description, r.is_verified) == ("Sportswear Store", "Just Do It.", True)
    assert (r.followers, r.followers_text, r.following, r.likes) == (39_000_000, "39M followers", 24, None)
    assert (r.website, r.phone, r.owner) == ("http://nike.com/", "+972 54-292-7777", "NIKE, Inc.")
    assert (r.email, r.address, r.hours, r.recommend_pct) == (None, None, None, None)
    assert r.image_url.startswith("https://scontent") and r.cover_url.startswith("https://scontent")


@check("page_row on the REAL BBC News, DER SPIEGEL and McDonald's captures: e-mail, street address, hours, a German Page with every contact item, a restaurant's rating ('32% recommend (37,633 reviews)') and price range")
def _():
    bbc = _page("bbcnews")
    assert (bbc.page_id, bbc.followers, bbc.following) == ("228735667216", 62_000_000, 49)
    assert (bbc.email, bbc.address, bbc.hours) == ("yourvoice@bbc.co.uk", "BBC Broadcasting House, London, United Kingdom", "Always open")
    assert bbc.website == "https://www.bbc.co.uk/news" and bbc.owner == "British Broadcasting Corporation"
    sp = _page("spiegel")
    assert (sp.followers, sp.followers_text, sp.page_category) == (2_200_000, "2.2M followers", "News & media website")
    assert (sp.phone, sp.email, sp.address) == ("+49 40 380800", "spiegel@spiegel.de", "Ericusspitze 1, Hamburg, Germany")
    assert sp.description == "DER SPIEGEL steht für unabhängigen investigativen Journalismus."
    mc = _page("mcdonalds")
    assert (mc.recommend_pct, mc.review_count, mc.price_range, mc.phone) == (32, 37633, "$", "+1 417-732-8787")
    assert mc.following is None and mc.followers == 82_000_000 and mc.username == "McDonalds"


@check("rounded_count: the site's rounding is read, never invented — K/M/B, decimals and thousands separators; anything else is None")
def _():
    for text, n in (("39M followers", 39_000_000), ("2.2M followers", 2_200_000), ("474K followers", 474_000),
                    ("138 followers", 138), ("1,234 followers", 1234), ("24 following", 24), ("1.5B followers", 1_500_000_000),
                    ("3.1K likes", 3100), ("1 follower", 1)):
        assert pp.rounded_count(text) == n, text
    for text in (None, "", "followers", "about 3M people"):
        assert pp.rounded_count(text) is None, text


@check("posts on the REAL Nike capture: one embedded in the page, two in the timeline response, in the site's order; the EXACT reactions, comments, shares and video views (cross-checked against the displayed count on all 67 captured posts)")
def _():
    page = _page("nike")
    stories = pp.stories(pp.json_blocks(_fx("nike")) + pp.graphql_payloads((_FIX / "fb_page_nike_20261006_feed.txt").read_text()))
    rows = [pp.post_row(s, page=page, position=i, scraped_at="x") for i, s in enumerate(stories, 1)]
    assert [r.post_id for r in rows] == _NIKE_POSTS
    r = rows[0]
    assert (r.sku, r.category, r.page_id, r.username, r.position) == ("facebook-post-1393461115481927", "post", "15087023444", "nike", 1)
    assert (r.posted_at, r.reactions, r.comments, r.shares, r.video_views) == ("2025-09-15T16:01:59Z", 8196, 2644, 1156, 495894)
    assert r.title == r.text == "Leave your limits at the surface. #JustDoIt" and r.product_url == "https://www.facebook.com/reel/2166091230582141/"
    assert (rows[1].reactions, rows[1].comments, rows[1].shares) == (5538, 854, 645)
    bbc = pp.stories(pp.json_blocks(_fx("bbcnews")))
    b = pp.post_row(bbc[0], page=_page("bbcnews"), position=1, scraped_at="x")
    assert b.video_views is None and b.title.endswith("…") and len(b.title) == 120 and len(b.text) > 120


@check("a post's counts are never a COMMENT's: the search for reactions/shares/views skips every comment subtree (a post carries its comments' reaction counts too)")
def _():
    story = {"post_id": "1", "comet_sections": {}, "feedback": {
        "interesting_top_level_comments": [{"comment": {"feedback": {"reaction_count": {"count": 999}}}}],
        "reaction_count": {"count": 7}, "share_count": {"count": 2}}}
    r = pp.post_row(story, page=_page("nike"), position=1, scraped_at="x")
    assert (r.reactions, r.shares) == (7, 2), (r.reactions, r.shares)
    del story["feedback"]["reaction_count"]
    assert pp.post_row(story, page=_page("nike"), position=1, scraped_at="x").reactions is None, "no count rather than a comment's"


@check("a profile with no Page behind it (a private person's) raises NotAPage and is never a row; a page with no header is a ValueError")
def _():
    html = _fx("nike").replace('"delegate_page":{"is_business_page_active":false,"id":"15087023444"}', '"delegate_page":null')
    assert '"delegate_page":null' in html, "the fixture's own delegate_page shape changed"
    try:
        pp.page_row(html)
        raise AssertionError("a personal profile became a row")
    except pp.NotAPage:
        pass
    try:
        pp.page_row(_fx("not_found"))
        raise AssertionError("a no-Page answer became a row")
    except ValueError:
        pass


@check("likes_from_ad_library on the REAL Ad Library captures: the EXACT like count from this Page's own ads (Nike 39,506,218 — the Page shows '39M'), never a branded-content ad's OTHER Page (1,478 on the same search); a Page with no ads is empty, the bare shell is loading")
def _():
    nike = (_FIX / "fb_page_adlibrary_nike_20261006.html").read_text(encoding="utf-8")
    assert pp.likes_from_ad_library(nike, "15087023444") == ("content", 39506218)
    assert "1478" in nike and pp.likes_from_ad_library(nike, "999") == ("loading", None), "another id never takes Nike's count"
    noads = (_FIX / "fb_page_adlibrary_noads_20261006.html").read_text(encoding="utf-8")
    assert pp.likes_from_ad_library(noads, "358173637964053") == ("empty", None)
    assert pp.likes_from_ad_library('<script type="application/json">{"x":"AdLibraryFoundationRoot"}</script>', "1") == ("loading", None)
    assert pp.likes_from_ad_library(_CHROMIUM_ERROR, "1") == ("unknown", None)
    u = pp.ad_library_url("15087023444")
    assert "view_all_page_id=15087023444" in u and "country=ALL" in u and "active_status=all" in u


@check("Product field order: family-common fields first, Page and post fields after")
def _():
    expected_head = ["sku", "source", "category", "title", "brand", "price", "currency",
                     "price_source", "product_url", "image_url", "scraped_at"]
    assert output_writer.PRODUCT_FIELD_NAMES[: len(expected_head)] == expected_head
    tail = output_writer.PRODUCT_FIELD_NAMES[len(expected_head):]
    for name in ("page_id", "profile_id", "username", "page_category", "description", "is_verified", "followers", "likes_exact",
                 "followers_text", "following", "likes", "website", "phone", "email", "address", "hours", "price_range",
                 "recommend_pct", "review_count", "owner", "other_accounts_json", "cover_url", "post_id", "posted_at",
                 "text", "reactions", "comments", "shares", "video_views", "position"):
        assert name in tail, f"{name} missing from Product's site-specific tail"


@check("fixtures are scrubbed and stay scrubbed: no session token, no comment or comment author, no link-shim signature — guarded by PATTERN (CLAUDE.md §10)")
def _():
    token = re.compile(r'["\']?(?:fb_dtsg|lsd|jazoest|__hsi|DTSGInitialData)["\']?\s*[:=]\s*["{]')
    people = re.compile(r'"(?:author|commenter|interesting_top_level_comments|comment_list_renderer|actors)"')
    shim = re.compile(r"[?&]h=(?!REDACTED)[A-Za-z0-9_-]{8,}")
    files = sorted(_FIX.glob("*"))
    assert len(files) == 9, [f.name for f in files]
    for path in files:
        text = path.read_text(encoding="utf-8")
        for name, pattern in (("token", token), ("people", people), ("link-shim signature", shim)):
            m = pattern.search(text)
            assert m is None, f"{path.name}: {name} {m.group(0)[:60]!r}"


# --------------------------------------------------------------------------- #
# CLI validation — bad usage never crashes, never writes output
# --------------------------------------------------------------------------- #
def _urls_file(lines):
    f = Path(tempfile.mkdtemp()) / "urls.txt"
    f.write_text("\n".join(lines), encoding="utf-8")
    return f


@check("each engine resolves --url / --urls-file into canonical Page addresses; a non-Page line is skipped (never fetched) with its reason; duplicates collapse; --posts is 0-3")
def _():
    for mod in _ENGINES:
        args = mod.build_arg_parser().parse_args(["--url", "@nike", "--posts", "3"])
        assert mod._resolve_urls(args) == ([_NIKE_URL], 0) and args.posts == 3, mod.__name__
        args = mod.build_arg_parser().parse_args(["--urls-file", str(_urls_file([
            "# a comment", "nike", "https://www.facebook.com/nike/", "https://www.facebook.com/groups/1", "bbcnews"]))])
        assert mod._resolve_urls(args) == ([_NIKE_URL, "https://www.facebook.com/bbcnews"], 1), mod.__name__
        for bad in ("4", "-1"):
            try:
                mod.build_arg_parser().parse_args(["--url", "nike", "--posts", bad])
                raise AssertionError(f"{mod.__name__}: --posts {bad} accepted")
            except SystemExit:
                pass


@check("each engine: no input, an input with nothing usable, and an --out directory that does not exist are all EXIT_BAD_USAGE before anything launches, and write nothing")
def _():
    for mod in _ENGINES:
        for argv in ([], ["--url", "https://www.facebook.com/groups/1"], ["--url", "nike", "--out", "/nonexistent_dir_8812/o.json"]):
            with tempfile.TemporaryDirectory() as td:
                out = str(Path(td) / "out.json")
                args = mod.build_arg_parser().parse_args(["--out", out, *argv])  # a later --out in argv wins
                code = asyncio_run_maybe(mod, args)
                assert code == output_writer.EXIT_BAD_USAGE, (mod.__name__, argv, code)
                assert not Path(out).exists()


@check("each engine: a malformed --proxy is EXIT_BAD_USAGE, not a crash, and writes nothing")
def _():
    _IMPORT_ERROR_ATTR = {
        "playwright_scraper": "_PLAYWRIGHT_IMPORT_ERROR",
        "selenium_scraper": "_SELENIUM_IMPORT_ERROR",
        "puppeteer_scraper": "_PYPPETEER_IMPORT_ERROR",
    }
    for mod in _ENGINES:
        if getattr(mod, _IMPORT_ERROR_ATTR[mod.__name__], None) is not None:
            continue
        with tempfile.TemporaryDirectory() as td:
            out = str(Path(td) / "out.json")
            args = mod.build_arg_parser().parse_args(["--url", "nike", "--proxy", "not a proxy!!", "--out", out])
            assert asyncio_run_maybe(mod, args) == output_writer.EXIT_BAD_USAGE, mod.__name__
            assert not Path(out).exists()


@check("selenium_scraper refuses a credentialed --cdp-endpoint with EXIT_BAD_USAGE")
def _():
    args = selenium_scraper.build_arg_parser().parse_args(["--url", "nike", "--cdp-endpoint", "ws://user:pass@cb.2captcha.com:9222"])
    assert selenium_scraper.run(args) == output_writer.EXIT_BAD_USAGE


@check("each engine rejects --max-results 0 at the argparse level")
def _():
    for mod in _ENGINES:
        try:
            mod.build_arg_parser().parse_args(["--url", "nike", "--max-results", "0"])
            raise AssertionError(f"{mod.__name__}: expected argparse to reject --max-results 0")
        except SystemExit:
            pass


# --------------------------------------------------------------------------- #
# diff_runs / scraper_api_client — sanity only (family-shared, no site knowledge)
# --------------------------------------------------------------------------- #
@check("diff_runs reports added/removed/changed between two real finish_run() outputs")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("b", title="B")],
            out_path=old_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a", title="A v1"), _mk_product("c", title="C")],
            out_path=new_out, fmt="json", engine="test", url="u", pages_requested=1, pages_completed=1,
            failed_pages=None, blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        result = diff_runs.diff(old_out, new_out)
        assert result["added"] == ["c"]
        assert result["removed"] == [] and result["left_selection"] == ["b"], "a Page is removed only when its address is gone"


@check("diff_runs refuses to compare a non-'complete' run")
def _():
    with tempfile.TemporaryDirectory() as td:
        old_out, new_out = str(Path(td) / "old.json"), str(Path(td) / "new.json")
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=old_out, fmt="json", engine="test", url="u",
            pages_requested=2, pages_completed=1, failed_pages=[2],
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        output_writer.finish_run(
            products=[_mk_product("a")], out_path=new_out, fmt="json", engine="test", url="u",
            pages_requested=1, pages_completed=1, failed_pages=None,
            blocked=False, remote_api_error=False, allow_empty=False, started_at=0.0,
        )
        try:
            diff_runs.diff(old_out, new_out)
            raise AssertionError("expected a refusal — old run is 'partial', not 'complete'")
        except SystemExit:
            pass


@check("scraper_api_client.TwoCaptchaClient._require_key rejects a missing/empty key")
def _():
    client = scraper_api_client.TwoCaptchaClient("")
    try:
        client._require_key()
        raise AssertionError("expected TwoCaptchaAuthError")
    except scraper_api_client.TwoCaptchaAuthError:
        pass


@check("scraper_api_client honors --captcha-api override, not the module-level API_BASE")
def _():
    client = scraper_api_client.TwoCaptchaClient("fakekey", api_base="https://mock.example.test")
    assert client.api_base == "https://mock.example.test"
    assert client.api_base != scraper_api_client.API_BASE


def _diff_run(td, name, rows, url, **kw):
    out = str(Path(td) / name)
    kw.setdefault("allow_empty", False)
    output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=url, pages_requested=1,
                             pages_completed=1, failed_pages=None, blocked=False, remote_api_error=False,
                             started_at=0.0, **kw)
    return out


@check("diff_runs refuses different selections, never calls a currency switch a price change (even at the same number), reads a capped top-N's missing SKU as left_selection, and rejects a sidecar that does not describe its file (audit 2026-09-30)")
def _():
    dress, jeans = "https://us.shein.com/pdsearch/dress/", "https://us.shein.com/pdsearch/jeans/"
    with tempfile.TemporaryDirectory() as td:
        a = _diff_run(td, "a.json", [_mk_product("s1", 9.93, currency="USD")], dress)
        b = _diff_run(td, "b.json", [_mk_product("s1", 19.93, currency="EUR")], jeans)
        try:
            diff_runs.diff(a, b)
            raise AssertionError("different selections must be refused")
        except SystemExit as exc:
            assert "different selections" in str(exc)
        r = diff_runs.diff(a, b, allow_different_scope=True)
        assert not r["changed"] and len(r["currency_changed"]) == 1

        c = _diff_run(td, "c.json", [_mk_product("s1", 10.0, currency="USD")], dress)
        e = _diff_run(td, "e.json", [_mk_product("s1", 10.0, currency="EUR")], dress + "?")
        r = diff_runs.diff(c, e)
        assert r["currency_changed"] and not r["changed"], "same number, other currency must still be reported"

        f = _diff_run(td, "f.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=2)
        g = _diff_run(td, "g.json", [_mk_product("s1"), _mk_product("s3")], dress, max_results=2)
        r = diff_runs.diff(f, g)
        assert r["capped"] and r["left_selection"] == ["s2"] and r["removed"] == [] and r["added"] == ["s3"]
        h = _diff_run(td, "h.json", [_mk_product("s1"), _mk_product("s2")], dress, max_results=50)
        i = _diff_run(td, "i.json", [_mk_product("s1")], dress, max_results=50)
        r = diff_runs.diff(h, i)
        assert r["removed"] == [] and r["left_selection"] == ["s2"] and not r["capped"], \
            "a missing row whose address the new run did not find gone is left_selection, not removed (Pages)"

        Path(g).write_text("[]", encoding="utf-8")
        try:
            diff_runs.diff(f, g)
            raise AssertionError("a sidecar whose hash does not match must be refused")
        except SystemExit as exc:
            assert "output_sha256" in str(exc)

@check("the credential scanner FINDS a planted key in every shape seen in the family (JSON-quoted, JSON-escaped, 32-hex next to a key word) and ignores placeholders, type hints and Python-name mappings — a scanner that cannot fail is not one (CLAUDE.md §24/§25)")
def _():
    import importlib.util
    scanner = ROOT / ".github" / "ci_checks.py"
    if not (ROOT / ".github").is_dir():
        return
    spec = importlib.util.spec_from_file_location("ci_checks_planted", scanner)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fake32 = "0123456789abcdef" * 2
    for planted in ('"api_key": "a8f3k2m9q7x1z5b4"', '{\\"api_key\\": \\"a8f3k2m9q7x1z5b4\\"}',
                    "TWOCAPTCHA_KEY=" + fake32, '"clientKey":"' + fake32 + '"'):
        assert mod.scan_text("planted.txt", planted), f"scanner missed a planted credential: {planted!r}"
    for harmless in ("api_key: Optional[str] = None", "TWOCAPTCHA_KEY=your-key-here", '"TWOCAPTCHA_KEY": "twocaptcha_key",'):
        assert not mod.scan_text("ok.txt", harmless), f"false positive: {harmless!r}"

@check("no workflow imports a local module inline — tests.yml calls ci_checks.py instead (CLAUDE.md §26: an inline heredoc import is red only on the first push)")
def _():
    import re as _re
    if not (ROOT / ".github").is_dir():
        return  # the Docker image ships no .github/ (CLAUDE.md §22)
    local = {p.stem for p in ROOT.glob("*.py")}
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        for m in _re.finditer(r"^\s*(?:from\s+([A-Za-z_]\w*)\s+import|import\s+([A-Za-z_]\w*))", text, _re.M):
            name = m.group(1) or m.group(2)
            assert name not in local, f"{wf.name}: imports local module {name!r} inline"
    assert "ci_checks.py --sample-check" in (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")

@check(".gitignore covers every artefact a run writes (CLAUDE.md §22/§26): .env copies, --dump-html challenge screenshots, *.pageN dumps, live/ — while sample outputs, fixtures and .env.example stay tracked")
def _():
    import subprocess as _sp
    if not (ROOT / ".git").exists():
        return
    must_ignore = [".env", ".env.bak", ".env.local", "facebook_pages_debug_1.html",
                   "out.json.page3", "live/x.html", "facebook_pages.json", "run.json"]
    must_keep = [".env.example", "sample_output.json", "sample_output.csv", "tests/fixtures/fb_page_nike_20261006.html",
                 "tests/fixtures/fb_page_nike_20261006_feed.txt"]
    for path in must_ignore:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode == 0, f"not ignored: {path}"
    for path in must_keep:
        assert _sp.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode != 0, f"wrongly ignored: {path}"


@check("diff_runs on Pages: watched Page and post fields with deltas; a post missing from the new run is left_selection (pushed out of the three shown), a Page is removed only when the new run found its address gone")
def _():
    with tempfile.TemporaryDirectory() as td:
        def run_(name, rows, **meta):
            out = str(Path(td) / name)
            output_writer.finish_run(products=rows, out_path=out, fmt="json", engine="t", url=_NIKE_URL, pages_requested=2,
                                     pages_completed=2, failed_pages=None, blocked=False, remote_api_error=False,
                                     allow_empty=False, started_at=0.0, max_results=100, extra_meta={"selection": {"x": 1}, **meta})
            return out
        a = run_("a.json", [_mk_product("nike", followers=39_000_000), _mk_product("gone"),
                            _mk_product("p1", category="post", reactions=10, comments=1)])
        b = run_("b.json", [_mk_product("nike", followers=40_000_000), _mk_product("p2", category="post")],
                 not_found_urls=["https://www.facebook.com/gone"])
        r = diff_runs.diff(a, b)
        assert r["removed"] == ["gone"] and r["left_selection"] == ["p1"] and r["added"] == ["p2"], r
        assert r["field_changes"] == [{"sku": "nike", "fields": {"followers": {"old": 39_000_000, "new": 40_000_000, "delta": 1_000_000}}}]


# --------------------------------------------------------------------------- #
# page_flow — the ONE fetch loop (CLAUDE.md §26), driven with a fake engine
# --------------------------------------------------------------------------- #
_SESSION = {"playwright_scraper": "_PlaywrightSession", "selenium_scraper": "_SeleniumSession", "puppeteer_scraper": "_PyppeteerSession"}
_ENGINE = {"playwright_scraper": "_PlaywrightEngine", "selenium_scraper": "_SeleniumEngine", "puppeteer_scraper": "_PyppeteerEngine"}


@check("the fetch loop exists ONCE: no engine parses or finishes a run itself, each calls page_flow.run and page_flow.resolve_urls; the CDP connect goes through connect_with_retry; pyppeteer disconnects instead of closing the remote browser")
def _():
    for mod in _ENGINES:
        src = (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
        for fragment in ("finish_run(", "page_row(", "post_row(", "stories(", "page_state("):
            assert fragment not in src, f"{mod.__name__}: loop logic {fragment!r} outside page_flow"
        assert src.count("page_flow.run(") == 1 and mod._resolve_urls is page_flow.resolve_urls, mod.__name__
        assert "pp.is_feed_response(" in src, f"{mod.__name__}: GraphQL bodies must be filtered by the shared rule"
    for mod in (playwright_scraper, puppeteer_scraper):
        assert "scraper_api_client.connect_with_retry(" in (ROOT / f"{mod.__name__}.py").read_text(encoding="utf-8")
    assert "reuse_default and browser.contexts" in (ROOT / "playwright_scraper.py").read_text(encoding="utf-8")
    pup = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "_bounded(browser.disconnect()" in pup and "_release(remote_browser, remote=True)" in pup


@check("§26 ops set, derived from page_flow's AST (every session.<op> / engine.<op> the loop uses): each engine's session and engine class provides all of them, and so does the Scraper API engine")
def _():
    import ast as _ast
    import scraper_api_engine
    tree = _ast.parse((ROOT / "page_flow.py").read_text(encoding="utf-8"))
    ops = {"session": set(), "engine": set()}
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Attribute) and isinstance(node.value, _ast.Name) and node.value.id in ops:
            ops[node.value.id].add(node.attr)
    assert {"goto", "content", "current_url", "wait", "take_responses", "close"} <= ops["session"], ops
    assert {"open", "sleep", "solve_captcha", "readiness_s", "name"} <= ops["engine"], ops
    pairs = [(getattr(m, _SESSION[m.__name__]), getattr(m, _ENGINE[m.__name__]), m.__name__) for m in _ENGINES]
    pairs.append((scraper_api_engine._ScraperApiSession, scraper_api_engine.ScraperApiEngine, "scraper_api_engine"))
    for sc, ec, name in pairs:
        assert not [o for o in ops["session"] if not hasattr(sc, o)], name
        assert not [o for o in ops["engine"] if not hasattr(ec, o)], name


def _canonical(url):
    """The address page_flow opened, minus the locale it adds (pp.fetch_url)."""
    assert url.endswith("locale=en_US"), f"opened without locale=en_US: {url}"
    return url[: -len("locale=en_US") - 1]


class _FakeSession:
    """Answers one Page address the way the site does: `pages` is what
    content() returns over time (each wait() moves one step on, so a shell
    can paint); `feed` is the timeline body, available from `feed_after`
    waits on (the live page's request leaves on load and lands seconds
    later)."""

    def __init__(self, engine, script):
        self.engine, self.script, self.closed = engine, dict(script), False
        self.pages = list(script.get("pages") or [script.get("html", "")])
        self.step, self.feed_taken = 0, False

    async def goto(self, url):
        self.engine.asked.append(url)
        step = self.script.get("goto")
        if isinstance(step, Exception):
            raise step
        return self.script.get("status", 200)

    async def content(self):
        return self.pages[min(self.step, len(self.pages) - 1)]

    async def current_url(self):
        return self.script.get("final_url", self.engine.asked[-1])

    async def wait(self, seconds):
        self.step += 1

    async def take_responses(self):
        if self.script.get("feed") and not self.feed_taken and self.step >= self.script.get("feed_after", 0):
            self.feed_taken = True
            return [self.script["feed"]]
        return []

    async def close(self):
        self.closed = True


class _FakeEngine:
    name = "fake"
    readiness_s = 0

    def __init__(self, by_url, solve=None):
        self.by_url, self.sessions, self.slept, self.asked, self._solve = by_url, [], [], [], solve
        self.fetched = []

    async def open(self, proxy):
        engine = self

        class _S(_FakeSession):
            async def goto(self, url):
                engine.fetched.append(url)
                url = _canonical(url)
                _FakeSession.__init__(self, engine, engine.by_url.get(url, engine.by_url.get("*", {})))
                return await _FakeSession.goto(self, url)
        sess = _S(self, {})
        self.sessions.append(sess)
        return sess

    async def sleep(self, seconds):
        self.slept.append(seconds)

    async def solve_captcha(self, session, *, html, url):
        return self._solve() if self._solve else None


def _pflow(by_url, argv, *, solve=None, proxy_pool=None):
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    engine = _FakeEngine(by_url, solve)
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=proxy_pool, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    assert all(s.closed for s in engine.sessions), "every opened session must be closed"
    return rc, meta, rows, engine




def _nike(**kw):
    return {"html": _fx("nike"), "feed": (_FIX / "fb_page_nike_20261006_feed.txt").read_text(encoding="utf-8"), **kw}


@check("page_flow END TO END on the REAL captures: a Page row, then with --posts its posts in the site's order (the embedded one and the two from the timeline response, waited for — bounded — when it lands late); without --posts nothing waits for the feed")
def _():
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(feed_after=4)}, ["--url", "nike", "--posts", "3"])
    assert rc == output_writer.EXIT_OK and [r["sku"] for r in rows] == ["facebook-page-15087023444"] + [f"facebook-post-{p}" for p in _NIKE_POSTS], rows
    assert [r["position"] for r in rows[1:]] == [1, 2, 3] and rows[3]["reactions"] == 3873
    assert meta["selection"] == {"mode": "pages", "urls": [_NIKE_URL], "posts": 3, "exact_likes": False, "max_results": 100}
    assert meta["pages"] == [{"url": _NIKE_URL, "state": "content", "rows": 4, "posts_shown": 3, "exact_likes": None, "result": "ok"}]
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(feed_after=4)}, ["--url", "nike", "--posts", "1"])
    assert len(rows) == 2 and rows[1]["post_id"] == _NIKE_POSTS[0] and eng.sessions[0].step == 0, "the embedded post needs no wait"
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike()}, ["--url", "nike"])
    assert len(rows) == 1 and eng.sessions[0].step == 0
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(feed_after=10 ** 6)}, ["--url", "nike", "--posts", "3"])
    assert rc == output_writer.EXIT_OK and len(rows) == 2 and meta["pages"][0]["posts_shown"] == 1
    assert eng.sessions[0].step <= page_flow.FEED_WAIT_S + 1, "bounded"


@check("page_flow across addresses: the same Page by username, profile id and Page id is ONE row (keyed on the Page id); a no-Page address is not_found (complete, listed, never retried, never a block); only not-found inputs = exit 4, nothing written; --max-results caps the Pages read and marks the run capped")
def _():
    other = "https://www.facebook.com/profile.php?id=100044541544829"
    gone = "https://www.facebook.com/zzqx-no-such-page-88123"
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(), other: _nike(), gone: {"html": _fx("not_found")}},
                                 ["--urls-file", str(_urls_file(["nike", other, gone]))])
    assert rc == output_writer.EXIT_OK and [r["sku"] for r in rows] == ["facebook-page-15087023444"], rows
    assert meta["not_found_urls"] == [gone] and eng.asked.count(gone) == 1 and meta["status"] == "complete"
    rc, meta, rows, eng = _pflow({"*": {"html": _fx("not_found")}}, ["--url", "zzqx-no-such-page-88123", "--retries", "3"])
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and meta is None and len(eng.asked) == 1
    many = ["nike", "bbcnews", "derspiegel", "cnn"]
    by = {"https://www.facebook.com/bbcnews": {"html": _fx("bbcnews")}, "*": _nike()}
    rc, meta, rows, eng = _pflow(by, ["--urls-file", str(_urls_file(many)), "--max-results", "2"])
    assert len(eng.asked) == 2 and meta["capped"] is True and meta["status"] == "complete" and meta["pages_requested"] == 2, meta
    # measured live: a second address of a Page already read used up the limit and left a real Page out
    rc, meta, rows, eng = _pflow(by, ["--urls-file", str(_urls_file(["nike", other, "bbcnews", "cnn"])), "--max-results", "2"])
    assert [r["title"] for r in rows] == ["Nike", "BBC News"] and len(eng.asked) == 3, [r["title"] for r in rows]


@check("every engine opens the English page: page_flow adds locale=en_US to the address (measured: pyppeteer on a Russian-language machine got '474 тыс. — подписчики' despite --lang=en-US, and the counts went unread); a page served in another language is warned about")
def _():
    assert pp.fetch_url(_NIKE_URL) == _NIKE_URL + "?locale=en_US"
    assert pp.fetch_url("https://www.facebook.com/profile.php?id=1") == "https://www.facebook.com/profile.php?id=1&locale=en_US"
    assert pp.page_language('<html id="facebook" lang="ru" dir="ltr">') == "ru" and pp.page_language("<html>") is None
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike()}, ["--url", "nike"])
    assert eng.fetched == [_NIKE_URL + "?locale=en_US"] and rows[0]["product_url"] == _NIKE_URL


@check("page_flow --exact-likes: one more page per Page, the Ad Library search for its own id; the exact count lands on the Page row; a Page with no ads keeps the row (no_ads); a failed lookup keeps the row (failed) — never fails the run")
def _():
    lib = pp.ad_library_url("15087023444")
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(), lib: {"html": (_FIX / "fb_page_adlibrary_nike_20261006.html").read_text(encoding="utf-8")}},
                                 ["--url", "nike", "--exact-likes"])
    assert rc == output_writer.EXIT_OK and rows[0]["likes_exact"] == 39506218 and rows[0]["followers"] == 39_000_000
    assert [_canonical(u) for u in eng.fetched] == [_NIKE_URL, lib] and meta["pages"][0]["exact_likes"] == "ok"
    assert meta["selection"]["exact_likes"] is True
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(), lib: {"html": (_FIX / "fb_page_adlibrary_noads_20261006.html").read_text(encoding="utf-8")}},
                                 ["--url", "nike", "--exact-likes"])
    assert rc == output_writer.EXIT_OK and rows[0]["likes_exact"] is None and meta["pages"][0]["exact_likes"] == "no_ads"
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(), lib: {"goto": RuntimeError("net::ERR_TIMED_OUT")}}, ["--url", "nike", "--exact-likes"])
    assert rc == output_writer.EXIT_OK and len(rows) == 1 and meta["pages"][0]["exact_likes"] == "failed"
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike()}, ["--url", "nike"])
    assert len(eng.fetched) == 1 and rows[0]["likes_exact"] is None, "without the flag, no extra page"


@check("page_flow on a personal profile: not_a_page — skipped, listed in the sidecar, NOTHING about the person written")
def _():
    person = _fx("nike").replace('"delegate_page":{"is_business_page_active":false,"id":"15087023444"}', '"delegate_page":null')
    rc, meta, rows, eng = _pflow({"*": {"html": person}, _NIKE_URL: _nike()}, ["--urls-file", str(_urls_file(["someone.private", "nike"]))])
    assert rc == output_writer.EXIT_OK and [r["category"] for r in rows] == ["page"] and rows[0]["page_id"] == "15087023444"
    assert meta["not_a_page_urls"] == ["https://www.facebook.com/someone.private"]


@check("page_flow on blocks and failures: a login redirect and a non-facebook page under 403 are blocked (exit 3); without a pool the run STOPS after 3 blocks in a row, the rest not_attempted, rows already read kept as partial (6); a shell that never paints is not_painted; a page that paints late is read; a dead proxy is bounded and exit 5")
def _():
    login = {"html": "<html><title>Log in to Facebook</title></html>", "final_url": "https://www.facebook.com/login/"}
    rc, meta, rows, eng = _pflow({"*": login}, ["--url", "nike"])
    assert rc == output_writer.EXIT_BLOCKED and meta is None
    many = ["p%d" % i for i in range(6)]
    rc, meta, rows, eng = _pflow({"*": {"html": _CHROMIUM_ERROR, "status": 403}}, ["--urls-file", str(_urls_file(many))])
    assert rc == output_writer.EXIT_BLOCKED and len(eng.asked) == page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS
    rc, meta, rows, eng = _pflow({_NIKE_URL: _nike(), "*": {"html": _CHROMIUM_ERROR, "status": 429}},
                                 ["--urls-file", str(_urls_file(["nike"] + many))])
    reasons = [f["reason"] for f in meta["failed_urls"]]
    assert rc == output_writer.EXIT_PARTIAL and len(rows) == 1 and meta["stop_reason"] == "blocked", (rc, meta["stop_reason"])
    assert reasons.count("not_attempted") == 6 - page_flow.STOP_AFTER_CONSECUTIVE_BLOCKS and reasons.count("blocked") == 3
    shell = '<script type="application/json">{"canonicalRouteName":"%s"}</script>' % pp.ROUTE_PAGE
    rc, meta, rows, eng = _pflow({"*": {"html": shell}}, ["--url", "nike", "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["stop_reason"] == "not_painted", meta
    rc, meta, rows, eng = _pflow({"*": {"pages": [shell] * 4 + [_fx("nike")]}}, ["--url", "nike"])
    assert rc == output_writer.EXIT_OK and len(rows) == 1
    rc, meta, rows, eng = _pflow({"*": {"html": "<html><body></body></html>", "status": 407}}, ["--url", "nike", "--allow-empty"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta["failed_urls"][0]["reason"] == "fetch_error", "a proxy's 407 is not a block"
    rc, meta, rows, eng = _pflow({"*": {"goto": RuntimeError("net::ERR_PROXY_CONNECTION_FAILED")}}, ["--url", "nike", "--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and meta is None and len(eng.asked) == 2


@check("page_flow and the paid-solve budget: a page carrying a generic challenge marker (not a facebook.com route) gets at most --max-solves solves across the WHOLE run")
def _():
    challenge = '<html><div class="g-recaptcha" data-sitekey="x"></div></html>'
    solves = []
    rc, meta, rows, eng = _pflow({"*": {"html": challenge, "status": 403}},
                                 ["--urls-file", str(_urls_file(["a1", "a2", "a3"])), "--max-solves", "1"],
                                 solve=lambda: solves.append(1) or {"action": "warning_solver_error"})
    assert len(solves) == 1 and rc == output_writer.EXIT_BLOCKED, (len(solves), rc)
    assert not page_flow.is_challenge(_fx("nike").replace("</body>", '<div class="g-recaptcha"></div></body>')), "facebook.com's own page is never a challenge"


class _FakeScrapeClient:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), []

    def scrape_url(self, url, *, data_format, timeout, cdp_url):
        self.calls.append((_canonical(url), cdp_url))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        status, body = answer
        return scraper_api_client.ScrapeResult(target_status=status, headers={}, body=body)


def _sapi(answers, argv, cdp="ws://u:p@cb.example:9222"):
    import scraper_api_engine
    args = playwright_scraper.build_arg_parser().parse_args([*argv, "--delay-between-pages", "0"])
    args._solve_budget = page_flow.SolveBudget(args.max_solves)
    urls, _skipped = page_flow.resolve_urls(args)
    client = _FakeScrapeClient(answers)
    engine = scraper_api_engine.ScraperApiEngine(client, cdp)
    slept = []

    async def _sleep(seconds):
        if engine.fatal is None:
            slept.append(seconds)
    engine.sleep = _sleep
    with tempfile.TemporaryDirectory() as td:
        args.out = str(Path(td) / "o.json")
        rc = asyncio.run(page_flow.run(engine, args, urls=urls, proxy_pool=None, client=None, started_at=0.0))
        meta_p = Path(args.out + ".meta.json")
        meta = json.loads(meta_p.read_text()) if meta_p.exists() else None
        rows = json.loads(Path(args.out).read_text()) if Path(args.out).exists() else None
    return rc, meta, rows, client, slept


@check("--scraper-api END TO END on the REAL captures (a fake Scraper API client): one call per Page, routed through the CDP profile when set; the Page and its ONE embedded post (the other two need a live page); a no-Page answer is not_found; a refused key is exit 5 with no further calls; a transient error is retried")
def _():
    rc, meta, rows, client, _ = _sapi([(200, _fx("nike"))], ["--url", "nike", "--posts", "3"])
    assert rc == output_writer.EXIT_OK and [r["category"] for r in rows] == ["page", "post"] and meta["engine"] == "scraper_api", rows
    assert client.calls == [(_NIKE_URL, "ws://u:p@cb.example:9222")]
    rc, meta, rows, client, _ = _sapi([(200, _fx("not_found"))], ["--url", "zzqx-no-such-page-88123"], cdp=None)
    assert rc == output_writer.EXIT_ZERO_PRODUCTS and client.calls[0][1] is None
    refused = scraper_api_client.TwoCaptchaAuthError("Scraper API: invalid/missing TWOCAPTCHA_KEY")
    rc, meta, rows, client, slept = _sapi([refused], ["--urls-file", str(_urls_file(["a1", "a2", "a3"]))])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 1 and slept == []
    flaky = scraper_api_client.TwoCaptchaError("Scraper API returned HTTP 502: bad gateway")
    rc, meta, rows, client, _ = _sapi([flaky, (200, _fx("bbcnews"))], ["--url", "bbcnews"])
    assert rc == output_writer.EXIT_OK and len(client.calls) == 2


@check("--scraper-api usage, all three engines, with NO engine driver needed: no key is exit 2 before any call; Selenium does not refuse a credentialed endpoint in this mode")
def _():
    for mod in _ENGINES:
        base = ["--url", "nike", "--scraper-api", "--out", str(Path(tempfile.mkdtemp()) / "o.json")]
        a = mod.build_arg_parser().parse_args(base + ["--cdp-endpoint", "ws://u:p@cb.example:9222"])
        rc = mod.run(a) if mod is selenium_scraper else asyncio.run(mod.run(a))
        assert rc == output_writer.EXIT_BAD_USAGE, (mod.__name__, rc)
    src = (ROOT / "selenium_scraper.py").read_text(encoding="utf-8")
    assert src.index("if args.scraper_api:") < src.index("_cdp_endpoint_has_credentials(args.cdp_endpoint):\n")


@check("pyppeteer answers proxy auth over CDP Fetch, never page.authenticate() — measured 2026-10-04 on a sibling repo: current Chromium has no Network.setRequestInterception, so every proxied URL failed")
def _():
    src = (ROOT / "puppeteer_scraper.py").read_text(encoding="utf-8")
    assert "await page.authenticate(" not in src
    for op in ('"Fetch.enable"', '"Fetch.authRequired"', '"Fetch.continueWithAuth"', '"Fetch.requestPaused"', '"Fetch.continueRequest"'):
        assert op in src, op


@check("fingerprint_client.user_agent_from reads the LIVE response shape (userAgent.userAgent, measured 2026-10-04) and the documented one (userAgent.value); nothing applied when neither is there")
def _():
    import fingerprint_client as fc
    live = {"id": 6472645, "country": "US", "userAgent": {
        "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "fullVersion": "152.0.7977.64", "platform": "Windows", "platformVersion": "10.0.0", "mobile": False}}
    assert fc.user_agent_from(live).startswith("Mozilla/5.0 (Windows NT 10.0"), "the live shape — reading only `value` made --fingerprint a no-op"
    assert fc.user_agent_from({"userAgent": {"value": "UA-doc"}}) == "UA-doc"
    for empty in ({}, {"userAgent": {}}, {"userAgent": {"userAgent": ""}}, None):
        assert fc.user_agent_from(empty) is None, empty
    for path in ("playwright_scraper.py", "puppeteer_scraper.py", "selenium_scraper.py"):
        assert 'log.info("Fingerprint applied: user agent %s", user_agent)' in (ROOT / path).read_text(encoding="utf-8"), path


@check("pyppeteer cleanup is BOUNDED: a page or browser whose close never returns does not hang the run (measured 2026-10-06: 20+ minutes stuck after a proxy dropped the connection); a local Chromium that will not close is killed")
def _():
    class _Never:
        killed = False

        def __init__(self):
            self.process = self

        async def close(self):
            await asyncio.sleep(3600)

        async def disconnect(self):
            await asyncio.sleep(3600)

        def kill(self):
            _Never.killed = True

    old = puppeteer_scraper.CLOSE_TIMEOUT_S
    puppeteer_scraper.CLOSE_TIMEOUT_S = 0.05
    try:
        import time as _time
        t0 = _time.monotonic()
        browser = _Never()
        asyncio.run(puppeteer_scraper._release(browser, remote=False))
        asyncio.run(puppeteer_scraper._release(browser, remote=True))
        session = puppeteer_scraper._PyppeteerSession.__new__(puppeteer_scraper._PyppeteerSession)
        session.page, session.browser, session.owns_browser = _Never(), _Never(), True
        asyncio.run(session.close())
        assert _time.monotonic() - t0 < 2 and _Never.killed
    finally:
        puppeteer_scraper.CLOSE_TIMEOUT_S = old


@check("a login page served under the address asked for (no redirect to go by — measured with the Scraper API's pool, canonical https://fi-fi.facebook.com/login) is a BLOCK (exit 3), never an unknown page; the canonical test fires on none of 66 real captures")
def _():
    login = ('<!DOCTYPE html><html id="facebook" lang="fi"><head><link rel="canonical" href="https://fi-fi.facebook.com/login" />'
             '<title>Facebook</title></head><body><form id="login_form"></form></body></html>')
    assert pp.page_state(login) == "login" and pp.is_login_page(login)
    for name in _PAGES:
        assert not pp.is_login_page(_fx(name)), name
    rc, meta, rows, eng = _pflow({"*": {"html": login, "status": 200}}, ["--url", "nike"])
    assert rc == output_writer.EXIT_BLOCKED and meta is None, rc


@check("an EMPTY Scraper API answer (no page, no target status — measured 2026-10-06) is the service failing: retried, and when it persists the run ends as remote_api_error (exit 5), never as an unreadable page")
def _():
    rc, meta, rows, client, _ = _sapi([(None, "")], _SAPI_ARGV + ["--retries", "1"])
    assert rc == output_writer.EXIT_REMOTE_API_ERROR and len(client.calls) == 2, (rc, len(client.calls))


def run() -> int:
    """All @check-decorated functions above already ran at import time
    (that's the point — see the `check()` docstring) and self-registered
    into RESULTS. This just reports them."""
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, d) for n, ok, d in RESULTS if not ok]
    print(f"smoke_test: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAIL: {name}\n        {detail}")
    return 0 if not failed else 1


if __name__ == "__main__":
    import sys
    sys.exit(run())
