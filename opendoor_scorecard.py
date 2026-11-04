#!/usr/bin/env python3
"""
Opendoor (OPEN) investor scorecard

Data sources (all free, pulled live unless --offline):
  1. SEC EDGAR XBRL      - reported GAAP financials (revenue, net income, cash, equity, debt, shares,
                           potential dilution from stock awards / convertibles / warrants)
  2. SEC earnings releases (8-K exhibit 99.1) - homes bought/sold, contribution margin, adjusted EBITDA,
                           adjusted net income, read automatically each quarter
  3. accountable.opendoor.com - Opendoor's own weekly tracker: acquisition contracts vs its projected
                           range, resale closings (COEs) vs management's expectation, relative clearance vs MLS
  4. Prices              - Yahoo Finance, then Nasdaq, then Stooq
  5. Freddie Mac 30-year mortgage rate via FRED (St. Louis Fed)
  6. 10-Q / 10-K table of anti-dilutive securities - potential new shares by type (awards, warrants, notes)
  7. Realtor.com monthly data (FRED as backup) - days on market, price cuts, asking prices, listings
                           in 12 Opendoor markets
  8. opendoor_kpis.csv   - hand-entered figures; any value here overrides the automatic sources
  9. CAPITAL_EVENTS / PRICE_TRIGGERED - deals after the last balance sheet, and the price terms of awards,
                           warrants and convertible notes (from Opendoor's 8-Ks)

Rates the company 0-10 on nine pillars and gives an overall grade.

Usage
  python3 opendoor_scorecard.py                        # live
  python3 opendoor_scorecard.py --offline              # csv + accountable_snapshot.json + price snapshot
  python3 opendoor_scorecard.py --shares 500 --avg-cost 4.20
  python3 opendoor_scorecard.py --export-rows rows     # flat JSON files for the dashboard
Output goes to www/ : index.html (the report), scorecard.json, history.csv (one line per run).
Optional phone alerts: set NTFY_TOPIC=<private topic> (ntfy.sh app) - sent when the score moves.

Quarterly figures are read automatically from Opendoor's earnings release on sec.gov. The csv is only
for corrections and for anything the release stops giving.
Standard library only. SEC asks for a contact User-Agent: set SEC_UA="Your Name you@email.com".
Not investment advice: the thresholds in RULES are opinions, tune them.
"""
import argparse, csv, datetime as dt, html, json, os, re, sys, urllib.request

TICKER, CIK = "OPEN", "0001801169"
METHOD = "1"  # bump when metrics, thresholds or weights change, so the history marks the break
HERE = os.path.dirname(os.path.abspath(__file__))

# Used by --offline or when the price feed fails. Source: daily closes (S&P Global via stockanalysis.com).
PRICE_SNAPSHOT = {"date": "2026-10-30", "price": 1.80, "high_52w": 9.15, "low_52w": 1.76,
                  "return_1y": -0.7548, "ma200": 4.19}

# Deals after the last reported balance sheet. Each is applied until a balance sheet dated after it is filed.
CAPITAL_EVENTS = [
    # 0.25% notes due 15 Aug 2026: $135M left (10-Q, 30 Jun 2026); conversion price $19.23, so repaid in cash
    {"date": "2026-08-15", "what": "$135M of 2026 convertible notes repaid at maturity",
     "convert_debt_musd": -135.0, "cash_musd": -135.0},
    # Opendoor 8-K, 13 Aug 2026 (settled 19 Aug 2026)
    {"date": "2026-08-19", "what": "$650M 0% convertible notes due 2030, $158M buyback of 45.3M shares, $52.5M capped call",
     "convert_debt_musd": +650.0, "cash_musd": +650.0 - 158.0 - 52.5, "equity_musd": -158.0 - 52.5, "shares_m": -45.3},
]

# Potential shares that only appear above a share price. The counts come from the latest 10-Q table of
# anti-dilutive securities; the prices from Opendoor's 8-Ks. How each kind turns into shares at price p:
#   hurdle   vests in full once the stock holds the price (CEO awards, 60-day average close)
#   warrant  worth (p - strike) per warrant, i.e. n x (1 - strike / p) shares; nothing after expiry
#   convert  principal is repaid, so only the value above it: n x (1 - conversion price / p)
#   capped   same, but the capped call covers everything up to the cap: n x (p - cap) / p
PRICE_TRIGGERED = [
    # 8-K and Form 4, 15 Sep 2025: two awards of 40,886,344 units, the second in seven equal tranches
    {"group": "market_rsus", "line": "CEO award 1", "n": 40.886344, "price": 6.24, "kind": "hurdle"},
    *[{"group": "market_rsus", "line": "CEO award 2", "n": 40.886344 / 7, "price": float(h), "kind": "hurdle"}
      for h in (9, 13, 17, 21, 25, 29, 33)],
    # warrant dividend, 21 Nov 2025: one of each series per 30 shares, all expiring 20 Nov 2026
    *[{"group": "warrants", "line": "Warrants (Series K, A, Z)", "part": 1 / 3, "price": k, "kind": "warrant",
       "expires": "2026-11-20"} for k in (9.0, 13.0, 17.0)],
    # 8-K, 19 Aug 2026: 212.2466 shares per $1,000, capped call to $6.98
    {"group": "converts", "line": "0% convertible notes due 2030", "n": 137.960290, "price": 4.71, "cap": 6.98,
     "kind": "capped", "issued": "2026-08-19"},
    # whatever else the 10-Q lists as convertible: the 7% notes due 2030 (conversion price $1.57)
    {"group": "converts", "line": "7% convertible notes due 2030", "rest": True, "price": 1.57, "kind": "convert"},
]

# Housing in Opendoor's markets: Realtor.com monthly data via FRED, equal-weighted, by CBSA code
HOUSING_METROS = [("Phoenix", "38060"), ("Atlanta", "12060"), ("Dallas", "19100"), ("Houston", "26420"),
                  ("Charlotte", "16740"), ("Raleigh", "39580"), ("Tampa", "45300"), ("Orlando", "36740"),
                  ("Las Vegas", "29820"), ("Denver", "19740"), ("Nashville", "34980"), ("San Antonio", "41700")]
HOUSING_SERIES = {"dom": "MEDDAYONMAR", "listings": "ACTLISCOU", "cuts": "PRIREDCOU", "price": "MEDLISPRI"}

# ---------------------------------------------------------------- scoring rules
# metric: (label, value scoring 0, value scoring 10, format). Linear in between, clipped.
RULES = {
    "rev_nowcast_yoy": ("Revenue YoY, latest qtr (est. until reported)",          -0.40, 0.30, "pct"),
    "coe_q4_yoy":      ("Quarter-to-date homes sold YoY",          -0.20, 0.50, "pct"),
    "contracts_qoq":   ("Acquisition contracts, last qtr vs prior",    -0.20, 0.20, "pct"),
    "contrib_margin":  ("Contribution margin",                 0.00, 0.07, "pct"),
    "gross_margin":    ("Gross margin",                        0.05, 0.12, "pct"),
    "aged_inventory":  ("Homes listed 120+ days, share of inventory", 0.40, 0.05, "share"),
    "net_margin_2q":   ("GAAP net margin, last 2 qtrs",       -0.20, 0.02, "pct"),
    "adj_ebitda_m":    ("Adj. EBITDA margin",                 -0.05, 0.03, "pct"),
    "ani_margin":      ("Adj. net income margin (target: positive by end-2026)", -0.10, 0.01, "pct"),
    "coe_vs_guide":    ("Last qtr homes sold vs mgmt expectation",  -0.15, 0.05, "pct"),
    "contracts_vs_proj": ("Contracts, last 4 wks vs projection midpoint", -0.40, 0.10, "pct"),
    "clearance":       ("Relative clearance vs MLS, 4-wk avg", 0.90, 1.30, "x"),
    "cash_vs_notes":   ("Cash vs the company's own debt (convertible notes)", 0.50, 2.00, "x"),
    "home_debt_ltv":   ("Home-backed debt vs the value of homes held", 1.00, 0.80, "share"),
    "runway_q":        ("Cash runway at current adj. net loss (home purchases are debt-funded, excluded)", 4.0, 16.0, "qtrs"),
    "share_yoy":       ("Share count change YoY",              0.30, 0.00, "pct"),
    "sbc_pct":         ("Stock comp / revenue",                0.10, 0.01, "share"),
    "dilution_live":   ("Potential new shares live at today's price", 0.25, 0.02, "share"),
    "ps":              ("Price / sales (TTM)",                 3.00, 0.30, "x"),
    "pb":              ("Price / book (pro forma)",            6.00, 1.00, "x"),
    "ret_1y":          ("Share price, 1 year",                -0.70, 0.30, "pct"),
    "range_pos":       ("Position in 52-week range",           0.00, 1.00, "share"),
    "vs_ma200":        ("Price vs 200-day average",           -0.40, 0.10, "pct"),
    "mortgage_rate":   ("30-year US mortgage rate",            0.08, 0.05, "rate"),
    "mortgage_13w":    ("Mortgage rate change, last 13 weeks", 0.0075, -0.0075, "pp"),
    "dom_yoy":         ("Days on market vs a year ago, Opendoor's markets", 0.30, -0.10, "pct"),
    "cuts_yoy":        ("Price cuts per 100 listings vs a year ago, Opendoor's markets", 0.06, -0.03, "pp"),
    "list_price_yoy":  ("Asking prices vs a year ago, Opendoor's markets", -0.05, 0.04, "pct"),
}
PILLARS = [  # (name, weight %, metrics, the question it answers)
    ("Growth",         15, ["rev_nowcast_yoy", "coe_q4_yoy", "contracts_qoq"], "Is the business getting bigger right now?"),
    ("Unit economics", 15, ["contrib_margin", "gross_margin", "aged_inventory"], "Does each home flip make money, and is stock fresh?"),
    ("Profitability",  15, ["net_margin_2q", "adj_ebitda_m", "ani_margin"],  "Does the whole company make money?"),
    ("Execution",      15, ["coe_vs_guide", "contracts_vs_proj", "clearance"], "Is management hitting its own targets?"),
    ("Balance sheet",  15, ["cash_vs_notes", "home_debt_ltv", "runway_q"],   "Can it survive a bad housing year?"),
    ("Dilution",       10, ["share_yoy", "sbc_pct", "dilution_live"],        "Is your slice of the company shrinking?"),
    ("Valuation",       5, ["ps", "pb"],                                     "Are you overpaying for what exists today?"),
    ("Momentum",        5, ["ret_1y", "range_pos", "vs_ma200"],              "What is the market saying?"),
    ("Housing market",  5, ["mortgage_rate", "mortgage_13w", "dom_yoy", "cuts_yoy", "list_price_yoy"],
                                                                             "Is the market helping or hurting?"),
]
# Plain-English reason for each pillar: (when it's a strength, when it's a weakness)
PILLAR_WORDS = {
    "Growth": ("sales are growing again", "sales are shrinking"),
    "Unit economics": ("each home sale earns a healthy margin", "margins on each home are thin"),
    "Profitability": ("the company is profitable", "the company still loses money overall"),
    "Execution": ("management is hitting its own targets", "it's missing its own targets"),
    "Balance sheet": ("the balance sheet is solid", "the cushion behind its debt is thin"),
    "Dilution": ("the share count is stable", "the share count keeps growing"),
    "Valuation": ("the stock is cheap against its sales", "the stock looks expensive"),
    "Momentum": ("the share price is trending up", "the share price is in a downtrend"),
    "Housing market": ("housing in its markets is helping", "housing in its markets is a headwind"),
}


def explain(sc):
    """Reason sentence: the two pillars lifting the score most and the three dragging it most (score x weight)."""
    ps = [p for p in sc["pillars"] if p["score"] is not None]
    ups = sorted((p for p in ps if p["score"] >= 6.5), key=lambda p: -p["score"] * p["weight"])[:2]
    downs = sorted((p for p in ps if p["score"] < 5.0), key=lambda p: -(10 - p["score"]) * p["weight"])[:3]
    join = lambda xs: xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    good = [PILLAR_WORDS[p["name"]][0] for p in ups]
    bad = [PILLAR_WORDS[p["name"]][1] for p in downs]
    if good and bad:
        text = f"{join(good)}, but {join(bad)}."
    elif good or bad:
        text = join(good or bad) + "."
    else:
        text = "no pillar stands out either way."
    return text[0].upper() + text[1:]


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


def get(url, ua=BROWSER_UA, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
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
    # Two very different kinds of debt. Opendoor files its asset-backed debt, secured by the homes and
    # non-recourse to the company, as lines of credit; its own (corporate) debt is the convertible notes.
    # 30 Jun 2026 check: 691 + 1,071 = 1,762 home-backed, 194 convertible, 1,956 in all ($M), as in the 10-Q.
    total, used = {}, []
    for name, tags in (("home_debt_musd", ("LinesOfCreditCurrent", "LongTermLineOfCredit")),
                       ("convert_debt_musd", ("ConvertibleDebtCurrent", "ConvertibleDebtNoncurrent"))):
        part, part_used = {}, []
        for tag in tags:
            vals, t = instant(f, [tag])
            if t:
                part_used.append(t)
                for k, v in vals.items():
                    part[k] = part.get(k, 0) + v
        if part:
            put(name, part, " + ".join(part_used))
            used += part_used
            for k, v in part.items():
                total[k] = total.get(k, 0) + v
    if total:
        put("total_debt_musd", total, " + ".join(used))
    for end, row in fields.items():  # a quarter with only one kind filed has none of the other
        if "total_debt_musd" in row:
            row.setdefault("home_debt_musd", 0.0)
            row.setdefault("convert_debt_musd", 0.0)
    s, t = instant(f, ["EntityCommonStockSharesOutstanding", "CommonStockSharesOutstanding"], unit="shares")
    put("shares_m", {_quarter_end_before(k): v for k, v in sorted(s.items())}, t)
    # shares that could be issued (stock awards, convertibles, warrants), excluded from diluted EPS while loss-making
    items, t = _items(f, ["AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount"], unit="shares")
    by_end = {}
    for i in items:  # several award classes are reported per period; sum them per period end (longest span wins ties)
        if i.get("start") and 80 <= _days(i["start"], i["end"]) <= 100:
            by_end.setdefault(i["end"], 0)
            by_end[i["end"]] += i["val"]
    put("potential_dilution_m", by_end, t)
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


# ---------------------------------------------------------------- 3b. earnings releases (8-K exhibit 99.1)
from html.parser import HTMLParser


class _Rows(HTMLParser):
    """Collects table rows as lists of cell texts (all rows, and grouped by table)."""
    def __init__(self):
        super().__init__()
        self.rows, self.tables, self._row, self._cell, self._in = [], [], None, [], False

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._in, self._cell = True, []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._row is not None and self._in:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._in = False
        elif tag == "tr" and self._row is not None:
            cells = [c for c in self._row if c]
            if cells:
                self.rows.append(cells)
                if self.tables:
                    self.tables[-1].append(cells)
            self._row = None

    def handle_data(self, d):
        if self._in:
            self._cell.append(d)


# csv column -> label of the row in the release (matched against the whole first cell, case-insensitive)
RELEASE_ROWS = [
    ("revenue_musd", r"revenue( \(gaap\))?"),
    ("gross_profit_musd", r"gross profit"),
    ("net_income_musd", r"net (loss|income)|net income \(loss\)"),
    ("homes_sold", r"homes sold"),
    ("homes_purchased", r"homes purchased"),
    ("homes_in_inventory", r"homes in inventory( \(at period end\))?"),
    ("inventory_musd", r"inventory( \(at period end\))?"),
    ("contribution_margin_pct", r"contribution margin"),
    ("adj_ebitda_musd", r"adjusted ebitda"),
    ("adj_net_income_musd", r"adjusted net (loss|income)|adjusted net income \(loss\)"),
]
_NUM = re.compile(r"\(\s*\$?\s*[\d,]+(?:\.\d+)?\s*\)|-?\$?\s*[\d,]*\d(?:\.\d+)?")


def _first_number(cells):
    text = " | ".join(cells[1:])
    text = re.sub(r"\(\s*\|\s*", "(", re.sub(r"\s*\|\s*\)", ")", text)).replace("$", "")
    for m in _NUM.finditer(text):
        tok = m.group(0).replace(" ", "").replace(",", "")
        neg = tok.startswith("(")
        tok = tok.strip("()")
        try:
            v = float(tok)
        except ValueError:
            continue
        return -v if neg else v
    return None


RELEASE_PARSER = 2  # bump to re-read cached releases with a newer parser
# "% of homes on the market over 120 Days declined from 10% to 9% quarter-over-quarter, compared to 27% for
# the overall market." The table header that names the same metric has no figure, so it never matches.
AGED_RE = re.compile(r"homes on the market (?:for )?(?:over|more than|greater than) 120 days[^.%]{0,60}?"
                     r"(?:from \d+(?:\.\d+)?% )?(?:to|at|was|of)\s+(\d+(?:\.\d+)?)\s?%", re.I)
MARKET_AGED_RE = re.compile(r"compared (?:to|with) (\d+(?:\.\d+)?)\s?% for the (?:overall|broader|wider) market", re.I)


def _page_text(page):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page)))


def parse_release(page):
    """Current-quarter figures from an earnings release: the first matching row, first number in it,
    plus the share of homes listed 120+ days, which Opendoor reports in a sentence."""
    p = _Rows()
    p.feed(page)
    out = {}
    text = _page_text(page)
    m = AGED_RE.search(text)
    if m:
        out["aged_120_pct"] = float(m.group(1))
        mk = MARKET_AGED_RE.search(text, m.end(), m.end() + 200)
        if mk:
            out["market_aged_120_pct"] = float(mk.group(1))
    for cells in p.rows:
        label = re.sub(r"\s*\(\d+\)$|\s*\d$", "", cells[0].strip().rstrip(":")).strip().lower()
        label = label.replace("\u2019", "'")
        for col, pat in RELEASE_ROWS:
            if col not in out and re.fullmatch(pat, label):
                v = _first_number(cells)
                if v is not None:
                    out[col] = v
    return out


def sec_submissions(ua):
    return json.loads(get(f"https://data.sec.gov/submissions/CIK{CIK}.json", ua))


def releases_from_sec(ua, cache_path, log, keep=6, sub=None):
    """Find Opendoor's earnings 8-Ks (item 2.02), parse exhibit 99.1, cache results by accession number."""
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    r = (sub or sec_submissions(ua))["filings"]["recent"]
    found = [(r["filingDate"][i], r["accessionNumber"][i]) for i in range(len(r["form"]))
             if r["form"][i] == "8-K" and "2.02" in (r.get("items", [""] * len(r["form"]))[i] or "")]
    for fdate, acc in sorted(found)[-keep:]:
        if cache.get(acc, {}).get("parser") == RELEASE_PARSER:
            continue
        folder = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc.replace('-', '')}"
        ex = [cache[acc]["file"]] if cache.get(acc, {}).get("file") else []
        if not ex:
            files = [x["name"] for x in json.loads(get(folder + "/index.json", ua))["directory"]["item"]]
            ex = [n for n in files if n.lower().endswith((".htm", ".html")) and re.search(r"ex-?99[-_.]?0?1|ex991|991", n.lower())]
        if not ex:
            log.append(f"  release {acc}: no exhibit 99.1 found")
            continue
        vals = parse_release(get(f"{folder}/{ex[0]}", ua))
        cache[acc] = {"filed": fdate, "quarter_end": _quarter_end_before(fdate), "file": ex[0], "values": vals,
                      "parser": RELEASE_PARSER}
        log.append(f"  release {fdate} ({ex[0]}): {len(vals)} figures"
                   + (f", 120+ days {vals['aged_120_pct']:g}%" if "aged_120_pct" in vals else ", no 120+ days figure"))
    with open(cache_path, "w") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
    return cache


def releases_to_rows(cache):
    rows = {}
    for item in sorted(cache.values(), key=lambda x: x["filed"]):
        rows.setdefault(item["quarter_end"], {}).update(item["values"])
    return rows


# ---------------------------------------------------------------- 3d. 10-Q / 10-K: potential shares by type
DILUTION_PARSER = 1
# row label in the table of anti-dilutive securities -> group (whole first cell, case-insensitive)
DILUTION_ROWS = [
    ("total", r"total (potentially )?(anti-?dilutive|dilutive) (securities|shares)"),
    ("warrants", r"(common )?(stock )?warrants"),
    ("market_rsus", r"(market[- ]condition|performance[- ]based|market[- ]based) (rsus?|restricted stock units|psus?)|psus?"),
    ("rsus", r"(time[- ]based )?(rsus?|restricted stock units)"),
    ("converts", r"convertible (senior )?notes"),
    ("options", r"(stock )?options"),
    ("espp", r"employee stock purchase plan|espp"),
]


def _first_cell_number(cells):
    """First figure in the row (the current period); a dash there means zero, not 'look further right'."""
    for c in cells[1:]:
        c = c.replace("$", "").replace(" ", " ").strip()
        if not c or re.fullmatch(r"\(\d\)", c):
            continue
        if c in ("—", "–", "-"):
            return 0.0
        m = re.fullmatch(r"\(?\s*([\d,]+(?:\.\d+)?)\s*\)?", c)
        if m:
            return float(m.group(1).replace(",", ""))
    return None


def parse_dilution(page):
    """The 'anti-dilutive securities' table of a 10-Q or 10-K, in millions of shares by group."""
    p = _Rows()
    p.feed(page)
    norm = lambda c: re.sub(r"\s*\(\d\)$|\s*\d$", "", c.strip().rstrip(":")).strip().lower().replace("’", "'")
    for table in p.tables:
        if not any(re.fullmatch(DILUTION_ROWS[0][1], norm(r[0])) for r in table):
            continue
        out = {}
        for cells in table:
            label, v = norm(cells[0]), _first_cell_number(cells)
            if v is None or not re.search(r"[a-z]", label):  # header rows of years, blank labels
                continue
            group = next((g for g, pat in DILUTION_ROWS if re.fullmatch(pat, label)), "other")
            out[group] = out.get(group, 0.0) + v / 1000  # the table is in thousands
            if group == "other":
                out.setdefault("other_labels", []).append(cells[0])
        if out.get("total"):
            return out
    return {}


def dilution_from_sec(ua, cache_path, log, sub=None, keep=3):
    """Parse the anti-dilutive table of the latest 10-Qs / 10-K; cached by accession number."""
    cache = json.load(open(cache_path)) if os.path.exists(cache_path) else {}
    r = (sub or sec_submissions(ua))["filings"]["recent"]
    found = sorted((r["filingDate"][i], r["accessionNumber"][i], r["reportDate"][i], r["primaryDocument"][i], r["form"][i])
                   for i in range(len(r["form"])) if r["form"][i] in ("10-Q", "10-K"))
    for fdate, acc, rdate, doc, form in found[-keep:]:
        if cache.get(acc, {}).get("parser") == DILUTION_PARSER:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(CIK)}/{acc.replace('-', '')}/{doc}"
        d = parse_dilution(get(url, ua, timeout=90))
        before = cache.get(acc, {}).get("dilution")
        if not d and before:  # never replace good figures with a failed read
            cache[acc]["parser"] = DILUTION_PARSER
            log.append(f"  {form} {rdate}: no anti-dilutive table found; kept the cached figures")
            continue
        cache[acc] = {"filed": fdate, "quarter_end": rdate, "form": form, "file": doc, "parser": DILUTION_PARSER, "dilution": d}
        parts = sum(v for k, v in d.items() if k not in ("total", "other_labels"))
        log.append(f"  {form} {rdate}: " + (f"{d['total']:.1f}M potential shares: "
                                             + ", ".join(f"{k} {v:.1f}" for k, v in d.items() if k not in ("total", "other_labels"))
                                             + ("" if abs(parts - d["total"]) < 0.5 else f" (groups sum to {parts:.1f}M)")
                                             + (f"; checked against cached {before['total']:.1f}M" if before else "")
                                             if d else "no anti-dilutive table found"))
    with open(cache_path, "w") as fh:
        json.dump(cache, fh, indent=1, sort_keys=True)
    return cache


def latest_dilution(cache):
    """(quarter_end, breakdown) from the most recent filing that had the table."""
    ok = [v for v in cache.values() if v.get("dilution", {}).get("total")]
    if not ok:
        return None, None
    best = max(ok, key=lambda v: (v["quarter_end"], v["filed"]))
    return best["quarter_end"], best["dilution"]


def live_shares(item, p, n, today):
    """Shares (or their value, in shares) an instrument hands out at share price p."""
    if item.get("expires") and today > item["expires"]:
        return 0.0
    k = item["price"]
    if item["kind"] == "hurdle":
        return n if p >= k else 0.0
    if item["kind"] in ("warrant", "convert"):
        return n * max(0.0, 1 - k / p)
    if item["kind"] == "capped":
        return n * max(0.0, p - item["cap"]) / p
    return n


def dilution_lines(breakdown, quarter_end, today):
    """Potential new shares as lines: (label, millions, 'needs', [(instrument, n)]), from the 10-Q groups plus
    instruments issued after that quarter. Anything not described in PRICE_TRIGGERED counts in full."""
    b = dict(breakdown)
    full = {"kind": "full", "price": 0.0}
    lines = []
    plain = sum(b.get(k, 0.0) for k in ("rsus", "options", "espp", "other"))
    lines.append(("RSUs, options and employee share plan", plain, "Counted in full", [(full, plain)]))
    # price-target awards: the CEO's are itemised; any others count in full
    ceo = [i for i in PRICE_TRIGGERED if i["group"] == "market_rsus"]
    for name in dict.fromkeys(i["line"] for i in ceo):
        items = [i for i in ceo if i["line"] == name]
        prices = sorted({i["price"] for i in items})
        needs = f"${prices[0]:g} average" if len(prices) == 1 else f"${prices[0]:g} to ${prices[-1]:g} average, in {len(items)} steps"
        lines.append((name, sum(i["n"] for i in items), needs, [(i, i["n"]) for i in items]))
    other_mkt = max(0.0, b.get("market_rsus", 0.0) - sum(i["n"] for i in ceo))
    if other_mkt > 0.05:
        lines.append(("Other price-target awards", other_mkt, "Counted in full (targets not itemised)", [(full, other_mkt)]))
    war = [i for i in PRICE_TRIGGERED if i["group"] == "warrants"]
    if b.get("warrants"):
        n = b["warrants"]
        expired = all(today > i["expires"] for i in war)
        needs = ("Expired " + _d(war[0]["expires"])) if expired else \
            " / ".join(f"${i['price']:g}" for i in war) + f", expire {_d(war[0]['expires'])}"
        lines.append((war[0]["line"], n, needs, [(i, n * i["part"]) for i in war]))
    conv = [i for i in PRICE_TRIGGERED if i["group"] == "converts"]
    known = [i for i in conv if not i.get("rest")]
    listed = b.get("converts", 0.0)
    for i in known:
        if i["issued"] <= today:
            lines.append((i["line"], i["n"], f"${i['price']:g} to convert, capped call to ${i['cap']:g}", [(i, i["n"])]))
            if i["issued"] <= quarter_end:  # already inside the 10-Q's convertible line
                listed -= i["n"]
    rest = next(i for i in conv if i.get("rest"))
    if listed > 0.05:
        lines.append((rest["line"], listed, f"${rest['price']:g} to convert", [(rest, listed)]))
    return lines


def dilution_at(lines, p, today):
    return sum(live_shares(i, p, n, today) for _, _, _, parts in lines for i, n in parts)


# ---------------------------------------------------------------- 3c. mortgage rates (FRED, no key needed)
def _fred():
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv?id=MORTGAGE30US", timeout=60)
    pts = []
    for line in txt.strip().splitlines()[1:]:
        d, v = line.split(",")[:2]
        if v not in ("", "."):
            pts.append((d, float(v) / 100))
    return pts, "Freddie Mac PMMS via FRED"


def _freddie():
    txt = get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv", timeout=60)
    lines = txt.strip().splitlines()
    head = [h.strip().lower() for h in lines[0].split(",")]
    di, vi = head.index("date"), head.index("pmms30")
    pts = []
    for line in lines[1:]:
        c = line.split(",")
        if len(c) > vi and c[vi].strip() not in ("", "."):
            m, d, y = c[di].strip().split("/")
            pts.append((f"{y}-{int(m):02d}-{int(d):02d}", float(c[vi]) / 100))
    return sorted(pts), "Freddie Mac PMMS"


def _fred_series(sid, start):
    txt = get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}&cosd={start}", timeout=40)
    pts = []
    for line in txt.strip().splitlines()[1:]:
        d, v = (line.split(",") + [""])[:2]
        if v not in ("", "."):
            pts.append((d[:7], float(v)))
    return sorted(pts)


def _yoy(pts, ratio=True):
    """(latest month, latest value, change vs the same month a year earlier)."""
    if not pts:
        return None, None, None
    m, v = pts[-1]
    prior = dict(pts).get(f"{int(m[:4]) - 1}{m[4:]}")
    if prior in (None, 0):
        return m, v, None
    return m, v, (v / prior - 1) if ratio else v - prior


RDC = "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/RDC_Inventory_Core_Metrics_{}.csv"


def _num_or_none(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


RDC_CHANGES = ("median_days_on_market_yy", "median_listing_price_yy", "active_listing_count_yy", "price_reduced_count_yy")


def _rdc_scales(rows):
    """Realtor.com gives changes as fractions (0.05); if a file ever switches to percent (5.0), the typical size
    across hundreds of metros gives it away."""
    out = {}
    for k in RDC_CHANGES:
        v = sorted(abs(x) for x in (_num_or_none(r.get(k)) for r in rows) if x is not None)
        out[k] = 100.0 if v and v[len(v) // 2] > 0.5 else 1.0
    return out


def _rdc_row(r, scales):
    """One Realtor.com row -> our fields. Changes are fractions; the price-cut share change is in share points."""
    f = lambda k: _num_or_none(r.get(k))
    yy = lambda k: (lambda v: None if v is None else v / scales.get(k, 1.0))(f(k))
    listings, cuts = f("active_listing_count"), f("price_reduced_count")
    share = cuts / listings if cuts is not None and listings else None
    l_yy, c_yy = yy("active_listing_count_yy"), yy("price_reduced_count_yy")
    prior = None
    if cuts and listings and l_yy is not None and c_yy is not None and c_yy > -1 and l_yy > -1:
        prior = (cuts / (1 + c_yy)) / (listings / (1 + l_yy))
    m = str(r.get("month_date_yyyymm", "")).strip()
    return {"month": f"{m[:4]}-{m[4:6]}" if len(m) >= 6 else None, "dom": f("median_days_on_market"),
            "dom_yoy": yy("median_days_on_market_yy"), "price": f("median_listing_price"),
            "price_yoy": yy("median_listing_price_yy"), "listings": listings, "listings_yoy": l_yy,
            "cut_share": share, "cuts_yoy": (share - prior) if share is not None and prior is not None else None}


def _housing_realtor():
    codes = {c: n for n, c in HOUSING_METROS}
    rows = list(csv.DictReader(get(RDC.format("Metro"), timeout=60).splitlines()))
    scales = _rdc_scales(rows)
    metros = {}
    for r in rows:
        code = str(r.get("cbsa_code", "")).strip()
        if code in codes:
            metros[code] = dict(_rdc_row(r, scales), name=codes[code], code=code)
    us = None
    for r in csv.DictReader(get(RDC.format("Country"), timeout=60).splitlines()):
        if _num_or_none(r.get("median_days_on_market")) is not None:
            us = dict(_rdc_row(r, scales), name="United States", code="US")
            break
    return [metros[c] for _, c in HOUSING_METROS if c in metros], us, "Realtor.com"


def _housing_fred():
    """Backup: the same Realtor.com series as mirrored by FRED (one quick probe first, as FRED often
    times out from cloud servers)."""
    from concurrent.futures import ThreadPoolExecutor
    start = (dt.date.today() - dt.timedelta(days=800)).isoformat()
    get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=MEDDAYONMARUS&cosd={start}", timeout=15)
    places = HOUSING_METROS + [("United States", "US")]
    jobs = {(name, key): f"{prefix}{code}" for name, code in places for key, prefix in HOUSING_SERIES.items()}
    got = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {k: pool.submit(_fred_series, sid, start) for k, sid in jobs.items()}
        for k, fu in futs.items():
            try:
                got[k] = fu.result()
            except Exception:
                pass
    rows = []
    for name, code in places:
        ser = {key: got.get((name, key), []) for key in HOUSING_SERIES}
        month, dom, dom_yoy = _yoy(ser["dom"])
        _, price, price_yoy = _yoy(ser["price"])
        _, listings, listings_yoy = _yoy(ser["listings"])
        lst = dict(ser["listings"])
        share = [(m, c / lst[m]) for m, c in ser["cuts"] if lst.get(m)]
        _, cut_share, cuts_yoy = _yoy(share, ratio=False)
        if month:
            rows.append({"name": name, "code": code, "month": month, "dom": dom, "dom_yoy": dom_yoy, "price": price,
                         "price_yoy": price_yoy, "listings": listings, "listings_yoy": listings_yoy,
                         "cut_share": cut_share, "cuts_yoy": cuts_yoy})
    return [r for r in rows if r["code"] != "US"], next((r for r in rows if r["code"] == "US"), None), "Realtor.com via FRED"


def housing_markets(log):
    """Days on market, price cuts, asking prices and listings in Opendoor's markets."""
    errors = []
    for fetch in (_housing_realtor, _housing_fred):
        try:
            metros, us, src = fetch()
            if len(metros) < len(HOUSING_METROS) // 2:
                raise RuntimeError(f"only {len(metros)} of {len(HOUSING_METROS)} markets")
            break
        except Exception as e:
            errors.append(f"{fetch.__name__.strip('_')}: {str(e)[:70]}")
    else:
        raise RuntimeError("; ".join(errors))
    def med(k):
        v = sorted(r[k] for r in metros if r.get(k) is not None)
        return None if not v else v[len(v) // 2] if len(v) % 2 else (v[len(v) // 2 - 1] + v[len(v) // 2]) / 2
    basket = {k: med(k) for k in ("dom", "dom_yoy", "cut_share", "cuts_yoy", "price_yoy", "listings_yoy")}
    month = max(r["month"] for r in metros if r.get("month"))
    log.append(f"housing ({src}): {len(metros)} of {len(HOUSING_METROS)} markets, {month}; median days on market "
               f"{basket['dom'] or 0:.0f} ({(basket['dom_yoy'] or 0) * 100:+.1f}% YoY)" + (f"; earlier tries: {'; '.join(errors)}" if errors else ""))
    return {"month": month, "metros": metros, "us": us, "basket": basket, "source": src}


def mortgage_rates(log=None):
    """Weekly 30-year fixed mortgage rate: FRED first, Freddie Mac's own file as backup."""
    errors = []
    for fetch in (_fred, _freddie):
        try:
            pts, src = fetch()
            if len(pts) < 60:
                raise ValueError(f"only {len(pts)} rows")
            return {"date": pts[-1][0], "rate": pts[-1][1], "rate_13w_ago": pts[-14][1], "rate_52w_ago": pts[-53][1],
                    "source": src}
        except Exception as e:
            errors.append(f"{fetch.__name__.strip('_')}: {str(e)[:60]}")
    raise RuntimeError("; ".join(errors))


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


def merge(edgar, kpis, releases=None):
    """Priority, lowest to highest: EDGAR, earnings releases, hand-entered csv."""
    rows = {}
    for end, vals in edgar.items():
        rows.setdefault(end, {}).update(vals)
    for end, vals in (releases or {}).items():
        match = next((e for e in rows if abs(_days(e, end)) <= 5), end)
        rows.setdefault(match, {}).update(vals)
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
    keys = ("cash_musd", "home_debt_musd", "convert_debt_musd", "total_debt_musd", "equity_musd")
    pf = {k: bs.get(k) for k in keys}
    pf["shares_m"] = sh.get("shares_m")
    pf["as_of"] = bs.get("quarter_end")
    applied = []
    for ev in CAPITAL_EVENTS:
        if bs and ev["date"] > bs["quarter_end"]:
            for k in keys:
                change = ev.get(k, 0.0) + (ev.get("convert_debt_musd", 0.0) if k == "total_debt_musd" else 0.0)
                if pf[k] is not None and change:
                    pf[k] += change
            applied.append(ev)
    for ev in CAPITAL_EVENTS:  # share changes after the date the share count was measured
        if sh and pf["shares_m"] is not None and ev["date"] > sh["quarter_end"] and "shares_m" in ev:
            pf["shares_m"] += ev["shares_m"]
            if ev not in applied:
                applied.append(ev)
    return pf, applied


# ---------------------------------------------------------------- metrics + score
def metrics(q, px, acc, macro=None, dil=None, housing=None, today=None):
    today = today or dt.date.today().isoformat()
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
    rows = {k: latest_with(q, k) for k in ("contribution_margin_pct", "gross_profit_musd", "aged_120_pct",
                                           "adj_ebitda_musd", "adj_net_income_musd", "sbc_musd")}
    ani_row = rows["adj_net_income_musd"]
    ani = ani_row.get("adj_net_income_musd")
    runway = None
    if ani is not None and pf["cash_musd"] is not None:
        runway = 99.0 if ani >= 0 else pf["cash_musd"] / -ani
    bs = latest_with(q, "cash_musd", "total_debt_musd", "equity_musd")
    home_ltv = div(bs.get("home_debt_musd"), bs.get("inventory_musd")) if bs.get("home_debt_musd") else None
    notes_debt = pf.get("convert_debt_musd")
    cash_vs_notes = None
    if pf["cash_musd"] is not None and notes_debt is not None:
        cash_vs_notes = 99.0 if notes_debt <= 0 else pf["cash_musd"] / notes_debt

    # dilution: every potential share by type, and how much of it is live at today's price
    dq, br = dil if dil else (None, None)
    full = {"kind": "full", "price": 0.0}
    if br:
        lines = dilution_lines(br, dq, today)
        dil_src = "10-Q"
    else:  # no breakdown: the XBRL total, all counted in full
        row = latest_with(q, "potential_dilution_m")
        dq = row.get("quarter_end")
        lines = [("All potential shares (no breakdown available)", row["potential_dilution_m"], "Counted in full",
                  [(full, row["potential_dilution_m"])])] if row else []
        dil_src = "XBRL total"
    live = dilution_at(lines, px["price"], today) if lines else None
    dilution = None
    if lines and shares:
        grid = sorted({round(x * 0.25, 2) for x in range(4, 141)} | {round(px["price"], 2)})
        dilution = {"quarter": dq, "source": dil_src, "price": px["price"], "shares_m": shares,
                    "potential_m": sum(l[1] for l in lines), "live_m": live,
                    "lines": [{"label": l, "n": n, "needs": nd, "live": sum(live_shares(i, px["price"], k, today) for i, k in parts)}
                              for l, n, nd, parts in lines],
                    "curve": [[p, dilution_at(lines, p, today) / shares] for p in grid]}

    hb = (housing or {}).get("basket") or {}
    m = {
        "rev_nowcast_yoy": ch(q3_rev_est, q3_prior) if unreported else ch(g(-1, "revenue_musd"), g(-5, "revenue_musd")),
        "coe_q4_yoy": acc.get("coe_q4_yoy") if acc else None,
        "contracts_qoq": acc.get("contracts_qoq") if acc else None,
        "contrib_margin": div(rows["contribution_margin_pct"].get("contribution_margin_pct"), 100),
        "gross_margin": div(rows["gross_profit_musd"].get("gross_profit_musd"), rows["gross_profit_musd"].get("revenue_musd")),
        "aged_inventory": div(rows["aged_120_pct"].get("aged_120_pct"), 100),
        "net_margin_2q": div(sum(r["net_income_musd"] for r in two), sum(r["revenue_musd"] for r in two))
                         if all("net_income_musd" in r for r in two) else None,
        "adj_ebitda_m": div(rows["adj_ebitda_musd"].get("adj_ebitda_musd"), rows["adj_ebitda_musd"].get("revenue_musd")),
        "ani_margin": div(ani, ani_row.get("revenue_musd")),
        "coe_vs_guide": acc.get("coe_vs_guide") if acc else None,
        "contracts_vs_proj": acc.get("contracts_vs_proj") if acc else None,
        "clearance": acc.get("clearance") if acc else None,
        "cash_vs_notes": cash_vs_notes,
        "home_debt_ltv": home_ltv,
        "runway_q": runway,
        "share_yoy": ch(shares, year_ago["shares_m"]) if year_ago else None,
        "sbc_pct": div(rows["sbc_musd"].get("sbc_musd"), rows["sbc_musd"].get("revenue_musd")),
        "dilution_live": div(live, shares),
        "ps": div(mcap, ttm_rev),
        "pb": div(mcap, pf["equity_musd"]),
        "ret_1y": px.get("return_1y"),
        "range_pos": div(px["price"] - px["low_52w"], px["high_52w"] - px["low_52w"]),
        "vs_ma200": ch(px["price"], px.get("ma200")),
        "mortgage_rate": (macro or {}).get("rate"),
        "mortgage_13w": ((macro or {}).get("rate") - macro["rate_13w_ago"]) if macro and macro.get("rate_13w_ago") is not None else None,
        "dom_yoy": hb.get("dom_yoy"),
        "cuts_yoy": hb.get("cuts_yoy"),
        "list_price_yoy": hb.get("price_yoy"),
    }
    # which quarter each quarterly figure comes from (adjusted balance-sheet figures are current, so not listed)
    asof = {"contrib_margin": rows["contribution_margin_pct"].get("quarter_end"),
            "gross_margin": rows["gross_profit_musd"].get("quarter_end"),
            "aged_inventory": rows["aged_120_pct"].get("quarter_end"),
            "adj_ebitda_m": rows["adj_ebitda_musd"].get("quarter_end"),
            "ani_margin": ani_row.get("quarter_end"), "sbc_pct": rows["sbc_musd"].get("quarter_end"),
            "home_debt_ltv": bs.get("quarter_end"), "dilution_live": dq}
    if not applied:
        asof.update(cash_vs_notes=bs.get("quarter_end"), runway_q=bs.get("quarter_end"))
    notes = {}
    mk = rows["aged_120_pct"].get("market_aged_120_pct")
    if mk is not None:
        notes["aged_inventory"] = f"Whole market: {mk:g}%"
    if dilution:
        notes["dilution_live"] = f"Of {dilution['potential_m']:,.0f}M potential shares in all"
    if hb:
        notes.update({k: f"{len(housing['metros'])} markets, {_month(housing['month'])}" for k in ("dom_yoy", "cuts_yoy", "list_price_yoy")})
    if home_ltv is not None:
        notes["home_debt_ltv"] = f"Cushion {100 - home_ltv * 100:.1f}% if home prices fall"
    extra = {"market_cap_musd": mcap, "ttm_revenue_musd": ttm_rev, "shares_m": shares, "pro_forma": pf,
             "capital_events": [e["what"] for e in applied], "q3_revenue_estimate_musd": q3_rev_est,
             "revenue_per_home_k": rph * 1000 if rph else None, "q3_prior_revenue_musd": q3_prior,
             "shares_year_ago": (year_ago or {}).get("shares_m"), "shares_year_ago_date": (year_ago or {}).get("quarter_end"),
             "reported_rev_yoy": ch(g(-1, "revenue_musd"), g(-5, "revenue_musd")),
             "adj_net_income_musd": ani, "adj_net_income_quarter": ani_row.get("quarter_end"),
             "balance_sheet": {k: bs.get(k) for k in ("quarter_end", "cash_musd", "home_debt_musd", "convert_debt_musd",
                                                      "total_debt_musd", "inventory_musd", "equity_musd")},
             "dilution": dilution, "housing": housing, "asof": asof, "notes": notes, "latest_quarter": last["quarter_end"],
             "mortgage": macro}
    return m, extra


def _quarters_between(a, b):
    """Whole quarters from quarter end a to quarter end b."""
    da, db = dt.date.fromisoformat(a), dt.date.fromisoformat(b)
    return round(((db.year - da.year) * 12 + db.month - da.month) / 3)


def _month(ym):
    try:
        return dt.date.fromisoformat(ym + "-01").strftime("%b %Y")
    except (TypeError, ValueError):
        return str(ym)


def score(m, extra=None):
    """Metrics to points. A quarterly figure one quarter behind still counts (and is labelled);
    two or more quarters behind, it is shown but no longer scored."""
    extra = extra or {}
    asof, notes, latest = extra.get("asof", {}), extra.get("notes", {}), extra.get("latest_quarter")
    pillars, total, wsum = [], 0.0, 0
    for name, w, keys, ask in PILLARS:
        items = []
        for k in keys:
            label, bad, good, f = RULES[k]
            lag = _quarters_between(asof[k], latest) if asof.get(k) and latest else 0
            pts = lin(m.get(k), bad, good) if lag < 2 else None
            items.append({"key": k, "label": label, "value": m.get(k), "score": pts, "fmt": f, "bad": bad, "good": good,
                          "asof": asof.get(k), "lag": lag, "note": notes.get(k)})
        got = [i["score"] for i in items if i["score"] is not None]
        ps = round(sum(got) / len(got), 1) if got else None
        if ps is not None:
            total += ps * w
            wsum += w
        pillars.append({"name": name, "weight": w, "question": ask, "score": ps, "metrics": items})
    overall = round(total / wsum, 2) if wsum else None
    grade, verdict = next((g, v) for t, g, v in GRADES if overall is not None and overall >= t)
    out = {"overall": overall, "grade": grade, "verdict": verdict, "pillars": pillars, "weights_used": wsum}
    out["why"] = explain(out)
    return out


# ---------------------------------------------------------------- output
def fmt(v, kind):
    if v is None:
        return "n/a"
    if kind == "pct":
        return f"{v*100:+.1f}%"
    if kind == "rate":
        return f"{v*100:.2f}%"
    if kind == "share":
        return f"{v*100:.1f}%"
    if kind == "pp":
        return f"{v*100:+.2f}pp"
    if kind == "qtrs":
        return "self-funding" if v >= 99 else f"{v:.1f} qtrs"
    return f"{v:.2f}x"


def print_report(res):
    s, px, ex, acc = res["score"], res["price"], res["extra"], res.get("accountable") or {}
    print(f"\nOpendoor ({TICKER}) scorecard  -  close ${px['price']:.2f} on {px['date']} ({px.get('source')})")
    print(f"Overall {s['overall']:.2f}/10  grade {s['grade']}  -  {s['verdict']}")
    print(f"{s['why']}\n")
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


INK, MUTED, MUTED2, LINE, BLUE = "#23201d", "#786e64", "#5d554d", "#ebe7e2", "#1d4ed8"


def _smooth(pts):
    """Smooth SVG path through points (Catmull-Rom as cubic Beziers)."""
    if not pts:
        return ""
    d = f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    for i in range(len(pts) - 1):
        p0 = pts[i - 1] if i > 0 else pts[i]
        p1, p2 = pts[i], pts[i + 1]
        p3 = pts[i + 2] if i + 2 < len(pts) else p2
        c1 = (p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6)
        d += f" C{c1[0]:.1f},{c1[1]:.1f} {c2[0]:.1f},{c2[1]:.1f} {p2[0]:.1f},{p2[1]:.1f}"
    return d


def _d(iso):
    """'2026-10-24' -> 'Oct 24, 2026'; leaves anything else alone."""
    try:
        d = dt.date.fromisoformat(str(iso)[:10])
        return f"{d.strftime('%b')} {d.day}, {d.year}"
    except ValueError:
        return str(iso)


def _qlabel(iso):
    d = dt.date.fromisoformat(iso)
    return f"Q{(d.month - 1) // 3 + 1} {d.year}"


def _num(v, suffix="", none="n/a"):
    return none if v is None else f"{v:.1f}{suffix}"


def _tone(score):
    if score is None:
        return "grey"
    return "green" if score >= 6.5 else "amber" if score >= 4 else "red"


def _badge(text, tone, arrow=""):
    return f'<span class="badge {tone}">{arrow}{html.escape(text)}</span>'


def _contracts_svg(rows):
    """Weekly contracts (blue line + soft fill) against Opendoor's projected range (blurred green band)."""
    year = next((r[0][:4] for r in reversed(rows) if r[1] is not None), None)
    rows = [r for r in rows if year and r[0] >= f"{year}-01-01"]
    if len(rows) < 4:
        return ""
    W, H, L, R, T, B = 1000, 340, 46, 16, 20, 34
    n = len(rows)
    ymax = max(max((r[1] or 0) for r in rows), max((r[3] or 0) for r in rows))
    step = 250 if ymax > 600 else 100
    top = (int(ymax / step) + 1) * step
    x = lambda i: L + i * (W - L - R) / (n - 1)
    y = lambda v: T + (1 - v / top) * (H - T - B)
    actual = [(x(i), y(r[1])) for i, r in enumerate(rows) if r[1] is not None]
    last_i = max(i for i, r in enumerate(rows) if r[1] is not None)
    band = [(i, r) for i, r in enumerate(rows) if r[2] is not None and r[3] is not None and r[2] != r[3]]
    pts = []
    for i, r in enumerate(rows):
        has_band = r[2] is not None and r[3] is not None and r[2] != r[3]
        pts.append({"d": _d(r[0]), "x": round(x(i), 1), "y": round(y(r[1]), 1) if r[1] is not None else None, "a": r[1],
                    "lo": r[2] if has_band else None, "hi": r[3] if has_band else None,
                    "yt": round(y(r[1] if r[1] is not None else (r[3] if has_band else 0)), 1)})
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Weekly acquisition contracts vs projected range" '
           f'class="ichart" data-w="{W}" data-l="{L}" data-r="{R}" data-points=\'{json.dumps(pts, separators=(",", ":"))}\'>',
           '<defs><linearGradient id="fill" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#1d4ed8" stop-opacity=".16"/>'
           '<stop offset="1" stop-color="#1d4ed8" stop-opacity="0"/></linearGradient>'
           '<filter id="blur" x="-5%" y="-20%" width="110%" height="140%"><feGaussianBlur stdDeviation="7"/></filter>'
           '<pattern id="dots" width="8" height="8" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r=".9" fill="#d9d4ce"/></pattern></defs>']
    if last_i < n - 1:  # the future: dotted background, like a forecast area
        out.append(f'<rect x="{x(last_i):.1f}" y="{T}" width="{x(n - 1) - x(last_i):.1f}" height="{H - T - B}" fill="url(#dots)"/>')
    for t in range(0, top + 1, step):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{LINE}" stroke-dasharray="{"0" if t == 0 else "2 4"}"/>'
                   f'<text x="{L - 10}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t:,}</text>')
    seen = set()
    for i, r in enumerate(rows):
        mth = r[0][:7]
        if mth not in seen:
            seen.add(mth)
            label = dt.date.fromisoformat(r[0]).strftime("%b")
            out.append(f'<text x="{x(i):.1f}" y="{H - 10}" text-anchor="middle" class="tick">{label}</text>')
    if band:
        hi = [(x(i), y(r[3])) for i, r in band]
        lo = [(x(i), y(r[2])) for i, r in reversed(band)]
        poly = " ".join(f"{a:.1f},{b:.1f}" for a, b in hi + lo)
        out.append(f'<polygon points="{poly}" fill="#34d399" opacity=".28" filter="url(#blur)"/>')
        out.append(f'<path d="{_smooth(hi)}" fill="none" stroke="#34d399" stroke-width="1.5" opacity=".55"/>')
        out.append(f'<path d="{_smooth(list(reversed(lo)))}" fill="none" stroke="#34d399" stroke-width="1.5" opacity=".55"/>')
    line = _smooth(actual)
    out.append(f'<path d="{line} L{actual[-1][0]:.1f},{y(0):.1f} L{actual[0][0]:.1f},{y(0):.1f} Z" fill="url(#fill)"/>')
    out.append(f'<path d="{line}" fill="none" stroke="{BLUE}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>')
    lx, ly = actual[-1]
    # latest point: soft halo, a ring, and a slow pulse (the "glow")
    out.append(f'<circle class="pulse" cx="{lx:.1f}" cy="{ly:.1f}" r="7" fill="{BLUE}"/>'
               f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="12" fill="{BLUE}" opacity=".12"/>'
               f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="8.5" fill="none" stroke="{BLUE}" stroke-opacity=".35" stroke-width="1.5"/>'
               f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="4.5" fill="{BLUE}" stroke="#fff" stroke-width="2"/>')
    # hover layer: vertical guide and a dot that snaps onto the curve
    out.append(f'<line class="vline" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="#cfd3dc" stroke-width="1.2" style="display:none"/>'
               f'<circle class="hdot" r="5" fill="{BLUE}" stroke="#fff" stroke-width="2" style="display:none"/>'
               f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="transparent"/>')
    out.append(f'<text x="{lx + 14:.1f}" y="{ly - 10:.1f}" class="endlabel">{int(rows[last_i][1]):,}</text>')
    out.append("</svg>")
    return "".join(out)


def _history_svg(hist):
    pts = [(h["run_at"][:10], float(h["overall"])) for h in hist if h.get("overall") not in (None, "", "None")]
    if len(pts) < 2:
        return '<p class="note">The chart appears after the second day of scores.</p>'
    W, H, L, R, T, B = 1000, 220, 34, 16, 16, 30
    x = lambda i: L + i * (W - L - R) / (len(pts) - 1)
    y = lambda v: T + (1 - v / 10) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Overall score over time">',
           '<defs><linearGradient id="fill2" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#1d4ed8" stop-opacity=".14"/>'
           '<stop offset="1" stop-color="#1d4ed8" stop-opacity="0"/></linearGradient></defs>']
    for t in (0, 5, 10):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{LINE}" stroke-dasharray="{"0" if t == 0 else "2 4"}"/>'
                   f'<text x="{L - 10}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t}</text>')
    xy = [(x(i), y(v)) for i, (_, v) in enumerate(pts)]
    line = _smooth(xy) if len(xy) > 2 else "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in xy)
    out.append(f'<path d="{line} L{xy[-1][0]:.1f},{y(0):.1f} L{xy[0][0]:.1f},{y(0):.1f} Z" fill="url(#fill2)"/>')
    out.append(f'<path d="{line}" fill="none" stroke="{BLUE}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>')
    for i, (d, v) in enumerate(pts):
        out.append(f'<circle cx="{x(i):.1f}" cy="{y(v):.1f}" r="3.5" fill="{BLUE}"><title>{d}: {v:.2f}</title></circle>')
    first, last = _d(pts[0][0]), _d(pts[-1][0])
    out.append(f'<text x="{L}" y="{H - 8}" class="tick">{first}</text><text x="{W - R}" y="{H - 8}" text-anchor="end" class="tick">{last}</text>')
    out.append("</svg>")
    return "".join(out)


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#c8aaff"/>
<stop offset=".55" stop-color="#a0c3ff"/><stop offset="1" stop-color="#eba0fa"/></linearGradient></defs>
<rect width="64" height="64" rx="15" fill="url(#g)"/>
<path d="M11 45 C17 45 20 29 27 31 S36 44 42 36 S45 25 48 22" fill="none" stroke="#1d4ed8" stroke-width="6"
stroke-linecap="round" stroke-linejoin="round"/>
<circle cx="48" cy="22" r="10" fill="#1d4ed8" opacity=".18"/>
<circle cx="48" cy="22" r="6.5" fill="#1d4ed8" stroke="#fff" stroke-width="3"/>
</svg>
"""

CSS = """
:root{--ink:#23201d;--muted:#786e64;--muted2:#5d554d;--line:#ebe7e2;--bg:#f3f2f0;--blue:#1d4ed8}
*{box-sizing:border-box}
html{background:#f9f8f8;scroll-padding-top:92px;scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media (max-width:520px){html{scroll-padding-top:104px}}
body{margin:0;color:var(--ink);font:400 15px/1.5 Inter,system-ui,-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif;
letter-spacing:-.011em;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;
background:radial-gradient(80% 40% at 10% 0%,rgba(200,170,255,.55) 0%,rgba(0,0,0,0) 60%),
radial-gradient(65% 35% at 50% 0%,rgba(160,195,255,.55) 0%,rgba(0,0,0,0) 58%),
radial-gradient(50% 30% at 88% 0%,rgba(235,160,250,.45) 0%,rgba(0,0,0,0) 50%),var(--bg);background-repeat:no-repeat}
a{color:inherit}
.bar{background:rgba(255,255,255,.92);backdrop-filter:saturate(1.4) blur(8px);position:sticky;top:0;z-index:5;border-bottom:1px solid rgba(0,0,0,.04)}
.bar .in{max-width:1280px;margin:0 auto;padding:12px 24px;display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:20px}
.bar .in>*{grid-row:1}
.brand{grid-column:2;text-align:center;font-weight:600;font-size:18px;line-height:1.25;letter-spacing:-.02em;text-decoration:none}
.brand small{display:block;font-weight:400;color:var(--muted);font-size:12.5px;margin-top:2px;letter-spacing:0}
.bar nav.desk{grid-column:1;justify-self:start;display:flex;gap:22px;font-size:15px;font-weight:500}
.bar nav.desk a{text-decoration:none;white-space:nowrap}
.burger{display:none;grid-column:3;justify-self:end;width:42px;height:42px;border:1px solid var(--line);border-radius:12px;background:#fff;
padding:0;cursor:pointer;position:relative}
.burger span{position:absolute;left:12px;right:12px;height:2px;border-radius:2px;background:var(--ink);transition:transform .25s,opacity .2s,top .25s}
.burger span:nth-child(1){top:14px}.burger span:nth-child(2){top:20px}.burger span:nth-child(3){top:26px}
.burger[aria-expanded="true"] span:nth-child(1){top:20px;transform:rotate(45deg)}
.burger[aria-expanded="true"] span:nth-child(2){opacity:0}
.burger[aria-expanded="true"] span:nth-child(3){top:20px;transform:rotate(-45deg)}
.menu{position:fixed;left:0;right:0;top:var(--barh,64px);bottom:0;background:rgba(249,248,248,.98);backdrop-filter:blur(10px);
padding:8px 16px 32px;overflow-y:auto;z-index:4;animation:drop .22s ease-out}
.menu[hidden]{display:none}
@keyframes drop{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.menu a{display:block;background:#fff;border-radius:16px;padding:14px 18px;margin-top:8px;text-decoration:none}
.menu a span{display:block;font-size:20px;font-weight:500;letter-spacing:-.02em}
.menu a small{display:block;font-size:14px;color:var(--muted);margin-top:1px}
.menu a.menu-by{background:none;text-align:center;font-size:14px;color:var(--muted);margin-top:18px}
.credit{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 28px;margin:32px 0 0}
.by{grid-column:3;justify-self:end;font-size:13px;color:var(--muted);text-decoration:none;white-space:nowrap;padding:6px 12px;border:1px solid var(--line);border-radius:10px;background:#fff}
.by:hover{color:var(--ink)}
.credit{font-size:14px;color:var(--ink);padding:14px 0;border-top:1px solid #e3dfda;border-bottom:1px solid #e3dfda}
.credit a{font-weight:500;text-decoration:none;border-bottom:1px solid #cfc8c0}.credit a:hover{border-color:var(--ink)}
.wrap{max-width:1280px;margin:0 auto;padding:0 24px 120px}
.hero{display:grid;grid-template-columns:1.3fr 1fr;gap:40px;align-items:end;padding:64px 0 56px;position:relative}
.asof{position:absolute;top:28px;right:0;text-align:right;font-size:14px;font-weight:500}
.asof i+i{margin-top:0}
.asof i{display:block;font-size:12px;font-weight:400;color:var(--muted)}
.asof .dot{display:inline-block;width:9px;height:9px;border-radius:50%;background:#4cc38a;margin-right:8px;vertical-align:1px;
box-shadow:0 0 0 0 rgba(76,195,138,.55),0 0 8px 1px rgba(76,195,138,.6);animation:glow 2.4s ease-out infinite}
@keyframes glow{0%{box-shadow:0 0 0 0 rgba(76,195,138,.55),0 0 8px 1px rgba(76,195,138,.6)}
70%{box-shadow:0 0 0 9px rgba(76,195,138,0),0 0 8px 1px rgba(76,195,138,.6)}100%{box-shadow:0 0 0 0 rgba(76,195,138,0),0 0 8px 1px rgba(76,195,138,.6)}}
.chartbox{position:relative}
@media (max-width:640px){.chartbox{overflow-x:auto;-webkit-overflow-scrolling:touch}.chartbox .ichart{min-width:760px;touch-action:auto}
.swipe{display:block!important}}
.swipe{display:none;font-size:12px;color:var(--muted);margin:2px 0 6px}
.ichart{touch-action:pan-y;cursor:crosshair}
.ichart .pulse{transform-box:fill-box;transform-origin:center;opacity:.45;animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{transform:scale(1);opacity:.45}80%,100%{transform:scale(3.2);opacity:0}}
.tip{position:absolute;pointer-events:none;background:#fff;border:1px solid #e6e3df;border-radius:10px;
box-shadow:0 6px 20px rgba(35,32,29,.08);padding:9px 13px;font-size:14px;line-height:1.55;white-space:nowrap;z-index:3}
.tip b{display:block;font-weight:600;color:var(--ink)}.tip .c{display:block;color:var(--blue)}.tip .g{display:block;color:#0f9f6e}
.tip .n{display:block;color:var(--muted)}
@media (prefers-reduced-motion:reduce){.asof .dot,.ichart .pulse{animation:none}}
h1{font-size:clamp(46px,8.4vw,92px);line-height:1;font-weight:500;letter-spacing:-.034em;margin:40px 0 0}
.lede{font-size:18px;color:var(--muted2);max-width:430px;margin:0 0 6px}
.card{background:#fff;border-radius:20px;margin-top:24px;overflow:hidden}
.card .hd{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:16px 24px;border-bottom:1px solid var(--line)}
.card h2{font-size:22px;font-weight:500;line-height:1.2;margin:0;letter-spacing:-.02em}
.sub{color:var(--muted);font-size:16px;margin:2px 0 0}
.right{text-align:right}
.right .sub{font-size:14px;color:var(--muted2);margin-top:6px}
.bd{padding:20px 24px}
.ft{padding:14px 24px 18px;border-top:1px solid var(--line);font-size:13px;color:#5d5d6a}
.badge{display:inline-block;border-radius:8px;padding:4px 8px;font-size:16px;font-weight:500;white-space:nowrap}
.badge.green{background:#e6f4ea;color:#10b981}.badge.red{background:#fdeceb;color:#e5484d}
.badge.amber{background:#fdf3e1;color:#c27c0e}.badge.grey{background:#f1efec;color:var(--muted2)}
.score{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.score .num{font-size:clamp(64px,10vw,112px);font-weight:500;letter-spacing:-.045em;line-height:1}
.score .of{font-size:28px;color:var(--muted);font-weight:400;letter-spacing:-.02em}
.score .grade{font-size:28px;font-weight:500;padding:6px 14px;border-radius:12px}
.verdict{font-size:20px;font-weight:500;margin:18px 0 4px;letter-spacing:-.015em}
.why{font-size:17px;color:var(--muted2);max-width:820px;margin:0}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);border-top:1px solid var(--line)}
.kpi{padding:16px 24px;border-right:1px solid var(--line)}.kpi:last-child{border-right:0}
.kpi .l{font-size:13px;color:var(--muted)}.kpi .v{font-size:26px;font-weight:500;letter-spacing:-.03em;margin-top:2px}
.kpi .s{font-size:13px;color:var(--muted2)}
.pbar{display:grid;grid-template-columns:150px 1fr 44px;align-items:center;gap:14px;padding:7px 0;font-size:15px}
.pbar .track{height:10px;border-radius:6px;background:#f1efec;overflow:hidden}
.pbar .fill{height:100%;border-radius:6px}
.fill.green{background:#34d399}.fill.amber{background:#f5b53f}.fill.red{background:#f07470}.fill.grey{background:#d6d1cb}
.pbar .w{color:var(--muted);font-size:13px;margin-left:6px}
.pbar b{font-weight:500;text-align:right}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
.grid .card{margin-top:0}
.section{font-size:13px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:56px 0 0}
.m{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:14px}
.m:last-child{border-bottom:0}.m .lab{color:var(--muted2)}.m .val{font-weight:500;white-space:nowrap}
.m .pts{color:var(--muted);font-size:12px;display:block;text-align:right}
.legend{display:flex;gap:22px;font-size:13px;color:var(--muted2);padding:0 0 6px}
.legend i{display:inline-block;width:18px;height:3px;border-radius:2px;vertical-align:3px;margin-right:8px}
.tick{font-size:12px;fill:#9a9088}.endlabel{font-size:13px;font-weight:500;fill:var(--blue)}
table{width:100%;border-collapse:collapse;font-size:14px}
th{text-align:right;font-weight:500;color:var(--muted);font-size:12px;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
td{text-align:right;padding:10px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
.scroll{overflow-x:auto}
details summary{cursor:pointer;list-style:none}details summary::-webkit-details-marker{display:none}
details[open] .chev{transform:rotate(180deg)}.chev{display:inline-block;transition:transform .2s;color:var(--muted)}
.note{color:var(--muted);font-size:13px}
footer{margin-top:56px;font-size:13px;color:#5d5d6a;line-height:1.6;max-width:900px}
footer b{color:var(--ink);font-weight:500}
.nw{white-space:nowrap}table.mk th small{display:block;font-weight:400;color:#9a9088;font-size:11px}
.grid2{display:grid;grid-template-columns:1fr 1.2fr;gap:24px;margin-top:16px;align-items:start}
.grid2 .card{margin-top:0}
.dbar{padding:4px 0 18px}.dbar+.dbar{border-top:1px solid var(--line);padding-top:18px;padding-bottom:4px}
.dlab{display:flex;justify-content:space-between;align-items:baseline;gap:12px;font-size:15px;color:var(--muted2);margin:0 0 10px}
.dlab b{font-size:26px;font-weight:500;color:var(--ink);letter-spacing:-.03em}
.dtrack{height:14px;border-radius:8px;background:#f1efec;overflow:hidden;display:flex}
.dfill{height:100%;background:var(--blue)}.dcush{height:100%;background:#34d399}.dcash{height:100%;background:#34d399}
.pair .dfill,.pair .dcash{border-radius:8px}
.dleg{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13px;color:var(--muted2);margin-top:10px}
.dleg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.dim{color:var(--muted)}.dnote{font-size:13px;color:var(--muted);margin:10px 0 0;max-width:560px}
.pair{display:grid;grid-template-columns:132px 1fr 76px;align-items:center;gap:12px;font-size:14px;margin:8px 0}
.pair b{font-weight:500;text-align:right}
table.dil td:first-child{white-space:normal}
table.dil td small{display:block;font-size:12px;color:var(--muted);font-weight:400;margin-top:1px}
td.on{font-weight:500}td.off{color:var(--muted)}
tr.tot td{font-weight:600;border-top:1px solid #d9d4ce}tr.us td{color:var(--muted)}
td.up{color:#10b981}td.down{color:#e5484d}
.m .lab small{display:block;font-size:12px;color:var(--muted);margin-top:1px}
.m .lab em.stale{display:table;font-style:normal;font-size:11px;font-weight:500;color:#c27c0e;background:#fdf3e1;border-radius:6px;padding:1px 7px;margin-top:4px}
.m .lab em.stale.old{color:var(--muted2);background:#f1efec}
.dchart{touch-action:pan-y;cursor:crosshair}
.dchart .pulse{transform-box:fill-box;transform-origin:center;opacity:.45;animation:pulse 2.4s ease-out infinite}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
@media (max-width:640px){.chartbox .dchart{min-width:600px}}
@media (max-width:520px){.pair{grid-template-columns:96px 1fr 70px;gap:8px}.dlab b{font-size:22px}}
@media (prefers-reduced-motion:reduce){.dchart .pulse{animation:none}}
@media (max-width:1180px){.bar nav.desk{display:none}.burger{display:block}.by{grid-column:1;justify-self:start}
.menu a{max-width:680px;margin-left:auto;margin-right:auto}}
@media (max-width:900px){.burger{display:block}.by{display:none}.bar nav.desk{display:none}.hero{grid-template-columns:1fr;gap:20px;padding-top:48px}.asof{position:static;text-align:left;order:-1}
.grid{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.kpi:nth-child(2){border-right:0}
.kpi:nth-child(-n+2){border-bottom:1px solid var(--line)}h1{margin-top:8px}.bar nav.desk{display:none}}
@media (max-width:520px){.brand small{font-size:11px}.brand{font-size:17px}.pbar{grid-template-columns:110px 1fr 34px;gap:10px;font-size:14px}.pbar .w{display:none}
.card .hd{flex-wrap:nowrap;gap:12px}.card .hd>div:first-child{flex:1 1 auto;min-width:0}.card .hd>.right{flex:0 0 auto}.card .hd.stack{flex-direction:column;gap:10px}.hd.stack .right{text-align:left}.by{display:none}.bar .in{grid-template-columns:42px 1fr 42px;gap:8px;padding:10px 14px}.brand{grid-column:2}.bd,.card .hd,.kpi,.ft{padding-left:18px;padding-right:18px}.wrap{padding:0 14px 80px}}
"""


def _money(v):
    return "n/a" if v is None else f"${v:,.0f}M"


def _debt_card(res, pillar):
    ex = res["extra"]
    bs, pf = ex.get("balance_sheet") or {}, ex.get("pro_forma") or {}
    home, inv, cash, notes = bs.get("home_debt_musd"), bs.get("inventory_musd"), pf.get("cash_musd"), pf.get("convert_debt_musd")
    if not home or not inv or cash is None or notes is None:
        return ""
    e = html.escape
    ltv = home / inv
    used, cush = min(ltv, 1.0) * 100, max(0.0, 1 - ltv) * 100
    top = max(cash, notes, 1)
    cover = "no notes outstanding" if notes <= 0 else f"{cash / notes:.1f}×"
    events = ex.get("capital_events") or []
    adj = (" Cash and notes are adjusted for: " + "; ".join(events) + ".") if events else ""
    q = _d(bs.get("quarter_end"))
    return (f'<div class="card" id="debt"><div class="hd"><div><h2>Debt, split by what backs it</h2>'
            f'<p class="sub">Most of the debt is secured by the homes themselves</p></div>'
            f'<div class="right">{_badge(_num(pillar["score"]), _tone(pillar["score"]))}<p class="sub nw">Balance sheet</p></div></div>'
            f'<div class="bd">'
            f'<div class="dbar"><div class="dlab"><span>Home-backed debt vs the homes it funds</span><b>{ltv * 100:.1f}%</b></div>'
            f'<div class="dtrack"><div class="dfill" style="width:{used:.1f}%"></div><div class="dcush" style="width:{cush:.1f}%"></div></div>'
            f'<div class="dleg"><span><i style="background:{BLUE}"></i>Debt {_money(home)}</span>'
            f'<span><i style="background:#34d399"></i>Cushion {_money(max(0, inv - home))}</span><span class="dim">Homes held {_money(inv)}</span></div>'
            f'<p class="dnote">If the homes sold for {cush:.1f}% less than their book value, the cushion would be gone. '
            f'Lenders can take the homes but, except in limited cases, cannot claim the company.</p></div>'
            f'<div class="dbar"><div class="dlab"><span>Cash vs the company&#39;s own debt</span><b>{cover}</b></div>'
            f'<div class="pair"><span>Cash</span><div class="dtrack"><div class="dcash" style="width:{cash / top * 100:.1f}%"></div></div><b>{_money(cash)}</b></div>'
            f'<div class="pair"><span>Convertible notes</span><div class="dtrack"><div class="dfill" style="width:{max(0, notes) / top * 100:.1f}%"></div></div><b>{_money(notes)}</b></div>'
            f'<p class="dnote">The notes are unsecured, so they are the debt the company itself must repay or convert.</p></div>'
            f'</div><div class="ft">Homes and home-backed debt from the {e(q)} balance sheet (10-Q).{e(adj)}</div></div>')


def _dilution_svg(curve, price, shares):
    W, H, L, R, T, B = 760, 230, 46, 16, 18, 32
    xmax = 35.0
    pts = [(p_, v) for p_, v in curve if p_ <= xmax]
    if len(pts) < 3:
        return ""
    vmax = max(v for _, v in pts)
    top = max(0.1, (int(vmax * 10) + 1) / 10)
    x = lambda p_: L + p_ / xmax * (W - L - R)
    y = lambda v: T + (1 - v / top) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Live potential dilution at different share prices" '
           f'class="dchart" data-w="{W}" data-l="{L}" data-r="{R}" data-xmax="{xmax}" data-shares="{shares:.1f}" '
           f'data-points=\'{json.dumps([[round(a, 2), round(b, 4), round(y(b), 1)] for a, b in pts], separators=(",", ":"))}\'>',
           '<defs><linearGradient id="fill3" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="#1d4ed8" stop-opacity=".16"/>'
           '<stop offset="1" stop-color="#1d4ed8" stop-opacity="0"/></linearGradient></defs>']
    step = 0.1 if top <= 0.5 else 0.2
    t = 0.0
    while t <= top + 1e-9:
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{LINE}" stroke-dasharray="{"0" if t == 0 else "2 4"}"/>'
                   f'<text x="{L - 10}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t * 100:.0f}%</text>')
        t += step
    for p_ in range(0, 36, 5):
        out.append(f'<text x="{x(p_):.1f}" y="{H - 10}" text-anchor="middle" class="tick">${p_}</text>')
    xy = [(x(a), y(b)) for a, b in pts]
    line = "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in xy)
    out.append(f'<path d="{line} L{xy[-1][0]:.1f},{y(0):.1f} L{xy[0][0]:.1f},{y(0):.1f} Z" fill="url(#fill3)"/>')
    out.append(f'<path d="{line}" fill="none" stroke="{BLUE}" stroke-width="2.4" stroke-linejoin="round"/>')
    now = min(pts, key=lambda pv: abs(pv[0] - price))
    cx, cy = x(price), y(now[1])
    out.append(f'<line x1="{cx:.1f}" x2="{cx:.1f}" y1="{T}" y2="{H - B}" stroke="{BLUE}" stroke-opacity=".35" stroke-dasharray="3 4"/>'
               f'<circle class="pulse" cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="{BLUE}"/>'
               f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="4.5" fill="{BLUE}" stroke="#fff" stroke-width="2"/>'
               f'<text x="{cx + 10:.1f}" y="{cy + 20:.1f}" class="endlabel">Today ${price:.2f}</text>')
    out.append(f'<line class="vline" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="#cfd3dc" stroke-width="1.2" style="display:none"/>'
               f'<circle class="hdot" r="5" fill="{BLUE}" stroke="#fff" stroke-width="2" style="display:none"/>'
               f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="transparent"/></svg>')
    return "".join(out)


def _dilution_card(res, metric):
    d = res["extra"].get("dilution")
    if not d:
        return ""
    e = html.escape
    rows = "".join(f'<tr><td>{e(l["label"])}<small>{e(l["needs"])}</small></td><td>{l["n"]:,.1f}M</td>'
                   + (f'<td class="on">{l["live"]:,.1f}M</td></tr>' if l["live"] > 0.05 else '<td class="off">0</td></tr>')
                   for l in d["lines"])
    pct = d["live_m"] / d["shares_m"]
    src = (f"10-Q for the quarter ended {_d(d['quarter'])}, table of anti-dilutive securities" if d["source"] == "10-Q"
           else f"XBRL total for {_d(d['quarter'])} (no breakdown found, all counted in full)")
    return (f'<div class="card" id="dilution"><div class="hd"><div><h2>Dilution, line by line</h2>'
            f'<p class="sub">Every potential new share, and how many are live at ${d["price"]:.2f}</p></div>'
            f'<div class="right">{_badge(f"{pct * 100:.1f}%", _tone(metric["score"]))}<p class="sub">live today</p></div></div>'
            f'<div class="bd scroll" style="padding-top:4px"><table class="dil"><tr><th>Instrument</th><th>Potential</th><th>Live today</th></tr>{rows}'
            f'<tr class="tot"><td>Total</td><td>{d["potential_m"]:,.1f}M</td><td>{d["live_m"]:,.1f}M</td></tr></table></div>'
            f'<div class="bd" style="padding-top:0"><p class="dlab" style="margin:6px 0 2px"><span>Live dilution if the share price were…</span></p>'
            f'<p class="swipe">Swipe for higher prices, tap the chart for figures</p><div class="chartbox dbox">{_dilution_svg(d["curve"], d["price"], d["shares_m"])}<div class="tip" hidden></div></div></div>'
            f'<div class="ft">Live means it would become shares, or hand over their value, at that price. Awards with a price target '
            f'count once the stock reaches it; warrants and convertibles count only the value above what they cost or repay. '
            f'Shares outstanding: {d["shares_m"]:,.1f}M. Source: {e(src)}; contract terms from Opendoor&#39;s 8-Ks.</div></div>')


def _markets_card(res, pillar):
    h = res["extra"].get("housing")
    if not h or not h.get("metros"):
        return ""
    e = html.escape
    def cell(v, kind, bad_up=True):
        if v is None:
            return '<td class="dim">–</td>'
        txt = f"{v * 100:+.1f}%" if kind == "pct" else f"{v * 100:+.1f}"
        good = (v < 0) if bad_up else (v > 0)
        return f'<td class="{"up" if good else "down" if abs(v) > 1e-9 else ""}">{txt}</td>'
    def row(r, cls=""):
        dom = "–" if r.get("dom") is None else f"{r['dom']:.0f}"
        cut = "–" if r.get("cut_share") is None else f"{r['cut_share'] * 100:.0f}"
        return (f'<tr class="{cls}"><td>{e(r["name"])}</td><td>{dom}</td>{cell(r.get("dom_yoy"), "pct")}<td>{cut}</td>'
                f'{cell(r.get("cuts_yoy"), "pp")}{cell(r.get("price_yoy"), "pct", bad_up=False)}{cell(r.get("listings_yoy"), "pct")}</tr>')
    b = dict(h["basket"], name=f"Median of {len(h['metros'])} markets")
    body = "".join(row(r) for r in h["metros"]) + row(b, "tot") + (row(h["us"], "us") if h.get("us") else "")
    return (f'<div class="card" id="markets"><div class="hd"><div><h2>Housing in Opendoor&#39;s markets</h2>'
            f'<p class="sub">How fast homes sell, and at what price, {e(_month(h["month"]))} vs a year earlier</p></div>'
            f'<div class="right">{_badge(_num(pillar["score"]), _tone(pillar["score"]))}<p class="sub nw">Housing market</p></div></div>'
            f'<div class="bd scroll" style="padding-top:4px"><table class="mk"><tr><th>Market</th><th>Days on market<small>median</small></th><th>Days on market<small>1-year change</small></th>'
            f'<th>Price cuts<small>per 100 listings</small></th><th>Price cuts<small>1-year change</small></th>'
            f'<th>Asking prices<small>1-year change</small></th><th>Homes for sale<small>1-year change</small></th></tr>{body}</table></div>'
            f'<div class="ft">Green helps Opendoor resell its homes faster and for more; red hurts. Price cuts are the month&#39;s price '
            f'reductions per 100 active listings. Twelve of Opendoor&#39;s largest Sunbelt and Mountain markets, equally weighted; the median feeds the score. Source: {e(h.get("source", "Realtor.com"))} monthly housing data.</div></div>')


def write_html(res, path):
    s, px, ex = res["score"], res["price"], res["extra"]
    acc = res.get("accountable") or {}
    e = html.escape
    tone = _tone(s["overall"])
    ch = res.get("change")
    if not ch:
        change = _badge("First score", "grey")
        change_sub = "Changes show from the next run"
    elif ch.get("method_changed"):
        change = _badge("Method updated", "grey")
        change_sub = f"Not comparable with {_d(ch['since'])} (v{METHOD})"
    else:
        d = ch["overall"]
        change = _badge(f"{d:+.2f}", "green" if d > 0.004 else "red" if d < -0.004 else "grey",
                        "↗ " if d > 0.004 else "↘ " if d < -0.004 else "")
        change_sub = f"vs {_d(ch['since'])}" + (f", grade was {ch['grade_from']}" if ch["grade_from"] != s["grade"] else "")
    asof = acc.get("as_of") or px["date"]
    mort = ex.get("mortgage") or {}
    q3s, q3e = acc.get("q3_homes_sold"), acc.get("q3_homes_sold_expected")
    kpis = [("Share price", f"${px['price']:.2f}", f"close {_d(px['date'])}"),
            ("Market cap", f"${(ex['market_cap_musd'] or 0) / 1000:.2f}bn", "pro forma shares"),
            (f"{acc.get('prev_quarter', 'Last qtr')} homes sold", f"{q3s:,.0f}" if q3s else "n/a",
             f"vs {q3e:,.0f} expected" if q3e else "Opendoor tracker"),
            ("30-yr mortgage rate", f"{mort['rate'] * 100:.2f}%" if mort.get("rate") else "n/a",
             f"week of {_d(mort.get('date'))}" if mort.get("date") else "Freddie Mac")]
    kpi_html = "".join(f'<div class="kpi"><div class="l">{e(l)}</div><div class="v">{e(v)}</div><div class="s">{e(sb)}</div></div>'
                       for l, v, sb in kpis)
    bars = "".join(f'<div class="pbar"><span>{e(p["name"])}<span class="w">{p["weight"]}%</span></span>'
                   f'<div class="track"><div class="fill {_tone(p["score"])}" style="width:{(p["score"] or 0) * 10:.0f}%"></div></div>'
                   f'<b>{_num(p["score"])}</b></div>' for p in s["pillars"])
    cards = ""
    for p in s["pillars"]:
        def note(i):
            return f'<small>{e(i["note"])}</small>' if i.get("note") else ""

        def tag(i):
            if not i.get("lag") or not i.get("asof"):
                return ""
            old_ = i["lag"] >= 2
            return f'<em class="stale{" old" if old_ else ""}">{_qlabel(i["asof"])} figure{", too old to score" if old_ else ""}</em>'
        rows = "".join(f'<div class="m" title="0 points at {e(fmt(i["bad"], i["fmt"]))}, 10 points at {e(fmt(i["good"], i["fmt"]))}">'
                       f'<span class="lab">{e(i["label"])}{note(i)}{tag(i)}</span>'
                       f'<span class="val">{e(fmt(i["value"], i["fmt"]))}'
                       f'<span class="pts">{_num(i["score"], " pts", "–")}</span></span></div>'
                       for i in p["metrics"])
        sc = "n/a" if p["score"] is None else f"{p['score']:.1f}"
        cards += (f'<div class="card"><div class="hd"><div><h2>{e(p["name"])}</h2><p class="sub">{e(p["question"])}</p></div>'
                  f'<div class="right">{_badge(sc, _tone(p["score"]))}<p class="sub">{p["weight"]}% weight</p></div></div>'
                  f'<div class="bd" style="padding-top:6px;padding-bottom:6px">{rows}</div></div>')
    pil = {p["name"]: p for p in s["pillars"]}
    dil_metric = next(i for i in pil["Dilution"]["metrics"] if i["key"] == "dilution_live")
    deep = (f'<div class="grid2">{_debt_card(res, pil["Balance sheet"])}{_dilution_card(res, dil_metric)}</div>'
            f'{_markets_card(res, pil["Housing market"])}')
    contracts = _contracts_svg(res.get("contracts_all") or [])
    lo4, hi4, a4 = acc.get("contracts_last4_proj_low"), acc.get("contracts_last4_proj_high"), acc.get("contracts_last4")
    vs_mid = acc.get("contracts_vs_proj")
    contracts_card = ""
    if contracts:
        contracts_card = (
            f'<div class="card"><div class="hd stack"><div><h2>Acquisition contracts vs projection</h2>'
            f'<p class="sub">Weekly contracts signed, against the range Opendoor projects for itself</p></div>'
            f'<div class="right">{_badge(f"{vs_mid * 100:+.0f}%", "green" if vs_mid >= 0 else "red", "↗ " if vs_mid >= 0 else "↘ ") if vs_mid is not None else ""}'
            f'<p class="sub">last 4 weeks vs projection midpoint</p></div></div>'
            f'<div class="bd"><div class="legend"><span><i style="background:{BLUE}"></i>Contracts signed</span>'
            f'<span><i style="background:#34d399"></i>Opendoor&#39;s projected range</span></div>'
            f'<p class="swipe">Swipe for earlier weeks, tap the chart for figures</p>'
            f'<div class="chartbox">{contracts}<div class="tip" hidden></div></div></div>'
            f'<div class="ft">Last 4 weeks: {a4:,} contracts vs a projected {lo4:,}–{hi4:,}; '
            f'{acc.get("weeks_below_proj_13", 0)} of the last 13 weeks were below the range. Source: accountable.opendoor.com, data as of {e(_d(asof))}.</div></div>'
            if a4 else "")
    n = lambda r, k, d=0: "–" if r.get(k) is None else f"{r[k]:,.{d}f}"
    qrows = "".join(f"<tr><td>{_qlabel(r['quarter_end'])}</td><td>{n(r, 'revenue_musd')}</td><td>{n(r, 'net_income_musd')}</td>"
                    f"<td>{n(r, 'adj_net_income_musd')}</td><td>{n(r, 'homes_purchased')}</td><td>{n(r, 'homes_sold')}</td>"
                    f"<td>{n(r, 'contribution_margin_pct', 1)}</td><td>{n(r, 'aged_120_pct', 0)}</td><td>{n(r, 'cash_musd')}</td></tr>"
                    for r in reversed(res["quarters"][-8:]))
    hist = res.get("history") or []
    heads = "".join(f"<th>{e(p[0])}</th>" for p in PILLARS)
    hrows = "".join(f"<tr><td>{_d(h['run_at'])}{' (v' + (h.get('method') or METHOD) + ')' if (h.get('method') or METHOD) != METHOD else ''}</td>"
                    f"<td>${float(h['price']):.2f}</td><td><b>{float(h['overall']):.2f}</b> {e(h['grade'])}</td>"
                    + "".join(f"<td>{h.get(p[0]) or '–'}</td>" for p in PILLARS) + "</tr>" for h in reversed(hist[-60:]))
    pf_note = ("Cash, notes, book value and share count include: " + "; ".join(ex["capital_events"]) + ". ") if ex.get("capital_events") else ""
    est_note = (f"{acc.get('prev_quarter')} revenue is estimated at ${ex['q3_revenue_estimate_musd']:,.0f}M "
                f"({ex['q3_revenue_estimate_musd'] / ex['q3_prior_revenue_musd'] * 100 - 100:+.1f}% YoY): homes sold × "
                f"${ex['revenue_per_home_k']:.1f}K average revenue per home, until Opendoor reports. ") if ex.get("q3_revenue_estimate_musd") and ex.get("q3_prior_revenue_musd") else ""
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opendoor Scorecard: {s['overall']:.2f}/10 ({s['grade']})</title>
<meta name="description" content="Independent, automatically updated scorecard for Opendoor ($OPEN). {e(s['verdict'])}">
<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png"><meta name="theme-color" content="#f3f2f0">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}</style></head><body>
<div class="bar"><div class="in">
<nav class="desk"><a href="#score">Score</a><a href="#pillars">Pillars</a><a href="#deep">Details</a><a href="#contracts">Contracts</a><a href="#history">History</a><a href="#method">Method</a></nav>
<a class="brand" href="./">Opendoor Scorecard<small>Independent and not affiliated with Opendoor</small></a>
<a class="by" href="https://thisisfed.xyz" target="_blank" rel="noopener">by Federico R. T. Ferrari</a>
<button class="burger" type="button" aria-label="Open menu" aria-expanded="false" aria-controls="menu"><span></span><span></span><span></span></button>
</div>
</div>
<nav class="menu" id="menu" hidden>
<a href="#score"><span>Overall score</span><small>The grade and why</small></a>
<a href="#pillars"><span>Pillars</span><small>What drives each score</small></a>
<a href="#deep"><span>Details</span><small>Debt, dilution and Opendoor&#39;s markets</small></a>
<a href="#contracts"><span>Contracts</span><small>Opendoor against its own targets</small></a>
<a href="#history"><span>History</span><small>Score over time and quarterly results</small></a>
<a href="#method"><span>Method</span><small>How the score works, and sources</small></a>
<a class="menu-by" href="https://thisisfed.xyz" target="_blank" rel="noopener">by Federico R. T. Ferrari</a>
</nav>
<div class="wrap">
<section class="hero">
<div class="asof"><span class="dot"></span>Updated {e(_d(res['generated']))}, {e(res['generated'][11:16])} UK<i>Prices to {e(_d(px['date']))}</i><i>Opendoor tracker as of {e(_d(asof))}, refreshed weekly</i></div>
<h1>Opendoor,<br>scored.<br>Every weekday.</h1>
<p class="lede">An independent scorecard that grades Opendoor 0–10 on nine pillars, rebuilt every weekday from SEC filings, the share price, housing data from its markets and Opendoor's own public tracker.</p>
</section>

<div class="card" id="score"><div class="hd stack"><div><h2>Overall score</h2><p class="sub">Weighted across nine pillars</p></div>
<div class="right">{change}<p class="sub">{e(change_sub)}</p></div></div>
<div class="bd"><div class="score"><span class="num">{s['overall']:.2f}</span><span class="of">/ 10</span>
<span class="grade badge {tone}">{e(s['grade'])}</span></div>
<p class="verdict">{e(s['verdict'])}</p><p class="why">{e(s['why'])}</p></div>
<div class="kpis">{kpi_html}</div></div>

<div class="card"><div class="hd"><div><h2>Score by pillar</h2><p class="sub">Each pillar is the average of its metrics, 0–10</p></div></div>
<div class="bd">{bars}</div></div>

<p class="section" id="pillars">What drives each score</p>
<div class="grid" style="margin-top:16px">{cards}</div>

<p class="section" id="deep">Details</p>
{deep}

<p class="section" id="contracts">Opendoor's own targets</p>
{contracts_card}

<p class="section" id="history">Over time</p>
<div class="card"><div class="hd"><div><h2>Overall score over time</h2><p class="sub">One point per weekday</p></div></div>
<div class="bd">{_history_svg(hist)}</div></div>

<div class="card"><div class="hd"><div><h2>Quarterly results</h2><p class="sub">$ millions, from Opendoor's earnings releases and SEC filings</p></div></div>
<div class="bd scroll" style="padding-top:4px"><table><tr><th>Quarter</th><th>Revenue</th><th>Net income</th><th>Adj. net income</th><th>Bought</th><th>Sold</th>
<th>Contrib. %</th><th>120+ days %</th><th>Cash</th></tr>{qrows}</table></div></div>

<div class="card"><details open><summary class="hd" style="border-bottom:0"><div><h2>Run history</h2><p class="sub">Every pillar's score on every day</p></div>
<span class="chev">▾</span></summary><div class="bd scroll" style="border-top:1px solid var(--line);padding-top:4px"><table><tr><th>Run</th><th>Price</th><th>Overall</th>{heads}</tr>{hrows}</table></div></details></div>

<footer id="method"><p><b>Method.</b> Each metric scores 0–10 on a straight line between a "0 points" and a "10 points" value (hover a metric to see them).
Pillars average their metrics, and the overall score weights the pillars as shown. Grades: A 8+, B 6.5+, C 5+, D 3.5+, F below.
Debt is split by what backs it: home-backed debt, which lenders can recover only from the homes, is measured against the homes it funds,
and the company's own convertible notes against its cash. Potential new shares count only once they are live at today's price (see Details).
A quarterly figure one quarter old is labelled; older than that, it stops counting.
The thresholds and weights are judgement calls and have not been back-tested against share-price returns. {e(est_note)}{e(pf_note)}</p>
<p><b>Sources.</b> SEC EDGAR (financial statements and earnings releases), accountable.opendoor.com, {e(str(px.get('source', 'price data')))} (price),
{e(mort.get('source') or 'Freddie Mac')} (mortgage rate), Realtor.com (housing in Opendoor's markets). Download the <a href="history.csv">score history</a> or <a href="scorecard.json">all the data</a>.</p>
<p><b>Independent project</b>, not affiliated with or endorsed by Opendoor Technologies Inc. A structured summary of public numbers, not a forecast,
a recommendation or investment advice.</p></footer>
<p class="credit"><span>An idea by Federico R. T. Ferrari (<a href="https://thisisfed.xyz" target="_blank" rel="noopener">thisisfed.xyz</a>)</span></p>
</div>
<script>
(function () {{
  var bar = document.querySelector(".bar"), btn = document.querySelector(".burger"), menu = document.getElementById("menu");
  function setBar() {{ document.documentElement.style.setProperty("--barh", bar.offsetHeight + "px"); }}
  setBar(); window.addEventListener("resize", setBar);
  function toggle(open) {{
    menu.hidden = !open; btn.setAttribute("aria-expanded", open ? "true" : "false");
    btn.setAttribute("aria-label", open ? "Close menu" : "Open menu");
    document.body.style.overflow = open ? "hidden" : "";
  }}
  btn.addEventListener("click", function () {{ toggle(menu.hidden); }});
  menu.addEventListener("click", function (ev) {{ if (ev.target.closest("a")) toggle(false); }});
  document.addEventListener("keydown", function (ev) {{ if (ev.key === "Escape" && !menu.hidden) {{ toggle(false); btn.focus(); }} }});
  window.addEventListener("resize", function () {{ if (window.innerWidth > 1180 && !menu.hidden) toggle(false); }});

}})();
(function () {{
  document.querySelectorAll(".chartbox").forEach(function (box) {{
    var svg = box.querySelector("svg.ichart"), tip = box.querySelector(".tip");
    if (!svg || !tip) return;
    var pts = JSON.parse(svg.getAttribute("data-points"));
    var W = +svg.getAttribute("data-w"), L = +svg.getAttribute("data-l"), R = +svg.getAttribute("data-r");
    var vline = svg.querySelector(".vline"), hdot = svg.querySelector(".hdot");
    var fmt = function (v) {{ return Number(v).toLocaleString("en-US"); }};
    function line(cls, text) {{ var s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }}
    function show(ev) {{
      var r = svg.getBoundingClientRect(), k = r.width / W, vx = (ev.clientX - r.left) / k;
      var i = Math.round((vx - L) / ((W - L - R) / (pts.length - 1)));
      i = Math.max(0, Math.min(pts.length - 1, i));
      var p = pts[i];
      vline.setAttribute("x1", p.x); vline.setAttribute("x2", p.x); vline.style.display = "";
      if (p.a !== null) {{ hdot.setAttribute("cx", p.x); hdot.setAttribute("cy", p.y); hdot.style.display = ""; }}
      else {{ hdot.style.display = "none"; }}
      var b = document.createElement("b"); b.textContent = p.d;
      tip.replaceChildren(b,
        p.a !== null ? line("c", "Contracts signed: " + fmt(p.a)) : line("n", "Not reported yet"));
      if (p.lo !== null) tip.appendChild(line("g", "Projected range: " + fmt(p.lo) + "\u2013" + fmt(p.hi)));
      tip.hidden = false;
      var vl = box.scrollLeft, vr = box.scrollLeft + box.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
      var left = p.x * k + 14; if (left + tw > vr - 4) left = p.x * k - tw - 14;
      left = Math.max(vl + 4, Math.min(left, vr - tw - 4));
      tip.style.left = left + "px";
      tip.style.top = Math.max(0, p.yt * k - th - 12) + "px";
    }}
    function hide() {{ tip.hidden = true; vline.style.display = "none"; hdot.style.display = "none"; }}
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", function (ev) {{ if (ev.pointerType === "mouse") hide(); }});
    box.addEventListener("scroll", hide);
    if (box.scrollWidth > box.clientWidth) box.scrollLeft = box.scrollWidth;  // phones: start at the latest weeks
  }});
}})();
(function () {{
  document.querySelectorAll(".dbox").forEach(function (box) {{
    var svg = box.querySelector("svg.dchart"), tip = box.querySelector(".tip");
    if (!svg || !tip) return;
    var pts = JSON.parse(svg.getAttribute("data-points"));
    var W = +svg.getAttribute("data-w"), L = +svg.getAttribute("data-l"), R = +svg.getAttribute("data-r");
    var XM = +svg.getAttribute("data-xmax"), shares = +svg.getAttribute("data-shares");
    var vline = svg.querySelector(".vline"), hdot = svg.querySelector(".hdot");
    function span(cls, text) {{ var s = document.createElement("span"); s.className = cls; s.textContent = text; return s; }}
    function show(ev) {{
      var r = svg.getBoundingClientRect(), k = r.width / W, price = ((ev.clientX - r.left) / k - L) / (W - L - R) * XM;
      var best = pts[0];
      pts.forEach(function (p) {{ if (Math.abs(p[0] - price) < Math.abs(best[0] - price)) best = p; }});
      var x = L + best[0] / XM * (W - L - R);
      vline.setAttribute("x1", x); vline.setAttribute("x2", x); vline.style.display = "";
      hdot.setAttribute("cx", x); hdot.setAttribute("cy", best[2]); hdot.style.display = "";
      var b = document.createElement("b"); b.textContent = "Share price $" + best[0].toFixed(2);
      tip.replaceChildren(b, span("c", (best[1] * 100).toFixed(1) + "% of shares live"),
        span("n", "about " + Math.round(best[1] * shares).toLocaleString("en-US") + "M new shares"));
      tip.hidden = false;
      var vl = box.scrollLeft, vr = box.scrollLeft + box.clientWidth, tw = tip.offsetWidth, th = tip.offsetHeight;
      var left = x * k + 14; if (left + tw > vr - 4) left = x * k - tw - 14;
      left = Math.max(vl + 4, Math.min(left, vr - tw - 4));
      tip.style.left = left + "px"; tip.style.top = Math.max(0, best[2] * k - th - 12) + "px";
    }}
    function hide() {{ tip.hidden = true; vline.style.display = "none"; hdot.style.display = "none"; }}
    svg.addEventListener("pointermove", show);
    svg.addEventListener("pointerdown", show);
    svg.addEventListener("pointerleave", function (ev) {{ if (ev.pointerType === "mouse") hide(); }});
    box.addEventListener("scroll", hide);
  }});
}})();
</script>
</body></html>"""
    with open(path, "w") as fh:
        fh.write(doc)
    with open(os.path.join(os.path.dirname(path) or ".", "favicon.svg"), "w") as fh:
        fh.write(FAVICON_SVG)


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


HIST_FIELDS = ["run_at", "price_date", "price", "tracker_as_of", "latest_quarter", "overall", "grade"] + [p[0] for p in PILLARS] + ["method"]


def update_history(res, path):
    """Append this run to history.csv and return the previous run (or None)."""
    """Keeps one row per day: a second run on the same day replaces that day's row."""
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    s = res["score"]
    row = {"run_at": res["generated"], "price_date": res["price"]["date"], "price": res["price"]["price"],
           "tracker_as_of": (res.get("accountable") or {}).get("as_of", ""), "latest_quarter": res["quarters"][-1]["quarter_end"],
           "overall": s["overall"], "grade": s["grade"], **{p["name"]: p["score"] for p in s["pillars"]}, "method": METHOD}
    if rows and rows[-1]["run_at"][:10] == row["run_at"][:10]:
        rows = rows[:-1]
    prev = rows[-1] if rows else None
    rows.append({k: str(v) for k, v in row.items()})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=HIST_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return prev, rows


def change_summary(res, prev):
    s = res["score"]
    if not prev:
        return None
    f = lambda v: None if v in (None, "", "None") else float(v)
    d = {"overall": s["overall"] - f(prev["overall"]), "grade_from": prev["grade"], "since": prev["run_at"],
         "method_changed": (prev.get("method") or METHOD) != METHOD,
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
    ap.add_argument("--releases-cache", default=os.path.join(HERE, "releases_cache.json"))
    ap.add_argument("--macro-cache", default=os.path.join(HERE, "macro_cache.json"))
    ap.add_argument("--dilution-cache", default=os.path.join(HERE, "dilution_cache.json"))
    ap.add_argument("--housing-cache", default=os.path.join(HERE, "housing_cache.json"))
    ap.add_argument("--ua", default=os.environ.get("SEC_UA", "OPEN scorecard research contact@example.com"))
    ap.add_argument("--shares", type=float, help="shares you hold")
    ap.add_argument("--avg-cost", type=float, help="your average cost per share, USD")
    ap.add_argument("--outdir", default=os.path.join(HERE, "www"), help="folder for index.html, scorecard.json, history.csv")
    ap.add_argument("--out", help=argparse.SUPPRESS)  # accepted for older commands; output goes to --outdir
    ap.add_argument("--export-rows", metavar="DIR", help="also write flat JSON row files for the dashboard")
    a = ap.parse_args()

    log, edgar, acc_raw, releases, macro, dil_cache, housing, sub = [], {}, None, {}, None, None, None, None
    px = dict(PRICE_SNAPSHOT, source="price snapshot")
    cached_px = json.load(open(a.price_cache)) if os.path.exists(a.price_cache) else None
    if a.offline and cached_px and cached_px.get("date", "") > PRICE_SNAPSHOT["date"]:
        px = dict(cached_px, source=f"last good price ({cached_px.get('source')})")
    if not a.offline:
        try:
            log.append("EDGAR tags used:")
            edgar = from_edgar(a.ua, log)
        except Exception as e:
            log.append(f"EDGAR unavailable ({e}); using csv only")
        try:
            sub = sec_submissions(a.ua)
        except Exception as e:
            log.append(f"SEC filing index unavailable ({e}); using cached filings")
        if sub:
            try:
                log.append("Earnings releases (sec.gov 8-K exhibit 99.1):")
                releases = releases_to_rows(releases_from_sec(a.ua, a.releases_cache, log, sub=sub))
            except Exception as e:
                log.append(f"earnings releases unavailable ({e}); using cached copies")
            try:
                log.append("Potential shares by type (10-Q / 10-K anti-dilutive table):")
                dil_cache = dilution_from_sec(a.ua, a.dilution_cache, log, sub=sub)
            except Exception as e:
                log.append(f"dilution table unavailable ({e}); using cached copy")
        try:
            housing = housing_markets(log)
            with open(a.housing_cache, "w") as fh:
                json.dump(housing, fh, indent=1)
        except Exception as e:
            log.append(f"housing data unavailable ({e}); using cached copy")
        try:
            macro = mortgage_rates(log)
            with open(a.macro_cache, "w") as fh:
                json.dump(macro, fh)
            log.append(f"mortgage rate {macro['rate']*100:.2f}% (week of {macro['date']})")
        except Exception as e:
            log.append(f"mortgage rates unavailable ({e}); using cached copy")
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
    if not releases and os.path.exists(a.releases_cache):
        releases = releases_to_rows(json.load(open(a.releases_cache)))
    if macro is None and os.path.exists(a.macro_cache):
        macro = json.load(open(a.macro_cache))
    if dil_cache is None and os.path.exists(a.dilution_cache):
        dil_cache = json.load(open(a.dilution_cache))
    if housing is None and os.path.exists(a.housing_cache):
        housing = json.load(open(a.housing_cache))
    dil = latest_dilution(dil_cache) if dil_cache else None
    if os.environ.get("RELEASE_DIAG"):
        kp = load_kpis(a.kpis)
        for end in sorted(releases):
            row = releases[end]
            diffs = {k: (v, kp.get(end, {}).get(k)) for k, v in row.items() if k in kp.get(end, {}) and abs(v - kp[end][k]) > 0.051}
            log.append(f"  [release diag] {end}: {row}  MISMATCH vs csv: {diffs or 'none'}")
    q = merge(edgar, load_kpis(a.kpis), releases)
    if len(q) < 2:
        sys.exit("Not enough quarterly data. Check network access or opendoor_kpis.csv.")
    m, extra = metrics(q, px, acc, macro, dil, housing)
    try:
        from zoneinfo import ZoneInfo
        now = dt.datetime.now(ZoneInfo("Europe/London")).replace(tzinfo=None)
    except Exception:
        now = dt.datetime.now()
    res = {"generated": now.isoformat(timespec="minutes"), "ticker": TICKER, "price": px,
           "extra": extra, "metrics": m, "score": score(m, extra), "quarters": q, "accountable": acc, "log": log,
           "contracts_all": (acc_raw or {}).get("contracts")}
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
        if ch.get("method_changed"):
            line += f" [scoring method updated to v{METHOD}; not comparable with earlier runs]"
        print(line)
        if ch.get("method_changed"):
            pass
        elif abs(ch["overall"]) >= 0.05 or ch["grade_from"] != res["score"]["grade"] or os.environ.get("NTFY_ALWAYS"):
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
