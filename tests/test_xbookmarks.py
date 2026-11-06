"""Tests for the X bookmarks collector. The browser test runs a local stand-in for
x.com/i/bookmarks, so it needs Playwright and a Chromium; it is skipped otherwise."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from xbookmarks import parse, report
from xbookmarks.models import Bookmark
from xbookmarks.store import Store

# ---------------------------------------------------------------- fixtures ---

def user(handle, name, new_layout=True):
    u = {"__typename": "User", "rest_id": "u" + handle, "legacy": {}}
    if new_layout:
        u["core"] = {"screen_name": handle, "name": name}
    else:
        u["legacy"] = {"screen_name": handle, "name": name}
    return u


def tweet(tid, handle="alice", name="Alice", text="hello", urls=(), media=(), quoted=None, note=None,
          visibility=False, rt=None, new_layout=True, created="Wed Oct 10 20:19:24 +0000 2018"):
    legacy = {"full_text": text, "created_at": created, "lang": "en", "favorite_count": 12,
              "bookmark_count": 3, "entities": {"urls": [{"url": f"https://t.co/{i}", "expanded_url": u}
                                                         for i, u in enumerate(urls)]}}
    if media:
        legacy["entities"]["media"] = [{"url": "https://t.co/m", "type": m, "media_url_https": f"https://pbs/{m}.jpg"} for m in media]
        legacy["extended_entities"] = {"media": [
            {"url": "https://t.co/m", "type": m, "media_url_https": f"https://pbs/{m}.jpg",
             **({"video_info": {"variants": [{"content_type": "video/mp4", "bitrate": 1, "url": "https://v/lo.mp4"},
                                             {"content_type": "video/mp4", "bitrate": 9, "url": "https://v/hi.mp4"}]}}
                if m == "video" else {})} for m in media]}
        legacy["full_text"] = text + " https://t.co/m"
    if rt:
        legacy["retweeted_status_result"] = {"result": rt}
    t = {"__typename": "Tweet", "rest_id": str(tid), "core": {"user_results": {"result": user(handle, name, new_layout)}},
         "legacy": legacy, "views": {"count": "4567"}}
    if note:
        t["note_tweet"] = {"note_tweet_results": {"result": {"text": note, "entity_set": {"urls": []}}}}
    if quoted:
        t["quoted_status_result"] = {"result": quoted}
    if visibility:
        t = {"__typename": "TweetWithVisibilityResults", "tweet": t}
    return t


def entry(t):
    tid = t.get("rest_id") or (t.get("tweet") or {}).get("rest_id") or "tomb"
    return {"entryId": f"tweet-{tid}",
            "content": {"entryType": "TimelineTimelineItem", "itemContent": {"itemType": "TimelineTweet", "tweet_results": {"result": t}}}}


def page_payload(tweets, cursor):
    entries = [entry(t) for t in tweets]
    entries.append({"entryId": "cursor-top-1", "content": {"entryType": "TimelineTimelineCursor", "cursorType": "Top", "value": "T"}})
    if cursor:
        entries.append({"entryId": "cursor-bottom-1", "content": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": cursor}})
    return {"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}


SAMPLE = page_payload([
    tweet(1, text="plain post https://t.co/0 trailing", urls=["https://example.com/a"]),
    tweet(2, handle="bob", name="Bob", text="pic", media=["photo", "video"], visibility=True),
    tweet(3, handle="carol", name="Carol", text="short", note="This is the long form text of a very long post."),
    tweet(4, text="quoting", quoted=tweet(99, handle="dave", name="Dave", text="original")),
    tweet(5, handle="erin", name="Erin", text="RT @frank: x", rt=tweet(98, handle="frank", name="Frank", text="the original", urls=["https://f.example"])),
    tweet(6, handle="old", name="Old Layout", text="legacy user layout", new_layout=False),
    {"__typename": "TweetTombstone", "tombstone": {}},
], cursor="C2")


class ParseTests(unittest.TestCase):
    def test_request_matcher(self):
        self.assertTrue(parse.is_bookmarks_request("https://x.com/i/api/graphql/AbC-123/Bookmarks?variables=%7B%7D"))
        self.assertFalse(parse.is_bookmarks_request("https://x.com/i/api/graphql/AbC/BookmarkFolderTimeline?x=1"))
        self.assertFalse(parse.is_bookmarks_request("https://x.com/i/api/graphql/AbC/HomeTimeline"))

    def test_timeline(self):
        bms, cursor = parse.parse_timeline(SAMPLE)
        self.assertEqual(cursor, "C2")
        self.assertEqual([b.id for b in bms], ["1", "2", "3", "4", "5", "6"])  # tombstone skipped
        by = {b.id: b for b in bms}
        self.assertEqual(by["1"].text, "plain post https://example.com/a trailing")
        self.assertEqual(by["1"].links, ["https://example.com/a"])
        self.assertEqual(by["1"].url, "https://x.com/alice/status/1")
        self.assertEqual(by["1"].created_at, "2018-10-10T20:19:24+00:00")
        self.assertEqual(by["1"].metrics["views"], 4567)
        self.assertEqual(by["2"].author_handle, "bob")           # unwrapped from TweetWithVisibilityResults
        self.assertEqual(by["2"].text, "pic")                     # media t.co link stripped
        self.assertEqual([m["type"] for m in by["2"].media], ["photo", "video"])
        self.assertEqual(by["2"].media[1]["video_url"], "https://v/hi.mp4")
        self.assertTrue(by["3"].text.startswith("This is the long form"))
        self.assertEqual(by["4"].quoted["author_handle"], "dave")
        self.assertEqual(by["4"].quoted["url"], "https://x.com/dave/status/99")
        self.assertTrue(by["5"].is_retweet)
        self.assertEqual(by["5"].text, "RT @frank: the original")
        self.assertEqual(by["5"].links, ["https://f.example"])
        self.assertEqual(by["6"].author_name, "Old Layout")
        self.assertEqual([b.position for b in bms], list(range(6)))

    def test_empty_and_garbage(self):
        self.assertEqual(parse.parse_timeline({}), ([], None))
        self.assertEqual(parse.parse_timeline({"data": {"bookmark_timeline_v2": {"timeline": {"instructions": [{"type": "TimelineClearCache"}]}}}}), ([], None))
        self.assertIsNone(parse.parse_tweet({"__typename": "Tweet"}))   # no id


class StoreTests(unittest.TestCase):
    def test_incremental(self):
        with TemporaryDirectory() as d:
            store = Store(Path(d) / "b.sqlite")
            bms, _ = parse.parse_timeline(SAMPLE)
            run = store.start_run("incremental")
            new = store.upsert(bms)
            store.finish_run(run, len(bms), len(new))
            self.assertEqual(len(new), 6)
            self.assertTrue(all(b.is_new for b in new))
            self.assertEqual(store.count(), 6)
            again = store.upsert(parse.parse_timeline(SAMPLE)[0])
            self.assertEqual(again, [])
            self.assertEqual(store.known_ids(), {"1", "2", "3", "4", "5", "6"})
            self.assertEqual(len(store.last_days(1)), 6)
            self.assertEqual(store.since(datetime.now().astimezone() + timedelta(days=1)), [])
            self.assertEqual([b.id for b in store.search("long form")], ["3"])
            self.assertIsNotNone(store.last_successful_run())
            bad = store.start_run("incremental")
            store.finish_run(bad, 0, 0, "boom")
            self.assertEqual(store.all()[0].quoted, None)
            self.assertEqual(store.all()[3].quoted["author_handle"], "dave")


class ReportTests(unittest.TestCase):
    def test_outputs(self):
        bms, _ = parse.parse_timeline(SAMPLE)
        meta = {"date": "2026-11-06", "since": "2026-11-05 05:30", "total": 123, "note": "a note"}
        with TemporaryDirectory() as d:
            paths = report.write_all(Path(d), bms, meta)
            md = paths["md"].read_text(encoding="utf-8")
            self.assertIn("# X bookmarks — 2026-11-06", md)
            self.assertIn("6 new bookmarks since 2026-11-05 05:30. 123 stored in total.", md)
            self.assertIn("## 1. @alice (Alice)", md)
            self.assertIn("> This is the long form", md)
            self.assertIn("Quoting @dave: original <https://x.com/dave/status/99>", md)
            self.assertIn("Media: 1 photo, 1 video", md)
            self.assertIn("Post: <https://x.com/alice/status/1>", md)
            html = paths["html"].read_text(encoding="utf-8")
            self.assertIn("Open on X", html)
            data = json.loads(paths["json"].read_text(encoding="utf-8"))
            self.assertEqual(len(data["bookmarks"]), 6)
            self.assertNotIn("raw", data["bookmarks"][0])
            empty = report.to_markdown([], {"date": "x", "total": 0})
            self.assertIn("Nothing new.", empty)


# ------------------------------------------------------- browser end-to-end ---

PAGE_HTML = """<!doctype html><html><body><h1>Bookmarks</h1><div id="feed"></div>
<script>
let cursor = null, done = false, loading = false;
async function load() {
  if (done || loading) return; loading = true;
  const vars = JSON.stringify(cursor ? {count: 20, cursor} : {count: 20});
  const r = await fetch('/i/api/graphql/QID123/Bookmarks?variables=' + encodeURIComponent(vars) + '&features=%7B%7D');
  const j = await r.json();
  const entries = j.data.bookmark_timeline_v2.timeline.instructions[0].entries;
  const tweets = entries.filter(e => e.entryId.startsWith('tweet-'));
  const bottom = entries.find(e => e.entryId.startsWith('cursor-bottom'));
  cursor = bottom ? bottom.content.value : null;
  if (!tweets.length || !cursor) done = true;
  const d = document.createElement('div'); d.style.height = '3000px'; d.textContent = 'page ' + tweets.length;
  document.getElementById('feed').appendChild(d);
  loading = false;
}
window.addEventListener('scroll', () => {
  if (window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 100) load();
});
load();
</script></body></html>"""


def make_pages(n_pages=3, per_page=20):
    pages = {}
    cur = None
    for p in range(n_pages):
        ids = [p * per_page + i + 1 for i in range(per_page)]
        nxt = f"C{p + 1}"
        pages[cur] = page_payload([tweet(i, text=f"post {i}") for i in ids], cursor=nxt)
        cur = nxt
    pages[cur] = page_payload([], cursor="END")   # X signals the end with a page of only cursors
    return pages


class MockX(BaseHTTPRequestHandler):
    pages = make_pages()
    hits: list[str] = []

    def log_message(self, *a):  # quiet
        pass

    def do_GET(self):
        parts = urlsplit(self.path)
        MockX.hits.append(parts.path)
        cookie = self.headers.get("Cookie", "")
        if parts.path.startswith("/i/flow/login"):
            self.send_response(200); self.end_headers(); self.wfile.write(b"<html>login</html>")
            return
        if "auth_token=good" not in cookie:
            self.send_response(302)
            self.send_header("Location", "/i/flow/login?redirect_after_login=%2Fi%2Fbookmarks")
            self.end_headers()
            return
        if parts.path == "/i/bookmarks":
            body = PAGE_HTML.encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        elif parts.path == "/i/api/graphql/QID123/Bookmarks":
            variables = json.loads(parse_qs(parts.query)["variables"][0])
            payload = self.pages.get(variables.get("cursor"))
            body = json.dumps(payload).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        else:
            self.send_response(404); self.end_headers()


def _chromium_path() -> str:
    for cand in (os.environ.get("XBOOKMARKS_TEST_CHROMIUM", ""), "/opt/pw-browsers/chromium"):
        if cand and Path(cand).exists():
            return cand
    return ""


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


@unittest.skipUnless(_playwright_available(), "playwright not installed")
class BrowserCaptureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockX)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def _profile(self, tmp, token="good") -> Path:
        from playwright.sync_api import sync_playwright
        profile = Path(tmp) / "profile"
        with sync_playwright() as p:
            kw = {"executable_path": _chromium_path()} if _chromium_path() else {}
            ctx = p.chromium.launch_persistent_context(str(profile), headless=True, **kw)
            if token:
                # Session cookies are not written to disk; give it an expiry like X's real auth_token.
                ctx.add_cookies([{"name": "auth_token", "value": token, "url": self.base,
                                  "expires": int(time.time()) + 3600}])
            ctx.close()
        return profile

    def capture(self, profile, **kw):
        from xbookmarks import browser
        return browser.capture(profile, base_url=self.base, headless=True, executable_path=_chromium_path(),
                               settle_ms=300, idle_rounds=4, page_timeout_ms=20000, **kw)

    def test_full_walk_to_end(self):
        with TemporaryDirectory() as tmp:
            res = self.capture(self._profile(tmp), full=True, max_items=10**6)
            self.assertEqual(len(res.bookmarks), 60)
            self.assertEqual([b.id for b in res.bookmarks[:3]], ["1", "2", "3"])
            self.assertTrue(res.reached_end)
            self.assertFalse(res.stopped_on_known)
            self.assertEqual(res.api["query_id"], "QID123")
            self.assertEqual(res.api["variables"], {"count": 20})

    def test_incremental_stops_at_known(self):
        with TemporaryDirectory() as tmp:
            known = {str(i) for i in range(21, 61)}       # pages 2 and 3 already stored
            res = self.capture(self._profile(tmp), known_ids=known, max_items=1000)
            self.assertTrue(res.stopped_on_known)
            self.assertEqual(len(res.bookmarks), 40)      # page 1 (new) + page 2 (all known) then stop
            self.assertEqual(res.pages, 2)

    def test_max_items(self):
        with TemporaryDirectory() as tmp:
            res = self.capture(self._profile(tmp), full=True, max_items=25)
            self.assertGreaterEqual(len(res.bookmarks), 25)
            self.assertLess(res.pages, 4)

    def test_not_logged_in(self):
        from xbookmarks import browser
        with TemporaryDirectory() as tmp:
            with self.assertRaises(browser.NotLoggedIn):
                self.capture(self._profile(tmp, token=""))

    def test_expired_session_redirect(self):
        from xbookmarks import browser
        with TemporaryDirectory() as tmp:
            with self.assertRaises(browser.NotLoggedIn):
                self.capture(self._profile(tmp, token="stale"))


if __name__ == "__main__":
    unittest.main()


class CliTests(unittest.TestCase):
    def test_run_report_and_email(self):
        from unittest import mock
        from xbookmarks import browser, cli

        bms, _ = parse.parse_timeline(SAMPLE)
        fake = browser.CaptureResult(bookmarks=bms, pages=1, reached_end=True,
                                     api={"query_id": "QID", "variables": {}})
        sent = []
        with TemporaryDirectory() as d:
            cfg_path = Path(d) / "x.toml"
            cfg_path.write_text(
                f'[paths]\ndata_dir = "{d}/data"\nreports_dir = "{d}/reports"\n'
                '[email]\nenabled = true\nusername = "me@example.com"\nto = "me@example.com"\n', encoding="utf-8")
            with mock.patch.object(browser, "capture", return_value=fake), \
                 mock.patch("xbookmarks.mailer.send", side_effect=lambda *a, **k: sent.append(a)):
                self.assertEqual(cli.main(["--config", str(cfg_path), "run"]), 0)
                self.assertEqual(len(sent), 1)
                self.assertTrue(sent[0][1].startswith("X bookmarks report ") and "(6 new)" in sent[0][1])
                self.assertIn("## 1. @alice", sent[0][2])
                # Second run: nothing new, so no email, and the report says so.
                self.assertEqual(cli.main(["--config", str(cfg_path), "run"]), 0)
                self.assertEqual(len(sent), 1)
            reports = sorted(Path(d, "reports").glob("bookmarks_*.md"))
            self.assertTrue(reports)
            self.assertIn("0 new bookmarks since", reports[-1].read_text(encoding="utf-8"))
            self.assertTrue(Path(d, "data", "last_request.json").exists())
            self.assertEqual(cli.main(["--config", str(cfg_path), "report", "--days", "3"]), 0)
            self.assertEqual(cli.main(["--config", str(cfg_path), "search", "long form"]), 0)
            self.assertEqual(cli.main(["--config", str(cfg_path), "export", "--out", f"{d}/all.json"]), 0)
            self.assertEqual(len(json.loads(Path(d, "all.json").read_text(encoding="utf-8"))), 6)
            with mock.patch.object(browser, "capture", side_effect=browser.NotLoggedIn("nope")):
                self.assertEqual(cli.main(["--config", str(cfg_path), "run"]), 2)

    def test_mailer_uses_ssl_and_attaches(self):
        from unittest import mock
        from xbookmarks import mailer
        with TemporaryDirectory() as d, mock.patch("smtplib.SMTP_SSL") as smtp, \
                mock.patch.dict(os.environ, {mailer.PASSWORD_ENV: "app-pass"}):
            att = Path(d) / "r.json"; att.write_text("{}", encoding="utf-8")
            mailer.send({"username": "me@example.com", "to": "you@example.com"}, "subj", "text", "<p>h</p>", [att])
            smtp.assert_called_once_with("smtp.gmail.com", 465, timeout=60)
            inst = smtp.return_value.__enter__.return_value
            inst.login.assert_called_once_with("me@example.com", "app-pass")
            msg = inst.send_message.call_args[0][0]
            self.assertEqual(msg["To"], "you@example.com")
            self.assertEqual([p.get_filename() for p in msg.iter_attachments()], ["r.json"])
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            mailer.send({"username": "me@example.com"}, "s", "t")
