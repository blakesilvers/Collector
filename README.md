# RFP Collector: window cleaning & pressure washing bids near Tyler, TX

Pulls open government solicitations (RFPs, RFQs, IFBs) for **window cleaning** and
**pressure/power washing** within **120 miles** of the Fish Window Cleaning Tyler
office (5400 University Blvd, Ste 5, Tyler TX 75707). Each run writes a CSV, a
JSON file and an HTML report, and flags bids it has not reported before.

No third-party Python packages are needed; it runs on Python 3.11+ (Windows, macOS, Linux).

```bash
python -m rfp_collector                    # all sources, settings from config.toml
python -m rfp_collector --include-related  # also janitorial/custodial bids
python -m rfp_collector --history          # also past ESBD window/pressure bids (who buys this work)
python -m rfp_collector --radius 150 --sources esbd,pages -v
```

Reports land in `reports/rfps_YYYY-MM-DD.{html,csv,json}`. Open the HTML file in a browser.

## Where the bids come from

| Source | What it covers | Setup |
|---|---|---|
| **Texas ESBD** (txsmartbuy.gov) | State agencies (TxDOT, HHSC, TFC, universities) and the local Texas buyers that choose to post there | none |
| **SAM.gov** | Federal work: VA clinics, Corps of Engineers lakes, federal courthouses, post offices, Barksdale AFB, Red River Army Depot | free API key |
| **Watched pages** | 33 city, county, ISD and college bid pages around Tyler (Bonfire, CivicPlus, BidNet Direct, IonWave, Jaggaer, Bid Express and plain pages), configured in `config.toml` under `[[pages]]` | none |
| **Portals (manual)** | 10 buyers this tool can't read: City of Tyler, Tyler ISD, Paris, Texarkana, Kilgore College, Greenville, Mount Pleasant, Palestine, Terrell, Lufkin. These are listed in every report as a checklist | register for their alerts |

### Adding a watched page

```toml
[[pages]]
name = "City of Example"
url = "https://www.example-tx.gov/Bids.aspx"
location = "Example, TX"   # used for distance
type = "html"              # html (default) | rss | bonfire
```

`bonfire` takes any `https://<agency>.bonfirehub.com/...` URL and reads the portal's
public JSON feed. CivicPlus `Bids.aspx`, BidNet Direct agency pages, IonWave
`SourcingEvents.aspx?SourceType=1` and Jaggaer public-event pages all work as `html`.

### SAM.gov API key
1. Sign in at sam.gov, open **Account Details**, and generate a **Public API Key**.
2. Set it as an environment variable `SAM_API_KEY` (preferred), or paste it into
   `config.toml` under `[sam] api_key`. Don't commit a real key.
3. Non-federal keys have a small daily request quota. The tool makes roughly
   one request per NAICS code per run and caches the responses for the day in `.cache/`.

## How a bid is judged relevant

* **Keywords** in the title (3 points) or description (2 points). These include window
  cleaning/washing, glass cleaning, high-rise windows, pressure/power/soft washing,
  exterior building cleaning, facade cleaning, parking garage/sidewalk/concrete cleaning,
  graffiti removal and fleet washing.
* **Commodity codes**: NIGP 910-81 *Window Washing* and 910-03 *Building Cleaning,
  Exterior* (3 points). NIGP 962-21 *Steam & Pressure Cleaning*, 910-39 *Janitorial*, and
  NAICS 561720/561790 count as *related* only.
* **Negative patterns** drop false hits: window replacement or tinting, pressure-washer
  *equipment* purchases, Microsoft Windows, windshields, dishwashers.
* A bid needs 2 or more points. *Related*-only bids (janitorial) appear only with
  `--include-related` or `include_related = true`.

Tune this in `[matching]` (`extra_keywords`, `exclude`) or in `rfp_collector/matching.py`.

## How distance is worked out

Listings rarely include coordinates, so location comes from the text:

1. `[agency_locations]` overrides, for agencies whose names mislead
   (e.g. *Stephen F. Austin State University* is in Nacogdoches, not Austin).
2. SAM.gov place-of-performance ZIP, then city. If neither is given, the contracting
   office's ZIP is used and the report labels it as such.
3. City and county names in the agency name and title ("City of Longview",
   "Tyler ISD", "GREGG CO"), matched against bundled U.S. Census Gazetteer data.
4. Watched pages use the location configured for the page.

The nearest location found is used. Bids with **no** detectable location (often statewide
contracts such as "TxDOT Window Cleaning Services") go in a separate
**Location unknown** section so you can check whether they include East Texas sites.
Use `--no-unknown` to hide them.

To rebuild the Census data: `python scripts/build_geodata.py 2024`.

## Running it on a schedule

* **Windows Task Scheduler:** create a daily task running
  `python -m rfp_collector` with *Start in* set to this folder.
* **macOS/Linux cron:** `15 6 * * 1-5 cd /path/to/Collector && python3 -m rfp_collector`

Each run updates `data/rfps.sqlite`, so the report marks only first-time bids as **NEW**.

## Known gaps

* Some local buyers use portals that render only with JavaScript or block automated
  access (PlanetBids, Beacon, OpenGov, and cityoftyler.org itself). They can't be scanned
  here. They are listed under `[[portals]]` and shown in every report as a checklist.
  Registering as a vendor on each one gets you email alerts, which is the reliable path.
* Louisiana (LaPAC), Arkansas and Oklahoma state portals are not integrated.
  Shreveport, Bossier City and Texarkana AR are covered only through watched pages
  and portals.
* Private buyers (hospitals, property managers, commercial real estate) don't post public
  RFPs. This tool finds public-sector work only.
* Watched-page scanning is heuristic: it reads link, row and list text. A page redesign
  can break it silently, and any errors appear at the bottom of the report.
* Many agency pages leave closed bids posted and show no due date. When a listing names
  only past years (e.g. "2024-03 RFP ...") it is treated as expired. Otherwise the report
  shows **"not listed — check posting"**, and you must open the document to see whether it's still open.

## Tests

```bash
python -m unittest discover -s tests -v
```

---

# X bookmarks collector

`python -m xbookmarks` collects the posts you bookmark on X (Twitter) and writes a daily
report of the ones it has not seen before. It costs nothing: it does not use the X API
(which no longer has a free tier). Instead a real browser, logged in as you, opens
`x.com/i/bookmarks` and the data X's own page loads is captured. That is the same route
the free bookmark-export browser extensions take. X's terms of service frown on automated
access, so keep it to one gentle run a day on your own account.

Because the page makes the requests, nothing here depends on X's internal query IDs,
feature flags or request-signing headers, which change often.

## Setup (once)

```bash
pip install playwright
playwright install chromium
python -m xbookmarks login       # a browser window opens; log in to X; it closes itself
```

The session is saved in `data/xbookmarks/profile/` (git-ignored, treat it like a password).

No screen to log in on (a server, a cloud session)? Copy two cookies from a browser where
you are logged in to X (DevTools > Application > Cookies > https://x.com): `auth_token` and
`ct0`. Put them in a file named `.env` in the repository root (copy `.env.example`; the file
is git-ignored), or export them as the environment variables `X_AUTH_TOKEN` and `X_CT0`.
Variables already set in the shell win over the file. The collector loads them into its
browser profile at startup. `auth_token` is equivalent to your password: store it only
where you would store one, and never paste it into a chat.

```bash
cp .env.example .env              # then fill in the two values
python -m xbookmarks session       # where the next run will log in from (never prints values)
python -m xbookmarks session --check   # also asks X whether the session still works
```

When a run says "not logged in" or "redirected to its login page", the cookies have
expired: copy fresh ones into `.env` (both from the same browser session) and re-run
`session --check`. In Claude Code, the `x-session-refresh` skill in `.claude/skills/`
walks through this.
To use your installed Chrome or Edge instead of Playwright's Chromium, set
`channel = "chrome"` (or `"msedge"`) under `[browser]` in `xbookmarks.toml`.

## Every day

```bash
python -m xbookmarks                 # fetch new bookmarks, store them, write today's report
python -m xbookmarks run --all       # once: walk your whole bookmark history
python -m xbookmarks report --days 7 # rebuild a report for the last week, no browser
python -m xbookmarks search "topic"  # grep the store
python -m xbookmarks export          # everything as JSON
python -m xbookmarks session --check # is the saved X session still accepted?
```

Reports land in `reports/bookmarks_YYYY-MM-DD.{md,html,json}`. Each run stops once it
reaches posts already stored, so a daily run only loads a page or two.

### What is behind the links

Most useful bookmarks point somewhere else, so each run also follows the links in new
posts (`[links]` in the config, or `--no-links` to skip) and records what it finds:

* any page: title, description, site name;
* GitHub repositories: description, stars, language, license, last push, archived flag,
  a README excerpt, and signals for an AI-tooling workflow: **Claude Code skill**
  (`SKILL.md`, also inside `tree/<branch>/<folder>` links), **Claude Code plugin**
  (`.claude-plugin/`), **MCP config** (`.mcp.json`), and mentions of MCP or Claude Code in
  the description or README. Dead or private repos are reported as not found.

GitHub's API allows 60 anonymous lookups an hour; set `GITHUB_TOKEN` for 5,000. Without
the API the README and signals still come from raw files. Links that were not described
(limit reached, network down) are picked up by the next run or by
`python -m xbookmarks enrich`, which also backfills links for everything already stored. The store is
`data/xbookmarks/bookmarks.sqlite` and keeps the full text, links, media, quoted post,
engagement counts and (by default) X's raw JSON for every bookmark.

Scheduling on Windows (runs headless at 05:30; the window never shows):

```powershell
.\scripts\install_xbookmarks_task.ps1 -At 05:30
```

macOS/Linux cron: `30 5 * * * cd /path/to/Collector && python3 -m xbookmarks`

## Emailing the report to yourself (optional, needed for the morning digest)

Set `enabled = true`, `username` and `to` under `[email]` in `xbookmarks.toml` (or a
git-ignored `xbookmarks.local.toml`). For Gmail, create an App Password at
myaccount.google.com/apppasswords (2-Step Verification must be on) and put it in the
`XBOOKMARKS_SMTP_PASSWORD` environment variable. Each day with new bookmarks you then get
an email titled `X bookmarks report YYYY-MM-DD (N new)` with the report in the body and the
JSON attached. An assistant with access to that mailbox can turn it into a morning digest.

## Troubleshooting

* **"No X session"** or **"X redirected to its login page"**: run `python -m xbookmarks login` again.
* **"loaded no bookmark data in time"**: run `python -m xbookmarks run --headed` to watch
  what X shows. If X is presenting a challenge page to the headless browser, set
  `headless = false` or `channel = "chrome"` in the config.
* `data/xbookmarks/last_request.json` records the query ID and parameters X's page used in
  the last run, which helps when diagnosing a change on X's side.
