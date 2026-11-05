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
import argparse, csv, datetime as dt, html, json, os, re, sys, urllib.parse, urllib.request

TICKER, CIK = "OPEN", "0001801169"
METHOD = "2"  # bump when metrics, thresholds or weights change, so the history marks the break
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

# Management's stated targets and quarterly guidance, as published in Opendoor's earnings releases (8-K exh. 99.1).
TARGETS = [
    {"what": "Adjusted net income positive by the end of 2026, measured on a twelve-month go-forward basis",
     "key": "adj_net_income_musd", "label": "Adj. net income", "by": "2026-12-31",
     "said": "Q4 2025 release (Feb 19, 2026); repeated in the Q2 2026 release (Aug 4, 2026)"},
    {"what": "Adjusted EBITDA profitable on a twelve-month go-forward basis from Q2 2026",
     "key": "adj_ebitda_musd", "label": "Adj. EBITDA", "by": "2026-06-30",
     "said": "Q1 2026 release (May 7, 2026); repeated in the Q2 2026 release (Aug 4, 2026)"},
]
# Quarterly guidance as Opendoor worded it, newest last. Each item: (wording, what to check, kind, target, better)
# kind: min (at least), max (at most), range (lo, hi), approx (target, tolerance), abs_max (within +/- of zero).
# better: +1 when a higher figure is better (so landing above counts as "beat"), -1 when lower is better, 0 when only
# accuracy matters. Guidance that can't be checked from reported quarterly figures (a monthly exit rate, say) is left out.
GUIDANCE = [
    {"quarter": "2025-09-30", "said": "Q2 2025 release (Aug 5, 2025)", "items": [
        ("Revenue of $800M to $875M", "revenue_musd", "range", (800.0, 875.0), 1),
        ("Contribution profit of $22M to $29M", "contribution_profit_musd", "range", (22.0, 29.0), 1),
        ("Adj. EBITDA of -$28M to -$21M", "adj_ebitda_musd", "range", (-28.0, -21.0), 1)]},
    {"quarter": "2025-12-31", "said": "Q3 2025 release (Nov 6, 2025)", "items": [
        ("Homes purchased up at least 35% on the quarter", "homes_purchased_qoq", "min", 0.35, 1),
        ("Revenue down about 35% on the quarter", "revenue_qoq", "approx", (-0.35, 0.02), 1),
        ("Contribution margin below Q3 2025's 2.2%", "contribution_margin", "max", 2.2, 0),
        ("Adj. EBITDA loss in the high $40Ms to mid $50Ms", "adj_ebitda_musd", "range", (-56.0, -46.0), 1)]},
    {"quarter": "2026-03-31", "said": "Q4 2025 release (Feb 19, 2026)", "items": [
        ("Revenue down about 10% on the quarter", "revenue_qoq", "approx", (-0.10, 0.02), 1),
        ("Adj. EBITDA loss in the low to mid $30Ms", "adj_ebitda_musd", "range", (-36.0, -30.0), 1)]},
    {"quarter": "2026-06-30", "said": "Q1 2026 release (May 7, 2026)", "items": [
        ("Revenue up about 25% on the quarter before", "revenue_qoq", "approx", (0.25, 0.02), 1),
        ("Contribution margin in the middle of the 5% to 7% range", "contribution_margin", "range", (5.5, 6.5), 1),
        ("Adj. EBITDA breakeven, plus or minus a few million", "adj_ebitda_musd", "abs_max", 5.0, 1)]},
    {"quarter": "2026-09-30", "said": "Q2 2026 release (Aug 4, 2026)", "items": [
        ("Revenue up at least 20% year on year", "revenue_yoy", "min", 0.20, 1),
        ("Contribution margin around 4% to 4.5%", "contribution_margin", "range", (4.0, 4.5), 1),
        ("Contribution profit more than double year on year", "contribution_profit_mult", "min", 2.0, 1),
        ("Stock-based compensation about $110M", "sbc_musd", "approx", (110.0, 10.0), -1)]},
]
# Homes under contract to purchase at quarter end, as printed in Opendoor's releases (current and year-earlier
# columns). Newer releases are parsed automatically and take priority; these cover quarters before the parser.
UNDER_CONTRACT_SEED = {"2024-09-30": 1006, "2024-12-31": 1705, "2025-03-31": 1051, "2025-06-30": 393,
                       "2025-09-30": 526, "2025-12-31": 710, "2026-03-31": 1939, "2026-06-30": 2310}
NEXT_RESULTS = {"date": "2026-11-05", "what": "Q3 2026 results, after the US close",  # Opendoor press release, Oct 19, 2026
                "quarter": "2026-09-30"}
# Which scored metrics a quarterly report changes, and when. The earnings release (8-K) is parsed the first evening
# it is on the SEC site; the balance sheet, share count and stock pay come from the 10-Q, often filed days later.
FROM_RELEASE = ["rev_nowcast_yoy", "contrib_margin", "contrib_trend", "gross_margin", "aged_inventory", "days_inventory",
                "net_margin_2q", "adj_ebitda_m", "ani_margin", "fallout", "ps"]
FROM_10Q = ["cash_vs_notes", "home_debt_ltv", "runway_q", "share_yoy", "sbc_pct", "dilution_live", "pb"]
# Scenario presets for the quarter about to be reported: hypothetical inputs set around Opendoor's own guidance
# (Q2 2026 release: revenue up at least 20% year on year, contribution margin around 4% to 4.5%). Not forecasts.
# They disappear once that quarter is in the score.
SCENARIOS = {"quarter": "2026-09-30", "presets": [
    {"name": "Bull Q3", "note": "Revenue +25% YoY, five points above the guided floor; contribution margin 4.5%, the top of the guided range",
     "values": {"rev_nowcast_yoy": 0.25, "contrib_margin": 0.045}},
    {"name": "Bear Q3", "note": "Revenue +12% YoY, this page's own estimate (a guidance miss); contribution margin 3.5%, below the guided range",
     "values": {"rev_nowcast_yoy": 0.12, "contrib_margin": 0.035}},
]}
# Changes to what is scored or how. The score history marks each break (method version in history.csv).
METHOD_CHANGES = [
    ("2", "2026-11-04", "Added three metrics: contribution margin vs the quarter before and days of inventory (Unit economics), "
                        "and signed contracts that fell through (Execution). Scores from before this date are not directly comparable."),
]

# ---------------------------------------------------------------- scoring rules
# metric: (label, value scoring 0, value scoring 10, format). Linear in between, clipped.
RULES = {
    "rev_nowcast_yoy": ("Revenue YoY, latest quarter",                              -0.40, 0.30, "pct"),
    "coe_q4_yoy":      ("Quarter-to-date homes sold YoY",          -0.20, 0.50, "pct"),
    "contracts_qoq":   ("Acquisition contracts, last qtr vs prior",    -0.20, 0.20, "pct"),
    "contrib_margin":  ("Contribution margin",                 0.00, 0.07, "pct"),
    "contrib_trend":   ("Contribution margin, latest qtr vs the one before", -0.015, 0.015, "pp"),
    "days_inventory":  ("Days of inventory (average inventory / cost of homes sold)", 200.0, 90.0, "days"),
    "gross_margin":    ("Gross margin",                        0.05, 0.12, "pct"),
    "aged_inventory":  ("Homes listed 120+ days, share of inventory", 0.40, 0.05, "share"),
    "net_margin_2q":   ("GAAP net margin, last 2 qtrs",       -0.20, 0.02, "pct"),
    "adj_ebitda_m":    ("Adj. EBITDA margin",                 -0.05, 0.03, "pct"),
    "ani_margin":      ("Adj. net income margin (target: positive by end-2026)", -0.10, 0.01, "pct"),
    "coe_vs_guide":    ("Last qtr homes sold vs mgmt expectation",  -0.15, 0.05, "pct"),
    "contracts_vs_proj": ("Contracts, last 4 wks vs projection midpoint", -0.40, 0.10, "pct"),
    "clearance":       ("Relative clearance vs MLS, 4-wk avg", 0.90, 1.30, "x"),
    "fallout":         ("Signed purchase contracts that fell through, last qtr (est.)", 0.35, 0.10, "share"),
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
    ("Unit economics", 15, ["contrib_margin", "contrib_trend", "gross_margin", "aged_inventory", "days_inventory"],
                                                                             "Does each home flip make money, and is stock fresh?"),
    ("Profitability",  15, ["net_margin_2q", "adj_ebitda_m", "ani_margin"],  "Does the whole company make money?"),
    ("Execution",      15, ["coe_vs_guide", "contracts_vs_proj", "clearance", "fallout"],
                                                                             "Is management hitting its own targets?"),
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


def _qend(d):
    """Calendar quarter end for a date."""
    m = 3 * ((d.month - 1) // 3) + 3
    return (dt.date(d.year + (m == 12), 1 if m == 12 else m + 1, 1) - dt.timedelta(days=1)).isoformat()


def contract_quarters(rows):
    """Acquisition contracts signed per complete calendar quarter, by the week-ending date. Uses the tracker's
    own weeks, and its 'a year earlier' column (shifted back 52 weeks) for the quarters before that."""
    wk = {}
    for d, a, lo, hi, py in rows:
        if a is not None:
            wk[dt.date.fromisoformat(d)] = (a, "tracker")
    last = max(wk) if wk else None
    for d, a, lo, hi, py in rows:
        if py:
            wk.setdefault(dt.date.fromisoformat(d) - dt.timedelta(days=364), (py, "tracker, year-earlier column"))
    out = {}
    for day, (v, src) in wk.items():
        q = out.setdefault(_qend(day), {"signed": 0, "weeks": 0, "sources": set()})
        q["signed"] += v
        q["weeks"] += 1
        q["sources"].add(src)
    return {qe: {"signed": q["signed"], "weeks": q["weeks"], "source": " + ".join(sorted(q["sources"]))}
            for qe, q in out.items() if q["weeks"] >= 13 and last and qe <= last.isoformat()}


def merge_contract_quarters(new, path):
    """Keep finished quarters once the tracker's window moves on; the tracker's own weeks beat the year-earlier column."""
    cache = json.load(open(path)) if path and os.path.exists(path) else {}
    for qe, v in new.items():
        old = cache.get(qe)
        if not old or "year-earlier" in old["source"] or "year-earlier" not in v["source"]:
            cache[qe] = v
    if path:
        with open(path, "w") as fh:
            json.dump(dict(sorted(cache.items())), fh, indent=1)
    return cache


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
        "quarters": contract_quarters(acc["contracts"]),
        # for sparklines: contracts in each rolling 4 weeks vs Opendoor's projected midpoint, last 13 weeks
        "contracts_vs_proj_series": [
            (c[j][0], sum(r[1] for r in c[j - 3:j + 1]) / ((sum(r[2] for r in c[j - 3:j + 1]) + sum(r[3] for r in c[j - 3:j + 1])) / 2) - 1)
            for j in range(max(3, len(c) - 13), len(c))],
        # and homes sold so far this quarter vs the same point a year earlier, week by week
        "coe_yoy_series": [(d, v / q4py[d] - 1) for d, v in q4 if q4py.get(d)],
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
    ("homes_under_contract", r"homes under contract to purchase( \(at period end\))?"),
    # contribution profit reconciliation: costs on homes sold in the quarter
    ("direct_selling_musd", r"direct selling costs.*"),
    ("holding_cur_musd", r"holding costs on sales\W+current period.*"),
    ("holding_prior_musd", r"holding costs on sales\W+prior periods?.*"),
    ("writedown_cur_musd", r"inventory valuation adjustment\W+current period.*"),
]
SECOND_NUMBER = {"homes_under_contract": "homes_under_contract_py"}  # rows whose year-earlier column is also kept
_NUM = re.compile(r"\(\s*\$?\s*[\d,]+(?:\.\d+)?\s*\)|-?\$?\s*[\d,]*\d(?:\.\d+)?")


def _numbers(cells):
    text = " | ".join(cells[1:])
    text = re.sub(r"\(\s*\|\s*", "(", re.sub(r"\s*\|\s*\)", ")", text)).replace("$", "")
    out = []
    for m in _NUM.finditer(text):
        tok = m.group(0).replace(" ", "").replace(",", "")
        neg = tok.startswith("(")
        tok = tok.strip("()")
        try:
            v = float(tok)
        except ValueError:
            continue
        out.append(-v if neg else v)
    return out


def _first_number(cells):
    n = _numbers(cells)
    return n[0] if n else None


RELEASE_PARSER = 3  # bump to re-read cached releases with a newer parser
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
        label = re.sub(r"\s+", " ", cells[0]).strip().rstrip(":")
        label = re.sub(r"(\s*\(\d+\))+$|\s*\d$", "", label).strip().lower()
        label = label.replace("\u2019", "'")
        for col, pat in RELEASE_ROWS:
            if col not in out and re.fullmatch(pat, label):
                nums = _numbers(cells)
                if nums:
                    out[col] = nums[0]
                    if col in SECOND_NUMBER and len(nums) > 1:
                        out[SECOND_NUMBER[col]] = nums[1]
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
def short_interest(log=None):
    """Latest short interest and days to cover (FINRA figures, as Nasdaq publishes them twice a month), plus the
    float from Yahoo when it answers; without a float the page uses shares outstanding and says so."""
    req = urllib.request.Request(
        f"https://api.nasdaq.com/api/quote/{TICKER}/short-interest?assetClass=stocks",
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9",
                 "Origin": "https://www.nasdaq.com", "Referer": "https://www.nasdaq.com/", "Accept-Encoding": "identity"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        j = json.loads(resp.read().decode("utf-8"))
    rows = ((j.get("data") or {}).get("shortInterestTable") or {}).get("rows") or []
    num = lambda v: float(str(v).replace(",", "").strip()) if v not in (None, "", "N/A") else None
    parsed = []
    for r in rows:
        m, d, y = r["settlementDate"].split("/")
        parsed.append({"settlement": f"{y}-{int(m):02d}-{int(d):02d}", "shares_short": num(r.get("interest")),
                       "avg_volume": num(r.get("avgDailyShareVolume")), "days_to_cover": num(r.get("daysToCover"))})
    if not parsed:
        raise ValueError("no short-interest rows")
    parsed.sort(key=lambda r: r["settlement"])
    out = dict(parsed[-1], previous=parsed[-2] if len(parsed) > 1 else None, source="Nasdaq (FINRA short interest)")
    try:
        import http.cookiejar
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        op.addheaders = [("User-Agent", BROWSER_UA)]
        try:
            op.open("https://fc.yahoo.com", timeout=15)
        except Exception:
            pass  # only the cookie it sets matters
        crumb = op.open("https://query1.finance.yahoo.com/v1/test/getcrumb", timeout=15).read().decode().strip()
        js = json.loads(op.open(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{TICKER}"
                                f"?modules=defaultKeyStatistics&crumb={urllib.parse.quote(crumb)}", timeout=15).read().decode())
        fl = js["quoteSummary"]["result"][0]["defaultKeyStatistics"]["floatShares"]["raw"]
        out.update(float_shares=float(fl), float_source="Yahoo Finance")
    except Exception as e:
        if log is not None:
            log.append(f"float unavailable ({e}); short interest shown against shares outstanding")
    return out


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
    # contribution margin, latest quarter vs the one before
    cmr = [r for r in q if r.get("contribution_margin_pct") is not None][-2:]
    contrib_trend = None
    if len(cmr) == 2 and 80 <= _days(cmr[0]["quarter_end"], cmr[1]["quarter_end"]) <= 100:
        contrib_trend = (cmr[1]["contribution_margin_pct"] - cmr[0]["contribution_margin_pct"]) / 100
    # inventory velocity: average inventory over the quarter / cost of the homes sold in it
    days_inv, inv_detail = None, None
    r1 = latest_with(q, "inventory_musd", "revenue_musd", "gross_profit_musd")
    if r1:
        r0 = next((r for r in reversed(q) if r.get("inventory_musd") is not None
                   and 80 <= _days(r["quarter_end"], r1["quarter_end"]) <= 100), None)
        cogs = r1["revenue_musd"] - r1["gross_profit_musd"]
        if r0 and cogs > 0:
            avg = (r0["inventory_musd"] + r1["inventory_musd"]) / 2
            days_inv = avg / cogs * _days(r0["quarter_end"], r1["quarter_end"])
            inv_detail = {"quarter": r1["quarter_end"], "avg_inventory": avg, "cogs": cogs, "turns": 365 / days_inv,
                          "growth": r1["inventory_musd"] / r0["inventory_musd"] - 1}
    # the revenue estimate, laid open: homes sold x an assumed revenue per home
    nowcast = None
    if q3_rev_est and q3_prior:
        pe = acc["prev_quarter_end"]
        prior_row = next((r for r in q if r["quarter_end"] == str(int(pe[:4]) - 1) + pe[4:]), {})
        ph = prior_row.get("homes_sold")
        guide = next((it[3] for gd in GUIDANCE if gd["quarter"] == pe for it in gd["items"] if it[1] == "revenue_yoy"), None)
        nowcast = {"quarter": pe, "homes": acc["q3_homes_sold"], "rph_k": rph * 1000, "estimate": q3_rev_est,
                   "prior_revenue": q3_prior, "prior_homes": ph,
                   "homes_yoy": acc["q3_homes_sold"] / ph - 1 if ph else None,
                   "prior_rph_k": q3_prior / ph * 1000 if ph else None,
                   "rph_change": rph / (q3_prior / ph) - 1 if ph else None,
                   "rph_basis": " and ".join(_qlabel(r["quarter_end"]) for r in hs),
                   "guide_min_yoy": guide, "guide_min_revenue": q3_prior * (1 + guide) if guide is not None else None}
    m = {
        "rev_nowcast_yoy": ch(q3_rev_est, q3_prior) if unreported else ch(g(-1, "revenue_musd"), g(-5, "revenue_musd")),
        "coe_q4_yoy": acc.get("coe_q4_yoy") if acc else None,
        "contracts_qoq": acc.get("contracts_qoq") if acc else None,
        "contrib_margin": div(rows["contribution_margin_pct"].get("contribution_margin_pct"), 100),
        "contrib_trend": contrib_trend,
        "days_inventory": days_inv,
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
            "home_debt_ltv": bs.get("quarter_end"), "dilution_live": dq,
            "contrib_trend": cmr[-1]["quarter_end"] if cmr else None, "days_inventory": (inv_detail or {}).get("quarter")}
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
    if contrib_trend is not None:
        notes["contrib_trend"] = (f"{_qlabel(cmr[0]['quarter_end'])} {cmr[0]['contribution_margin_pct']:.1f}%, "
                                  f"{_qlabel(cmr[1]['quarter_end'])} {cmr[1]['contribution_margin_pct']:.1f}%")
    if inv_detail:
        notes["days_inventory"] = (f"About {inv_detail['turns']:.1f} turns a year"
                                   + (f"; inventory grew {inv_detail['growth'] * 100:.0f}% in the quarter, which lengthens this"
                                      if inv_detail["growth"] > 0.25 else ""))
    if nowcast:
        notes["rev_nowcast_yoy"] = (f"Estimate: {nowcast['homes']:,.0f} homes x ${nowcast['rph_k']:.0f}K a home. Homes "
                                    f"{nowcast['homes_yoy'] * 100:+.1f}% YoY; assumes revenue per home "
                                    f"{nowcast['rph_change'] * 100:+.1f}%"
                                    + (f". Opendoor guided at least {nowcast['guide_min_yoy'] * 100:+.0f}%" if nowcast["guide_min_yoy"] is not None else ""))
    if acc and acc.get("q4_homes_sold_qtd") and acc.get("coe_q4_yoy") is not None:
        notes["coe_q4_yoy"] = (f"{acc.get('curr_quarter')} to {_d(acc.get('q4_qtd_date'))}: {acc['q4_homes_sold_qtd']:,.0f} homes vs "
                               f"{acc['q4_homes_sold_qtd'] / (1 + acc['coe_q4_yoy']):,.0f} a year earlier")

    # contracts that fell through, from an identity on Opendoor's own figures:
    # open at the start of the quarter + signed in it - homes purchased - open at the end = cancelled or lapsed
    uc = dict(UNDER_CONTRACT_SEED)
    for r in q:
        if r.get("homes_under_contract_py") is not None:
            uc[str(int(r["quarter_end"][:4]) - 1) + r["quarter_end"][4:]] = r["homes_under_contract_py"]
    for r in q:
        if r.get("homes_under_contract") is not None:
            uc[r["quarter_end"]] = r["homes_under_contract"]
    cq = (acc or {}).get("quarters") or {}
    fallout_hist = []
    for r in q:
        qe, pq = r["quarter_end"], _prev_qend(r["quarter_end"])
        if r.get("homes_purchased") and pq in uc and qe in uc and qe in cq:
            opened = uc[pq] + cq[qe]["signed"]
            lost = opened - r["homes_purchased"] - uc[qe]
            fallout_hist.append({"quarter": qe, "open_start": uc[pq], "signed": cq[qe]["signed"], "weeks": cq[qe]["weeks"],
                                 "purchased": r["homes_purchased"], "open_end": uc[qe], "lost": lost,
                                 "rate": max(0.0, lost / opened) if opened else None, "source": cq[qe]["source"]})
    fallout_hist = [f for f in fallout_hist if f["rate"] is not None and f["lost"] > -0.02 * (f["open_start"] + f["signed"])]
    if fallout_hist:
        f = fallout_hist[-1]
        m["fallout"] = f["rate"]
        asof["fallout"] = f["quarter"]
        notes["fallout"] = (f"{f['lost']:,.0f} of {f['open_start'] + f['signed']:,.0f} contracts ({f['open_start']:,.0f} open at the "
                            f"start + {f['signed']:,.0f} signed)"
                            + (f"; {_qlabel(fallout_hist[-2]['quarter'])} {fallout_hist[-2]['rate'] * 100:.0f}%" if len(fallout_hist) > 1 else ""))

    # unit economics quarter by quarter
    ue_hist = []
    for i, r in enumerate(q):
        if r.get("contribution_margin_pct") is None or not r.get("revenue_musd"):
            continue
        rv, hs_, gp = r["revenue_musd"], r.get("homes_sold"), r.get("gross_profit_musd")
        prev = q[i - 1] if i and 80 <= _days(q[i - 1]["quarter_end"], r["quarter_end"]) <= 100 else None
        cogs = rv - gp if gp is not None else None
        d_inv = None
        if prev and prev.get("inventory_musd") and r.get("inventory_musd") and cogs and cogs > 0:
            d_inv = (prev["inventory_musd"] + r["inventory_musd"]) / 2 / cogs * _days(prev["quarter_end"], r["quarter_end"])
        holding = r.get("holding_musd")
        if holding is None and r.get("holding_cur_musd") is not None:
            holding = abs(r["holding_cur_musd"]) + abs(r.get("holding_prior_musd") or 0)
        wd = r.get("writedown_cur_musd")
        per = lambda v: abs(v) / hs_ * 1000 if v is not None and hs_ else None
        ue_hist.append({"quarter": r["quarter_end"], "cm": r["contribution_margin_pct"], "gm": gp / rv * 100 if gp is not None else None,
                        "homes_sold": hs_, "revenue_per_home_k": rv / hs_ * 1000 if hs_ else None,
                        "cp_per_home_k": rv * r["contribution_margin_pct"] / 100 / hs_ * 1000 if hs_ else None,
                        "days_inv": d_inv, "aged": r.get("aged_120_pct"), "selling_k": per(r.get("direct_selling_musd")),
                        "holding_k": per(holding), "writedown_k": per(wd)})
    ue_hist = ue_hist[-8:]

    # stress tests: which inputs each one moves, and to what
    stress = {}
    if m["mortgage_rate"] is not None:
        ch_ = [("mortgage_rate", m["mortgage_rate"] + 0.005)]
        if m["mortgage_13w"] is not None:
            ch_.append(("mortgage_13w", m["mortgage_13w"] + 0.005))
        stress["r"] = {"name": "Mortgage rates 0.5 points higher", "changes": ch_,
                       "note": "Moves the mortgage rate and its 13-week change."}
    ch_ = []
    if home_ltv is not None:
        ch_.append(("home_debt_ltv", home_ltv / 0.95))
    if m["list_price_yoy"] is not None:
        ch_.append(("list_price_yoy", m["list_price_yoy"] - 0.05))
    inv_now = bs.get("inventory_musd")
    if mcap and pf.get("equity_musd") and inv_now:
        book = pf["equity_musd"] - 0.05 * inv_now
        ch_.append(("pb", mcap / book if book > 0 else 99.0))
    if ch_:
        stress["h"] = {"name": "Home prices 5% lower", "changes": ch_,
                       "note": ("Homes are marked 5% lower against the debt on them and against book value, and asking prices "
                                "fall 5 points. Margins are left as reported: how much reaches them depends on how fast "
                                "Opendoor reprices, which this page can't know.")}
    ch_ = [(k, (1 + m[k]) * 0.8 - 1) for k in ("contracts_vs_proj", "contracts_qoq") if m.get(k) is not None]
    if ch_:
        stress["c"] = {"name": "Contracts another 20% short", "changes": ch_,
                       "note": "Contracts signed 20% below today's level, against the projection and the quarter before."}

    # how old each metric's data is; flagged when more than a quarter old
    last_qe = _quarter_end_before(today)
    def qf(qe, adjusted=False):
        return {"text": _qlabel(qe) + (", adjusted for later deals" if adjusted else ""), "date": qe, "old": qe < last_qe} if qe else None
    def df(d, label, max_days):
        return {"text": f"{label} {_d(d)}", "date": str(d)[:10], "old": _days(str(d)[:10], today) > max_days} if d else None
    fresh = {}
    for k in ("contrib_margin", "contrib_trend", "gross_margin", "aged_inventory", "days_inventory", "adj_ebitda_m",
              "ani_margin", "sbc_pct", "home_debt_ltv", "fallout"):
        fresh[k] = qf(asof.get(k))
    fresh["net_margin_2q"] = qf(last["quarter_end"])
    fresh["rev_nowcast_yoy"] = ({"text": f"{_qlabel(acc['prev_quarter_end'])} estimate", "date": acc["prev_quarter_end"], "old": False}
                                if unreported and q3_rev_est else qf(last["quarter_end"]))
    for k in ("cash_vs_notes", "runway_q"):
        fresh[k] = qf(bs.get("quarter_end"), adjusted=bool(applied))
    sh_row = latest_with(q, "shares_m")
    fresh["share_yoy"] = qf(sh_row.get("quarter_end"), adjusted=any("shares_m" in e for e in applied))
    fresh["dilution_live"] = qf(dq, adjusted=bool(dilution and any(l["label"].startswith("0%") for l in dilution["lines"])))
    for k in ("coe_q4_yoy", "contracts_qoq", "coe_vs_guide", "contracts_vs_proj", "clearance"):
        fresh[k] = df(acc.get("as_of"), "Tracker", 14) if acc else None
    for k in ("ret_1y", "range_pos", "vs_ma200"):
        fresh[k] = df(px["date"], "Close", 5)
    fresh["ps"] = {"text": f"Price {_d(px['date'])}, sales to {_qlabel(last['quarter_end'])}", "date": last["quarter_end"],
                   "old": last["quarter_end"] < last_qe}
    fresh["pb"] = {"text": f"Price {_d(px['date'])}, book at {_qlabel(bs.get('quarter_end'))}" if bs.get("quarter_end") else "",
                   "date": bs.get("quarter_end"), "old": bool(bs.get("quarter_end")) and bs["quarter_end"] < last_qe}
    for k in ("mortgage_rate", "mortgage_13w"):
        fresh[k] = df((macro or {}).get("date"), "Week of", 14)
    if housing:
        hm = housing["month"] + "-01"
        for k in ("dom_yoy", "cuts_yoy", "list_price_yoy"):
            fresh[k] = {"text": _month(housing["month"]), "date": hm, "old": _days(hm, today) > 95}

    # management's targets: where the reported numbers stand and the pace of the last quarter
    targets = []
    for t in TARGETS:
        rows_t = [r for r in q if r.get(t["key"]) is not None][-4:]
        if not rows_t:
            continue
        lastv = rows_t[-1][t["key"]]
        step = lastv - rows_t[-2][t["key"]] if len(rows_t) >= 2 else None
        qs_left = max(0, _quarters_between(rows_t[-1]["quarter_end"], t["by"])) if t["by"] > rows_t[-1]["quarter_end"] else 0
        proj = lastv + step * qs_left if step is not None and qs_left else None
        targets.append(dict(t, history=[[r["quarter_end"], r[t["key"]]] for r in rows_t], latest=lastv, step=step,
                            quarters_left=qs_left, projection=proj))
    # quarterly guidance vs what was reported
    guidance = []
    for gd in GUIDANCE:
        row = next((r for r in q if r["quarter_end"] == gd["quarter"]), None)
        prev_q = next((r for r in q if 80 <= _days(r["quarter_end"], gd["quarter"]) <= 100), None)
        prev_y = next((r for r in q if abs(_days(r["quarter_end"], gd["quarter"]) - 365) <= 10), None)
        items = []
        for what, key, kind, target, better in gd["items"]:
            actual = None
            if row and row.get("revenue_musd") is not None:
                rv = row["revenue_musd"]
                if key == "revenue_qoq" and prev_q:
                    actual = rv / prev_q["revenue_musd"] - 1
                elif key == "revenue_yoy" and prev_y:
                    actual = rv / prev_y["revenue_musd"] - 1
                elif key == "homes_purchased_qoq" and prev_q and prev_q.get("homes_purchased") and row.get("homes_purchased"):
                    actual = row["homes_purchased"] / prev_q["homes_purchased"] - 1
                elif key == "contribution_margin":
                    actual = row.get("contribution_margin_pct")
                elif key == "contribution_profit_musd" and row.get("contribution_margin_pct") is not None:
                    actual = rv * row["contribution_margin_pct"] / 100
                elif key == "contribution_profit_mult" and prev_y and row.get("contribution_margin_pct") is not None \
                        and prev_y.get("contribution_margin_pct"):
                    actual = (rv * row["contribution_margin_pct"]) / (prev_y["revenue_musd"] * prev_y["contribution_margin_pct"])
                elif key in ("revenue_musd", "adj_ebitda_musd", "sbc_musd"):
                    actual = row.get(key)
            items.append({"what": what, "key": key, "kind": kind, "target": target, "better": better, "actual": actual,
                          "status": _guide_status(actual, kind, target, better, key)})
        guidance.append({"quarter": gd["quarter"], "said": gd["said"], "items": items})
    done = [g_ for g_ in guidance if any(i["status"] != "pending" for i in g_["items"])][-4:]
    tally = {k: sum(1 for g_ in done for i in g_["items"] if i["status"] == k) for k in ("met", "beat", "close", "missed")}
    hit_rate = dict(tally, quarters=[g_["quarter"] for g_ in done], items=sum(tally.values())) if done else None
    # analyst view: (1) how the revenue-estimate method would have done on past quarters, using reported homes sold
    backtest = []
    for i in range(2, len(q)):
        a, b, c = q[i - 2], q[i - 1], q[i]
        if all(r.get("homes_sold") and r.get("revenue_musd") for r in (a, b, c)) and \
                80 <= _days(a["quarter_end"], b["quarter_end"]) <= 100 and 80 <= _days(b["quarter_end"], c["quarter_end"]) <= 100:
            rph_ = (a["revenue_musd"] + b["revenue_musd"]) / (a["homes_sold"] + b["homes_sold"])
            est = c["homes_sold"] * rph_
            backtest.append({"quarter": c["quarter_end"], "homes": c["homes_sold"], "rph_k": rph_ * 1000, "estimate": est,
                             "reported": c["revenue_musd"], "error": est / c["revenue_musd"] - 1})
    # (2) per-share figures from the pro-forma balance sheet, on today's shares and with potential shares live at today's price
    inv_ps = bs.get("inventory_musd")
    per_share = None
    if shares and pf.get("cash_musd") is not None:
        diluted = shares + (live or 0)
        lines_ = [("Cash", pf["cash_musd"]), ("Book value (equity)", pf.get("equity_musd")),
                  ("Equity in homes (homes less the debt secured on them)",
                   inv_ps - pf["home_debt_musd"] if inv_ps is not None and pf.get("home_debt_musd") is not None else None),
                  ("Convertible notes (the company's own debt)", -pf["convert_debt_musd"] if pf.get("convert_debt_musd") is not None else None),
                  ("Cash less convertible notes", pf["cash_musd"] - pf["convert_debt_musd"] if pf.get("convert_debt_musd") is not None else None)]
        per_share = {"shares_m": shares, "diluted_m": diluted, "price": px["price"], "as_of": pf.get("as_of"),
                     "rows": [{"label": l, "musd": v, "basic": v / shares if v is not None else None,
                               "diluted": v / diluted if v is not None else None} for l, v in lines_]}
    # (3) calendar-year revenue, for scale next to what the price implies
    years = {}
    for r in q:
        years.setdefault(r["quarter_end"][:4], []).append(r["revenue_musd"])
    year_rev = {y: sum(v) for y, v in years.items() if len(v) == 4}
    analyst = {"backtest": backtest, "per_share": per_share, "year_revenue": year_rev, "market_cap_musd": mcap,
               "ttm_revenue_musd": ttm_rev, "price": px["price"], "price_date": px["date"]}

    extra = {"analyst": analyst, "next_results": NEXT_RESULTS, "fallout": fallout_hist, "ue_history": ue_hist, "stress": stress, "hit_rate": hit_rate,
             "market_cap_musd": mcap, "ttm_revenue_musd": ttm_rev, "shares_m": shares, "pro_forma": pf,
             "capital_events": [e["what"] for e in applied], "q3_revenue_estimate_musd": q3_rev_est,
             "revenue_per_home_k": rph * 1000 if rph else None, "q3_prior_revenue_musd": q3_prior,
             "shares_year_ago": (year_ago or {}).get("shares_m"), "shares_year_ago_date": (year_ago or {}).get("quarter_end"),
             "reported_rev_yoy": ch(g(-1, "revenue_musd"), g(-5, "revenue_musd")),
             "adj_net_income_musd": ani, "adj_net_income_quarter": ani_row.get("quarter_end"),
             "balance_sheet": {k: bs.get(k) for k in ("quarter_end", "cash_musd", "home_debt_musd", "convert_debt_musd",
                                                      "total_debt_musd", "inventory_musd", "equity_musd")},
             "dilution": dilution, "housing": housing, "asof": asof, "notes": notes, "latest_quarter": last["quarter_end"],
             "fresh": fresh, "nowcast": nowcast, "inventory_velocity": inv_detail, "targets": targets, "guidance": guidance,
             "mortgage": macro}
    return m, extra


def _prev_qend(qe):
    d = dt.date.fromisoformat(qe)
    first = dt.date(d.year, d.month - 2, 1)
    return (first - dt.timedelta(days=1)).isoformat()


def _guide_status(a, kind, target, better, key=""):
    """met / beat / close / missed / pending for one guided item. 'beat' needs a direction: landing on the better side
    of the guidance by more than its tolerance. Rounding slack: 0.05 points for margins, $0.5M for dollar figures."""
    if a is None:
        return "pending"
    eps = 0.05 if "margin" in key else (0.0 if key.endswith(("_qoq", "_yoy", "_mult")) else 0.5)
    if kind == "min":
        return "met" if a >= target - eps else ("close" if a >= target * 0.9 else "missed")
    if kind == "max":
        return "met" if a <= target + eps else "missed"
    if kind == "range":
        lo, hi = target
        if lo - eps <= a <= hi + eps:
            return "met"
        if (better > 0 and a > hi) or (better < 0 and a < lo):
            return "beat"
        band = (hi - lo) / 2
        return "close" if lo - band <= a <= hi + band else "missed"
    if kind == "approx":
        t, tol = target
        if abs(a - t) <= tol + eps:
            return "met"
        if better and (a - t) * better > 0:
            return "beat"
        return "close" if abs(a - t) <= 2 * tol else "missed"
    if kind == "abs_max":
        if abs(a) <= target + eps:
            return "met"
        return "beat" if better and a * better > 0 else "missed"
    return "pending"


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
            if str(((extra.get("fresh") or {}).get(k) or {}).get("text", "")).endswith("estimate"):
                label += " (estimate until reported)"   # display only; drops off by itself once the quarter is reported
            lag = _quarters_between(asof[k], latest) if asof.get(k) and latest else 0
            pts = lin(m.get(k), bad, good) if lag < 2 else None
            items.append({"key": k, "label": label, "value": m.get(k), "score": pts, "fmt": f, "bad": bad, "good": good,
                          "asof": asof.get(k), "lag": lag, "note": notes.get(k), "fresh": (extra.get("fresh") or {}).get(k)})
        got = [i["score"] for i in items if i["score"] is not None]
        ps = round(sum(got) / len(got), 1) if got else None
        if ps is not None:
            total += ps * w
            wsum += w
        pillars.append({"name": name, "weight": w, "question": ask, "score": ps, "metrics": items})
    overall = round(total / wsum, 2) if wsum else None
    grade, verdict = next((g, v) for t, g, v in GRADES if overall is not None and overall >= t)
    # how many of the overall points each pillar supplies, rounded so the parts add up to the total exactly
    raw = {p["name"]: p["score"] * p["weight"] / wsum for p in pillars if p["score"] is not None and wsum}
    cents = {k: int(v * 100) for k, v in raw.items()}
    short = round((overall or 0) * 100) - sum(cents.values())
    for k in sorted(raw, key=lambda k: -(raw[k] * 100 - cents[k]))[:max(0, short)]:
        cents[k] += 1
    for p in pillars:
        p["contribution"] = cents[p["name"]] / 100 if p["name"] in cents else None
    out = {"overall": overall, "grade": grade, "verdict": verdict, "pillars": pillars, "weights_used": wsum}
    out["why"] = explain(out)
    out["what_if"] = what_if(pillars, "contracts_vs_proj", 0.0, overall)
    out["leverage"] = leverage(pillars, overall)
    # stress tests, alone and combined: the same scoring run on the changed inputs
    base = extra.get("stress") or {}
    out["stress"] = {}
    keys = sorted(base, key="rhc".index)
    for n in range(1, 1 << len(keys)):
        combo = "".join(k for b, k in enumerate(keys) if n >> b & 1)
        over = {k: v for c in combo for k, v in base[c]["changes"]}
        ov, ps = _rescore(pillars, over)
        out["stress"][combo] = {"overall": ov, "change": round(ov - overall, 2) if ov is not None else None,
                                "pillars": [[p["name"], p["score"], ps[p["name"]]] for p in pillars
                                            if ps.get(p["name"]) is not None and ps[p["name"]] != p["score"]]}
    return out


def leverage(pillars, overall):
    """How much each scored metric supplies to the overall score, what one more point of it is worth, and how far the
    overall would move if it reached full marks (its "10 points" value), all else unchanged."""
    wsum = sum(p["weight"] for p in pillars if p["score"] is not None)
    rows = []
    for p in pillars:
        scored = [i for i in p["metrics"] if i["score"] is not None]
        for i in scored:
            per_pt = p["weight"] / (len(scored) * wsum)
            full = None
            if i["score"] < 9.95:
                ov, _ = _rescore(pillars, {i["key"]: i["good"]})
                full = round(ov - overall, 2)
            rows.append({"key": i["key"], "label": i["label"], "pillar": p["name"], "value": i["value"], "fmt": i["fmt"],
                         "points": i["score"], "bad": i["bad"], "good": i["good"], "per_point": per_pt,
                         "contribution": i["score"] * per_pt, "to_full": full,
                         "step": (i["good"] - i["bad"]) / 10})  # metric change worth one point
    top = sorted((r for r in rows if r["to_full"]), key=lambda r: -r["to_full"])[:5]
    together = _rescore(pillars, {r["key"]: r["good"] for r in top})[0] if top else overall
    grades = [{"grade": g, "at": t, "gap": round(t - overall, 2)} for t, g, _ in GRADES if t > overall]
    return {"rows": rows, "top": [r["key"] for r in top], "top_together": together, "grades": sorted(grades, key=lambda x: x["at"])}


def _rescore(pillars, overrides):
    """Overall and pillar scores with some metric values replaced (only metrics that are scored today)."""
    total, wsum, ps = 0.0, 0, {}
    for p in pillars:
        got = []
        for i in p["metrics"]:
            sc = i["score"]
            if sc is not None and i["key"] in overrides:
                sc = lin(overrides[i["key"]], i["bad"], i["good"])
            if sc is not None:
                got.append(sc)
        ps[p["name"]] = round(sum(got) / len(got), 1) if got else None
        if got:
            total += ps[p["name"]] * p["weight"]
            wsum += p["weight"]
    return (round(total / wsum, 2) if wsum else None), ps


def what_if(pillars, key, value, overall):
    """The overall score if one metric took a different value (used for 'contracts hit the projection midpoint')."""
    total, wsum, hit = 0.0, 0, None
    for p in pillars:
        got = []
        for i in p["metrics"]:
            sc = i["score"]
            if i["key"] == key and sc is not None:
                sc = lin(value, i["bad"], i["good"])
                hit = (p, i, sc)
            if sc is not None:
                got.append(sc)
        if got:
            total += round(sum(got) / len(got), 1) * p["weight"]
            wsum += p["weight"]
    if not hit or not wsum:
        return None
    p, i, sc = hit
    new_p = round(sum((sc if j["key"] == key else j["score"]) for j in p["metrics"] if j["score"] is not None)
                  / len([j for j in p["metrics"] if j["score"] is not None]), 1)
    return {"key": key, "value": value, "metric_score": sc, "pillar": p["name"], "pillar_from": p["score"],
            "pillar_to": new_p, "overall_to": round(total / wsum, 2), "overall_change": round(total / wsum - overall, 2)}


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
    if kind == "days":
        return f"{v:.0f} days"
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
:root{--bartop:0px;--ink:#23201d;--muted:#786e64;--muted2:#5d554d;--line:#ebe7e2;--bg:#f3f2f0;--blue:#1d4ed8}
*{box-sizing:border-box}
html{background:#f9f8f8;scroll-padding-top:88px;scroll-behavior:smooth}
@media (prefers-reduced-motion:reduce){html{scroll-behavior:auto}}
@media (max-width:520px){html{scroll-padding-top:76px}}
body{margin:0;color:var(--ink);font:400 15px/1.5 Inter,system-ui,-apple-system,"Segoe UI","Helvetica Neue",Arial,sans-serif;
letter-spacing:-.011em;-webkit-font-smoothing:antialiased;font-variant-numeric:tabular-nums;
background:radial-gradient(80% 40% at 10% 0%,var(--g1) 0%,rgba(0,0,0,0) 60%),
radial-gradient(65% 35% at 50% 0%,var(--g2) 0%,rgba(0,0,0,0) 58%),
radial-gradient(50% 30% at 88% 0%,var(--g3) 0%,rgba(0,0,0,0) 50%),var(--bg);background-repeat:no-repeat;
padding-top:var(--bartop)}
a{color:inherit}
/* Top bar, after accountable.opendoor.com: flat white, full width, sticky, 72px; text links that get a soft grey
   rounded box on hover; a white raised button on the right. Our own title and disclaimer, no Opendoor branding. */
.bar{position:sticky;top:0;z-index:5;background:#fff}
.bar .in{max-width:1280px;height:72px;margin:0 auto;padding:0 24px;display:flex;align-items:center;gap:4px}
.brand{order:0;text-align:left;font-weight:600;font-size:18px;line-height:1.2;letter-spacing:-.03em;text-decoration:none;white-space:nowrap}
.brand small{display:block;font-weight:400;color:var(--muted);font-size:12px;margin-top:2px;letter-spacing:0}
.bar nav.desk{order:1;margin-left:auto;display:flex;gap:0}
.bar nav.desk a{display:inline-flex;align-items:center;height:44px;padding:0 16px;border-radius:12px;font-size:16px;font-weight:500;
letter-spacing:-.05em;text-decoration:none;white-space:nowrap;transition:background .15s}
.bar nav.desk a:hover{background:#efebe8}
.burger{display:none;order:3;margin-left:8px;width:44px;height:44px;border:0;border-radius:12px;background:#fff;
box-shadow:0 0 0 1px rgba(88,64,50,.16),0 2px 0 rgba(88,64,50,.10);
padding:0;cursor:pointer;position:relative}
.burger span{position:absolute;left:12px;right:12px;height:2px;border-radius:2px;background:var(--ink);transition:transform .25s,opacity .2s,top .25s}
.burger span:nth-child(1){top:14px}.burger span:nth-child(2){top:20px}.burger span:nth-child(3){top:26px}
.burger[aria-expanded="true"] span:nth-child(1){top:20px;transform:rotate(45deg)}
.burger[aria-expanded="true"] span:nth-child(2){opacity:0}
.burger[aria-expanded="true"] span:nth-child(3){top:20px;transform:rotate(-45deg)}
.menu{position:fixed;left:0;right:0;top:0;bottom:0;background:rgba(249,248,248,.98);backdrop-filter:blur(10px);
padding:calc(var(--barh,64px) + 2 * var(--bartop,14px)) 16px 32px;overflow-y:auto;z-index:4;animation:drop .22s ease-out}
.menu[hidden]{display:none}
@keyframes drop{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
.menu a{display:block;background:#fff;border-radius:16px;padding:14px 18px;margin-top:8px;text-decoration:none}
.menu a span{display:block;font-size:20px;font-weight:500;letter-spacing:-.02em}
.menu a small{display:block;font-size:14px;color:var(--muted);margin-top:1px}
.menu a.menu-by{background:none;text-align:center;font-size:14px;color:var(--muted);margin-top:18px}
.credit{display:flex;flex-wrap:wrap;justify-content:center;gap:6px 28px;margin:32px 0 0}
.r3{order:2;margin-left:20px;display:flex;align-items:center;gap:14px}
.nl-btn{display:inline-flex;align-items:center;height:44px;padding:0 20px;border-radius:12px;background:rgba(255,255,255,.9);color:var(--ink);
font-size:16px;font-weight:500;letter-spacing:-.04em;text-decoration:none;white-space:nowrap;
box-shadow:0 0 0 1px rgba(88,64,50,.16),inset 0 0 0 1px rgba(255,255,255,.3),0 2px 0 rgba(88,64,50,.10)}
.nl-btn:hover{background:#faf8f6}
.by{order:-1;font-size:14px;color:var(--muted);text-decoration:none;white-space:nowrap}
.by:hover{color:var(--ink)}
@media (max-width:1320px){.r3 .by{display:none}}
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
.pbar{display:grid;grid-template-columns:150px 1fr 44px 46px;align-items:center;gap:14px;padding:7px 0;font-size:15px}
.pbar .track{height:10px;border-radius:6px;background:#f1efec;overflow:hidden}
.pbar .fill{height:100%;border-radius:6px}
.pmv{font-style:normal;font-size:12px;font-variant-numeric:tabular-nums;text-align:center;border-radius:999px;padding:1px 0}.pmv.up{color:#0f7a4f;background:#e6f6ee}.pmv.dn{color:#c2410c;background:#fdeee6}.pmv.fl{color:#b3ada4}
.fill.green{background:#34d399}.fill.amber{background:#f5b53f}.fill.red{background:#f07470}.fill.grey{background:#d6d1cb}
.pbar .w{color:var(--muted);font-size:13px;margin-left:6px}
.pbar b{font-weight:500;text-align:right}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px}
.grid .card{margin-top:0}
.section{font-size:13px;font-weight:500;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin:56px 0 0}
.m{display:flex;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line);font-size:14px}
.m:last-child{border-bottom:0}.m .lab{color:var(--muted2)}
.mini{display:flex;align-items:center;gap:6px;margin-top:4px}.mini svg{flex:0 0 auto}.mini i{font-style:normal;font-size:11.5px;color:var(--muted)}
.mini-a{font-size:12px;color:#1d4ed8;display:inline-block;margin-top:2px}
.hbox{position:relative}.hchart{display:block;touch-action:pan-y}.hchart .tick{font-size:12px;fill:#786e64}
.htip{position:absolute;pointer-events:none;background:#fff;border:1px solid var(--line);border-radius:10px;padding:8px 11px;
box-shadow:0 6px 18px rgba(35,32,29,.10);font-size:13px;white-space:nowrap}.htip b{display:block;font-weight:600}
.htip span{display:block;color:var(--muted2)}
.hnow{font-size:26px;font-weight:600;letter-spacing:-.02em;vertical-align:middle;margin-right:6px}
.hnote{margin:10px 0 0;font-size:13px;color:var(--muted)}

.score .mini{margin:0 0 10px 14px;align-self:flex-end}
.si{margin-top:10px;padding:10px 12px;border-radius:10px;background:#f6f4f1;font-size:13.5px;color:var(--muted2);line-height:1.45}
.si b{color:var(--ink,#23201d);font-weight:500}.si small{display:block;font-size:11.5px;color:var(--muted);margin-top:3px}
.ns{font-size:11px;background:#fff;border:1px solid var(--line);border-radius:6px;padding:0 6px;margin-left:4px;color:var(--muted)}
.rb{display:flex;gap:12px;align-items:flex-start;margin-top:8px;padding:14px 18px;border-radius:14px;background:rgba(255,255,255,.75);
border:1px solid #d9e1fb;font-size:14.5px;color:var(--muted2);line-height:1.5}.rb p{margin:0}.rb b{color:var(--ink,#23201d);font-weight:500}
.rb ul{margin:6px 0 0;padding-left:18px}.rb li{margin:2px 0}.rb-dot{flex:0 0 auto;width:9px;height:9px;border-radius:50%;background:#1d4ed8;margin-top:7px}
.rb.done{border-color:#bfe8d5}.rb.done .rb-dot{background:#10b981}
.gaps{display:flex;flex-wrap:wrap;gap:8px;margin:0}.gap{background:#f6f4f1;border-radius:10px;padding:7px 12px;font-size:14px;color:var(--muted2)}
.gap b{color:var(--ink,#23201d);font-weight:600}.mv-h{margin:16px 0 0;font-weight:500;font-size:15px}
table.mv td small{display:block;font-size:11.5px;color:var(--muted)}table.mv td,table.mv th{white-space:nowrap}
.mv-sum{font-size:14px;color:var(--muted2);margin:12px 0 0}.mv-all{margin-top:12px}.mv-all summary{cursor:pointer;color:#1d4ed8;font-size:14px}
table.mv2 td,table.mv2 th{white-space:nowrap}
.tool-h{margin:4px 0 8px;font-weight:500;font-size:15px}.tool-h~.tool-h{margin-top:22px}
.scen-note{font-size:13.5px;color:var(--muted2);margin:-4px 0 10px}
.ins{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.sin{display:flex;flex-direction:column;gap:6px;border:1px solid var(--line);
border-radius:12px;padding:10px 12px;font-size:14px}.sin small{display:block;font-size:12px;color:var(--muted)}
.in-f{display:flex;align-items:center;gap:6px}.in-f input{width:100%;font:inherit;font-size:16px;padding:7px 9px;border:1px solid var(--line);border-radius:8px}
.in-note{margin:8px 0 0;font-size:12.5px}.sc-pil{font-size:13.5px;color:var(--muted2);margin:12px 0 0}
.spre{font:inherit;font-size:14px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 14px;cursor:pointer}
.spre.on{background:#1d4ed8;color:#fff;border-color:#1d4ed8}
table.trk td small,table.psh th small{display:block;font-size:11.5px;color:var(--muted);font-weight:400}
table.trk td,table.trk th{white-space:nowrap}table.psh td:not(.wtxt){white-space:nowrap}
.imp-in{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.imp-in select{width:100%;font:inherit;font-size:16px;padding:7px 9px;
border:1px solid var(--line);border-radius:8px;background:#fff}.imp-cash{display:flex;gap:8px;align-items:center;font-size:14px;color:var(--muted2);margin-top:12px}
.imp-out{font-size:17px;line-height:1.5;margin:14px 0 0}.imp-out b{font-weight:600}
table.imp-grid td,table.imp-grid th{white-space:nowrap;text-align:right}table.imp-grid td:first-child,table.imp-grid th:first-child{text-align:left}
table.imp-grid td small{display:block;font-size:11.5px;color:var(--muted)}table.imp-grid td.on{background:#eef2ff;border-radius:8px;font-weight:600}
@media (max-width:900px){.imp-in{grid-template-columns:1fr 1fr}}
@media (max-width:640px){table.trk tr:first-child,table.psh tr:first-child{display:none}table.trk,table.trk tbody,table.psh,table.psh tbody{display:block}
table.trk tr,table.psh tr{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 8px;padding:10px 0;border-bottom:1px solid #eee}
table.trk td,table.psh td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px}
table.trk td:first-child,table.psh td:first-child{grid-column:1/-1;font-weight:500;min-width:0}
table.trk td[data-l]::before,table.psh td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}
table.imp-grid td:first-child,table.imp-grid th:first-child{position:sticky;left:0;background:#fff;z-index:1}}
.moved{margin:24px 0 0;padding:14px 18px;border-radius:14px;background:rgba(255,255,255,.75);border:1px solid var(--line);
font-size:14.5px;line-height:1.5;color:var(--muted2)}
.moved b{color:var(--ink,#23201d);font-weight:500}
.mv-dir{display:inline-block;font-weight:600;border-radius:8px;padding:1px 9px;margin:0 2px}
.mv-dir.up{background:#e6f4ea;color:#0f9f6e}.mv-dir.down{background:#fdeceb;color:#e5484d}.mv-dir.flat{background:#f1efec;color:var(--muted2)}
/* dashboard: the top card */
.dash .d-top{display:grid;grid-template-columns:3fr 1fr;gap:0;padding:0}.dash .d-main{padding:24px 28px 22px;min-width:0}.dash .d-side{padding:24px 28px 22px;min-width:0}
.d-k{margin:0 0 6px;font-size:13px;font-weight:500;color:var(--muted2)}.d-k span{font-weight:400;color:var(--muted)}
.dash .score{align-items:center;gap:12px}.dash .score .mini i{display:none}.dash .score .num{font-size:clamp(60px,8vw,96px)}
.d-chg{display:inline-flex;flex-direction:column;align-items:flex-start;gap:3px;margin-left:4px}.d-chg small{font-size:12px;color:var(--muted)}
.dash .verdict{margin-top:14px}.dash .why{font-size:16px}
.d-moved{margin-top:14px;padding:10px 14px;border-radius:12px;background:#f6f4f1;font-size:14.5px;line-height:1.5;color:var(--muted2)}
.d-moved>p{margin:0}.d-moved summary{cursor:pointer;color:#1d4ed8;font-size:13px;margin-top:6px}
.d-moved .moved{margin:8px 0 0;padding:0;border:0;background:none;font-size:13.5px}
.d-side{border-left:1px solid var(--line)}
.d-pills{list-style:none;margin:0;padding:0}.d-pills li{display:flex;justify-content:space-between;align-items:center;gap:10px;
padding:6px 0;border-bottom:1px solid var(--line);font-size:13.5px}.d-pills li:last-child{border-bottom:0}
.d-src small{display:block;font-size:11.5px;color:var(--muted)}.d-pills .badge{font-size:12px;padding:2px 8px}
.d-more{display:inline-block;margin-top:8px;font-size:13px;color:#1d4ed8}
.dash .kpis .kpi{padding:14px 28px}.dash .kpis .kpi .v{font-size:24px}
.d-sec{border-top:1px solid var(--line);padding:16px 28px 20px}
.d-h{display:flex;justify-content:space-between;align-items:baseline;gap:6px 16px;flex-wrap:wrap;margin-bottom:10px}
.d-h .d-k{margin:0}.d-hint{margin:0;font-size:13px;color:var(--muted)}.d-hint a{color:#1d4ed8;margin-left:6px}
.mv5{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px}
.mv5-t{border:1px solid var(--line);border-radius:12px;padding:10px 12px;display:flex;flex-direction:column;gap:4px}
.mv5-n{margin:0;font-size:13.5px;font-weight:500;line-height:1.3}.mv5-n small{display:block;font-weight:400;font-size:11.5px;color:var(--muted)}
.mv5-v{margin:0;font-size:15px}.mv5-v small{font-size:12px;color:var(--muted);margin-left:6px}
.mv5-f{margin:0;font-size:12px;color:var(--muted)}.mv5-l{margin:auto 0 0;font-size:15px;font-weight:600;color:#0f9f6e}
.d-call{border-top:1px solid var(--line);padding:16px 28px 20px;background:#fbfaf8}
.d-ct{margin:0 0 12px;font-size:14.5px;color:var(--muted2);display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.d-ct b{color:var(--ink);font-weight:600}.d-ct .rb-dot,.d-call .rb-dot{margin-top:0;align-self:center}
.d-cols{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:18px}
.d-ch{margin:0 0 6px;font-size:14px;font-weight:500}.d-ch small{display:block;font-weight:400;font-size:12px;color:var(--muted)}
.d-cols ul{margin:0;padding:0;list-style:none;font-size:13.5px;color:var(--muted2)}.d-cols li{padding:2px 0}.d-cols li b{color:var(--ink);font-weight:500}
.d-call.q3{background:linear-gradient(180deg,#f3f6fd,#fbfaf8 70%);padding:20px 28px 22px}
.q3-hd{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.q3-t{margin:0;font-size:18px;font-weight:600;color:var(--ink);display:flex;align-items:center;gap:9px}
.q3-when{font-size:13px;font-weight:500;color:#1d4ed8;background:#e3ebfd;border-radius:999px;padding:4px 12px}
.q3-i{margin:0 0 14px;font-size:14px;color:var(--muted2)}
.q3-gs{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}
.q3-g{background:#fff;border:1px solid var(--line);border-radius:14px;padding:12px 14px 6px}
.q3-gh{margin:0 0 6px;font-size:14px;font-weight:600;display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}
.q3-gh small{font-weight:400;font-size:12px;color:var(--muted)}
.q3-c{display:inline-grid;place-items:center;min-width:22px;height:22px;border-radius:999px;background:#eef1f6;font-size:12px;font-weight:600;color:var(--muted2);align-self:center}
.q3-l{list-style:none;margin:0;padding:0}
.q3-l li{display:grid;grid-template-columns:minmax(0,1fr) auto auto auto auto;align-items:center;gap:8px;padding:7px 0;border-top:1px solid #f1efeb;font-size:13.5px}
.q3-l li:first-child{border-top:0}
.q3-m{color:var(--ink);font-weight:500;min-width:0}.q3-m small{display:block;font-weight:400;font-size:11.5px;color:var(--muted)}
.q3-v{font-variant-numeric:tabular-nums;color:var(--muted2);text-align:right}
.q3-tag{font-size:11.5px;border-radius:999px;padding:2px 8px;white-space:nowrap;border:1px solid transparent}
.q3-tag.old{background:#f1f0ed;color:var(--muted2)}.q3-tag.est{background:#fdf3e1;color:#92580b}
.q3-tag.to{border-color:#c9d6f5;color:#1d4ed8;background:#fff}.q3-tag.rep{background:#e6f6ee;color:#0f7a4f}
.q3-ar{color:var(--muted);font-size:13px}
.q3-same{margin:12px 0 0;font-size:13px;color:var(--muted2)}.q3-same b{color:var(--ink);font-weight:500}
.d-call.q3.done{background:#f6fbf8}.d-call.q3.done .rb-dot{background:#10b981}
.q3-l li.rep{grid-template-columns:minmax(0,1fr) auto auto}
@media (max-width:900px){.dash .d-top{grid-template-columns:1fr;gap:0;padding:0}.dash .d-main{padding:20px 18px 18px}.dash .d-side{border-left:0;border-top:1px solid var(--line);padding:14px 18px 18px}
.mv5{grid-template-columns:repeat(2,minmax(0,1fr))}.d-cols{grid-template-columns:1fr;gap:12px}.d-sec,.d-call{padding:14px 18px 18px}.d-call.q3{padding:16px 16px 18px}.q3-gs{grid-template-columns:1fr}.q3-l li{display:flex;gap:6px}.q3-m{flex:1 1 auto}.q3-tag.old,.q3-ar{display:none}.q3-tag.to::before{content:'\\2192  '}.q3-t{font-size:16.5px}
.dash .kpis .kpi{padding:12px 18px}}
.mc{margin:0;padding-left:18px;font-size:14px;color:var(--muted2)}.mc li{margin:4px 0}
/* wide tables on phones: scroll sideways, keep the first column in view */
table.dil td:first-child,table.dil th:first-child,table.mk td:first-child,table.mk th:first-child,table.mv td:first-child,
table.mv th:first-child,table.mv2 td:first-child,table.mv2 th:first-child{position:sticky;left:0;background:#fff;z-index:1;box-shadow:1px 0 0 var(--line)}
table.dil tr.tot td:first-child{background:inherit}
@media (max-width:640px){table.mk{min-width:620px}.ins{grid-template-columns:1fr}
/* the two densest tables stack into cards on phones instead of scrolling sideways */
table.dil tr:first-child,table.qr tr:first-child{display:none}table.dil,table.dil tbody,table.qr,table.qr tbody{display:block}
table.dil tr{display:grid;grid-template-columns:1fr 1fr;gap:2px 10px;padding:9px 0;border-bottom:1px solid #eee}
table.qr tr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px 8px;padding:10px 0;border-bottom:1px solid #eee}
table.dil td,table.qr td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px;position:static!important;box-shadow:none!important;background:none!important}
table.dil td:first-child,table.qr td:first-child{grid-column:1/-1;font-weight:500}table.dil tr.tot td{border:0!important;font-weight:600}
table.dil td[data-l]::before,table.qr td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}
table.mv tr:first-child{display:none}table.mv,table.mv tbody{display:block}
table.mv tr{display:grid;grid-template-columns:1fr 1fr 1fr;gap:4px 8px;padding:10px 0;border-bottom:1px solid #eee}
table.mv td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px;position:static!important;box-shadow:none!important}
table.mv td:first-child{grid-column:1/-1;font-weight:500;min-width:0}
table.mv td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}
.score .mini{margin:6px 0 0;flex-basis:100%}.score{flex-wrap:wrap}}
.m .scale{display:none;font-size:12px;color:#1d4ed8;margin-top:3px}.show-scale .m .scale{display:block}
.spark{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:4px}.spark svg{flex:0 0 auto}
.spark i{font-style:normal;font-size:12px;color:var(--muted)}.spark a,.how a{font-size:12px;color:#1d4ed8}
.how{display:block;font-size:12px;color:var(--muted);margin-top:3px}
.sec-tools{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-top:12px}
.scale-tog{font:inherit;font-size:14px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:7px 14px;cursor:pointer}
.scale-tog[aria-pressed=true]{background:#23201d;color:#fff;border-color:#23201d}
.sec-tools a{font-size:14px;color:#1d4ed8;text-decoration:none;padding:7px 4px}.m .val{font-weight:500;white-space:nowrap}
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
.m .lab em.age{display:table;font-style:normal;font-size:11px;font-weight:500;color:var(--muted2);background:#f1efec;border-radius:6px;padding:1px 7px;margin-top:4px}
.m .lab em.age.old{color:#c27c0e;background:#fdf3e1}
.decomp{font-size:13px;color:var(--muted2);margin:12px 0 0;line-height:1.6}
table.stress{margin-top:12px}table.stress td,table.stress th{padding:8px 10px}
@media (max-width:640px){table.stress{width:100%;table-layout:fixed}table.stress td,table.stress th{padding:7px 4px;white-space:normal;font-size:13px}
table.stress td:first-child,table.stress th:first-child{width:38%}
table.fresh tr:first-child{display:none}table.fresh,table.fresh tbody{display:block}
table.fresh tr{display:grid;grid-template-columns:1fr 1fr 1fr;gap:2px 8px;padding:10px 0;border-bottom:1px solid #eee}
table.fresh td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px}
table.fresh td:first-child{grid-column:1/-1;font-weight:500}table.fresh td:last-child{grid-column:1/-1}table.fresh td:last-child:empty{display:none}
table.fresh td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}
table.guide td,table.guide th{padding:7px 4px}
table.sx tr:first-child{display:none}table.sx,table.sx tbody{display:block}
table.sx tr{display:grid;grid-template-columns:1fr 1fr;gap:2px 8px;padding:8px 0;border-bottom:1px solid #eee}
table.sx td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px}
table.sx td:first-child{grid-column:1/-1;font-weight:500}table.sx tr.grp td,table.sx td[colspan]{grid-column:1/-1;padding:4px 0}
table.sx td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}}
td.wtxt{white-space:normal;text-align:left;min-width:220px}table.thr td.old{color:#c27c0e}
tr.grp td{font-weight:600;color:var(--ink);background:#faf9f7;text-align:left}
table.thr td:nth-child(6),table.fresh td:nth-child(2){white-space:nowrap}
@media (max-width:640px){table.thr tr:first-child{display:none}table.thr,table.thr tbody{display:block}
table.thr tr{display:grid;grid-template-columns:repeat(4,1fr);gap:2px 8px;padding:10px 0;border-bottom:1px solid #eee}
table.thr td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px}
table.thr td.wtxt{grid-column:1/-1;min-width:0;font-weight:500;padding-bottom:4px;text-align:left!important}
table.thr td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}
table.thr td:nth-child(6){grid-column:1/-1;padding-top:4px}table.thr td:nth-child(6)::before{display:inline;content:"Data: "}
table.thr tr.grp{display:block}table.thr tr.grp td{padding:6px 0}}
.tgt{padding:14px 0;border-bottom:1px solid var(--line)}.tgt:first-child{padding-top:0}.tgt:last-child{border-bottom:0;padding-bottom:0}
.tgt-hd{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap}.tgt-hd b{font-weight:500;font-size:16px}
.tgt p{margin:6px 0 0;font-size:14px;color:var(--muted2);max-width:900px}.tgt p.dim{font-size:13px;color:var(--muted)}
table.guide{margin-top:8px}table.guide td:first-child{white-space:normal}
.nl{margin-top:24px;border-radius:22px;padding:36px 40px;background:radial-gradient(120% 140% at 0% 0%,#2b4fd8 0%,#1b2f8a 45%,#131a3d 100%);
color:#fff;display:grid;grid-template-columns:1.05fr 1fr;gap:40px;align-items:center;box-shadow:0 18px 50px rgba(19,26,61,.28);position:relative;overflow:hidden}
.nl:after{content:"";position:absolute;right:-80px;top:-80px;width:260px;height:260px;border-radius:50%;background:rgba(255,255,255,.06)}
.nl-k{margin:0 0 8px;font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:#a9bbff;font-weight:600}
.nl h2{margin:0;font-size:34px;line-height:1.1;font-weight:600;letter-spacing:-.02em;color:#fff}
.nl-p{margin:12px 0 0;font-size:17px;line-height:1.45;color:#d6defc}
.nl-list{margin:16px 0 0;padding:0;list-style:none;font-size:15px;color:#e8edff}.nl-list li{padding:4px 0 4px 26px;position:relative}
.nl-list li:before{content:"";position:absolute;left:0;top:9px;width:14px;height:8px;border-left:2px solid #7ee2b8;border-bottom:2px solid #7ee2b8;transform:rotate(-45deg)}
.nl-form{position:relative;z-index:1;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.14);border-radius:16px;padding:20px}
.nl-what{margin:0 0 14px}.nl-what b{display:block;font-size:19px;font-weight:600;color:#fff}.nl-what span{display:block;font-size:14px;color:#c9d4ff;margin-top:2px}
.freq{border:0;margin:0 0 14px;padding:0;display:grid;grid-template-columns:1fr 1fr;gap:10px}.freq legend{font-size:14px;color:#c9d4ff;margin-bottom:8px;padding:0}
.freq label{display:block;cursor:pointer}.freq input{position:absolute;opacity:0;pointer-events:none}
.freq span{display:block;border:1.5px solid rgba(255,255,255,.22);border-radius:12px;padding:12px 14px;transition:.15s}
.freq b{display:block;font-size:17px;font-weight:600;color:#fff}.freq small{display:block;font-size:13px;color:#c9d4ff;margin-top:2px}
.freq input:checked+span{background:#fff;border-color:#fff}.freq input:checked+span b{color:#1b2f8a}.freq input:checked+span small{color:#4b5a8f}
.freq input:focus-visible+span{outline:2px solid #7ee2b8;outline-offset:2px}
.nl-row{display:flex;gap:8px}.nl-row input{flex:1;min-width:0;font:inherit;font-size:16px;padding:13px 14px;border-radius:12px;border:0;color:#23201d}
.nl-row button{font:inherit;font-size:16px;font-weight:600;padding:13px 18px;border:0;border-radius:12px;background:#7ee2b8;color:#0f2a1f;cursor:pointer;white-space:nowrap}
.nl-row button:hover{background:#9cf0cc}
.nl-ok{margin:12px 0 0;font-size:14px;color:#7ee2b8}.nl-fine{margin:12px 0 0;font-size:12.5px;color:#a9b6e8}.nl-fine a{color:#fff}
/* Context vs Opendoor's own history */
table.oh td small{display:block;font-size:12px;color:var(--muted);font-weight:400;margin-top:1px}table.oh td:first-child{white-space:normal}table.oh td em{font-style:normal;color:var(--muted2);display:block}table.oh td.oh-r{width:150px;min-width:120px}.oh-t{position:relative;display:block;height:6px;border-radius:3px;background:#ece9e4;margin:0 6px}.oh-t i{position:absolute;top:50%;width:11px;height:11px;margin:-5.5px 0 0 -5.5px;border-radius:50%;background:#1d4ed8;box-shadow:0 0 0 2px #fff}.oh-e{display:flex;justify-content:space-between;font-size:11px;color:var(--muted);margin-top:4px}
@media (max-width:640px){table.oh tr:first-child{display:none}table.oh,table.oh tbody{display:block}table.oh tr{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 10px;padding:10px 0;border-bottom:1px solid #eee}table.oh td{border:0;padding:0;text-align:left!important;white-space:normal!important;font-size:13px}table.oh td:first-child,table.oh td.oh-r{grid-column:1/-1}table.oh td:first-child{font-weight:500}table.oh td.oh-r{width:auto;padding-top:4px}table.oh td[data-l]::before{content:attr(data-l);display:block;font-size:11px;color:#8a8a8a}}
.howto{margin-top:16px;font-size:14.5px;color:var(--muted2);max-width:560px}.howto summary{cursor:pointer;font-weight:500;color:var(--ink);list-style:none;display:inline-flex;align-items:center;gap:6px}.howto summary::-webkit-details-marker{display:none}.howto summary::before{content:'+';display:inline-grid;place-items:center;width:18px;height:18px;border-radius:50%;border:1px solid #cfc9bf;font-size:13px;line-height:1;color:var(--muted2)}.howto[open] summary::before{content:'\\2212'}.howto p{margin:10px 0 0;line-height:1.55}
@media (min-width:901px){.hero>div:last-child{padding-top:92px}}
.nl-cta{display:inline-flex;align-items:center;gap:8px;margin-top:18px;font-size:16px;font-weight:600;color:#fff;background:#1d4ed8;
padding:11px 18px;border-radius:12px;text-decoration:none;box-shadow:0 6px 20px rgba(29,78,216,.28)}.nl-cta:hover{background:#1e40af}
.nl-strip{display:flex;justify-content:space-between;align-items:center;gap:16px;margin-top:28px;padding:18px 22px;border-radius:16px;
background:#131a3d;color:#e8edff;text-decoration:none;font-size:16px}.nl-strip b{color:#fff;font-weight:600}
.nl-go{background:#7ee2b8;color:#0f2a1f;font-weight:600;padding:8px 14px;border-radius:10px;white-space:nowrap}
.menu a.menu-nl{background:#1d4ed8;color:#fff}.menu a.menu-nl small{color:#c9d4ff}
@media (max-width:900px){.nl{grid-template-columns:1fr;padding:26px 20px;gap:22px;border-radius:18px}.nl h2{font-size:27px}
.nl-row{flex-direction:column}.freq{grid-template-columns:1fr 1fr}.nl-strip{flex-direction:column;align-items:flex-start}}
.hit{font-size:15px!important;color:var(--ink,#23201d)!important;margin:8px 0 4px!important}.hit b{font-weight:600}
table.guide tr.grp td,table.sx tr.grp td{background:#faf8f5;font-weight:500;padding-top:10px}table.guide .dim{color:var(--muted);font-weight:400;font-size:13px}
td.dim{color:var(--muted);font-size:13px}
.uechart .val{font-size:13px;fill:#23201d;font-weight:500}.uebox{overflow-x:auto}@media (max-width:640px){.uechart{min-width:560px}}.uechart .tick{font-size:12px;fill:#786e64}
table.ue td,table.fall td{white-space:nowrap}table.ue th,table.fall th{white-space:normal;vertical-align:bottom;min-width:64px}
.togs{display:flex;flex-wrap:wrap;gap:10px}.tog{display:flex;align-items:center;gap:8px;border:1px solid var(--line);border-radius:10px;
padding:9px 12px;font-size:15px;cursor:pointer;background:#fff}.tog input{width:17px;height:17px;accent-color:#1d4ed8}
.stress-out{margin-top:14px;padding:12px 14px;border-radius:10px;background:#f6f4f1}.stress-out p{margin:0}.so-main{font-size:17px}
.so-pil{font-size:14px;color:var(--muted2);margin-top:4px!important}.stress-out.down{background:#fdeceb}.stress-out.up{background:#e6f4ea}
.presets{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}.pre{font:inherit;font-size:14px;border:1px solid var(--line);
background:#fff;border-radius:999px;padding:7px 14px;cursor:pointer}.pre.on{background:#23201d;color:#fff;border-color:#23201d}
.sliders{display:grid;grid-template-columns:1fr 1fr;gap:6px 32px}.sl{display:grid;grid-template-columns:150px 1fr 44px;align-items:center;gap:10px;font-size:14px}
.sl-n i{font-style:normal;color:var(--muted);margin-left:6px}.sl-v{text-align:right;font-variant-numeric:tabular-nums}
.sl input{width:100%;accent-color:#1d4ed8}.wp-score{font-size:34px;font-weight:500;letter-spacing:-.02em}
@media (max-width:800px){.sliders{grid-template-columns:1fr}.sl{grid-template-columns:120px 1fr 40px}}
.dchart{touch-action:pan-y;cursor:crosshair}
.dchart .pulse{transform-box:fill-box;transform-origin:center;opacity:.45;animation:pulse 2.4s ease-out infinite}
@media (max-width:900px){.grid2{grid-template-columns:1fr}}
@media (max-width:640px){.chartbox .dchart{min-width:600px}}
@media (max-width:520px){.pair{grid-template-columns:96px 1fr 70px;gap:8px}.dlab b{font-size:22px}}
@media (prefers-reduced-motion:reduce){.dchart .pulse{animation:none}}
@media (max-width:1180px){.bar nav.desk{display:none}.burger{display:block}.r3{margin-left:auto}
.menu a{max-width:680px;margin-left:auto;margin-right:auto}}
@media (max-width:900px){.burger{display:block}.by{display:none}.r3 .nl-btn{display:none}.bar nav.desk{display:none}.hero{grid-template-columns:1fr;gap:20px;padding-top:48px}.asof{position:static;text-align:left;order:-1}
.grid{grid-template-columns:1fr}.kpis{grid-template-columns:1fr 1fr}.kpi:nth-child(2){border-right:0}
.kpi:nth-child(-n+2){border-bottom:1px solid var(--line)}h1{margin-top:8px}.bar nav.desk{display:none}}
@media (max-width:520px){.brand small{font-size:11px}.brand{font-size:17px}.pbar{grid-template-columns:112px 1fr 28px 38px;gap:7px;font-size:13.5px}.pbar .w{display:none}
.card .hd{flex-wrap:nowrap;gap:12px}.card .hd>div:first-child{flex:1 1 auto;min-width:0}.card .hd>.right{flex:0 0 auto}.card .hd.stack{flex-direction:column;gap:10px}.hd.stack .right{text-align:left}.by{display:none}.bar .in{height:60px;padding:0 14px}.burger{margin-left:auto}.brand{grid-column:2}.bd,.card .hd,.kpi,.ft{padding-left:18px;padding-right:18px}.wrap{padding:0 14px 80px}}
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
            f'<p class="dnote">Opendoor holds homes on its books at {_money(inv)} against {_money(home)} of debt secured on them, '
            f'so {_money(max(0, inv - home))} ({cush:.1f}%) of its own money is in them. That is the cushion before a fall in home '
            f'prices reaches the lenders. Lenders can take the homes but, except in limited cases, cannot claim the company.</p>'
            f'{_stress_table(inv, home)}</div>'
            f'<div class="dbar"><div class="dlab"><span>Cash vs the company&#39;s own debt</span><b>{cover}</b></div>'
            f'<div class="pair"><span>Cash</span><div class="dtrack"><div class="dcash" style="width:{cash / top * 100:.1f}%"></div></div><b>{_money(cash)}</b></div>'
            f'<div class="pair"><span>Convertible notes</span><div class="dtrack"><div class="dfill" style="width:{max(0, notes) / top * 100:.1f}%"></div></div><b>{_money(notes)}</b></div>'
            f'<p class="dnote">The notes are unsecured, so they are the debt the company itself must repay or convert.</p></div>'
            f'</div><div class="ft">Homes and home-backed debt from the {e(q)} balance sheet (10-Q).{e(adj)}</div></div>')


def _stress_table(inv, home):
    rows = ""
    for d in (0.0, 0.05, 0.10, 0.20):
        v = inv * (1 - d)
        gap = v - home
        what = (f"{_money(gap)} to spare" if gap >= 0 else f"{_money(-gap)} short")
        rows += (f'<tr><td>{"Today" if d == 0 else f"Homes worth {d * 100:.0f}% less"}</td><td>{_money(v)}</td>'
                 f'<td class="{"up" if gap >= 0 else "down"}">{what}</td></tr>')
    return ('<table class="stress"><tr><th>If home values fell</th><th>Homes worth</th><th>Vs the debt on them</th></tr>' + rows +
            '</table><p class="dnote">Book values: what Opendoor paid plus renovation, less write-downs. Selling costs would '
            'take a little more off what the homes fetch.</p>')


def _what_if_text(s):
    w = s.get("what_if")
    if not w:
        return ""
    return (f"If the last 4 weeks had hit the projection midpoint, {w['pillar']} would score {w['pillar_to']:.1f} instead of "
            f"{w['pillar_from']:.1f}, and the overall score would be {w['overall_to']:.2f} ({w['overall_change']:+.2f}). ")


def _fresh_rows(res):
    """Each source: (name, short name, data-to text, date, out of date?, how often it updates)."""
    ex, px, acc = res["extra"], res["price"], res.get("accountable") or {}
    today = res["generated"][:10]
    last_qe = _quarter_end_before(today)
    bs_q = (ex.get("balance_sheet") or {}).get("quarter_end")
    rel_q = next((r["quarter_end"] for r in reversed(res["quarters"]) if r.get("homes_sold") is not None), None)
    dq = (ex.get("dilution") or {}).get("quarter")
    mort = (ex.get("mortgage") or {}).get("date")
    hous = (ex.get("housing") or {}).get("month")
    return [
        ("Balance sheet and cash flow (10-Q)", "Balance sheet (10-Q)", bs_q and _qlabel(bs_q), bs_q, bs_q and bs_q < last_qe, "quarterly"),
        ("Earnings release (homes, margins, adj. figures)", "Earnings release", rel_q and _qlabel(rel_q), rel_q,
         rel_q and rel_q < last_qe, "quarterly"),
        ("Potential new shares (10-Q table)", "Share table (10-Q)", dq and _qlabel(dq), dq, dq and dq < last_qe, "quarterly"),
        ("Opendoor's tracker", "Opendoor's tracker", acc.get("as_of") and _d(acc["as_of"]), acc.get("as_of"),
         acc.get("as_of") and _days(acc["as_of"], today) > 14, "weekly"),
        ("Share price", "Share price", _d(px["date"]), px["date"], _days(px["date"], today) > 5, "every weekday"),
        ("Mortgage rate", "Mortgage rate", mort and f"week of {_d(mort)}", mort, mort and _days(mort, today) > 14, "weekly"),
        ("Housing in Opendoor's markets", "Housing data", hous and _month(hous), hous and hous + "-01",
         hous and _days(hous + "-01", today) > 95, "monthly"),
    ]


def _fresh_card(res):
    today = res["generated"][:10]
    body = ""
    for name, _, asof, date_, old, every in _fresh_rows(res):
        if not asof:
            continue
        age = _days(str(date_)[:10], today)
        badge = _badge("Over a quarter old" if old else "Current", "amber" if old else "green")
        body += (f'<tr><td>{html.escape(name)}</td><td data-l="Data to">{html.escape(asof)}</td>'
                 f'<td data-l="Age">{age} day{"" if age == 1 else "s"}</td><td data-l="Updates">{every}</td><td>{badge}</td></tr>')
    nxt = ""
    if NEXT_RESULTS["date"] >= today:
        nxt = (f' Next: {html.escape(NEXT_RESULTS["what"])} on {_d(NEXT_RESULTS["date"])}; the first weekday run after that '
               'picks them up.')
    return ('<div class="card" id="freshness"><div class="hd"><div><h2>How fresh the data is</h2><p class="sub">Every source, '
            'its date, and whether it is more than a quarter old</p></div></div><div class="bd scroll" style="padding-top:4px">'
            '<table class="fresh"><tr><th>Source</th><th>Data to</th><th>Age</th><th>Updates</th><th></th></tr>' + body +
            f'</table></div><div class="ft">Quarterly figures are flagged once a newer quarter has ended but not yet been reported.'
            f' Each metric below carries its own date.{nxt}</div></div>')


def _targets_card(res):
    ex = res["extra"]
    tg, gd = ex.get("targets") or [], ex.get("guidance") or []
    if not tg and not gd:
        return ""
    e = html.escape
    money = lambda v: ("-" if v < 0 else "+") + f"${abs(v):,.0f}M"
    parts = ""
    for t in tg:
        hist = ", ".join(f"{_qlabel(qe)} {money(v)}" for qe, v in t["history"])
        if t.get("projection") is not None:
            ok = t["projection"] >= 0
            line = (f"At the latest quarter's pace ({money(t['step'])} a quarter), {_qlabel(t['by'])} would come in at about "
                    f"{money(t['projection'])}. That is a straight line from the last two quarters, not a forecast.")
            badge = _badge("On pace" if ok else "Behind pace", "green" if ok else "amber")
        else:
            ok = t["latest"] >= 0
            line = (f"The target date has passed: reported {t['label']} is {money(t['latest'])} for {_qlabel(t['history'][-1][0])}.")
            badge = _badge("Reported positive" if ok else "Not yet positive", "green" if ok else "amber")
        parts += (f'<div class="tgt"><div class="tgt-hd"><b>{e(t["what"])}</b>{badge}</div>'
                  f'<p>{e(t["label"])} by quarter: {e(hist)}. {e(line)}</p>'
                  f'<p class="dim">Said in the {e(t["said"])}. "Twelve-month go-forward basis" is management\'s own measure and '
                  'isn\'t reported, so the page tracks the reported quarterly figure.</p></div>')
    parts += _guidance_html(ex)
    nc = ex.get("nowcast")
    foot = ""
    if nc and nc.get("guide_min_revenue"):
        foot = (f'<div class="ft">Before the {_qlabel(nc["quarter"])} report: this page\'s revenue estimate is '
                f'${nc["estimate"]:,.0f}M against guidance of ${nc["guide_min_revenue"]:,.0f}M or more. The gap is the revenue per home '
                f'the estimate assumes (${nc["rph_k"]:.0f}K); to meet guidance on {nc["homes"]:,.0f} homes, revenue per home '
                f'would need to be at least ${nc["guide_min_revenue"] / nc["homes"] * 1000:.0f}K.</div>')
    return ('<div class="card" id="targets"><div class="hd"><div><h2>Path to management\'s targets</h2><p class="sub">What '
            'Opendoor said it would do, and what it has reported</p></div></div><div class="bd">' + parts + '</div>' + foot + '</div>')


NEWSLETTER_USER = os.environ.get("BUTTONDOWN_USERNAME", "").strip()  # set as a repository variable to switch the signup on


def _how_to_read():
    """Short, collapsed explainer for first-time visitors. Weights and grade bands are read from PILLARS and GRADES."""
    tiers = {}
    for name, w, *_ in PILLARS:
        tiers.setdefault(w, []).append(name)
    def _and(xs):
        return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " and " + xs[-1]
    wtxt = "; ".join(f"{_and(n)} {w}%{' each' if len(n) > 1 else ''}" for w, n in sorted(tiers.items(), reverse=True))
    bands = ", ".join(f"{g} from {t:.1f}" for t, g, _ in GRADES if t > 0) + f", {GRADES[-1][1]} below"
    return ('<details class="howto"><summary>How to read this scorecard</summary>'
            '<p>Opendoor is rated on nine pillars, from growth and unit economics to dilution, valuation and the housing market. '
            'Each metric earns 0 to 10 points on a straight line between two published levels, and a pillar is the average of its metrics. '
            f'The overall score is the weighted average of the pillars ({html.escape(wtxt)}), '
            f'and the grade follows from it: {bands}. '
            'It is an independent summary of public numbers, not a forecast, a recommendation or investment advice.</p></details>')


def _own_history_card(res):
    """Latest reported quarter against Opendoor's own high and low over the quarters in the Quarterly results table.
    Context only: nothing here is scored."""
    q = res["quarters"]
    idx = {r["quarter_end"]: i for i, r in enumerate(q)}
    win = q[-8:]   # the same quarters as the Quarterly results table

    def days_inv(r):
        i = idx[r["quarter_end"]]
        p = q[i - 1] if i else None
        if not p or not 80 <= _days(p["quarter_end"], r["quarter_end"]) <= 100:
            return None
        if None in (p.get("inventory_musd"), r.get("inventory_musd"), r.get("revenue_musd"), r.get("gross_profit_musd")):
            return None
        cogs = r["revenue_musd"] - r["gross_profit_musd"]
        return (p["inventory_musd"] + r["inventory_musd"]) / 2 / cogs * _days(p["quarter_end"], r["quarter_end"]) if cogs > 0 else None
    specs = [("Contribution margin", lambda r: r.get("contribution_margin_pct"), lambda v: f"{v:.1f}%", True),
             ("Gross margin", lambda r: r["gross_profit_musd"] / r["revenue_musd"] * 100
              if r.get("gross_profit_musd") is not None and r.get("revenue_musd") else None, lambda v: f"{v:.1f}%", True),
             ("Days of inventory", days_inv, lambda v: f"{v:.0f} days", False),
             ("Homes listed 120+ days", lambda r: r.get("aged_120_pct"), lambda v: f"{v:.0f}%", False),
             ("Revenue, quarter", lambda r: r.get("revenue_musd"), lambda v: f"${v:,.0f}M", True)]
    e = html.escape
    rows = ""
    for name, get, f, up in specs:
        pts = [(r["quarter_end"], get(r)) for r in win]
        pts = [(d, v) for d, v in pts if v is not None]
        if len(pts) < 3:
            continue
        cur_d, cur = pts[-1]
        lo_d, lo = min(pts, key=lambda x: x[1])
        hi_d, hi = max(pts, key=lambda x: x[1])
        pos = 50 if hi == lo else (cur - lo) / (hi - lo) * 100
        best, worst = (hi, lo) if up else (lo, hi)
        tag = ("<em>best of the period</em>" if cur == best else "<em>worst of the period</em>" if cur == worst else "")
        rows += (f'<tr><td>{e(name)}<small>{len(pts)} quarters, {_qlabel(pts[0][0])} to {_qlabel(cur_d)}; '
                 f'{"higher" if up else "lower"} is better</small></td>'
                 f'<td data-l="Latest"><b>{f(cur)}</b><small>{_qlabel(cur_d)}{tag}</small></td>'
                 f'<td data-l="Low">{f(lo)}<small>{_qlabel(lo_d)}</small></td>'
                 f'<td data-l="High">{f(hi)}<small>{_qlabel(hi_d)}</small></td>'
                 f'<td class="oh-r" aria-hidden="true"><span class="oh-t"><i style="left:{pos:.0f}%"></i></span>'
                 f'<span class="oh-e"><span>low</span><span>high</span></span></td></tr>')
    if not rows:
        return ""
    return ('<div class="card" id="own-history"><div class="hd"><div><h2>Context vs Opendoor\'s own history</h2>'
            '<p class="sub">The latest reported quarter against Opendoor\'s own high and low over the quarters in the '
            'Quarterly results table. These are Opendoor\'s own past numbers, not peer comparisons, and are not scored.</p></div></div>'
            f'<div class="bd scroll" style="padding-top:4px"><table class="oh"><tr><th>Metric</th><th>Latest</th><th>Low</th>'
            f'<th>High</th><th>Where it sits</th></tr>{rows}</table></div>'
            '<div class="ft">Housing conditions already influence Unit economics and Execution; this is context only.</div></div>')


def _dashboard(res, change, change_sub, ospark, kpi_html):
    """The top of the page in one card: score and why it moved, data status, live numbers, the five biggest moves,
    and what the next report will change. Every figure here also appears, in full, further down the page."""
    e, s = html.escape, res["score"]
    tone = _tone(s["overall"])
    why_short = _why_moved(res, short=True)
    moved = f'<div class="d-moved"><p>{why_short}</p></div>' if why_short else ""
    pills = ""
    for _, short, asof, _, old, _ in _fresh_rows(res):
        if asof:
            pills += (f'<li><span class="d-src">{e(short)}<small>{e(asof)}</small></span>'
                      f'{_badge("Over a quarter old" if old else "Current", "amber" if old else "green")}</li>')
    lev = s.get("leverage") or {}
    by = {r["key"]: r for r in lev.get("rows", [])}
    tiles = ""
    for k in lev.get("top", []):
        r = by[k]
        tiles += (f'<div class="mv5-t"><p class="mv5-n">{e(NAMES.get(k, r["label"])[:1].upper() + NAMES.get(k, r["label"])[1:])}<small>{e(r["pillar"])}</small></p>'
                  f'<p class="mv5-v">{e(fmt(r["value"], r["fmt"]))}<small>{r["points"]:.1f} pts</small></p>'
                  f'<p class="mv5-f">Full marks at {e(fmt(r["good"], r["fmt"]))}</p><p class="mv5-l">+{r["to_full"]:.2f}</p></div>')
    gap_b = next((g for g in lev.get("grades", []) if g["grade"] == "B"), None)
    moves = (f'<div class="d-sec"><div class="d-h"><p class="d-k">Five biggest single moves</p>'
             f'<p class="d-hint">Overall score if each reached full marks, alone'
             + (f'. B starts at {gap_b["at"]:.2f}, {gap_b["gap"]:.2f} away' if gap_b else "")
             + ' <a href="#move">How the score moves</a></p></div><div class="mv5">' + tiles + '</div></div>') if tiles else ""
    ri = _results_info(res)
    call = ""
    if ri:
        n_sw = sum(len(g[2]) for g in ri["groups"])
        grp = ""
        for h, sub, rows in ri["groups"]:
            if not rows:
                continue
            li = "".join(f'<li{" class=\"rep\"" if r["now"][1] == "rep" else ""}><span class="q3-m">{e(r["name"])}<small>{e(r["pillar"])}</small></span>'
                         f'<span class="q3-v">{e(r["value"])}</span>'
                         + (f'<span class="q3-tag {r["now"][1]}">{e(r["now"][0])}</span>' if r["now"][1] != "rep" else "")
                         + (f'<span class="q3-ar" aria-hidden="true">&rarr;</span><span class="q3-tag to">{e(ri["next"])}</span>'
                            if r["now"][1] != "rep" else f'<span class="q3-tag rep">{e(r["now"][0])}</span>') + '</li>'
                         for r in rows)
            grp += (f'<div class="q3-g"><p class="q3-gh"><span class="q3-c">{len(rows)}</span>{e(h)}<small>{e(sub)}</small></p>'
                    f'<ul class="q3-l">{li}</ul></div>')
        intro = (f'{n_sw} of {ri["total"]} scored metrics switch from {e(ri["latest"])} figures'
                 + (' or this page\'s estimate' if any(r["now"][1] == "est" for g in ri["groups"] for r in g[2]) else "")
                 + f' to {e(ri["next"])} reported.' if ri["state"] != "done" else
                 f'Release metrics now use {e(ri["next"])} reported figures; {e(ri["when"])}.')
        call = (f'<div class="d-call q3 {"done" if ri["state"] == "done" else ""}" id="q3">'
                f'<div class="q3-hd"><p class="q3-t"><span class="rb-dot"></span>{e(ri["title"])}</p>'
                + (f'<span class="q3-when">{e(ri["when"])}</span>' if ri["state"] != "done" else "")
                + f'</div><p class="q3-i">{intro}</p><div class="q3-gs">{grp}</div>'
                + (f'<p class="q3-same"><b>Not affected:</b> {e(", ".join(ri["same"]))}, plus the tracker metrics, '
                   'which update weekly.</p>' if ri["same"] else "") + '</div>')
    return (f'<div class="card dash" id="score"><div class="d-top"><div class="d-main">'
            f'<p class="d-k">Overall score <span>weighted across nine pillars</span></p>'
            f'<div class="score"><span class="num">{s["overall"]:.2f}</span><span class="of">/ 10</span>'
            f'<span class="grade badge {tone}">{e(s["grade"])}</span>'
            f'<span class="d-chg">{change}<small>{e(change_sub)}</small></span>{ospark}</div>'
            f'<p class="verdict">{e(s["verdict"])}</p><p class="why">{e(s["why"])}</p>{moved}</div>'
            f'<div class="d-side"><p class="d-k">Data status</p><ul class="d-pills">{pills}</ul>'
            f'<a class="d-more" href="#freshness">Every source and its age</a></div></div>'
            f'<div class="kpis">{kpi_html}</div>{moves}{call}</div>')


def _subscribe_card():
    if not NEWSLETTER_USER:
        return ""
    u = html.escape(NEWSLETTER_USER)
    return (
        '<div class="nl" id="subscribe"><div class="nl-l"><p class="nl-k">Free newsletter</p>'
        '<h2>Get the score in your inbox</h2>'
        '<p class="nl-p">The grade, what moved and why, and what is coming up, written from the same numbers as this page.</p>'
        '<ul class="nl-list"><li>A round-up every Friday evening</li><li>An extra email the evening new results land</li>'
        '<li>No ads, no tips, unsubscribe in one click</li></ul></div>'
        f'<form class="nl-form" action="https://buttondown.com/api/emails/embed-subscribe/{u}" method="post" target="_blank">'
        '<p class="nl-what"><b>Weekly, on Fridays</b><span>Plus results nights, about four times a year</span></p>'
        '<div class="nl-row"><input type="email" name="email" required placeholder="you@example.com" aria-label="Email address" autocomplete="email">'
        '<button type="submit">Subscribe, free</button></div>'
        '<input type="hidden" name="embed" value="1">'
        '<p class="nl-ok" hidden>Nearly there: confirm in the tab that just opened, then click the link in the email we sent you.</p>'
        f'<p class="nl-fine">Sent with Buttondown. Your email is used for this newsletter only. '
        f'<a href="https://buttondown.com/{u}/archive" target="_blank" rel="noopener">Read past emails</a></p>'
        '</form></div>')


def _subscribe_strip():
    if not NEWSLETTER_USER:
        return ""
    return ('<a class="nl-strip" href="#subscribe"><span><b>Follow the score by email.</b> Weekly, free.</span>'
            '<span class="nl-go">Subscribe</span></a>')


STATUS_BADGE = {"met": ("Met", "green"), "beat": ("Beat", "green"), "close": ("Close", "amber"),
                "missed": ("Missed", "red"), "pending": ("Pending", "grey")}


def _guide_value(i):
    a = i["actual"]
    if a is None:
        return "–"
    if i["key"].endswith(("_qoq", "_yoy")):
        return f"{a * 100:+.1f}%"
    if i["key"] == "contribution_margin":
        return f"{a:.1f}%"
    if i["key"] == "contribution_profit_mult":
        return f"{a:.1f}x"
    return ("-" if a < 0 else "") + f"${abs(a):,.0f}M"


def _guidance_html(ex):
    gd, hr = ex.get("guidance") or [], ex.get("hit_rate")
    if not gd:
        return ""
    e = html.escape
    rows = ""
    for g in reversed(gd):
        rows += (f'<tr class="grp"><td colspan="3">{_qlabel(g["quarter"])} guidance <span class="dim">from the '
                 f'{e(g["said"])}</span></td></tr>')
        rows += "".join(f'<tr><td>{e(i["what"])}</td><td>{_guide_value(i)}</td><td>{_badge(*STATUS_BADGE[i["status"]])}</td></tr>'
                        for i in g["items"])
    head = ""
    if hr and hr["items"]:
        ok = hr["met"] + hr["beat"]
        head = (f'<p class="hit"><b>{ok} of {hr["items"]}</b> guided items met or beaten over the last {len(hr["quarters"])} '
                f'reported quarters ({_qlabel(hr["quarters"][0])} to {_qlabel(hr["quarters"][-1])}): {hr["beat"]} beaten, '
                f'{hr["met"]} met, {hr["close"]} close, {hr["missed"]} missed.</p>')
    return (f'<div class="tgt"><div class="tgt-hd"><b>Guidance track record</b></div>{head}'
            f'<table class="guide"><tr><th>What Opendoor said</th><th>Reported</th><th></th></tr>{rows}</table>'
            '<p class="dim">Met: inside the guided range (allowing for rounding). Beat: better than the range. Close: within half '
            'the range width (or twice the tolerance) on the wrong side. Guidance that reported quarterly figures can\'t check, '
            'such as a monthly exit rate, is left out.</p></div>')


def _fallout_card(res):
    fh = res["extra"].get("fallout") or []
    if not fh:
        return ""
    e = html.escape
    last = fh[-1]
    tone = "green" if last["rate"] <= 0.15 else ("amber" if last["rate"] <= 0.25 else "red")
    rows = "".join(f'<tr><td>{_qlabel(f["quarter"])}</td><td>{f["open_start"]:,.0f}</td><td>{f["signed"]:,.0f}</td>'
                   f'<td>{f["purchased"]:,.0f}</td><td>{f["open_end"]:,.0f}</td><td>{f["lost"]:,.0f}</td>'
                   f'<td><b>{f["rate"] * 100:.0f}%</b></td></tr>' for f in reversed(fh))
    py = [f for f in fh if "year-earlier" in f["source"]]
    return ('<div class="card" id="fallout"><div class="hd stack"><div><h2>Contracts that fell through</h2><p class="sub">Signed '
            'purchase contracts that never became a home bought</p></div>'
            f'<div class="right">{_badge(f"{last["rate"] * 100:.0f}%", tone)}<p class="sub">{_qlabel(last["quarter"])}, derived</p></div></div>'
            '<div class="bd scroll" style="padding-top:4px"><table class="fall"><tr><th>Quarter</th><th>Open at start</th>'
            '<th>Signed</th><th>Bought</th><th>Open at end</th><th>Fell through</th><th>Rate</th></tr>' + rows + '</table></div>'
            '<div class="ft">Worked out from Opendoor\'s own numbers: contracts open at the start of the quarter, plus contracts '
            'signed in it, less homes bought, less contracts still open at the end. What is left was cancelled or lapsed. "Open" '
            'counts are the homes under contract to purchase that each earnings release reports; "Signed" is the weekly tracker '
            'summed by the week-ending date, which Opendoor says is not reduced for later cancellations. The rate divides by '
            'everything open during the quarter. Week boundaries don\'t match quarter ends exactly, so treat each figure as '
            '+/- 1 to 2 points.'
            + (f' Quarters up to {_qlabel(py[-1]["quarter"])} use the tracker\'s "a year earlier" column.' if py else "")
            + '</div></div>')


def _ue_svg(h):
    W, H, L, R, T, B = 760, 200, 40, 10, 22, 30
    vals = [r["cm"] for r in h]
    top = max(7.0, max(vals) + 1)
    n = len(h)
    bw = (W - L - R) / n
    y = lambda v: T + (1 - v / top) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Contribution margin by quarter" class="uechart">']
    for t in range(0, int(top) + 1, 2):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{LINE}" '
                   f'stroke-dasharray="{"0" if t == 0 else "2 4"}"/><text x="{L - 8}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t}%</text>')
    for k, r in enumerate(h):
        x = L + k * bw + bw * 0.22
        w = bw * 0.56
        v = r["cm"]
        out.append(f'<rect x="{x:.1f}" y="{y(max(v, 0)):.1f}" width="{w:.1f}" height="{abs(y(v) - y(0)):.1f}" rx="4" '
                   f'fill="{BLUE}" opacity="{1 if k == n - 1 else 0.55}"/>'
                   f'<text x="{x + w / 2:.1f}" y="{y(max(v, 0)) - 6:.1f}" text-anchor="middle" class="val">{v:.1f}%</text>'
                   f'<text x="{x + w / 2:.1f}" y="{H - 10}" text-anchor="middle" class="tick">{_qlabel(r["quarter"])}</text>')
    out.append("</svg>")
    return "".join(out)


def _ue_card(res):
    h = res["extra"].get("ue_history") or []
    if len(h) < 2:
        return ""
    n = lambda v, f="{:,.0f}", pre="": "–" if v is None else pre + f.format(v)
    rows = ""
    for k in range(len(h) - 1, -1, -1):
        r = h[k]
        prev = h[k - 1]["cm"] if k else None
        chg = "–" if prev is None else f"{r['cm'] - prev:+.1f}pp"
        rows += (f'<tr><td>{_qlabel(r["quarter"])}</td><td><b>{r["cm"]:.1f}%</b></td><td>{chg}</td><td>{n(r["gm"], "{:.1f}%")}</td>'
                 f'<td>{n(r["cp_per_home_k"], "{:.1f}K", "$")}</td><td>{n(r["revenue_per_home_k"], "{:.0f}K", "$")}</td>'
                 f'<td>{n(r["days_inv"], "{:.0f}")}</td><td>{n(r["aged"], "{:.0f}%")}</td>'
                 f'<td>{n(r["selling_k"], "{:.1f}K", "$")}</td><td>{n(r["holding_k"], "{:.1f}K", "$")}</td>'
                 f'<td>{n(r["writedown_k"], "{:.1f}K", "$")}</td></tr>')
    return ('<div class="card" id="unit-history"><div class="hd"><div><h2>Unit economics, quarter by quarter</h2>'
            '<p class="sub">Contribution margin and what sits behind it</p></div></div>'
            f'<div class="bd"><div class="uebox">{_ue_svg(h)}</div></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="ue"><tr><th>Quarter</th><th>Contrib. margin</th><th>Change</th>'
            '<th>Gross margin</th><th>Contrib. / home</th><th>Revenue / home</th><th>Days of inventory</th><th>120+ days</th>'
            '<th>Selling costs / home</th><th>Holding costs / home</th><th>Write-downs / home</th></tr>' + rows + '</table></div>'
            '<div class="ft">Per home means per home sold in the quarter. Selling and holding costs and write-downs are the lines '
            'Opendoor itemises between gross profit and contribution profit (holding costs include those from earlier quarters on '
            'homes sold now; write-downs are this quarter\'s inventory valuation adjustment). Opendoor doesn\'t report renovation '
            'spend on its own: it is part of what each home costs, so it shows up in gross margin. Days of inventory: average '
            'inventory over the quarter divided by the cost of the homes sold, times the days in the quarter. A dash means the '
            'release for that quarter isn\'t parsed yet.</div></div>')


def _stress_card(res):
    s, ex = res["score"], res["extra"]
    base, combos = ex.get("stress") or {}, s.get("stress") or {}
    if not base:
        return ""
    e = html.escape
    rules = {i["key"]: i for p in s["pillars"] for i in p["metrics"]}
    boxes = "".join(f'<label class="tog"><input type="checkbox" value="{k}"> {e(base[k]["name"])}</label>'
                    for k in sorted(base, key="rhc".index))
    detail = ""
    for k in sorted(base, key="rhc".index):
        b = base[k]
        lines = "".join(f'<tr><td>{e(rules[key]["label"])}</td><td data-l="Today">{e(fmt(rules[key]["value"], rules[key]["fmt"]))}</td>'
                        f'<td data-l="Stressed">{e(fmt(v, rules[key]["fmt"]))}</td></tr>' for key, v in b["changes"] if key in rules)
        detail += (f'<tr class="grp"><td colspan="3">{e(b["name"])}</td></tr>{lines}'
                   f'<tr><td colspan="3" class="wtxt dim">{e(b["note"])}</td></tr>')
    data = json.dumps(combos, separators=(",", ":"))
    return ('<div class="card" id="stress"><div class="hd"><div><h2>Stress tests</h2><p class="sub">Tick one or more to see '
            'which way the score would move</p></div></div>'
            f'<div class="bd"><div class="togs">{boxes}</div>'
            f'<div class="stress-out" data-base="{s["overall"]:.2f}" data-combos=\'{html.escape(data)}\'>'
            '<p class="so-main">Nothing ticked: the score is as published.</p><p class="so-pil"></p></div></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="sx"><tr><th>What changes</th><th>Today</th><th>Stressed</th></tr>'
            + detail + '</table></div>'
            '<div class="ft">Each test re-runs the same scoring on the changed inputs and leaves everything else as it is. It shows '
            'direction and rough size, not a forecast.</div></div>')


PRESETS = [("This page", None),
           ("Equal", {n: 10 for n, *_ in PILLARS}),
           ("Business only", {"Growth": 20, "Unit economics": 20, "Profitability": 20, "Execution": 20, "Balance sheet": 20,
                              "Dilution": 0, "Valuation": 0, "Momentum": 0, "Housing market": 0}),
           ("Housing 10%", {"Growth": 15, "Unit economics": 12.5, "Profitability": 15, "Execution": 12.5, "Balance sheet": 15,
                            "Dilution": 10, "Valuation": 5, "Momentum": 5, "Housing market": 10})]


SCENARIO_INPUTS = [("rev_nowcast_yoy", "Revenue YoY, latest quarter"), ("contrib_margin", "Contribution margin"),
                   ("coe_vs_guide", "Homes sold vs Opendoor's expectation")]


def _scenario_card(res):
    """Weights and a few inputs, re-scored in the browser with the same thresholds and arithmetic as the page."""
    s, ex = res["score"], res["extra"]
    e = html.escape
    pending = res["quarters"][-1]["quarter_end"] < SCENARIOS["quarter"]
    cm_rows = [r for r in res["quarters"] if r.get("contribution_margin_pct") is not None]
    cm_last = cm_rows[-1]["contribution_margin_pct"] / 100 if cm_rows else None
    items = {i["key"]: i for p in s["pillars"] for i in p["metrics"]}
    data = {"published": s["overall"], "grades": GRADES, "pending": pending, "cmLast": cm_last,
            "pillars": [{"n": p["name"], "w": p["weight"], "s": p["score"],
                         "ms": [{"k": i["key"], "v": i["value"], "b": i["bad"], "g": i["good"]}
                                for i in p["metrics"] if i["score"] is not None]} for p in s["pillars"]]}
    ins = ""
    for k, label in SCENARIO_INPUTS:
        i = items.get(k)
        if not i or i["score"] is None:
            continue
        pub = i["value"] * 100
        ins += (f'<label class="sin"><span>{e(label)}<small>published {e(fmt(i["value"], i["fmt"]))}'
                f'{" (estimate)" if k == "rev_nowcast_yoy" and ex.get("nowcast") else ""}</small></span>'
                f'<span class="in-f"><input type="number" step="0.1" value="{pub:.1f}" data-k="{k}" data-pub="{pub:.4f}">%</span></label>')
    scen = [{"name": "Base", "note": "Today's published inputs", "values": {}}] + (SCENARIOS["presets"] if pending else [])
    sbtn = "".join(f'<button type="button" class="spre{" on" if k == 0 else ""}" data-v=\'{html.escape(json.dumps(x["values"]))}\' '
                   f'data-note="{e(x["note"])}">{e(x["name"])}</button>' for k, x in enumerate(scen))
    sliders = "".join(f'<div class="sl"><span class="sl-n">{e(p["name"])}<i>{_num(p["score"])}</i></span>'
                      f'<input type="range" min="0" max="30" step="0.5" value="{p["weight"]}" data-i="{k}" aria-label="{e(p["name"])} weight">'
                      f'<span class="sl-v">{p["weight"]:g}%</span></div>' for k, p in enumerate(s["pillars"]))
    wbtn = "".join(f'<button type="button" class="pre{" on" if w is None else ""}" data-w=\'{json.dumps(w) if w else ""}\'>{e(name)}</button>'
                   for name, w in PRESETS)
    derived = (f'<p class="dim in-note">A contribution margin you enter is treated as {_qlabel(SCENARIOS["quarter"])}\'s, so '
               f'"margin vs the quarter before" becomes it minus {_qlabel(cm_rows[-1]["quarter_end"])}\'s {cm_last * 100:.1f}%. '
               f'Homes sold vs expectation for {_qlabel(SCENARIOS["quarter"])} is already final on the tracker, so the presets leave it alone.</p>'
               if pending and cm_last is not None else "")
    return ('<div class="card" id="weights-play"><div class="hd stack"><div><h2>Scenario tool</h2><p class="sub">Change a few '
            'inputs or the pillar weights; the same thresholds and arithmetic re-score it here. The published score doesn\'t change.</p></div>'
            f'<div class="right"><span class="wp-score">{s["overall"]:.2f}</span><p class="sub wp-grade">grade {e(s["grade"])}, as published</p></div></div>'
            f'<div class="bd" data-scen=\'{html.escape(json.dumps(data, separators=(",", ":")))}\'>'
            f'<p class="tool-h">Inputs</p><div class="presets">{sbtn}</div><p class="scen-note">Today\'s published inputs</p>'
            f'<div class="ins">{ins}</div>{derived}'
            f'<p class="tool-h">Pillar weights</p><div class="presets">{wbtn}</div><div class="sliders">{sliders}</div>'
            '<p class="sc-pil"></p></div>'
            '<div class="ft">Presets are hypothetical inputs set around Opendoor\'s own guidance, not forecasts, and disappear once '
            'that quarter is reported. Weights are scaled to add up to 100%, so only their sizes relative to each other matter. '
            'A pillar with no score is left out, as on the rest of the page.</div></div>')


# ---------------------------------------------------------------- what would move the score, results banner, sparklines
SHORT = {"rev_nowcast_yoy": "revenue YoY, replacing the estimate", "contrib_margin": "contribution margin",
         "contrib_trend": "margin vs the quarter before", "gross_margin": "gross margin", "aged_inventory": "homes listed 120+ days",
         "days_inventory": "days of inventory", "net_margin_2q": "GAAP net margin", "adj_ebitda_m": "Adj. EBITDA margin",
         "ani_margin": "Adj. net income margin", "fallout": "contracts that fell through", "ps": "price/sales",
         "cash_vs_notes": "cash vs notes", "home_debt_ltv": "home-backed debt", "runway_q": "cash runway",
         "share_yoy": "share count", "sbc_pct": "stock pay", "dilution_live": "potential new shares", "pb": "price/book"}


def _delta(a, b, kind):
    """The change from a to b in the metric's own units."""
    d = b - a
    if kind in ("pct", "share", "rate", "pp"):
        return f"{d * 100:+.1f}pp"
    if kind == "x":
        return f"{d:+.2f}x"
    if kind == "days":
        return f"{d:+.0f} days"
    if kind == "qtrs":
        return f"{d:+.1f} qtrs"
    return f"{d:+.2f}"


def _grade_of(v):
    return next(g for t, g, _ in GRADES if v >= t)


def _move_card(res):
    s = res["score"]
    lev = s.get("leverage")
    if not lev:
        return ""
    e = html.escape
    by = {r["key"]: r for r in lev["rows"]}
    gaps = " ".join(f'<span class="gap"><b>{g["grade"]}</b> starts at {g["at"]:.2f}: <b>{g["gap"]:.2f}</b> away</span>'
                    for g in lev["grades"])
    top = [by[k] for k in lev["top"]]
    rows = "".join(
        f'<tr><td class="wtxt">{e(r["label"])}<small>{e(r["pillar"])}</small></td>'
        f'<td data-l="Now">{e(fmt(r["value"], r["fmt"]))}<small>{r["points"]:.1f} pts</small></td>'
        f'<td data-l="Full marks at">{e(fmt(r["good"], r["fmt"]))}</td>'
        f'<td data-l="Change needed">{e(_delta(r["value"], r["good"], r["fmt"]))}</td>'
        f'<td data-l="Overall"><b>+{r["to_full"]:.2f}</b></td>'
        f'<td data-l="Each point">{e(_delta(0, r["step"], r["fmt"]).lstrip("+"))} = +{r["per_point"]:.3f}</td></tr>' for r in top)
    tog = lev["top_together"]
    gap_b = next((g["gap"] for g in lev["grades"] if g["grade"] == "B"), None)
    alone = max((r["to_full"] for r in top), default=0)
    lines = [f'All five together: {s["overall"]:.2f} to <b>{tog:.2f}</b>, grade {_grade_of(tog)}.']
    if gap_b is not None and alone < gap_b:
        lines.append(f'No single metric closes the {gap_b:.2f} gap to B on its own.')
    allrows = ""
    for p in s["pillars"]:
        for r in [x for x in lev["rows"] if x["pillar"] == p["name"]]:
            full = "–" if r["to_full"] is None else "+%.2f" % r["to_full"]
            allrows += (f'<tr><td class="wtxt">{e(r["label"])}</td><td>{e(r["pillar"])}</td><td>{r["points"]:.1f}</td>'
                        f'<td>{r["per_point"]:.3f}</td><td>{r["contribution"]:.2f}</td><td>{full}</td></tr>')
    total = sum(r["contribution"] for r in lev["rows"])
    note = ("" if abs(total - s["overall"]) < 0.005 else
            f' They add up to {total:.2f}; the published {s["overall"]:.2f} differs only because pillar scores are rounded to one decimal first.')
    return ('<div class="card" id="move"><div class="hd"><div><h2>What would move the score</h2>'
            f'<p class="sub">Where the {s["overall"]:.2f} comes from, and the moves that would add most</p></div></div>'
            f'<div class="bd"><p class="gaps"><span class="gap">Now <b>{s["overall"]:.2f}</b>, grade {e(s["grade"])}</span>{gaps}</p>'
            '<p class="mv-h">The five biggest single moves</p></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="mv"><tr><th>Metric</th><th>Now</th><th>Full marks at</th>'
            '<th>Change needed</th><th>Overall</th><th>Each point</th></tr>' + rows + '</table>'
            f'<p class="mv-sum">{" ".join(lines)}</p>'
            '<details class="mv-all"><summary>Every metric\'s share of the score</summary><div class="scroll"><table class="mv2">'
            '<tr><th>Metric</th><th>Pillar</th><th>Points</th><th>Overall per point</th><th>Supplies</th><th>At full marks</th></tr>'
            + allrows + f'</table></div><p class="dim">A metric supplies its points x its pillar\'s weight, shared between the '
            f'pillar\'s scored metrics.{note}</p></details></div>'
            '<div class="ft">"Overall" re-runs the scoring with that one metric at its 10-point value and everything else '
            'unchanged. "Each point" is the change in the metric that earns one more point, and what that adds to the overall. '
            'The list ranks arithmetic, not likelihood: some inputs, such as the share price, are not in management\'s hands.</div></div>')


def _results_info(res):
    """State of the next report and which scored metrics it moves: None, or a dict for the dashboard callout.
    Each row says where a metric's number comes from now and where it will come from once the report is in."""
    nr = NEXT_RESULTS
    if not nr.get("quarter"):
        return None
    today = res["generated"][:10]
    latest = res["quarters"][-1]["quarter_end"]
    ql, ll = _qlabel(nr["quarter"]), _qlabel(latest)
    done = latest >= nr["quarter"]
    if done and _days(nr["date"], today) > 10:
        return None
    if not done and _days(nr["date"], today) > 21:
        return None
    bs_in = ((res["extra"].get("pro_forma") or {}).get("as_of") or "") >= nr["quarter"]
    has_est = bool(res["extra"].get("nowcast")) and not done
    pil = {p["name"]: [i for i in p["metrics"] if i["score"] is not None] for p in res["score"]["pillars"]}

    pf_q = (res["extra"].get("pro_forma") or {}).get("as_of")
    bs_q = _qlabel(pf_q or _prev_qend(latest)) if done and not bs_in else ll

    def rows(keys, reported):
        out = []
        for name, items in pil.items():
            for i in items:
                if i["key"] not in keys:
                    continue
                n = NAMES.get(i["key"], i["label"])
                now = ("Estimate", "est") if i["key"] == "rev_nowcast_yoy" and has_est else (bs_q if done else ll, "old")
                out.append({"name": n[:1].upper() + n[1:], "pillar": name, "value": fmt(i["value"], i["fmt"]),
                            "now": (f"{ql} reported", "rep") if reported else now})
        return out
    same = [n for n, items in pil.items() if items and not any(i["key"] in FROM_RELEASE + FROM_10Q for i in items)]
    total = sum(len(v) for v in pil.values())
    if done:
        when = ("balance sheet, share count and stock pay are updated too (10-Q filed)" if bs_in else
                "balance sheet, share count and stock pay switch when the 10-Q is filed, usually within days")
        return {"state": "done", "title": f"{ql} results are in the score", "when": when, "latest": ll, "total": total,
                "same": same, "next": ql,
                "groups": [("Earnings release", "in the score now", rows(FROM_RELEASE, True)),
                           ("10-Q", "in the score now" if bs_in else "next, within days", rows(FROM_10Q, bs_in))]}
    when = ("Tonight, about 21:00 UK" if today == nr["date"] else
            f"{_d(nr['date'])}, about 21:00 UK" if today < nr["date"] else
            f"Due {_d(nr['date'])}; first evening run after the SEC filing")
    return {"state": "upcoming" if today <= nr["date"] else "due", "title": f"What updates with {ql} results",
            "when": when, "latest": ll, "total": total, "same": same, "next": ql,
            "groups": [("Earnings release", "first evening run after it", rows(FROM_RELEASE, False)),
                       ("10-Q", "often a few days later", rows(FROM_10Q, False))]}


def _results_banner(res):
    nr = NEXT_RESULTS
    if not nr.get("quarter"):
        return ""
    e = html.escape
    today = res["generated"][:10]
    latest = res["quarters"][-1]["quarter_end"]
    ql = _qlabel(nr["quarter"])
    if latest >= nr["quarter"]:
        if _days(nr["date"], today) > 10:
            return ""
        bs_in = ((res["extra"].get("pro_forma") or {}).get("as_of") or "") >= nr["quarter"]
        return (f'<div class="rb done"><span class="rb-dot"></span><p><b>{ql} results are in the score.</b> '
                + ("The balance sheet, share count and stock pay are updated too (10-Q filed)." if bs_in else
                   "Balance sheet, share count and stock pay still use the previous quarter until the 10-Q is filed, "
                   "usually within days.") + '</p></div>')
    if _days(nr["date"], today) > 21:
        return ""
    pil = {p["name"]: [i for i in p["metrics"] if i["score"] is not None] for p in res["score"]["pillars"]}

    def group(keys):
        out = []
        for name, items in pil.items():
            hit = [i for i in items if i["key"] in keys]
            if hit:
                out.append(f"{name} ({'all ' + str(len(hit)) if len(hit) == len(items) and len(hit) > 1 else ', '.join(SHORT.get(i['key'], i['label']) for i in hit)})")
        return "; ".join(out)
    same = [n for n, items in pil.items() if items and not any(i["key"] in FROM_RELEASE + FROM_10Q for i in items)]
    when = (f"due after the US close on {_d(nr['date'])} (about 21:00 UK)" if today <= nr["date"] else
            f"were due on {_d(nr['date'])}; they reach the score on the first evening run after the release is on the SEC site")
    return (f'<div class="rb"><span class="rb-dot"></span><div><p><b>{ql} results {when}.</b> Until then the quarterly '
            f'figures below are {_qlabel(latest)}, marked "over a quarter old".</p><ul>'
            f'<li><b>With the earnings release</b> (the first evening run after it): {e(group(FROM_RELEASE))}.</li>'
            f'<li><b>With the 10-Q</b>, often filed a few days later: {e(group(FROM_10Q))}.</li>'
            f'<li><b>Not affected:</b> {e(", ".join(same))}, and the tracker-based metrics (homes sold, contracts, sale speed), '
            'which update weekly.</li></ul></div></div>')


def _fmtv(v, kind):
    """A value as the sparkline tooltips show it."""
    if kind == "pctv":   # already in percent
        return f"{v:.1f}%"
    if kind == "days":
        return f"{v:.0f} days"
    if kind == "x":
        return f"{v:.2f}x"
    if kind == "pct":
        return f"{v * 100:+.1f}%"
    return f"{v:.2f}"


def _mini(values, caption, labels=None, kind=""):
    """A tiny sparkline with its caption; each point shows its date and exact value on hover. With fewer than two
    points, just the caption."""
    pairs = [(labels[k] if labels else "", v) for k, v in enumerate(values) if v is not None]
    if len(pairs) < 2:
        return f'<span class="mini"><i>{html.escape(caption)}</i></span>'
    W, H, P = 84, 20, 3
    vals = [v for _, v in pairs]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    pts = [(P + k * (W - 2 * P) / (len(vals) - 1), H - P - (v - lo) / span * (H - 2 * P)) for k, v in enumerate(vals)]
    x, y = pts[-1]
    dots = "".join(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="4" fill="transparent"><title>{html.escape(l)}: {_fmtv(v, kind)}</title></circle>'
                   for (a, b), (l, v) in zip(pts, pairs))
    summary = ", ".join(f"{l} {_fmtv(v, kind)}" for l, v in pairs)
    return (f'<span class="mini"><svg viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{html.escape(summary)}">'
            f'<polyline points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}" fill="none" stroke="{BLUE}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/><circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="{BLUE}"/>{dots}</svg>'
            f'<i>{html.escape(caption)}</i></span>')


def _series(res):
    """Where each sparkline comes from: quarterly figures, the weekly tracker, or this page's own run history.
    Each entry: (values, caption, point labels, value kind)."""
    ex, acc = res["extra"], res.get("accountable") or {}
    mh = res.get("metric_history") or []
    out = {}
    ue = (ex.get("ue_history") or [])[-6:]
    if ue:
        out["contrib_margin"] = ([r["cm"] for r in ue], f"{_qlabel(ue[0]['quarter'])} to {_qlabel(ue[-1]['quarter'])}, by quarter",
                                 [_qlabel(r["quarter"]) for r in ue], "pctv")
        di = [r for r in ue if r.get("days_inv") is not None]
        if di:
            out["days_inventory"] = ([r["days_inv"] for r in di], f"{_qlabel(di[0]['quarter'])} to {_qlabel(di[-1]['quarter'])}, by quarter",
                                     [_qlabel(r["quarter"]) for r in di], "days")
    cy = acc.get("coe_yoy_series") or []
    if cy:
        out["coe_q4_yoy"] = ([v for _, v in cy], "quarter to date vs a year earlier, week by week (tracker)",
                             [_d(d) for d, _ in cy], "pct")
    cv = acc.get("contracts_vs_proj_series") or []
    if cv:
        out["contracts_vs_proj"] = ([v for _, v in cv], "rolling 4 weeks, last 13 weeks", [_d(d) for d, _ in cv], "pct")
    # figures that only exist run by run: from this page's own daily metric history
    for k in ("cash_vs_notes", "ps", "pb"):
        rows = [(r["run_date"], float(r[k])) for r in mh if r.get(k) not in (None, "")]
        cap = (f"daily, since {_d(rows[0][0])}" if len(rows) > 1 else
               f"history starts {_d(rows[0][0])}" if rows else "history builds daily")
        out[k] = ([v for _, v in rows], cap, [_d(d) for d, _ in rows], "x")
    return out


def _score_chart(res):
    """Overall score, one point per weekday, over shaded grade bands; hover or tap for the exact values."""
    hist = [h for h in (res.get("history") or []) if h.get("overall") not in (None, "", "None")]
    s = res["score"]
    pts = [{"d": h["run_at"][:10], "v": float(h["overall"]), "g": h.get("grade") or _grade_of(float(h["overall"])),
            "p": float(h["price"]) if h.get("price") not in (None, "", "None") else None, "m": h.get("method") or METHOD}
           for h in hist]
    W, H, L, R, T, B = 1000, 260, 34, 40, 14, 30
    n = len(pts)
    x = lambda i: L + (i * (W - L - R) / (n - 1) if n > 1 else (W - L - R) / 2)
    y = lambda v: T + (1 - v / 10) * (H - T - B)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" class="hchart" role="img" aria-label="Overall score over time, one point per weekday">']
    # grade bands, faint, labelled on the right
    bands = [(8.0, 10.0, "A"), (6.5, 8.0, "B"), (5.0, 6.5, "C"), (3.5, 5.0, "D"), (0.0, 3.5, "F")]
    for lo, hi, g in bands:
        out.append(f'<rect x="{L}" y="{y(hi):.1f}" width="{W - L - R}" height="{y(lo) - y(hi):.1f}" fill="{"#f6f4f1" if g in "ACF" else "#fbfaf8"}"/>'
                   f'<text x="{W - R + 10}" y="{(y(lo) + y(hi)) / 2 + 4:.1f}" class="tick">{g}</text>')
    for t in (0, 2.5, 5, 7.5, 10):
        out.append(f'<line x1="{L}" x2="{W - R}" y1="{y(t):.1f}" y2="{y(t):.1f}" stroke="{LINE}" stroke-dasharray="{"0" if t in (0, 10) else "2 4"}"/>'
                   f'<text x="{L - 8}" y="{y(t) + 4:.1f}" text-anchor="end" class="tick">{t:g}</text>')
    xy = [(x(i), y(p["v"])) for i, p in enumerate(pts)]
    if n > 1:
        line = _smooth(xy) if n > 2 else "M" + " L".join(f"{a:.1f},{b:.1f}" for a, b in xy)
        out.append(f'<path d="{line}" fill="none" stroke="{BLUE}" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/>')
    for (a, b), p in zip(xy, pts):
        out.append(f'<circle cx="{a:.1f}" cy="{b:.1f}" r="4" fill="#fff" stroke="{BLUE}" stroke-width="2"/>')
    if pts:
        out.append(f'<text x="{L}" y="{H - 8}" class="tick">{_d(pts[0]["d"])}</text>')
        if n > 1:
            out.append(f'<text x="{W - R}" y="{H - 8}" text-anchor="end" class="tick">{_d(pts[-1]["d"])}</text>')
    out.append(f'<line class="hx" x1="0" x2="0" y1="{T}" y2="{H - B}" stroke="{INK}" stroke-opacity=".25" visibility="hidden"/>'
               f'<circle class="hdot" r="6" fill="{BLUE}" stroke="#fff" stroke-width="2" visibility="hidden"/></svg>')
    data = [[round(a, 1), round(b, 1), _d(p["d"]), p["v"], p["g"], p["p"], p["m"]] for (a, b), p in zip(xy, pts)]
    tone = _tone(s["overall"])
    return ('<div class="card" id="score-history"><div class="hd"><div><h2>Overall score over time</h2>'
            '<p class="sub">One point per weekday, over the grade bands</p></div>'
            f'<div class="right"><span class="hnow">{s["overall"]:.2f}</span> {_badge(s["grade"], tone)}<p class="sub">today</p></div></div>'
            f'<div class="bd"><div class="hbox" data-h=\'{html.escape(json.dumps(data, separators=(",", ":")))}\' data-w="{W}">'
            + "".join(out) + '<div class="htip" hidden></div></div>'
            '<p class="hnote">History builds daily. Thresholds and weights are judgment calls and have not been back-tested '
            'against returns.</p></div></div>')


def _short_line(res):
    si = res.get("short_interest")
    if not si or not si.get("shares_short"):
        return ""
    sh = (res["extra"].get("shares_m") or 0) * 1e6
    base, what = (si["float_shares"], "of float") if si.get("float_shares") else (sh, "of shares outstanding (float not available)")
    pct = f", {si['shares_short'] / base * 100:.1f}% {what}" if base else ""
    prev = si.get("previous") or {}
    chg = (f"; {(si['shares_short'] / prev['shares_short'] - 1) * 100:+.0f}% since {_d(prev['settlement'])}"
           if prev.get("shares_short") else "")
    dtc = f", {si['days_to_cover']:.1f} days to cover" if si.get("days_to_cover") is not None else ""
    return (f'<div class="si"><b>Short interest</b> <span class="ns">not scored</span><br>{si["shares_short"] / 1e6:,.1f}M shares'
            f'{pct}{dtc}{chg}.<small>Settlement {_d(si["settlement"])}; {html.escape(si.get("source", ""))}'
            + (f', float from {html.escape(si["float_source"])}' if si.get("float_shares") else "")
            + '. Published twice a month, about a week after each settlement date.</small></div>')


def _method_changes_card():
    items = "".join(f'<li><b>v{v}, {_d(d)}.</b> {html.escape(t)}</li>' for v, d, t in reversed(METHOD_CHANGES))
    return ('<div class="card" id="method-changes"><div class="hd"><div><h2>Method changes</h2><p class="sub">What changed in '
            'the scoring, and when. The run history marks each break with the method version.</p></div></div>'
            f'<div class="bd"><ul class="mc">{items or "<li>None yet.</li>"}</ul></div></div>')


# ---------------------------------------------------------------- analyst view
def _bn(v):
    sign, v = ("-" if v < 0 else ""), abs(v)
    return sign + (f"${v / 1000:,.2f}bn" if v >= 1000 else f"${v:,.0f}M")


def _track_card(res):
    """The page's own revenue estimates against what Opendoor then reported, plus a backtest of the same method."""
    e = html.escape
    log, bt = res.get("estimates_log") or {}, res["extra"]["analyst"]["backtest"]
    live = ""
    for qe, x in sorted(log.items(), reverse=True):
        if x.get("reported") is not None:
            f = x["final"]
            live += (f'<tr><td>{_qlabel(qe)}</td><td data-l="Our estimate">${f["revenue"]:,.0f}M<small>{_d(f["date"])}</small></td>'
                     f'<td data-l="Guidance">{f"at least ${x['guide_min']:,.0f}M" if x.get("guide_min") else "–"}</td>'
                     f'<td data-l="Reported">${x["reported"]:,.0f}M</td><td data-l="Error"><b>{x["error"] * 100:+.1f}%</b></td></tr>')
        elif x.get("estimates"):
            f = x["estimates"][-1]
            live += (f'<tr><td>{_qlabel(qe)}</td><td data-l="Our estimate">${f["revenue"]:,.0f}M<small>{_d(f["date"])}, '
                     f'{len(x["estimates"])} so far</small></td>'
                     f'<td data-l="Guidance">{f"at least ${x['guide_min']:,.0f}M" if x.get("guide_min") else "–"}</td>'
                     f'<td data-l="Reported">pending</td><td data-l="Error">–</td></tr>')
    settled = [x for x in log.values() if x.get("reported") is not None]
    live_note = (f'Average miss so far: {sum(abs(x["error"]) for x in settled) / len(settled) * 100:.1f}% over {len(settled)} '
                 f'quarter{"s" if len(settled) != 1 else ""}.' if settled else
                 "The live record starts with Q3 2026: the estimate is saved on every run until the results are out, "
                 "and the last one made before them is the one that counts.")
    btr = "".join(f'<tr><td>{_qlabel(b["quarter"])}</td><td data-l="Homes sold">{b["homes"]:,.0f}</td>'
                  f'<td data-l="Revenue per home">${b["rph_k"]:,.0f}K</td><td data-l="Estimate">${b["estimate"]:,.0f}M</td>'
                  f'<td data-l="Reported">${b["reported"]:,.0f}M</td><td data-l="Error"><b>{b["error"] * 100:+.1f}%</b></td></tr>'
                  for b in reversed(bt))
    mae = sum(abs(b["error"]) for b in bt) / len(bt) * 100 if bt else None
    return ('<div class="card" id="track"><div class="hd"><div><h2>Estimate track record</h2><p class="sub">This page\'s revenue '
            'estimate before each report, against what Opendoor then reported</p></div>'
            + (f'<div class="right">{_badge(f"{mae:.1f}%", "green" if mae < 3 else "amber")}<p class="sub">average miss, backtest</p></div>' if mae is not None else "")
            + '</div><div class="bd"><p class="mv-h" style="margin-top:0">Live</p></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="trk"><tr><th>Quarter</th><th>Our estimate</th><th>Guidance</th>'
            f'<th>Reported</th><th>Error</th></tr>{live or "<tr><td colspan=5>No estimates yet.</td></tr>"}</table>'
            f'<p class="dim">{e(live_note)}</p></div>'
            '<div class="bd"><p class="mv-h" style="margin-top:0">Backtest of the same method</p></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="trk"><tr><th>Quarter</th><th>Homes sold</th><th>Revenue per home</th>'
            f'<th>Estimate</th><th>Reported</th><th>Error</th></tr>{btr}</table></div>'
            '<div class="ft">Method: homes sold in the quarter (from Opendoor\'s tracker, before the report) x revenue per home sold '
            'over the two quarters before. The backtest uses the homes sold Opendoor later reported, so it tests only the '
            'revenue-per-home assumption; the live record also carries any gap between the tracker and the reported count. '
            'Errors are estimate vs reported: positive means the estimate was too high.</div></div>')


def _implied_card(res):
    """What today's market value assumes, worked backwards; every assumption is editable."""
    e = html.escape
    an = res["extra"]["analyst"]
    ps = an.get("per_share") or {}
    mcap, ttm = an.get("market_cap_musd"), an.get("ttm_revenue_musd")
    if not mcap or not ttm:
        return ""
    net_cash = next((r["musd"] for r in ps.get("rows", []) if r["label"].startswith("Cash less")), None)
    yr = an["year_revenue"]
    peak = max(yr.items(), key=lambda kv: kv[1]) if yr else None
    last_y = max(yr) if yr else None
    data = {"mcap": mcap, "ttm": ttm, "net": net_cash or 0, "price": an["price"], "date": an["price_date"]}
    year0 = int(an["price_date"][:4])
    years = "".join(f'<option value="{y}"{" selected" if y == year0 + 3 else ""}>{y}</option>' for y in range(year0 + 2, year0 + 6))
    scale = (f'For scale: revenue was {_bn(peak[1])} in {peak[0]}, the highest calendar year in the data, and '
             f'{_bn(yr[last_y])} in {last_y}.' if peak else "")
    return ('<div class="card" id="implied"><div class="hd"><div><h2>What today\'s price implies</h2><p class="sub">Worked '
            'backwards from the share price: the revenue Opendoor would need, on assumptions you can change</p></div></div>'
            f'<div class="bd" data-imp=\'{html.escape(json.dumps(data))}\'>'
            '<div class="imp-in">'
            f'<label class="sin"><span>Year<small>when the business is judged</small></span><span class="in-f"><select data-k="year">{years}</select></span></label>'
            '<label class="sin"><span>Return wanted<small>a year, from today\'s price</small></span><span class="in-f"><input type="number" step="0.5" value="12" data-k="r">%</span></label>'
            '<label class="sin"><span>Net margin then<small>net income / revenue</small></span><span class="in-f"><input type="number" step="0.5" value="2" data-k="m">%</span></label>'
            '<label class="sin"><span>Earnings multiple then<small>price / earnings</small></span><span class="in-f"><input type="number" step="1" value="20" data-k="pe">x</span></label>'
            '</div>'
            f'<label class="imp-cash"><input type="checkbox" data-k="cash" checked> Count today\'s cash less the convertible notes '
            f'({_bn(net_cash) if net_cash is not None else "n/a"}) as still there</label>'
            '<p class="imp-out"></p>'
            f'<p class="dim">{e(scale)}</p>'
            '<p class="mv-h">Revenue needed, by margin and multiple</p></div>'
            '<div class="bd scroll" style="padding-top:0"><table class="imp-grid"></table></div>'
            '<div class="ft">Starts from today\'s market value (price x pro-forma shares), grows it at the return you want to the '
            'end of the year you pick, takes off the cash if you count it, and divides by the multiple and the margin. It leaves '
            'out further dilution and cash burn before then, both of which would raise the bar. A way of reading the price, '
            'not a forecast or a price target.</div></div>')


def _per_share_card(res):
    ps = res["extra"]["analyst"].get("per_share")
    if not ps:
        return ""
    e = html.escape
    money = lambda v: "–" if v is None else ("-" if v < 0 else "") + f"${abs(v):,.2f}"
    rows = "".join(f'<tr><td class="wtxt">{e(r["label"])}</td><td data-l="Total">{"–" if r["musd"] is None else _bn(r["musd"])}</td>'
                   f'<td data-l="Per share">{money(r["basic"])}</td><td data-l="Per share, diluted">{money(r["diluted"])}</td></tr>'
                   for r in ps["rows"])
    return ('<div class="card" id="per-share"><div class="hd"><div><h2>Per share</h2><p class="sub">The balance sheet, per share, '
            f'next to the ${ps["price"]:.2f} share price</p></div></div>'
            '<div class="bd scroll" style="padding-top:4px"><table class="psh"><tr><th>What</th><th>Total</th>'
            f'<th>Per share<small>{ps["shares_m"]:,.1f}M shares</small></th><th>Diluted<small>{ps["diluted_m"]:,.1f}M shares</small></th></tr>' + rows + '</table></div>'
            f'<div class="ft">From the {_qlabel(ps["as_of"])} balance sheet, adjusted for the capital deals since (see Details). '
            'Diluted adds the potential new shares that are live at today\'s price. '
            'Equity in homes is what they are carried at less the debt secured on them, before selling costs or any fall in '
            'prices (see the stress table in Details).</div></div>')


NAMES = {"rev_nowcast_yoy": "revenue YoY", "coe_q4_yoy": "homes sold this quarter vs a year ago",
         "contracts_qoq": "contracts vs the quarter before", "contrib_margin": "contribution margin",
         "contrib_trend": "margin vs the quarter before", "days_inventory": "days of inventory", "gross_margin": "gross margin",
         "aged_inventory": "homes listed 120+ days", "net_margin_2q": "GAAP net margin", "adj_ebitda_m": "Adj. EBITDA margin",
         "ani_margin": "Adj. net income margin", "coe_vs_guide": "homes sold vs Opendoor's expectation",
         "contracts_vs_proj": "contracts vs projection", "clearance": "sale speed vs the market",
         "fallout": "contracts that fell through", "cash_vs_notes": "cash vs notes", "home_debt_ltv": "home-backed debt vs homes",
         "runway_q": "cash runway", "share_yoy": "share count vs a year ago", "sbc_pct": "stock pay / revenue",
         "dilution_live": "potential new shares live", "ps": "price / sales", "pb": "price / book", "ret_1y": "1-year return",
         "range_pos": "place in the 52-week range", "vs_ma200": "price vs 200-day average", "mortgage_rate": "mortgage rate",
         "mortgage_13w": "mortgage rate change over 13 weeks", "dom_yoy": "days on market vs a year ago",
         "cuts_yoy": "price cuts vs a year ago", "list_price_yoy": "asking prices vs a year ago"}


def _why_moved(res, short=False):
    """One short paragraph under the score: how much it moved since the previous day, which pillars did it and the
    metrics behind them, and what new data arrived. Built only from this run and the previous day's run."""
    prev, s = res.get("previous") or {}, res["score"]
    ch = res.get("change")
    if not prev or not ch:
        return ""
    e = html.escape
    since = _d(prev.get("generated") or ch["since"])
    if ch.get("method_changed"):
        if short:
            return (f'<span class="mv-dir flat">Method updated</span> since {since}: the scoring changed (v{METHOD}), '
                    'so this is not a like-for-like move.')
        return (f'<p class="moved"><b>Why it changed.</b> The scoring method was updated (v{METHOD}) since {since}, '
                'so the change is not like-for-like. See Method changes.</p>')
    # values from the previous run: the snapshot, or the metric history for that day
    pv = prev.get("values") or {}
    if not pv:
        day = (prev.get("generated") or "")[:10]
        row = next((r for r in res.get("metric_history") or [] if r.get("run_date") == day), {})
        pv = {k: float(v) for k, v in row.items() if k not in ("run_date", "overall") and v not in ("", None)}
    pts = prev.get("points") or {}
    wsum = s.get("weights_used") or 100
    moved = []
    for p in s["pillars"]:
        a = (prev.get("pillars") or {}).get(p["name"])
        if a is None:
            a = (ch.get("pillars") or {}).get(p["name"])
            a = None if a is None or p["score"] is None else p["score"] - a
        if a is None or p["score"] is None or abs(p["score"] - a) < 0.05:
            continue
        mets = []
        for i in p["metrics"]:
            o = pts.get(i["key"])
            if o is None or i["score"] is None or abs(i["score"] - o) < 0.05:
                continue
            ov = pv.get(i["key"])
            what = (f'{NAMES.get(i["key"], i["label"])} {fmt(ov, i["fmt"])} to {fmt(i["value"], i["fmt"])}'
                    if ov is not None else NAMES.get(i["key"], i["label"]))
            mets.append((abs(i["score"] - o), f'{what} ({i["score"] - o:+.1f} pts)', what))
        mets = sorted(mets, reverse=True)[:2]
        moved.append((abs(p["score"] - a) * p["weight"] / wsum,
                      f'{p["name"]} {a:.1f} to {p["score"]:.1f}' + (f': {", ".join(t for _, t, _ in mets)}' if mets else ""),
                      f'{p["name"]} {a:.1f} to {p["score"]:.1f}', mets[0][2] if mets else ""))
    d = ch["overall"]
    tone = "flat" if abs(d) < 0.005 else ("up" if d > 0 else "down")
    word = "Unchanged" if tone == "flat" else ("Up" if d > 0 else "Down") + f" {abs(d):.2f}"
    head = f" since {since}."
    moved.sort(key=lambda m: -m[0])
    body = "; ".join(m[1] for m in moved[:3]) + "." if moved else \
        "No pillar moved: the new data did not shift any metric enough to change a pillar's score."
    # what new data the run brought
    st, now = prev.get("stamps") or {}, _stamps(res)
    new = []
    if st.get("quarter") and now["quarter"] and now["quarter"] > st["quarter"]:
        new.append(f'{_qlabel(now["quarter"])} results')
    if st.get("balance_sheet") and now["balance_sheet"] and now["balance_sheet"] > st["balance_sheet"]:
        new.append("the 10-Q balance sheet")
    if st.get("price_date") and now["price_date"] and now["price_date"] > st["price_date"] and st.get("price"):
        new.append(f'share price ${st["price"]:.2f} to ${now["price"]:.2f} ({(now["price"] / st["price"] - 1) * 100:+.1f}%)')
    if st.get("tracker") and now["tracker"] and now["tracker"] > st["tracker"]:
        new.append(f'Opendoor\'s tracker to {_d(now["tracker"])}')
    if st.get("mortgage") and now["mortgage"] and now["mortgage"] > st["mortgage"]:
        new.append("the weekly mortgage rate")
    if st.get("housing") and now["housing"] and now["housing"] > st["housing"]:
        new.append("a new month of housing data")
    tail = f' New since then: {", ".join(new)}.' if new else ""
    arrow = {"up": "&#8599; ", "down": "&#8600; ", "flat": ""}[tone]
    if short:  # one sentence: the biggest pillar move and what drove it, plus the next one
        if not moved:
            gist = "no pillar moved."
        else:
            gist = "mainly " + moved[0][2] + (f" ({moved[0][3]})" if moved[0][3] else "")
            gist += (f"; {moved[1][2]}." if len(moved) > 1 else ".")
        return f'<span class="mv-dir {tone}">{arrow}{e(word)}</span> since {e(since)}: {e(gist)}'

    return (f'<p class="moved"><b>Why it moved.</b> <span class="mv-dir {tone}">{arrow}{e(word)}</span>{e(head)} '
            f'{e(body)}{e(tail)}</p>')


WEIGHT_WHY = {
    "Growth": "Scale drives operating leverage; uses Opendoor's live tracker as well as reported revenue.",
    "Unit economics": "Whether each home earns money and sells quickly decides everything else.",
    "Profitability": "Management's own headline target is company-level profit (adjusted net income).",
    "Execution": "Opendoor publishes weekly projections for itself; this checks them.",
    "Balance sheet": "Inventory is funded with debt, so a fall in home prices shows up here first.",
    "Dilution": "Heavy stock pay and convertibles decide your share of any recovery.",
    "Valuation": "Kept small: at this stage price multiples say less than execution does.",
    "Momentum": "The market's view, kept small so the share price doesn't drive the score.",
    "Housing market": ("Kept at 5% on purpose: housing conditions already move Unit economics (margins, aged homes, days of "
                       "inventory) and Execution (contracts, sale speed). A bigger weight would count the same headwind twice."),
}


def _weights_card(s):
    rows = "".join(f'<tr><td>{html.escape(p["name"])}</td><td>{p["weight"]}%</td><td>{_num(p["score"])}</td>'
                   f'<td class="wtxt">{html.escape(WEIGHT_WHY.get(p["name"], ""))}</td></tr>' for p in s["pillars"])
    return ('<div class="card"><div class="hd"><div><h2>Pillar weights, and why</h2><p class="sub">The overall score is the '
            'weighted average of the pillar scores</p></div></div><div class="bd scroll" style="padding-top:4px"><table class="wt">'
            '<tr><th>Pillar</th><th>Weight</th><th>Score</th><th>Why this weight</th></tr>' + rows + '</table></div></div>')


def _thresholds_card(s):
    body = ""
    for p in s["pillars"]:
        body += f'<tr class="grp"><td colspan="6">{html.escape(p["name"])} ({p["weight"]}%), scoring {_num(p["score"])}</td></tr>'
        for i in p["metrics"]:
            f_ = i.get("fresh") or {}
            body += (f'<tr><td class="wtxt">{html.escape(i["label"])}</td><td data-l="Now">{html.escape(fmt(i["value"], i["fmt"]))}</td>'
                     f'<td data-l="0 pts at">{html.escape(fmt(i["bad"], i["fmt"]))}</td><td data-l="10 pts at">{html.escape(fmt(i["good"], i["fmt"]))}</td>'
                     f'<td data-l="Points">{_num(i["score"], "", "–")}</td>'
                     f'<td data-l="Data" class="{"old" if f_.get("old") else ""}">{html.escape(f_.get("text", ""))}</td></tr>')
    return ('<div class="card" id="thresholds"><div class="hd"><div><h2>Every metric, its thresholds and its data</h2><p class="sub">'
            'Points run in a straight line from the "0 points" value to the "10 points" value, and stop at 0 and 10</p></div></div>'
            '<div class="bd scroll" style="padding-top:4px"><table class="thr"><tr><th>Metric</th><th>Now</th><th>0 points at</th>'
            '<th>10 points at</th><th>Points</th><th>Data</th></tr>' + body + '</table></div>'
            '<div class="ft">A metric whose data is two quarters behind the latest report stops counting; its pillar then averages '
            'the rest. Data more than a quarter old is marked in amber.</div></div>')


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
    rows = "".join(f'<tr><td>{e(l["label"])}<small>{e(l["needs"])}</small></td><td data-l="Potential">{l["n"]:,.1f}M</td>'
                   + (f'<td class="on" data-l="Live today">{l["live"]:,.1f}M</td></tr>' if l["live"] > 0.05
                      else '<td class="off" data-l="Live today">0</td></tr>')
                   for l in d["lines"])
    pct = d["live_m"] / d["shares_m"]
    src = (f"10-Q for the quarter ended {_d(d['quarter'])}, table of anti-dilutive securities" if d["source"] == "10-Q"
           else f"XBRL total for {_d(d['quarter'])} (no breakdown found, all counted in full)")
    return (f'<div class="card" id="dilution"><div class="hd"><div><h2>Dilution, line by line</h2>'
            f'<p class="sub">Every potential new share, and how many are live at ${d["price"]:.2f}</p></div>'
            f'<div class="right">{_badge(f"{pct * 100:.1f}%", _tone(metric["score"]))}<p class="sub">live today</p></div></div>'
            f'<div class="bd scroll" style="padding-top:4px"><table class="dil"><tr><th>Instrument</th><th>Potential</th><th>Live today</th></tr>{rows}'
            f'<tr class="tot"><td>Total</td><td data-l="Potential">{d["potential_m"]:,.1f}M</td><td data-l="Live today">{d["live_m"]:,.1f}M</td></tr></table></div>'
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


# score -> hue of the page's background glow, through the badge colours: red, amber at a C, green from a B up
GLOW_HUES = [(0.0, 0.0), (3.5, 12.0), (5.0, 34.0), (6.5, 72.0), (8.0, 128.0), (10.0, 148.0)]


def _glow(score):
    """Three pastel colours for the background, from red-ish (weak) to green-ish (strong), smooth in between."""
    v = max(0.0, min(10.0, score if score is not None else 5.0))
    for (a, ha), (b, hb) in zip(GLOW_HUES, GLOW_HUES[1:]):
        if v <= b:
            h = ha + (hb - ha) * (v - a) / (b - a)
            break
    return (f"--g1:hsla({(h - 8) % 360:.0f},95%,79%,.55);--g2:hsla({(h + 10) % 360:.0f},90%,80%,.5);"
            f"--g3:hsla({(h - 12) % 360:.0f},90%,82%,.42)")


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
    pch = (ch or {}).get("pillars") if ch and not ch.get("method_changed") else None

    def mv(name):
        """Small move chip since the last scored run: green up, red down, grey flat. Nothing on a first or re-based run."""
        if pch is None or pch.get(name) is None:
            return '<i class="pmv"></i>'
        d = pch[name]
        cls, txt = ("up", f"+{d:.1f}") if d > 0.05 else ("dn", f"\u2212{-d:.1f}") if d < -0.05 else ("fl", "0.0")
        return f'<i class="pmv {cls}" title="Change since {e(_d(ch["since"]))}">{txt}</i>'
    bars = "".join(f'<div class="pbar"><span>{e(p["name"])}<span class="w">{p["weight"]}%</span></span>'
                   f'<div class="track"><div class="fill {_tone(p["score"])}" style="width:{(p["score"] or 0) * 10:.0f}%"></div></div>'
                   f'<b>{_num(p["score"])}</b>{mv(p["name"])}</div>' for p in s["pillars"])
    decomp = " + ".join(f'{e(p["name"])} {p["contribution"]:.2f}' for p in s["pillars"] if p.get("contribution") is not None)
    cards = ""
    ser = _series(res)  # sparkline data per metric
    for p in s["pillars"]:
        def note(i):
            return f'<small>{e(i["note"])}</small>' if i.get("note") else ""

        def tag(i):
            f_ = i.get("fresh")
            if not f_ or not f_.get("text"):
                return ""
            gone = i.get("lag", 0) >= 2
            title = ("Two quarters behind the latest report, so not scored" if gone else
                     "More than a quarter old" if f_["old"] else "Current")
            return (f'<em class="age{" old" if f_["old"] else ""}" title="{title}">{e(f_["text"])}'
                    f'{", too old to score" if gone else (", over a quarter old" if f_["old"] else "")}</em>')
        def extra_bits(i):
            k = i["key"]
            if k in ser:
                link = '<a class="mini-a" href="#unit-history">By quarter</a>' if k in ("contrib_margin", "days_inventory") else ""
                return _mini(*ser[k]) + link
            if k == "fallout":
                return ('<span class="how">Open contracts at the start, plus contracts signed, less homes bought, less '
                        'contracts still open at the end. <a href="#fallout">How it\'s estimated</a></span>')
            return ""
        rows = "".join(f'<div class="m">'
                       f'<span class="lab">{e(i["label"])}{note(i)}{extra_bits(i)}{tag(i)}'
                       f'<span class="scale">0 pts at {e(fmt(i["bad"], i["fmt"]))}, 10 pts at {e(fmt(i["good"], i["fmt"]))}</span></span>'
                       f'<span class="val">{e(fmt(i["value"], i["fmt"]))}'
                       f'<span class="pts">{_num(i["score"], " pts", "–")}</span></span></div>'
                       for i in p["metrics"])
        sc = "n/a" if p["score"] is None else f"{p['score']:.1f}"
        cards += (f'<div class="card"><div class="hd"><div><h2>{e(p["name"])}</h2><p class="sub">{e(p["question"])}</p></div>'
                  f'<div class="right">{_badge(sc, _tone(p["score"]))}<p class="sub">{p["weight"]}% weight</p></div></div>'
                  f'<div class="bd" style="padding-top:6px;padding-bottom:6px">{rows}'
                  f'{_short_line(res) if p["name"] == "Momentum" else ""}</div></div>')
    hv = [float(h["overall"]) for h in (res.get("history") or []) if h.get("overall")]
    h0 = (res.get("history") or [{}])[0].get("run_at", res["generated"])
    ospark = _mini(hv, f"since {_d(h0)}" if len(hv) > 1 else f"score history starts {_d(h0)}")
    pil = {p["name"]: p for p in s["pillars"]}
    dil_metric = next(i for i in pil["Dilution"]["metrics"] if i["key"] == "dilution_live")
    deep = (f'<div class="grid2">{_debt_card(res, pil["Balance sheet"])}{_dilution_card(res, dil_metric)}</div>'
            f'{_markets_card(res, pil["Housing market"])}{_ue_card(res)}{_stress_card(res)}')
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
            f'{acc.get("weeks_below_proj_13", 0)} of the last 13 weeks were below the range. {e(_what_if_text(s))}'
            f'Source: accountable.opendoor.com, data as of {e(_d(asof))}.</div></div>'
            if a4 else "")
    n = lambda r, k, d=0: "–" if r.get(k) is None else f"{r[k]:,.{d}f}"
    qrows = "".join(f"<tr><td>{_qlabel(r['quarter_end'])}</td><td data-l='Revenue'>{n(r, 'revenue_musd')}</td>"
                    f"<td data-l='Net income'>{n(r, 'net_income_musd')}</td><td data-l='Adj. income'>{n(r, 'adj_net_income_musd')}</td>"
                    f"<td data-l='Cash'>{n(r, 'cash_musd')}</td><td data-l='Homes bought'>{n(r, 'homes_purchased')}</td>"
                    f"<td data-l='Homes sold'>{n(r, 'homes_sold')}</td><td data-l='Contrib. %'>{n(r, 'contribution_margin_pct', 1)}</td>"
                    f"<td data-l='120+ days %'>{n(r, 'aged_120_pct', 0)}</td></tr>"
                    for r in reversed(res["quarters"][-8:]))
    hist = res.get("history") or []
    heads = "".join(f"<th>{e(p[0])}</th>" for p in PILLARS)
    hrows = "".join(f"<tr><td>{_d(h['run_at'])}{' (v' + (h.get('method') or METHOD) + ')' if (h.get('method') or METHOD) != METHOD else ''}</td>"
                    f"<td>${float(h['price']):.2f}</td><td><b>{float(h['overall']):.2f}</b> {e(h['grade'])}</td>"
                    + "".join(f"<td>{h.get(p[0]) or '–'}</td>" for p in PILLARS) + "</tr>" for h in reversed(hist[-60:]))
    pf_note = ("Cash, notes, book value and share count include: " + "; ".join(ex["capital_events"]) + ". ") if ex.get("capital_events") else ""
    nc = ex.get("nowcast")
    est_note = (f"{_qlabel(nc['quarter'])} revenue is estimated at ${nc['estimate']:,.0f}M "
                f"({nc['estimate'] / nc['prior_revenue'] * 100 - 100:+.1f}% YoY) until Opendoor reports: {nc['homes']:,.0f} homes sold "
                f"(Opendoor's tracker) x ${nc['rph_k']:.0f}K, the average revenue per home in {nc['rph_basis']}. Homes sold are "
                f"{nc['homes_yoy'] * 100:+.1f}% on a year earlier, so the estimate assumes revenue per home "
                f"{nc['rph_change'] * 100:+.1f}% above the ${nc['prior_rph_k']:.0f}K of a year earlier."
                + (f" Opendoor guided revenue up at least {nc['guide_min_yoy'] * 100:.0f}% (${nc['guide_min_revenue']:,.0f}M or more), "
                   "which implies a higher revenue per home than this estimate assumes." if nc.get("guide_min_yoy") is not None else "")
                + " ") if nc and nc.get("homes_yoy") is not None else ""
    doc = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Opendoor Scorecard: {s['overall']:.2f}/10 ({s['grade']})</title>
<meta name="description" content="Independent, automatically updated scorecard for Opendoor ($OPEN). {e(s['verdict'])}">
<link rel="icon" href="favicon.svg" type="image/svg+xml"><link rel="icon" href="favicon-32.png" sizes="32x32" type="image/png">
<link rel="apple-touch-icon" href="apple-touch-icon.png"><meta name="theme-color" content="#f3f2f0">
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>{CSS}:root{{{_glow(s["overall"])}}}</style></head><body>
<div class="bar"><div class="in">
<nav class="desk"><a href="#score">Score</a><a href="#pillars">Pillars</a><a href="#deep">Details</a><a href="#analyst">Analyst</a><a href="#contracts">Contracts</a><a href="#history">History</a><a href="#method">Method</a></nav>
<a class="brand" href="./">Opendoor Scorecard<small>Independent and not affiliated with Opendoor</small></a>
<div class="r3">{'<a class="nl-btn" href="#subscribe">Subscribe</a>' if NEWSLETTER_USER else ''}</div>
<button class="burger" type="button" aria-label="Open menu" aria-expanded="false" aria-controls="menu"><span></span><span></span><span></span></button>
</div>
</div>
<nav class="menu" id="menu" hidden>
{'<a class="menu-nl" href="#subscribe"><span>Get it by email</span><small>Weekly round-up, free</small></a>' if NEWSLETTER_USER else ''}
<a href="#score"><span>Overall score</span><small>The grade and why</small></a>
<a href="#pillars"><span>Pillars</span><small>What drives each score</small></a>
<a href="#deep"><span>Details</span><small>Debt, dilution and Opendoor&#39;s markets</small></a>
<a href="#analyst"><span>Analyst view</span><small>Estimate record, what the price implies, per share</small></a>
<a href="#contracts"><span>Contracts</span><small>Opendoor against its own targets</small></a>
<a href="#history"><span>History</span><small>Score over time and quarterly results</small></a>
<a href="#method"><span>Method</span><small>Weights, every threshold, and sources</small></a>
</nav>
<div class="wrap">
<section class="hero">
<div class="asof"><span class="dot"></span>Updated {e(_d(res['generated']))}, {e(res['generated'][11:16])} UK<i>Prices to {e(_d(px['date']))}</i><i>Opendoor tracker as of {e(_d(asof))}, refreshed weekly</i></div>
<h1>Opendoor,<br>scored.<br>Every weekday.</h1>
<div><p class="lede">An independent scorecard that grades Opendoor 0–10 on nine pillars, rebuilt every weekday from SEC filings, the share price, housing data from its markets and Opendoor's own public tracker.</p>
{'<a class="nl-cta" href="#subscribe">Get the weekly email, free <span>&rarr;</span></a>' if NEWSLETTER_USER else ''}
{_how_to_read()}</div>
</section>

{_dashboard(res, change, change_sub, ospark, kpi_html)}

{_subscribe_card()}

<div class="card"><div class="hd"><div><h2>Score by pillar</h2><p class="sub">Each pillar is the average of its metrics, 0–10</p></div></div>
<div class="bd">{bars}<p class="decomp">How the {s['overall']:.2f} adds up (pillar score x weight): {decomp}.</p></div></div>
{_own_history_card(res)}
{_fresh_card(res)}

<p class="section" id="pillars">What drives each score</p>
<div class="sec-tools"><button type="button" class="scale-tog" aria-pressed="false">Show each metric's 0 and 10 point values</button>
<a href="#thresholds">All thresholds in one table</a><a href="#weights-play">Try your own weights</a></div>
<div class="grid" style="margin-top:16px">{cards}</div>

<p class="section" id="deep">Details</p>
{deep}

<p class="section" id="analyst">Analyst view</p>
{_move_card(res)}
<div class="grid2">{_track_card(res)}{_per_share_card(res)}</div>
{_implied_card(res)}

<p class="section" id="contracts">Opendoor's own targets</p>
{contracts_card}
{_fallout_card(res)}
{_targets_card(res)}

<p class="section" id="history">Over time</p>
{_score_chart(res)}

<div class="card"><div class="hd"><div><h2>Quarterly results</h2><p class="sub">$ millions, from Opendoor's earnings releases and SEC filings</p></div></div>
<div class="bd scroll" style="padding-top:4px"><table class="qr"><tr><th>Quarter</th><th>Revenue</th><th>Net income</th><th>Adj. net income</th><th>Cash</th>
<th>Bought</th><th>Sold</th><th>Contrib. %</th><th>120+ days %</th></tr>{qrows}</table></div></div>

<div class="card"><details open><summary class="hd" style="border-bottom:0"><div><h2>Run history</h2><p class="sub">Every pillar's score on every day</p></div>
<span class="chev">▾</span></summary><div class="bd scroll" style="border-top:1px solid var(--line);padding-top:4px"><table><tr><th>Run</th><th>Price</th><th>Overall</th>{heads}</tr>{hrows}</table></div></details></div>

<p class="section" id="method">How the score works</p>
{_weights_card(s)}
{_scenario_card(res)}
{_thresholds_card(s)}
{_method_changes_card()}

{_subscribe_strip()}
<footer><p><b>Method.</b> Each metric scores 0–10 on a straight line between a "0 points" and a "10 points" value (all of them are listed above).
Pillars average their metrics, and the overall score weights the pillars as shown. Grades: A 8+, B 6.5+, C 5+, D 3.5+, F below. The colour at the top of the page follows the score, from red to green.
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
<script>
(function () {{
  var tog = document.querySelector(".scale-tog");
  if (tog) tog.addEventListener("click", function () {{
    var on = document.body.classList.toggle("show-scale");
    tog.setAttribute("aria-pressed", on ? "true" : "false");
    tog.textContent = on ? "Hide the 0 and 10 point values" : "Show each metric's 0 and 10 point values";
  }});
  // Score history, drawn at the box's real width (so text stays legible on phones) and redrawn on resize.
  // Grade bands behind the line; hover or tap shows the nearest day's score, grade and share price.
  document.querySelectorAll(".hbox").forEach(function (box) {{
    var P = JSON.parse(box.getAttribute("data-h")), tip = box.querySelector(".htip"), NS = "http://www.w3.org/2000/svg";
    if (!P.length) return;
    var bands = [[8, 10, "A"], [6.5, 8, "B"], [5, 6.5, "C"], [3.5, 5, "D"], [0, 3.5, "F"]], geo = [];
    function el(tag, at, txt) {{ var e = document.createElementNS(NS, tag); for (var k in at) e.setAttribute(k, at[k]); if (txt !== undefined) e.textContent = txt; return e; }}
    function draw() {{
      var W = box.clientWidth, H = W < 520 ? 200 : 250, L = 30, R = 26, T = 10, B = 24, n = P.length;
      var x = function (i) {{ return L + (n > 1 ? i * (W - L - R) / (n - 1) : (W - L - R) / 2); }};
      var y = function (v) {{ return T + (1 - v / 10) * (H - T - B); }};
      var svg = el("svg", {{viewBox: "0 0 " + W + " " + H, width: W, height: H, class: "hchart", role: "img",
                             "aria-label": "Overall score over time, one point per weekday"}});
      bands.forEach(function (b, k) {{
        svg.appendChild(el("rect", {{x: L, y: y(b[1]), width: W - L - R, height: y(b[0]) - y(b[1]), fill: k % 2 ? "#fbfaf8" : "#f6f4f1"}}));
        svg.appendChild(el("text", {{x: W - R + 9, y: (y(b[0]) + y(b[1])) / 2 + 4, class: "tick"}}, b[2]));
      }});
      (W < 520 ? [0, 5, 10] : [0, 2.5, 5, 7.5, 10]).forEach(function (t) {{
        svg.appendChild(el("line", {{x1: L, x2: W - R, y1: y(t), y2: y(t), stroke: "#ebe7e2", "stroke-dasharray": t % 10 ? "2 4" : "0"}}));
        svg.appendChild(el("text", {{x: L - 7, y: y(t) + 4, "text-anchor": "end", class: "tick"}}, String(t)));
      }});
      geo = P.map(function (p, i) {{ return [x(i), y(p[3]), p]; }});
      if (n > 1) svg.appendChild(el("polyline", {{points: geo.map(function (g) {{ return g[0] + "," + g[1]; }}).join(" "), fill: "none",
        stroke: "#1d4ed8", "stroke-width": 2.4, "stroke-linejoin": "round", "stroke-linecap": "round"}}));
      geo.forEach(function (g) {{ svg.appendChild(el("circle", {{cx: g[0], cy: g[1], r: 3.5, fill: "#fff", stroke: "#1d4ed8", "stroke-width": 2}})); }});
      svg.appendChild(el("text", {{x: L, y: H - 6, class: "tick"}}, P[0][2]));
      if (n > 1) svg.appendChild(el("text", {{x: W - R, y: H - 6, "text-anchor": "end", class: "tick"}}, P[n - 1][2]));
      var hx = el("line", {{y1: T, y2: H - B, stroke: "#23201d", "stroke-opacity": .25, visibility: "hidden"}});
      var dot = el("circle", {{r: 6, fill: "#1d4ed8", stroke: "#fff", "stroke-width": 2, visibility: "hidden"}});
      svg.appendChild(hx); svg.appendChild(dot);
      function show(ev) {{
        var r = svg.getBoundingClientRect(), cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
        var g = geo.reduce(function (a, b) {{ return Math.abs(b[0] - cx) < Math.abs(a[0] - cx) ? b : a; }}), p = g[2];
        hx.setAttribute("x1", g[0]); hx.setAttribute("x2", g[0]); hx.setAttribute("visibility", "visible");
        dot.setAttribute("cx", g[0]); dot.setAttribute("cy", g[1]); dot.setAttribute("visibility", "visible");
        tip.innerHTML = "<b>" + p[2] + "</b><span>" + p[3].toFixed(2) + " / 10, grade " + p[4] + "</span>" +
          (p[5] !== null ? "<span>Share price $" + p[5].toFixed(2) + "</span>" : "");
        tip.hidden = false;
        var tw = tip.offsetWidth;
        tip.style.left = Math.max(0, Math.min(W - tw, g[0] - tw / 2)) + "px";
        tip.style.top = Math.max(0, g[1] - tip.offsetHeight - 14) + "px";
      }}
      function hide() {{ tip.hidden = true; hx.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); }}
      svg.addEventListener("mousemove", show); svg.addEventListener("mouseleave", hide);
      svg.addEventListener("touchstart", show, {{passive: true}}); svg.addEventListener("touchmove", show, {{passive: true}});
      var old = box.querySelector("svg"); box.replaceChild(svg, old);
    }}
    draw();
    var last = box.clientWidth;
    window.addEventListener("resize", function () {{ if (box.clientWidth !== last) {{ last = box.clientWidth; draw(); }} }});
  }});
  document.querySelectorAll(".nl-form").forEach(function (f) {{
    f.addEventListener("submit", function () {{ setTimeout(function () {{ f.querySelector(".nl-ok").hidden = false; }}, 300); }});
  }});
  document.querySelectorAll(".uebox").forEach(function (b) {{ b.scrollLeft = b.scrollWidth; }});
  var out = document.querySelector(".stress-out");
  if (out) {{
    var combos = JSON.parse(out.getAttribute("data-combos")), base = parseFloat(out.getAttribute("data-base"));
    var boxes = document.querySelectorAll("#stress input[type=checkbox]");
    var main = out.querySelector(".so-main"), pil = out.querySelector(".so-pil");
    function upd() {{
      var key = "";
      ["r", "h", "c"].forEach(function (k) {{
        boxes.forEach(function (b) {{ if (b.value === k && b.checked) key += k; }});
      }});
      var c = combos[key];
      if (!key || !c) {{ main.textContent = "Nothing ticked: the score is as published."; pil.textContent = ""; out.className = "stress-out"; return; }}
      var d = c.change;
      main.innerHTML = "Overall <b>" + base.toFixed(2) + " \u2192 " + c.overall.toFixed(2) + "</b> (" + (d >= 0 ? "+" : "\u2212") + Math.abs(d).toFixed(2) + ")";
      pil.textContent = c.pillars.length ? c.pillars.map(function (p) {{ return p[0] + " " + p[1].toFixed(1) + " \u2192 " + p[2].toFixed(1); }}).join(";  ") : "No pillar moves: the inputs it changes are already at 0 or 10 points.";
      out.className = "stress-out " + (d < -0.005 ? "down" : d > 0.005 ? "up" : "");
    }}
    boxes.forEach(function (b) {{ b.addEventListener("change", upd); }});
  }}
  // Scenario tool: the page's scoring, re-run in the browser. lin() and the rounding match the Python exactly:
  // metric points on a straight line between its 0 and 10 values, pillar = mean rounded to 0.1, overall = weighted mean.
  // What today's price implies: future value = market value x (1 + return)^years; less today's net cash if counted;
  // / multiple = earnings needed; / margin = revenue needed. Growth is measured from the last four quarters' revenue.
  var im = document.querySelector("[data-imp]");
  if (im) {{
    var I = JSON.parse(im.getAttribute("data-imp")), out = im.querySelector(".imp-out"), grid = document.querySelector(".imp-grid");
    var val = function (k) {{ var el = im.querySelector('[data-k="' + k + '"]'); return el.type === "checkbox" ? el.checked : parseFloat(el.value); }};
    var bn = function (v) {{ return Math.abs(v) >= 1000 ? "$" + (v / 1000).toFixed(2) + "bn" : "$" + Math.round(v) + "M"; }};
    function need(y, r, m, pe, cash) {{
      var t = (Date.UTC(y, 11, 31) - Date.parse(I.date + "T00:00:00Z")) / (365.25 * 864e5);
      var fut = I.mcap * Math.pow(1 + r / 100, t) - (cash ? I.net : 0);
      var rev = fut / pe / (m / 100);
      return {{t: t, fut: fut, rev: rev, g: Math.pow(rev / I.ttm, 1 / t) - 1}};
    }}
    function upd() {{
      var y = val("year"), r = val("r"), m = val("m"), pe = val("pe"), cash = val("cash");
      if ([y, r, m, pe].some(isNaN) || m <= 0 || pe <= 0) return;
      var n = need(y, r, m, pe, cash);
      out.innerHTML = "To return " + r + "% a year from $" + I.price.toFixed(2) + ", Opendoor would need to be worth about <b>" +
        bn(n.fut + (cash ? I.net : 0)) + "</b> at the end of " + y + ". At " + pe + "x earnings and a " + m + "% net margin, that takes revenue of about <b>" +
        bn(n.rev) + "</b> in " + y + ": <b>" + (n.g * 100).toFixed(0) + "% a year</b> from the last four quarters' " + bn(I.ttm) + ".";
      var ms = [1, 2, 3, 4, 5], pes = [10, 15, 20, 25];
      var h = "<tr><th>Net margin</th>" + pes.map(function (p) {{ return "<th>" + p + "x</th>"; }}).join("") + "</tr>";
      ms.forEach(function (mm) {{
        h += "<tr><td>" + mm + "%</td>" + pes.map(function (p) {{
          var x = need(y, r, mm, p, cash), on = (mm === m && p === pe) ? ' class="on"' : "";
          return "<td" + on + ">" + bn(x.rev) + "<small>" + (x.g * 100).toFixed(0) + "% a year</small></td>";
        }}).join("") + "</tr>";
      }});
      grid.innerHTML = h;
    }}
    im.querySelectorAll("input,select").forEach(function (el) {{ el.addEventListener("input", upd); el.addEventListener("change", upd); }});
    upd();
  }}
  var sc = document.querySelector("[data-scen]");
  if (sc) {{
    var D = JSON.parse(sc.getAttribute("data-scen")), ov = {{}};
    var sliders = sc.querySelectorAll(".sliders input"), ins = sc.querySelectorAll(".ins input");
    var scoreEl = document.querySelector(".wp-score"), gradeEl = document.querySelector(".wp-grade");
    var pilEl = sc.querySelector(".sc-pil"), noteEl = sc.querySelector(".scen-note");
    var page = D.pillars.map(function (p) {{ return p.w; }});
    // Python's round(): exact ties go to the even digit, everything else rounds on the exact binary value (as toFixed does)
    function r(x, n) {{
      var t = Math.abs(x).toFixed(n + 30), cut = t.indexOf(".") + n + 1;   // the double's exact decimal digits
      if (/^50*$/.test(t.slice(cut))) {{                                     // an exact tie: round to the even digit
        var keep = parseFloat(t.slice(0, cut)), last = parseInt(t.charAt(cut - 1 - (n === 0 ? 1 : 0)), 10);
        var v = last % 2 === 0 ? keep : keep + Math.pow(10, -n);
        return parseFloat((x < 0 ? -v : v).toFixed(n));
      }}
      return parseFloat(x.toFixed(n));
    }}
    function lin(x, b, g) {{ return r(Math.max(0, Math.min(1, (x - b) / (g - b))) * 10, 1); }}
    // Python's sum() of floats (3.12+) uses compensated summation; the same here, so pillar means match to the bit
    function fsum(a) {{
      var s = 0, c = 0;
      a.forEach(function (x) {{ var t = s + x; c += Math.abs(s) >= Math.abs(x) ? (s - t) + x : (x - t) + s; s = t; }});
      return s + c;
    }}
    function grade(v) {{ for (var i = 0; i < D.grades.length; i++) if (v >= D.grades[i][0]) return D.grades[i][1]; return "F"; }}
    function setInput(k, v) {{
      // an input is a metric override; the margin also moves "vs the quarter before" while a quarter is pending
      ov[k] = v;
      if (k === "contrib_margin" && D.pending && D.cmLast !== null) ov.contrib_trend = v - D.cmLast;
    }}
    function calc() {{
      var w = [], t = 0, ws = 0, moved = [];
      sliders.forEach(function (s, k) {{ w[k] = parseFloat(s.value); }});
      var sum = w.reduce(function (a, b) {{ return a + b; }}, 0);
      D.pillars.forEach(function (p, k) {{
        sliders[k].parentNode.querySelector(".sl-v").textContent = (sum ? w[k] / sum * 100 : 0).toFixed(0) + "%";
        if (!p.ms.length) return;
        var pts = p.ms.map(function (m) {{ return lin(m.k in ov ? ov[m.k] : m.v, m.b, m.g); }});
        var ps = r(fsum(pts) / pts.length, 1);
        if (p.s !== null && Math.abs(ps - p.s) > 0.001) moved.push(p.n + " " + p.s.toFixed(1) + " \u2192 " + ps.toFixed(1));
        if (w[k] > 0) {{ t += ps * w[k]; ws += w[k]; }}
      }});
      if (!ws) {{ scoreEl.textContent = "n/a"; gradeEl.textContent = "set at least one weight"; return; }}
      var v = r(t / ws, 2), d = v - D.published;
      var asPub = !Object.keys(ov).length && w.every(function (x, k) {{ return x === page[k]; }});
      scoreEl.textContent = v.toFixed(2);
      gradeEl.textContent = "grade " + grade(v) + (asPub ? ", as published" :
        " (" + (d >= 0 ? "+" : "\u2212") + Math.abs(d).toFixed(2) + " vs published " + D.published.toFixed(2) + ")");
      pilEl.textContent = moved.length ? "Pillars that move: " + moved.join("; ") : "";
    }}
    ins.forEach(function (i) {{ i.addEventListener("input", function () {{
      var v = parseFloat(i.value);
      if (isNaN(v)) return;
      setInput(i.getAttribute("data-k"), v / 100);
      sc.querySelectorAll(".spre").forEach(function (b) {{ b.classList.remove("on"); }});
      noteEl.textContent = "Your inputs";
      calc();
    }}); }});
    sc.querySelectorAll(".spre").forEach(function (b) {{ b.addEventListener("click", function () {{
      var vals = JSON.parse(b.getAttribute("data-v"));
      ov = {{}};
      ins.forEach(function (i) {{
        var k = i.getAttribute("data-k");
        i.value = (k in vals ? vals[k] * 100 : parseFloat(i.getAttribute("data-pub"))).toFixed(1);
      }});
      Object.keys(vals).forEach(function (k) {{ setInput(k, vals[k]); }});
      sc.querySelectorAll(".spre").forEach(function (x) {{ x.classList.remove("on"); }});
      b.classList.add("on"); noteEl.textContent = b.getAttribute("data-note"); calc();
    }}); }});
    sliders.forEach(function (s) {{ s.addEventListener("input", function () {{
      sc.querySelectorAll(".pre").forEach(function (b) {{ b.classList.remove("on"); }}); calc(); }}); }});
    sc.querySelectorAll(".pre").forEach(function (b) {{ b.addEventListener("click", function () {{
      var w = b.getAttribute("data-w") ? JSON.parse(b.getAttribute("data-w")) : null;
      sliders.forEach(function (s, k) {{ s.value = w ? w[D.pillars[k].n] : page[k]; }});
      sc.querySelectorAll(".pre").forEach(function (x) {{ x.classList.remove("on"); }});
      b.classList.add("on"); calc();
    }}); }});
    calc();
  }}
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


def _stamps(r):
    """When each kind of input was last refreshed, to say what new data a run brought."""
    fr = (r.get("extra") or {}).get("fresh") or {}
    date = lambda k: (fr.get(k) or {}).get("date")
    return {"quarter": (r.get("quarters") or [{}])[-1].get("quarter_end"), "price_date": (r.get("price") or {}).get("date"),
            "price": (r.get("price") or {}).get("price"), "tracker": (r.get("accountable") or {}).get("as_of"),
            "mortgage": date("mortgage_rate"), "housing": date("dom_yoy"), "balance_sheet": (r.get("extra") or {}).get("pro_forma", {}).get("as_of")}


def update_estimates_log(res, path):
    """Every run before a quarter is reported records this page's revenue estimate for it; once the release is in,
    the last estimate made before it is settled against the reported figure. Kept in the repo, never rewritten."""
    log = json.load(open(path)) if os.path.exists(path) else {}
    nc = res["extra"].get("nowcast")
    today = res["generated"][:10]
    if nc and nc.get("estimate"):
        e = log.setdefault(nc["quarter"], {"estimates": []})
        if "reported" not in e:
            e["estimates"] = [x for x in e["estimates"] if x["date"] != today] + [
                {"date": today, "revenue": round(nc["estimate"], 1), "homes": nc["homes"], "rph_k": round(nc["rph_k"], 1)}]
            e["guide_min"] = nc.get("guide_min_revenue")
    for qe, e in log.items():
        row = next((r for r in res["quarters"] if r["quarter_end"] == qe and r.get("revenue_musd")), None)
        if row and "reported" not in e and e.get("estimates"):
            final = e["estimates"][-1]
            e.update(reported=row["revenue_musd"], reported_on=today, final=final,
                     error=final["revenue"] / row["revenue_musd"] - 1, homes_reported=row.get("homes_sold"))
    with open(path, "w") as fh:
        json.dump(dict(sorted(log.items())), fh, indent=1)
    return log


def update_metric_history(res, path):
    """One row per day with every metric's value, so sparklines can show how a figure has moved between runs."""
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as fh:
            rows = list(csv.DictReader(fh))
    row = {"run_date": res["generated"][:10], "overall": res["score"]["overall"],
           **{k: ("" if v is None else round(v, 6)) for k, v in res["metrics"].items()}}
    rows = [r for r in rows if r.get("run_date") != row["run_date"]] + [{k: str(v) for k, v in row.items()}]
    fields = ["run_date", "overall"] + list(res["metrics"])
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return rows


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
    ap.add_argument("--contracts-cache", default=os.path.join(HERE, "contracts_quarterly.json"))
    ap.add_argument("--short-cache", default=os.path.join(HERE, "short_cache.json"))
    ap.add_argument("--estimates-log", default=os.path.join(HERE, "estimates_log.json"))
    ap.add_argument("--ua", default=os.environ.get("SEC_UA", "OPEN scorecard research contact@example.com"))
    ap.add_argument("--shares", type=float, help="shares you hold")
    ap.add_argument("--avg-cost", type=float, help="your average cost per share, USD")
    ap.add_argument("--outdir", default=os.path.join(HERE, "www"), help="folder for index.html, scorecard.json, history.csv")
    ap.add_argument("--out", help=argparse.SUPPRESS)  # accepted for older commands; output goes to --outdir
    ap.add_argument("--export-rows", metavar="DIR", help="also write flat JSON row files for the dashboard")
    a = ap.parse_args()

    log, edgar, acc_raw, releases, macro, dil_cache, housing, sub, short = [], {}, None, {}, None, None, None, None, None
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
            short = short_interest(log)
            old = json.load(open(a.short_cache)) if os.path.exists(a.short_cache) else {}
            if not short.get("float_shares") and old.get("float_shares"):  # the float changes slowly: keep the last one
                short.update(float_shares=old["float_shares"], float_source=old.get("float_source", "") + " (last good copy)")
            with open(a.short_cache, "w") as fh:
                json.dump(short, fh, indent=1)
            log.append(f"short interest {short['shares_short'] / 1e6:,.1f}M shares, {short['days_to_cover']} days to cover "
                       f"(settlement {short['settlement']})")
        except Exception as e:
            log.append(f"short interest unavailable ({e}); using cached copy")
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
    if acc:
        acc["quarters"] = merge_contract_quarters(acc.get("quarters") or {}, a.contracts_cache)
    if not releases and os.path.exists(a.releases_cache):
        releases = releases_to_rows(json.load(open(a.releases_cache)))
    if macro is None and os.path.exists(a.macro_cache):
        macro = json.load(open(a.macro_cache))
    if dil_cache is None and os.path.exists(a.dilution_cache):
        dil_cache = json.load(open(a.dilution_cache))
    if short is None and os.path.exists(a.short_cache):
        short = json.load(open(a.short_cache))
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
    res = {"generated": now.isoformat(timespec="minutes"), "ticker": TICKER, "price": px, "method": METHOD,
           "extra": extra, "metrics": m, "score": score(m, extra), "quarters": q, "accountable": acc, "log": log,
           "contracts_all": (acc_raw or {}).get("contracts"), "short_interest": short}
    if a.shares and a.avg_cost:
        val, cost = a.shares * px["price"], a.shares * a.avg_cost
        res["position"] = {"shares": a.shares, "avg_cost": a.avg_cost, "value": val, "pnl": val - cost,
                           "pnl_pct": val / cost - 1, "to_breakeven": a.avg_cost / px["price"] - 1}
    if acc_raw is not None and not a.offline and any("data as of" in l for l in log):
        save_snapshot(acc_raw, a.snapshot)
    os.makedirs(a.outdir, exist_ok=True)
    prev, history = update_history(res, os.path.join(a.outdir, "history.csv"))
    res["metric_history"] = update_metric_history(res, os.path.join(a.outdir, "metric_history.csv"))
    res["estimates_log"] = update_estimates_log(res, a.estimates_log)
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
    old_path = os.path.join(a.outdir, "scorecard.json")
    if os.path.exists(old_path):
        try:
            old = json.load(open(old_path))
            if old.get("generated", "")[:10] != res["generated"][:10]:
                res["previous"] = {"generated": old.get("generated"), "overall": old["score"].get("overall"),
                                   "method": old.get("method", METHOD),
                                   "points": {i["key"]: i["score"] for p in old["score"]["pillars"] for i in p["metrics"]},
                                   "values": {i["key"]: i["value"] for p in old["score"]["pillars"] for i in p["metrics"]},
                                   "pillars": {p["name"]: p["score"] for p in old["score"]["pillars"]},
                                   "stamps": _stamps(old)}
            else:
                res["previous"] = old.get("previous")
        except Exception:
            pass
    with open(os.path.join(a.outdir, "scorecard.json"), "w") as fh:
        json.dump(res, fh, indent=2, default=str)
    write_html(res, os.path.join(a.outdir, "index.html"))
    print(f"Wrote {a.outdir}/index.html, scorecard.json and history.csv")
    if a.export_rows:
        export_rows(res, a.export_rows)
        print(f"Wrote dashboard row files to {a.export_rows}/")


if __name__ == "__main__":
    main()
