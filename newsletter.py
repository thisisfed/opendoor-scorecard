#!/usr/bin/env python3
"""Email the scorecard to subscribers through Buttondown (free up to 100 subscribers).

One list, two kinds of email, both to every subscriber (Buttondown's free plan has no tags to split a list):
  - weekly:  Fridays, a round-up of the week
  - results: the first evening a new quarter's results are in the score

Runs from the GitHub workflow after the page is published. Needs the BUTTONDOWN_API_KEY secret; without it,
it does nothing. A log (newsletter_log.json) stops the same email going out twice.

  python3 newsletter.py --send               send what is due today
  python3 newsletter.py --test               email both kinds to you only (the newsletter's own address), as drafts
  python3 newsletter.py --preview weekly     print an email instead of sending it (also: results)
"""
import argparse
import csv
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.buttondown.com/v1"
SITE = os.environ.get("SITE_URL", "https://thisisfed.github.io/opendoor-scorecard/")


def api(method, path, key, body=None, extra=None):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Token {key}", "Content-Type": "application/json",
                                          "X-API-Version": "2026-04-01", "X-Buttondown-Live-Dangerously": "true",
                                          **(extra or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        txt = r.read().decode()
        return json.loads(txt) if txt else {}


# ---------------------------------------------------------------- writing the emails
def _d(iso):
    d = dt.date.fromisoformat(iso[:10])
    return f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _q(iso):
    return f"Q{(int(iso[5:7]) - 1) // 3 + 1} {iso[:4]}"


def _chg(v, nd=2):
    return "unchanged" if abs(v) < 0.005 else (f"up {v:.{nd}f}" if v > 0 else f"down {-v:.{nd}f}")


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def load(site):
    res = json.load(open(os.path.join(site, "scorecard.json")))
    with open(os.path.join(site, "history.csv"), newline="") as fh:
        hist = list(csv.DictReader(fh))
    return res, hist


def _standing(res):
    lines = []
    for p in res["score"]["pillars"]:
        sc = "n/a" if p["score"] is None else f"{p['score']:.1f}"
        lines.append(f"- {p['name']} ({p['weight']}%): **{sc}**")
    return "\n".join(lines)


def _upcoming(res, today):
    nxt = (res.get("extra") or {}).get("next_results") or {}
    if nxt.get("date") and nxt["date"] >= today:
        return f"**Coming up:** {nxt['what']}, {_d(nxt['date'])}. The first evening run after that picks them up.\n\n"
    return ""


def _footer(kind):
    return (f"\n\n[See the full scorecard]({SITE})\n\n---\n"
            "Independent project, not affiliated with or endorsed by Opendoor. A summary of public numbers, not a forecast, "
            "a recommendation or investment advice. You get this because you signed up on the Opendoor Scorecard: a round-up "
            "every Friday, plus an email on the evening new results land.")


def results(res, hist):
    """The evening a new quarter's results reach the score: what changed since the run before."""
    s, px = res["score"], res["price"]
    today = res["generated"][:10]
    prev = hist[-2] if len(hist) >= 2 else None
    ch = s["overall"] - _f(prev["overall"]) if prev else 0.0
    subject = f"Opendoor {_q(res['quarters'][-1]['quarter_end'])} results: score {s['overall']:.2f}/10, grade {s['grade']} ({_chg(ch)})"
    out = [" ".join(x for x in (f"**{s['overall']:.2f} / 10, grade {s['grade']}.**",
                                (f"{_chg(ch).capitalize()} since {_d(prev['run_at'])}." if prev else ""), s["verdict"]) if x)]
    if prev and prev.get("latest_quarter") and prev["latest_quarter"] != res["quarters"][-1]["quarter_end"]:
        out.append(f"**New results:** {_q(res['quarters'][-1]['quarter_end'])} is now in the score.")
    if prev and prev.get("grade") and prev["grade"] != s["grade"]:
        out.append(f"**Grade change:** {prev['grade']} to {s['grade']}.")
    moved = []
    if prev:
        for p in s["pillars"]:
            a, b = _f(prev.get(p["name"])), p["score"]
            if a is not None and b is not None and abs(b - a) >= 0.05:
                moved.append(f"- {p['name']}: {a:.1f} to {b:.1f}")
    old_pts = ((res.get("previous") or {}).get("points")) or {}
    metric_moves = []
    for p in s["pillars"]:
        for i in p["metrics"]:
            a, b = old_pts.get(i["key"]), i["score"]
            if a is not None and b is not None and abs(b - a) >= 0.5:
                metric_moves.append((abs(b - a), f"- {i['label']}: {a:.1f} to {b:.1f} points"))
    if moved:
        out.append("**What moved**\n" + "\n".join(moved))
        if metric_moves:
            out.append("**Behind it**\n" + "\n".join(t for _, t in sorted(metric_moves, reverse=True)[:5]))
    else:
        out.append("No pillar moved on the new figures.")
    pp = _f(prev.get("price")) if prev else None
    out.append(f"**Share price:** ${px['price']:.2f} (close {_d(px['date'])})"
               + (f", {(px['price'] / pp - 1) * 100:+.1f}% on the previous run." if pp else "."))
    out.append("**Where each pillar stands**\n" + _standing(res))
    return subject, "\n\n".join(out) + _footer("results")


def weekly(res, hist):
    s, px = res["score"], res["price"]
    today = res["generated"][:10]
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=6)).isoformat()
    week_ago = next((h for h in reversed(hist[:-1]) if h["run_at"][:10] < cutoff), hist[0] if len(hist) > 1 else None)
    ch = s["overall"] - _f(week_ago["overall"]) if week_ago else 0.0
    subject = f"OPEN weekly: {s['overall']:.2f}/10, grade {s['grade']} ({_chg(ch)} on the week)"
    out = [" ".join(x for x in (f"**{s['overall']:.2f} / 10, grade {s['grade']}.**",
                                (f"{_chg(ch).capitalize()} since {_d(week_ago['run_at'])}." if week_ago else ""), s["verdict"]) if x),
           s["why"]]
    if week_ago and week_ago.get("latest_quarter") != res["quarters"][-1]["quarter_end"]:
        out.append(f"**New results this week:** {_q(res['quarters'][-1]['quarter_end'])} is now in the score.")
    if week_ago:
        rows = []
        for p in s["pillars"]:
            a, b = _f(week_ago.get(p["name"])), p["score"]
            if b is None:
                continue
            rows.append(f"- {p['name']} ({p['weight']}%): **{b:.1f}**" + (f" (was {a:.1f})" if a is not None and abs(b - a) >= 0.05 else ""))
        out.append("**Pillars, now vs a week ago**\n" + "\n".join(rows))
    else:
        out.append("**Where each pillar stands**\n" + _standing(res))
    acc, ex = res.get("accountable") or {}, res.get("extra") or {}
    facts = []
    pw = _f(week_ago.get("price")) if week_ago else None
    facts.append(f"- Share price ${px['price']:.2f}" + (f", {(px['price'] / pw - 1) * 100:+.1f}% on the week" if pw else ""))
    if acc.get("contracts_last4") and acc.get("contracts_vs_proj") is not None:
        facts.append(f"- Acquisition contracts, last 4 weeks: {acc['contracts_last4']:,} "
                     f"({acc['contracts_vs_proj'] * 100:+.0f}% vs Opendoor's projected midpoint)")
    fo = ex.get("fallout") or []
    if fo:
        facts.append(f"- Signed contracts that fell through: {fo[-1]['rate'] * 100:.0f}% in {_q(fo[-1]['quarter'])} (estimate)")
    hr = ex.get("hit_rate")
    if hr and hr.get("items"):
        facts.append(f"- Guidance: {hr['met'] + hr['beat']} of {hr['items']} items met or beaten over the last {len(hr['quarters'])} quarters")
    out.append("**The numbers worth knowing**\n" + "\n".join(facts))
    out.append(_upcoming(res, today).strip())
    return subject, "\n\n".join(x for x in out if x) + _footer("weekly")


def _week(d):
    return f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"


def due(today_uk, latest_quarter, log):
    """Which emails are due. Results: the first run on which a newer quarter is in the score (on a Friday the weekly
    email carries the results instead, so nobody gets two). Weekly: Fridays. Never twice for the same quarter or week."""
    d = dt.date.fromisoformat(today_uk)
    if d.weekday() >= 5:
        return []
    out = []
    new_q = bool(log.get("results")) and latest_quarter > log["results"]
    if d.weekday() == 4 and log.get("weekly") != _week(d):
        out.append("weekly")
    elif new_q:
        out.append("results")
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--site", default=os.path.join(HERE, "site"))
    ap.add_argument("--log", default=os.path.join(HERE, "newsletter_log.json"))
    ap.add_argument("--setup", action="store_true", help=argparse.SUPPRESS)  # older workflow; nothing to set up now
    ap.add_argument("--send", action="store_true")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--preview", choices=["weekly", "results"])
    a = ap.parse_args()
    key = os.environ.get("BUTTONDOWN_API_KEY", "").strip()
    if a.preview:
        res, hist = load(a.site)
        subject, body = (results if a.preview == "results" else weekly)(res, hist)
        print(f"Subject: {subject}\n\n{body}")
        return
    if not key:
        print("BUTTONDOWN_API_KEY not set: newsletter off")
        return
    try:
        if a.test:
            res, hist = load(a.site)
            for kind, build in (("weekly", weekly), ("results", results)):
                subject, body = build(res, hist)
                em = api("POST", "/emails", key, {"subject": "[Test] " + subject, "body": body, "status": "draft"})
                try:
                    # test mode: rendered as for a subscriber, delivered only to the newsletter's own address
                    api("POST", f"/emails/{em['id']}/send-draft", key, {"subscribers": [], "recipients": []},
                        extra={"X-Buttondown-Test-Mode": "true"})
                    print(f"test {kind} emailed to you: {subject}")
                except urllib.error.HTTPError as e:
                    print(f"::warning::test {kind} saved as a draft in Buttondown but not emailed ({e.code}: "
                          f"{e.read().decode()[:200]}). Open it under Emails, Drafts, and use Send draft.")
        if a.send:
            res, hist = load(a.site)
            log = json.load(open(a.log)) if os.path.exists(a.log) else {}
            today, latest = res["generated"][:10], res["quarters"][-1]["quarter_end"]
            log.setdefault("results", latest)  # first run: today's quarter is not news
            for kind in due(today, latest, log):
                subject, body = (results if kind == "results" else weekly)(res, hist)
                api("POST", "/emails", key, {"subject": subject, "body": body, "status": "about_to_send"})
                if kind == "weekly":
                    log["weekly"] = _week(dt.date.fromisoformat(today))
                print(f"sent {kind}: {subject}")
            log["results"] = max(log["results"], latest)  # the weekly email covers new results on a Friday
            with open(a.log, "w") as fh:
                json.dump(log, fh, indent=1)
    except urllib.error.HTTPError as e:
        # a newsletter problem must never stop the scorecard; the warning shows in the Actions log
        print(f"::warning::Buttondown {e.code}: {e.read().decode()[:300]}")
    except Exception as e:
        print(f"::warning::newsletter skipped: {e}")


if __name__ == "__main__":
    sys.exit(main())
