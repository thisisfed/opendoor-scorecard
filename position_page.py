#!/usr/bin/env python3
"""Private position page: your $OPEN holding in pounds, locked with a passphrase.

Runs after the scorecard. It reads three repository secrets:
  POSITION_PASSPHRASE  the passphrase that unlocks the page (use a long one: it is the only thing protecting the page)
  POSITION_SHARES      how many shares you hold now, e.g. 1250.5
  POSITION_COST_USD    everything you have paid for them, in dollars, fees included, e.g. 6200
  POSITION_FINNHUB_KEY optional: a free finnhub.io API key, for live trades in the regular session and a live
                       pound/dollar rate. Pre-market and after-hours prices come from a relay on thisisfed.xyz
                       (opendoor-quote.php, in relay/ here) that asks CNBC and Nasdaq. The key stays in the blob.
  POSITION_OPENED      optional: the day you opened the position, e.g. 2026-10-14. The days before the log began are
                       then filled in from daily closes, assuming today's holding throughout (marked on the page).

Each run adds a line for the latest close: price in dollars, the pound/dollar rate (FRED series DEXUSUK, dollars per
pound; the ECB rate via Frankfurter if FRED does not answer), the value in pounds, and the gain or loss in pounds: the dollar gain or loss (value less cost) at that day's
rate. The day's figure is the change in that total, so money you add (a top-up raises POSITION_COST_USD) never shows
as a gain, and the days add up to the total.

Nothing readable leaves this script. The log and the page both carry the same blob, encrypted with AES-256-GCM under a
key derived from the passphrase (PBKDF2-SHA256, 310,000 rounds); the page decrypts it in your browser. The repository
and the site are public, so the share count and pound figures exist only inside that blob. Without the three secrets
the script does nothing.
"""
import base64
import csv
import datetime as dt
import json
import os
import secrets
import sys
import urllib.request

ITER = 310_000
LOG = "position_log.enc"
PAGE_DIR = os.path.join("site", "me")


def _b64(b):
    return base64.b64encode(b).decode()


def _key(passphrase, salt):
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode(), salt, ITER, dklen=32)


def encrypt(obj, passphrase, salt):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    iv = secrets.token_bytes(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(iv, json.dumps(obj, separators=(",", ":")).encode(), None)
    return {"v": 1, "kdf": "PBKDF2-SHA256", "iter": ITER, "salt": _b64(salt), "iv": _b64(iv), "ct": _b64(ct)}


def decrypt(blob, passphrase):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    salt = base64.b64decode(blob["salt"])
    pt = AESGCM(_key(passphrase, salt)).decrypt(base64.b64decode(blob["iv"]), base64.b64decode(blob["ct"]), None)
    return json.loads(pt)


def _fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (opendoor-scorecard)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def fx_series(start):
    """Dollars per pound by day, oldest first: ([(date, rate)], source). FRED's DEXUSUK (Federal Reserve noon rate) first,
    then the ECB reference rate via Frankfurter if FRED does not answer."""
    for attempt in (1,):   # FRED often does not answer GitHub's runners; do not wait long before the fallback
        try:
            txt = _fetch(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXUSUK&cosd={start}", 25)
            out = []
            for line in txt.strip().splitlines()[1:]:
                d, v = (line.split(",") + [""])[:2]
                if v not in ("", "."):
                    out.append((d[:10], float(v)))
            if out:
                return out, "Federal Reserve (FRED DEXUSUK)"
        except Exception as ex:
            print(f"Position page: FRED attempt {attempt} failed ({ex}).")
    for host in ("https://api.frankfurter.dev/v1", "https://api.frankfurter.app"):
        try:
            data = json.loads(_fetch(f"{host}/{start}..?from=GBP&to=USD", 40))
            out = sorted((d, float(v["USD"])) for d, v in data.get("rates", {}).items() if v.get("USD"))
            if out:
                return out, "ECB reference rate (Frankfurter)"
        except Exception as ex:
            print(f"Position page: {host} failed ({ex}).")
    raise RuntimeError("no exchange-rate source answered")


def fx_on(series, day):
    """The latest rate published on or before the day."""
    best = None
    for d, v in series:
        if d <= day:
            best = (d, v)
    return best


def entry(day, price, fx, fx_day, shares, cost_usd, prev, seed=False):
    """One close. Cost is in dollars; pound figures use that day's rate (fx = dollars per pound)."""
    val_usd = shares * price
    gain_usd = val_usd - cost_usd
    e = {"d": day, "px": round(price, 4), "fx": round(fx, 4), "fxd": fx_day, "sh": shares,
         "cost_usd": round(cost_usd, 2), "val_usd": round(val_usd, 2), "tot_usd": round(gain_usd, 2),
         "cost": round(cost_usd / fx, 2), "val": round(val_usd / fx, 2), "tot": round(gain_usd / fx, 2),
         "day": None, "dayfx": None}
    if seed:
        e["seed"] = True
    if prev:
        # the day's gain in pounds: the change in the pound total, so a top-up (cost and value up together) adds nothing
        e["day"] = round(e["tot"] - prev["tot"], 2)
        # the part that came from the pound/dollar rate alone: yesterday's dollar gain, revalued at today's rate
        e["dayfx"] = round(prev["tot_usd"] * (1 / fx - 1 / prev["fx"]), 2)
    return e


def closes(start):
    """Daily closes since start from the scorecard's own sources (Yahoo, Nasdaq, Stooq): [(date, close)]."""
    import opendoor_scorecard as sc
    for name, fetch in (("Yahoo", lambda: sc._yahoo("query1.finance.yahoo.com")), ("Yahoo", lambda: sc._yahoo("query2.finance.yahoo.com")),
                        ("Nasdaq", sc._nasdaq), ("Stooq", sc._stooq)):
        try:
            rows = [r for r in fetch() if r[0] >= start]
            if rows:
                return rows
        except Exception as ex:
            print(f"Position page: {name} closes failed ({str(ex)[:80]}).")
    return []


def rechain(entries):
    """Work out each day's change again after days are filled in. The first day's figure is its gain since buying."""
    out, prev = [], None
    for x in sorted(entries, key=lambda e: e["d"]):
        e = entry(x["d"], x["px"], x["fx"], x["fxd"], x["sh"], x["cost_usd"], prev, seed=x.get("seed", False))
        for k in ("src", "bf"):
            if k in x:
                e[k] = x[k]
        if prev is None and e.get("bf"):
            e["day"] = e["tot"]
        out.append(e)
        prev = e
    return out


def main():
    pw = os.environ.get("POSITION_PASSPHRASE", "")
    sh_s, cost_s = os.environ.get("POSITION_SHARES", ""), os.environ.get("POSITION_COST_USD", "")
    if not (pw and sh_s and cost_s):
        print("Position page: secrets not set, skipped.")
        return 0
    try:
        shares, cost = float(sh_s.replace(",", "")), float(cost_s.replace(",", "").replace("$", ""))
    except ValueError:
        print("Position page: POSITION_SHARES and POSITION_COST_USD must be plain numbers; skipped.")
        return 0
    if len(pw) < 12:
        print("Position page: warning, the passphrase is short; a long one is much harder to guess.")
    sc = json.load(open(os.path.join("site", "scorecard.json")))
    price, pday = float(sc["price"]["price"]), sc["price"]["date"][:10]

    salt, entries = None, []
    if os.path.exists(LOG):
        try:
            blob = json.load(open(LOG))
            entries = [x for x in decrypt(blob, pw)["entries"] if "cost_usd" in x]   # lines from the pound-cost version are dropped
            salt = base64.b64decode(blob["salt"])
        except Exception:
            print("Position page: the old log would not open with this passphrase (changed?); starting a new one.")
            entries, salt = [], None
    salt = salt or secrets.token_bytes(16)

    opened = os.environ.get("POSITION_OPENED", "").strip()[:10]
    try:
        dt.date.fromisoformat(opened)
    except ValueError:
        opened = ""
    start = (dt.date.fromisoformat(min(opened or pday, pday)) - dt.timedelta(days=10)).isoformat()
    try:
        series, src = fx_series(start)
    except Exception as ex:  # keep going on the last known rate rather than fail the run
        print(f"Position page: FX fetch failed ({ex}); using the last known rate.")
        series = [(entries[-1]["fxd"], entries[-1]["fx"])] if entries else []
        src = entries[-1].get("src", "") if entries else ""
    hit = fx_on(series, pday)
    if not hit:
        print("Position page: no pound/dollar rate available; skipped.")
        return 0

    entries = [x for x in entries if x["d"] != pday]          # a second run on the same close replaces it
    prev = entries[-1] if entries else None
    if prev is None:
        # first run: seed the previous close from the scorecard's own history, holding as today, so day one has a figure
        try:
            rows = [r for r in csv.DictReader(open(os.path.join("site", "history.csv"))) if r.get("price_date", "") < pday]
            if rows:
                r = rows[-1]
                h = fx_on(series, r["price_date"])
                if h:
                    prev = entry(r["price_date"], float(r["price"]), h[1], h[0], shares, cost, None, seed=True)
                    entries.append(prev)
        except FileNotFoundError:
            pass
    entries.append(entry(pday, price, hit[1], hit[0], shares, cost, prev))
    entries[-1]["src"] = src
    # fill in from the opening day, once: closes and rates for the days before the log began, today's holding throughout
    live = [x for x in entries if not x.get("seed") and not x.get("bf")]
    first_live = live[0]["d"] if live else pday
    if opened and opened < first_live and not any(x.get("bf") for x in entries):
        filled = []
        for d, c in closes(opened):
            if d >= first_live:
                break
            h = fx_on(series, d)
            if h:
                e = entry(d, c, h[1], h[0], shares, cost, None)
                e.update(bf=True, src=src)
                filled.append(e)
        if filled:
            entries = filled + live
            print(f"Position page: filled in {len(filled)} days from {filled[0]['d']}.")
    entries = rechain(entries)
    data = {"entries": entries[-400:], "updated": sc.get("generated"), "ticker": "OPEN", "sc": scorecard_view(sc),
            "events": results_days(), "opened": opened or None}
    live_key = os.environ.get("POSITION_FINNHUB_KEY", "").strip()
    if live_key:
        data["live"] = {"provider": "finnhub", "key": live_key}
    blob = encrypt(data, pw, salt)
    with open(LOG, "w") as fh:
        json.dump(blob, fh)
    os.makedirs(PAGE_DIR, exist_ok=True)
    with open(os.path.join(PAGE_DIR, "index.html"), "w") as fh:
        fh.write(PAGE.replace("__BLOB__", json.dumps(blob)))
    print(f"Position page: updated for the {pday} close ({len(entries)} days in the log).")
    return 0


def results_days():
    """Earnings release dates from the scorecard's release cache, to mark on the chart and in the context line."""
    try:
        cache = json.load(open("releases_cache.json"))
    except (OSError, ValueError):
        return []
    out = []
    for v in cache.values():
        q, f = str(v.get("quarter_end", "")), str(v.get("filed", ""))
        if len(q) >= 7 and f:
            out.append({"d": f[:10], "t": f"Q{(int(q[5:7]) + 2) // 3} {q[:4]} results"})
    return sorted(out, key=lambda e: e["d"])


def scorecard_view(sc):
    """The public scorecard's headline numbers, for the box beside the position. Facts only, no call."""
    try:
        score, ch, prev = sc["score"], sc.get("change") or {}, sc.get("previous") or {}
        pch = ch.get("pillars") or {}
        pillars = [{"n": p["name"], "w": p["weight"], "s": p["score"], "c": round(pch.get(p["name"], 0) or 0, 1)}
                   for p in score["pillars"]]
        return {"o": score["overall"], "g": score["grade"], "v": score.get("verdict", ""), "why": score.get("why", ""),
                "from": prev.get("overall"), "gfrom": ch.get("grade_from"), "since": (ch.get("since") or "")[:10],
                "q": (prev.get("stamps") or {}).get("quarter"), "p": pillars}
    except (KeyError, TypeError):
        return None


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow,noarchive"><meta name="referrer" content="no-referrer">
<title>Private</title>
<script>(function(){var d=document.documentElement,t=null;try{t=localStorage.getItem("theme")}catch(e){}
d.setAttribute("data-theme",t==="dark"||t==="light"?t:(matchMedia&&matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"))})();</script>
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#f3f2f0;--card:#fff;--soft:#f6f4f1;--line:#ebe7e2;--line2:#dcd6cf;--ink:#23201d;--muted:#72685e;--muted2:#5a524a;
--good:#047857;--good-bg:#e3f3ea;--bad:#b4302b;--bad-bg:#fbe9e7;--blue:#1d4ed8;--blue-bg:#e8eefc;--accent:#1d4ed8;--track:#f1efec;color-scheme:light}
:root[data-theme=dark]{--bg:#141312;--card:#1e1c1a;--soft:#272421;--line:#36312d;--line2:#4a443e;--ink:#f2ede7;--muted:#ada296;--muted2:#cbc2b8;
--good:#5fd49b;--good-bg:#173126;--bad:#ff8e82;--bad-bg:#3a201d;--blue:#7ea2ff;--blue-bg:#1d2540;--accent:#3563e9;--track:#322e2a;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 Inter,system-ui,sans-serif;
letter-spacing:-.011em;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:960px;margin:0 auto;padding:28px 18px 80px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
h1{font-size:22px;font-weight:600;letter-spacing:-.025em;margin:0}.sub{color:var(--muted);margin:2px 0 0;font-size:14px}
h2{font-size:15px;font-weight:600;margin:0;letter-spacing:-.01em}
.card{background:var(--card);border-radius:20px;padding:22px 24px;margin-top:16px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
:root[data-theme=dark] .card{box-shadow:0 0 0 1px var(--line)}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
input[type=password]{flex:1 1 240px;min-height:48px;font:inherit;font-size:16px;padding:0 14px;border:1px solid var(--line2);border-radius:12px;background:var(--card);color:var(--ink)}
button{min-height:48px;font:inherit;font-size:16px;font-weight:600;padding:0 20px;border:0;border-radius:12px;background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:none;color:var(--ink);border:1px solid var(--line2);font-weight:500;min-height:40px;font-size:14px}
label.rem{display:flex;align-items:center;gap:8px;min-height:44px;font-size:14px;color:var(--muted2);flex-basis:100%}
.err{color:var(--bad);margin:10px 0 0;min-height:1.5em}
.up{color:var(--good)}.dn{color:var(--bad)}.muted{color:var(--muted)}
.note{font-size:13px;color:var(--muted);margin:12px 0 0}
/* context line */
.ctx{margin:18px 0 0;font-size:14px;color:var(--muted2);display:flex;gap:10px;align-items:center}
.ctx i{width:7px;height:7px;border-radius:50%;background:var(--blue);flex:none}
/* hero */
.hero{padding:26px 28px 22px}
.h-top{display:flex;flex-wrap:wrap;align-items:center;gap:8px 14px}
.badge{display:inline-flex;align-items:center;gap:7px;font-size:13px;font-weight:600;padding:4px 11px 4px 9px;border-radius:999px;background:var(--soft);color:var(--muted2)}
.badge.on{background:var(--good-bg);color:var(--good)}
.lv-dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.badge.on .lv-dot{background:#2fb67c;animation:pulse 2s ease-out infinite}
.h-px{font-size:15px;display:flex;flex-wrap:wrap;gap:2px 8px;align-items:baseline}.h-px b{font-weight:600;font-size:16px}
.h-l{margin:20px 0 0;font-size:13.5px;color:var(--muted)}
.h-v{margin:0;font-size:56px;font-weight:600;letter-spacing:-.045em;line-height:1.08}
.h-pl{margin:6px 0 0;display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;font-size:14.5px}
.h-pl b{font-size:26px;font-weight:600;letter-spacing:-.025em}
.pill{font-weight:600;font-size:14px;padding:2px 9px;border-radius:999px;background:var(--soft)}
.pill.up{background:var(--good-bg)}.pill.dn{background:var(--bad-bg)}
.split{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:20px}
.split>div{background:var(--soft);border-radius:14px;padding:12px 14px;min-width:0}
.sl{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--muted)}
.split b{display:block;font-size:20px;font-weight:600;letter-spacing:-.02em;margin-top:2px}
.split small{display:block;font-size:12.5px;color:var(--muted)}
.fxbar{display:flex;height:6px;border-radius:3px;overflow:hidden;background:var(--track);margin-top:14px}
.fxbar i{display:block;height:100%}.fxbar .s{background:var(--blue)}.fxbar .f{background:var(--muted)}
.fxk{display:flex;flex-wrap:wrap;gap:4px 16px;margin:6px 0 0;font-size:12.5px;color:var(--muted)}
.fxk span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;background:var(--blue)}.fxk span.f::before{background:var(--muted)}
.h-t{margin:14px 0 0;font-size:12.5px;color:var(--muted)}
/* metrics */
.mx{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));padding:4px 6px}
.mx>div{padding:12px 14px;border-left:1px solid var(--line);min-width:0}.mx>div:first-child{border-left:0}
.mx b{display:block;font-size:17px;font-weight:600;letter-spacing:-.015em}.mx span{font-size:12.5px;color:var(--muted)}
.hl{display:flex;flex-wrap:wrap;gap:8px 24px;margin:0 -6px;padding:12px 20px 2px;border-top:1px solid var(--line);font-size:13.5px;color:var(--muted2);align-items:center}
.hl b{color:var(--ink);font-weight:600}.hl .tip{margin-left:4px;vertical-align:-3px}
.mcard{padding:12px 18px 14px}
/* chart */
.ch-h{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap}
.seg{display:inline-flex;background:var(--soft);border-radius:10px;padding:3px}
.seg button{min-height:34px;padding:0 13px;font-size:13px;font-weight:500;background:none;color:var(--muted2);border-radius:8px}
.seg button[aria-pressed=true]{background:var(--card);color:var(--ink);box-shadow:0 1px 2px rgba(0,0,0,.1)}
.chw{position:relative;margin-top:14px;touch-action:pan-y;user-select:none;-webkit-user-select:none}
.chw svg{display:block;overflow:visible;cursor:crosshair}
.chw svg:focus{outline:none}.chw svg:focus-visible{outline:2px solid var(--blue);outline-offset:4px;border-radius:6px}
.chw text{font-size:11.5px;fill:var(--muted)}
.cht{position:absolute;top:4px;pointer-events:none;background:var(--card);border:1px solid var(--line2);border-radius:12px;padding:9px 11px;font-size:12.5px;box-shadow:0 6px 18px rgba(0,0,0,.12);min-width:168px;z-index:2}
.cht b{display:block;font-size:13px;margin-bottom:2px}.cht p{margin:1px 0 0;display:flex;justify-content:space-between;gap:14px}.cht p span:first-child{color:var(--muted)}
.lg{display:flex;flex-wrap:wrap;gap:6px 16px;margin:12px 0 0;font-size:12.5px;color:var(--muted)}
.lg i{display:inline-block;width:16px;height:0;border-top:2.5px solid var(--blue);vertical-align:middle;margin-right:6px}
.lg i.c{border-top:2px dashed var(--muted)}.lg i.e{width:0;height:12px;border-top:0;border-left:1.5px dashed var(--muted2)}
.lg i.m{width:9px;height:9px;border:2px solid var(--blue);border-radius:50%;background:var(--card)}
/* day by day */
table.days{width:100%;border-collapse:separate;border-spacing:0;font-size:14px;margin-top:10px}
.days th{font-size:12px;font-weight:500;color:var(--muted);text-align:right;padding:8px 12px;border-bottom:1px solid var(--line)}
.days td{text-align:right;padding:11px 12px;border-bottom:1px solid var(--line);white-space:nowrap}
.days th:first-child,.days td:first-child{text-align:left}
.days td small{font-size:12px;margin-left:6px}
.days td.dy,.days td.tt{font-weight:600}
.days tr.cur td{background:var(--blue-bg);border-bottom-color:transparent}
.days tr.cur td:first-child{border-radius:12px 0 0 12px}.days tr.cur td:last-child{border-radius:0 12px 12px 0}
.tag{font-size:10.5px;font-weight:600;color:var(--blue);margin-left:8px;text-transform:uppercase;letter-spacing:.05em}
.est{color:var(--muted);text-decoration:none;cursor:help;margin-left:2px}
tbody[hidden]{display:none!important}.more-days{margin-top:12px}
/* scorecard box */
.sc-top{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}.sc-o{font-size:30px;font-weight:600;letter-spacing:-.03em}
.sc-g{font-size:14px;font-weight:600;border:1px solid var(--line2);border-radius:999px;padding:2px 10px}
.sc-p{list-style:none;margin:14px 0 0;padding:0;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
.sc-p li{background:var(--soft);border-radius:12px;padding:10px 12px;font-size:13px;color:var(--muted)}.sc-p b{display:block;font-size:17px;color:var(--ink);font-weight:600}.sc-p b.up{color:var(--good)}.sc-p b.dn{color:var(--bad)}
input:focus{outline:2px solid var(--blue);outline-offset:1px}
/* how it's worked out */
.how ul{margin:10px 0 0;padding-left:18px;font-size:13.5px;color:var(--muted2)}.how li{margin:4px 0}
/* tooltips */
.tip{display:inline-grid;place-items:center;width:17px;height:17px;min-height:0;padding:0;border-radius:50%;border:1px solid var(--line2);background:none;color:var(--muted);font-size:10.5px;font-weight:600;cursor:help;position:relative;flex:none;line-height:1}
.tip::before{content:"";position:absolute;inset:-13px}
.tip:focus-visible{outline:2px solid var(--blue);outline-offset:2px}
.tipbox{position:absolute;z-index:20;max-width:min(290px,calc(100vw - 16px));background:var(--ink);color:var(--card);font-size:12.5px;line-height:1.45;padding:9px 11px;border-radius:10px;box-shadow:0 6px 18px rgba(0,0,0,.2);pointer-events:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,182,124,.5)}70%{box-shadow:0 0 0 8px rgba(47,182,124,0)}100%{box-shadow:0 0 0 0 rgba(47,182,124,0)}}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(47,182,124,.9),0 0 10px 2px rgba(47,182,124,.9)}100%{box-shadow:0 0 0 12px rgba(47,182,124,0),0 0 0 0 rgba(47,182,124,0)}}
.badge.on .lv-dot.ping{animation:ping .9s ease-out}
@media (prefers-reduced-motion:reduce){.lv-dot,.badge.on .lv-dot,.badge.on .lv-dot.ping{animation:none}}
@media (max-width:760px){.mx{grid-template-columns:repeat(2,minmax(0,1fr));padding:4px 0}.mx>div{border-left:0;border-top:1px solid var(--line)}
.mx>div:nth-child(-n+2){border-top:0}.mx>div:last-child{grid-column:1/-1}}
@media (max-width:640px){.wrap{padding:20px 12px 64px}.card{padding:18px 16px;border-radius:18px}.hero{padding:20px 18px 18px}
.h-v{font-size:44px}.h-pl b{font-size:22px}.split{grid-template-columns:1fr 1fr}.split>div:last-child{grid-column:1/-1}.split b{font-size:18px}
.sc-p{grid-template-columns:1fr 1fr}.hl{padding:12px 14px 2px}
.days thead{display:none}.days,.days tbody{display:block}
.days tr{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px 8px;padding:12px 6px;border-bottom:1px solid var(--line)}
.days tr.cur{background:var(--blue-bg);border-radius:12px;border-bottom-color:transparent;padding:12px 10px}.days tr.cur td{background:none}
.days td{border:0;padding:0;text-align:left;font-size:13.5px;min-width:0}.days td small{display:none}
.days td::before{content:attr(data-l);display:block;font-size:11px;font-weight:400;color:var(--muted)}
.days td.dt{grid-row:1;grid-column:1/3;font-weight:600;font-size:15px}.days td.dt::before{content:none}
.days td.dy{grid-row:1;grid-column:3/5;text-align:right;font-size:15px}.days td.dy::before{display:inline;margin-right:6px;font-size:12px}}
</style></head><body><div class="wrap">
<div class="top"><div><h1>Your OPEN position</h1><p class="sub">In pounds. Private: unlocks only with your passphrase.</p></div>
<button class="ghost" id="lock" hidden>Lock</button></div>
<div class="card" id="gate"><form id="f"><input type="password" id="pw" autocomplete="current-password" placeholder="Passphrase" aria-label="Passphrase" required>
<button type="submit">Unlock</button><label class="rem"><input type="checkbox" id="rem"> Stay unlocked on this device</label></form><p class="err" id="err" role="alert"></p></div>
<div id="out" hidden></div></div>
<script>
var BLOB = __BLOB__;
function b64(s) { return Uint8Array.from(atob(s), function (c) { return c.charCodeAt(0); }); }
function u8(b) { return btoa(String.fromCharCode.apply(null, new Uint8Array(b))); }
async function derive(pw) {
  var base = await crypto.subtle.importKey("raw", new TextEncoder().encode(pw), "PBKDF2", false, ["deriveKey"]);
  return crypto.subtle.deriveKey({name: "PBKDF2", salt: b64(BLOB.salt), iterations: BLOB.iter, hash: "SHA-256"}, base,
    {name: "AES-GCM", length: 256}, true, ["encrypt", "decrypt"]);
}
async function open_(key) {
  var pt = await crypto.subtle.decrypt({name: "AES-GCM", iv: b64(BLOB.iv)}, key, b64(BLOB.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
// ---- formatting
var MINUS = "−";
var gbp = function (v, sign) { var s = Math.abs(v).toLocaleString("en-GB", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return (v < -0.004 ? MINUS : sign && v > 0.004 ? "+" : "") + "£" + s; };
var usd = function (v, sign, dp) { dp = dp == null ? 2 : dp; var s = Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: dp, maximumFractionDigits: dp});
  return (v < 0 && s.replace(/[0.,]/g, "") ? MINUS : sign && v > 0 ? "+" : "") + "$" + s; };
var pct = function (v) { return (v < 0 ? MINUS : "+") + Math.abs(v * 100).toFixed(2) + "%"; };
var cls = function (v) { return v > 0.004 ? "up" : v < -0.004 ? "dn" : ""; };       // pound amounts
var clsr = function (v) { return v > 0 ? "up" : v < 0 ? "dn" : ""; };               // ratios
var dlab = function (d) { var p = String(d).slice(0, 10).split("-"); return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : String(d); };
var px$ = function (v) { return "$" + v.toFixed(v < 10 ? 4 : 2); };
function esc(t) { return String(t == null ? "" : t).replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
function tip(t) { return '<button type="button" class="tip" aria-label="' + esc(t) + '" data-tip="' + esc(t) + '">?</button>'; }
function $(id) { return document.getElementById(id); }
function set(id, t, c) { var e = $(id); if (!e) return; e.textContent = t; if (c !== undefined) e.className = c; }
var DAY = 864e5;
var ymd = function (ms) { return new Date(ms).toLocaleDateString("en-CA", {timeZone: "Europe/London"}); };

// ---- one definition of profit and loss, used everywhere on the page.
// Money put in is counted in pounds at the rate on the day it went in. Total P&L = value in pounds - pounds put in.
// It splits exactly into the share price part (the dollar gain at today's rate) and the pound/dollar part (the rest:
// what the rate has done to the pounds you put in). The day figure is the change in total P&L, so new money never counts.
function enrich(E) {
  var X = [], cg = 0, idx = 1, peak = 1, peakD = null, dd = {v: 0, from: null, to: null};
  E.forEach(function (e, i) {
    var p = X[i - 1] || null;
    var add = p ? (e.cost_usd - p.cost_usd) / e.fx : e.cost_usd / e.fx;
    cg += add;
    var pl = e.val - cg, stk = (e.val_usd - e.cost_usd) / e.fx;
    // value index that ignores money added, for the drawdown (starts at 1 = what you paid)
    idx = p ? idx * (e.val - add) / p.val : e.val / cg;
    if (idx > peak) { peak = idx; peakD = e.d; }
    if (idx / peak - 1 < dd.v) dd = {v: idx / peak - 1, from: peakD, to: e.d};
    X.push(Object.assign({}, e, {add: add, cg: cg, pl: pl, stk: stk, fxp: pl - stk, dpl: p ? pl - p.pl : pl,
      ret: p ? e.px / p.px - 1 : null, est: !!(e.seed || e.bf)}));
  });
  X.dd = dd; X.off = idx / peak - 1; X.peakD = peakD;
  return X;
}
function held(X, opened) {
  var now = Date.now(), start = opened && opened < X[0].d ? opened : X[0].d;
  var days = Math.max(0, Math.round((now - Date.parse(start + "T12:00:00Z")) / DAY)), w = 0, s = 0;
  X.forEach(function (x) { if (x.add > 0) { w += x.add; s += x.add * (now - Date.parse(x.d + "T12:00:00Z")) / DAY; } });
  return {days: days, start: start, avg: w ? Math.round(s / w) : days};
}
function context(ev) {
  if (!ev || !ev.length) return "";
  var today = ymd(Date.now()), last = null;
  ev.forEach(function (e) { if (e.d <= today) last = e; });
  if (!last) return "";
  var n = Math.round((Date.parse(today) - Date.parse(last.d)) / DAY);
  if (n > 7) return "";
  return "Opendoor reported its " + last.t + " " + (n === 0 ? "today" : n === 1 ? "yesterday" : n + " days ago") + " (" + dlab(last.d) + ").";
}

var STATE = null;
function render(D, key) {
  var X = enrich(D.entries), L = X[X.length - 1], P = X.length > 1 ? X[X.length - 2] : null, avg = L.cost_usd / L.sh, H = held(X, D.opened);
  STATE = {D: D, X: X, L: L, P: P, avg: avg, range: "all", q: null};
  var ctx = context(D.events), src = /ecb|frankfurter/i.test(L.src || "") ? "ECB" : "Fed";
  var h = (ctx ? '<p class="ctx"><i aria-hidden="true"></i>' + esc(ctx) + '</p>' : '') +
    // 1. hero: one primary number, then the P&L, then where it came from
    '<section class="card hero" aria-labelledby="h-l">' +
      '<div class="h-top"><span class="badge" id="h-badge"><span class="lv-dot" aria-hidden="true"></span><span id="h-sess">At close</span></span>' +
        '<span class="h-px"><b id="h-pxv"></b><span id="h-pxc"></span><span class="muted" id="h-ref"></span></span></div>' +
      '<p class="h-l" id="h-l">Value now, <span id="h-vt"></span></p>' +
      '<p class="h-v" id="h-val" aria-live="polite"></p>' +
      '<div class="h-pl"><b id="h-pl"></b><span id="h-plp"></span><span class="muted">unrealised, vs <span id="h-in"></span> put in</span>' +
        tip("Value now in pounds minus the pounds you put in, each amount at the pound/dollar rate on the day it went in.") + '</div>' +
      '<div class="split">' +
        '<div><span class="sl">Share price ' + tip("What the share price did: your dollar gain or loss, converted at today's rate.") + '</span><b id="h-stk"></b><small id="h-stks"></small></div>' +
        '<div><span class="sl">Pound vs dollar ' + tip("What the exchange rate did: the dollars you put in are worth this much more (or less) in pounds now than on the days you bought. The two parts add up to the total.") + '</span><b id="h-fx"></b><small id="h-fxs"></small></div>' +
        '<div><span class="sl"><span id="h-dl">Today</span> ' + tip("Change in total P&L since the reference close, including any move in the exchange rate. Money you add never counts.") + '</span><b id="h-day"></b><small id="h-days"></small></div>' +
      '</div>' +
      '<div class="fxbar" aria-hidden="true"><i class="s" id="h-bs"></i><i class="f" id="h-bf"></i></div>' +
      '<p class="fxk"><span id="h-ks"></span><span class="f" id="h-kf"></span></p>' +
      '<p class="h-t" id="h-t"></p>' +
    '</section>' +
    // 2. compact facts
    '<section class="card mcard" aria-label="Position facts"><div class="mx">' +
      '<div><b>' + L.sh.toLocaleString("en-GB", {maximumFractionDigits: 4}) + '</b><span>shares</span></div>' +
      '<div><b>$' + avg.toFixed(3) + '</b><span>average cost, breakeven</span></div>' +
      '<div><b>$' + L.px.toFixed(2) + '</b><span>last close, ' + dlab(L.d) + '</span></div>' +
      '<div><b id="m-fx"></b><span id="m-fxs"></span></div>' +
      '<div><b id="m-usd"></b><span>unrealised in dollars <span id="m-usdp"></span></span></div></div>' +
      '<div class="hl">' +
        '<span>Max drawdown <b class="' + clsr(X.dd.v) + '">' + (X.dd.v < -0.00005 ? pct(X.dd.v) : "none yet") + '</b>' +
          (X.dd.to ? ' <span class="muted">' + (X.dd.from ? dlab(X.dd.from) : "from buying") + ' to ' + dlab(X.dd.to) + '</span>' : '') + ' ' +
        tip("The worst fall from a high, on a value index that ignores money you added. Based on daily closes, starting from what you paid.") + '</span>' +
        '<span>From the high <b class="' + clsr(X.off) + '">' + (X.off < -0.00005 ? pct(X.off) : "at the high") + '</b></span>' +
        '<span>Held <b>' + H.days + ' day' + (H.days === 1 ? '' : 's') + '</b> <span class="muted">since ' + dlab(H.start) +
          (Math.abs(H.avg - H.days) >= 1 ? ', money in for ' + H.avg + ' days on average' : '') + '</span></span>' +
      '</div></section>' +
    // 3. chart
    '<section class="card" aria-labelledby="ch-t"><div class="ch-h"><h2 id="ch-t">Value and money put in</h2>' +
      '<div class="seg" role="group" aria-label="Chart range">' +
        ['1m:1M', '3m:3M', 'all:All'].map(function (r) { var p = r.split(":"); return '<button type="button" data-r="' + p[0] + '" aria-pressed="' + (p[0] === "all") + '">' + p[1] + '</button>'; }).join("") +
      '</div></div><div class="chw" id="chw"></div>' +
      '<p class="lg"><span><i></i>Value</span><span><i class="c"></i>Money put in</span><span><i class="e"></i>Results day</span><span><i class="m"></i>Move of 10% or more</span></p></section>' +
    daysHTML(X) + scCard(D.sc, L, avg) +
    // how
    '<section class="card how" aria-labelledby="w-t"><h2 id="w-t">How the figures are worked out</h2><ul>' +
      '<li>Your cost is in dollars. <b>Money put in</b> counts each dollar amount in pounds at the rate on the day it went in.</li>' +
      '<li><b>Total P&amp;L</b> is the value in pounds minus the pounds put in. It splits into the <b>share price</b> part (your dollar gain at today\'s rate) and the <b>pound vs dollar</b> part (the rest), which always add up to the total.</li>' +
      '<li><b>Day P&amp;L</b> is the change in total P&amp;L from one close to the next, so money you add never counts as a gain.</li>' +
      '<li>Rates are the dollars per pound for each day, or the latest before it, from ' + esc(L.src || "the Federal Reserve (FRED DEXUSUK)") + '. Live figures use a live pound/dollar rate from Finnhub when one arrives, otherwise the latest daily rate.</li>' +
      '<li>Live prices, pre-market and after-hours included, come from CNBC and Nasdaq through a small relay on thisisfed.xyz, and from Finnhub trades in the regular session.</li>' +
      (X.some(function (x) { return x.est; }) ? '<li><span class="est">*</span> Days before the daily log began are worked out from that day\'s close and rate, assuming your current holding throughout.</li>' : '') +
    '</ul><p class="note">Log updated ' + (D.updated ? dlab(D.updated) + " " + D.updated.slice(11, 16) : "") + ' UK. Not advice.</p></section>';
  $("out").innerHTML = h; $("out").hidden = false;
  paint(null);
  drawChart();
  document.querySelectorAll(".seg button").forEach(function (b) { b.addEventListener("click", function () {
    STATE.range = b.dataset.r; document.querySelectorAll(".seg button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); }); drawChart(); }); });
  var md = $("more-days");
  if (md) md.addEventListener("click", function () {
    var t = $("older"), open = t.hidden, n = +md.dataset.n;
    t.hidden = !open; md.setAttribute("aria-expanded", open ? "true" : "false");
    md.textContent = open ? "Hide earlier days" : "Show " + n + " earlier day" + (n === 1 ? "" : "s");
  });
  var rt = null; addEventListener("resize", function () { clearTimeout(rt); rt = setTimeout(drawChart, 150); });
  tips(); live(D, L);
  $("gate").hidden = true; $("lock").hidden = false;
}

// ---- the hero and live facts: q is null (at the latest close) or a live quote {px, fx, fxLive, ref, sess}
var SESS = {pre: "Pre-market", regular: "Live", after: "After hours", closed: "Closed"};
function paint(q) {
  var S = STATE, L = S.L, P = S.P, sh = L.sh;
  var px = q ? q.px : L.px, fx = q ? q.fx : L.fx;
  // at the close, use the logged figures so the hero and the table agree to the penny
  var val = q ? sh * px / fx : L.val, pl = val - L.cg, stk = q ? (sh * px - L.cost_usd) / fx : L.stk, fxp = pl - stk, g$ = sh * px - L.cost_usd;
  set("h-val", gbp(val));
  set("h-vt", q ? (q.sess === "closed" ? "at the last trade" : "live") : "at the close on " + dlab(L.d));
  set("h-pl", gbp(pl, true), cls(pl)); set("h-plp", pct(pl / L.cg), "pill " + clsr(pl)); set("h-in", gbp(L.cg));
  set("h-stk", gbp(stk, true), cls(stk)); set("h-stks", pct(g$ / L.cost_usd) + " in dollars");
  set("h-fx", gbp(fxp, true), cls(fxp)); set("h-fxs", "$" + (L.cost_usd / L.cg).toFixed(4) + " per £ when bought, $" + fx.toFixed(4) + " now");
  var ref, d, dp, dl;
  if (q) { ref = q.ref; d = pl - (sh * ref / L.fx - L.cg); dp = px / ref - 1; dl = q.sess === "regular" ? "Today" : "Since the last close"; }
  else { ref = P ? P.px : null; d = L.dpl; dp = L.ret; dl = "Day, " + dlab(L.d); }
  set("h-dl", dl); set("h-day", gbp(d, true), cls(d)); set("h-days", dp == null ? "first day" : pct(dp) + " share price");
  // where the P&L comes from, by size
  var a = Math.abs(stk), b = Math.abs(fxp), t = a + b || 1;
  $("h-bs").style.width = (a / t * 100) + "%"; $("h-bf").style.width = (b / t * 100) + "%";
  set("h-ks", "Share price " + Math.round(a / t * 100) + "%"); set("h-kf", "Pound vs dollar " + Math.round(b / t * 100) + "%");
  // price line and badge
  set("h-pxv", px$(px));
  if (ref) { var c = px - ref; set("h-pxc", (c < 0 ? MINUS : "+") + "$" + Math.abs(c).toFixed(4) + " (" + pct(c / ref) + ")", clsr(c)); }
  set("h-ref", q ? "vs the " + (q.sess === "regular" ? "previous" : "last") + " close, $" + ref.toFixed(2) : P ? "close " + dlab(L.d) + " vs " + dlab(P.d) : "close " + dlab(L.d));
  var on = q && q.sess !== "closed";
  $("h-badge").className = "badge" + (on ? " on" : ""); set("h-sess", q ? SESS[q.sess] : "At close");
  if (!q) set("h-t", "Waiting for a live price; showing the close on " + dlab(L.d) + ".");
  set("m-fx", "$" + fx.toFixed(4)); set("m-fxs", "per £1, " + (q && q.fxLive ? "live" : (/ecb|frankfurter/i.test(L.src || "") ? "ECB " : "Fed ") + dlab(L.fxd)));
  set("m-usd", usd(g$, true), cls(g$)); set("m-usdp", "(" + pct(g$ / L.cost_usd) + ")");
}

// ---- chart: value and money put in, hover or tap for a day, arrow keys to step
function drawChart() {
  var S = STATE, all = S.X, box = $("chw"); if (!box) return;
  if (all.length < 2) { box.innerHTML = '<p class="note">The chart starts once there are two days in the log.</p>'; return; }
  var last = Date.parse(all[all.length - 1].d), cut = S.range === "1m" ? 31 : S.range === "3m" ? 92 : 1e9;
  var X = all.filter(function (x) { return (last - Date.parse(x.d)) / DAY <= cut; }); if (X.length < 2) X = all.slice(-2);
  var W = Math.round(Math.max(300, box.clientWidth || 600)), H = W < 520 ? 220 : 270, Lp = 62, R = 14, T = 22, B = 28;
  var vs = []; X.forEach(function (x) { vs.push(x.val, x.cg); });
  var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs), pad = (hi - lo) * 0.12 || hi * 0.05 || 1; lo -= pad; hi += pad;
  var n = X.length, step = (W - Lp - R) / (n - 1);
  var xs = function (i) { return Lp + i * step; }, ys = function (v) { return T + (1 - (v - lo) / (hi - lo)) * (H - T - B); };
  var pts = function (k) { return X.map(function (x, i) { return xs(i).toFixed(1) + "," + ys(x[k]).toFixed(1); }).join(" "); };
  var g = "", dpp = hi - lo < 60 ? 2 : 0;
  [lo + pad, (lo + hi) / 2, hi - pad].forEach(function (v) {
    g += '<line x1="' + Lp + '" x2="' + (W - R) + '" y1="' + ys(v).toFixed(1) + '" y2="' + ys(v).toFixed(1) + '" stroke="var(--line)"/>' +
      '<text x="' + (Lp - 8) + '" y="' + (ys(v) + 4).toFixed(1) + '" text-anchor="end">£' + v.toLocaleString("en-GB", {minimumFractionDigits: dpp, maximumFractionDigits: dpp}) + '</text>'; });
  // results days, at the first close on or after the release
  (S.D.events || []).forEach(function (e) {
    var i = -1; X.forEach(function (x, j) { if (i < 0 && x.d >= e.d) i = j; });
    if (i < 0 || e.d < X[0].d) return;
    var x = xs(i), anchor = x > W - 90 ? "end" : x < Lp + 60 ? "start" : "middle";
    g += '<line x1="' + x.toFixed(1) + '" x2="' + x.toFixed(1) + '" y1="' + (T - 4) + '" y2="' + (H - B) + '" stroke="var(--muted2)" stroke-dasharray="3 4" stroke-width="1.2"/>' +
      '<text x="' + x.toFixed(1) + '" y="' + (T - 9) + '" text-anchor="' + anchor + '">' + esc(e.t.replace(" results", "")) + ' results</text>';
  });
  var area = "M" + xs(0).toFixed(1) + "," + (H - B) + " L" + pts("val").replace(/ /g, " L") + " L" + xs(n - 1).toFixed(1) + "," + (H - B) + " Z";
  var marks = X.map(function (x, i) { return x.ret !== null && Math.abs(x.ret) >= 0.1 ?
    '<circle cx="' + xs(i).toFixed(1) + '" cy="' + ys(x.val).toFixed(1) + '" r="4.5" fill="var(--card)" stroke="var(--blue)" stroke-width="2"><title>' + dlab(x.d) + ': share price ' + pct(x.ret) + '</title></circle>' : ""; }).join("");
  var lab = '<text x="' + Lp + '" y="' + (H - 7) + '">' + dlab(X[0].d) + '</text><text x="' + (W - R) + '" y="' + (H - 7) + '" text-anchor="end">' + dlab(X[n - 1].d) + '</text>' +
    (W >= 520 && n > 4 ? '<text x="' + xs(Math.floor((n - 1) / 2)).toFixed(1) + '" y="' + (H - 7) + '" text-anchor="middle">' + dlab(X[Math.floor((n - 1) / 2)].d) + '</text>' : '');
  box.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" tabindex="0" role="img" aria-label="Value and money put in from ' + dlab(X[0].d) + ' to ' + dlab(X[n - 1].d) +
      '. Use the arrow keys to step through the days.">' + g +
    '<path d="' + area + '" fill="var(--blue)" opacity=".07"/>' +
    '<polyline points="' + pts("cg") + '" fill="none" stroke="var(--muted)" stroke-dasharray="5 4" stroke-width="1.6"/>' +
    '<polyline points="' + pts("val") + '" fill="none" stroke="var(--blue)" stroke-width="2.4" stroke-linejoin="round" stroke-linecap="round"/>' + marks + lab +
    '<line id="cx" x1="0" x2="0" y1="' + T + '" y2="' + (H - B) + '" stroke="var(--ink)" stroke-opacity=".3" visibility="hidden"/>' +
    '<circle id="cd2" r="4" fill="var(--muted)" visibility="hidden"/><circle id="cd1" r="5.5" fill="var(--blue)" stroke="var(--card)" stroke-width="2" visibility="hidden"/>' +
    '</svg><div class="cht" id="cht" hidden role="status" aria-live="polite"></div>';
  var svg = box.querySelector("svg"), tipEl = $("cht"), cur = -1;
  function show(i) {
    i = Math.max(0, Math.min(n - 1, i)); cur = i; var x = X[i], cx = xs(i);
    ["cx", "cd1", "cd2"].forEach(function (id) { $(id).setAttribute("visibility", "visible"); });
    $("cx").setAttribute("x1", cx); $("cx").setAttribute("x2", cx);
    $("cd1").setAttribute("cx", cx); $("cd1").setAttribute("cy", ys(x.val)); $("cd2").setAttribute("cx", cx); $("cd2").setAttribute("cy", ys(x.cg));
    tipEl.innerHTML = '<b>' + dlab(x.d) + (x.est ? ' <span class="est">* estimated</span>' : '') + '</b>' +
      '<p><span>Value</span><span>' + gbp(x.val) + '</span></p><p><span>Put in</span><span>' + gbp(x.cg) + '</span></p>' +
      '<p><span>Total P&amp;L</span><span class="' + cls(x.pl) + '">' + gbp(x.pl, true) + ' (' + pct(x.pl / x.cg) + ')</span></p>' +
      '<p><span>Day</span><span class="' + cls(x.dpl) + '">' + gbp(x.dpl, true) + '</span></p>' +
      '<p><span>Close</span><span>$' + x.px.toFixed(2) + (x.ret === null ? '' : ' (' + pct(x.ret) + ')') + '</span></p>';
    tipEl.hidden = false;
    var r = svg.getBoundingClientRect(), k = r.width / W, left = cx * k, tw = tipEl.offsetWidth;
    tipEl.style.left = Math.max(0, Math.min(r.width - tw, left > r.width / 2 ? left - tw - 14 : left + 14)) + "px";
  }
  function hide() { cur = -1; tipEl.hidden = true; ["cx", "cd1", "cd2"].forEach(function (id) { $(id).setAttribute("visibility", "hidden"); }); }
  function at(ev) { var r = svg.getBoundingClientRect(); return Math.round(((ev.clientX - r.left) * W / r.width - Lp) / step); }
  svg.addEventListener("pointermove", function (ev) { show(at(ev)); });
  svg.addEventListener("pointerdown", function (ev) { show(at(ev)); });
  svg.addEventListener("pointerleave", function (ev) { if (ev.pointerType === "mouse") hide(); });
  svg.addEventListener("blur", hide);
  svg.addEventListener("keydown", function (ev) {
    var k = ev.key, i = cur < 0 ? n - 1 : cur;
    if (k === "ArrowLeft") show(cur < 0 ? n - 1 : i - 1); else if (k === "ArrowRight") show(cur < 0 ? n - 1 : i + 1);
    else if (k === "Home") show(0); else if (k === "End") show(n - 1); else if (k === "Escape") hide(); else return;
    ev.preventDefault();
  });
}

// ---- day by day: latest close highlighted, the last 7 shown, earlier days behind a toggle
function daysHTML(X) {
  var rows = X.slice().reverse().map(function (x, i) {
    return '<tr' + (i === 0 ? ' class="cur"' : '') + '><td class="dt">' + dlab(x.d) +
      (x.est ? '<abbr class="est" title="Estimated from that day\'s close and rate, assuming your current holding">*</abbr>' : '') + (i === 0 ? '<span class="tag">Latest</span>' : '') + '</td>' +
      '<td data-l="Close">$' + x.px.toFixed(2) + (x.ret === null ? '' : '<small class="' + clsr(x.ret) + '">' + pct(x.ret) + '</small>') + '</td>' +
      '<td data-l="$ per £">' + x.fx.toFixed(4) + '</td><td data-l="Value">' + gbp(x.val) + '</td>' +
      '<td data-l="Day" class="dy ' + cls(x.dpl) + '">' + gbp(x.dpl, true) + '</td>' +
      '<td data-l="Total" class="tt ' + cls(x.pl) + '">' + gbp(x.pl, true) + '</td></tr>'; });
  var older = rows.length - 7;
  return '<section class="card" aria-labelledby="d-t"><div class="ch-h"><h2 id="d-t">Day by day</h2>' +
    tip("Day P&L is the change in total P&L from the close before. Total P&L is value minus the pounds put in, as in the hero.") + '</div>' +
    '<table class="days"><thead><tr><th scope="col">Close</th><th scope="col">Share price</th><th scope="col">$ per £</th><th scope="col">Value</th><th scope="col">Day P&amp;L</th><th scope="col">Total P&amp;L</th></tr></thead>' +
    '<tbody>' + rows.slice(0, 7).join("") + '</tbody>' + (older > 0 ? '<tbody id="older" hidden>' + rows.slice(7).join("") + '</tbody>' : '') + '</table>' +
    (older > 0 ? '<button type="button" class="ghost more-days" id="more-days" aria-expanded="false" aria-controls="older" data-n="' + older + '">Show ' + older + ' earlier day' + (older === 1 ? '' : 's') + '</button>' : '') +
    (X.some(function (x) { return x.est; }) ? '<p class="note"><span class="est">*</span> Estimated: days before the daily log began, worked out from that day\'s close and rate, assuming your current holding. The first is the gain since buying.</p>' : '') +
    '</section>';
}

// ---- tooltips: one floating box, on hover, keyboard focus or tap, kept inside the screen
function tips() {
  var box = document.createElement("div"); box.className = "tipbox"; box.id = "tipbox"; box.setAttribute("role", "tooltip"); box.hidden = true; document.body.appendChild(box);
  var cur = null;
  function show(el) { cur = el; box.textContent = el.dataset.tip; box.hidden = false; el.setAttribute("aria-describedby", "tipbox");
    var r = el.getBoundingClientRect(), w = box.offsetWidth, x = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), innerWidth - w - 8);
    box.style.left = (x + scrollX) + "px"; box.style.top = (r.bottom + 8 + scrollY) + "px"; }
  function hide() { if (cur) cur.removeAttribute("aria-describedby"); cur = null; box.hidden = true; }
  document.addEventListener("mouseover", function (e) { var t = e.target.closest && e.target.closest(".tip"); if (t) show(t); });
  document.addEventListener("mouseout", function (e) { if (e.target.closest && e.target.closest(".tip")) hide(); });
  document.addEventListener("focusin", function (e) { if (e.target.classList && e.target.classList.contains("tip")) show(e.target); });
  document.addEventListener("focusout", function (e) { if (e.target.classList && e.target.classList.contains("tip")) hide(); });
  document.addEventListener("click", function (e) { var t = e.target.closest && e.target.closest(".tip"); if (t) show(t); else hide(); });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") hide(); });
  addEventListener("scroll", function () { if (cur && !cur.matches(":focus")) hide(); }, {passive: true});
}

// The public scorecard's numbers next to the position. It lines up the facts; add, trim or hold stays your call.
function scCard(S, L, avg) {
  if (!S) return "";
  var mv = S.p.filter(function (p) { return Math.abs(p.c) >= 0.05; }).sort(function (a, b) { return Math.abs(b.c) - Math.abs(a.c); }).slice(0, 3);
  var lo = S.p.slice().sort(function (a, b) { return a.s - b.s; })[0], hi = S.p.slice().sort(function (a, b) { return b.s - a.s; })[0];
  var d = S.from == null ? null : S.o - S.from, be = L.px / avg - 1;
  var sgn = function (v) { return (v > 0 ? "+" : v < 0 ? "\u2212" : "") + Math.abs(v).toFixed(1); };
  var li = mv.map(function (p) { return '<li><b class="' + (p.c > 0 ? "up" : "dn") + '">' + sgn(p.c) + '</b>' + esc(p.n) + ', now ' + p.s.toFixed(1) + '</li>'; });
  if (!li.length) li = ['<li><b>' + hi.s.toFixed(1) + '</b>Strongest: ' + esc(hi.n) + '</li>', '<li><b>' + lo.s.toFixed(1) + '</b>Weakest: ' + esc(lo.n) + '</li>'];
  return '<div class="card"><p style="margin:0 0 8px;font-weight:600">What the scorecard says</p>' +
    '<div class="sc-top"><span class="sc-o">' + S.o.toFixed(2) + '</span><span class="sc-g">' + esc(S.g) + '</span>' +
    (d === null || Math.abs(d) < 0.005 ? '' : '<span class="' + (d > 0 ? "up" : "dn") + '">' + (d > 0 ? "+" : "\u2212") + Math.abs(d).toFixed(2) +
      ' from ' + S.from.toFixed(2) + (S.gfrom && S.gfrom !== S.g ? ' (' + esc(S.gfrom) + ')' : '') + (S.since ? ' on ' + dlab(S.since) : '') + '</span>') + '</div>' +
    '<p class="note" style="margin-top:6px">' + esc(S.v) + ' ' + esc(S.why) + '</p>' +
    '<ul class="sc-p">' + li.join("") + '<li><b class="' + cls(be) + '">' + pct(be) + '</b>last close vs your $' + avg.toFixed(3) + ' breakeven</li></ul>' +
    '</div>';
}

// ---- live prices. Two feeds, the fresher wins:
//  1. a small relay on thisisfed.xyz that asks CNBC and Nasdaq server-side every 2 seconds (pre-market and after-hours
//     included; those sites refuse requests made straight from a web page);
//  2. Finnhub trades over a websocket during the regular session, plus a live pound/dollar rate when it sends one.
// Pound figures use the logged rate unless a live rate arrives.
var RELAY = "https://thisisfed.xyz/opendoor-quote.php";
var LIVE = null;
function session(ms) {
  var f = new Intl.DateTimeFormat("en-GB", {timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false});
  var p = {}; f.formatToParts(new Date(ms)).forEach(function (x) { p[x.type] = x.value; });
  var m = (+p.hour % 24) * 60 + +p.minute;
  if (p.weekday === "Sat" || p.weekday === "Sun") return "closed";
  return m >= 240 && m < 570 ? "pre" : m >= 570 && m < 960 ? "regular" : m >= 960 && m < 1200 ? "after" : "closed";
}
function live(D, L) {
  if (LIVE) return;
  LIVE = {px: null, t: null, src: "", fx: L.fx, fxLive: false, close: null, prev: null, checked: null, ws: null, tries: 0};
  var key = D.live && D.live.key, sym = D.ticker || "OPEN";
  function take(px, t, src) {
    if (!(px > 0)) return false;
    if (LIVE.t === null || t >= LIVE.t) { var moved = px !== LIVE.px; LIVE.px = px; LIVE.t = t; LIVE.src = src; return moved; }
    return false;
  }
  function ping() { var d = document.querySelector("#h-badge .lv-dot"); if (d) { d.classList.remove("ping"); void d.offsetWidth; d.classList.add("ping"); } }
  function relay() {
    if (document.hidden) return;
    fetch(RELAY, {cache: "no-store"}).then(function (r) { return r.ok ? r.json() : null; }).then(function (j) {
      if (!j || !j.px) return;
      LIVE.checked = Date.now(); LIVE.close = j.close || LIVE.close; LIVE.prev = j.prev || LIVE.prev;
      if (take(j.px, j.t, j.src)) ping();
      draw(); ago();
    }).catch(function () {});
  }
  function quote() {
    if (!key) return;
    fetch("https://finnhub.io/api/v1/quote?symbol=" + sym + "&token=" + encodeURIComponent(key)).then(function (r) { return r.json(); })
      .then(function (j) { if (j && j.c) { if (!LIVE.close) { LIVE.close = j.c; LIVE.prev = j.pc; } if (LIVE.px === null) take(j.c, j.t * 1000, "Finnhub"); draw(); ago(); } })
      .catch(function () {});
  }
  function draw() {
    if (LIVE.px === null) return;
    var s = session(Date.now());
    // reference: in the regular session the previous close; outside it the latest regular close
    var ref = s === "regular" ? (LIVE.prev || L.px) : (LIVE.close || L.px);
    STATE.q = {px: LIVE.px, fx: LIVE.fx, fxLive: LIVE.fxLive, ref: ref, sess: s};
    paint(STATE.q);
  }
  function ago() {
    if (!LIVE.t) return;
    var hm = function (ms) { return new Date(ms).toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Europe/London"}); };
    var c = LIVE.checked ? Math.max(0, Math.round((Date.now() - LIVE.checked) / 1000)) : null;
    var dd = ymd(LIVE.t) === ymd(Date.now()) ? "" : " on " + dlab(ymd(LIVE.t));
    set("h-t", "Price at " + hm(LIVE.t) + " UK" + dd + " from " + LIVE.src + (c === null ? "" : ", checked " + (c < 1 ? "just now" : c + " s ago")) +
      ". $" + LIVE.fx.toFixed(4) + " per £1" + (LIVE.fxLive ? ", live." : ", the latest daily rate."));
    var s = session(Date.now()); set("h-sess", SESS[s]); $("h-badge").className = "badge" + (s !== "closed" ? " on" : "");
  }
  function connect() {
    if (!key) return;
    var ws = new WebSocket("wss://ws.finnhub.io?token=" + encodeURIComponent(key)); LIVE.ws = ws;
    ws.onopen = function () { LIVE.tries = 0; ws.send(JSON.stringify({type: "subscribe", symbol: sym})); ws.send(JSON.stringify({type: "subscribe", symbol: "OANDA:GBP_USD"})); };
    ws.onmessage = function (ev) {
      var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type !== "trade" || !m.data) return;
      var moved = false;
      m.data.forEach(function (t) {
        if (t.s === sym) moved = take(t.p, t.t, "Finnhub") || moved;
        else if (t.s === "OANDA:GBP_USD" && t.p > 0.5 && t.p < 3) { LIVE.fx = t.p; LIVE.fxLive = true; }
      });
      if (moved) ping();
      draw(); ago();
    };
    ws.onclose = function () { var w = Math.min(30000, 1000 * Math.pow(2, LIVE.tries++)); setTimeout(connect, w); };
    ws.onerror = function () { try { ws.close(); } catch (e) {} };
  }
  relay(); quote(); connect();
  setInterval(relay, 2000); setInterval(ago, 1000); setInterval(quote, 60000);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) relay(); });
}
document.getElementById("f").addEventListener("submit", async function (ev) {
  ev.preventDefault(); var err = $("err"); err.textContent = "Unlocking…";
  try {
    var key = await derive($("pw").value), D = await open_(key);
    if ($("rem").checked) { try { localStorage.setItem("pos-key", JSON.stringify({salt: BLOB.salt, k: u8(await crypto.subtle.exportKey("raw", key))})); } catch (e) {} }
    err.textContent = ""; $("pw").value = ""; render(D, key);
  } catch (e) { err.textContent = "That passphrase did not unlock it."; }
});
document.getElementById("lock").addEventListener("click", function () { try { localStorage.removeItem("pos-key"); } catch (e) {} location.reload(); });
(async function () {
  var s = null; try { s = JSON.parse(localStorage.getItem("pos-key") || "null"); } catch (e) {}
  if (!s || s.salt !== BLOB.salt) return;
  try { var key = await crypto.subtle.importKey("raw", b64(s.k), "AES-GCM", false, ["encrypt", "decrypt"]); render(await open_(key), key); }
  catch (e) { try { localStorage.removeItem("pos-key"); } catch (x) {} }
})();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
