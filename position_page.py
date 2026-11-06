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
    data = {"entries": entries[-400:], "updated": sc.get("generated"), "ticker": "OPEN"}
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
--good:#047857;--good-bg:#e3f3ea;--bad:#b4302b;--bad-bg:#fbe9e7;--blue:#1d4ed8;--accent:#1d4ed8;--track:#f1efec;color-scheme:light}
:root[data-theme=dark]{--bg:#141312;--card:#1e1c1a;--soft:#272421;--line:#36312d;--line2:#4a443e;--ink:#f2ede7;--muted:#ada296;--muted2:#cbc2b8;
--good:#5fd49b;--good-bg:#173126;--bad:#ff8e82;--bad-bg:#3a201d;--blue:#7ea2ff;--accent:#3563e9;--track:#322e2a;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:400 15px/1.5 Inter,system-ui,sans-serif;
letter-spacing:-.011em;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:32px 18px 80px}
h1{font-size:28px;font-weight:600;letter-spacing:-.03em;margin:0}.sub{color:var(--muted);margin:4px 0 0}
.card{background:var(--card);border-radius:20px;padding:22px 24px;margin-top:18px;box-shadow:0 1px 2px rgba(0,0,0,.06)}
:root[data-theme=dark] .card{box-shadow:0 0 0 1px var(--line)}
form{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
input[type=password]{flex:1 1 240px;min-height:48px;font:inherit;font-size:16px;padding:0 14px;border:1px solid var(--line2);border-radius:12px;background:var(--card);color:var(--ink)}
button{min-height:48px;font:inherit;font-size:16px;font-weight:600;padding:0 20px;border:0;border-radius:12px;background:var(--accent);color:#fff;cursor:pointer}
button.ghost{background:none;color:var(--ink);border:1px solid var(--line2);font-weight:500;min-height:40px;font-size:14px}
label.rem{display:flex;align-items:center;gap:8px;min-height:44px;font-size:14px;color:var(--muted2);flex-basis:100%}
.err{color:var(--bad);margin:10px 0 0;min-height:1.5em}
.big{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
.k{background:var(--soft);border-radius:14px;padding:14px 16px}.k p{margin:0}
.k .l{font-size:13px;color:var(--muted)}.k .v{font-size:30px;font-weight:600;letter-spacing:-.03em;line-height:1.15;margin-top:2px}
.k .s{font-size:13px;color:var(--muted2);margin-top:2px}
.up{color:var(--good)}.dn{color:var(--bad)}.k.up{background:var(--good-bg)}.k.dn{background:var(--bad-bg)}
.facts{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin-top:16px}
.facts div{border-top:1px solid var(--line);padding-top:8px}.facts b{display:block;font-size:17px;font-weight:600}.facts span{font-size:12.5px;color:var(--muted)}
table{width:100%;border-collapse:collapse;font-size:14px}th{font-size:12px;font-weight:500;color:var(--muted);text-align:right;padding:8px 10px;border-bottom:1px solid var(--line)}
td{text-align:right;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th:first-child,td:first-child{text-align:left}
tbody[hidden]{display:none!important}.more-days{margin-top:12px}
.scroll{overflow-x:auto}.note{font-size:13px;color:var(--muted);margin:12px 0 0}
svg text{font-size:12px;fill:var(--muted)}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}
.live .lv-top{display:flex;justify-content:space-between;align-items:baseline;gap:8px 16px;flex-wrap:wrap;margin-bottom:12px}
.lv-h{margin:0;font-weight:600;display:flex;align-items:center;gap:8px}.lv-h span#lv-sess{font-weight:400;color:var(--muted2)}
.lv-t{margin:0;font-size:13px;color:var(--muted)}
.lv-dot{width:9px;height:9px;border-radius:50%;background:#2fb67c;box-shadow:0 0 0 0 rgba(47,182,124,.5);animation:pulse 2s ease-out infinite}
.live.off .lv-dot{background:var(--muted);animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(47,182,124,.5)}70%{box-shadow:0 0 0 9px rgba(47,182,124,0)}100%{box-shadow:0 0 0 0 rgba(47,182,124,0)}}
@keyframes ping{0%{box-shadow:0 0 0 0 rgba(47,182,124,.9),0 0 10px 2px rgba(47,182,124,.9)}100%{box-shadow:0 0 0 12px rgba(47,182,124,0),0 0 0 0 rgba(47,182,124,0)}}
.lv-dot.ping{animation:ping .9s ease-out}
@media (prefers-reduced-motion:reduce){.lv-dot,.lv-dot.ping{animation:none}}
@media (max-width:640px){.big{grid-template-columns:1fr}.facts{grid-template-columns:1fr 1fr}.k .v{font-size:28px}.card{padding:18px 16px}
table.days thead{display:none}table.days,table.days tbody{display:block}table.days tr{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 8px;padding:10px 0;border-bottom:1px solid var(--line)}
table.days td{border:0;padding:0;text-align:left}table.days td:first-child{grid-column:1/-1;font-weight:600}table.days td::before{content:attr(data-l);display:block;font-size:11px;color:var(--muted)}}
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
    {name: "AES-GCM", length: 256}, true, ["decrypt"]);
}
async function open_(key) {
  var pt = await crypto.subtle.decrypt({name: "AES-GCM", iv: b64(BLOB.iv)}, key, b64(BLOB.ct));
  return JSON.parse(new TextDecoder().decode(pt));
}
var gbp = function (v, sign) { var s = Math.abs(v).toLocaleString("en-GB", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  return (v < 0 ? "−" : sign && v > 0 ? "+" : "") + "£" + s; };
var pct = function (v) { return (v < 0 ? "−" : "+") + Math.abs(v * 100).toFixed(2) + "%"; };
var cls = function (v) { return v > 0.004 ? "up" : v < -0.004 ? "dn" : ""; };
var dlab = function (d) { var p = String(d).slice(0, 10).split("-"); return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : String(d); };
function chart(E) {
  if (E.length < 2) return '<p class="note">The chart starts once there are two days in the log.</p>';
  var W = Math.round(Math.min(840, Math.max(300, window.innerWidth - 72))), H = W < 520 ? 200 : 220, L = 56, R = 12, T = 12, B = 26, vs = [];
  E.forEach(function (e) { vs.push(e.val, e.cost); });
  var lo = Math.min.apply(null, vs), hi = Math.max.apply(null, vs), pad = (hi - lo) * 0.1 || hi * 0.05 || 1; lo -= pad; hi += pad;
  var x = function (i) { return L + i * (W - L - R) / (E.length - 1); }, y = function (v) { return T + (1 - (v - lo) / (hi - lo)) * (H - T - B); };
  var line = function (k) { return E.map(function (e, i) { return x(i).toFixed(1) + "," + y(e[k]).toFixed(1); }).join(" "); };
  var g = "";
  [lo + pad, (lo + hi) / 2, hi - pad].forEach(function (v) { g += '<line x1="' + L + '" x2="' + (W - R) + '" y1="' + y(v) + '" y2="' + y(v) + '" stroke="var(--line)"/>' +
    '<text x="' + (L - 8) + '" y="' + (y(v) + 4) + '" text-anchor="end">£' + Math.round(v).toLocaleString("en-GB") + '</text>'; });
  return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%" role="img" aria-label="Value and money invested, by day">' + g +
    '<polyline points="' + line("cost") + '" fill="none" stroke="var(--muted)" stroke-dasharray="4 4" stroke-width="1.5"/>' +
    '<polyline points="' + line("val") + '" fill="none" stroke="var(--blue)" stroke-width="2.4" stroke-linejoin="round"/>' +
    '<text x="' + L + '" y="' + (H - 6) + '">' + dlab(E[0].d) + '</text><text x="' + (W - R) + '" y="' + (H - 6) + '" text-anchor="end">' + dlab(E[E.length - 1].d) + '</text></svg>' +
    '<p class="note"><span style="color:var(--blue)">—</span> value &nbsp; <span>- -</span> money put in, at each day\'s rate</p>';
}
function render(D) {
  var E = D.entries, L = E[E.length - 1], P = E.length > 1 ? E[E.length - 2] : null;
  var usd = function (v) { return "$" + Math.abs(v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}); };
  var avg = L.cost_usd / L.sh;
  var day = L.day === null ? null : L.day, dayp = day === null || !P ? null : day / P.val;
  var h = (true ? '<div class="card live" id="live"><div class="lv-top"><p class="lv-h"><span class="lv-dot"></span>Live <span id="lv-sess">connecting</span></p>' +
      '<p class="lv-t" id="lv-t"></p></div><div class="big">' +
      '<div class="k"><p class="l">Price now</p><p class="v" id="lv-px">\u2013</p><p class="s" id="lv-pxs"></p></div>' +
      '<div class="k" id="lv-k1"><p class="l">Since the reference close</p><p class="v" id="lv-day">\u2013</p><p class="s" id="lv-days"></p></div>' +
      '<div class="k" id="lv-k2"><p class="l">Total, live</p><p class="v" id="lv-tot">\u2013</p><p class="s" id="lv-tots"></p></div></div></div>' : '') +
    '<div class="card"><div class="big">' +
    '<div class="k"><p class="l">Value now</p><p class="v">' + gbp(L.val) + '</p><p class="s">close ' + dlab(L.d) + '</p></div>' +
    '<div class="k ' + cls(L.tot) + '"><p class="l">Total gain or loss</p><p class="v">' + gbp(L.tot, true) + '</p><p class="s">' + pct(L.tot_usd / L.cost_usd) + ' on ' + usd(L.cost_usd) + ' put in (' + gbp(L.cost) + ' today)</p></div>' +
    '<div class="k ' + (day === null ? "" : cls(day)) + '"><p class="l">Day</p><p class="v">' + (day === null ? "n/a" : gbp(day, true)) + '</p><p class="s">' +
      (day === null ? "from the next close" : pct(dayp) + (L.dayfx !== null && Math.abs(L.dayfx) >= 0.005 ? ", of which the pound/dollar rate " + gbp(L.dayfx, true) : "")) + '</p></div></div>' +
    '<div class="facts"><div><b>' + L.sh.toLocaleString("en-GB", {maximumFractionDigits: 4}) + '</b><span>shares</span></div>' +
    '<div><b>$' + avg.toFixed(3) + '</b><span>average cost a share, your breakeven</span></div>' +
    '<div><b>$' + L.px.toFixed(2) + '</b><span>last close</span></div>' +
    '<div><b>$' + L.fx.toFixed(4) + '</b><span>per £1, ' + dlab(L.fxd) + '</span></div></div></div>' +
    '<div class="card"><p class="l" style="margin:0 0 8px;font-weight:600">Value and money put in</p>' + chart(E) + '</div>';
  var rows = E.slice().reverse().map(function (e) {
    return '<tr><td>' + dlab(e.d) + (e.seed || e.bf ? " *" : "") + '</td><td data-l="Close">$' + e.px.toFixed(2) + '</td><td data-l="$ per £">' + e.fx.toFixed(4) + '</td>' +
      '<td data-l="Value">' + gbp(e.val) + '</td><td data-l="Day" class="' + (e.day === null ? "" : cls(e.day)) + '">' + (e.day === null ? "–" : gbp(e.day, true)) + '</td>' +
      '<td data-l="Total" class="' + cls(e.tot) + '">' + gbp(e.tot, true) + '</td></tr>'; });
  var older = rows.length - 7;   // the last 7 closes show; earlier days sit behind a toggle
  h += '<div class="card"><p style="margin:0 0 8px;font-weight:600">Day by day</p><div class="scroll"><table class="days"><thead><tr><th>Close</th><th>Price</th><th>$ per £</th><th>Value</th><th>Day</th><th>Total</th></tr></thead><tbody>' + rows.slice(0, 7).join("") + '</tbody>' +
    (older > 0 ? '<tbody id="older" hidden>' + rows.slice(7).join("") + '</tbody>' : '') + '</table></div>' +
    (older > 0 ? '<button type="button" class="ghost more-days" id="more-days" aria-expanded="false" aria-controls="older" data-n="' + older + '">Show ' + older + ' earlier day' + (older === 1 ? '' : 's') + '</button>' : '') +
    '<p class="note">Values use each close and the dollars-per-pound rate for that day or the latest before it, from ' + (L.src || 'the Federal Reserve (FRED DEXUSUK)') + '. ' +
    'Your cost is in dollars; the pound figures convert the dollar value and gain at each day\'s rate, so the total moves with the pound as well as the share price. ' +
    'The day\'s figure is the change in the pound total, so money you add never counts as a gain. ' + (E.some(function (e) { return e.seed || e.bf; }) ? "* Days before the daily log began are worked out from that day\'s close and rate, assuming today\'s holding throughout; the first is the gain since buying. " : "") +
    'Updated ' + (D.updated ? dlab(D.updated) + " " + D.updated.slice(11, 16) : "") + ' UK. Not advice.</p></div>';
  document.getElementById("out").innerHTML = h; document.getElementById("out").hidden = false;
  var md = document.getElementById("more-days");
  if (md) md.addEventListener("click", function () {
    var t = document.getElementById("older"), open = t.hidden, n = +md.dataset.n;
    t.hidden = !open; md.setAttribute("aria-expanded", open ? "true" : "false");
    md.textContent = open ? "Hide earlier days" : "Show " + n + " earlier day" + (n === 1 ? "" : "s");
  });
  live(D, L);
  document.getElementById("gate").hidden = true; document.getElementById("lock").hidden = false;
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
var SESS = {pre: "pre-market", regular: "market open", after: "after-hours", closed: "market closed"};
function live(D, L) {
  if (LIVE) return;
  LIVE = {px: null, t: null, src: "", fx: L.fx, fxLive: false, close: null, prev: null, checked: null, ws: null, tries: 0};
  var key = D.live && D.live.key, sym = D.ticker || "OPEN";
  function take(px, t, src) {
    if (!(px > 0)) return false;
    if (LIVE.t === null || t >= LIVE.t) { var moved = px !== LIVE.px; LIVE.px = px; LIVE.t = t; LIVE.src = src; return moved; }
    return false;
  }
  function ping() { var d = document.querySelector("#live .lv-dot"); if (d) { d.classList.remove("ping"); void d.offsetWidth; d.classList.add("ping"); } }
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
      .then(function (j) { if (j && j.c) { if (!LIVE.close) { LIVE.close = j.c; LIVE.prev = j.pc; } if (LIVE.px === null) take(j.c, j.t * 1000, "Finnhub"); draw(); } })
      .catch(function () {});
  }
  function draw() {
    if (LIVE.px === null) return;
    var s = session(Date.now()), fx = LIVE.fx, sh = L.sh;
    // reference: in the regular session the previous close; outside it the latest regular close
    var ref = s === "regular" ? (LIVE.prev || L.px) : (LIVE.close || L.px);
    var refLab = s === "regular" ? "previous close" : "last regular close";
    var chg = LIVE.px - ref, dayG = sh * chg / fx, totG = (sh * LIVE.px - L.cost_usd) / fx;
    document.getElementById("lv-sess").textContent = SESS[s];
    document.getElementById("live").classList.toggle("off", s === "closed");
    document.getElementById("lv-px").textContent = "$" + LIVE.px.toFixed(LIVE.px < 10 ? 4 : 2);
    document.getElementById("lv-pxs").textContent = (chg >= 0 ? "+" : "−") + "$" + Math.abs(chg).toFixed(4) + " (" + pct(chg / ref) + ") vs the " + refLab + " $" + ref.toFixed(2);
    var k1 = document.getElementById("lv-k1"), k2 = document.getElementById("lv-k2");
    k1.querySelector(".l").textContent = "Since the " + refLab;
    document.getElementById("lv-day").textContent = gbp(dayG, true); k1.className = "k " + cls(dayG);
    document.getElementById("lv-days").textContent = pct(chg / ref) + " on " + sh.toLocaleString("en-GB", {maximumFractionDigits: 4}) + " shares";
    document.getElementById("lv-tot").textContent = gbp(totG, true); k2.className = "k " + cls(totG);
    document.getElementById("lv-tots").textContent = "value " + gbp(sh * LIVE.px / fx) + ", $" + fx.toFixed(4) + " per £1" + (LIVE.fxLive ? " live" : " (" + dlab(L.fxd) + ")");
  }
  function ago() {
    var el = document.getElementById("lv-t"); if (!el || !LIVE.t) return;
    var hm = function (ms) { return new Date(ms).toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", second: "2-digit"}); };
    var c = LIVE.checked ? Math.max(0, Math.round((Date.now() - LIVE.checked) / 1000)) : null;
    el.textContent = "price at " + hm(LIVE.t) + " UK from " + LIVE.src + (c === null ? "" : ", checked " + (c < 1 ? "just now" : c + " s ago"));
    var sEl = document.getElementById("lv-sess"); if (sEl) sEl.textContent = SESS[session(Date.now())];
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
  ev.preventDefault(); var err = document.getElementById("err"); err.textContent = "Unlocking…";
  try {
    var key = await derive(document.getElementById("pw").value), D = await open_(key);
    if (document.getElementById("rem").checked) { try { localStorage.setItem("pos-key", JSON.stringify({salt: BLOB.salt, k: u8(await crypto.subtle.exportKey("raw", key))})); } catch (e) {} }
    err.textContent = ""; document.getElementById("pw").value = ""; render(D);
  } catch (e) { err.textContent = "That passphrase did not unlock it."; }
});
document.getElementById("lock").addEventListener("click", function () { try { localStorage.removeItem("pos-key"); } catch (e) {} location.reload(); });
(async function () {
  var s = null; try { s = JSON.parse(localStorage.getItem("pos-key") || "null"); } catch (e) {}
  if (!s || s.salt !== BLOB.salt) return;
  try { var key = await crypto.subtle.importKey("raw", b64(s.k), "AES-GCM", false, ["decrypt"]); render(await open_(key)); }
  catch (e) { try { localStorage.removeItem("pos-key"); } catch (x) {} }
})();
</script></body></html>
"""

if __name__ == "__main__":
    sys.exit(main())
