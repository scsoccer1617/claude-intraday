#!/usr/bin/env python3
"""
Claude's INTRADAY $100 pot (Alpaca PAPER). Runs every 5 min, 9:45 AM - 3:15 PM ET.
Strategy: opening-range breakout + VWAP trend filter, flat by 3:10 PM ET every day.
  Entry (9:45 - 2:45 ET): price > 9:30-9:45 high, price > VWAP, EMA9 > EMA21 on 5-min bars.
          Strongest (% above VWAP) first, max 2 positions x $50, one entry per symbol per day.
  Exit:   -1% stop, +2% target, close back below VWAP, or 3:10 PM ET flatten.
State is rebuilt each run from Alpaca fills tagged INTRA- (no files needed).
Env: APCA_API_KEY_ID, APCA_API_SECRET_KEY.  Flags: --dry-run, --report (summary only, no trading)
"""
import json, os, sys, time, uuid, math, datetime as dt
import urllib.request, urllib.parse, urllib.error
from zoneinfo import ZoneInfo

TRADE = "https://paper-api.alpaca.markets"   # paper only, hard-coded on purpose
DATA = "https://data.alpaca.markets"
ET = ZoneInfo("America/New_York")
TAG, START_CASH, SLOT, MAX_POS = "INTRA", 100.0, 50.0, 2
UNIVERSE = ["SPY", "QQQ", "IWM", "TQQQ", "NVDA", "TSLA", "AMD", "META"]
STOP, TARGET = 0.01, 0.02
ENTRY_START, ENTRY_END, FLATTEN = dt.time(9, 45), dt.time(14, 45), dt.time(15, 10)
DRY, REPORT = "--dry-run" in sys.argv, "--report" in sys.argv


def api(method, base, path, params=None, body=None):
    url = base + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, headers={
        "APCA-API-KEY-ID": os.environ["APCA_API_KEY_ID"],
        "APCA-API-SECRET-KEY": os.environ["APCA_API_SECRET_KEY"],
        "Content-Type": "application/json"})
    if body is not None:
        req.data = json.dumps(body).encode()
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                t = r.read()
                return json.loads(t) if t else None
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")
            if e.code in (429, 500, 502, 503) and attempt < 2:
                time.sleep(2 + attempt * 3); continue
            raise RuntimeError(f"{method} {path} -> {e.code}: {msg}")


def intra_orders():
    out, after = [], "2026-11-01T00:00:00Z"
    while True:
        r = api("GET", TRADE, "/v2/orders", {"status": "all", "limit": 500, "direction": "asc",
                                             "after": after, "nested": "false"})
        if not r: break
        out += r
        if len(r) < 500: break
        after = r[-1]["submitted_at"]
    seen, res = set(), []
    for o in out:
        if o["id"] in seen or not (o.get("client_order_id") or "").startswith(TAG + "-"): continue
        seen.add(o["id"]); res.append(o)
    return res


def ledger(orders):
    led = {"cash": START_CASH, "pos": {}, "fills": []}
    filled = [o for o in orders if float(o.get("filled_qty") or 0) > 0 and o.get("filled_at")]
    for o in sorted(filled, key=lambda o: o["filled_at"]):
        s, q, px = o["symbol"], float(o["filled_qty"]), float(o["filled_avg_price"])
        p = led["pos"].setdefault(s, {"qty": 0.0, "cost": 0.0})
        if o["side"] == "buy":
            p["qty"] += q; p["cost"] += q * px; led["cash"] -= q * px
        else:
            avg = p["cost"] / p["qty"] if p["qty"] > 0 else px
            p["qty"] -= q; p["cost"] -= q * avg; led["cash"] += q * px
            if p["qty"] <= 1e-6: p.update(qty=0.0, cost=0.0)
        led["fills"].append(o)
    led["pos"] = {s: p for s, p in led["pos"].items() if p["qty"] > 1e-6}
    return led


def today_bars(symbols, day):
    start = dt.datetime.combine(day, dt.time(9, 30), ET).astimezone(dt.timezone.utc)
    out, token = {s: [] for s in symbols}, None
    while True:
        p = {"symbols": ",".join(symbols), "timeframe": "5Min", "start": start.isoformat().replace("+00:00", "Z"),
             "limit": 10000, "feed": "iex"}
        if token: p["page_token"] = token
        r = api("GET", DATA, "/v2/stocks/bars", p)
        for s, bars in (r.get("bars") or {}).items(): out[s] += bars
        token = r.get("next_page_token")
        if not token: break
    return out


def ema(xs, n):
    if not xs: return None
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]: e = x * k + e * (1 - k)
    return e


def signals(bars, px, day):
    or_end = dt.datetime.combine(day, dt.time(9, 45), ET)
    orb = [b for b in bars if dt.datetime.fromisoformat(b["t"].replace("Z", "+00:00")) < or_end]
    vol = sum(b["v"] for b in bars)
    if not orb or not vol or len(bars) < 3: return None
    vwap = sum(b["vw"] * b["v"] for b in bars) / vol
    closes = [b["c"] for b in bars] + [px]
    return {"or_high": max(b["h"] for b in orb), "vwap": vwap, "ema9": ema(closes, 9), "ema21": ema(closes, 21)}


def order(sym, side, notional=None, qty=None):
    body = {"symbol": sym, "side": side, "type": "market", "time_in_force": "day",
            "client_order_id": f"{TAG}-{uuid.uuid4().hex[:16]}"}
    if notional is not None: body["notional"] = f"{math.floor(notional * 100) / 100:.2f}"
    else: body["qty"] = f"{qty:.9f}".rstrip("0").rstrip(".")
    if DRY: return "dry-run"
    try:
        api("POST", TRADE, "/v2/orders", body=body); return "sent"
    except RuntimeError as e:
        return f"FAILED: {e}"


def report(led, px, day):
    val = led["cash"] + sum(p["qty"] * px.get(s, p["cost"] / p["qty"]) for s, p in led["pos"].items())
    print(f"## Intraday (5-min ORB + VWAP): ${val:,.2f} ({(val / START_CASH - 1) * 100:+.2f}%)")
    for s, p in sorted(led["pos"].items()):
        v = p["qty"] * px.get(s, 0)
        print(f"- {s}: {p['qty']:.4f} sh, ${v:,.2f} ({(v / p['cost'] - 1) * 100:+.1f}% vs cost)")
    print(f"- Cash: ${led['cash']:,.2f}")
    todays = [o for o in led["fills"] if o["filled_at"][:10] == day.isoformat()]
    print("Intraday trades today: " + ("; ".join(
        f"{dt.datetime.fromisoformat(o['filled_at'].replace('Z', '+00:00')).astimezone(ET):%H:%M} "
        f"{o['side'].upper()} {o['symbol']} ${float(o['filled_qty']) * float(o['filled_avg_price']):,.2f}"
        for o in todays) or "none"))


def main():
    clock = api("GET", TRADE, "/v2/clock")
    now = dt.datetime.now(ET); day = now.date()
    orders = intra_orders(); led = ledger(orders)
    syms = sorted(set(UNIVERSE) | set(led["pos"]))
    r = api("GET", DATA, "/v2/stocks/trades/latest", {"symbols": ",".join(syms), "feed": "iex"})
    px = {s: t["p"] for s, t in (r.get("trades") or {}).items()}

    if REPORT or not clock["is_open"]:
        if not clock["is_open"] and not REPORT: print(f"{now:%H:%M} ET market closed, nothing to do.")
        report(led, px, day); return
    if now.time() < ENTRY_START:
        print(f"{now:%H:%M} ET before 9:45, waiting for opening range."); return

    pending = {o["symbol"] for o in orders if o["status"] in ("new", "accepted", "pending_new", "partially_filled")}
    bought_today = {o["symbol"] for o in led["fills"] if o["side"] == "buy" and o["filled_at"][:10] == day.isoformat()}
    bars = today_bars(syms, day)
    log = []

    # exits
    for s, p in list(led["pos"].items()):
        if s in pending or s not in px: continue
        entry, sig = p["cost"] / p["qty"], signals(bars.get(s, []), px[s], day)
        why = None
        if now.time() >= FLATTEN: why = "3:10 PM flatten"
        elif px[s] <= entry * (1 - STOP): why = "stop -1%"
        elif px[s] >= entry * (1 + TARGET): why = "target +2%"
        elif sig and px[s] < sig["vwap"]: why = "fell below VWAP"
        if why:
            res = order(s, "sell", qty=p["qty"])
            log.append(f"SELL {s} {p['qty']:.4f} sh @~{px[s]:.2f} ({px[s] / entry - 1:+.2%}) - {why} [{res}]")
            led["cash"] += p["qty"] * px[s]; del led["pos"][s]

    # entries
    if ENTRY_START <= now.time() < ENTRY_END:
        cands = []
        for s in UNIVERSE:
            if s in led["pos"] or s in pending or s in bought_today or s not in px: continue
            sig = signals(bars.get(s, []), px[s], day)
            if sig and px[s] > sig["or_high"] and px[s] > sig["vwap"] and sig["ema9"] > sig["ema21"]:
                cands.append((px[s] / sig["vwap"] - 1, s))
        open_slots = MAX_POS - len(led["pos"]) - len(pending - set(led["pos"]))
        for strength, s in sorted(cands, reverse=True)[:max(open_slots, 0)]:
            amt = min(SLOT, led["cash"] - 0.01)
            if amt < 1: break
            res = order(s, "buy", notional=amt)
            log.append(f"BUY {s} ${amt:.2f} @~{px[s]:.2f} - broke opening range, {strength:+.2%} above VWAP [{res}]")
            led["cash"] -= amt

    print(f"{now:%H:%M} ET " + ("; ".join(log) if log else "no action") + (" (DRY RUN)" if DRY else ""))


if __name__ == "__main__":
    main()
