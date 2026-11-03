# Opendoor (OPEN) investor scorecard: auto-updating website

GitHub runs the scorecard every weekday evening after the US market closes and publishes the result as a
web page you can open from anywhere. No Pi or computer needs to be on.

**Your site:** `https://thisisfed.github.io/opendoor-scorecard/`

Each run pulls:
- reported financials from SEC EDGAR;
- Opendoor's weekly tracker from accountable.opendoor.com;
- the share price from Yahoo Finance, with Stooq as backup.

It rescores the company, adds a line to the score history and republishes the page. The page shows the change
since the last run, a chart of the score over time, and every pillar and metric.

Not investment advice. The scoring thresholds are opinions; they're in `RULES` at the top of `opendoor_scorecard.py`.

## One-time setup (3 minutes, in the browser)

The files are already in this repository. Three settings remain, and only the account owner can change them:

1. **Add your SEC contact.** The SEC requires a name and email with every request. Go to **Settings → Secrets and
   variables → Actions → New repository secret**. Name: `SEC_UA`, value: your name and email
   (e.g. `Federico Ferrari you@example.com`). It's stored privately and isn't shown on the public site.

2. **Turn on the website.** Go to **Settings → Pages → Build and deployment → Source: GitHub Actions**.

3. **First run.** Go to **Actions**, click **Update Opendoor scorecard → Run workflow**, then wait about a minute
   for the green tick. Your site is now at `https://thisisfed.github.io/opendoor-scorecard/`. Bookmark it, or
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

## Each quarter (5 minutes, in the browser)

After Opendoor reports (next: Q3 2026 on Thursday 5 November, after the US close):

1. Open the earnings letter at investor.opendoor.com.
2. In the repo, click `opendoor_kpis.csv` → the pencil icon. Add one line for the new quarter end
   (e.g. `2026-09-30`), with all money in $ millions. Leave unknown columns empty. Commit.
3. Saving the file starts a run automatically, so the site updates within a minute or two.

If Opendoor announces a financing deal, buyback or share issue **after** the last quarter end, add it to
`CAPITAL_EVENTS` near the top of `opendoor_scorecard.py` (copy the August 2026 entry's format). Old entries
switch off by themselves once a reported quarter includes them.

## Optional: use your own domain (e.g. open.thisisfed.xyz)

1. In **Settings → Pages → Custom domain**, enter `open.thisisfed.xyz` and save.
2. Where you manage thisisfed.xyz's DNS, add a **CNAME** record: name `open`, value `thisisfed.github.io`.
3. Once GitHub shows the domain as verified, tick **Enforce HTTPS**.

## Good to know

- **The page is public.** Anyone with the link can see it. It contains only public market data, not your position.
- **Fallbacks:** if a data site blocks GitHub's servers on a given day, the run uses the last saved copy
  (Opendoor's tracker) or a backup price feed, and the log says so. Yahoo sometimes rate-limits cloud servers;
  Stooq is the backup.
- **The schedule stays on:** GitHub pauses schedules on repositories with no activity for 60 days. Every run
  commits the updated history, which counts as activity.
- **Run it on demand** at any time from **Actions → Update Opendoor scorecard → Run workflow**.
- **Run it on a computer instead:** `python3 opendoor_scorecard.py` writes the same page to `www/index.html`.
