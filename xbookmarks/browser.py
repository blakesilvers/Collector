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
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .models import Bookmark
from .parse import is_bookmarks_request, parse_timeline

BOOKMARKS_PATH = "/i/bookmarks"
LOGIN_MARKERS = ("/login", "/i/flow/login", "/i/flow/signup", "/account/access", "mode=login")


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
            if not _logged_in(context, base_url):
                raise NotLoggedIn("No X session in the browser profile. Run:  python -m xbookmarks login")
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
