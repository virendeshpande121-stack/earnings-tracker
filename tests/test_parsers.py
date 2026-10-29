"""Parser/classifier tests on saved live samples plus hand-made edge cases.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import earnings_tracker as et  # noqa: E402

SAMPLES = os.path.join(ROOT, "data", "samples")
QEND = date(2026, 9, 30)


def sample(name):
    p = os.path.join(SAMPLES, name)
    if not os.path.exists(p):
        raise unittest.SkipTest("missing sample " + name)
    with open(p, encoding="utf-8") as f:
        return json.load(f)


class Quarter(unittest.TestCase):
    def test_auto(self):
        q = et.quarter_info(date(2026, 10, 28))
        self.assertEqual(q["label"], "Q2 FY27")
        self.assertEqual(q["qend"], QEND)
        self.assertEqual(q["season_start"], date(2026, 10, 1))

    def test_boundaries(self):
        self.assertEqual(et.quarter_info(date(2026, 10, 1))["label"], "Q2 FY27")
        self.assertEqual(et.quarter_info(date(2026, 9, 30))["label"], "Q1 FY27")
        self.assertEqual(et.quarter_info(date(2027, 1, 15))["label"], "Q3 FY27")
        self.assertEqual(et.quarter_info(date(2027, 4, 2))["label"], "Q4 FY27")

    def test_override(self):
        self.assertEqual(et.quarter_info(date(2026, 10, 28), "30-Jun-2026")["label"], "Q1 FY27")


class Dates(unittest.TestCase):
    def test_nse(self):
        d = et.parse_nse_dt("28-Oct-2026 19:40:10")
        self.assertEqual((d.hour, d.minute, str(d.tzinfo)), (19, 40, "Asia/Kolkata"))
        self.assertEqual(et.parse_day("30-SEP-2026"), QEND)
        self.assertEqual(et.parse_nse_dt("2026-10-28 19:40:10"), d)

    def test_bse(self):
        self.assertEqual(et.parse_bse_dt("2026-10-28T19:39:59.26").second, 59)
        self.assertEqual(et.parse_bse_dt("2026-10-28T19:39:59.603").minute, 39)
        self.assertEqual(et.parse_day("28 Oct 2026"), date(2026, 10, 28))


class LiveSamples(unittest.TestCase):
    def test_nse_announcements(self):
        items = sample("nse_announcements.json")
        for k in ("symbol", "desc", "attchmntText", "attchmntFile", "sm_isin", "sort_date", "an_dt"):
            self.assertIn(k, items[0])
        hits = [x for x in items if et.classify_nse(x, QEND)]
        self.assertGreater(len(hits), 10)
        for x in hits:
            self.assertIsNotNone(et.nse_ann_time(x))
            self.assertNotIn(x["desc"], ("Copy of Newspaper Publication", "Investor Presentation"))

    def test_bse_announcements(self):
        res = sample("bse_ann_result.json")
        self.assertIn("ROWCNT", res["Table1"][0])
        hits = [x for x in res["Table"] if et.classify_bse(x, QEND)]
        self.assertGreater(len(hits), 10)
        self.assertTrue(et.bse_link(hits[0]).startswith(et.BSE_ATTACH))
        board = sample("bse_ann_board.json")["Table"]
        for x in board:
            if x["SUBCATNAME"] == "Board Meeting":   # intimations
                self.assertFalse(et.classify_bse(x, QEND), x["NEWSSUB"])

    def test_board_meetings(self):
        rows = sample("nse_board_meetings.json")
        for k in ("bm_symbol", "bm_date", "bm_purpose", "bm_desc", "sm_isin", "bm_timestamp"):
            self.assertIn(k, rows[0])
        m = et.pick_meetings(rows, date(2026, 10, 1))
        self.assertGreater(len(m), 500)
        for v in m.values():
            self.assertGreaterEqual(v["date"], "2026-10-01")

    def test_register(self):
        reg = sample("nse_integrated_fin.json")
        self.assertIn("totalCount", reg)
        r = reg["data"][0]
        for k in ("symbol", "qe_Date", "broadcast_Date", "type"):
            self.assertIn(k, r)
        self.assertEqual(et.parse_day(r["qe_Date"]), QEND)

    def test_masters(self):
        for fn in ("ind_nifty500list.csv", "EQUITY_L.csv"):
            p = os.path.join(SAMPLES, fn)
            if not os.path.exists(p):
                self.skipTest(fn)
        with open(os.path.join(SAMPLES, "EQUITY_L.csv"), encoding="utf-8") as f:
            rows = et.read_csv_rows(f.read())
        self.assertIn("ISIN NUMBER", rows[0])   # header had a leading space
        with open(os.path.join(SAMPLES, "ind_nifty500list.csv"), encoding="utf-8") as f:
            n500 = et.read_csv_rows(f.read())
        self.assertGreaterEqual(len(n500), 495)
        self.assertIn("ISIN Code", n500[0])
        scrips = sample("bse_scrips.json")
        self.assertTrue({"SCRIP_CD", "ISIN_NUMBER", "Mktcap", "scrip_id"} <= set(scrips[0]))


def nse_item(desc, text, ts="2026-10-28 15:30:00"):
    return {"desc": desc, "attchmntText": text, "sort_date": ts, "sm_isin": "INE000A01011", "symbol": "ABC"}


def bse_item(cat, sub, subj, ts="2026-10-28T15:28:00.12"):
    return {"CATEGORYNAME": cat, "SUBCATNAME": sub, "NEWSSUB": subj, "HEADLINE": "", "DissemDT": ts,
            "SCRIP_CD": 500001, "ATTACHMENTNAME": "x.pdf"}


class EdgeCases(unittest.TestCase):
    def test_intimation_not_declared(self):
        self.assertFalse(et.classify_bse(bse_item(
            "Board Meeting", "Board Meeting",
            "ABC Ltd - 500001 - Board Meeting Intimation for To Consider And Approve The Unaudited Financial "
            "Results For The Quarter Ended September 30, 2026"), QEND))
        self.assertFalse(et.classify_nse(nse_item(
            "Outcome of Board Meeting",
            "Prior intimation: meeting of the Board will be held on Nov 5, 2026 to consider financial results"), QEND))
        self.assertFalse(et.classify_bse(bse_item(
            "Board Meeting", "Outcome of Board Meeting",
            "ABC Ltd - Outcome: Board will consider the financial results on 5th November 2026"), QEND))

    def test_newspaper_not_declared(self):
        self.assertFalse(et.classify_nse(nse_item(
            "Copy of Newspaper Publication",
            "ABC has submitted newspaper publication of unaudited financial results for Sept 30, 2026"), QEND))
        self.assertFalse(et.classify_bse(bse_item(
            "Result", "Financial Results",
            "ABC Ltd - Newspaper Publication Of Unaudited Financial Results For Quarter Ended 30.09.2026"), QEND))
        self.assertFalse(et.classify_bse(bse_item(
            "Result", "Financial Results", "ABC Ltd - Results-Delay in Financial Results"), QEND))

    def test_real_results_count(self):
        self.assertTrue(et.classify_nse(nse_item(
            "Outcome of Board Meeting",
            "ABC Limited has submitted to the Exchange, the financial results for the period ended "
            "September 30, 2026."), QEND))
        self.assertTrue(et.classify_nse(nse_item("Integrated Filing- Financial", ""), QEND))
        self.assertTrue(et.classify_bse(bse_item(
            "Result", "Financial Results",
            "ABC Ltd - 500001 - Unaudited Financial Results For The Quarter Ended 30Th September,2026"), QEND))

    def test_prior_quarter_rejected(self):
        self.assertFalse(et.classify_nse(nse_item(
            "Outcome of Board Meeting",
            "XYZ Limited has submitted to the Exchange, the financial results for the period ended Jun 30, 2026."),
            QEND))

    def test_earlier_bse_beats_nse(self):
        decl = {}
        nse_ts = et.parse_nse_dt("28-Oct-2026 15:30:00")
        bse_ts = et.parse_bse_dt("2026-10-28T15:28:00.12")
        self.assertTrue(et.merge_declaration(decl, "INE000A01011", nse_ts, "NSE", "nse.pdf", "nse hl"))
        self.assertFalse(et.merge_declaration(decl, "INE000A01011", bse_ts, "BSE", "bse.pdf", "bse hl"))
        d = decl["INE000A01011"]
        self.assertEqual(d["src"], "BSE")
        self.assertEqual(et.from_iso(d["ts"]), bse_ts)
        self.assertEqual((d["nse"], d["bse"]), ("nse.pdf", "bse.pdf"))
        self.assertEqual(et.from_iso(d["nse_ts"]), nse_ts)
        # a later duplicate never moves the timestamp forward
        et.merge_declaration(decl, "INE000A01011", et.parse_nse_dt("28-Oct-2026 18:00:00"), "NSE", None, "")
        self.assertEqual(d["src"], "BSE")
        self.assertEqual(et.from_iso(d["nse_ts"]), nse_ts)

    def test_postponed_meeting(self):
        rows = [
            {"sm_isin": "INE1", "bm_date": "05-Nov-2026", "bm_purpose": "Financial Results", "bm_desc": "",
             "bm_timestamp": "20-Oct-2026 10:00:00"},
            {"sm_isin": "INE1", "bm_date": "12-Nov-2026", "bm_purpose": "Board meeting Rescheduled",
             "bm_desc": "Meeting to approve financial results rescheduled", "bm_timestamp": "03-Nov-2026 18:00:00"},
            {"sm_isin": "INE2", "bm_date": "20-Sep-2026", "bm_purpose": "Financial Results", "bm_desc": "",
             "bm_timestamp": "10-Sep-2026 10:00:00"},      # before season start: ignored
            {"sm_isin": "INE3", "bm_date": "10-Nov-2026", "bm_purpose": "Fund Raising", "bm_desc": "QIP",
             "bm_timestamp": "01-Nov-2026 10:00:00"},       # not result-related: ignored
        ]
        m = et.pick_meetings(rows, date(2026, 10, 1))
        self.assertEqual(m["INE1"]["date"], "2026-11-12")
        self.assertEqual(m["INE1"]["prev_date"], "2026-11-05")
        self.assertNotIn("INE2", m)
        self.assertNotIn("INE3", m)
        # postponement seen only across polls (state held the old date)
        m2 = et.pick_meetings(rows[1:2], date(2026, 10, 1),
                              {"INE1": {"date": "2026-11-07", "src": "NSE", "prev_date": None}})
        self.assertEqual(m2["INE1"]["prev_date"], "2026-11-07")

    def test_status(self):
        t = date(2026, 10, 28)
        meet = {"A": {"date": "2026-10-28"}, "B": {"date": "2026-11-02"}, "C": {"date": "2026-10-20"},
                "D": {"date": "2026-10-28"}}
        decl = {"D": {"ts": "2026-10-28T15:00:00+05:30"}}
        self.assertEqual([et.status_for(i, meet, decl, t) for i in "ABCDE"],
                         ["AWAITED", "UPCOMING", "OVERDUE", "DECLARED", "UNSCHEDULED"])


if __name__ == "__main__":
    unittest.main()
