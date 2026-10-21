import os, json, time, traceback
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import yfinance as yf
from pnsea import NSE

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
IST = ZoneInfo("Asia/Kolkata")

INDEX = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK"}
FILE = "trades_o1.json"
SNAP_FROM, SNAP_UNTIL = dtime(9, 30), dtime(11, 0)
LAST_ENTRY, EXIT_AT = dtime(14, 45), dtime(15, 15)
RATIO = 1.5          # OI dominance
REJ_BODY = 0.8       # rejection candle body
BRK_BODY = 0.6       # breakdown candle body
TOL = 0.0015         # how close to the level counts as "at the level"
MIN_RR = 1.0         # minimum distance to T1 in R
MAX_STALE_MIN = 20


def tg(msg):
    try:
        requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": msg}, timeout=20)
    except Exception as e:
        print("Telegram error:", e)


def load():
    try:
        with open(FILE) as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("open", [])
    d.setdefault("closed", [])
    d.setdefault("days", {})
    return d


def save(d):
    with open(FILE, "w") as f:
        json.dump(d, f, indent=2)


# ---------- data ----------
def candles(sym):
    df = yf.Ticker(sym).history(period="5d", interval="5m")
    df = df[["Open", "High", "Low", "Close"]].dropna()
    df.index = df.index.tz_convert(IST)
    now = datetime.now(IST)
    return df[df.index + pd.Timedelta(minutes=5) <= now]


def is_fresh(df, now):
    if len(df) == 0:
        return False
    last_close = df.index[-1] + pd.Timedelta(minutes=5)
    return last_close.date() == now.date() and \
        (now - last_close).total_seconds() / 60 <= MAX_STALE_MIN


def chain(sym):
    last = None
    for _ in range(3):
        try:
            res = NSE().options.option_chain(sym)
            return res[0], float(res[2])
        except Exception as e:
            last = e
            time.sleep(3)
    raise last


# ---------- OI snapshot ----------
def snapshot(df, spot):
    s = df.drop_duplicates("strikePrice").set_index("strikePrice").sort_index()
    strikes = list(s.index)
    atm = min(strikes, key=lambda x: abs(x - spot))
    calls = [x for x in strikes if x > atm][:5]          # nearest first
    puts = [x for x in strikes if x < atm][-5:][::-1]    # nearest first
    if len(calls) < 5 or len(puts) < 5:
        return None
    coi = [float(s.loc[x, "CE_openInterest"]) for x in calls]
    poi = [float(s.loc[x, "PE_openInterest"]) for x in puts]
    cs, ps = sum(coi), sum(poi)
    if cs <= 0 or ps <= 0:
        return None
    cavg, pavg = cs / 5, ps / 5
    heavy_put = sum(1 for v in poi if v >= RATIO * cavg)
    heavy_call = sum(1 for v in coi if v >= RATIO * pavg)

    snap = {"spot": spot, "atm": float(atm), "call_oi": cs, "put_oi": ps,
            "time": datetime.now(IST).isoformat()}
    if ps >= RATIO * cs and heavy_put >= 2:
        snap["bias"] = "BEARISH"
        snap["rej"] = float(calls[coi.index(max(coi))])   # resistance
        snap["brk"] = float(puts[0])                      # support
        rest = list(zip(puts[1:], poi[1:]))
        t1 = max(rest, key=lambda x: x[1])[0]
        lower = [x for x in rest if x[0] < t1]
        t2 = max(lower, key=lambda x: x[1])[0] if lower else None
    elif cs >= RATIO * ps and heavy_call >= 2:
        snap["bias"] = "BULLISH"
        snap["rej"] = float(puts[poi.index(max(poi))])    # support
        snap["brk"] = float(calls[0])                     # resistance
        rest = list(zip(calls[1:], coi[1:]))
        t1 = max(rest, key=lambda x: x[1])[0]
        upper = [x for x in rest if x[0] > t1]
        t2 = max(upper, key=lambda x: x[1])[0] if upper else None
    else:
        snap["bias"] = "NEUTRAL"
        return snap
    snap["t1"] = float(t1)
    snap["t2"] = float(t2) if t2 is not None else None
    return snap


# ---------- entry ----------
def body_ratio(c):
    r = c.High - c.Low
    return abs(c.Close - c.Open) / r if r > 0 else 0


def find_entry(c, snap):
    ratio, rej, brk = body_ratio(c), snap["rej"], snap["brk"]
    if snap["bias"] == "BEARISH" and c.Close < c.Open:
        if ratio >= REJ_BODY and c.High >= rej * (1 - TOL) and c.Close < rej:
            return "SELL", "Rejection at resistance", float(c.High)
        if ratio >= BRK_BODY and c.High >= brk and c.Close < brk:
            return "SELL", "Breakdown below support", float(c.High)
    if snap["bias"] == "BULLISH" and c.Close > c.Open:
        if ratio >= REJ_BODY and c.Low <= rej * (1 + TOL) and c.Close > rej:
            return "BUY", "Rejection at support", float(c.Low)
        if ratio >= BRK_BODY and c.Low <= brk and c.Close > brk:
            return "BUY", "Breakout above resistance", float(c.Low)
    return None


# ---------- trade result ----------
def resolve(t, df):
    entry, sl, t1, t2 = t["entry"], t["sl"], t["t1"], t["t2"]
    risk = abs(entry - sl)
    buy = t["side"] == "BUY"
    et = pd.Timestamp(t["time"])
    r1 = abs(t1 - entry) / risk
    r2 = abs(t2 - entry) / risk if t2 else None
    stage, locked, cur_sl = 0, 0.0, sl

    def mv(p):
        return ((p - entry) if buy else (entry - p)) / risk

    for ts, c in df[df.index >= et].iterrows():
        hit_sl = c.Low <= cur_sl if buy else c.High >= cur_sl
        hit1 = c.High >= t1 if buy else c.Low <= t1
        hit2 = bool(t2) and (c.High >= t2 if buy else c.Low <= t2)
        if stage == 0:
            if hit_sl:
                return -1.0
            if hit1:
                if t2 is None:
                    return r1
                locked, stage, cur_sl = 0.5 * r1, 1, entry
                continue
        else:
            if hit_sl:
                return locked
            if hit2:
                return locked + 0.5 * r2
        ct = ts + pd.Timedelta(minutes=5)
        if ct.date() > et.date() or ct.time() >= EXIT_AT:
            m = mv(float(c.Close))
            return m if stage == 0 else locked + 0.5 * m
    return None


def update_open(d, name, df):
    keep = []
    for t in d["open"]:
        if t["index"] != name:
            keep.append(t)
            continue
        r = resolve(t, df)
        if r is None:
            keep.append(t)
            continue
        t["r"] = round(float(r), 2)
        d["closed"].append(t)
        wins = sum(1 for x in d["closed"] if x["r"] > 0)
        tot = len(d["closed"])
        tr = sum(x["r"] for x in d["closed"])
        tg(f"[O1] {'WIN' if r > 0 else 'LOSS' if r < 0 else 'BREAK-EVEN'} "
           f"{name} {t['side']}  {r:+.2f}R\n"
           f"O1 total: {wins}/{tot} wins, {tr:+.1f}R")
    d["open"] = keep


# ---------- main ----------
now = datetime.now(IST)
if now.weekday() >= 5:
    print("Weekend")
    raise SystemExit

d = load()
today = now.strftime("%Y-%m-%d")

for name, ysym in INDEX.items():
    try:
        df = candles(ysym)
        if any(t["index"] == name for t in d["open"]) and len(df):
            update_open(d, name, df)

        if not (SNAP_FROM <= now.time() <= dtime(15, 30)):
            print(name, "outside trading window")
            continue
        if not is_fresh(df, now):
            print(name, "no fresh candles (holiday or stale)")
            continue

        key = f"{name}|{today}"
        day = d["days"].setdefault(key, {"snap": None, "skipped": False,
                                          "traded": False, "last_scan": None})

        if day["snap"] is None and not day["skipped"]:
            if now.time() > SNAP_UNTIL:
                day["skipped"] = True
                tg(f"[O1] {name}: OI snapshot missed (after 11:00), no trade today")
                continue
            tbl, spot = chain(name)
            snap = snapshot(tbl, spot)
            if snap is None:
                print(name, "bad OI data, will retry")
                continue
            day["snap"] = snap
            if snap["bias"] == "NEUTRAL":
                tg(f"[O1] {name}: OI balanced (Call {snap['call_oi']:.0f}, "
                   f"Put {snap['put_oi']:.0f}). No trade today.")
            else:
                tg(f"[O1] {name} {snap['bias']} bias\n"
                   f"Spot {snap['spot']:.2f}\n"
                   f"Call OI {snap['call_oi']:.0f} | Put OI {snap['put_oi']:.0f}\n"
                   f"Rejection level {snap['rej']:.0f}\n"
                   f"Breakout level {snap['brk']:.0f}\n"
                   f"T1 {snap['t1']:.0f} | T2 {snap['t2'] if snap['t2'] else '-'}\n"
                   f"Waiting for candle entry (paper).")

        snap = day["snap"]
        if not snap or snap["bias"] == "NEUTRAL" or day["traded"]:
            print(name, "no scan")
            continue

        start = pd.Timestamp(day["last_scan"] or snap["time"])
        for ts, c in df[df.index + pd.Timedelta(minutes=5) > start].iterrows():
            ct = ts + pd.Timedelta(minutes=5)
            if ct.date() != now.date():
                continue
            day["last_scan"] = ct.isoformat()
            if ct.time() > LAST_ENTRY:
                break
            e = find_entry(c, snap)
            if not e:
                continue
            side, why, sl = e
            entry = float(c.Close)
            risk = abs(entry - sl)
            t1, t2 = snap["t1"], snap["t2"]
            ok1 = (t1 < entry) if side == "SELL" else (t1 > entry)
            if risk <= 0 or not ok1 or abs(t1 - entry) / risk < MIN_RR:
                print(name, "setup skipped (T1 too close or passed)")
                continue
            if t2 is not None and not ((t2 < t1) if side == "SELL" else (t2 > t1)):
                t2 = None
            d["open"].append({"index": name, "side": side, "entry": entry,
                              "sl": sl, "t1": t1, "t2": t2, "why": why,
                              "time": ct.isoformat()})
            day["traded"] = True
            tg(f"[O1] {side} {name} (paper)\n{why}\n"
               f"Entry {entry:.2f}\nStop-loss {sl:.2f}\n"
               f"T1 {t1:.0f} (book half, SL to entry)\n"
               f"T2 {t2 if t2 else '-'}\n"
               f"Time exit 3:15 PM\n(Index points, not option premium)")
            update_open(d, name, df)
            break
    except Exception:
        traceback.print_exc()

save(d)
