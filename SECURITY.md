# Security

## Supported versions

Only the latest commit on `main` and the most recently tagged release get
security fixes. This project is pre-1.0 (see `CHANGELOG.md`) — older tags
are not backported.

## Reporting a vulnerability

Please report security issues privately — open a GitHub security advisory
on this repo, or email support@2captcha.com — rather than a public issue.
Include the version/commit and a minimal reproduction.

We aim to acknowledge a new report within 5 business days, confirm whether
it's in scope, and share a fix timeline once it's confirmed. If a report
turns out to be a real, exploitable issue, we'll credit the reporter in
`CHANGELOG.md` unless they'd rather stay anonymous.

## Scope

**In scope**: this repo's own code and dependencies — credential handling,
injection risks, anything that would make this tool leak a secret, execute
untrusted code, or misreport what it actually did (a `complete` status on a
run that silently dropped data would count, for example).

**Out of scope**: vulnerabilities in facebook.com itself — report those to
Meta's bug bounty programme, not here. This tool only ever reads what a
public Page serves a logged-out visitor; it never logs in and never
submits anything — a report that this tool "can't" read something behind
a login is a scope statement, not a vulnerability. Similarly, an input
that is not a Page address being skipped (never requested at all — see
`page_parser.normalize_input`), and a personal profile being skipped, are
documented, intended behavior.

## What this tool does with credentials

- `TWOCAPTCHA_KEY`, `FACEBOOK_PROXY`, `FACEBOOK_CDP_ENDPOINT` belong in `.env` or the environment (see `env_config.py`). The matching `--twocaptcha-key` / `--proxy` / `--cdp-endpoint` flags exist, but a credential on a command line ends up in shell history and process lists, so the docs never use them. Credentials are never logged in full: every log line masks the password; a proxy's login and host:port stay visible so exits can be told apart.
- This project's own code doesn't phone home. The only network calls it makes are to `facebook.com` (the browser also loads the page's own assets from Facebook's CDN, `fbcdn.net`) and, when configured, `api.2captcha.com` / `scraper.2captcha.com` / `cb.2captcha.com`. **One caveat on the Selenium engine**: Selenium's own bundled Selenium Manager sends anonymous usage stats to `plausible.io` by default whenever it launches a browser, which `selenium_scraper.py` disables on your behalf (`SE_AVOID_STATS=true`, set as a default rather than forced, so it never overrides a value you set yourself) so this project's "nothing phones home" claim actually holds. Selenium Manager can still make a *separate* network call to resolve a matching `chromedriver` version if one isn't already reachable on `PATH` / `SELENIUM_CHROME_BIN` — set that variable to a Chrome/Chromium binary you already have to avoid it entirely (see `selenium_scraper.py`'s module docstring).
- This is a scraper, not an account tool: it never logs in, never touches an authenticated Facebook session, and never writes anything back to Facebook — no reports, likes, follows, comments or messages, ever.
