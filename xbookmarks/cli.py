from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import browser, enrich, report
from .config import load
from .store import Store


def _store(cfg) -> Store:
    return Store(Path(cfg["paths"]["data_dir"]) / "bookmarks.sqlite")


def _profile(cfg) -> Path:
    return Path(cfg["paths"]["data_dir"]) / "profile"


def cmd_login(args, cfg) -> int:
    ok = browser.login(_profile(cfg), channel=cfg["browser"]["channel"],
                       executable_path=cfg["browser"]["executable_path"])
    return 0 if ok else 1


def _enrich(store: Store, bookmarks, cfg, limit: int | None = None) -> int:
    """Describe the outbound links of these bookmarks that the store has not described yet."""
    todo = store.links_missing(store.link_urls(bookmarks))
    limit = cfg["links"]["max_per_run"] if limit is None else limit
    done = 0
    for url in todo[:limit]:
        info = enrich.describe(url, timeout=cfg["links"]["timeout"])
        store.save_link(info.to_dict())
        done += 1
    store.attach_links(bookmarks)
    return len(todo) - done


def cmd_run(args, cfg) -> int:
    store = _store(cfg)
    run_id = store.start_run("full" if args.all else "incremental")
    since = store.last_successful_run()
    try:
        res = browser.capture(
            _profile(cfg), known_ids=store.known_ids(), full=args.all,
            max_items=args.max or (10**9 if args.all else cfg["browser"]["max_items"]),
            headless=not args.headed and cfg["browser"]["headless"],
            channel=cfg["browser"]["channel"], executable_path=cfg["browser"]["executable_path"],
            keep_raw=cfg["browser"]["keep_raw"])
    except browser.NotLoggedIn as e:
        store.finish_run(run_id, 0, 0, f"not logged in: {e}")
        print("ERROR:", e, file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        store.finish_run(run_id, 0, 0, f"{type(e).__name__}: {e}")
        print("ERROR:", e, file=sys.stderr)
        return 1
    new = store.upsert(res.bookmarks)
    store.finish_run(run_id, len(res.bookmarks), len(new))
    left = 0
    if cfg["links"]["enabled"] and not args.no_links and new:
        left = _enrich(store, new, cfg)
    if res.api:
        (Path(cfg["paths"]["data_dir"]) / "last_request.json").write_text(json.dumps(res.api, indent=2), encoding="utf-8")

    meta = {"date": datetime.now().strftime("%Y-%m-%d"),
            "since": since.strftime("%Y-%m-%d %H:%M") if since else "",
            "total": store.count(), "fetched": len(res.bookmarks), "pages": res.pages}
    if since is None and not args.all:
        meta["note"] = (f"First run: the newest {len(res.bookmarks)} bookmarks were stored and are listed here. "
                        "Run with --all once to store your whole history.")
    elif res.bookmarks and not res.stopped_on_known and not res.reached_end:
        meta["note"] = ("Stopped at the per-run limit before reaching already-stored posts; "
                        "there may be more new bookmarks. Raise max_items or run again.")
    if left:
        meta["note"] = (meta.get("note", "") + f" {left} link(s) not yet described; run `python -m xbookmarks enrich`.").strip()
    paths = report.write_all(Path(cfg["paths"]["reports_dir"]), new, meta)
    print(f"Fetched {len(res.bookmarks)} bookmark(s) in {res.pages} page(s); {len(new)} new; {store.count()} stored.")
    for k, p in paths.items():
        print(f"  {k}: {p}")

    email_cfg = cfg["email"]
    if email_cfg.get("enabled") and not args.no_email and (new or email_cfg.get("send_when_empty")):
        from . import mailer
        subject = f"X bookmarks report {meta['date']} ({len(new)} new)"
        mailer.send(email_cfg, subject, paths["md"].read_text(encoding="utf-8"),
                    paths["html"].read_text(encoding="utf-8"), [paths["json"]])
        print(f"  emailed to {email_cfg.get('to') or email_cfg.get('username')}")
    return 0


def cmd_enrich(args, cfg) -> int:
    store = _store(cfg)
    bookmarks = store.last_days(args.days) if args.days else store.all()
    todo = store.links_missing(store.link_urls(bookmarks))
    left = _enrich(store, bookmarks, cfg, limit=args.limit)
    print(f"{len(todo) - left} link(s) described; {left} left for a later run.")
    return 0


def cmd_report(args, cfg) -> int:
    """Rebuild a report from the store without touching the browser."""
    store = _store(cfg)
    bookmarks = store.last_days(args.days)
    store.attach_links(bookmarks)
    meta = {"date": datetime.now().strftime("%Y-%m-%d"), "total": store.count(),
            "since": (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d %H:%M")}
    paths = report.write_all(Path(cfg["paths"]["reports_dir"]), bookmarks, meta,
                             stamp=datetime.now().strftime("%Y-%m-%d") + f"_last{args.days}d")
    print(f"{len(bookmarks)} bookmark(s) from the last {args.days} day(s).")
    for k, p in paths.items():
        print(f"  {k}: {p}")
    return 0


def cmd_search(args, cfg) -> int:
    for b in _store(cfg).search(args.term):
        print(f"@{b.author_handle}  {b.created_at[:10]}  {b.url}\n   {' '.join(b.text.split())[:200]}")
    return 0


def cmd_export(args, cfg) -> int:
    store = _store(cfg)
    bookmarks = store.all()
    store.attach_links(bookmarks)
    rows = [b.to_row() for b in bookmarks]
    out = Path(args.out) if args.out else Path(cfg["paths"]["reports_dir"]) / "bookmarks_all.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"{len(rows)} bookmark(s) written to {out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="xbookmarks", description="Collect and report your X bookmarks.")
    p.add_argument("--config", type=Path, help="TOML config (default: xbookmarks.local.toml, then xbookmarks.toml)")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("login", help="open a browser window to log in to X once; the session is saved").set_defaults(fn=cmd_login)
    r = sub.add_parser("run", help="fetch new bookmarks, store them and write today's report (default)")
    r.add_argument("--all", action="store_true", help="walk the entire bookmark list, not just until known posts")
    r.add_argument("--max", type=int, help="stop after this many posts")
    r.add_argument("--headed", action="store_true", help="show the browser window")
    r.add_argument("--no-email", action="store_true", help="do not send the email even if enabled in config")
    r.add_argument("--no-links", action="store_true", help="do not follow and describe the links in new posts")
    r.set_defaults(fn=cmd_run)
    en = sub.add_parser("enrich", help="describe links of stored bookmarks that have not been looked up yet")
    en.add_argument("--days", type=int, default=0, help="only bookmarks first seen in the last N days (0 = all)")
    en.add_argument("--limit", type=int, help="maximum links to look up (default: links.max_per_run)")
    en.set_defaults(fn=cmd_enrich)
    rp = sub.add_parser("report", help="rebuild a report from stored bookmarks (no browser)")
    rp.add_argument("--days", type=int, default=1)
    rp.set_defaults(fn=cmd_report)
    s = sub.add_parser("search", help="search stored bookmarks")
    s.add_argument("term")
    s.set_defaults(fn=cmd_search)
    e = sub.add_parser("export", help="dump every stored bookmark to JSON")
    e.add_argument("--out")
    e.set_defaults(fn=cmd_export)
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    subcommands = {"login", "run", "report", "search", "export", "enrich"}
    if not (subcommands & set(argv)) and not ({"-h", "--help"} & set(argv)):
        i = 0                   # no subcommand given: behave as `run`, keeping global options first
        while i < len(argv):
            if argv[i] == "--config":
                i += 2
            elif argv[i].startswith("--config="):
                i += 1
            else:
                break
        argv = argv[:i] + ["run"] + argv[i:]
    args = parser.parse_args(argv)
    cfg = load(args.config)
    return args.fn(args, cfg)
