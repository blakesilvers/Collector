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
| **Watched pages** | City, county, ISD and college bid pages around Tyler, configured in `config.toml` under `[[pages]]` | none |
| **Portals (manual)** | Buyers on login or JavaScript portals (Bonfire, BidNet, IonWave, Public Purchase). These are listed in the report as a checklist | register for their alerts |

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

* Many local buyers use portals that need a login or render with JavaScript
  (Bonfire, BidNet Direct, IonWave, Public Purchase, DemandStar). They can't be scanned
  here. They are listed under `[[portals]]` and shown in every report as a checklist.
  Registering as a vendor on each one gets you email alerts, which is the reliable path.
* Louisiana (LaPAC), Arkansas and Oklahoma state portals are not integrated.
  Shreveport, Bossier City and Texarkana AR are covered only through watched pages
  and portals.
* Private buyers (hospitals, property managers, commercial real estate) don't post public
  RFPs. This tool finds public-sector work only.
* Watched-page scanning is heuristic: it reads link, row and list text. A page redesign
  can break it silently, and any errors appear at the bottom of the report.

## Tests

```bash
python -m unittest discover -s tests -v
```
