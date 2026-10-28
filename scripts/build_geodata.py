"""Rebuild the bundled geodata from the U.S. Census Gazetteer files.

    python scripts/build_geodata.py [YEAR]

Downloads the national place, county and ZCTA gazetteers and writes compact
gzipped CSVs (name/state/lat/lon) into rfp_collector/data/.
"""
import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

YEAR = sys.argv[1] if len(sys.argv) > 1 else "2024"
BASE = f"https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{YEAR}_Gazetteer/"
OUT = Path(__file__).resolve().parent.parent / "rfp_collector" / "data"

# Census LSAD suffixes appended to place names ("Tyler city", "Lindale town").
PLACE_SUFFIXES = (" city and borough", " consolidated government", " metropolitan government",
                  " unified government", " urban county", " city", " town", " village",
                  " borough", " CDP", " municipality", " comunidad", " zona urbana")


def fetch_rows(kind):
    url = f"{BASE}{YEAR}_Gaz_{kind}_national.zip"
    print("downloading", url)
    raw = urllib.request.urlopen(url, timeout=120).read()
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        text = zf.read(zf.namelist()[0]).decode("latin-1")
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    for row in reader:
        yield dict(zip(header, (c.strip() for c in row)))


def strip_suffix(name):
    for suf in PLACE_SUFFIXES:
        if name.endswith(suf):
            return name[: -len(suf)]
    return name


def write(name, header, rows):
    path = OUT / name
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print("wrote", path, len(rows), "rows")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    places = [(strip_suffix(r["NAME"]), r["USPS"], r["INTPTLAT"], r["INTPTLONG"]) for r in fetch_rows("place")]
    write("places.csv.gz", ["name", "state", "lat", "lon"], places)
    counties = [(r["NAME"], r["USPS"], r["INTPTLAT"], r["INTPTLONG"]) for r in fetch_rows("counties")]
    write("counties.csv.gz", ["name", "state", "lat", "lon"], counties)
    zctas = [(r["GEOID"], r["INTPTLAT"], r["INTPTLONG"]) for r in fetch_rows("zcta")]
    write("zcta.csv.gz", ["zip", "lat", "lon"], zctas)


if __name__ == "__main__":
    main()
