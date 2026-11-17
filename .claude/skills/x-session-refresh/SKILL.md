---
name: x-session-refresh
description: Check and refresh the X (Twitter) login session the xbookmarks collector uses. Use when `python -m xbookmarks` reports "not logged in", "redirected to its login page", "No X session in the browser profile", "loaded no bookmark data", or when the user asks where the X auth token goes, how to renew the X cookies, or to set up the collector on a machine without a screen.
---

# Refresh the X session for the bookmarks collector

The collector logs in to X with two browser cookies, `auth_token` and `ct0`. They live in
one of three places, checked in this order:

1. Environment variables `X_AUTH_TOKEN` and `X_CT0` already set in the shell or saved on a
   Claude Code cloud environment (Settings > Environments). These always win.
2. The file `.env` in the repository root (git-ignored). `python -m xbookmarks` loads it at
   startup and never overrides variables that are already set. `.env.example` shows the shape.
3. The saved browser profile `data/xbookmarks/profile/`, written by `python -m xbookmarks login`
   (needs a screen).

`auth_token` is equivalent to the user's X password. These rules hold no matter what else is asked:

- Never print, echo, log, grep -o, cat, or send a cookie value. Confirm presence with counts and
  names only, for example `grep -cE '^X_(AUTH_TOKEN|CT0)=.+' .env`.
- Never ask the user to paste a cookie into the chat. If they already did, write it to `.env`
  at once, tell them the value is now in the conversation transcript, and recommend they log out
  of X on all devices after the run so that cookie is invalidated.
- Never try to log in for the user, bypass a login page, or solve a challenge or captcha.
- Never commit `.env` or the browser profile. Both are in `.gitignore`; leave them there.

## Procedure

1. **Diagnose.** From the repository root, with the virtualenv active:

   ```bash
   python -m xbookmarks session --check
   ```

   It prints which source the next run will use and one of: `ok`, `expired`, `no_cookies`,
   `blocked`, `error`, `unknown`. Exit code 0 means the session works and nothing else is needed.
   `blocked` (exit 3) means X refused the page itself with an HTTP error before looking at any
   cookie; that is the machine or browser being refused, not a session problem. Headless
   Chromium in Claude's cloud containers gets HTTP 403 from x.com this way. Do not try to
   disguise the browser or otherwise get past it: tell the user to run the collector on their
   own computer instead. It opens a
   headless browser, so it takes 10 to 45 seconds. If Playwright is missing:
   `pip install playwright`; on a Claude cloud container do not run `playwright install`,
   set `executable_path = "/opt/pw-browsers/chromium"` under `[browser]` in
   `xbookmarks.local.toml` instead.

2. **If `expired` or `no_cookies`, have the user copy fresh cookies.** Give them these steps:
   - Open https://x.com in Chrome, Edge or Firefox and make sure they are logged in.
   - Press F12. Chrome/Edge: Application tab > Storage > Cookies > https://x.com.
     Firefox: Storage tab > Cookies > https://x.com. Safari: enable the Develop menu,
     then Web Inspector > Storage > Cookies.
   - Copy the **Value** of the row named `auth_token` (40 hex characters) and of the row
     named `ct0` (longer hex string). Both must come from the same browser at the same time;
     a `ct0` from another session is rejected.

3. **Tell the user where to put them**, depending on where the collector runs:
   - **Their own machine:** create `.env` in the repository root (copy `.env.example`) and
     fill in the two lines. They edit the file themselves; do not have them dictate it.
     If a `.env` already exists, they replace the two values in place.
   - **Claude Code cloud session:** save `X_AUTH_TOKEN` and `X_CT0` as environment variables on
     the cloud environment (Settings > Environments > the environment the session uses), then
     start a **new** session. Variables only reach containers built after they are saved; the
     current session will not see them. Values are stored in plain text there.
   - **Cloud session, right now, no new session possible:** `.env` in the repository root works
     too, but only the user can get the value there without it passing through chat. Prefer the
     environment variable route.

4. **Verify without exposing anything:**

   ```bash
   grep -cE '^X_(AUTH_TOKEN|CT0)=.+' .env     # expect 2
   python -m xbookmarks session --check        # expect "check: ok"
   ```

   If the session was previously saved in the browser profile and keeps failing after the
   `.env` is updated, the env values override the profile automatically; no need to delete it.
   If `--check` says `unknown`, X loaded neither bookmarks nor a login redirect; run it once
   more before concluding anything, then show the user the exact output.

5. **Resume the original task** (usually `python -m xbookmarks run ...`). Report the session
   status in words. Do not retry a failing login more than once.

## When to suspect the cookies rather than the code

- `run` exits with code 2 and "not logged in" or "redirected to its login page".
- `run` worked yesterday and fails today with no code change: the user logged out of X in
  that browser, changed their password, or X invalidated the session.
- Exit code 1 with a Playwright or network error is not a cookie problem; fix that first.
