"""Check which NSE/BSE endpoints answer from this machine (used to test GitHub Actions runners)."""
from __future__ import annotations

import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import earnings_tracker as et  # noqa: E402


def main():
    today = et.now_ist().date()
    t = today.strftime("%d-%m-%Y")
    s = (today - timedelta(days=7)).strftime("%d-%m-%Y")
    nse, bse = et.NSEClient(), et.BSEClient()
    checks = [
        ("NSE announcements", lambda: len(nse.api("/api/corporate-announcements",
                                                   {"index": "equities", "from_date": t, "to_date": t}))),
        ("NSE board meetings", lambda: len(nse.api("/api/corporate-board-meetings",
                                                    {"index": "equities", "from_date": s, "to_date": t}))),
        ("NSE results register", lambda: nse.api("/api/integrated-filing-results", {
            "index": "equities", "type": "Integrated Filing- Financials", "from_date": s, "to_date": t,
            "page": 1, "size": 50}).get("totalCount")),
        ("NSE Nifty 500 prices", lambda: len(nse.api("/api/equity-stock-indices", {"index": "NIFTY 500"})["data"])),
        ("NSE index CSV", lambda: len(nse.get(et.NSE_ARCH + "/content/indices/ind_nifty500list.csv").content)),
        ("BSE announcements", lambda: bse.api("/AnnSubCategoryGetData/w", {
            "pageno": 1, "strCat": "Result", "subcategory": "-1", "strPrevDate": today.strftime("%Y%m%d"),
            "strToDate": today.strftime("%Y%m%d"), "strSearch": "P", "strScrip": "", "strType": "C"})
            ["Table1"][0]["ROWCNT"]),
        ("BSE scrip master", lambda: len(bse.api("/ListofScripData/w", {
            "Group": "", "Scripcode": "", "industry": "", "segment": "Equity", "status": "Active"}))),
    ]
    ok = 0
    for name, fn in checks:
        try:
            print("PASS  %-22s %s" % (name, fn()), flush=True)
            ok += 1
        except Exception as e:
            print("FAIL  %-22s %s" % (name, str(e)[:150]), flush=True)
    print("%d/%d reachable" % (ok, len(checks)))


if __name__ == "__main__":
    main()
