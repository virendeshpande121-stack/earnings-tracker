"""Parsers behind the results snapshot, concall time and deck pointers.

Pure functions (no network) so they can be unit-tested against saved samples:
- XBRL (NSE Integrated Filing - Financials) -> quarterly P&L metrics and growth
- concall intimation text -> (date, time) slots
- investor presentation text -> two management pointers (extractive; optional Claude summary)
"""
from __future__ import annotations

import io
import json
import logging
import re
from datetime import date, timedelta

try:
    import pypdf
except ImportError:  # deck pointers and PDF-only concall times need it; everything else works without
    pypdf = None
logging.getLogger("pypdf").setLevel(logging.ERROR)   # font-encoding warnings on most decks are noise

CR = 1e7   # XBRL values are in rupees; the dashboard shows Rs crore


# ---------------------------------------------------------------- XBRL financials

_CTX = re.compile(r"<(?:\w+:)?context\b[^>]*\bid=\"([^\"]+)\"[^>]*>(.*?)</(?:\w+:)?context>", re.S)
_FACT = re.compile(r"<in-capmkt:(\w+)\b[^>]*\bcontextRef=\"([^\"]+)\"[^>]*>([^<]*)<")


def xbrl_facts(xml: str, qstart: date, qend: date) -> dict:
    """Facts for the plain (no dimension) duration context covering exactly qstart..qend."""
    ctxs = set()
    for cid, body in _CTX.findall(xml):
        if "explicitMember" in body or "typedMember" in body:
            continue
        s = re.search(r"startDate>\s*([\d-]+)", body)
        e = re.search(r"endDate>\s*([\d-]+)", body)
        if s and e and s.group(1) == qstart.isoformat() and e.group(1) == qend.isoformat():
            ctxs.add(cid)
    out = {}
    for name, cref, val in _FACT.findall(xml):
        if cref in ctxs and name not in out:
            v = val.strip()
            try:
                out[name] = float(v)
            except ValueError:
                out[name] = v
    return out


def _num(f, *keys):
    for k in keys:
        v = f.get(k)
        if isinstance(v, float):
            return v
    return None


def fin_metrics(f: dict, lender: bool = False) -> dict | None:
    """Revenue, EBITDA (ex-other income), margin, PBT, PAT, EPS. Banks report NII and operating
    profit (PPOP) instead; other lenders show PPOP (PBT + impairment) in the EBITDA slot."""
    if not f:
        return None
    if "GrossPremiumIncome" in f or "GrossPremiumsWritten" in f:   # life / general insurers
        m = {"kind": "insurer", "rev": _num(f, "GrossPremiumIncome", "GrossPremiumsWritten"), "ebitda": None,
             "pbt": _num(f, "ProfitLossBeforeTax", "ProfitOrLossBeforeTax"),
             "pat": _num(f, "ProfitLossAfterTaxAndExtraordinaryItems", "ProfitLossAfterTax"),
             "eps": _num(f, "BasicAndDilutedEPSAfterExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized",
                         "BasicAndDilutedEPSBeforeExtraordinaryItemsNetOfTaxExpenseForThePeriodNotToBeAnnualized"),
             "margin": None}
    elif "InterestExpended" in f or "OperatingProfitBeforeProvisionAndContingencies" in f:
        ie, ix = _num(f, "InterestEarned"), _num(f, "InterestExpended")
        m = {"kind": "bank",
             "rev": ie - ix if ie is not None and ix is not None else None,
             "ebitda": _num(f, "OperatingProfitBeforeProvisionAndContingencies"),
             "pbt": _num(f, "ProfitLossFromOrdinaryActivitiesBeforeTax"),
             "pat": _num(f, "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates",
                         "ProfitLossForThePeriod"),
             "eps": _num(f, "BasicEarningsPerShareAfterExtraordinaryItems",
                         "BasicEarningsPerShareBeforeExtraordinaryItems")}
        m["margin"] = None
    else:
        rev = _num(f, "RevenueFromOperations")
        pbe = _num(f, "ProfitBeforeExceptionalItemsAndTax", "ProfitBeforeTax")
        fc, da, oi = (_num(f, "FinanceCosts") or 0.0, _num(f, "DepreciationDepletionAndAmortisationExpense") or 0.0,
                      _num(f, "OtherIncome") or 0.0)
        if lender:   # NBFCs/insurers: pre-provision operating profit in place of EBITDA
            imp = _num(f, "ImpairmentOnFinancialInstruments")
            ebitda = pbe + imp if pbe is not None and imp is not None else None
        else:
            ebitda = None if pbe is None else pbe + fc + da - oi
        m = {"kind": "lender" if lender else "corp", "rev": rev, "ebitda": ebitda,
             "pbt": _num(f, "ProfitBeforeTax", "ProfitLossBeforeTax"),
             "pat": _num(f, "ProfitOrLossAttributableToOwnersOfParent", "ProfitLossForPeriod"),
             "eps": _num(f, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                         "BasicEarningsLossPerShareFromContinuingOperations")}
        m["margin"] = ebitda / rev * 100 if ebitda is not None and rev and not lender else None
    for k in ("rev", "ebitda", "pbt", "pat"):
        if m[k] is not None:
            m[k] = round(m[k] / CR, 2)
    if m["margin"] is not None:
        m["margin"] = round(m["margin"], 2)
    if all(m[k] is None for k in ("rev", "pbt", "pat")):
        return None
    return m


def pct_change(cur, prev):
    """% change; None when either side is missing or the base is zero/negative (not meaningful)."""
    if cur is None or prev is None or prev <= 0:
        return None
    return round((cur - prev) / prev * 100, 1)


def growth(cur: dict, prev: dict | None) -> dict | None:
    if not cur or not prev:
        return None
    g = {k: pct_change(cur.get(k), prev.get(k)) for k in ("rev", "ebitda", "pbt", "pat", "eps")}
    a, b = cur.get("margin"), prev.get("margin")
    g["margin_bps"] = round((a - b) * 100) if a is not None and b is not None else None
    return g


def shift_quarter(qend: date, quarters: int) -> date:
    """Quarter end `quarters` back (positive = earlier)."""
    m = qend.month - 3 * quarters
    y = qend.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return nxt - timedelta(days=1)


def quarter_start(qend: date) -> date:
    return date(qend.year, qend.month - 2, 1)


# ---------------------------------------------------------------- concall time

_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
           "oct": 10, "nov": 11, "dec": 12}
_MON_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_D1 = re.compile(r"\b(\d{1,2})\s*(?:st|nd|rd|th)?\s*(?:day\s+of\s+|of\s+)?" + _MON_RE + r"\b[.,]?\s*(\d{4})?", re.I)
_D2 = re.compile(r"\b" + _MON_RE + r"\.?\s+(\d{1,2})\s*(?:st|nd|rd|th)?\b,?\s*(\d{4})?", re.I)
_D3 = re.compile(r"\b(\d{1,2})[./-](\d{1,2})[./-](20\d\d)\b")
_T1 = re.compile(r"\b(\d{1,2})\s*[:.]\s*(\d{2})\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?|hrs\b|hours\b)?", re.I)
_T2 = re.compile(r"\b(\d{1,2})\s*(a\.?\s?m\b\.?|p\.?\s?m\b\.?)", re.I)
_T3 = re.compile(r"\b([01]\d|2[0-3])([0-5]\d)\s*(?:hrs|hours)\b", re.I)


def _mk_date(y, m, d, hint: date):
    try:
        if y:
            y = int(y)
            if not hint.year - 1 <= y <= hint.year + 1:   # typo'd years ("2826") -> assume the season's
                y = None
        if not y:
            y = hint.year
            if date(y, m, int(d)) < hint - timedelta(days=200):
                y += 1
        return date(int(y), m, int(d))
    except ValueError:
        return None


def find_dates(text: str, hint: date) -> list:
    """[(pos, end, date)] for every date mention."""
    out = []
    for m in _D3.finditer(text):
        d = _mk_date(m.group(3), int(m.group(2)), m.group(1), hint) if 1 <= int(m.group(2)) <= 12 else None
        if d:
            out.append((m.start(), m.end(), d))
    for m in _D1.finditer(text):
        d = _mk_date(m.group(3), _MONTHS[m.group(2)[:3].lower()], m.group(1), hint)
        if d:
            out.append((m.start(), m.end(), d))
    for m in _D2.finditer(text):
        d = _mk_date(m.group(3), _MONTHS[m.group(1)[:3].lower()], m.group(2), hint)
        if d:
            out.append((m.start(), m.end(), d))
    return out


def find_times(text: str, date_spans: list) -> list:
    """[(pos, 'HH:MM')]; ignores digits that are part of a date."""
    def in_date(p):
        return any(a <= p < b for a, b, _ in date_spans)
    out = []
    for m in _T1.finditer(text):
        if in_date(m.start()):
            continue
        h, mi, suf = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
        if mi > 59:
            continue
        if suf.startswith("p") and h < 12:
            h += 12
        elif suf.startswith("a") and h == 12:
            h = 0
        elif not suf and not 8 <= h <= 23:
            if 1 <= h <= 7:
                h += 12   # "4.00" with no am/pm: earnings calls are not at 4 am
            else:
                continue
        if h <= 23:
            out.append((m.start(), "%02d:%02d" % (h, mi)))
    for m in _T2.finditer(text):
        if in_date(m.start()) or any(abs(m.start() - p) < 6 for p, _ in out):
            continue
        h = int(m.group(1))
        if not 1 <= h <= 12:
            continue
        h = (h % 12) + (12 if m.group(2).lower().startswith("p") else 0)
        out.append((m.start(), "%02d:00" % h))
    for m in _T3.finditer(text):
        if not any(abs(m.start() - p) < 6 for p, _ in out):
            out.append((m.start(), "%s:%s" % (m.group(1), m.group(2))))
    return out


def call_slots(text: str, hint: date) -> list:
    """[(date_iso, 'HH:MM')] pairing each time with the closest date mention (within 250 chars)."""
    text = re.sub(r"\s+", " ", text or "")
    dates = find_dates(text, hint)
    if not dates:
        return []
    slots = []
    for p, t in find_times(text, [(a, b, d) for a, b, d in dates]):
        best = min(dates, key=lambda x: abs(x[0] - p) + (0 if x[0] <= p else 40))
        if abs(best[0] - p) <= 250:
            slots.append((best[2].isoformat(), t))
    seen, out = set(), []
    for s in slots:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


CALL_RECORDING = re.compile(r"recording|transcript|audio|video", re.I)
CALL_EARNINGS = re.compile(r"earning|financial results?|results? for|financial performance|quarter (?:and half[- ]year )?ended|"
                           r"\bQ[1-4]\s?(?:&\s?H[12]\s?)?FY|\bH1\s?FY", re.I)


def pick_concall(decl_day: date, calls: list) -> dict | None:
    """Earnings call nearest the results: a scheduled slot from day-1 to day+2 (up to day+7 when the
    intimation is about the results), else the time the call recording was posted."""
    best = None
    for c in calls:
        reach = 7 if c.get("earn") else 2
        for d, t in c.get("slots") or []:
            dd = date.fromisoformat(d)
            if not decl_day - timedelta(days=1) <= dd <= decl_day + timedelta(days=reach):
                continue
            key = (abs((dd - decl_day).days) + (0.5 if dd < decl_day else 0), t)
            if best is None or key < best[0]:
                best = (key, {"date": d, "time": t, "link": c.get("link"), "kind": "scheduled"})
    if best:
        return best[1]
    recs = [c for c in calls if c.get("kind") == "recording" and c.get("ts")
            and decl_day <= date.fromisoformat(c["ts"][:10]) <= decl_day + timedelta(days=7)]
    if recs:
        c = min(recs, key=lambda c: c["ts"])
        return {"date": c["ts"][:10], "time": None, "posted": c["ts"][11:16], "link": c.get("link"),
                "kind": "recording"}
    return None


# ---------------------------------------------------------------- PDFs

def pdf_pages(data: bytes, max_pages: int = 60) -> list:
    if pypdf is None:
        raise RuntimeError("pypdf not installed (python3 -m pip install --user pypdf)")
    rd = pypdf.PdfReader(io.BytesIO(data))
    out = []
    for p in rd.pages[:max_pages]:
        try:
            out.append(p.extract_text() or "")
        except Exception:
            out.append("")
    return out


# ---------------------------------------------------------------- deck pointers

_BOILER = re.compile(
    r"disclaimer|forward[- ]looking|safe\s+harbou?r|copyright|all rights reserved|regulation\s+\d|sebi|"
    r"listing obligations|dear sir|pursuant|exchange plaza|registered office|\bcin\b|www\.|e-?mail|"
    r"scrip code|this presentation|investor presentation|thank you|for your information|company secretary|"
    r"no representation|not be relied|without notice|press release|earnings call|conference call|"
    r"source\s*:|note\s*:|in (?:rs|₹|inr)\.? ?(?:cr|mn|crore|million)|table of content|agenda", re.I)
_OUTLOOK = re.compile(
    r"outlook|guidance|expect|going forward|we (?:will|aim|plan|target|see|anticipate)|"
    r"remains? (?:confident|optimistic|positive|focused|committed|on track)|on track|optimistic|confident|"
    r"(?<![QH]\d)(?<![QH]\d )(?<!9M)(?<!9M )\bFY\s?'?2[7-9]E?\b|next (?:quarter|year|few)|medium[- ]term|long[- ]term|\bH2\b|second half|"
    r"\btarget|\bplan(?:s|ned)?\b|\baim", re.I)
_OUTLOOK_SOFT = re.compile(r"pipeline|order ?book|visibility|momentum|demand|headwind|tailwind|recovery|"
                           r"sustain|trajectory|capex", re.I)
_SELF = re.compile(r"\b(?:we|our|the company|company's|management|board)\b", re.I)
_MACRO = re.compile(r"\b(?:GDP|IIP|CPI|WPI|inflation|economy|economic|industry|sector|monsoon|"
                    r"government|RBI|repo|geopolitic\w*)\b", re.I)
_SKIP_PAGE = re.compile(r"disclaimer|forward[- ]looking statements|safe harbou?r|source of data", re.I)
_HIGHLIGHT = re.compile(
    r"\bgr[eo]w|growth|\bup\b|increas|record|highest|expan|improv|margin|bps|yoy|y-o-y|robust|strong|"
    r"declin|lower|impact|pressure|moderat|\bdriven\b|led by|on account of", re.I)
_SECTION = re.compile(
    r"outlook|guidance|highlights|executive summary|key messages|management commentary|ceo|md'?s message|"
    r"strategic priorities|way forward|takeaways|performance overview|summary", re.I)
_SPLIT = re.compile(r"\s*(?:[•▪●❖➢►■◆✓]|\s\d{1,2}[.)]\s(?=[A-Z])|(?<=[a-z%)])\.\s+(?=[A-Z]))\s*")


def _sentences(page: str) -> list:
    txt = re.sub(r"[ \t]+", " ", page or "")
    txt = re.sub(r"(\w)-\n(\w)", r"\1\2", txt)
    txt = re.sub(r"\s*\n\s*", " ", txt)
    return [s.strip(" -–|:;") for s in _SPLIT.split(txt) if s and s.strip()]


def _clip(s: str, n: int = 230) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= n:
        return s.rstrip(".") + "."
    cut = s[:n].rsplit(" ", 1)[0]
    return cut.rstrip(",;:") + "…"


def deck_points(pages: list) -> list:
    """Two extractive pointers: the strongest performance highlight and the strongest
    outlook/commentary line. Returns [] when the deck has no usable text (image-only)."""
    cands = []
    for pi, page in enumerate(pages):
        if _SKIP_PAGE.search(page or ""):
            continue
        head = re.sub(r"\s+", " ", page or "")[:160]
        sec = bool(_SECTION.search(head))
        for s in _sentences(page):
            words = s.split()
            if not 7 <= len(words) <= 60 or len(s) < 45:
                continue
            letters = sum(ch.isalpha() for ch in s)
            digits = sum(ch.isdigit() for ch in s)
            if letters / len(s) < 0.6 or digits / len(s) > 0.2 or _BOILER.search(s):
                continue
            if not re.search(r"\b(?:is|are|was|were|has|have|had|grew|rose|fell|expect\w*|remain\w*|continu\w*|"
                             r"deliver\w*|led|driven|reported|stood|saw|see|will|improv\w*|increas\w*|"
                             r"declin\w*|expand\w*|achiev\w*|added|posted|moderat\w*)\b", s, re.I):
                continue
            o, so, h = len(_OUTLOOK.findall(s)), len(_OUTLOOK_SOFT.findall(s)), len(_HIGHLIGHT.findall(s))
            base = ((2 if sec else 0) + (1 if re.search(r"\d", s) else 0) + max(0, 1.5 - pi * 0.05)
                    + (2 if _SELF.search(s) else 0) - 3 * len(_MACRO.findall(s)))
            cands.append({"s": s, "page": pi, "o": o * 3 + so + base, "h": min(h, 4) * 2 + base, "is_o": o > 0})
    if not cands:
        return []
    hi = max(cands, key=lambda c: c["h"])
    outs = [c for c in cands if c["is_o"] and c["s"] != hi["s"]]
    pts = [{"tag": "Highlights", "text": _clip(hi["s"])}]
    if outs:
        ou = max(outs, key=lambda c: c["o"])
        pts.append({"tag": "Outlook", "text": _clip(ou["s"])})
    else:
        rest = [c for c in cands if c["s"] != hi["s"]]
        if rest:
            pts.append({"tag": "Highlights", "text": _clip(max(rest, key=lambda c: c["h"])["s"])})
    return pts


CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_PROMPT = (
    "Below is the text of {name}'s investor presentation for {quarter}. Write exactly two pointers on "
    "management's view, each under 35 words, using only facts in the text:\n"
    "1. Highlights: the main performance driver or what management says about the quarter.\n"
    "2. Outlook: management's guidance, outlook or forward commentary (if the deck has none, a second "
    "key highlight and use the tag Highlights).\n"
    'Reply with JSON only: {{"points":[{{"tag":"Highlights","text":"..."}},{{"tag":"Outlook","text":"..."}}]}}\n\n'
    "<presentation>\n{text}\n</presentation>")


def claude_points(api_key: str, name: str, quarter: str, pages: list, max_chars: int = 150000) -> list | None:
    """Optional: summarize with Claude when an API key is configured. None on any failure,
    so the caller falls back to deck_points."""
    try:
        import anthropic
    except ImportError:
        return None
    text = "\n\n".join("[slide %d]\n%s" % (i + 1, p.strip()) for i, p in enumerate(pages) if p and p.strip())
    if len(text) < 300:
        return None
    text = text[:max_chars]   # cap cost on very long decks; the opening slides carry the commentary
    client = anthropic.Anthropic(api_key=api_key, timeout=120.0)
    resp = client.beta.messages.create(
        model=CLAUDE_MODEL, max_tokens=2000,
        betas=["server-side-fallback-2026-07-01"],
        output_config={"effort": "low"},
        extra_body={"fallbacks": "default"},
        messages=[{"role": "user", "content": CLAUDE_PROMPT.format(name=name, quarter=quarter, text=text)}])
    if resp.stop_reason == "refusal":
        return None
    out = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        return None
    pts = json.loads(m.group(0)).get("points") or []
    pts = [{"tag": str(p.get("tag") or "Highlights")[:20], "text": _clip(str(p.get("text") or ""), 260)}
           for p in pts if p.get("text")]
    return pts[:2] or None
