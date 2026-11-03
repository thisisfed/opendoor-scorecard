#!/usr/bin/env python3
"""
Opendoor (OPEN) investor scorecard  -  v2

Data sources (all free, pulled live unless --offline):
  1. SEC EDGAR XBRL      - reported GAAP financials (revenue, net income, cash, equity, debt, shares)
  2. accountable.opendoor.com - Opendoor's own weekly tracker: acquisition contracts vs its projected
                           range, resale closings (COEs) vs management's expectation, relative clearance vs MLS
  3. Yahoo Finance       - daily prices (falls back to Stooq)
  4. opendoor_kpis.csv   - figures only published in earnings letters (homes bought/sold, contribution
                           margin, adjusted EBITDA). Values here override EDGAR.
  5. CAPITAL_EVENTS      - financing deals announced after the last reported quarter, applied pro forma.

Rates the company 0-10 on eight pillars and gives an overall grade.

Usage
  python3 opendoor_scorecard.py                        # live
  python3 opendoor_scorecard.py --offline              # csv + accountable_snapshot.json + price snapshot
  python3 opendoor_scorecard.py --shares 500 --avg-cost 4.20
  python3 opendoor_scorecard.py --export-rows rows     # flat JSON files for the dashboard
Output goes to www/ : index.html (the report), scorecard.json, history.csv (one line per run).
Optional phone alerts: set NTFY_TOPIC=<private topic> (ntfy.sh app) - sent when the score moves.

Each quarter: add a row to opendoor_kpis.csv from the earnings letter (investor.opendoor.com) and
remove CAPITAL_EVENTS that the new quarter's balance sheet already includes.
Standard library only. SEC asks for a contact User-Agent: set SEC_UA="Your Name you@email.com".
Not investment advice: the thresholds in RULES are opinions, tune them.
"""
import argparse, csv, datetime as dt, html, json, os, re, sys, urllib.request

TICKER, CIK = "OPEN", "0001801169"
HERE = os.path.dirname(os.path.abspath(__file__))

# Used by --offline or when the price feed fails. Source: daily closes (S&P Global via stockanalysis.com).
PRICE_SNAPSHOT = {"date": "2026-10-30", "price": 1.80, "high_52w": 9.15, "low_52w": 1.76,
                  "return_1y": -0.7548, "ma200": 4.19}

# Deals announced after the last reported quarter (30 Jun 2026). Applied when the event date is later
# than the latest quarter in the data. Source: Opendoor 8-K, 13 Aug 2026 (settled 19 Aug 2026).
CAPITAL_EVENTS = [
    {"date": "2026-08-19", "what": "$650M 0% convertible notes due 2030, $158M buyback of 45.3M shares, $52.5M capped call",
     "total_debt_musd": +650.0, "cash_musd": +650.0 - 158.0 - 52.5, "equity_musd": -158.0 - 52.5, "shares_m": -45.3},
]

# ---------------------------------------------------------------- scoring rules
# metric: (label, value scoring 0, value scoring 10, format). Linear in between, clipped.
RULES = {
    "rev_nowcast_yoy": ("Revenue YoY, latest qtr (est. until reported)",          -0.40, 0.30, "pct"),
    "coe_q4_yoy":      ("Quarter-to-date homes sold YoY",          -0.20, 0.50, "pct"),
    "contracts_qoq":   ("Acquisition contracts, last qtr vs prior",    -0.20, 0.20, "pct"),
    "contrib_margin":  ("Contribution margin",                 0.00, 0.07, "pct"),
    "gross_margin":    ("Gross margin",                        0.05, 0.12, "pct"),
    "net_margin_2q":   ("GAAP net margin, last 2 qtrs",       -0.20, 0.02, "pct"),
    "adj_ebitda_m":    ("Adj. EBITDA margin",                 -0.05, 0.03, "pct"),
    "coe_vs_guide":    ("Last qtr homes sold vs mgmt expectation",  -0.15, 0.05, "pct"),
    "contracts_vs_proj": ("Contracts, last 4 wks vs projection midpoint", -0.40, 0.10, "pct"),
    "clearance":       ("Relative clearance vs MLS, 4-wk avg", 0.90, 1.30, "x"),
    "cash_to_debt":    ("Cash / total debt (pro forma)",       0.20, 1.00, "x"),
    "debt_to_equity":  ("Debt / equity (pro forma)",           4.00, 0.50, "x"),
    "share_yoy":       ("Share count change YoY",              0.30, 0.00, "pct"),
    "sbc_pct":         ("Stock comp / revenue",                0.10, 0.01, "pct"),
    "ps":              ("Price / sales (TTM)",                 3.00, 0.30, "x"),
    "pb":              ("Price / book (pro forma)",            6.00, 1.00, "x"),
    "ret_1y":          ("Share price, 1 year",                -0.70, 0.30, "pct"),
    "range_pos":       ("Position in 52-week range",           0.00, 1.00, "pct"),
    "vs_ma200":        ("Price vs 200-day average",           -0.40, 0.10, "pct"),
}
PILLARS = [  # (name, weight %, metrics, the question it answers)
    ("Growth",         15, ["rev_nowcast_yoy", "coe_q4_yoy", "contracts_qoq"], "Is the business getting bigger right now?"),
    ("Unit economics", 15, ["contrib_margin", "gross_margin"],               "Does each home flip make money?"),
    ("Profitability",  15, ["net_margin_2q", "adj_ebitda_m"],                "Does the whole company make money?"),
    ("Execution",      15, ["coe_vs_guide", "contracts_vs_proj", "clearance"], "Is management hitting its own targets?"),
    ("Balance sheet",  15, ["cash_to_debt", "debt_to_equity"],               "Can it survive a bad housing year?"),
    ("Dilution",       10, ["share_yoy", "sbc_pct"],                         "Is your slice of the company shrinking?"),
    ("Valuation",      10, ["ps", "pb"],                                     "Are you overpaying for what exists today?"),
    ("Momentum",        5, ["ret_1y", "range_pos", "vs_ma200"],              "What is the market saying?"),
]
GRADES = [(8.0, "A", "Strong: the numbers back the story."),
          (6.5, "B", "Good: improving, with manageable risks."),
          (5.0, "C", "Mixed: the turnaround is plausible but unproven."),
          (3.5, "D", "Weak: speculative; size the position accordingly."),
          (0.0, "F", "Poor: the numbers do not support the price yet.")]


def lin(x, bad, good):
    if x is None:
        return None
    return round(max(0.0, min(1.0, (x - bad) / (good - bad))) * 10, 1)


def _days(a, b):
    return (dt.date.fromisoformat(b) - dt.date.fromisoformat(a)).days


BROWSER_UA = "Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


def get(url, ua=BROWSER_UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


# ---------------------------------------------------------------- 1. SEC EDGAR
def edgar_facts(ua):
    return json.loads(get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK}.json", ua))


def _items(facts, tags, unit="USD"):
    for ns in ("us-gaap", "dei", "open"):
        for tag in tags:
            node = facts.get("facts", {}).get(ns, {}).get(tag)
            if node and unit in node.get("units", {}):
                best = {}
                for it in node["units"][unit]:
                    if it.get("form", "")[:4] not in ("10-Q", "10-K"):
                        continue
                    key = (it.get("start"), it["end"])
                    if key not in best or it["filed"] > best[key]["filed"]:
                        best[key] = it
                return list(best.values()), f"{ns}:{tag}"
    return [], None


def quarterly_flow(facts, tags):
    """{quarter_end: value} for a P&L / cash-flow item; derives quarters from YTD or annual figures."""
    items, tag = _items(facts, tags)
    by = {(i["start"], i["end"]): i["val"] for i in items if i.get("start")}
    out = {e: v for (s, e), v in by.items() if 80 <= _days(s, e) <= 100}
    for (s, e), v in by.items():
        if e in out or _days(s, e) < 170:
            continue
        for (s2, e2), v2 in by.items():
            if s2 == s and 80 <= _days(e2, e) <= 100:
                out[e] = v - v2
                break
    return out, tag


def instant(facts, tags, unit="USD"):
    items, tag = _items(facts, tags, unit)
    return {i["end"]: i["val"] for i in items if not i.get("start")}, tag


def _quarter_end_before(iso):
    d = dt.date.fromisoformat(iso)
    ends = [dt.date(d.year - 1, 12, 31)] + [dt.date(d.year, m, dd) for m, dd in ((3, 31), (6, 30), (9, 30), (12, 31))]
    return max(e for e in ends if e <= d).isoformat()


def from_edgar(ua, log):
    f, M, fields = edgar_facts(ua), 1e6, {}
    def put(name, series, tag, scale=M):
        log.append(f"  {name:<18} <- {tag or 'NOT FOUND'}")
        for end, v in series.items():
            fields.setdefault(end, {})[name] = v / scale
    s, t = quarterly_flow(f, ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax"]); put("revenue_musd", s, t)
    s, t = quarterly_flow(f, ["GrossProfit"]); put("gross_profit_musd", s, t)
    s, t = quarterly_flow(f, ["NetIncomeLoss"]); put("net_income_musd", s, t)
    s, t = quarterly_flow(f, ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"]); put("sbc_musd", s, t)
    ocf, t1 = quarterly_flow(f, ["NetCashProvidedByUsedInOperatingActivities"])
    capex, t2 = quarterly_flow(f, ["PaymentsToAcquirePropertyPlantAndEquipment"])
    put("fcf_musd", {k: v - capex.get(k, 0) for k, v in ocf.items()}, f"{t1} - {t2}")
    s, t = instant(f, ["CashAndCashEquivalentsAtCarryingValue"]); put("cash_musd", s, t)
    s, t = instant(f, ["InventoryRealEstate", "RealEstateInventory", "InventoryNet"]); put("inventory_musd", s, t)
    s, t = instant(f, ["StockholdersEquity"]); put("equity_musd", s, t)
    total, used = {}, []
    for tags in (["AssetBackedSecuritiesDebtCurrent", "NonRecourseDebtCurrent", "SecuredDebtCurrent", "AssetBackedDebtCurrent"],
                 ["AssetBackedSecuritiesDebtNoncurrent", "NonRecourseDebtNoncurrent", "SecuredDebtNoncurrent", "AssetBackedDebtNoncurrent"],
                 ["ConvertibleNotesPayable", "ConvertibleDebtNoncurrent", "ConvertibleNotesPayableNoncurrent"]):
        vals, tag = instant(f, tags)
        if tag:
            used.append(tag)
            for k, v in vals.items():
                total[k] = total.get(k, 0) + v
    if total:
        put("total_debt_musd", total, " + ".join(used))
    if os.environ.get("DEBT_DIAG"):  # list every debt-like balance-sheet field with its latest value
        for ns, tags in f.get("facts", {}).items():
            for tag, node in tags.items():
                if True:
                    vals = [i for i in node.get("units", {}).get("USD", []) if not i.get("start") and i.get("form", "")[:4] in ("10-Q", "10-K")]
                    last = max(vals, key=lambda i: (i["end"], i["filed"])) if vals else None
                    if last and last["end"] >= "2026-06-30" and 150e6 <= abs(last["val"]) <= 3000e6:
                        log.append(f"  [debt diag] {ns}:{tag} = {last['val']/1e6:,.0f}M at {last['end']}")
    s, t = instant(f, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"], unit="shares")
    put("shares_m", {_quarter_end_before(k): v for k, v in sorted(s.items())}, t)
    return fields


# ---------------------------------------------------------------- 2. accountable.opendoor.com
def parse_accountable(page):
    """Pull the three chart data sets out of the page's embedded Next.js payload."""
    pushes = re.findall(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)', page)
    lines = "".join(json.loads(p) for p in pushes).split("\n")
    nodes = {}
    for line in lines:
        key, _, body = line.partition(":")
        if body.startswith("[") and ('"chart":[{"date"' in body or '"title":"Resale COEs"' in body
                                     or '"title":"Relative Clearance"' in body):
            try:
                nodes[key] = json.loads(body)[3]
            except (ValueError, IndexError, TypeError):
                pass
    def resolve(obj, root):  # "$a1:props:variants:0:priorYear" style references
        if isinstance(obj, str) and obj.startswith("$") and ":props:" in obj:
            cur = root
            for part in obj.split(":props:", 1)[1].split(":"):
                cur = cur[int(part)] if isinstance(cur, list) else cur[part]
            return cur
        return obj
    out = {"as_of": None}
    m = re.search(r"Data as of ([A-Z][a-z]+ \d{1,2}, \d{4})", page)
    if m:
        out["as_of"] = dt.datetime.strptime(m.group(1), "%b %d, %Y").date().isoformat()
    for n in nodes.values():
        if "chart" in n:
            out["contracts"] = [(r["date"], r.get("actual"), r.get("proposedLow"), r.get("proposedHigh"), r.get("priorYear"))
                                for r in n["chart"]]
        elif n.get("title") == "Relative Clearance":
            out["clearance"] = [(p["date"], p.get("value")) for v in n["variants"] for p in v["points"]]
        elif n.get("title") == "Resale COEs":
            out["coe"] = []
            for v in n["variants"]:
                cp = v.get("comparisonPoints", {})
                out["coe"].append({"label": v["label"],
                    "actual": [(p["date"], p.get("value")) for p in v["points"]],
                    "expectation": [(p["date"], p.get("value")) for p in resolve(cp.get("expectation", []), {"variants": n["variants"]}) or []],
                    "prior_year": [(p["date"], p.get("value")) for p in resolve(cp.get("priorYear", []), {"variants": n["variants"]}) or []]})
    if "contracts" not in out or not out.get("coe"):
        raise ValueError("accountable page layout changed: charts not found")
    return out


def accountable_from_snapshot(path):
    s = json.load(open(path))
    pairs = lambda txt, f=float: [(d, f(v) if v else None) for d, v in (x.split(",") for x in txt.split(";"))]
    contracts = []
    for r in s["acquisition_contracts_weekly"].split(";"):
        d, a, lo, hi, py = r.split(",")
        contracts.append((d, int(a) if a else None, int(lo), int(hi), int(py)))
    g = lambda k: pairs(s[k]) if s.get(k) else []
    if "resale_coe_prev_cumulative" in s:
        coe = [{"label": s.get("resale_coe_prev_label", "prev"), "actual": g("resale_coe_prev_cumulative"),
                "expectation": g("resale_coe_prev_expectation"), "prior_year": g("resale_coe_prev_prior_year")},
               {"label": s.get("resale_coe_curr_label", "curr"), "actual": g("resale_coe_curr_cumulative"),
                "expectation": g("resale_coe_curr_expectation"), "prior_year": g("resale_coe_curr_prior_year")}]
    else:  # first snapshot format (Q3/Q4 2026)
        coe = [{"label": "Q3", "actual": g("resale_coe_q3_cumulative"), "expectation": g("resale_coe_q3_expectation"),
                "prior_year": g("resale_coe_q3_prior_year")},
               {"label": "Q4", "actual": g("resale_coe_q4_cumulative"), "expectation": [],
                "prior_year": g("resale_coe_q4_prior_year")}]
    return {"as_of": s["as_of"], "contracts": contracts, "clearance": pairs(s["relative_clearance_weekly"]), "coe": coe}


def accountable_metrics(acc):
    c = [r for r in acc["contracts"] if r[1] is not None]
    def total(lo, hi):
        return sum(r[1] for r in c if lo <= r[0] <= hi)
    last4 = c[-4:]
    a4, lo4, hi4 = sum(r[1] for r in last4), sum(r[2] for r in last4), sum(r[3] for r in last4)
    # the two most recent complete calendar quarters before the latest week
    last_week = dt.date.fromisoformat(c[-1][0])
    qs = dt.date(last_week.year, 3 * ((last_week.month - 1) // 3) + 1, 1)  # start of current quarter
    def prev_q(d):
        m, y = d.month - 3, d.year
        return dt.date(y - 1, m + 12, 1) if m < 1 else dt.date(y, m, 1)
    b_start, a_start = prev_q(qs), prev_q(prev_q(qs))
    q3 = total(b_start.isoformat(), (qs - dt.timedelta(days=1)).isoformat())
    q2 = total(a_start.isoformat(), (b_start - dt.timedelta(days=1)).isoformat())
    prev, curr = acc["coe"][0], acc["coe"][-1]
    q3a = [v for _, v in prev["actual"] if v is not None]
    q3e = [v for _, v in prev["expectation"] if v is not None]
    q4 = [(d, v) for d, v in curr["actual"] if v is not None] if len(acc["coe"]) > 1 else []
    q4py = dict(curr["prior_year"])
    cl = [v for _, v in acc["clearance"] if v is not None]
    return {
        "as_of": acc["as_of"],
        "prev_quarter": prev["label"], "curr_quarter": curr["label"],
        "prev_quarter_end": (qs - dt.timedelta(days=1)).isoformat(),
        "q3_homes_sold": q3a[-1] if q3a else None,
        "q3_homes_sold_expected": q3e[-1] if q3e else None,
        "coe_vs_guide": q3a[-1] / q3e[-1] - 1 if q3a and q3e else None,
        "q4_homes_sold_qtd": q4[-1][1] if q4 else None,
        "q4_qtd_date": q4[-1][0] if q4 else None,
        "coe_q4_yoy": q4[-1][1] / q4py[q4[-1][0]] - 1 if q4 and q4py.get(q4[-1][0]) else None,
        "contracts_q2": q2, "contracts_q3": q3,
        "contracts_qoq": q3 / q2 - 1 if q2 and q3 else None,
        "contracts_last4": a4, "contracts_last4_proj_low": lo4, "contracts_last4_proj_high": hi4,
        "contracts_vs_proj": a4 / ((lo4 + hi4) / 2) - 1,
        "weeks_below_proj_13": sum(1 for r in c[-13:] if r[1] < r[2]),
        "clearance": sum(cl[-4:]) / len(cl[-4:]) if cl else None,
        "weekly": c,
    }


# ---------------------------------------------------------------- 3. prices
def _yahoo(host):
    j = json.loads(get(f"https://{host}/v8/finance/chart/{TICKER}?range=2y&interval=1d"))
    r = j["chart"]["result"][0]
    return [(dt.datetime.fromtimestamp(t, dt.timezone.utc).date().isoformat(), float(c)) for t, c in
            zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c is not None]


def _nasdaq():
    start = (dt.date.today() - dt.timedelta(days=730)).isoformat()
    req = urllib.request.Request(
        f"https://api.nasdaq.com/api/quote/{TICKER}/historical?assetclass=stocks&fromdate={start}&limit=9999",
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*",
                 "Accept-Language": "en-US,en;q=0.9", "Origin": "https://www.nasdaq.com",
                 "Referer": "https://www.nasdaq.com/", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    rows = []
    for r in j["data"]["tradesTable"]["rows"]:
        m, d, y = r["date"].split("/")
        rows.append((f"{y}-{m}-{d}", float(r["close"].replace("$", "").replace(",", ""))))
    return sorted(rows)


def _stooq():
    txt = get(f"https://stooq.com/q/d/l/?s={TICKER.lower()}.us&i=d")
    if not txt.startswith("Date,"):
        raise ValueError("Stooq returned a non-CSV page")
    return [(l.split(",")[0], float(l.split(",")[4])) for l in txt.strip().splitlines()[1:]]


def prices(ua=None, log=None):
    """Daily closes from the first source that answers: Yahoo (two hosts), Nasdaq, Stooq."""
    sources = [("Yahoo Finance", lambda: _yahoo("query1.finance.yahoo.com")),
               ("Yahoo Finance", lambda: _yahoo("query2.finance.yahoo.com")),
               ("Nasdaq", _nasdaq), ("Stooq", _stooq)]
    rows, src, errors = None, None, []
    for name, fetch in sources:
        try:
            rows = fetch()
            if len(rows) < 30:
                raise ValueError(f"only {len(rows)} rows")
            src = name
            break
        except Exception as e:
            errors.append(f"{name}: {str(e)[:80]}")
    if log is not None and errors:
        log.append("price sources that failed: " + "; ".join(errors))
    if rows is None:
        raise RuntimeError("all price sources failed")
    closes = [c for _, c in rows]
    last_date, price = rows[-1]
    yr = closes[-253:]
    return {"date": last_date, "price": price, "high_52w": max(yr), "low_52w": min(yr),
            "return_1y": price / yr[0] - 1 if len(closes) >= 253 else None,
            "ma200": sum(closes[-200:]) / 200 if len(closes) >= 200 else None, "source": src}


# ---------------------------------------------------------------- 4. csv + merge
def load_kpis(path):
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as fh:
            for row in csv.DictReader(fh):
                end = row.pop("quarter_end").strip()
                note = row.pop("note", "")
                out[end] = {k: float(v) for k, v in row.items() if v not in (None, "")}
                if note:
                    out[end]["note"] = note
    return out


def merge(edgar, kpis):
    rows = {}
    for end, vals in edgar.items():
        rows.setdefault(end, {}).update(vals)
    for end, vals in kpis.items():
        match = next((e for e in rows if abs(_days(e, end)) <= 5), end)
        rows.setdefault(match, {}).update(vals)
    q = [dict(quarter_end=e, **v) for e, v in sorted(rows.items()) if "revenue_musd" in v]
    return [r for r in q if dt.date.fromisoformat(r["quarter_end"]).month in (3, 6, 9, 12)]


def latest_with(q, *keys):
    """Most recent quarter row that has all of keys (falls back when a new quarter is only partly filled in)."""
    return next((r for r in reversed(q) if all(r.get(k) is not None for k in keys)), {})


def pro_forma(q):
    """Latest complete balance sheet plus capital events dated after it."""
    bs = latest_with(q, "cash_musd", "total_debt_musd", "equity_musd")
    sh = latest_with(q, "shares_m")
    pf = {k: bs.get(k) for k in ("cash_musd", "total_debt_musd", "equity_musd")}
    pf["shares_m"] = sh.get("shares_m")
    pf["as_of"] = bs.get("quarter_end")
    applied = []
    for ev in CAPITAL_EVENTS:
        if bs and ev["date"] > bs["quarter_end"]:
            for k in ("cash_musd", "total_debt_musd", "equity_musd"):
                if pf[k] is not None and k in ev:
                    pf[k] += ev[k]
    for ev in CAPITAL_EVENTS:  # share changes after the date the share count was measured
        if sh and pf["shares_m"] is not None and ev["date"] > sh["quarter_end"] and "shares_m" in ev:
            pf["shares_m"] += ev["shares_m"]
            applied.append(ev)
    return pf, applied


# ---------------------------------------------------------------- metrics + score
def metrics(q, px, acc):
    last = q[-1]
    g = lambda i, k: q[i].get(k) if -len(q) <= i < len(q) else None
    div = lambda a, b: a / b if a is not None and b not in (None, 0) else None
    ch = lambda a, b: a / b - 1 if a is not None and b not in (None, 0) else None
    pf, applied = pro_forma(q)
    shares = pf["shares_m"]
    mcap = px["price"] * shares if shares else None
    ttm_rev = sum(r["revenue_musd"] for r in q[-4:]) if len(q) >= 4 else None
    # share count a year before the price date
    target = (dt.date.fromisoformat(px["date"]) - dt.timedelta(days=365)).isoformat()
    year_ago = min((r for r in q if r.get("shares_m")), key=lambda r: abs(_days(r["quarter_end"], target)), default=None)
    # Q3 revenue estimate: Opendoor's Q3 homes sold x revenue per home sold over the last two reported quarters
    hs = [r for r in q if r.get("homes_sold")][-2:]
    rph = div(sum(r["revenue_musd"] for r in hs), sum(r["homes_sold"] for r in hs))
    unreported = bool(acc and acc.get("prev_quarter_end") and acc["prev_quarter_end"] > last["quarter_end"])
    q3_rev_est = acc["q3_homes_sold"] * rph if unreported and acc.get("q3_homes_sold") and rph else None
    q3_prior = None
    if unreported:
        pe = acc["prev_quarter_end"]
        q3_prior = next((r["revenue_musd"] for r in q if r["quarter_end"] == str(int(pe[:4]) - 1) + pe[4:]), None)
    two = q[-2:]
    m = {
        "rev_nowcast_yoy": ch(q3_rev_est, q3_prior) if unreported else ch(g(-1, "revenue_musd"), g(-5, "revenue_musd")),
        "coe_q4_yoy": acc.get("coe_q4_yoy") if acc else None,
        "contracts_qoq": acc.get("contracts_qoq") if acc else None,
        "contrib_margin": div(latest_with(q, "contribution_margin_pct").get("contribution_margin_pct"), 100),
        "gross_margin": div(latest_with(q, "gross_profit_musd").get("gross_profit_musd"), latest_with(q, "gross_profit_musd").get("revenue_musd")),
        "net_margin_2q": div(sum(r["net_income_musd"] for r in two), sum(r["revenue_musd"] for r in two))
                         if all("net_income_musd" in r for r in two) else None,
        "adj_ebitda_m": div(latest_with(q, "adj_ebitda_musd").get("adj_ebitda_musd"), latest_with(q, "adj_ebitda_musd").get("revenue_musd")),
        "coe_vs_guide": acc.get("coe_vs_guide") if acc else None,
        "contracts_vs_proj": acc.get("contracts_vs_proj") if acc else None,
        "clearance": acc.get("clearance") if acc else None,
        "cash_to_debt": div(pf["cash_musd"], pf["total_debt_musd"]),
        "debt_to_equity": div(pf["total_debt_musd"], pf["equity_musd"]),
        "share_yoy": ch(shares, year_ago["shares_m"]) if year_ago else None,
        "sbc_pct": div(latest_with(q, "sbc_musd").get("sbc_musd"), latest_with(q, "sbc_musd").get("revenue_musd")),
        "ps": div(mcap, ttm_rev),
        "pb": div(mcap, pf["equity_musd"]),
        "ret_1y": px.get("return_1y"),
        "range_pos": div(px["price"] - px["low_52w"], px["high_52w"] - px["low_52w"]),
        "vs_ma200": ch(px["price"], px.get("ma200")),
    }
    extra = {"market_cap_musd": mcap, "ttm_revenue_musd": ttm_rev, "shares_m": shares, "pro_forma": pf,
             "capital_events": [e["what"] for e in applied], "q3_revenue_estimate_musd": q3_rev_est,
             "revenue_per_home_k": rph * 1000 if rph else None, "q3_prior_revenue_musd": q3_prior,
             "shares_year_ago": (year_ago or {}).get("shares_m"), "shares_year_ago_date": (year_ago or {}).get("quarter_end"),
             "reported_rev_yoy": ch(g(-1, "revenue_musd"), g(-5, "revenue_musd"))}
    return m, extra


def score(m):
    pillars, total, wsum = [], 0.0, 0
    for name, w, keys, ask in PILLARS:
        items = []
        for k in keys:
            label, bad, good, f = RULES[k]
            items.append({"key": k, "label": label, "value": m.get(k), "score": lin(m.get(k), bad, good),
                          "fmt": f, "bad": bad, "good": good})
        got = [i["score"] for i in items if i["score"] is not None]
        ps = round(sum(got) / len(got), 1) if got else None
        if ps is not None:
            total += ps * w
            wsum += w
        pillars.append({"name": name, "weight": w, "question": ask, "score": ps, "metrics": items})
    overall = round(total / wsum, 2) if wsum else None
    grade, verdict = next((g, v) for t, g, v in GRADES if overall is not None and overall >= t)
    return {"overall": overall, "grade": grade, "verdict": verdict, "pillars": pillars, "weights_used": wsum}


# ---------------------------------------------------------------- output
def fmt(v, kind):
    if v is None:
        return "n/a"
    return f"{v*100:+.1f}%" if kind == "pct" else f"{v:.2f}x"


def print_report(res):
    s, px, ex, acc = res["score"], res["price"], res["extra"], res.get("accountable") or {}
    print(f"\nOpendoor ({TICKER}) scorecard  -  close ${px['price']:.2f} on {px['date']} ({px.get('source')})")
    print(f"Overall {s['overall']:.2f}/10  grade {s['grade']}  -  {s['verdict']}\n")
    for p in s["pillars"]:
        print(f"{p['name']:<15} {str(p['score']):>5}/10  (weight {p['weight']}%)  {p['question']}")
        for i in p["metrics"]:
            sc = "    -" if i["score"] is None else f"{i['score']:>5}"
            print(f"    {i['label']:<46} {fmt(i['value'], i['fmt']):>9} {sc}")
    if acc:
        print(f"\nOpendoor tracker (data as of {acc['as_of']}): {acc['prev_quarter']} homes sold {acc['q3_homes_sold']:,.0f} vs expected "
              f"{acc['q3_homes_sold_expected']:,.0f}; contracts last 4 wks {acc['contracts_last4']:,} vs projected "
              f"{acc['contracts_last4_proj_low']:,}-{acc['contracts_last4_proj_high']:,}; "
              f"{acc['weeks_below_proj_13']} of last 13 weeks below projection.")
    if ex.get("q3_revenue_estimate_musd"):
        print(f"{acc.get('prev_quarter')} revenue estimate ${ex['q3_revenue_estimate_musd']:,.0f}M = {acc.get('q3_homes_sold'):,.0f} homes x "
              f"${ex['revenue_per_home_k']:.1f}K (vs ${ex['q3_prior_revenue_musd']:,.0f}M a year ago)")
    if ex["capital_events"]:
        print("Pro forma for: " + "; ".join(ex["capital_events"]))
    if res.get("position"):
        pos = res["position"]
        print(f"\nYour position: {pos['shares']:g} sh @ ${pos['avg_cost']:.2f}  value ${pos['value']:,.0f}  "
              f"P/L ${pos['pnl']:,.0f} ({pos['pnl_pct']*100:+.1f}%)  breakeven needs {pos['to_breakeven']*100:+.0f}%")
    print("\nNot investment advice. Thresholds live in RULES at the top of the script.\n")


def write_html(res, path):
    s = res["score"]
    color = lambda x: "#9aa0a6" if x is None else ("#2e7d32" if x >= 6.5 else "#f9a825" if x >= 4 else "#c62828")
    rows = ""
    for p in s["pillars"]:
        rows += (f"<tr class='p'><td>{p['name']}<div class='q'>{p['question']}</div></td><td>{p['weight']}%</td>"
                 f"<td><div class='bar'><span style='width:{(p['score'] or 0)*10}%;background:{color(p['score'])}'></span></div></td>"
                 f"<td><b>{p['score'] if p['score'] is not None else 'n/a'}</b></td></tr>")
        for i in p["metrics"]:
            rows += (f"<tr><td class='m'>{html.escape(i['label'])}</td><td>{fmt(i['value'], i['fmt'])}</td>"
                     f"<td class='t'>0 at {fmt(i['bad'], i['fmt'])} &middot; 10 at {fmt(i['good'], i['fmt'])}</td>"
                     f"<td>{i['score'] if i['score'] is not None else '&ndash;'}</td></tr>")
    n = lambda r, k, d=0: "&ndash;" if r.get(k) is None else f"{r[k]:,.{d}f}"
    qrows = "".join(f"<tr><td>{r['quarter_end']}</td><td>{n(r,'revenue_musd')}</td><td>{n(r,'net_income_musd')}</td>"
                    f"<td>{n(r,'homes_purchased')}</td><td>{n(r,'homes_sold')}</td><td>{n(r,'contribution_margin_pct',1)}</td>"
                    f"<td>{n(r,'cash_musd')}</td></tr>" for r in res["quarters"][-8:])
    ch = res.get("change")
    if ch:
        arrow = lambda v: "" if v is None else (f"<span style='color:#2e7d32'>&#9650; {v:+.2f}</span>" if v > 0.004 else
                                                 f"<span style='color:#c62828'>&#9660; {v:+.2f}</span>" if v < -0.004 else "<span style='color:#888'>no change</span>")
        change_html = (f"<p><b>Since last run ({html.escape(ch['since'][:16].replace('T', ' '))}):</b> {arrow(ch['overall'])}"
                       + (f" &middot; grade was {ch['grade_from']}" if ch["grade_from"] != s["grade"] else " &middot; grade unchanged") + "</p>")
    else:
        change_html = "<p style='color:#888'>First run: changes will show from the next run.</p>"
    hist = res.get("history") or []
    history_html = ""
    if hist:
        pts = [(h["run_at"][:10], float(h["overall"])) for h in hist if h.get("overall") not in (None, "", "None")]
        if len(pts) >= 2:
            W, H, pad = 640, 160, 30
            xs = lambda i: pad + i * (W - 2 * pad) / (len(pts) - 1)
            ys = lambda v: H - pad - v / 10 * (H - 2 * pad)
            line_d = " ".join(f"{'M' if i == 0 else 'L'}{xs(i):.1f},{ys(v):.1f}" for i, (_, v) in enumerate(pts))
            grid = "".join(f"<line x1='{pad}' x2='{W-pad}' y1='{ys(t):.1f}' y2='{ys(t):.1f}' stroke='#eee'/><text x='4' y='{ys(t)+4:.1f}' font-size='10' fill='#999'>{t}</text>" for t in (0, 5, 10))
            dots = "".join(f"<circle cx='{xs(i):.1f}' cy='{ys(v):.1f}' r='3' fill='#1a73e8'><title>{d}: {v:.2f}</title></circle>" for i, (d, v) in enumerate(pts))
            history_html += (f"<h2>Overall score over time</h2><svg viewBox='0 0 {W} {H}' width='100%'>{grid}"
                             f"<path d='{line_d}' fill='none' stroke='#1a73e8' stroke-width='2'/>{dots}</svg>")
        heads = "".join(f"<th>{p[0]}</th>" for p in PILLARS)
        body = "".join(f"<tr><td>{h['run_at'][:16].replace('T', ' ')}</td><td>${float(h['price']):.2f}</td><td>{h['tracker_as_of']}</td>"
                       f"<td><b>{float(h['overall']):.2f}</b> {h['grade']}</td>" + "".join(f"<td>{h.get(p[0], '')}</td>" for p in PILLARS) + "</tr>"
                       for h in reversed(hist[-26:]))
        history_html += (f"<h2>Run history (newest first)</h2><div style='overflow-x:auto'><table><tr><th>Run</th><th>Price</th>"
                         f"<th>Tracker data</th><th>Overall</th>{heads}</tr>{body}</table></div>")
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OPEN scorecard {res['price']['date']}</title>
<style>body{{font:15px system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
.big{{font-size:3rem;font-weight:700;color:{color(s['overall'])}}}table{{border-collapse:collapse;width:100%;margin:1rem 0}}
td,th{{padding:.35rem .5rem;border-bottom:1px solid #eee;text-align:left}}tr.p td{{background:#fafafa;font-weight:600}}
.q{{font-weight:400;color:#777;font-size:.8rem}}.m{{padding-left:1.5rem}}.t{{color:#999;font-size:.8rem}}
.bar{{background:#eee;height:10px;border-radius:5px;width:min(160px,22vw)}}@media (max-width:600px){{.t{{display:none}}td,th{{padding:.3rem .3rem}}body{{margin:1rem auto}}}}.bar span{{display:block;height:10px;border-radius:5px}}</style></head><body>
<h1>Opendoor ({TICKER}) investor scorecard</h1>
<p>Close ${res['price']['price']:.2f} on {res['price']['date']} &middot; market cap ${(res['extra']['market_cap_musd'] or 0)/1000:.2f}bn &middot; updated {res['generated'].replace('T', ' ')}</p>
<div class="big">{s['overall']:.2f}/10 &nbsp;{s['grade']}</div><p>{s['verdict']}</p>{change_html}
<table><tr><th>Pillar / metric</th><th>Weight / value</th><th></th><th>Score</th></tr>{rows}</table>
{history_html}<h2>Quarters ($M)</h2><table><tr><th>Quarter end</th><th>Revenue</th><th>Net income</th><th>Bought</th><th>Sold</th><th>Contrib. %</th><th>Cash</th></tr>{qrows}</table>
<p style="color:#888;font-size:.8rem">Updates automatically every weekday evening (UK time). Times shown are UK time.
Download: <a href="history.csv">score history (CSV)</a> &middot; <a href="scorecard.json">full data (JSON)</a>.<br>
Sources: SEC EDGAR XBRL, accountable.opendoor.com, {res['price'].get('source', 'price snapshot')}, opendoor_kpis.csv. Not investment advice.</p>
</body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)


def export_rows(res, d):
    """Flat row files for refreshing the dashboard."""
    os.makedirs(d, exist_ok=True)
    s, px, ex, acc = res["score"], res["price"], res["extra"], res.get("accountable") or {}
    r2 = lambda v, n=2: None if v is None else round(v, n)
    files = {
        "overall": [{"overall": s["overall"], "grade": s["grade"], "verdict": s["verdict"], "price": px["price"],
                     "price_date": px["date"], "market_cap_b": r2((ex["market_cap_musd"] or 0) / 1000),
                     "ps": r2(res["metrics"]["ps"]), "tracker_as_of": acc.get("as_of"),
                     "q3_homes_sold": acc.get("q3_homes_sold"), "q3_homes_sold_expected": acc.get("q3_homes_sold_expected"),
                     "q3_revenue_est_musd": r2(ex.get("q3_revenue_estimate_musd"), 0),
                     "q3_revenue_est_yoy_pct": r2((res["metrics"]["rev_nowcast_yoy"] or 0) * 100, 1),
                     "shares_m": r2(ex["shares_m"], 1), "debt_pf_musd": r2(ex["pro_forma"]["total_debt_musd"], 0),
                     "cash_pf_musd": r2(ex["pro_forma"]["cash_musd"], 0)}],
        "pillars": [{"pillar": p["name"], "weight_pct": p["weight"], "score": p["score"], "question": p["question"]}
                    for p in s["pillars"]],
        "metrics": [{"metric": i["label"], "pillar": p["name"], "value": fmt(i["value"], i["fmt"]),
                     "score": i["score"], "rule": f"0 at {fmt(i['bad'], i['fmt'])}, 10 at {fmt(i['good'], i['fmt'])}"}
                    for p in s["pillars"] for i in p["metrics"]],
        "quarters": [{"quarter_end": r["quarter_end"], "revenue_musd": r.get("revenue_musd"),
                      "net_income_musd": r.get("net_income_musd"), "homes_purchased": r.get("homes_purchased"),
                      "homes_sold": r.get("homes_sold"), "contribution_margin_pct": r.get("contribution_margin_pct"),
                      "cash_musd": r.get("cash_musd")} for r in res["quarters"][-8:]],
        "weekly": [{"week_ending": w[0], "contracts": w[1], "projected_low": w[2], "projected_high": w[3]}
                   for w in acc.get("weekly", []) if w[0] >= "2026-01-01"],
    }
    for name, rows in files.items():
        with open(os.path.join(d, f"opendoor_{name}.json"), "w") as fh:
            json.dump(rows, fh, indent=1)


def save_snapshot(acc, path):
    """Keep the offline fallback fresh after a successful live read of the tracker."""
    j = lambda rows: ";".join(",".join("" if v is None else (str(int(v)) if isinstance(v, (int, float)) and float(v).is_integer() and not isinstance(v, bool) else str(v)) for v in r) for r in rows)
    snap = {"source": "https://accountable.opendoor.com/", "as_of": acc["as_of"], "extracted": dt.date.today().isoformat(),
            "acquisition_contracts_weekly": j(acc["contracts"]),
            "acquisition_contracts_columns": "week_ending,actual,projected_low,projected_high,prior_year",
            "relative_clearance_weekly": j(acc["clearance"]),
            "resale_coe_prev_label": acc["coe"][0]["label"],
            "resale_coe_prev_cumulative": j([r for r in acc["coe"][0]["actual"] if r[1] is not None]),
            "resale_coe_prev_expectation": j([r for r in acc["coe"][0]["expectation"] if r[1] is not None]),
            "resale_coe_prev_prior_year": j(acc["coe"][0]["prior_year"]),
            "resale_coe_curr_label": acc["coe"][-1]["label"],
            "resale_coe_curr_cumulative": j([r for r in acc["coe"][-1]["actual"] if r[1] is not None]),
            "resale_coe_curr_expectation": j([r for r in acc["coe"][-1]["expectation"] if r[1] is not None]),
            "resale_coe_curr_prior_year": j(acc["coe"][-1]["prior_year"])}
    with open(path, "w") as fh:
        json.dump(snap, fh, indent=1)


HIST_FIELDS = ["run_at", "price_date", "price", "tracker_as_of", "latest_quarter", "overall", "grade"] + [p[0] for p in PILLARS]


def update_history(res, path):
    """Append this run to history.csv and return the previous run (or None)."""
    prev = None
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        prev = rows[-1] if rows else None
    s = res["score"]
    row = {"run_at": res["generated"], "price_date": res["price"]["date"], "price": res["price"]["price"],
           "tracker_as_of": (res.get("accountable") or {}).get("as_of", ""), "latest_quarter": res["quarters"][-1]["quarter_end"],
           "overall": s["overall"], "grade": s["grade"], **{p["name"]: p["score"] for p in s["pillars"]}}
    new = not os.path.exists(path)
    with open(path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HIST_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return prev, rows + [{k: str(v) for k, v in row.items()}]


def change_summary(res, prev):
    s = res["score"]
    if not prev:
        return None
    f = lambda v: None if v in (None, "", "None") else float(v)
    d = {"overall": s["overall"] - f(prev["overall"]), "grade_from": prev["grade"], "since": prev["run_at"],
         "pillars": {p["name"]: (p["score"] - f(prev.get(p["name"])) if p["score"] is not None and f(prev.get(p["name"])) is not None else None)
                     for p in s["pillars"]}}
    return d


def notify(text, title):
    """Optional phone push via ntfy.sh: set NTFY_TOPIC to a private topic name and install the ntfy app."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=text.encode(), headers={"Title": title})
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"ntfy push failed: {e}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true", help="no network: csv + accountable snapshot + price snapshot")
    ap.add_argument("--kpis", default=os.path.join(HERE, "opendoor_kpis.csv"))
    ap.add_argument("--snapshot", default=os.path.join(HERE, "accountable_snapshot.json"))
    ap.add_argument("--price-cache", default=os.path.join(HERE, "price_cache.json"))
    ap.add_argument("--ua", default=os.environ.get("SEC_UA", "OPEN scorecard research contact@example.com"))
    ap.add_argument("--shares", type=float, help="shares you hold")
    ap.add_argument("--avg-cost", type=float, help="your average cost per share, USD")
    ap.add_argument("--outdir", default=os.path.join(HERE, "www"), help="folder for index.html, scorecard.json, history.csv")
    ap.add_argument("--out", help=argparse.SUPPRESS)  # accepted for older commands; output goes to --outdir
    ap.add_argument("--export-rows", metavar="DIR", help="also write flat JSON row files for the dashboard")
    a = ap.parse_args()

    log, edgar, acc_raw = [], {}, None
    px = dict(PRICE_SNAPSHOT, source="price snapshot")
    if not a.offline:
        try:
            log.append("EDGAR tags used:")
            edgar = from_edgar(a.ua, log)
        except Exception as e:
            log.append(f"EDGAR unavailable ({e}); using csv only")
        try:
            acc_raw = parse_accountable(get("https://accountable.opendoor.com/"))
            log.append(f"accountable.opendoor.com: data as of {acc_raw['as_of']}")
        except Exception as e:
            log.append(f"accountable.opendoor.com unavailable ({e}); using snapshot")
        try:
            px = prices(log=log)
            with open(a.price_cache, "w") as fh:
                json.dump(px, fh)
        except Exception as e:
            cached = json.load(open(a.price_cache)) if os.path.exists(a.price_cache) else None
            if cached and cached.get("date", "") > PRICE_SNAPSHOT["date"]:
                px = dict(cached, source=f"last good price ({cached.get('source')})")
                log.append(f"price feed unavailable ({e}); using last good price from {cached['date']}")
            else:
                log.append(f"price feed unavailable ({e}); using snapshot")
    if acc_raw is None and os.path.exists(a.snapshot):
        acc_raw = accountable_from_snapshot(a.snapshot)
    acc = accountable_metrics(acc_raw) if acc_raw else None
    q = merge(edgar, load_kpis(a.kpis))
    if len(q) < 2:
        sys.exit("Not enough quarterly data. Check network access or opendoor_kpis.csv.")
    m, extra = metrics(q, px, acc)
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)
    except Exception:
        now = dt.datetime.now()
    res = {"generated": now.isoformat(timespec="minutes"), "ticker": TICKER, "price": px,
           "extra": extra, "metrics": m, "score": score(m), "quarters": q, "accountable": acc, "log": log}
    if a.shares and a.avg_cost:
        val, cost = a.shares * px["price"], a.shares * a.avg_cost
        res["position"] = {"shares": a.shares, "avg_cost": a.avg_cost, "value": val, "pnl": val - cost,
                           "pnl_pct": val / cost - 1, "to_breakeven": a.avg_cost / px["price"] - 1}
    if acc_raw is not None and not a.offline and any("data as of" in l for l in log):
        save_snapshot(acc_raw, a.snapshot)
    os.makedirs(a.outdir, exist_ok=True)
    prev, history = update_history(res, os.path.join(a.outdir, "history.csv"))
    res["change"] = change_summary(res, prev)
    res["history"] = history
    print("\n".join(log))
    print_report(res)
    ch = res["change"]
    if ch:
        movers = sorted(((k, v) for k, v in ch["pillars"].items() if v), key=lambda kv: -abs(kv[1]))[:2]
        line = (f"OPEN score {res['score']['overall']:.2f} ({ch['overall']:+.2f} since {ch['since'][:10]}), grade {res['score']['grade']}"
                + (f" (was {ch['grade_from']})" if ch["grade_from"] != res["score"]["grade"] else "")
                + ("; biggest moves: " + ", ".join(f"{k} {v:+.1f}" for k, v in movers) if movers else ""))
        print(line)
        if abs(ch["overall"]) >= 0.05 or ch["grade_from"] != res["score"]["grade"] or os.environ.get("NTFY_ALWAYS"):
            notify(line, "Opendoor scorecard")
    with open(os.path.join(a.outdir, "scorecard.json"), "w") as fh:
        json.dump(res, fh, indent=2, default=str)
    write_html(res, os.path.join(a.outdir, "index.html"))
    print(f"Wrote {a.outdir}/index.html, scorecard.json and history.csv")
    if a.export_rows:
        export_rows(res, a.export_rows)
        print(f"Wrote dashboard row files to {a.export_rows}/")


if __name__ == "__main__":
    main()
