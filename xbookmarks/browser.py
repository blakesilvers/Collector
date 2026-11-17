"""Capture bookmarks by letting a real browser load x.com/i/bookmarks.

A persistent Chromium profile (``data/xbookmarks/profile``) keeps you logged in after a
one-time ``python -m xbookmarks login``. Each run opens the bookmarks page, listens for
the ``Bookmarks`` GraphQL responses the page itself requests, scrolls to load more, and
stops when it reaches posts it has already stored (or the end, or ``max_items``).

Because the page makes the requests, nothing here depends on X's internal query IDs,
feature flags or request-signing headers, which change often.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import Bookmark
from .parse import is_bookmarks_request, parse_timeline

BOOKMARKS_PATH = "/i/bookmarks"
LOGIN_MARKERS = ("/login", "/i/flow/login", "/i/flow/signup", "/account/access", "mode=login")


COOKIE_ENV = ("X_AUTH_TOKEN", "X_CT0")   # alternative to `login`: copy these two cookies from a logged-in browser


class NotLoggedIn(RuntimeError):
    pass


class CaptureError(RuntimeError):
    pass


@dataclass
class CaptureResult:
    bookmarks: list[Bookmark] = field(default_factory=list)
    pages: int = 0
    reached_end: bool = False
    stopped_on_known: bool = False
    api: dict | None = None          # query id, variables and features X's page used (for debugging)


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError as e:  # pragma: no cover
        raise CaptureError("Playwright is not installed. Run:  pip install playwright  &&  playwright install chromium") from e
    from playwright.sync_api import sync_playwright
    return sync_playwright


def _launch(p, profile_dir: Path, *, headless: bool, channel: str = "", executable_path: str = ""):
    profile_dir.mkdir(parents=True, exist_ok=True)
    kwargs = dict(headless=headless, viewport={"width": 1280, "height": 900},
                  args=["--disable-blink-features=AutomationControlled"])
    if channel:
        kwargs["channel"] = channel
    if executable_path:
        kwargs["executable_path"] = executable_path
    return p.chromium.launch_persistent_context(str(profile_dir), **kwargs)


def _inject_env_cookies(context, base_url: str) -> bool:
    """If X_AUTH_TOKEN (and optionally X_CT0) are set, put them in the browser profile.

    This lets the collector run on a machine where nobody can click through the login
    page, such as a server or a cloud session. Copy the two cookies from a browser where
    you are logged in to X: DevTools > Application > Cookies > x.com.
    """
    token = os.environ.get(COOKIE_ENV[0], "").strip()
    if not token:
        return False
    host = urlsplit(base_url).hostname or "x.com"
    domain = host if host.replace(".", "").isdigit() or host == "localhost" else "." + host.removeprefix("www.")
    secure = base_url.startswith("https")
    cookies = [{"name": "auth_token", "value": token, "domain": domain, "path": "/", "secure": secure,
                "httpOnly": True, "sameSite": "None" if secure else "Lax", "expires": int(time.time()) + 365 * 86400}]
    ct0 = os.environ.get(COOKIE_ENV[1], "").strip()
    if ct0:
        cookies.append({"name": "ct0", "value": ct0, "domain": domain, "path": "/", "secure": secure,
                        "sameSite": "None" if secure else "Lax", "expires": int(time.time()) + 365 * 86400})
    context.add_cookies(cookies)
    return True


def _logged_in(context, base_url: str) -> bool:
    try:
        cookies = context.cookies(base_url)
    except Exception:
        cookies = context.cookies()
    return any(c.get("name") == "auth_token" and c.get("value") for c in cookies)


def login(profile_dir: Path, *, base_url: str = "https://x.com", channel: str = "", executable_path: str = "",
          timeout_s: int = 600) -> bool:
    """Open a visible browser on the X login page and wait until the session cookie appears."""
    with _playwright()() as p:
        context = _launch(p, profile_dir, headless=False, channel=channel, executable_path=executable_path)
        page = context.pages[0] if context.pages else context.new_page()
        if _logged_in(context, base_url):
            print("Already logged in. Close the browser window to finish.")
        page.goto(f"{base_url}/login", wait_until="domcontentloaded")
        print("Log in to X in the browser window. This waits up to %d minutes." % (timeout_s // 60))
        deadline = time.time() + timeout_s
        ok = False
        while time.time() < deadline:
            if _logged_in(context, base_url):
                ok = True
                break
            try:
                page.wait_for_timeout(1500)
            except Exception:
                break
        if ok:
            print("Logged in. Session saved to", profile_dir)
            try:
                page.wait_for_timeout(1500)   # let X finish setting cookies
            except Exception:
                pass
        context.close()
        return ok


def cookie_source(profile_dir: Path) -> str:
    """Where the next run will get its X session from, without touching the browser.

    ``"env"`` when X_AUTH_TOKEN is set (the file or variable wins over the saved profile),
    ``"profile"`` when a saved browser profile exists, ``"none"`` otherwise. Never returns a value.
    """
    if os.environ.get(COOKIE_ENV[0], "").strip():
        return "env"
    if (profile_dir / "Default" / "Cookies").exists() or (profile_dir / "Cookies").exists():
        return "profile"
    return "none"


def check_session(profile_dir: Path, *, base_url: str = "https://x.com", channel: str = "",
                  executable_path: str = "", timeout_ms: int = 45000) -> tuple[str, str]:
    """Open the bookmarks page headlessly and report whether X accepts the session.

    Returns ``(status, detail)`` where status is ``"ok"`` (a Bookmarks response arrived),
    ``"no_cookies"`` (nothing to log in with), ``"expired"`` (X redirected to its login page),
    ``"blocked"`` (X refused the page itself with an HTTP error, independent of cookies),
    ``"error"`` (navigation failed for another reason) or ``"unknown"`` (nothing conclusive
    before the timeout). Cookie values are never returned.
    """
    responses: list = []
    with _playwright()() as p:
        context = _launch(p, profile_dir, headless=True, channel=channel, executable_path=executable_path)
        try:
            _inject_env_cookies(context, base_url)
            if not _logged_in(context, base_url):
                return "no_cookies", f"no auth_token in the profile and {COOKIE_ENV[0]} is not set"
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", lambda r: responses.append(r) if is_bookmarks_request(r.url) else None)
            documents: list = []
            page.on("response", lambda r: documents.append(r) if r.request.resource_type == "document" else None)
            try:
                page.goto(f"{base_url}{BOOKMARKS_PATH}", wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as e:  # noqa: BLE001
                codes = [d.status for d in documents if d.status >= 400]
                if codes:
                    return "blocked", (f"X answered the page request with HTTP {codes[-1]} before any login check; "
                                       "this machine or browser is being refused, so fresh cookies will not help here")
                return "error", str(e).splitlines()[0]
            deadline = time.time() + timeout_ms / 1000
            while time.time() < deadline:
                if any(m in page.url for m in LOGIN_MARKERS):
                    return "expired", "X redirected to its login page"
                for resp in responses:
                    if resp.status == 200:
                        return "ok", "X served the bookmarks timeline"
                    if resp.status in (401, 403):
                        return "expired", f"X answered the bookmarks request with HTTP {resp.status}"
                page.wait_for_timeout(500)
            return "unknown", "no bookmarks response and no login redirect before the timeout"
        finally:
            context.close()


def _api_info(request) -> dict:
    parts = urlsplit(request.url)
    qs = parse_qs(parts.query)
    info = {"url": parts.path, "query_id": parts.path.rstrip("/").split("/")[-2]}
    for key in ("variables", "features", "fieldToggles"):
        if key in qs:
            try:
                info[key] = json.loads(qs[key][0])
            except (ValueError, IndexError):
                info[key] = qs[key][0]
    return info


def capture(profile_dir: Path, *, known_ids: set[str] | None = None, full: bool = False, max_items: int = 400,
            headless: bool = True, base_url: str = "https://x.com", channel: str = "", executable_path: str = "",
            idle_rounds: int = 6, settle_ms: int = 1500, page_timeout_ms: int = 60000,
            keep_raw: bool = True) -> CaptureResult:
    """Scroll the bookmarks page and return everything the page loaded, newest bookmark first.

    Stops early when a whole loaded page consists of already-known IDs (unless ``full``),
    when ``max_items`` is reached, or when scrolling stops producing new pages.
    """
    known_ids = known_ids or set()
    result = CaptureResult()
    seen_ids: set[str] = set()
    responses: list = []

    with _playwright()() as p:
        context = _launch(p, profile_dir, headless=headless, channel=channel, executable_path=executable_path)
        try:
            _inject_env_cookies(context, base_url)
            if not _logged_in(context, base_url):
                raise NotLoggedIn("No X session in the browser profile. Run:  python -m xbookmarks login  "
                                  f"(or set the {COOKIE_ENV[0]} and {COOKIE_ENV[1]} environment variables)")
            page = context.pages[0] if context.pages else context.new_page()
            page.on("response", lambda r: responses.append(r) if is_bookmarks_request(r.url) else None)
            page.goto(f"{base_url}{BOOKMARKS_PATH}", wait_until="domcontentloaded", timeout=page_timeout_ms)

            processed = 0
            idle = 0
            deadline = time.time() + page_timeout_ms / 1000
            while True:
                if any(m in page.url for m in LOGIN_MARKERS):
                    raise NotLoggedIn("X redirected to its login page: the saved session expired. "
                                      "Run:  python -m xbookmarks login")
                if len(responses) > processed:
                    idle = 0
                    stop = False
                    while processed < len(responses):
                        resp = responses[processed]
                        processed += 1
                        if resp.status != 200:
                            continue
                        try:
                            payload = resp.json()
                        except Exception:
                            continue
                        if result.api is None:
                            result.api = _api_info(resp.request)
                        bookmarks, cursor = parse_timeline(payload)
                        result.pages += 1
                        fresh = [b for b in bookmarks if b.id not in seen_ids]
                        for b in fresh:
                            if not keep_raw:
                                b.raw = None
                            seen_ids.add(b.id)
                            b.position = len(result.bookmarks)
                            result.bookmarks.append(b)
                        if not bookmarks or not cursor:
                            result.reached_end = True
                            stop = True
                        elif not full and fresh and all(b.id in known_ids for b in fresh):
                            result.stopped_on_known = True
                            stop = True
                        elif not full and bookmarks and not fresh:
                            # The page re-sent a slice we already hold: nothing new to learn here.
                            result.stopped_on_known = True
                            stop = True
                        if len(result.bookmarks) >= max_items:
                            stop = True
                    if stop:
                        break
                    deadline = time.time() + page_timeout_ms / 1000
                else:
                    idle += 1
                    if idle > idle_rounds:
                        result.reached_end = True
                        break
                    if time.time() > deadline:
                        if result.pages == 0:
                            raise CaptureError("The bookmarks page loaded no bookmark data in time. "
                                               "Try again with --headed to see what X is showing.")
                        break
                page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                page.wait_for_timeout(settle_ms)
        finally:
            context.close()
    return result
