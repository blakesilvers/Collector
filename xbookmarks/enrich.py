"""Follow the links in bookmarked posts and describe what is behind them.

For every non-X link the post carries this fetches the page title and description.
GitHub repositories get more: stars, last push, language, license, archived flag, a
README excerpt, and signals that matter for an AI-tooling workflow: whether the repo
is a Claude Code skill (SKILL.md), a Claude Code plugin (.claude-plugin/), or an MCP
server (.mcp.json, or MCP in its description/README). Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

UA = "Mozilla/5.0 (compatible; xbookmarks/1.0; +https://github.com/blakesilvers/Collector)"
SKIP_HOSTS = ("x.com", "twitter.com", "t.co", "pic.twitter.com", "pbs.twimg.com", "video.twimg.com")
MAX_BYTES = 400_000
README_EXCERPT = 1200


@dataclass
class LinkInfo:
    url: str
    final_url: str = ""
    kind: str = "page"                 # page | github | github-file | skipped | error
    title: str = ""
    description: str = ""
    site: str = ""
    github: dict | None = None         # see _github()
    signals: list[str] = field(default_factory=list)
    readme_excerpt: str = ""
    fetched_at: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LinkInfo":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ------------------------------------------------------------------ fetching ---

def fetch(url: str, *, headers: dict | None = None, timeout: int = 15) -> tuple[int, dict, bytes]:
    """GET a URL. Returns (status, lower-cased headers, body). Never raises for HTTP errors."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BYTES)
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, body
    except urllib.error.HTTPError as e:
        try:
            body = e.read(MAX_BYTES)
        except Exception:
            body = b""
        return e.code, {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}, body
    except Exception as e:  # noqa: BLE001  (DNS, timeout, TLS, refused)
        raise ConnectionError(f"{type(e).__name__}: {e}") from e


def _final_url(url: str, timeout: int) -> str:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl()
    except Exception:
        return url


class _Meta(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta: dict[str, str] = {}
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and a.get("content") and key not in self.meta:
                self.meta[key] = a["content"]

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


def _decode(body: bytes, headers: dict) -> str:
    m = re.search(r"charset=([\w-]+)", headers.get("content-type", ""))
    for enc in ([m.group(1)] if m else []) + ["utf-8", "latin-1"]:
        try:
            return body.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return ""


def _clean(s: str, limit: int = 600) -> str:
    return " ".join((s or "").split())[:limit]


def _page(info: LinkInfo, timeout: int) -> LinkInfo:
    status, headers, body = fetch(info.url, headers={"Accept": "text/html,*/*;q=0.8"}, timeout=timeout)
    if status >= 400:
        info.error = f"HTTP {status}"
        return info
    ctype = headers.get("content-type", "")
    if "html" not in ctype and "xml" not in ctype:
        info.title = f"{ctype.split(';')[0] or 'file'} ({int(headers.get('content-length', 0) or 0) // 1024} KB)"
        return info
    p = _Meta()
    try:
        p.feed(_decode(body, headers))
    except Exception:
        pass
    info.title = _clean(p.meta.get("og:title") or p.title, 200)
    info.description = _clean(p.meta.get("description") or p.meta.get("og:description") or p.meta.get("twitter:description"))
    info.site = _clean(p.meta.get("og:site_name"), 80)
    return info


# -------------------------------------------------------------------- GitHub ---

GITHUB_RE = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?(?:/(tree|blob)/([^/]+)(?:/(.*))?)?/?(?:[?#].*)?$")


def _gh_headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _raw_exists(owner: str, repo: str, branch: str, path: str, timeout: int) -> bytes | None:
    try:
        status, _, body = fetch(f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}", timeout=timeout)
    except ConnectionError:
        return None
    return body if status == 200 else None


def _github(info: LinkInfo, m: re.Match, timeout: int) -> LinkInfo:
    owner, repo, _, ref, path = m.groups()
    info.kind = "github"
    info.site = "GitHub"
    gh: dict = {"owner": owner, "repo": repo, "path": path or ""}
    branch = ref or "main"
    api_ok = False
    try:
        status, headers, body = fetch(f"https://api.github.com/repos/{owner}/{repo}", headers=_gh_headers(), timeout=timeout)
        if status == 200:
            d = json.loads(body)
            api_ok = True
            gh.update({
                "description": _clean(d.get("description") or ""), "stars": d.get("stargazers_count"),
                "forks": d.get("forks_count"), "open_issues": d.get("open_issues_count"),
                "language": d.get("language") or "", "license": (d.get("license") or {}).get("spdx_id") or "",
                "archived": bool(d.get("archived")), "fork": bool(d.get("fork")),
                "pushed_at": d.get("pushed_at") or "", "created_at": d.get("created_at") or "",
                "topics": d.get("topics") or [], "homepage": d.get("homepage") or "",
                "default_branch": d.get("default_branch") or "main",
            })
            branch = ref or gh["default_branch"]
        elif status == 404:
            info.error = "repository not found (deleted, renamed, or private)"
            info.github = gh
            return info
        elif status in (403, 429):
            gh["api_note"] = "GitHub API rate limit or blocked; using raw files only (set GITHUB_TOKEN for 5,000 requests/hour)"
    except ConnectionError as e:
        gh["api_note"] = f"GitHub API unreachable: {e}"

    if not api_ok:
        # Fall back to the repository page's Open Graph tags.
        try:
            status, headers, body = fetch(f"https://github.com/{owner}/{repo}", headers={"Accept": "text/html"}, timeout=timeout)
            if status == 200:
                p = _Meta()
                p.feed(_decode(body, headers))
                desc = _clean(p.meta.get("og:description") or "")
                desc = re.sub(r"\.?\s*Contribute to .*? on GitHub\.?$", "", desc)
                desc = re.sub(rf"\s*-\s*{re.escape(owner)}/{re.escape(repo)}\s*$", "", desc, flags=re.I)
                gh["description"] = desc.strip()
            elif status == 404:
                info.error = "repository not found (deleted, renamed, or private)"
                info.github = gh
                return info
        except ConnectionError:
            pass

    # Signals and README from raw files (no API quota).
    base = f"{path.rstrip('/')}/" if path else ""
    branches = [branch] + (["master"] if branch == "main" else ["main"])
    found_branch = None
    readme = None
    for b in branches:
        for name in ("README.md", "readme.md", "README.MD", "README", "README.rst"):
            readme = _raw_exists(owner, repo, b, base + name, timeout)
            if readme is not None:
                found_branch = b
                break
        if found_branch:
            break
    b = found_branch or branch
    signals = []
    if _raw_exists(owner, repo, b, base + "SKILL.md", timeout) is not None:
        signals.append("Claude Code skill (SKILL.md)")
    if _raw_exists(owner, repo, b, ".claude-plugin/plugin.json", timeout) is not None or \
            _raw_exists(owner, repo, b, ".claude-plugin/marketplace.json", timeout) is not None:
        signals.append("Claude Code plugin (.claude-plugin/)")
    if _raw_exists(owner, repo, b, ".mcp.json", timeout) is not None:
        signals.append("MCP config (.mcp.json)")
    text_blob = " ".join([gh.get("description", ""), " ".join(gh.get("topics", [])),
                          (readme or b"")[:4000].decode("utf-8", "ignore")]).lower()
    if "model context protocol" in text_blob or re.search(r"\bmcp\b", text_blob):
        signals.append("mentions MCP / Model Context Protocol")
    if re.search(r"\bclaude code\b", text_blob):
        signals.append("mentions Claude Code")
    if gh.get("archived"):
        signals.append("ARCHIVED repository")
    pushed = gh.get("pushed_at")
    if pushed:
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(pushed.replace("Z", "+00:00"))).days
            gh["days_since_push"] = age
            if age > 365:
                signals.append(f"no commits in {age // 30} months")
        except ValueError:
            pass
    if readme is not None:
        text = readme.decode("utf-8", "ignore")
        text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # images
        text = re.sub(r"<[^>]+>", "", text)                        # html badges
        text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)      # links -> text
        text = re.sub(r"\n{2,}", "\n", text)
        info.readme_excerpt = "\n".join(line for line in text.splitlines() if line.strip())[:README_EXCERPT].strip()
    elif api_ok or gh.get("description"):
        signals.append("no README found")
    info.signals = signals
    info.github = gh
    info.title = f"{owner}/{repo}" + (f" ({path})" if path else "")
    info.description = gh.get("description", "")
    return info


# ---------------------------------------------------------------------- main ---

def describe(url: str, *, timeout: int = 15) -> LinkInfo:
    """Fetch and describe one URL. Never raises; failures land in `.error`."""
    info = LinkInfo(url=url, fetched_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    host = urlsplit(url).netloc.lower()
    if any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        info.kind = "skipped"
        return info
    try:
        m = GITHUB_RE.match(url)
        if m:
            if m.group(3) == "blob":
                info.kind = "github-file"
                info.title = f"{m.group(1)}/{m.group(2)}: {m.group(5) or ''}"
                info.final_url = url
                return info
            info.final_url = url
            return _github(info, m, timeout)
        info.final_url = _final_url(url, timeout)
        if info.final_url != url and GITHUB_RE.match(info.final_url):
            return _github(info, GITHUB_RE.match(info.final_url), timeout)
        return _page(info, timeout)
    except ConnectionError as e:
        info.kind = "error"
        info.error = str(e)
        return info
    except Exception as e:  # noqa: BLE001
        info.kind = "error"
        info.error = f"{type(e).__name__}: {e}"
        return info
