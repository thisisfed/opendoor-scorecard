# Opendoor (OPEN) investor scorecard: auto-updating website

GitHub runs the scorecard every weekday evening after the US market closes and publishes the result as a
web page you can open from anywhere. No Pi or computer needs to be on.

**Your site:** `https://opendoor.thisisfed.xyz/`

Each run pulls:
- reported financials and earnings releases from the SEC;
- Opendoor's weekly tracker from accountable.opendoor.com;
- the share price (Yahoo Finance, then Nasdaq, then Stooq);
- the 30-year US mortgage rate (Freddie Mac, via the St. Louis Fed);
- days on market, price cuts, asking prices and listings in 12 of Opendoor's markets (Realtor.com's monthly data);
- the table of potential new shares (awards, warrants, convertible notes) from each 10-Q.

It rescores the company, records the day's score in the history and republishes the page. The page shows the change
since the last run, a chart of the score over time, and every pillar and metric.

Not investment advice: a structured summary of the numbers, not a forecast or a recommendation.

## One-time setup (3 minutes, in the browser)

The files are already in this repository. Three settings remain, and only the account owner can change them:

1. **Add your SEC contact.** The SEC requires a name and email with every request. Go to **Settings → Secrets and
   variables → Actions → New repository secret**. Name: `SEC_UA`, value: your name and email
   (e.g. `Federico Ferrari you@example.com`). It's stored privately and isn't shown on the public site.

2. **Turn on the website.** Go to **Settings → Pages → Build and deployment → Source: GitHub Actions**.

3. **First run.** Go to **Actions**, click **Update Opendoor scorecard → Run workflow**, then wait about a minute
   for the green tick. Your site is now at `https://opendoor.thisisfed.xyz/`. Bookmark it, or
   add it to your phone's home screen.

From then on it runs by itself every weekday at 22:30 UTC: 22:30 UK time in winter, 23:30 in summer.

## Seeing whether the score changed

- **The website:** near the top, "Since last run: ▲/▼ change · grade unchanged / grade was X". Below that are
  the score-over-time chart and the run history, with every pillar's score on every run.
- **Downloads:** the footer links to `history.csv` (the full history) and `scorecard.json` (all the data).
- **Phone alerts (optional):** install the free **ntfy** app and subscribe to a long private topic name
  (e.g. `fed-open-score-7k2q9`). Add it as a repository secret named `NTFY_TOPIC`. You'll get a push like
  *"OPEN score 4.93 (+0.20 since 2026-11-02), grade D; biggest moves: Profitability +1.2"* whenever the score
  moves by 0.05 or more or the grade changes.
- **If a run fails,** GitHub emails you, and the **Actions** tab shows the log.

## Each quarter: nothing to do

When Opendoor files its earnings release with the SEC (next: Q3 2026 on Thursday 5 November, after the US close),
the next run reads it automatically: revenue, profit, homes bought and sold, contribution margin, adjusted
EBITDA, adjusted net income and the share of homes listed for more than 120 days (with the whole-market figure
it's compared to). It was checked against six past releases (Q1 2025 to Q2 2026) with no differences.
The reported balance sheet and the table of potential new shares arrive with the 10-Q, usually within days.
Until then, figures from the previous quarter are labelled on the page; a figure two quarters old stops counting.

`opendoor_kpis.csv` is only for corrections. Any value in it overrides the automatic sources. Saving the file on
GitHub starts a run.

If Opendoor announces a financing deal, buyback or share issue **after** the last quarter end, add it to
`CAPITAL_EVENTS` near the top of `opendoor_scorecard.py` (copy the August 2026 entry's format). Old entries
switch off by themselves once a reported quarter includes them. A new award, warrant or convertible with a price
condition goes in `PRICE_TRIGGERED` next to it; until then it counts in full, which errs on the cautious side.

## What's scored

Nine pillars, each 0–10 (the average of its metrics), weighted into the overall score:

| Pillar | Weight | Metrics |
|---|---|---|
| Growth | 15% | revenue YoY (estimated from homes sold until reported), quarter-to-date homes sold YoY, contracts vs prior quarter |
| Unit economics | 15% | contribution margin and its change on the quarter before, gross margin, share of homes listed 120+ days, days of inventory |
| Profitability | 15% | GAAP net margin (last 2 quarters), adjusted EBITDA margin, adjusted net income margin (the end-2026 target) |
| Execution | 15% | homes sold vs management's expectation, contracts vs Opendoor's projected range, sale speed vs the market, signed contracts that fell through |
| Balance sheet | 15% | cash vs the company's own debt (convertible notes), home-backed debt vs the homes it funds, cash runway (adjusted for deals after the last quarter) |
| Dilution | 10% | share count change, stock pay / revenue, potential new shares that are live at today's price |
| Valuation | 5% | price / sales, price / book |
| Momentum | 5% | 1-year return, place in 52-week range, price vs 200-day average |
| Housing market | 5% | 30-year mortgage rate and its 13-week change; days on market, price cuts and asking prices vs a year ago in Opendoor's markets |

**Debt, split by what backs it.** Most of Opendoor's debt is secured by the homes it holds and can't be claimed from
the company itself, except in limited cases. So it's measured against the value of those homes (the cushion if prices fall), while the
convertible notes, the company's own debt, are measured against its cash.

**Live dilution.** RSUs and options count in full. The CEO's awards count once the stock reaches their price targets
($6.24, then $9 to $33). Warrants count only their value above the exercise price ($9, $13, $17; they expire on
20 November 2026). Convertible notes count only their value above the cash they repay, net of the capped call on
the 2030 0% notes. The page shows every line, and how dilution would grow at higher share prices.

**Contracts that fell through.** Contracts open at the start of a quarter, plus those signed in it (the tracker,
which Opendoor says isn't reduced for cancellations), less homes bought, less contracts still open at the end
(both from the earnings release). Finished quarters are kept in `contracts_quarterly.json` once the tracker's
window moves on.

**Also on the page, not scored:** management's targets and a guidance track record (met, beaten, close or missed,
item by item), unit economics quarter by quarter (including selling and holding costs and write-downs per home),
stress tests (rates +0.5 points, home prices -5%, contracts another 20% short) that re-run the same scoring, and
sliders to try your own pillar weights. Every metric's thresholds and data date are listed under "How the score works".

Grades: A 8+, B 6.5+, C 5+, D 3.5+, F below. The thresholds and weights are judgement calls and have not been
back-tested against share-price returns. They live in `RULES` and `PILLARS` at the top of the script.

## Newsletter (optional, free)

Readers sign up on the page. Everyone gets the same emails, through Buttondown (free up to 100 subscribers):
a **round-up every Friday evening**, and an **extra email the evening new quarterly results reach the score**
(on a Friday, the round-up carries the results instead, so nobody gets two). Until it is set up, the page shows no signup.

1. Create a free account at buttondown.com. Your newsletter's username is the last part of `buttondown.com/<username>`.
2. In this repo: **Settings → Secrets and variables → Actions → Variables → New repository variable**:
   `BUTTONDOWN_USERNAME` = your username. The next run shows the signup on the page.
3. In Buttondown, copy your API key (Settings → API). In the same GitHub page, under **Secrets**:
   `BUTTONDOWN_API_KEY` = the key. From then on the evening run sends whatever is due.

To read an email before anyone receives one: `python3 newsletter.py --preview weekly` (or `results`).
To send what is due today by hand: **Actions → Update Opendoor scorecard → Run workflow**, tick "Also email subscribers".
`newsletter_log.json` records what was sent, so nothing goes out twice. (Buttondown's free plan has no tags, which is
why there is one list rather than a daily/weekly choice; its $9 Basic plan would allow one.)

## Good to know

- **The page is public.** Anyone with the link can see it. It contains only public market data, not your position.
- **Fallbacks:** if a data site blocks GitHub's servers on a given day, the run uses the last saved copy
  (Opendoor's tracker) or a backup price feed, and the log says so. Yahoo sometimes rate-limits cloud servers;
  Stooq is the backup.
- **The schedule stays on:** GitHub pauses schedules on repositories with no activity for 60 days. Every run
  commits the updated history, which counts as activity.
- **Run it on demand** at any time from **Actions → Update Opendoor scorecard → Run workflow**.
- **Run it on a computer instead:** `python3 opendoor_scorecard.py` writes the same page to `www/index.html`.

## Private position page

`position_page.py` adds a page at `/me/` with your own holding in pounds: value, the day's gain or loss and the total,
with a chart and a day-by-day table. It is locked: the figures are encrypted (AES-256-GCM, key from your passphrase
via PBKDF2-SHA256) before they reach this public repository, and only decrypt in your browser.

Set three repository secrets (Settings > Secrets and variables > Actions > New repository secret):

- `POSITION_PASSPHRASE`: the passphrase for the page. Make it long (four or five random words); it is the only lock.
- `POSITION_SHARES`: how many shares you hold, e.g. `1250.5`
- `POSITION_COST_USD`: everything you have paid for them in dollars, fees included, e.g. `6200`
- `POSITION_FINNHUB_KEY` (optional): a free API key from finnhub.io. While the page is open it then streams live
  trades, pre-market and after-hours included, and works out value and gain from each one. The key is stored only inside
  the encrypted blob.
- `POSITION_OPENED` (optional): the day you opened the position, e.g. `2026-10-14`. The days before the log began are
  filled in once from daily closes and rates, assuming today's holding throughout, and marked on the page.

- `POSITION_TRADES` (optional, recommended): every buy, one per line: day first, shares, price paid per share in
  dollars, then optionally the pounds that left (or reached) your account, e.g. `14/10/2026 1000 1.60 1221.37`.
  Without the pounds, that day's exchange rate is used. A negative number of shares records a sale (at the average cost). When it is
  set, the holding on each day comes from this list and the whole history is worked out again on every run, so
  `POSITION_SHARES`, `POSITION_COST_USD` and `POSITION_OPENED` are no longer used. The page's "Add shares" tool
  previews a buy and gives you the updated list to paste in.

Without a trade list, update `POSITION_SHARES` and `POSITION_COST_USD` after a top-up. The page counts the money put
in at the pound/dollar rate on the day it went in; total gain or loss is the value in pounds less that, split into the
share price part and the exchange-rate part. The day's figure is the change in that total, so a purchase never shows
as a gain. Rates are FRED series DEXUSUK, with the ECB rate as a fallback. If you change the passphrase, the
day-by-day log starts again.
Without the secrets the step does nothing.
