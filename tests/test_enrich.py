"""Tests for the results snapshot, concall time and deck pointer parsers.

Run: python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from datetime import date

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import enrich  # noqa: E402

SAMPLES = os.path.join(ROOT, "data", "samples")
HINT = date(2026, 10, 1)


class TestFinancials(unittest.TestCase):
    def test_tvs_xbrl(self):
        p = os.path.join(SAMPLES, "xbrl_tvsmotor_q2fy27_consol.xml")
        if not os.path.exists(p):
            raise unittest.SkipTest("missing sample")
        with open(p, encoding="utf-8") as f:
            facts = enrich.xbrl_facts(f.read(), date(2026, 7, 1), date(2026, 9, 30))
        m = enrich.fin_metrics(facts)
        self.assertEqual(m["kind"], "corp")
        self.assertAlmostEqual(m["rev"], 19474.44)
        # EBITDA ex-OI = PBT before exceptional + finance costs + D&A - other income
        self.assertAlmostEqual(m["ebitda"], (17956000000 + 6696600000 + 3582300000 - 979900000) / 1e7, places=2)
        self.assertAlmostEqual(m["margin"], 14.0, places=1)
        self.assertAlmostEqual(m["pat"], 1194.51)   # attributable to owners, not total
        self.assertEqual(m["eps"], 25.14)

    def test_bank_and_lender(self):
        bank = enrich.fin_metrics({"InterestEarned": 100e7, "InterestExpended": 60e7,
                                   "OperatingProfitBeforeProvisionAndContingencies": 30e7,
                                   "ProfitLossFromOrdinaryActivitiesBeforeTax": 20e7, "ProfitLossForThePeriod": 15e7,
                                   "BasicEarningsPerShareAfterExtraordinaryItems": 3.1})
        self.assertEqual((bank["kind"], bank["rev"], bank["ebitda"], bank["margin"]), ("bank", 40.0, 30.0, None))
        nbfc = enrich.fin_metrics({"InterestEarned": 90e7, "RevenueFromOperations": 100e7, "FinanceCosts": 40e7,
                                   "ProfitBeforeExceptionalItemsAndTax": 25e7, "ProfitBeforeTax": 25e7,
                                   "ImpairmentOnFinancialInstruments": 5e7, "ProfitLossForPeriod": 19e7}, lender=True)
        self.assertEqual((nbfc["kind"], nbfc["rev"], nbfc["ebitda"], nbfc["margin"]), ("lender", 100.0, 30.0, None))

    def test_growth(self):
        g = enrich.growth({"rev": 110, "ebitda": 22, "margin": 20.0, "pbt": 10, "pat": 8, "eps": 2},
                          {"rev": 100, "ebitda": 25, "margin": 25.0, "pbt": -5, "pat": 8, "eps": 2})
        self.assertEqual(g["rev"], 10.0)
        self.assertEqual(g["ebitda"], -12.0)
        self.assertEqual(g["margin_bps"], -500)
        self.assertIsNone(g["pbt"])   # loss base: growth not meaningful
        self.assertEqual(g["pat"], 0.0)

    def test_quarters(self):
        self.assertEqual(enrich.shift_quarter(date(2026, 9, 30), 1), date(2026, 6, 30))
        self.assertEqual(enrich.shift_quarter(date(2026, 9, 30), 4), date(2025, 9, 30))
        self.assertEqual(enrich.shift_quarter(date(2026, 3, 31), 1), date(2025, 12, 31))
        self.assertEqual(enrich.quarter_start(date(2026, 12, 31)), date(2026, 10, 1))


class TestConcall(unittest.TestCase):
    def test_slots(self):
        cases = [
            ("Conference Call on Thursday, 29th October, 2026 at 10:30 a.m. (IST).", ("2026-10-29", "10:30")),
            ("will hold Investors/Analysts Calls at 04:00 p.m. (IST) on Tuesday, November 03, 2026", ("2026-11-03", "16:00")),
            ("scheduled on October 29, 2826 (Thursday) at 1600 hrs IST", ("2026-10-29", "16:00")),   # typo'd year
            ("Date: 28.10.2026 Time: 17:30 hrs", ("2026-10-28", "17:30")),
            ("on 30th October 2026 at 4 PM", ("2026-10-30", "16:00")),
        ]
        for text, want in cases:
            self.assertIn(want, enrich.call_slots(text, HINT), text)

    def test_no_time(self):
        self.assertEqual(enrich.call_slots("a presentation shall be made on Wednesday, 28th October, 2026 "
                                           "after the Board Meeting", HINT), [])

    def test_pick(self):
        calls = [{"kind": "schedule", "slots": [["2026-10-12", "11:00"]]},   # a different (non-results) meet
                 {"kind": "schedule", "slots": [["2026-11-02", "15:00"]], "earn": False},   # investor meet, not the call
                 {"kind": "schedule", "slots": [["2026-10-28", "17:00"]], "link": "x.pdf", "earn": True},
                 {"kind": "recording", "ts": "2026-10-28T21:25:00+05:30"}]
        c = enrich.pick_concall(date(2026, 10, 28), calls)
        self.assertEqual((c["date"], c["time"], c["kind"]), ("2026-10-28", "17:00", "scheduled"))
        c = enrich.pick_concall(date(2026, 10, 28), calls[1:2])
        self.assertIsNone(c)
        c = enrich.pick_concall(date(2026, 10, 28), calls[3:])
        self.assertEqual((c["date"], c["time"], c["posted"]), ("2026-10-28", None, "21:25"))


class TestDeck(unittest.TestCase):
    def test_points(self):
        pages = [
            "Disclaimer\nThis presentation contains forward-looking statements and we expect nothing.",
            "Executive Summary\n• Consolidated revenue grew 14% YoY, with margins expanding 40 bps to 14%.\n"
            "• GDP growth remained strong at 7% and the economy is expected to grow.\n",
            "Outlook\n• We expect demand momentum to sustain in H2 and remain confident of double-digit growth in FY27.",
        ]
        pts = enrich.deck_points(pages)
        self.assertEqual([p["tag"] for p in pts], ["Highlights", "Outlook"])
        self.assertIn("revenue grew 14%", pts[0]["text"])
        self.assertIn("We expect demand", pts[1]["text"])

    def test_image_only(self):
        self.assertEqual(enrich.deck_points(["", "  ", "Q2 FY27"]), [])


if __name__ == "__main__":
    unittest.main()
