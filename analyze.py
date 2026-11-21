"""
Nifty 50 end-of-day analyzer.

Each run:
  1. Downloads daily Nifty 50 and India VIX prices (Yahoo Finance via yfinance)
  2. Downloads the Nifty option chain from NSE (open interest, PCR, max pain)
  3. Computes indicators, key levels and a rule-based CE / PE / no-trade signal
  4. Logs today's signal to track.json and grades earlier signals against
     what the market actually did the next day
  5. Writes everything the web page needs to data.json

Usage:
    python analyze.py                  # live data
    python analyze.py --demo           # synthetic data, for testing offline
    python analyze.py --demo --cut 3   # demo data minus the last 3 days
"""
import json
import os
import sys
import datetime as dt

import numpy as np
import pandas as pd

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
STRIKE_STEP = 50          # Nifty strikes are 50 points apart
CHAIN_STRIKES_EACH_SIDE = 15
BACKFILL_DAYS = 60        # first run: grade the rule over this many past days
TRACK_FILE = "track.json"
CHAIN_CACHE = "oc_cache.json"
NSE = "https://www.nseindia.com"
NSE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/option-chain",
}


# =====================================================================
# Price data
# =====================================================================
def _yf(symbol, period="2y"):
    import yfinance as yf
    df = yf.download(symbol, period=period, interval="1d",
                     auto_adjust=False, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def load_live():
    nifty = _yf("^NSEI")[["Open", "High", "Low", "Close", "Volume"]].dropna()
    try:
        vix = _yf("^INDIAVIX")["Close"].dropna()
    except Exception as e:
        print("VIX download failed:", e)
        vix = pd.Series(dtype=float)
    return nifty, vix


def load_demo(n=500):
    rng = np.random.default_rng(7)
    idx = pd.bdate_range(end=dt.date.today(), periods=n)
    close = 22000 * np.exp(np.cumsum(rng.normal(0.0004, 0.009, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n)))
    df = pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                       "Volume": rng.integers(2e5, 6e5, n)}, index=idx)
    vix = pd.Series(np.clip(14 + np.cumsum(rng.normal(0, 0.4, n)), 9, 35), index=idx)
    return df, vix


# =====================================================================
# Option chain (NSE)
# =====================================================================
def fetch_option_chain(as_of):
    """Return the raw option-chain rows for the nearest expiry after as_of."""
    import requests
    s = requests.Session()
    s.headers.update(NSE_HEADERS)
    s.get(NSE + "/option-chain", timeout=20)  # sets the cookies NSE requires

    def pick_expiry(dates):
        parsed = sorted(dt.datetime.strptime(d, "%d-%b-%Y").date() for d in dates)
        future = [d for d in parsed if d > as_of] or parsed
        return future[0].strftime("%d-%b-%Y")

    try:  # current API
        info = s.get(NSE + "/api/option-chain-contract-info",
                     params={"symbol": "NIFTY"}, timeout=20).json()
        expiry = pick_expiry(info["expiryDates"])
        raw = s.get(NSE + "/api/option-chain-v3",
                    params={"type": "Indices", "symbol": "NIFTY", "expiry": expiry},
                    timeout=20).json()
    except Exception:  # older API, kept as a fallback
        raw = s.get(NSE + "/api/option-chain-indices",
                    params={"symbol": "NIFTY"}, timeout=20).json()
        expiry = pick_expiry(raw["records"]["expiryDates"])

    rec = raw.get("records", raw)
    rows = [r for r in rec["data"] if r.get("expiryDate", expiry) == expiry]
    if not rows:
        raise ValueError("Option chain came back empty")
    spot = rec.get("underlyingValue") or next(
        (r[k]["underlyingValue"] for r in rows for k in ("CE", "PE") if k in r), None)
    return expiry, spot, rows


def demo_option_chain(spot):
    rng = np.random.default_rng(3)
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    rows = []
    for k in range(atm - 40 * STRIKE_STEP, atm + 41 * STRIKE_STEP, STRIKE_STEP):
        d = (k - atm) / STRIKE_STEP
        ce = int(max(0, 9e4 * np.exp(-((d - 4) / 8) ** 2) * rng.uniform(.6, 1.4)))
        pe = int(max(0, 9e4 * np.exp(-((d + 4) / 8) ** 2) * rng.uniform(.6, 1.4)))
        rows.append({"strikePrice": k,
                     "CE": {"openInterest": ce, "changeinOpenInterest": int(ce * rng.normal(0, .2))},
                     "PE": {"openInterest": pe, "changeinOpenInterest": int(pe * rng.normal(0, .2))}})
    exp = (dt.date.today() + dt.timedelta(days=(1 - dt.date.today().weekday()) % 7 or 7))
    return exp.strftime("%d-%b-%Y"), spot, rows


def summarize_chain(expiry, spot, rows):
    df = pd.DataFrame([{
        "strike": r["strikePrice"],
        "ce_oi": (r.get("CE") or {}).get("openInterest", 0) or 0,
        "pe_oi": (r.get("PE") or {}).get("openInterest", 0) or 0,
        "ce_chg": (r.get("CE") or {}).get("changeinOpenInterest", 0) or 0,
        "pe_chg": (r.get("PE") or {}).get("changeinOpenInterest", 0) or 0,
    } for r in rows]).groupby("strike").sum().sort_index()

    k = df.index.values
    pain = [(df.ce_oi * np.maximum(0, x - k)).sum() + (df.pe_oi * np.maximum(0, k - x)).sum() for x in k]
    atm = round(spot / STRIKE_STEP) * STRIKE_STEP
    win = df[(df.index >= atm - CHAIN_STRIKES_EACH_SIDE * STRIKE_STEP) &
             (df.index <= atm + CHAIN_STRIKES_EACH_SIDE * STRIKE_STEP)]
    return {
        "expiry": expiry,
        "spot": round(float(spot), 2),
        "pcr": round(float(df.pe_oi.sum() / max(df.ce_oi.sum(), 1)), 2),
        "pcr_chg_oi": round(float(df.pe_chg.sum() / df.ce_chg.sum()), 2) if df.ce_chg.sum() > 0 else None,
        "max_pain": int(k[int(np.argmin(pain))]),
        "max_ce_oi_strike": int(df.ce_oi.idxmax()),   # heaviest call writing = resistance
        "max_pe_oi_strike": int(df.pe_oi.idxmax()),   # heaviest put writing = support
        "strikes": [int(x) for x in win.index],
        "ce_oi": [int(x) for x in win.ce_oi], "pe_oi": [int(x) for x in win.pe_oi],
        "ce_chg": [int(x) for x in win.ce_chg], "pe_chg": [int(x) for x in win.pe_chg],
        "fetched_at": dt.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
        "stale": False,
    }


def get_chain(demo, spot, as_of):
    if demo:
        return summarize_chain(*demo_option_chain(spot))
    try:
        oc = summarize_chain(*fetch_option_chain(as_of))
        with open(CHAIN_CACHE, "w") as f:
            json.dump(oc, f)
        return oc
    except Exception as e:
        print("Option chain fetch failed:", e)
        if os.path.exists(CHAIN_CACHE):  # show the last good snapshot, flagged as old
            with open(CHAIN_CACHE) as f:
                oc = json.load(f)
            oc["stale"] = True
            return oc
        return None


# =====================================================================
# Indicators and signal
# =====================================================================
def add_indicators(df):
    c = df["Close"]
    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    df["SMA200"] = c.rolling(200).mean()
    d = c.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    df["RSI"] = 100 - 100 / (1 + gain / loss)
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    df["MACD"] = ema12 - ema26
    df["MACD_SIG"] = df["MACD"].ewm(span=9, adjust=False).mean()
    df["MACD_HIST"] = df["MACD"] - df["MACD_SIG"]
    sd = c.rolling(20).std()
    df["BB_UP"], df["BB_LO"] = df["SMA20"] + 2 * sd, df["SMA20"] - 2 * sd
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - c.shift()).abs(),
                    (df["Low"] - c.shift()).abs()], axis=1).max(axis=1)
    df["ATR"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def score_row(r, prev_hist, vix=None, vix_prev=None, oc=None):
    """Score > 0 bullish, < 0 bearish. Returns (score, reasons)."""
    s, why = 0.0, []

    def add(pts, text):
        nonlocal s
        s += pts
        why.append(text)

    add(1 if r.Close > r.SMA50 else -1,
        "Close above 50-day average (uptrend)" if r.Close > r.SMA50 else "Close below 50-day average (downtrend)")
    add(1 if r.SMA20 > r.SMA50 else -1,
        "20-DMA above 50-DMA" if r.SMA20 > r.SMA50 else "20-DMA below 50-DMA")
    add(1 if r.MACD_HIST > 0 else -1,
        "MACD above signal line" if r.MACD_HIST > 0 else "MACD below signal line")
    add(.5 if r.MACD_HIST > prev_hist else -.5,
        "MACD histogram rising" if r.MACD_HIST > prev_hist else "MACD histogram falling")
    if r.RSI > 60:
        add(1, f"RSI {r.RSI:.0f}: bullish zone")
    elif r.RSI < 40:
        add(-1, f"RSI {r.RSI:.0f}: bearish zone")
    else:
        why.append(f"RSI {r.RSI:.0f}: neutral")
    if r.RSI > 70:
        why.append("⚠ RSI overbought: pullback risk")
    if r.RSI < 30:
        why.append("⚠ RSI oversold: bounce risk")

    # Option chain: high PCR = heavy put writing (support), low PCR = heavy call writing
    if oc and not oc.get("stale"):
        p = oc["pcr"]
        if p >= 1.3:
            add(.5, f"PCR {p}: strong put writing (supportive)")
        elif p <= 0.7:
            add(-.5, f"PCR {p}: strong call writing (resistive)")
        else:
            why.append(f"PCR {p}: balanced")
        if r.Close >= oc["max_ce_oi_strike"]:
            why.append(f"Price at/above biggest call-OI strike {oc['max_ce_oi_strike']}")
        if r.Close <= oc["max_pe_oi_strike"]:
            why.append(f"Price at/below biggest put-OI strike {oc['max_pe_oi_strike']}")

    # VIX: does not give direction, but changes how risky option buying is
    if vix is not None:
        if vix_prev and vix > vix_prev * 1.08:
            add(-.5, f"India VIX jumped to {vix:.1f}: fear rising")
        elif vix_prev and vix < vix_prev * 0.92:
            add(.5, f"India VIX fell to {vix:.1f}: fear easing")
        if vix >= 20:
            why.append(f"⚠ VIX {vix:.1f} is high: premiums expensive, use smaller size")
        elif vix <= 12:
            why.append(f"VIX {vix:.1f} is low: premiums cheap, but moves may be small")
    return s, why


def bias_from(score):
    if score >= 2.5:
        return "CALL"
    if score <= -2.5:
        return "PUT"
    return "NONE"


BIAS_LABEL = {"CALL": "CALL (CE) bias", "PUT": "PUT (PE) bias", "NONE": "NO TRADE / neutral"}


def pivots(h, l, c):
    p = (h + l + c) / 3
    return {"R2": p + (h - l), "R1": 2 * p - l, "Pivot": p, "S1": 2 * p - h, "S2": p - (h - l)}


def make_plan(bias, lv, close):
    atm = int(round(close / STRIKE_STEP) * STRIKE_STEP)
    if bias == "CALL":
        return {"instrument": f"NIFTY {atm} CE (ATM)", "trigger": f"Only if Nifty trades above R1 {lv['R1']:.0f}",
                "target": f"R2 {lv['R2']:.0f}", "stop": f"Exit if Nifty falls below Pivot {lv['Pivot']:.0f}"}
    if bias == "PUT":
        return {"instrument": f"NIFTY {atm} PE (ATM)", "trigger": f"Only if Nifty trades below S1 {lv['S1']:.0f}",
                "target": f"S2 {lv['S2']:.0f}", "stop": f"Exit if Nifty rises above Pivot {lv['Pivot']:.0f}"}
    return {"instrument": "None", "trigger": f"Wait for a break above R1 {lv['R1']:.0f} or below S1 {lv['S1']:.0f}",
            "target": "-", "stop": "-"}


def backtest(df):
    hp = df["MACD_HIST"].shift()
    scores = [score_row(r, h)[0] for r, h in zip(df.itertuples(), hp)]
    df = df.assign(score=scores)
    df["ret1"] = df["Close"].shift(-1) / df["Close"] - 1
    df["ret5"] = df["Close"].shift(-5) / df["Close"] - 1
    out = {}
    for name, mask, sign in [("call", df.score >= 2.5, 1), ("put", df.score <= -2.5, -1)]:
        sub = df[mask].dropna(subset=["ret5"])
        n = len(sub)
        out[name] = {"signals": n,
                     "hit_1d": round(float((np.sign(sub.ret1) == sign).mean() * 100), 1) if n else None,
                     "hit_5d": round(float((np.sign(sub.ret5) == sign).mean() * 100), 1) if n else None}
    out["base_up_days_pct"] = round(float((df.ret1.dropna() > 0).mean() * 100), 1)
    out["note"] = "Technical rules only (no historical option-chain data)"
    return out, df["score"]


# =====================================================================
# Signal tracking
# =====================================================================
def make_entry(date, row, prev_hist, vix=None, vix_prev=None, oc=None, backfilled=False):
    score, _ = score_row(row, prev_hist, vix, vix_prev, oc)
    bias = bias_from(score)
    lv = {k: round(float(v), 1) for k, v in pivots(row.High, row.Low, row.Close).items()}
    return {"date": date, "bias": bias, "score": score, "close": round(float(row.Close), 2),
            "levels": lv, "vix": None if vix is None else round(float(vix), 2),
            "pcr": oc["pcr"] if oc and not oc.get("stale") else None,
            "backfilled": backfilled, "outcome": None}


def grade(entry, nxt):
    """Grade a signal against the next session's OHLC (a 1-day plan).
    Daily bars don't show which level was touched first, so if the stop and
    target were both hit we count it as a loss (the cautious assumption)."""
    lv, b = entry["levels"], entry["bias"]
    o = {"next_date": nxt["date"], "next_close": round(nxt["Close"], 2),
         "move": round(nxt["Close"] - entry["close"], 2)}
    if b == "CALL":
        entry_px, tgt, stop = lv["R1"], lv["R2"], lv["Pivot"]
        if nxt["High"] < entry_px:
            o.update(result="Not triggered", points=0.0)
        elif nxt["Low"] <= stop:
            o.update(result="Stopped out", points=round(stop - entry_px, 1))
        elif nxt["High"] >= tgt:
            o.update(result="Target hit", points=round(tgt - entry_px, 1))
        else:
            o.update(result="Closed at day end", points=round(nxt["Close"] - entry_px, 1))
        o["direction_ok"] = nxt["Close"] > entry["close"]
    elif b == "PUT":
        entry_px, tgt, stop = lv["S1"], lv["S2"], lv["Pivot"]
        if nxt["Low"] > entry_px:
            o.update(result="Not triggered", points=0.0)
        elif nxt["High"] >= stop:
            o.update(result="Stopped out", points=round(entry_px - stop, 1))
        elif nxt["Low"] <= tgt:
            o.update(result="Target hit", points=round(entry_px - tgt, 1))
        else:
            o.update(result="Closed at day end", points=round(entry_px - nxt["Close"], 1))
        o["direction_ok"] = nxt["Close"] < entry["close"]
    else:
        up, dn = nxt["High"] > lv["R1"], nxt["Low"] < lv["S1"]
        o["result"] = ("Broke both sides" if up and dn else "Broke above R1" if up
                       else "Broke below S1" if dn else "Stayed in range")
        o["points"], o["direction_ok"] = None, (not up and not dn)
    return o


def summarize_track(entries):
    def stats(es):
        trades = [e for e in es if e["outcome"] and e["bias"] != "NONE"]
        trig = [e for e in trades if e["outcome"]["result"] != "Not triggered"]
        wins = [e for e in trig if e["outcome"]["points"] > 0]
        nt = [e for e in es if e["outcome"] and e["bias"] == "NONE"]
        return {"signals": len(trades), "triggered": len(trig),
                "win_rate": round(len(wins) / len(trig) * 100, 1) if trig else None,
                "direction_ok": round(sum(e["outcome"]["direction_ok"] for e in trades) / len(trades) * 100, 1) if trades else None,
                "total_points": round(sum(e["outcome"]["points"] for e in trig), 1),
                "no_trade_days": len(nt),
                "no_trade_correct": round(sum(e["outcome"]["direction_ok"] for e in nt) / len(nt) * 100, 1) if nt else None}
    return {"all": stats(entries), "live": stats([e for e in entries if not e["backfilled"]])}


def update_track(df, vix_s, today_entry):
    track = []
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE) as f:
            track = json.load(f).get("entries", [])

    if not track:  # first run: backfill recent history so the table isn't empty
        hp = df["MACD_HIST"].shift()
        for i in range(max(1, len(df) - 1 - BACKFILL_DAYS), len(df) - 1):
            d = df.index[i]
            v = vix_s.get(d) if len(vix_s) else None
            vp = vix_s.get(df.index[i - 1]) if len(vix_s) else None
            track.append(make_entry(d.strftime("%Y-%m-%d"), df.iloc[i], hp.iloc[i],
                                    v, vp, None, backfilled=True))

    track = [e for e in track if e["date"] != today_entry["date"]] + [today_entry]
    track.sort(key=lambda e: e["date"])

    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    pos = {d: i for i, d in enumerate(dates)}
    for e in track:
        if e["outcome"] is None and e["date"] in pos and pos[e["date"]] + 1 < len(df):
            j = pos[e["date"]] + 1
            r = df.iloc[j]
            e["outcome"] = grade(e, {"date": dates[j], "High": float(r.High),
                                     "Low": float(r.Low), "Close": float(r.Close)})
    track = track[-500:]
    with open(TRACK_FILE, "w") as f:
        json.dump({"entries": track}, f, indent=1)
    return track


# =====================================================================
# Main
# =====================================================================
def main():
    demo = "--demo" in sys.argv
    df, vix_s = load_demo() if demo else load_live()
    if "--cut" in sys.argv:
        n = int(sys.argv[sys.argv.index("--cut") + 1])
        if n > 0:
            df, vix_s = df.iloc[:-n], vix_s.iloc[:-n]
    df = add_indicators(df).dropna(subset=["SMA50"])
    vix_s = vix_s.reindex(df.index).ffill() if len(vix_s) else vix_s

    bt, scores = backtest(df.copy())
    df["score"] = scores
    last, prev = df.iloc[-1], df.iloc[-2]
    as_of = df.index[-1].date()

    vix = float(vix_s.iloc[-1]) if len(vix_s) else None
    vix_prev = float(vix_s.iloc[-2]) if len(vix_s) > 1 else None
    oc = get_chain(demo, float(last.Close), as_of)

    score, reasons = score_row(last, prev.MACD_HIST, vix, vix_prev, oc)
    bias = bias_from(score)
    lv = pivots(last.High, last.Low, last.Close)

    today = make_entry(as_of.strftime("%Y-%m-%d"), last, prev.MACD_HIST, vix, vix_prev, oc)
    track = update_track(df, vix_s, today)

    def col(s, nd=2):
        return [None if pd.isna(v) else round(float(v), nd) for v in s]

    levels = {k: round(float(v), 1) for k, v in lv.items()}
    if oc:
        levels["Call-OI wall"] = oc["max_ce_oi_strike"]
        levels["Put-OI wall"] = oc["max_pe_oi_strike"]
        levels["Max pain"] = oc["max_pain"]

    data = {
        "symbol": "NIFTY 50" + (" (DEMO DATA)" if demo else ""),
        "updated": dt.datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST"),
        "as_of": as_of.strftime("%Y-%m-%d"),
        "dates": [d.strftime("%Y-%m-%d") for d in df.index],
        **{k.lower(): col(df[k]) for k in ["Open", "High", "Low", "Close", "SMA20", "SMA50", "SMA200",
                                             "BB_UP", "BB_LO", "RSI", "MACD", "MACD_SIG", "MACD_HIST", "score"]},
        "vix_series": col(vix_s) if len(vix_s) else [],
        "last": {"close": round(float(last.Close), 2),
                 "change": round(float(last.Close - prev.Close), 2),
                 "change_pct": round(float((last.Close / prev.Close - 1) * 100), 2),
                 "rsi": round(float(last.RSI), 1), "atr": round(float(last.ATR), 1)},
        "vix": None if vix is None else {
            "value": round(vix, 2),
            "change_pct": round((vix / vix_prev - 1) * 100, 2) if vix_prev else None,
            # VIX is annualised %, so 1-day expected move ≈ VIX / sqrt(252)
            "day_move": round(float(last.Close) * vix / 100 / np.sqrt(252), 1)},
        "levels": levels,
        "expected_range": [round(float(last.Close - last.ATR), 1), round(float(last.Close + last.ATR), 1)],
        "option_chain": oc,
        "signal": {"bias": BIAS_LABEL[bias], "score": score, "reasons": reasons,
                   "plan": make_plan(bias, lv, float(last.Close))},
        "backtest": bt,
        "track": {"entries": track[::-1], "summary": summarize_track(track)},
    }
    with open("data.json", "w") as f:
        json.dump(data, f)
    print(f"{data['as_of']} close={last.Close:.1f} vix={vix} pcr={oc and oc['pcr']} "
          f"score={score} -> {bias}; tracked={len(track)}")


if __name__ == "__main__":
    main()
