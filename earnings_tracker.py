#!/usr/bin/env python3
"""Alphapeak Earnings Tracker: live quarterly-results status for Nifty 500 + watchlist.

Runs a background poller against NSE/BSE and serves static/index.html plus a JSON API
on 127.0.0.1. Python 3.9+, `requests` + stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import smtplib
import ssl
import sys
import threading
import time
import traceback
import webbrowser
from datetime import date, datetime, timedelta
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

try:
    import requests
except ImportError:  # demo mode and the unit tests work without it
    requests = None

IST = ZoneInfo("Asia/Kolkata")
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
STATIC = os.path.join(BASE, "static")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36")
NSE = "https://www.nseindia.com"
NSE_ARCH = "https://nsearchives.nseindia.com"
NIFTYIDX = "https://www.niftyindices.com/IndexConstituent/"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api"
BSE_ATTACH = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"

INDEX_FILES = {
    "N500": "ind_nifty500list.csv",
    "N100": "ind_nifty100list.csv",
    "MID150": "ind_niftymidcap150list.csv",
    "SML250": "ind_niftysmallcap250list.csv",
}

DEFAULT_CONFIG = {
    "port": 8765,
    "fast_poll_seconds": 60,
    "slow_poll_minutes": 15,
    "night_hours_ist": [1, 7],
    "lookahead_days": 45,
    "quarter_end_override": "",
    "alert_groups": ["WATCH", "N100"],
    "email": {
        "enabled": False,
        "smtp_user": "",
        "smtp_app_password": "",
        "to": "",
        "groups": ["WATCH", "N100"],
    },
}


def log(*a):
    print(datetime.now(IST).strftime("%H:%M:%S"), *a, flush=True)


def now_ist() -> datetime:
    return datetime.now(IST)


# ---------------------------------------------------------------- date parsing

def parse_nse_dt(s) -> datetime | None:
    """'28-Oct-2026 19:40:10' / '30-SEP-2026' / '2026-10-28 19:40:10' -> aware IST."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M", "%d-%b-%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=IST)
        except ValueError:
            pass
    return None


def parse_bse_dt(s) -> datetime | None:
    """'2026-10-28T19:39:59.26' (fraction of any length) -> aware IST."""
    if not s:
        return None
    s = str(s).strip().split(".")[0]
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=IST)
    except ValueError:
        return None


def parse_day(s) -> date | None:
    """'05-Dec-2026', '28 Oct 2026', '30-SEP-2026' or ISO -> date."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%d-%b-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return None


def iso(dt) -> str | None:
    return dt.isoformat() if dt else None


def from_iso(s) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=IST)
    except ValueError:
        return None


# ---------------------------------------------------------------- quarter

Q_NAMES = {1: "First Quarter", 2: "Second Quarter", 3: "Third Quarter", 4: "Fourth Quarter"}


def quarter_info(today: date, override: str = "") -> dict:
    qend = parse_day(override) if override else None
    if not qend:
        y = today.year
        ends = [date(y - 1, 12, 31), date(y, 3, 31), date(y, 6, 30), date(y, 9, 30), date(y, 12, 31)]
        qend = max(e for e in ends if e < today)
    qn = {6: 1, 9: 2, 12: 3, 3: 4}[qend.month]
    fy = qend.year + 1 if qend.month >= 4 else qend.year
    return {
        "qend": qend,
        "season_start": qend + timedelta(days=1),
        "label": "Q%d FY%02d" % (qn, fy % 100),
        "qnum": qn,
        "qname": Q_NAMES[qn],
    }


# ---------------------------------------------------------------- classifier

RESULT_TXT = re.compile(
    r"financial\s+results?|un-?\s?audited|audited\s+(?:standalone|consolidated|financial)"
    r"|results?\s+for\s+the\s+(?:quarter|period|half|year)|quarterly\s+results?", re.I)
EXCLUDE_TXT = re.compile(
    r"intimation\s+of\s+(?:the\s+)?board|board\s+meeting\s+intimation|prior\s+intimation"
    r"|(?:to|will|shall)\s+consider|will\s+be\s+held|is\s+scheduled|scheduled\s+to\s+be|proposed\s+to\s+be"
    r"|newspaper|news\s+paper|publication"
    r"|earnings?\s+call|conference\s+call|con\.?\s*call|concall|transcript|audio|video\s+recording"
    r"|investor\s+presentation|earnings\s+presentation|analyst|investors?\s+meet"
    r"|trading\s+window|reasons?\s+for\s+(?:the\s+)?delay|delay\s+in|non-submission", re.I)
NSE_RESULT_DESCS = ("financial result updates", "integrated filing- financial", "integrated filing - financial",
                    "integrated filing-financial")
MEETING_TXT = re.compile(r"financial\s+results?|\bresults\b|un-?\s?audited|audited", re.I)

_MON = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_QE_WORD = re.compile(r"\b(mar|march|jun|june|sep|sept|september|dec|december)\b\.?[\s,]*(?:\d{1,2}(?:st|nd|rd|th)?)?"
                      r"[\s,.\-]*(20\d\d)", re.I)
_QE_NUM = re.compile(r"\b(31|30)[./-](03|06|09|12)[./-](20\d\d)\b")


def mentioned_quarter_ends(text: str) -> set:
    out = set()
    for m in _QE_WORD.finditer(text or ""):
        out.add((_MON[m.group(1)[:3].lower()], int(m.group(2))))
    for m in _QE_NUM.finditer(text or ""):
        out.add((int(m.group(2)), int(m.group(3))))
    return out


def wrong_period(text: str, qend: date) -> bool:
    """True when the text names quarter-end dates and none is the current one."""
    found = mentioned_quarter_ends(text)
    return bool(found) and (qend.month, qend.year) not in found


def is_result_text(text: str, qend: date, need_result_words=True) -> bool:
    if not text:
        return not need_result_words
    if EXCLUDE_TXT.search(text):
        return False
    if need_result_words and not RESULT_TXT.search(text):
        return False
    return not wrong_period(text, qend)


def classify_nse(item: dict, qend: date) -> bool:
    desc = (item.get("desc") or "").strip().lower()
    text = item.get("attchmntText") or ""
    if any(desc.startswith(d) for d in NSE_RESULT_DESCS):
        return is_result_text(text, qend, need_result_words=False)
    if desc.startswith("outcome of board meeting"):
        return is_result_text(text, qend)
    return False


def classify_bse(item: dict, qend: date) -> bool:
    cat = (item.get("CATEGORYNAME") or "").strip().lower()
    sub = (item.get("SUBCATNAME") or "").strip().lower()
    text = "%s %s" % (item.get("NEWSSUB") or "", item.get("HEADLINE") or "")
    if cat == "result":
        return is_result_text(text, qend, need_result_words=False)
    if cat == "board meeting" and sub.startswith("outcome"):
        return is_result_text(text, qend)
    return False


def nse_ann_time(item: dict) -> datetime | None:
    return parse_nse_dt(item.get("sort_date")) or parse_nse_dt(item.get("an_dt"))


def bse_ann_time(item: dict) -> datetime | None:
    return parse_bse_dt(item.get("DissemDT")) or parse_bse_dt(item.get("NEWS_DT"))


def bse_link(item: dict) -> str | None:
    a = (item.get("ATTACHMENTNAME") or "").strip()
    return BSE_ATTACH + a if a else None


def is_result_meeting(item: dict) -> bool:
    return bool(MEETING_TXT.search("%s %s" % (item.get("bm_purpose") or "", item.get("bm_desc") or "")))


def pick_meetings(nse_rows: list, season_start: date, prev: dict | None = None) -> dict:
    """NSE intimations -> {isin: {date, prev_date, src, ts, purpose}}.

    The intimation with the latest bm_timestamp wins; an earlier intimation (or the date
    previously held in state) with a different date becomes prev_date.
    """
    prev = prev or {}
    by_isin = {}
    for r in nse_rows:
        isin = (r.get("sm_isin") or "").strip()
        d = parse_day(r.get("bm_date"))
        if not isin or not d or d < season_start or not is_result_meeting(r):
            continue
        ts = parse_nse_dt(r.get("bm_timestamp")) or datetime(1970, 1, 1, tzinfo=IST)
        by_isin.setdefault(isin, []).append((ts, d, r))
    out = {}
    for isin, lst in by_isin.items():
        lst.sort(key=lambda x: x[0])
        ts, d, r = lst[-1]
        prev_date = None
        for ts2, d2, _ in reversed(lst[:-1]):
            if d2 != d:
                prev_date = d2.isoformat()
                break
        old = prev.get(isin)
        if old and old.get("src") == "NSE":
            if old.get("date") and old["date"] != d.isoformat():
                prev_date = old["date"]
            elif not prev_date and old.get("prev_date"):
                prev_date = old["prev_date"]
        out[isin] = {"date": d.isoformat(), "prev_date": prev_date, "src": "NSE", "ts": iso(ts),
                     "purpose": (r.get("bm_purpose") or "")[:80]}
    return out


def merge_declaration(declared: dict, isin: str, ts: datetime, src: str, link: str | None,
                      headline: str, detail: str | None = None, seen: datetime | None = None) -> bool:
    """Record a results filing. Earliest timestamp wins; keep links from both exchanges.
    Returns True when this is the first time the company is seen as declared."""
    k = "nse" if src == "NSE" else "bse"
    d = declared.get(isin)
    if d is None:
        declared[isin] = {"ts": iso(ts), "src": src, "nse": None, "bse": None, "nse_ts": None, "bse_ts": None,
                          "headline": (headline or "")[:300], "detail": detail,
                          "seen": iso(seen or now_ist())}
        d = declared[isin]
        d[k] = link
        d[k + "_ts"] = iso(ts)
        return True
    if link and not d.get(k):
        d[k] = link
    cur_k = from_iso(d.get(k + "_ts"))
    if cur_k is None or ts < cur_k:
        d[k + "_ts"] = iso(ts)
    if ts < from_iso(d["ts"]):
        d["ts"] = iso(ts)
        d["src"] = src
        if headline:
            d["headline"] = headline[:300]
    if detail:
        d["detail"] = detail
    return False


def status_for(isin: str, meetings: dict, declared: dict, today: date) -> str:
    if isin in declared:
        return "DECLARED"
    m = meetings.get(isin)
    if not m or not m.get("date"):
        return "UNSCHEDULED"
    d = date.fromisoformat(m["date"])
    if d == today:
        return "AWAITED"
    return "UPCOMING" if d > today else "OVERDUE"


def read_csv_rows(text: str) -> list:
    rdr = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    return [{(k or "").strip(): (v or "").strip() for k, v in row.items()} for row in rdr]


# ---------------------------------------------------------------- HTTP clients

class NSEClient:
    def __init__(self):
        self.s = None
        self.born = 0.0
        self.last = 0.0

    def _new(self):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9",
                          "Referer": NSE + "/"})
        for u in (NSE + "/", NSE + "/option-chain"):
            try:
                s.get(u, timeout=20)
            except Exception:
                pass
            time.sleep(0.35)
        self.s, self.born = s, time.time()

    def _pace(self):
        gap = time.time() - self.last
        if gap < 0.35:
            time.sleep(0.35 - gap)
        self.last = time.time()

    def get(self, url, params=None, timeout=30, retries=3):
        err = None
        for i in range(retries):
            if self.s is None or time.time() - self.born > 25 * 60:
                self._new()
            self._pace()
            try:
                r = self.s.get(url, params=params, timeout=timeout)
                if r.status_code in (401, 403):
                    err = "HTTP %d" % r.status_code
                    self.s = None
                elif r.status_code == 200:
                    return r
                else:
                    err = "HTTP %d" % r.status_code
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.5 * (2 ** i))
        raise RuntimeError(err or "failed")

    def api(self, path, params=None, **kw):
        r = self.get(NSE + path, params=params, **kw)
        try:
            return r.json()
        except ValueError:
            raise RuntimeError("non-JSON response (%s)" % r.headers.get("content-type", "?"))


class BSEClient:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update({"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                               "Origin": "https://www.bseindia.com", "Referer": "https://www.bseindia.com/"})

    def api(self, path, params=None, timeout=60, retries=3):
        err = None
        for i in range(retries):
            try:
                r = self.s.get(BSE_API + path, params=params, timeout=timeout)
                if r.status_code == 200:
                    return r.json()
                err = "HTTP %d" % r.status_code
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, e)
            time.sleep(1.5 * (2 ** i))
        raise RuntimeError(err or "failed")


# ---------------------------------------------------------------- files

def atomic_write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_config():
    path = os.path.join(BASE, "config.json")
    cfg = load_json(path, None)
    if cfg is None:
        atomic_write(path, DEFAULT_CONFIG)
        cfg = {}
    out = json.loads(json.dumps(DEFAULT_CONFIG))
    for k, v in cfg.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k].update(v)
        else:
            out[k] = v
    return out


def cached_today(path) -> bool:
    try:
        return datetime.fromtimestamp(os.path.getmtime(path), IST).date() == now_ist().date()
    except OSError:
        return False


# ---------------------------------------------------------------- tracker

class Tracker:
    def __init__(self, cfg: dict, demo: bool = False):
        self.cfg = cfg
        self.demo = demo
        pre = "demo_" if demo else ""
        self.state_path = os.path.join(DATA, pre + "state.json")
        self.watch_path = os.path.join(BASE, pre + "watchlist.json")
        self.lock = threading.RLock()
        self.refresh_evt = threading.Event()
        self.stop_evt = threading.Event()
        self.health = {}
        self.dir = {}          # isin -> {name, nse, bse, ind, mcap}
        self.sym2isin = {}
        self.code2isin = {}
        self.groups = {}       # isin -> [N100|MID150|SML250] for Nifty 500 members
        self.n500 = set()
        self.prices = {}       # isin -> [ltp, pchg]
        self.cycle = 0
        self.next_fast = 0.0
        self.next_slow = 0.0
        self.last_cycle = None
        self.primed = False
        self.bse_seen = set()
        self.nse = None if demo else NSEClient()
        self.bse = None if demo else BSEClient()
        self.quote_mode = "quote-equity"
        self.q = quarter_info(now_ist().date(), cfg.get("quarter_end_override", ""))
        self.watch = load_json(self.watch_path, {"items": []})
        self.state = self._load_state()
        if demo:
            self._demo_init()

    # ---- state
    def _fresh_state(self):
        return {"quarter": self.q["label"], "qend": self.q["qend"].isoformat(), "meetings": {}, "declared": {},
                "feed": [], "backfill_done": False, "backfill_cursor": None}

    def _load_state(self):
        st = load_json(self.state_path, None)
        if not st or st.get("qend") != self.q["qend"].isoformat():
            if st:
                log("quarter changed (%s -> %s); resetting season state" % (st.get("quarter"), self.q["label"]))
            return self._fresh_state()
        for k, v in self._fresh_state().items():
            st.setdefault(k, v)
        return st

    def save_state(self):
        with self.lock:
            atomic_write(self.state_path, self.state)

    def check_quarter(self):
        q = quarter_info(now_ist().date(), self.cfg.get("quarter_end_override", ""))
        if q["qend"] != self.q["qend"]:
            with self.lock:
                self.q = q
                self.state = self._fresh_state()
                self.bse_seen.clear()
            log("new quarter", q["label"])

    def _ok(self, key, count=None, msg=""):
        t = now_ist().isoformat(timespec="seconds")
        self.health[key] = {"ok": True, "at": t, "count": count, "msg": msg, "last_ok": t}

    def _fail(self, key, e):
        prev = self.health.get(key, {})
        msg = str(e)[:300]
        self.health[key] = {"ok": False, "at": now_ist().isoformat(timespec="seconds"), "count": None,
                            "msg": msg, "last_ok": prev.get("last_ok")}
        log("!!", key, msg)

    def _guard(self, key, fn, *a):
        try:
            return fn(*a)
        except Exception as e:
            if os.environ.get("ET_DEBUG"):
                traceback.print_exc()
            self._fail(key, e)
            return None

    # ---- universe
    def tracked(self) -> set:
        return self.n500 | {w["isin"] for w in self.watch.get("items", [])}

    def watch_isins(self) -> set:
        return {w["isin"] for w in self.watch.get("items", [])}

    def _fetch_text(self, name, urls):
        path = os.path.join(DATA, name)
        if cached_today(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(), "cache"
        err = None
        for u in urls:
            try:
                r = self.nse.get(u, timeout=40, retries=2) if "nseindia" in u else \
                    requests.get(u, headers={"User-Agent": UA}, timeout=40)
                if r.status_code == 200 and len(r.content) > 200:
                    txt = r.content.decode("utf-8", errors="replace")
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(txt)
                    return txt, "live"
                err = "HTTP %d" % r.status_code
            except Exception as e:
                err = str(e)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                return f.read(), "stale cache (%s)" % err
        raise RuntimeError(err or "no data")

    def load_masters(self):
        os.makedirs(DATA, exist_ok=True)
        dir_, sym2, code2, groups, n500 = {}, {}, {}, {}, set()

        def eq_l():
            txt, how = self._fetch_text("EQUITY_L.csv", [NSE_ARCH + "/content/equities/EQUITY_L.csv"])
            rows = read_csv_rows(txt)
            for r in rows:
                isin, sym = r.get("ISIN NUMBER"), r.get("SYMBOL")
                if isin and sym:
                    dir_.setdefault(isin, {"name": r.get("NAME OF COMPANY") or sym, "nse": sym, "bse": None,
                                           "ind": None, "mcap": None})
                    dir_[isin]["nse"] = sym
                    sym2[sym.upper()] = isin
            self._ok("nse_equity_master", len(rows), how)
        self._guard("nse_equity_master", eq_l)

        def bse_scrips():
            path = os.path.join(DATA, "bse_scrips.json")
            rows, how = None, "cache"
            if cached_today(path):
                rows = load_json(path, None)
            if not rows:
                try:
                    rows = self.bse.api("/ListofScripData/w", {"Group": "", "Scripcode": "", "industry": "",
                                                                "segment": "Equity", "status": "Active"})
                    atomic_write(path, rows)
                    how = "live"
                except Exception as e:
                    rows = load_json(path, None)
                    if not rows:
                        raise
                    how = "stale cache (%s)" % e
            for r in rows:
                isin, code = (r.get("ISIN_NUMBER") or "").strip(), str(r.get("SCRIP_CD") or "").strip()
                if not isin or not code:
                    continue
                d = dir_.setdefault(isin, {"name": r.get("Issuer_Name") or r.get("Scrip_Name"), "nse": None,
                                           "bse": None, "ind": None, "mcap": None})
                d["bse"] = code
                if not d.get("name"):
                    d["name"] = r.get("Issuer_Name") or r.get("Scrip_Name")
                d["ind"] = d.get("ind") or r.get("INDUSTRY")
                d["bse_id"] = r.get("scrip_id")
                try:
                    d["mcap"] = round(float(r.get("Mktcap")), 1) if r.get("Mktcap") else None
                except ValueError:
                    pass
                code2[code] = isin
            self._ok("bse_scrip_master", len(rows), how)
        self._guard("bse_scrip_master", bse_scrips)

        def indices():
            notes = []
            for g, fn in INDEX_FILES.items():
                txt, how = self._fetch_text(fn, [NSE_ARCH + "/content/indices/" + fn, NIFTYIDX + fn])
                notes.append("%s:%s" % (g, how))
                for r in read_csv_rows(txt):
                    isin = r.get("ISIN Code")
                    if not isin or not isin.startswith("IN"):   # skips NSE's DUMMY placeholder rows
                        continue
                    d = dir_.setdefault(isin, {"name": r.get("Company Name"), "nse": r.get("Symbol"), "bse": None,
                                               "ind": None, "mcap": None})
                    d["ind"] = r.get("Industry") or d.get("ind")
                    if r.get("Symbol"):
                        d["nse"] = r["Symbol"]
                        sym2[r["Symbol"].upper()] = isin
                    if g == "N500":
                        n500.add(isin)
                        d["name"] = r.get("Company Name") or d.get("name")
                    else:
                        groups.setdefault(isin, []).append(g)
            if len(n500) < 400:
                raise RuntimeError("Nifty 500 list has only %d rows" % len(n500))
            self._ok("nse_index_lists", len(n500), ", ".join(notes))
        self._guard("nse_index_lists", indices)

        with self.lock:
            if dir_:
                self.dir, self.sym2isin, self.code2isin = dir_, sym2, code2
            if n500:
                self.n500, self.groups = n500, groups

    # ---- sources
    def _register_nse(self, items, source_key):
        qend = self.q["qend"]
        start = datetime.combine(self.q["season_start"], datetime.min.time(), IST)
        tracked = self.tracked()
        new = []
        for it in items:
            if not classify_nse(it, qend):
                continue
            # sm_isin is stale for companies that changed ISIN (e.g. after a split), so prefer the symbol.
            isin = self.sym2isin.get((it.get("symbol") or "").upper()) or (it.get("sm_isin") or "").strip()
            ts = nse_ann_time(it)
            if not isin or isin not in tracked or not ts or ts < start:
                continue
            if merge_declaration(self.state["declared"], isin, ts, "NSE", it.get("attchmntFile"),
                                 it.get("attchmntText") or it.get("desc") or ""):
                new.append(isin)
        return new

    def _register_bse(self, items):
        qend = self.q["qend"]
        start = datetime.combine(self.q["season_start"], datetime.min.time(), IST)
        tracked = self.tracked()
        new = []
        for it in items:
            if not classify_bse(it, qend):
                continue
            isin = self.code2isin.get(str(it.get("SCRIP_CD") or "").strip())
            ts = bse_ann_time(it)
            if not isin or isin not in tracked or not ts or ts < start:
                continue
            if merge_declaration(self.state["declared"], isin, ts, "BSE", bse_link(it),
                                 it.get("NEWSSUB") or it.get("HEADLINE") or ""):
                new.append(isin)
        return new

    def nse_announcements_today(self):
        t = now_ist().strftime("%d-%m-%Y")
        items = self.nse.api("/api/corporate-announcements",
                             {"index": "equities", "from_date": t, "to_date": t})
        if not isinstance(items, list):
            raise RuntimeError("unexpected payload: %s" % type(items).__name__)
        with self.lock:
            new = self._register_nse(items, "nse_announcements")
        self._ok("nse_announcements", len(items), "%d results today in universe" % self._today_count("NSE"))
        return new

    def _today_count(self, src):
        today = now_ist().date().isoformat()
        return sum(1 for d in self.state["declared"].values() if (d.get(src.lower() + "_ts") or "")[:10] == today)

    def bse_announcements(self, day_from: date, day_to: date, cat: str, stop_on_seen=False):
        out, page, total = [], 1, None
        while True:
            j = self.bse.api("/AnnSubCategoryGetData/w", {
                "pageno": page, "strCat": cat, "subcategory": "-1",
                "strPrevDate": day_from.strftime("%Y%m%d"), "strToDate": day_to.strftime("%Y%m%d"),
                "strSearch": "P", "strScrip": "", "strType": "C"})
            rows = j.get("Table") or []
            if total is None:
                t1 = j.get("Table1") or [{}]
                total = int(t1[0].get("ROWCNT") or 0) if t1 else 0
            fresh = [r for r in rows if r.get("NEWSID") not in self.bse_seen]
            out.extend(rows)
            if not rows or len(out) >= total or page > 400 or (stop_on_seen and not fresh):
                break
            page += 1
            time.sleep(0.35)
        return out, total

    def bse_today(self):
        today = now_ist().date()
        n, tot = 0, 0
        new = []
        for cat in ("Result", "Board Meeting"):
            rows, total = self.bse_announcements(today, today, cat, stop_on_seen=True)
            n += len(rows)
            tot += total or 0
            with self.lock:
                new += self._register_bse(rows)
            self.bse_seen.update(r.get("NEWSID") for r in rows)
        self._ok("bse_announcements", tot, "%d results today in universe" % self._today_count("BSE"))
        return new

    def bse_backfill_step(self, max_days=4):
        """Walk the season day by day (resumable) so the fast loop never stalls for long."""
        st = self.state
        if st.get("backfill_done"):
            return []
        today = now_ist().date()
        cur = date.fromisoformat(st["backfill_cursor"]) if st.get("backfill_cursor") else self.q["season_start"]
        new, rows_n = [], 0
        for _ in range(max_days):
            if cur > today:
                break
            for cat in ("Result", "Board Meeting"):
                rows, _t = self.bse_announcements(cur, cur, cat)
                rows_n += len(rows)
                with self.lock:
                    new += self._register_bse(rows)
            cur += timedelta(days=1)
            with self.lock:
                st["backfill_cursor"] = cur.isoformat()
        with self.lock:
            if cur > today:
                st["backfill_done"] = True
        self._ok("bse_backfill", rows_n, "done" if st["backfill_done"] else "through %s" % cur.isoformat())
        return new

    def nse_register(self):
        """NSE results register. The legacy /api/corporates-financial-results only returns
        Dec-2024 rows, so this uses the Integrated Filing (Financials) register instead."""
        t = now_ist().strftime("%d-%m-%Y")
        f = self.q["season_start"].strftime("%d-%m-%Y")
        items, page, total = [], 1, None
        while True:
            j = self.nse.api("/api/integrated-filing-results", {
                "index": "equities", "type": "Integrated Filing- Financials", "period": "Quarterly",
                "from_date": f, "to_date": t, "page": page, "size": 1000}, timeout=60)
            rows = j.get("data") or []
            total = j.get("totalCount") or 0
            items.extend(rows)
            if not rows or len(items) >= total or page >= 20:
                break
            page += 1
        qend = self.q["qend"]
        tracked = self.tracked()
        new, matched = [], 0
        with self.lock:
            for r in items:
                if "financ" not in (r.get("type") or "").lower() or parse_day(r.get("qe_Date")) != qend:
                    continue
                isin = self.sym2isin.get((r.get("symbol") or "").upper())
                ts = parse_nse_dt(r.get("broadcast_Date")) or parse_nse_dt(r.get("creation_Date"))
                if not isin or isin not in tracked or not ts:
                    continue
                matched += 1
                detail = " · ".join(x for x in (self.q["qname"], r.get("consolidated"), r.get("audited")) if x)
                link = r.get("pdf_attach") or r.get("ixbrl") or r.get("xbrl")
                if merge_declaration(self.state["declared"], isin, ts, "NSE", link,
                                     "Integrated Filing (Financials) for %s" % self.q["qname"].lower(), detail):
                    new.append(isin)
        self._ok("nse_results_register", len(items), "%d in universe for %s" % (matched, self.q["label"]))
        return new

    def nse_board_meetings(self):
        f = self.q["season_start"].strftime("%d-%m-%Y")
        t = (now_ist().date() + timedelta(days=int(self.cfg.get("lookahead_days", 45)))).strftime("%d-%m-%Y")
        rows = self.nse.api("/api/corporate-board-meetings", {"index": "equities", "from_date": f, "to_date": t},
                            timeout=60)
        if not isinstance(rows, list):
            raise RuntimeError("unexpected payload")
        with self.lock:
            rows = [dict(r, sm_isin=self.sym2isin.get((r.get("bm_symbol") or "").upper()) or r.get("sm_isin"))
                    for r in rows]   # same stale-ISIN issue as announcements
            old = self.state["meetings"]
            picked = pick_meetings(rows, self.q["season_start"], old)
            merged = {k: v for k, v in old.items() if v.get("src") == "BSE" and k not in picked}
            merged.update(picked)
            self.state["meetings"] = merged
            n = len([k for k in picked if k in self.tracked()])
        self._ok("nse_board_meetings", len(rows), "%d result meetings in universe" % n)

    def bse_calendar(self):
        f = self.q["season_start"].strftime("%Y%m%d")
        t = (now_ist().date() + timedelta(days=int(self.cfg.get("lookahead_days", 45)))).strftime("%Y%m%d")
        rows = self.bse.api("/Corpforthresults/w", {"fromdate": f, "todate": t})
        filled = 0
        with self.lock:
            m = self.state["meetings"]
            for r in rows or []:
                isin = self.code2isin.get(str(r.get("scrip_Code") or "").strip())
                d = parse_day(r.get("meeting_date"))
                if not isin or not d or d < self.q["season_start"]:
                    continue
                cur = m.get(isin)
                if cur and cur.get("src") == "NSE":
                    continue
                if cur and cur.get("date") and cur["date"] != d.isoformat():
                    m[isin] = {"date": d.isoformat(), "prev_date": cur["date"], "src": "BSE", "ts": None,
                               "purpose": "Results"}
                elif not cur:
                    m[isin] = {"date": d.isoformat(), "prev_date": None, "src": "BSE", "ts": None,
                               "purpose": "Results"}
                filled += isin in self.tracked()
        self._ok("bse_calendar", len(rows or []), "%d gap-fills in universe" % filled)

    def nifty500_prices(self):
        j = None
        for path in ("/api/equity-stock-indices", "/api/equity-stockIndices"):
            try:
                j = self.nse.api(path, {"index": "NIFTY 500"}, retries=2)
                break
            except Exception as e:
                err = e
        if j is None:
            raise err
        n = 0
        with self.lock:
            for r in j.get("data") or []:
                if r.get("priority") == 1 or (r.get("symbol") or "").upper() == "NIFTY 500":
                    continue
                isin = self.sym2isin.get((r.get("symbol") or "").upper())
                if isin:
                    self.prices[isin] = [r.get("lastPrice"), r.get("pChange")]
                    n += 1
        self._ok("nse_prices", n, j.get("timestamp") or "")

    def watch_quotes(self):
        todo = [(i, self.dir.get(i, {}).get("nse")) for i in self.watch_isins() - self.n500]
        todo = [(i, s) for i, s in todo if s]
        n, errs = 0, []
        for isin, sym in todo:
            try:
                ltp = chg = None
                if self.quote_mode == "quote-equity":
                    try:
                        j = self.nse.api("/api/quote-equity", {"symbol": sym}, retries=1)
                        ltp, chg = j["priceInfo"]["lastPrice"], j["priceInfo"]["pChange"]
                    except Exception:
                        self.quote_mode = "nextapi"   # quote-equity is Akamai-blocked for scripts
                if self.quote_mode == "nextapi":
                    j = self.nse.api("/api/NextApi/apiClient/GetQuoteApi",
                                     {"functionName": "getSymbolData", "marketType": "N", "series": "EQ",
                                      "symbol": sym}, retries=2)
                    e = (j.get("equityResponse") or [{}])[0]
                    md, ti = e.get("metaData") or {}, e.get("tradeInfo") or {}
                    ltp = ti.get("lastPrice") or md.get("closePrice")
                    chg = md.get("pChange")
                with self.lock:
                    self.prices[isin] = [ltp, chg]
                n += 1
            except Exception as e:
                errs.append("%s: %s" % (sym, e))
        if errs and not n:
            raise RuntimeError("; ".join(errs)[:300])
        self._ok("nse_watch_quotes", n, ("via %s" % self.quote_mode) + (("; failed " + ", ".join(errs)) if errs else ""))

    # ---- cycles
    def slow_cycle(self):
        log("slow cycle")
        self.load_masters()
        self._guard("nse_board_meetings", self.nse_board_meetings)
        self._guard("bse_calendar", self.bse_calendar)
        return self._guard("nse_results_register", self.nse_register) or []

    def fast_cycle(self):
        new = []
        new += self._guard("nse_announcements", self.nse_announcements_today) or []
        new += self._guard("bse_announcements", self.bse_today) or []
        self._guard("nse_prices", self.nifty500_prices)
        if self.cycle % 5 == 0 and self.watch_isins() - self.n500:
            self._guard("nse_watch_quotes", self.watch_quotes)
        return new

    def fast_interval(self):
        if self.demo:
            return 5
        h = now_ist().hour
        a, b = (self.cfg.get("night_hours_ist") or [1, 7])[:2]
        if a <= h < b:
            return int(self.cfg.get("slow_poll_minutes", 15)) * 60
        return int(self.cfg.get("fast_poll_seconds", 60))

    def run(self):
        while not self.stop_evt.is_set():
            force = self.refresh_evt.is_set()
            self.refresh_evt.clear()
            new = []
            try:
                if self.demo:
                    new = self._demo_tick()
                else:
                    self.check_quarter()
                    if force or time.time() >= self.next_slow:
                        new += self.slow_cycle()
                        self.next_slow = time.time() + int(self.cfg.get("slow_poll_minutes", 15)) * 60
                    if not self.state.get("backfill_done"):
                        new += self._guard("bse_backfill", self.bse_backfill_step) or []
                    new += self.fast_cycle()
                self._on_new(new)
                self.cycle += 1
                self.last_cycle = now_ist().isoformat(timespec="seconds")
                self.save_state()
                if not self.demo:
                    log("cycle %d: %s" % (self.cycle, self.counts_line()))
            except Exception as e:
                traceback.print_exc()
                self._fail("poller", e)
            self.primed = True
            iv = self.fast_interval()
            self.next_fast = time.time() + iv
            self.refresh_evt.wait(iv)

    def counts_line(self):
        c = {}
        for r in self.rows():
            c[r["status"]] = c.get(r["status"], 0) + 1
        return " ".join("%s=%d" % (k, c.get(k, 0)) for k in
                        ("DECLARED", "AWAITED", "UPCOMING", "OVERDUE", "UNSCHEDULED"))

    def _on_new(self, isins):
        if not isins:
            return
        seen_feed = {e["isin"] for e in self.state["feed"]}
        events = []
        with self.lock:
            for isin in dict.fromkeys(isins):
                if isin in seen_feed:
                    continue
                d = self.state["declared"].get(isin)
                c = self.dir.get(isin, {})
                if not d:
                    continue
                ev = {"ts": d["ts"], "seen": now_ist().isoformat(timespec="seconds"), "isin": isin,
                      "name": c.get("name") or isin, "nse": c.get("nse"), "bse": c.get("bse"), "src": d["src"],
                      "link": d.get("nse") or d.get("bse"), "headline": d.get("headline"),
                      "groups": self.groups_for(isin)}
                events.append(ev)
            # Backfilled filings sort in by filing time; the newest-first order is by ts.
            feed = events + self.state["feed"]
            feed.sort(key=lambda e: e["ts"], reverse=True)
            self.state["feed"] = feed[:600]
        if self.primed and events:
            self._email(events)

    def groups_for(self, isin):
        g = list(self.groups.get(isin, []))
        if isin in self.n500:
            g.insert(0, "N500")
        if isin in self.watch_isins():
            g.append("WATCH")
        return g

    # ---- email
    def _email(self, events):
        em = self.cfg.get("email") or {}
        if not em.get("enabled") or self.demo:
            return
        want = set(em.get("groups") or self.cfg.get("alert_groups") or [])
        fresh = now_ist() - timedelta(minutes=60)   # backfilled filings never trigger mail
        hits = [e for e in events if want & set(e["groups"]) and from_iso(e["ts"]) >= fresh]
        if not hits:
            return
        try:
            send_email(em, hits, self.q["label"])
            self._ok("email", len(hits), "sent")
        except Exception as e:
            self._fail("email", e)

    # ---- views
    def rows(self):
        today = now_ist().date()
        out = []
        with self.lock:
            m, decl = self.state["meetings"], self.state["declared"]
            for isin in self.tracked():
                c = self.dir.get(isin) or {}
                if not c:
                    w = next((x for x in self.watch["items"] if x["isin"] == isin), {})
                    c = {"name": w.get("name"), "nse": w.get("nse"), "bse": w.get("bse")}
                mt = m.get(isin)
                d = decl.get(isin)
                p = self.prices.get(isin) or [None, None]
                out.append({
                    "isin": isin, "name": c.get("name") or isin, "nse": c.get("nse"), "bse": c.get("bse"),
                    "ind": c.get("ind"), "mcap": c.get("mcap"), "groups": self.groups_for(isin),
                    "status": status_for(isin, m, decl, today),
                    "meet": {"date": mt["date"], "prev": mt.get("prev_date"), "src": mt.get("src")} if mt else None,
                    "decl": {"ts": d["ts"], "src": d["src"], "nse": d.get("nse"), "bse": d.get("bse"),
                             "nse_ts": d.get("nse_ts"), "bse_ts": d.get("bse_ts"), "detail": d.get("detail"),
                             "headline": d.get("headline")} if d else None,
                    "ltp": p[0], "chg": p[1],
                })
        return out

    def snapshot(self):
        with self.lock:
            rows = self.rows()
            return {
                "meta": {
                    "quarter": self.q["label"], "qend": self.q["qend"].isoformat(),
                    "season_start": self.q["season_start"].isoformat(), "today": now_ist().date().isoformat(),
                    "now": now_ist().isoformat(timespec="seconds"), "next_fast": int(self.next_fast * 1000),
                    "next_slow": int(self.next_slow * 1000), "cycle": self.cycle, "last_cycle": self.last_cycle,
                    "demo": self.demo, "alert_groups": self.cfg.get("alert_groups") or [],
                    "fast_interval": self.fast_interval(), "backfill_done": self.state.get("backfill_done"),
                    "universe": len(self.n500), "watch": len(self.watch_isins()),
                },
                "health": self.health,
                "rows": rows,
                "feed": self.state["feed"][:200],
            }

    # ---- search / watchlist
    def search(self, q, limit=20):
        q = (q or "").strip().upper()
        if not q:
            return []
        res = []
        with self.lock:
            for isin, c in self.dir.items():
                nse, bse, name = (c.get("nse") or "").upper(), c.get("bse") or "", (c.get("name") or "").upper()
                if q in (nse, bse, isin):
                    score = 0
                elif nse.startswith(q) or bse.startswith(q):
                    score = 1
                elif name.startswith(q):
                    score = 2
                elif q in name or q in nse:
                    score = 3
                else:
                    continue
                res.append((score, isin not in self.n500, -(c.get("mcap") or 0), isin, c))
        res.sort(key=lambda x: x[:3])
        return [{"isin": r[3], "name": r[4].get("name"), "nse": r[4].get("nse"), "bse": r[4].get("bse"),
                 "mcap": r[4].get("mcap"), "n500": r[3] in self.n500,
                 "watched": r[3] in self.watch_isins()} for r in res[:limit]]

    def resolve(self, tok):
        t = tok.strip().strip('"').strip()
        if not t:
            return None
        u = t.upper()
        if ":" in u:
            ex, _, val = u.partition(":")
            val = val.strip()
            if ex.strip() == "NSE":
                return self.sym2isin.get(val)
            if ex.strip() == "BSE":
                return self.code2isin.get(val)
        if re.fullmatch(r"IN[A-Z0-9]{10}", u) and u in self.dir:
            return u
        if re.fullmatch(r"\d{6}", u):
            return self.code2isin.get(u)
        if u in self.sym2isin:
            return self.sym2isin[u]
        hits = [i for i, c in self.dir.items() if (c.get("name") or "").upper() == u]
        if not hits:
            norm = re.sub(r"\b(LTD|LIMITED)\.?$", "", u).strip()
            hits = [i for i, c in self.dir.items() if norm and norm in (c.get("name") or "").upper()]
            if len(hits) > 1:   # prefer the Nifty 500 member, then the larger company
                n5 = [i for i in hits if i in self.n500]
                hits = n5 if len(n5) == 1 else hits
        return hits[0] if len(hits) == 1 else None

    def watch_add(self, text):
        added, dup, unresolved = [], [], []
        tokens = [t for t in re.split(r"[\n,;\t]+", text or "") if t.strip()]
        with self.lock:
            have = self.watch_isins()
            for tok in tokens:
                isins = []
                i = self.resolve(tok)
                if i:
                    isins = [i]
                elif " " in tok.strip():   # "RELIANCE TCS INFY" pasted on one line
                    parts = [self.resolve(p) for p in tok.split()]
                    if all(parts):
                        isins = parts
                if not isins:
                    unresolved.append(tok.strip())
                    continue
                for i in isins:
                    c = self.dir.get(i, {})
                    label = c.get("nse") or c.get("bse") or i
                    if i in have:
                        dup.append(label)
                        continue
                    have.add(i)
                    self.watch["items"].append({"isin": i, "name": c.get("name"), "nse": c.get("nse"),
                                                "bse": c.get("bse"), "added": now_ist().isoformat(timespec="seconds")})
                    added.append(label)
            atomic_write(self.watch_path, self.watch)
        return {"added": added, "duplicate": dup, "unresolved": unresolved}

    def watch_remove(self, isin):
        with self.lock:
            before = len(self.watch["items"])
            self.watch["items"] = [w for w in self.watch["items"] if w["isin"] != isin]
            atomic_write(self.watch_path, self.watch)
            return {"removed": before - len(self.watch["items"])}

    def export_csv(self):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Company", "NSE", "BSE", "ISIN", "Groups", "Industry", "Mcap Rs cr", "Status", "Board meeting",
                    "Prev meeting", "Meeting src", "Declared (IST)", "Declared src", "Detail", "LTP", "% chg",
                    "NSE filing", "BSE filing"])
        rank = {"DECLARED": 0, "AWAITED": 1, "UPCOMING": 2, "OVERDUE": 3, "UNSCHEDULED": 4}
        for r in sorted(self.rows(), key=lambda r: (rank[r["status"]], -(r["mcap"] or 0))):
            m, d = r["meet"] or {}, r["decl"] or {}
            w.writerow([r["name"], r["nse"] or "", r["bse"] or "", r["isin"], " ".join(r["groups"]), r["ind"] or "",
                        r["mcap"] or "", r["status"], m.get("date") or "", m.get("prev") or "", m.get("src") or "",
                        (d.get("ts") or "")[:19].replace("T", " "), d.get("src") or "", d.get("detail") or "",
                        r["ltp"] if r["ltp"] is not None else "", r["chg"] if r["chg"] is not None else "",
                        d.get("nse") or "", d.get("bse") or ""])
        return buf.getvalue()

    # ---- demo
    def _demo_init(self):
        rnd = random.Random(7)
        a = ["Aurora", "Bharat", "Crest", "Deccan", "Everest", "Falcon", "Ganga", "Horizon", "Indus", "Jupiter",
             "Kaveri", "Lotus", "Meridian", "Narmada", "Orion", "Pinnacle", "Quantum", "Rohini", "Sahyadri",
             "Trident", "Udaya", "Vertex", "Western", "Yamuna", "Zenith"]
        b = [("Polymers", "Chemicals"), ("Finance", "Financial Services"), ("Motors", "Automobile and Auto Components"),
             ("Pharma", "Healthcare"), ("Infotech", "Information Technology"), ("Cements", "Construction Materials"),
             ("Power", "Power"), ("Foods", "Fast Moving Consumer Goods"), ("Bank", "Financial Services"),
             ("Steel", "Metals & Mining"), ("Realty", "Realty"), ("Textiles", "Textiles")]
        today = now_ist().date()
        ss = self.q["season_start"]
        n = 0
        dir_, groups, n500 = {}, {}, set()
        for i in range(155):
            w1, (w2, ind) = a[i % len(a)], b[(i * 7 + i // len(a)) % len(b)]
            isin = "INE%06dD01" % (100000 + i)
            sym = (w1[:5] + w2[:4]).upper() + ("" if i < 25 else str(i))
            mcap = round(250000 * (0.965 ** i) + rnd.random() * 500, 1)
            name = "%s %s" % (w1, w2)
            while any(c["name"] == name + " Ltd" for c in dir_.values()):   # keep demo names unique
                name += " II"
            dir_[isin] = {"name": name + " Ltd", "nse": sym, "bse": str(500000 + i * 13), "ind": ind,
                          "mcap": mcap}
            if i < 150:
                n500.add(isin)
                groups[isin] = ["N100"] if i < 30 else ["MID150"] if i < 80 else ["SML250"]
            self.prices[isin] = [round(100 + rnd.random() * 3000, 2), round(rnd.uniform(-3, 3), 2)]
        self.dir, self.groups, self.n500 = dir_, groups, n500
        self.sym2isin = {c["nse"]: i for i, c in dir_.items()}
        self.code2isin = {c["bse"]: i for i, c in dir_.items()}
        if not self.watch.get("items"):
            ws = ["INE%06dD01" % (100000 + i) for i in (0, 3, 40, 151, 152, 153)]
            self.watch = {"items": [{"isin": i, "name": dir_[i]["name"], "nse": dir_[i]["nse"],
                                     "bse": dir_[i]["bse"], "added": now_ist().isoformat()} for i in ws]}
            atomic_write(self.watch_path, self.watch)
        st = self._fresh_state()
        days = (today - ss).days
        for isin in self.tracked():
            r = rnd.random()
            if r < 0.12:
                continue
            if r < 0.55:
                d = ss + timedelta(days=rnd.randint(10, max(10, days - 1)))
            elif r < 0.68:
                d = today
            else:
                d = today + timedelta(days=rnd.randint(1, 30))
            prev = (d - timedelta(days=rnd.randint(2, 9))).isoformat() if rnd.random() < 0.06 else None
            st["meetings"][isin] = {"date": d.isoformat(), "prev_date": prev, "src": rnd.choice(["NSE", "NSE", "BSE"]),
                                    "ts": None, "purpose": "Financial Results"}
            if d < today and rnd.random() < 0.93:
                ts = datetime.combine(d, datetime.min.time(), IST) + timedelta(hours=rnd.randint(13, 20),
                                                                                minutes=rnd.randint(0, 59))
                self._demo_declare(st, isin, ts, rnd)
                n += 1
            elif d == today and rnd.random() < 0.3:
                ts = now_ist() - timedelta(minutes=rnd.randint(15, 300))
                self._demo_declare(st, isin, ts, rnd)
        self.state = st
        self.state["backfill_done"] = True
        for k in ("nse_announcements", "bse_announcements", "nse_board_meetings", "nse_results_register",
                  "bse_calendar", "nse_prices", "nse_index_lists", "bse_scrip_master"):
            self._ok(k, None, "demo")
        self._fail("nse_watch_quotes", "demo: simulated failure (tooltip check)")
        self._on_new(sorted(self.state["declared"], key=lambda i: self.state["declared"][i]["ts"]))
        self.demo_rnd = rnd

    def _demo_declare(self, st, isin, ts, rnd):
        both = rnd.random() < 0.8
        src = rnd.choice(["NSE", "BSE"])
        merge_declaration(st["declared"], isin, ts, src, "https://www.nseindia.com/" if src == "NSE" else
                          "https://www.bseindia.com/", "Unaudited financial results for the quarter ended "
                          + self.q["qend"].strftime("%d %b %Y"), "%s · Consolidated · Un-Audited" % self.q["qname"],
                          seen=ts)
        if both:
            other = "BSE" if src == "NSE" else "NSE"
            merge_declaration(st["declared"], isin, ts + timedelta(minutes=rnd.randint(1, 6)), other,
                              "https://www.%sindia.com/" % other.lower(), "", None)

    def _demo_tick(self):
        rnd = self.demo_rnd
        with self.lock:
            for isin, p in self.prices.items():
                p[0] = round(p[0] * (1 + rnd.uniform(-0.002, 0.002)), 2)
                p[1] = round(p[1] + rnd.uniform(-0.08, 0.08), 2)
            for k in self.health:
                if self.health[k]["ok"]:
                    self._ok(k, None, "demo")
            self.demo_last = getattr(self, "demo_last", 0)
            if time.time() - self.demo_last < 15:
                return []
            self.demo_last = time.time()
            today = now_ist().date().isoformat()
            m, decl = self.state["meetings"], self.state["declared"]
            pool = [i for i in self.tracked() if i not in decl and (m.get(i) or {}).get("date") == today]
            if not pool:
                pool = [i for i in self.tracked() if i not in decl]
                if not pool:
                    return []
            isin = rnd.choice(pool)
            self._demo_declare(self.state, isin, now_ist(), rnd)
            return [isin]


# ---------------------------------------------------------------- email

def send_email(em, events, quarter):
    rows = "".join(
        '<tr><td style="padding:8px 10px;border-bottom:1px solid #1d2c4d;color:#ffffff;background:#0b1730;'
        'font:600 14px Arial,sans-serif">%s<div style="color:#8ea0c4;font:12px Arial,sans-serif">%s · %s</div></td>'
        '<td style="padding:8px 10px;border-bottom:1px solid #1d2c4d;color:#d4af37;background:#0b1730;'
        'font:13px Arial,sans-serif;white-space:nowrap">%s<br><span style="color:#8ea0c4">%s</span></td>'
        '<td style="padding:8px 10px;border-bottom:1px solid #1d2c4d;background:#0b1730;font:13px Arial,sans-serif">'
        '%s</td></tr>' % (
            html_esc(e["name"]), html_esc(e.get("nse") or "-"), html_esc(e.get("bse") or "-"),
            (e["ts"] or "")[11:16] + " IST", html_esc(" ".join(e["groups"])),
            ('<a href="%s" style="color:#d4af37">filing ↗</a>' % html_esc(e["link"])) if e.get("link") else "")
        for e in events)
    body = (
        '<div style="background:#07101f;padding:20px;font-family:Arial,sans-serif">'
        '<table role="presentation" cellpadding="0" cellspacing="0" style="width:100%%;max-width:640px;'
        'background:#0b1730;border:1px solid #d4af37;border-collapse:collapse">'
        '<tr><td colspan="3" style="background:#07101f;padding:14px 10px;color:#d4af37;font:700 16px Arial,sans-serif">'
        'Aα Alphapeak · %s results declared</td></tr>%s'
        '<tr><td colspan="3" style="padding:10px;background:#07101f;color:#8ea0c4;font:11px Arial,sans-serif">'
        'Earnings Tracker · %s</td></tr></table></div>') % (html_esc(quarter), rows,
                                                               now_ist().strftime("%d %b %Y %H:%M IST"))
    msg = MIMEText(body, "html", "utf-8")
    names = ", ".join(e.get("nse") or e["name"] for e in events[:4]) + (" +%d" % (len(events) - 4) if len(events) > 4 else "")
    msg["Subject"] = "Results declared: %s (%s)" % (names, quarter)
    msg["From"] = em["smtp_user"]
    to = [x.strip() for x in re.split(r"[,;]", em.get("to") or em["smtp_user"]) if x.strip()]
    msg["To"] = ", ".join(to)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=30) as s:
        s.login(em["smtp_user"], em["smtp_app_password"])
        s.sendmail(em["smtp_user"], to, msg.as_string())


def html_esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


# ---------------------------------------------------------------- HTTP

def make_handler(tr: Tracker):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
            if isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            try:
                return json.loads(self.rfile.read(n).decode("utf-8"))
            except ValueError:
                return {}

        def do_GET(self):
            u = urlparse(self.path)
            p = u.path
            try:
                if p in ("/", "/index.html"):
                    with open(os.path.join(STATIC, "index.html"), "rb") as f:
                        return self._send(200, f.read(), "text/html; charset=utf-8")
                if p == "/api/state":
                    return self._send(200, tr.snapshot())
                if p == "/api/search":
                    return self._send(200, tr.search(parse_qs(u.query).get("q", [""])[0]))
                if p == "/api/watchlist":
                    return self._send(200, tr.watch)
                if p == "/api/export.csv":
                    fn = "earnings_%s_%s.csv" % (tr.q["label"].replace(" ", ""), now_ist().strftime("%Y%m%d_%H%M"))
                    return self._send(200, "﻿" + tr.export_csv(), "text/csv; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="%s"' % fn})
                if p == "/favicon.ico":
                    return self._send(204, b"", "image/x-icon")
                self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})

        def do_POST(self):
            p = urlparse(self.path).path
            try:
                b = self._body()
                if p == "/api/watchlist/add":
                    res = tr.watch_add(b.get("text", ""))
                    if res["added"]:
                        tr.refresh_evt.set()
                    return self._send(200, res)
                if p == "/api/watchlist/remove":
                    return self._send(200, tr.watch_remove(b.get("isin", "")))
                if p == "/api/refresh":
                    tr.refresh_evt.set()
                    return self._send(200, {"ok": True})
                self._send(404, {"error": "not found"})
            except Exception as e:
                traceback.print_exc()
                self._send(500, {"error": str(e)})
    return H


def main(argv=None):
    ap = argparse.ArgumentParser(description="Alphapeak Earnings Tracker")
    ap.add_argument("--demo", action="store_true", help="synthetic data, no network")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args(argv)
    if not args.demo and requests is None:
        sys.exit("The 'requests' package is required: python3 -m pip install --user requests")
    cfg = load_config()
    port = args.port or int(cfg.get("port", 8765))
    tr = Tracker(cfg, demo=args.demo)
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(tr))
    threading.Thread(target=tr.run, name="poller", daemon=True).start()
    url = "http://127.0.0.1:%d/" % port
    log("Earnings Tracker %s%s on %s" % (tr.q["label"], " [DEMO]" if args.demo else "", url))
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        tr.stop_evt.set()
        tr.refresh_evt.set()
        srv.server_close()


if __name__ == "__main__":
    main()
