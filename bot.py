#!/usr/bin/env python3
"""
Sensex PDH/PDL reversal paper trader (no real orders, ever).

Strategy (version B from the backtest):
  CALL: price breaks previous-day LOW -> RSI(14, 1-min) touches <= 20 after the break ->
        a green candle (body >= 10 pts) that closes back above the previous-day low ->
        the NEXT candle closes above that green candle's close -> enter at that close.
  PUT : mirror image at the previous-day HIGH (RSI >= 80, red candle closing back below PDH,
        next candle closes below it).
  Entries only before 13:00 IST, one open trade per book, stop for the day after 2 losses,
  everything squared off at 15:15 IST.

Three virtual books trade the same signals side by side (P&L always uses the ATM option premium):
  TRAIL : main book. Stops measured on the Sensex: initial SL 75 pts; at +100 pts SL moves to
          breakeven; at +150 pts SL locks +100 pts and then trails 50 pts below the best level.
          No fixed target, so big moves can run until 15:15. (Best exit in the 60-day backtest.)
  SLIDE : the original sliding stop on premium: SL -10%, target +20%, both move up 10% for
          every +10% gain (works as a 10% trailing stop). Kept for comparison.
  BT75  : fixed 75 / 150 Sensex points, the original backtest exit. Kept for comparison.

Data: Groww public chart / option endpoints (same data as the Groww website).
"""
import json, os, re, sys, time, math, subprocess, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone, date

IST = timezone(timedelta(hours=5, minutes=30))
BASE = "https://groww.in/v1/api"
UA = {"User-Agent": "Mozilla/5.0 (paper-trader)", "Accept": "application/json"}

# ---------------- settings (override with environment variables) ----------------
END_DATE      = os.getenv("END_DATE", "2026-11-26")      # last day to trade
LOT           = int(os.getenv("LOT_SIZE", "20"))          # Sensex lot size
LOTS          = int(os.getenv("LOTS", "1"))
CHARGES       = float(os.getenv("CHARGES_PER_TRADE", "60"))  # Rs per round trip per lot (brokerage+taxes)
POLL_SEC      = int(os.getenv("POLL_SEC", "15"))
RSI_LEVEL     = float(os.getenv("RSI_LEVEL", "20"))
MIN_BODY      = float(os.getenv("MIN_BODY", "10"))
ENTRY_CUTOFF  = os.getenv("ENTRY_CUTOFF", "13:00")
SQUARE_OFF    = os.getenv("SQUARE_OFF", "15:15")
MAX_LOSSES    = int(os.getenv("MAX_LOSSES_PER_DAY", "2"))
SL_PCT, TGT_PCT, STEP_PCT = 10.0, 20.0, 10.0
BT_SL, BT_TGT = 75.0, 150.0
TR_SL, TR_BE, TR_LOCK_AT, TR_LOCK, TR_GAP = 75.0, 100.0, 150.0, 100.0, 50.0
BOOKS = {"TRAIL": "Main: 75-pt SL, breakeven at +100, lock +100 and trail 50 pts after +150",
         "SLIDE": "Sliding SL on premium (10% SL, 20% target, steps of 10%)",
         "BT75": "Fixed exit (75 / 150 Sensex points)"}
STATE_FILE, TRADES_CSV, REPORT_DIR = "state.json", "trades.csv", "reports"

def now(): return datetime.now(IST)
def hm(s): h, m = map(int, s.split(":")); return h * 60 + m
def mins(dt): return dt.hour * 60 + dt.minute
def ist_dt(ts): return datetime.fromtimestamp(ts, IST)
def log(*a): print(now().strftime("%H:%M:%S"), *a, flush=True)

# ---------------- data ----------------
def get(url, as_json=True, tries=4):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                return json.loads(body) if as_json else body
        except Exception as e:
            if k == tries - 1: raise
            time.sleep(2 + 3 * k)

def chart(path, interval, days_back):
    end = int(time.time() * 1000); start = end - days_back * 86400000
    j = get(f"{BASE}/charting_service/v2/chart/exchange/{path}?endTimeInMillis={end}"
            f"&intervalInMinutes={interval}&startTimeInMillis={start}")
    return j.get("candles") or []

def sensex_1m():   return chart("BSE/segment/CASH/1", 1, 6)
def sensex_day():  return chart("BSE/segment/CASH/1", 1440, 20)
def vix_now():
    try:
        c = chart("NSE/segment/CASH/INDIAVIX", 1, 1); return c[-1][4] if c else None
    except Exception: return None

def add_rsi(candles, n=14):
    out, ag, al = [], 0.0, 0.0
    for i, c in enumerate(candles):
        r = None
        if i > 0:
            ch = c[4] - candles[i - 1][4]; g, l = max(ch, 0), max(-ch, 0)
            if i <= n:
                ag += g / n; al += l / n
                if i == n: r = 100 - 100 / (1 + ag / (al or 1e-9))
            else:
                ag = (ag * (n - 1) + g) / n; al = (al * (n - 1) + l) / n
                r = 100 - 100 / (1 + ag / (al or 1e-9))
        out.append({"ts": c[0], "o": c[1], "h": c[2], "l": c[3], "c": c[4], "rsi": r})
    return out

def prev_day_levels(day_candles, today):
    prev = [c for c in day_candles if ist_dt(c[0]).date() < today]
    if not prev: raise RuntimeError("no previous daily candle")
    p = prev[-1]; return p[2], p[3], ist_dt(p[0]).date().isoformat()

# ---------------- options ----------------
_contract_cache = {}
def pick_expiry(today):
    j = get(f"{BASE}/option_chain_service/v1/option_chain/sp-bse-sensex")
    exps = j["optionChain"]["expiryDetailsDto"]["expiryDates"]
    exps = [e for e in exps if e >= today.isoformat()]
    # on expiry day itself, roll to the next expiry (premium of a same-day expiry decays too fast)
    return exps[1] if exps and exps[0] == today.isoformat() and len(exps) > 1 else exps[0]

def contracts_for(expiry):
    if expiry in _contract_cache: return _contract_cache[expiry]
    html = get(f"https://groww.in/options/sp-bse-sensex?expiry={expiry}", as_json=False)
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    data = json.loads(m.group(1)); found = {}
    def walk(o):
        if isinstance(o, dict):
            sp = o.get("strikePrice")
            if isinstance(sp, (int, float)):
                strike = int(round(sp / 100 if sp > 1e6 else sp))
                cands = [o] + [o.get(k) for k in ("ce", "pe", "callOption", "putOption") if isinstance(o.get(k), dict)]
                for x in cands:
                    cid = x.get("growwContractId")
                    if isinstance(cid, str) and cid[-2:] in ("CE", "PE"):
                        found[(strike, cid[-2:])] = cid
            for v in o.values(): walk(v)
        elif isinstance(o, list):
            for v in o: walk(v)
    walk(data)
    if not found: raise RuntimeError(f"no contracts parsed for {expiry}")
    _contract_cache[expiry] = found; return found

def option_ltp(cid):
    j = get(f"{BASE}/stocks_fo_data/v1/tr_live_prices/exchange/BSE/segment/FNO/{cid}/latest")
    return float(j["ltp"])

def atm_contract(spot, side, today):
    exp = pick_expiry(today); cmap = contracts_for(exp)
    typ = "CE" if side == "CALL" else "PE"
    strikes = sorted(s for (s, t) in cmap if t == typ)
    strike = min(strikes, key=lambda s: abs(s - spot))
    return cmap[(strike, typ)], strike, exp

# ---------------- signal (shared by live and replay) ----------------
def find_signal(cs, i, pdh, pdl, last_exit_ts):
    """cs: today's completed candles with rsi. Returns 'CALL'/'PUT'/None for candle i (the confirm candle)."""
    if i < 1: return None
    c, p = cs[i], cs[i - 1]
    if mins(ist_dt(c["ts"])) >= hm(ENTRY_CUTOFF): return None
    if p["ts"] <= last_exit_ts: return None
    for side in ("CALL", "PUT"):
        brk = next((k for k in range(i + 1) if (cs[k]["l"] < pdl if side == "CALL" else cs[k]["h"] > pdh)), None)
        if brk is None or i - 1 <= brk: continue
        rs = [x["rsi"] for x in cs[brk:i + 1] if x["rsi"] is not None]
        if not rs: continue
        if side == "CALL":
            ok = (min(rs) <= RSI_LEVEL and p["c"] > p["o"] and p["c"] - p["o"] >= MIN_BODY
                  and p["c"] > pdl and c["c"] > p["c"])
        else:
            ok = (max(rs) >= 100 - RSI_LEVEL and p["o"] > p["c"] and p["o"] - p["c"] >= MIN_BODY
                  and p["c"] < pdh and c["c"] < p["c"])
        if ok: return side
    return None

# ---------------- state ----------------
def new_book(): return {"pos": None, "day": None, "losses": 0, "last_exit_ts": 0}
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f: s = json.load(f)
        for b in BOOKS: s["books"].setdefault(b, new_book())
        return s
    return {"started": now().date().isoformat(), "books": {b: new_book() for b in BOOKS}, "trades": []}
def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=1)

def notify(text):
    tok, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (tok and chat): return
    try:
        data = json.dumps({"chat_id": chat, "text": text[:4000]}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=data,
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e: log("telegram failed:", e)

# ---------------- trade management ----------------
def open_trade(state, book, side, spot, sig_ts, today):
    try:
        cid, strike, exp = atm_contract(spot, side, today); prem = option_ltp(cid)
    except Exception as e:
        log("could not price option, skipping signal:", e); return
    pos = {"side": side, "cid": cid, "strike": strike, "expiry": exp, "entry_prem": prem,
           "entry_spot": spot, "entry_time": now().strftime("%H:%M:%S"), "signal_ts": sig_ts,
           "peak_pct": 0.0, "mfe": 0.0, "vix": vix_now()}
    state["books"][book]["pos"] = pos
    msg = f"[{book}] ENTRY {side} {cid} @ Rs{prem:.2f} (Sensex {spot:.2f})"
    log(msg); notify(msg)

def ladder(peak_pct):
    k = int(math.floor(peak_pct / STEP_PCT + 1e-9)) if peak_pct > 0 else 0
    sl = -SL_PCT if k == 0 else (k - 1) * STEP_PCT
    return sl, TGT_PCT + k * STEP_PCT

def check_exit(book, pos, ltp, spot_hi, spot_lo, spot, t_min):
    if t_min >= hm(SQUARE_OFF): return "TIME"
    if book == "SLIDE":
        g = (ltp / pos["entry_prem"] - 1) * 100
        pos["peak_pct"] = max(pos["peak_pct"], g)
        sl, tgt = ladder(pos["peak_pct"]); pos["sl_pct"], pos["tgt_pct"] = sl, tgt
        if g <= sl: return "SL" if sl < 0 else "TRAIL"
        if g >= tgt: return "TGT"
    elif book == "TRAIL":
        e = pos["entry_spot"]; call = pos["side"] == "CALL"; sg = 1 if call else -1
        lvl = pos.get("stop_lvl", -TR_SL)                      # stop as points from entry
        if (spot - e) * sg <= lvl: return "SL" if lvl < 0 else "TRAIL"
        pos["mfe"] = max(pos.get("mfe", 0.0), (spot_hi - e) if call else (e - spot_lo))
        m = pos["mfe"]; new = -TR_SL
        if m >= TR_BE: new = 0.0
        if m >= TR_LOCK_AT: new = max(TR_LOCK, m - TR_GAP)
        pos["stop_lvl"] = max(lvl, new)
    else:
        e = pos["entry_spot"]; call = pos["side"] == "CALL"
        if (spot_lo <= e - BT_SL) if call else (spot_hi >= e + BT_SL): return "SL"
        if (spot_hi >= e + BT_TGT) if call else (spot_lo <= e - BT_TGT): return "TGT"
    return None

def close_trade(state, book, ltp, spot, reason, today):
    b = state["books"][book]; pos = b["pos"]
    pnl = (ltp - pos["entry_prem"]) * LOT * LOTS - CHARGES * LOTS
    idx_pts = (spot - pos["entry_spot"]) * (1 if pos["side"] == "CALL" else -1)
    t = {"date": today.isoformat(), "book": book, "side": pos["side"], "contract": pos["cid"],
         "entry_time": pos["entry_time"], "exit_time": now().strftime("%H:%M:%S"),
         "entry_prem": round(pos["entry_prem"], 2), "exit_prem": round(ltp, 2),
         "prem_pct": round((ltp / pos["entry_prem"] - 1) * 100, 1), "index_pts": round(idx_pts, 1),
         "pnl_rs": round(pnl, 0), "reason": reason, "vix": pos.get("vix")}
    state["trades"].append(t); b["pos"] = None
    b["last_exit_ts"] = int(time.time()) // 60 * 60
    if pnl <= 0: b["losses"] += 1
    new = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a") as f:
        if new: f.write(",".join(t.keys()) + "\n")
        f.write(",".join(str(v) for v in t.values()) + "\n")
    msg = f"[{book}] EXIT {pos['side']} {pos['cid']} @ Rs{ltp:.2f} ({t['prem_pct']:+}%) {reason} P&L Rs{pnl:,.0f}"
    log(msg); notify(msg)

# ---------------- reports ----------------
def stats(ts):
    n = len(ts); w = sum(1 for t in ts if t["pnl_rs"] > 0); tot = sum(t["pnl_rs"] for t in ts)
    return n, (100 * w / n if n else 0), tot

def write_report(state, today):
    os.makedirs(REPORT_DIR, exist_ok=True)
    d = today.isoformat(); day_tr = [t for t in state["trades"] if t["date"] == d]
    L = [f"# Paper-trading report: {d}", "",
         "Paper trades only. Premiums are Groww last-traded prices polled every "
         f"{POLL_SEC}s; P&L is for {LOTS} lot(s) of {LOT} after Rs{CHARGES:.0f}/trade charges.", ""]
    for book, label in BOOKS.items():
        bt = [t for t in day_tr if t["book"] == book]; n, w, tot = stats(bt)
        cn, cw, ctot = stats([t for t in state["trades"] if t["book"] == book])
        L += [f"## {book}: {label}", "",
              f"Today: {n} trades, win rate {w:.0f}%, net Rs{tot:,.0f}. "
              f"Since {state['started']}: {cn} trades, win rate {cw:.0f}%, net Rs{ctot:,.0f}.", ""]
        if bt:
            L += ["| Side | Contract | Entry | Exit | Entry Rs | Exit Rs | Premium % | Index pts | Reason | P&L Rs |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
            L += [f"| {t['side']} | {t['contract']} | {t['entry_time']} | {t['exit_time']} | {t['entry_prem']} | "
                  f"{t['exit_prem']} | {t['prem_pct']:+} | {t['index_pts']:+} | {t['reason']} | {t['pnl_rs']:,.0f} |" for t in bt]
            L.append("")
    note = state.get("day_notes", {}).get(d)
    if note: L += [f"Notes: {note}", ""]
    txt = "\n".join(L)
    with open(f"{REPORT_DIR}/{d}.md", "w") as f: f.write(txt)
    days = sorted({t["date"] for t in state["trades"]} | {d})
    summ = ["# Summary", "", "| Date | " + " | ".join(f"{b} trades | {b} P&L Rs" for b in BOOKS) + " |",
            "|---|" + "---|---|" * len(BOOKS)]
    for dd in days + ["TOTAL"]:
        cells = []
        for b in BOOKS:
            n, w, tot = stats([t for t in state["trades"] if t["book"] == b and (dd == "TOTAL" or t["date"] == dd)])
            cells += [str(n), f"{tot:,.0f}"]
        name = "**Total**" if dd == "TOTAL" else f"[{dd}](reports/{dd}.md)"
        summ.append(f"| {name} | " + " | ".join(cells) + " |")
    with open("REPORT.md", "w") as f: f.write("\n".join(summ) + "\n")
    if os.getenv("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f: f.write(txt + "\n")
    notify(txt.replace("|", " ")[:3900])
    log("report written")

# ---------------- live loop ----------------
def live():
    t0 = now(); today = t0.date()
    if today.isoformat() > END_DATE:
        log(f"END_DATE {END_DATE} passed; nothing to do. Disable the workflow in GitHub."); return
    if today.weekday() >= 5: log("weekend"); return
    stop_at = hm(os.getenv("STOP_AT") or ("12:45" if mins(t0) < 12 * 60 else "15:25"))
    state = load_state()
    for b in state["books"].values():
        if b["day"] != today.isoformat():
            b.update({"day": today.isoformat(), "losses": 0, "last_exit_ts": 0})
            if b["pos"]: log("stale position from earlier day dropped"); b["pos"] = None
    pdh, pdl, pday = prev_day_levels(sensex_day(), today)
    log(f"Prev day {pday}: high {pdh:.2f} low {pdl:.2f}; running until {stop_at // 60:02d}:{stop_at % 60:02d}")
    start_ts = int(time.time()) - 120            # don't act on signals completed before this run (2-min grace)
    seen = set(); reported = False
    while True:
        tnow = now(); tm = mins(tnow)
        if tm >= stop_at: break
        if tm < hm("09:15"): time.sleep(20); continue
        try:
            raw = sensex_1m()
        except Exception as e:
            log("data error:", e); time.sleep(POLL_SEC); continue
        allc = add_rsi(raw)
        today_all = [c for c in allc if ist_dt(c["ts"]).date() == today]
        if not today_all:
            if tm > hm("09:45"):
                state.setdefault("day_notes", {})[today.isoformat()] = "No market data today (holiday?)"
                log("no candles today, treating as holiday"); break
            time.sleep(POLL_SEC); continue
        nowts = int(time.time())
        done = [c for c in today_all if c["ts"] + 60 <= nowts]
        spot = today_all[-1]["c"]
        # manage open positions
        for book, b in state["books"].items():
            pos = b["pos"]
            if not pos: continue
            since = [c for c in today_all if c["ts"] + 60 > pos["signal_ts"] + 60]
            hi = max([c["h"] for c in since] + [spot]); lo = min([c["l"] for c in since] + [spot])
            try: ltp = option_ltp(pos["cid"])
            except Exception as e: log("ltp error:", e); continue
            reason = check_exit(book, pos, ltp, hi, lo, spot, tm)
            if reason: close_trade(state, book, ltp, spot, reason, today)
        # new signals on the latest completed candle
        if done and len(done) >= 2:
            i = len(done) - 1; c = done[i]
            if c["ts"] not in seen and c["ts"] + 60 >= start_ts:
                seen.add(c["ts"])
                for book, b in state["books"].items():
                    if b["pos"] or b["losses"] >= MAX_LOSSES: continue
                    side = find_signal(done, i, pdh, pdl, b["last_exit_ts"])
                    if side: open_trade(state, book, side, c["c"], c["ts"], today)
        save_state(state)
        if tm >= hm(SQUARE_OFF) + 1 and not any(b["pos"] for b in state["books"].values()):
            write_report(state, today); reported = True; break
        time.sleep(POLL_SEC)
    save_state(state)
    if stop_at > hm("15:00") and not reported: write_report(state, today)

# ---------------- replay (index-only check of the signal logic on past days) ----------------
def replay(days=60):
    raw = []; end = int(time.time() * 1000)
    for _ in range(math.ceil(days * 1.6 / 5) + 2):
        st = end - 5 * 86400000
        j = get(f"{BASE}/charting_service/v2/chart/exchange/BSE/segment/CASH/1?endTimeInMillis={end}"
                f"&intervalInMinutes=1&startTimeInMillis={st}")
        raw += j.get("candles") or []; end = st; time.sleep(0.3)
    raw = sorted({c[0]: c for c in raw}.values())
    allc = add_rsi(raw); dayc = chart("BSE/segment/CASH/1", 1440, int(days * 1.6) + 30)
    by = {}
    for c in allc: by.setdefault(ist_dt(c["ts"]).date(), []).append(c)
    today = now().date(); keys = [k for k in sorted(by) if k < today and len(by[k]) > 200][-days:]
    res = []
    for d in keys:
        pdh, pdl, _ = prev_day_levels(dayc, d); cs = by[d]; pos = None; last_exit = 0; losses = 0
        for i, c in enumerate(cs):
            t = mins(ist_dt(c["ts"]))
            if pos:
                call = pos["side"] == "CALL"; e = pos["e"]; x = None
                if (c["l"] <= e - BT_SL) if call else (c["h"] >= e + BT_SL): x = e - BT_SL if call else e + BT_SL
                elif (c["h"] >= e + BT_TGT) if call else (c["l"] <= e - BT_TGT): x = e + BT_TGT if call else e - BT_TGT
                elif t >= hm(SQUARE_OFF) or i == len(cs) - 1: x = c["c"]
                if x is not None:
                    pts = (x - e if call else e - x) - 6; res.append((d, pos["side"], pts)); pos = None
                    last_exit = c["ts"]; losses += pts <= 0
                continue
            if losses >= MAX_LOSSES: continue
            side = find_signal(cs[:i + 1], i, pdh, pdl, last_exit)
            if side: pos = {"side": side, "e": c["c"]}
    n = len(res); w = sum(1 for r in res if r[2] > 0); tot = sum(r[2] for r in res)
    print(f"Replay {keys[0]}..{keys[-1]}: {n} trades, win {100*w/max(n,1):.0f}%, avg {tot/max(n,1):.1f} pts, total {tot:.0f} pts")
    return res

def once():
    today = now().date(); pdh, pdl, pday = prev_day_levels(sensex_day(), today)
    c = add_rsi(sensex_1m()); spot = c[-1]["c"]
    print(f"Prev day {pday} H {pdh} L {pdl}; spot {spot}; RSI {c[-1]['rsi']:.1f}; VIX {vix_now()}")
    for side in ("CALL", "PUT"):
        cid, strike, exp = atm_contract(spot, side, today); print(side, cid, exp, option_ltp(cid))

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "live"
    {"live": live, "replay": replay, "once": once}[mode]()
