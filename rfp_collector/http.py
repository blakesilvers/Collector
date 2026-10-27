from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; rfp-collector/1.0; +https://github.com/blakesilvers/collector)"


def request(url: str, *, data: dict | None = None, headers: dict | None = None,
            timeout: float = 60, retries: int = 3, backoff: float = 2.0) -> bytes:
    """GET (or JSON POST when `data` is given) with retry on network/5xx errors."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        hdrs["Content-Type"] = "application/json"
    hdrs.update(headers or {})
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdrs, method="POST" if body else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if attempt < retries:
            wait = backoff * (2 ** attempt)
            log.debug("retrying %s in %.0fs after %s", url, wait, last)
            time.sleep(wait)
    assert last is not None
    raise last


def get_json(url: str, **kw):
    return json.loads(request(url, **kw).decode("utf-8", "replace"))
