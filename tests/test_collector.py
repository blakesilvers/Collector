import json
import unittest
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from rfp_collector import cli, geo
from rfp_collector.matching import score
from rfp_collector.models import Opportunity
from rfp_collector.sources import esbd, pages, sam

TYLER = (32.3144, -95.2386)


def opp(title, codes=(), desc=""):
    o = Opportunity(source="t", source_id=title, title=title, codes=list(codes), description=desc)
    score(o)
    return o


class MatchingTests(unittest.TestCase):
    def test_strong_matches(self):
        for t in ["Window Cleaning Services", "Interior and Exterior Window Cleaning Services",
                  "Annual Contract for Window Washing", "RFP - Power Washing Services",
                  "Pressure Washing of Sidewalks", "Parking Garage Cleaning", "Exterior Building Cleaning",
                  "Soft Wash of Roofs", "High-Rise Window and Glass Services", "Graffiti Removal Services"]:
            self.assertGreaterEqual(opp(t).match_score, 3, t)

    def test_category(self):
        self.assertEqual(opp("Window Cleaning Services").category, "window")
        self.assertEqual(opp("Power Washing Services").category, "pressure")
        self.assertEqual(opp("Window Cleaning and Pressure Washing").category, "both")
        self.assertEqual(opp("Custodial Services").category, "related")

    def test_negatives(self):
        for t in ["Window Replacement Project", "Purchase of Pressure Washers", "Windshield Repair",
                  "Microsoft Windows Server Licenses", "Dishwasher Maintenance", "Window Tinting Services",
                  "Window Unit Air Conditioners", "Road Striping Contract"]:
            o = opp(t)
            self.assertNotIn(o.category, ("window", "pressure", "both"), t)

    def test_codes(self):
        o = opp("General Services", codes=["91081-Window Washing Services"])
        self.assertEqual((o.match_score, o.category), (3, "window"))
        self.assertEqual(opp("Stuff", codes=["91039-Janitorial/Custodial Services"]).category, "related")

    def test_description_match(self):
        o = opp("Facility Services", desc="Contractor shall provide exterior window washing quarterly.")
        self.assertGreaterEqual(o.match_score, 2)
        self.assertTrue(all(t.endswith("(description)") for t in o.match_terms))

    def test_exclude(self):
        o = Opportunity(source="t", source_id="1", title="Window Cleaning Services - Lubbock")
        score(o, exclude=["lubbock"])
        self.assertEqual(o.match_score, 0)


class GeoTests(unittest.TestCase):
    def labels(self, text):
        return [l.label() for l in geo.find_locations(text, ["TX"])]

    def test_agency_names(self):
        self.assertEqual(self.labels("City of Longview"), ["Longview, TX"])
        self.assertEqual(self.labels("Tyler ISD"), ["Tyler, TX"])
        self.assertEqual(self.labels("Smith County"), ["Smith County, TX"])
        self.assertIn("Dallas, TX", self.labels("Head Start Greater Dallas, Inc"))

    def test_txdot_county_abbrev(self):
        self.assertEqual(self.labels("STATE LET MAINTENANCE CONTRACT, GREGG CO, MOWING"), ["Gregg County, TX"])

    def test_ambiguous_words_need_context(self):
        self.assertEqual(self.labels("Center for Advanced Studies"), [])
        self.assertEqual(self.labels("City of Center"), ["Center, TX"])

    def test_distance(self):
        tyler = geo.lookup_place("Tyler", "TX")
        longview = geo.lookup_place("Longview", "TX")
        d = geo.distance_miles(tyler.lat, tyler.lon, longview.lat, longview.lon)
        self.assertTrue(30 < d < 40, d)

    def test_zip(self):
        self.assertIsNotNone(geo.lookup_zip("75701"))
        self.assertIsNone(geo.lookup_zip("00000"))


ESBD_LINE = {
    "internalid": "1", "title": "Window Cleaning Services", "solicitationId": "ABC-1",
    "responseDue": "12/01/2099", "responseTime": "2:00 PM", "agencyName": "City of Longview - M1234",
    "statusName": "Posted", "postingDate": "10/01/2026", "nigpCodes": "91081-Window Washing Services;", "url": "",
}

SAM_ROW = {
    "noticeId": "n1", "title": "Window Washing - VA Tyler CBOC", "solicitationNumber": "36C1",
    "fullParentPathName": "VETERANS AFFAIRS", "postedDate": "2026-10-01",
    "responseDeadLine": "2099-11-05T14:00:00-05:00", "naicsCode": "561720", "classificationCode": "S201",
    "type": "Solicitation", "typeOfSetAsideDescription": "Total Small Business",
    "placeOfPerformance": {"city": {"code": "74144", "name": "Tyler"}, "state": {"code": "TX", "name": "Texas"}, "zip": "75701"},
    "officeAddress": {"zipcode": "76102", "city": "FORT WORTH", "state": "TX"},
    "uiLink": "https://sam.gov/opp/n1/view",
}


class SourceParsingTests(unittest.TestCase):
    def test_esbd_line(self):
        o = esbd._to_opp(ESBD_LINE)
        self.assertEqual(o.agency, "City of Longview")
        self.assertEqual(o.due, date(2099, 12, 1))
        self.assertEqual(o.url, "https://www.txsmartbuy.gov/esbd/ABC-1")
        self.assertEqual([c.label() for c in o.candidates], ["Longview, TX"])

    def test_sam_row(self):
        o = sam._to_opp(SAM_ROW)
        self.assertEqual(o.due, date(2099, 11, 5))
        self.assertEqual(o.candidates[0].kind, "zip")
        self.assertIn("561720", o.codes)

    def test_sam_office_fallback(self):
        row = dict(SAM_ROW, placeOfPerformance={})
        o = sam._to_opp(row)
        self.assertIn("contracting office", o.candidates[0].evidence)

    def test_html_page(self):
        page = {"name": "City of Tyler", "url": "https://example.gov/bids", "location": "Tyler, TX"}
        html = b"""<html><body><nav><a href="/">Home page link</a></nav><table>
        <tr><td><a href="/bid/7">Bid 26-07 Window Cleaning City Hall</a></td><td>Closes 12/15/2099</td></tr>
        <tr><td><a href="/bid/8">Bid 26-08 Asphalt Overlay</a></td><td>Closes 12/20/2099</td></tr>
        </table><script>var x = "pressure washing";</script></body></html>"""
        with mock.patch.object(pages, "request", return_value=html):
            items = pages.fetch_page(page)
        for i in items:
            score(i)
        matched = [i for i in items if i.match_score >= 3]
        self.assertTrue(any(i.url == "https://example.gov/bid/7" for i in matched))
        self.assertFalse(any("Asphalt" in i.title for i in matched))
        self.assertFalse(any("var x" in i.title for i in items))
        row = next(i for i in matched if i.title.startswith("Bid 26-07 Window Cleaning City Hall Closes"))
        self.assertEqual(row.due, date(2099, 12, 15))

    def test_rss_page(self):
        page = {"name": "Smith County", "url": "https://example.gov/rss", "location": "Tyler, TX", "type": "rss"}
        rss = b"""<?xml version="1.0"?><rss><channel><item><title>Pressure Washing - Courthouse</title>
        <link>https://example.gov/bids/1</link><description>Due 01/05/2099</description>
        <pubDate>Mon, 26 Oct 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        with mock.patch.object(pages, "request", return_value=rss):
            items = pages.fetch_page(page)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].due, date(2099, 1, 5))
        self.assertEqual(items[0].posted, date(2026, 10, 26))


class PipelineTests(unittest.TestCase):
    def test_end_to_end(self):
        lines = [
            ESBD_LINE,                                                                   # Longview, ~35 mi
            dict(ESBD_LINE, solicitationId="F", agencyName="City of Lubbock - M0007"),    # far
            dict(ESBD_LINE, solicitationId="U", agencyName="Texas Department of Transportation"),  # unknown
            dict(ESBD_LINE, solicitationId="O", agencyName="Stephen F. Austin State University - 755"),  # override
            dict(ESBD_LINE, solicitationId="P", responseDue="01/01/2001"),               # past due
            dict(ESBD_LINE, solicitationId="X", title="Asphalt Paving", nigpCodes=""),   # irrelevant
        ]
        with TemporaryDirectory() as tmp:
            cfgp = Path(tmp) / "config.toml"
            cfgp.write_text(f"""
sources = ["esbd"]
radius_miles = 120
[office]
name = "Tyler"
lat = {TYLER[0]}
lon = {TYLER[1]}
[agency_locations]
"Stephen F. Austin" = "Nacogdoches, TX"
""")
            with mock.patch.object(esbd, "get_json", return_value={"lines": lines, "totalRecordsFound": len(lines), "recordsPerPage": 24}):
                rc = cli.main(["--config", str(cfgp)])
                self.assertEqual(rc, 0)
                data = json.loads(next((Path(tmp) / "reports").glob("*.json")).read_text())
                near = {r["source_id"]: r for r in data["within_radius"]}
                self.assertEqual(set(near), {"ABC-1", "O"})
                self.assertEqual(near["O"]["location"], "Nacogdoches, TX")
                self.assertTrue(near["ABC-1"]["is_new"])
                self.assertEqual([r["source_id"] for r in data["location_unknown"]], ["U"])
                # second run: same bids are no longer new
                cli.main(["--config", str(cfgp)])
                data = json.loads(next((Path(tmp) / "reports").glob("*.json")).read_text())
                self.assertFalse(any(r["is_new"] for r in data["within_radius"]))


if __name__ == "__main__":
    unittest.main()
