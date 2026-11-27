#!/usr/bin/env python3
"""
Auto day trader driven by criteria.yaml.

Commands:
  python bot.py check        validate criteria.yaml and show the effective settings
  python bot.py scan         show every watchlist symbol and which entry rules pass/fail
  python bot.py once         run ONE trading cycle (manage exits, then look for entries)
  python bot.py run          loop `once` every poll_seconds until flatten time / Ctrl-C
  python bot.py status       account, day P&L, open bot positions, today's trades
  python bot.py flatten      close every position the bot opened and halt for the day
  python bot.py backtest     simulate the criteria (synthetic data, or --source alpaca)

Safety:
  * Default mode is paper. Live needs `mode: live` AND env DAYTRADER_ALLOW_LIVE=yes.
  * Create a file named STOP in this folder to flatten and halt immediately.
  * The bot only touches positions it opened itself (tracked in state.json).
  * Educational tool, not financial advice. Backtest and paper trade first.
"""
import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

HERE = Path(__file__).resolve().parent
CRITERIA = HERE / "criteria.yaml"
STATE = HERE / "state.json"
JOURNAL = HERE / "journal.jsonl"
STOPFILE = HERE / "STOP"
ET = ZoneInfo("America/New_York")
TF_MIN = {"1Min": 1, "5Min": 5, "15Min": 15}


# ----------------------------------------------------------------- config
def load_env():
    f = HERE / ".env"
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def load_criteria():
    c = yaml.safe_load(CRITERIA.read_text())
    errors = []
    if c.get("mode") not in ("sim", "paper", "live"):
        errors.append("mode must be sim, paper or live")
    if c.get("timeframe") not in TF_MIN:
        errors.append(f"timeframe must be one of {list(TF_MIN)}")
    if not c.get("watchlist"):
        errors.append("watchlist is empty")
    e, r = c.get("entry", {}), c.get("risk", {})
    if e.get("ema_fast", 0) >= e.get("ema_slow", 0):
        errors.append("entry.ema_fast must be smaller than entry.ema_slow")
    if r.get("stop_loss_pct", 0) <= 0:
        errors.append("risk.stop_loss_pct must be > 0 (every trade needs a stop)")
    if r.get("daily_loss_limit", 0) <= 0:
        errors.append("risk.daily_loss_limit must be > 0")
    if not (0 < r.get("risk_per_trade_pct", 0) <= 5):
        errors.append("risk.risk_per_trade_pct must be between 0 and 5")
    need = max(e.get("ema_slow", 21), e.get("rsi_period", 14)) + 25
    if c.get("lookback_bars", 0) < need:
        errors.append(f"lookback_bars must be at least {need} for these indicator lengths")
    for k in ("no_new_entries_after", "flatten_at", "market_open"):
        try:
            hhmm(c["schedule"][k])
        except Exception:
            errors.append(f"schedule.{k} must look like \"15:30\"")
    if errors:
        raise SystemExit("criteria.yaml problems:\n  - " + "\n  - ".join(errors))
    c["watchlist"] = [s.upper() for s in c["watchlist"]]
    return c


def hhmm(s):
    h, m = str(s).split(":")
    return int(h) * 60 + int(m)


def minutes_et(dt):
    d = dt.astimezone(ET)
    return d.hour * 60 + d.minute


# ------------------------------------------------------------- indicators
def ema(vals, n):
    k, out = 2 / (n + 1), []
    for i, v in enumerate(vals):
        out.append(v if i == 0 else v * k + out[-1] * (1 - k))
    return out


def rsi(vals, n):
    out = [50.0] * len(vals)
    if len(vals) <= n:
        return out
    gains = [max(vals[i] - vals[i - 1], 0) for i in range(1, len(vals))]
    losses = [max(vals[i - 1] - vals[i], 0) for i in range(1, len(vals))]
    ag, al = sum(gains[:n]) / n, sum(losses[:n]) / n
    for i in range(n, len(vals)):
        if i > n:
            ag = (ag * (n - 1) + gains[i - 1]) / n
            al = (al * (n - 1) + losses[i - 1]) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def session_vwap(bars):
    day = bars[-1]["t"].astimezone(ET).date()
    pv = vol = 0.0
    for b in reversed(bars):
        if b["t"].astimezone(ET).date() != day:
            break
        tp = (b["h"] + b["l"] + b["c"]) / 3
        pv += tp * b["v"]
        vol += b["v"]
    return pv / vol if vol else bars[-1]["c"]


# --------------------------------------------------------------- strategy
class Strategy:
    def __init__(self, c):
        self.c, self.e, self.x, self.r = c, c["entry"], c["exit"], c["risk"]

    def min_bars(self):
        return max(self.e["ema_slow"], self.e["rsi_period"]) + 21

    def indicators(self, bars):
        closes = [b["c"] for b in bars]
        vols = [b["v"] for b in bars[-21:-1]]
        return {
            "price": closes[-1],
            "ef": ema(closes, self.e["ema_fast"]),
            "es": ema(closes, self.e["ema_slow"]),
            "rsi": rsi(closes, self.e["rsi_period"])[-1],
            "vwap": session_vwap(bars),
            "avgv": sum(vols) / max(len(vols), 1),
            "lastv": bars[-1]["v"],
        }

    def _fresh_cross(self, ef, es, up):
        n = self.e["require_fresh_cross_bars"]
        if n <= 0:
            return True
        for i in range(len(ef) - n, len(ef)):
            if i < 1:
                continue
            if up and ef[i - 1] <= es[i - 1] and ef[i] > es[i]:
                return True
            if not up and ef[i - 1] >= es[i - 1] and ef[i] < es[i]:
                return True
        return False

    def evaluate(self, bars):
        """Return (side or None, {side: [(rule, passed, detail), ...]}, indicators)."""
        if len(bars) < self.min_bars():
            return None, {"-": [("enough bars", False, f"{len(bars)}/{self.min_bars()}")]}, None
        ind, e = self.indicators(bars), self.e
        p, ef, es = ind["price"], ind["ef"], ind["es"]
        common = [
            ("price in range", e["price_min"] <= p <= e["price_max"], f"{p:.2f}"),
            ("avg volume", ind["avgv"] >= e["min_avg_volume"], f"{ind['avgv']:.0f}"),
        ]
        if e["min_bar_volume_vs_avg"] > 0:
            ratio = ind["lastv"] / ind["avgv"] if ind["avgv"] else 0
            common.append(("volume surge", ratio >= e["min_bar_volume_vs_avg"], f"{ratio:.2f}x"))
        out = {}
        if e["allow_long"]:
            rules = common + [
                ("EMA fast > slow", ef[-1] > es[-1], f"{ef[-1]:.2f}/{es[-1]:.2f}"),
                ("fresh cross up", self._fresh_cross(ef, es, True), ""),
                ("RSI in long band", e["rsi_long_min"] <= ind["rsi"] <= e["rsi_long_max"], f"{ind['rsi']:.1f}"),
            ]
            if e["require_vwap_confirm"]:
                rules.append(("above VWAP", p > ind["vwap"], f"vwap {ind['vwap']:.2f}"))
            out["long"] = rules
        if e["allow_short"]:
            rules = common + [
                ("EMA fast < slow", ef[-1] < es[-1], f"{ef[-1]:.2f}/{es[-1]:.2f}"),
                ("fresh cross down", self._fresh_cross(ef, es, False), ""),
                ("RSI in short band", e["rsi_short_min"] <= ind["rsi"] <= e["rsi_short_max"], f"{ind['rsi']:.1f}"),
            ]
            if e["require_vwap_confirm"]:
                rules.append(("below VWAP", p < ind["vwap"], f"vwap {ind['vwap']:.2f}"))
            out["short"] = rules
        side = next((s for s, rules in out.items() if all(ok for _, ok, _ in rules)), None)
        return side, out, ind

    def exit_reason(self, bars, pos, now, include_hard=True):
        """pos: {side, entry_price, entry_time, best_price}. Returns reason or None."""
        ind = self.indicators(bars) if len(bars) >= self.min_bars() else None
        p = bars[-1]["c"]
        long = pos["side"] == "long"
        r, x = self.r, self.x
        move = (p - pos["entry_price"]) / pos["entry_price"] * 100 * (1 if long else -1)
        if include_hard and move <= -r["stop_loss_pct"]:
            return f"stop loss ({move:.2f}%)"
        if include_hard and move >= r["take_profit_pct"]:
            return f"take profit ({move:.2f}%)"
        if r["trailing_stop_pct"] > 0:
            best = pos["best_price"]
            give = (best - p) / best * 100 if long else (p - best) / best * 100
            in_profit = (best > pos["entry_price"]) if long else (best < pos["entry_price"])
            if in_profit and give >= r["trailing_stop_pct"]:
                return f"trailing stop ({give:.2f}% off best {best:.2f})"
        if r["max_hold_minutes"] > 0:
            held = (now - datetime.fromisoformat(pos["entry_time"])).total_seconds() / 60
            if held >= r["max_hold_minutes"]:
                return f"max hold time ({held:.0f} min)"
        if ind:
            if x["exit_on_ema_cross_back"]:
                if long and ind["ef"][-1] < ind["es"][-1]:
                    return "EMA crossed back down"
                if not long and ind["ef"][-1] > ind["es"][-1]:
                    return "EMA crossed back up"
            if long and x["exit_long_rsi_above"] and ind["rsi"] >= x["exit_long_rsi_above"]:
                return f"RSI overbought ({ind['rsi']:.1f})"
            if not long and x["exit_short_rsi_below"] and ind["rsi"] <= x["exit_short_rsi_below"]:
                return f"RSI oversold ({ind['rsi']:.1f})"
        return None

    def size(self, equity, buying_power, price):
        r = self.r
        risk_dollars = equity * r["risk_per_trade_pct"] / 100
        per_share_risk = price * r["stop_loss_pct"] / 100
        qty = math.floor(risk_dollars / per_share_risk)
        qty = min(qty, math.floor(r["max_position_value"] / price), math.floor(buying_power * 0.95 / price))
        return max(qty, 0)

    def brackets(self, side, price):
        s, t = self.r["stop_loss_pct"] / 100, self.r["take_profit_pct"] / 100
        if side == "long":
            return round(price * (1 - s), 2), round(price * (1 + t), 2)
        return round(price * (1 + s), 2), round(price * (1 - t), 2)


# ----------------------------------------------------------------- broker
class Alpaca:
    def __init__(self, c):
        import requests

        self.rq = requests.Session()
        load_env()
        key, sec = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_API_SECRET")
        if not key or not sec:
            raise SystemExit(
                "Missing ALPACA_API_KEY / ALPACA_API_SECRET. Put them in daytrader/.env "
                "(see .env.example) or export them. Or set mode: sim to run offline."
            )
        if c["mode"] == "live":
            if os.environ.get("DAYTRADER_ALLOW_LIVE") != "yes":
                raise SystemExit("mode is live but DAYTRADER_ALLOW_LIVE=yes is not set. Refusing to trade real money.")
            self.base = "https://api.alpaca.markets"
        else:
            self.base = "https://paper-api.alpaca.markets"
        self.data = "https://data.alpaca.markets"
        self.rq.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": sec})
        self.c = c

    def _req(self, method, url, **kw):
        for attempt in range(3):
            resp = self.rq.request(method, url, timeout=20, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"{method} {url} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.text else {}
        raise RuntimeError(f"{method} {url} failed after retries")

    def clock(self):
        j = self._req("GET", f"{self.base}/v2/clock")
        return j["is_open"], datetime.fromisoformat(j["timestamp"].replace("Z", "+00:00"))

    def account(self):
        j = self._req("GET", f"{self.base}/v2/account")
        return {
            "equity": float(j["equity"]),
            "last_equity": float(j["last_equity"]),
            "buying_power": float(j["buying_power"]),
            "pdt": j.get("pattern_day_trader"),
            "daytrade_count": j.get("daytrade_count"),
        }

    def positions(self):
        out = {}
        for p in self._req("GET", f"{self.base}/v2/positions"):
            out[p["symbol"]] = {
                "qty": abs(float(p["qty"])),
                "side": p["side"],
                "avg_entry": float(p["avg_entry_price"]),
                "price": float(p["current_price"]),
                "upl": float(p["unrealized_pl"]),
            }
        return out

    def bars(self, symbols, now, days=None, keep=None):
        tf = self.c["timeframe"]
        days = days or max(4, math.ceil(self.c["lookback_bars"] * TF_MIN[tf] / 390) + 4)
        params = {
            "symbols": ",".join(symbols),
            "timeframe": tf,
            "start": (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 10000,
            "feed": self.c.get("data_feed", "iex"),
            "adjustment": "raw",
        }
        out = {s: [] for s in symbols}
        while True:
            j = self._req("GET", f"{self.data}/v2/stocks/bars", params=params)
            for s, bl in (j.get("bars") or {}).items():
                for b in bl:
                    out[s].append({
                        "t": datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                        "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"],
                    })
            if not j.get("next_page_token"):
                break
            params["page_token"] = j["next_page_token"]
        n = keep or self.c["lookback_bars"]
        return {s: sorted(bl, key=lambda b: b["t"])[-n:] for s, bl in out.items()}

    def open_bracket(self, symbol, side, qty, stop, target):
        return self._req("POST", f"{self.base}/v2/orders", json={
            "symbol": symbol, "qty": str(qty), "side": "buy" if side == "long" else "sell",
            "type": "market", "time_in_force": "day", "order_class": "bracket",
            "take_profit": {"limit_price": f"{target:.2f}"},
            "stop_loss": {"stop_price": f"{stop:.2f}"},
        })

    def close(self, symbol):
        # bracket legs reserve the shares, so cancel them first
        for o in self._req("GET", f"{self.base}/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"}):
            try:
                self._req("DELETE", f"{self.base}/v2/orders/{o['id']}")
            except RuntimeError:
                pass
        time.sleep(0.5)
        try:
            return self._req("DELETE", f"{self.base}/v2/positions/{symbol}")
        except RuntimeError as err:
            if "404" in str(err):
                return None  # already flat
            raise


# ------------------------------------------------------------ state/journal
def load_state(today):
    s = json.loads(STATE.read_text()) if STATE.exists() else {}
    if s.get("date") != today:
        s = {"date": today, "trades_today": 0, "halted": False, "halt_reason": "",
             "positions": s.get("positions", {}), "cooldown": {}, "closed": []}
    return s


def save_state(s):
    STATE.write_text(json.dumps(s, indent=2, default=str))


def journal(event, **kw):
    row = {"ts": datetime.now(timezone.utc).isoformat(), "event": event, **kw}
    with JOURNAL.open("a") as f:
        f.write(json.dumps(row, default=str) + "\n")
    print(f"  [{event}] " + ", ".join(f"{k}={v}" for k, v in kw.items()))


# ------------------------------------------------------------- live engine
class Engine:
    def __init__(self):
        self.c = load_criteria()
        if self.c["mode"] == "sim":
            raise SystemExit("mode is sim: use `python bot.py backtest` (or set mode: paper to trade).")
        self.b = Alpaca(self.c)
        self.st = Strategy(self.c)

    def flatten(self, s, reason, halt=True):
        for sym in list(s["positions"]):
            self.b.close(sym)
            journal("exit", symbol=sym, reason=reason)
            s["closed"].append({"symbol": sym, "reason": reason})
            del s["positions"][sym]
        if halt:
            s["halted"], s["halt_reason"] = True, reason

    def cycle(self):
        self.c = load_criteria()  # pick up edits live
        self.st = Strategy(self.c)
        is_open, now = self.b.clock()
        s = load_state(now.astimezone(ET).date().isoformat())
        sch, risk = self.c["schedule"], self.c["risk"]
        m = minutes_et(now)
        print(f"== cycle {now.astimezone(ET):%Y-%m-%d %H:%M:%S} ET | mode={self.c['mode']} ==")
        try:
            if STOPFILE.exists():
                self.flatten(s, "STOP file present")
                print("STOP file found: flattened and halted. Delete daytrader/STOP to resume tomorrow.")
                return "stopped"
            if not is_open:
                print("Market closed. Nothing to do.")
                return "closed"
            acct = self.b.account()
            day_pnl = acct["equity"] - acct["last_equity"]
            print(f"equity ${acct['equity']:,.2f} | day P&L ${day_pnl:+,.2f} | trades today {s['trades_today']}")

            if m >= hhmm(sch["flatten_at"]):
                if s["positions"]:
                    self.flatten(s, "end-of-day flatten", halt=False)
                print("Past flatten time: flat for the day.")
                return "eod"
            if day_pnl <= -risk["daily_loss_limit"] and not s["halted"]:
                self.flatten(s, f"daily loss limit hit (${day_pnl:,.2f})")
            if s["halted"]:
                print(f"HALTED for today: {s['halt_reason']}")
                return "halted"

            held = self.b.positions()
            # positions closed by the broker's bracket stop/target
            for sym in list(s["positions"]):
                if sym not in held:
                    journal("exit", symbol=sym, reason="bracket stop/target filled at broker")
                    s["closed"].append({"symbol": sym, "reason": "bracket"})
                    s["cooldown"][sym] = now.isoformat()
                    del s["positions"][sym]

            symbols = sorted(set(self.c["watchlist"]) | set(s["positions"]))
            bars = self.b.bars(symbols, now)

            # ---- exits
            for sym, pos in list(s["positions"].items()):
                bl = bars.get(sym) or []
                if not bl:
                    continue
                p = held[sym]["price"]
                pos["best_price"] = max(pos["best_price"], p) if pos["side"] == "long" else min(pos["best_price"], p)
                bl[-1] = {**bl[-1], "c": p}
                why = self.st.exit_reason(bl, pos, now)
                if why:
                    self.b.close(sym)
                    journal("exit", symbol=sym, side=pos["side"], price=round(p, 2), reason=why, upl=round(held[sym]["upl"], 2))
                    s["closed"].append({"symbol": sym, "reason": why, "upl": held[sym]["upl"]})
                    s["cooldown"][sym] = now.isoformat()
                    del s["positions"][sym]
                else:
                    print(f"  holding {sym} {pos['side']} {held[sym]['qty']:.0f} @ {pos['entry_price']:.2f} "
                          f"now {p:.2f} (uPL ${held[sym]['upl']:+.2f})")

            # ---- entries
            window_open = hhmm(sch["market_open"]) + sch["no_entries_first_minutes"] <= m < hhmm(sch["no_new_entries_after"])
            target = risk["daily_profit_target"]
            blockers = []
            if not window_open:
                blockers.append("outside entry window")
            if s["trades_today"] >= risk["max_trades_per_day"]:
                blockers.append("max trades per day reached")
            if target and day_pnl >= target:
                blockers.append(f"daily profit target hit (${day_pnl:,.2f})")
            if blockers:
                print("No new entries: " + "; ".join(blockers))
                return "ok"
            for sym in self.c["watchlist"]:
                if len(s["positions"]) >= risk["max_open_positions"]:
                    print("  max open positions reached")
                    break
                if sym in s["positions"] or sym in held:
                    continue
                cd = s["cooldown"].get(sym)
                if cd and (now - datetime.fromisoformat(cd)).total_seconds() < risk["cooldown_minutes"] * 60:
                    continue
                side, rules, ind = self.st.evaluate(bars.get(sym) or [])
                if not side:
                    continue
                price = ind["price"]
                qty = self.st.size(acct["equity"], acct["buying_power"], price)
                if qty < 1:
                    print(f"  {sym}: signal {side} but size < 1 share")
                    continue
                stop, tgt = self.st.brackets(side, price)
                order = self.b.open_bracket(sym, side, qty, stop, tgt)
                s["positions"][sym] = {"side": side, "qty": qty, "entry_price": price, "best_price": price,
                                       "entry_time": now.isoformat(), "stop": stop, "target": tgt,
                                       "order_id": order.get("id")}
                s["trades_today"] += 1
                acct["buying_power"] -= qty * price
                journal("entry", symbol=sym, side=side, qty=qty, price=round(price, 2), stop=stop, target=tgt,
                        why="; ".join(f"{n} {d}".strip() for n, _, d in rules[side]))
            if not s["positions"]:
                print("  no positions; no symbols met every entry rule")
            return "ok"
        finally:
            save_state(s)

    def run(self):
        print("Running. Ctrl-C to stop (open positions stay protected by broker bracket orders).")
        while True:
            try:
                res = self.cycle()
            except Exception as err:  # keep looping through transient API errors
                journal("error", error=str(err))
                res = "error"
            if res in ("eod", "stopped"):
                print("Done for the day.")
                return
            time.sleep(max(15, int(load_criteria()["poll_seconds"])))


# ------------------------------------------------------------- backtester
def synthetic_bars(symbols, days, tf, seed):
    rnd = random.Random(seed)
    per_day = 390 // TF_MIN[tf]
    out = {}
    start = date.today() - timedelta(days=days * 2)
    for sym in symbols:
        price = rnd.uniform(40, 400)
        bl, d = [], start
        while len({b["t"].date() for b in bl}) < days:
            d += timedelta(days=1)
            if d.weekday() >= 5:
                continue
            t0 = datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET)
            drift = 0.0
            for i in range(per_day):
                if rnd.random() < 0.05:
                    drift = rnd.gauss(0, 0.0015)  # regime shift -> trends for the strategy to find
                vol_s = 0.002 * math.sqrt(TF_MIN[tf] / 5)
                o = price
                c = max(1, o * (1 + drift + rnd.gauss(0, vol_s)))
                h = max(o, c) * (1 + abs(rnd.gauss(0, vol_s / 2)))
                l = min(o, c) * (1 - abs(rnd.gauss(0, vol_s / 2)))
                v = int(rnd.uniform(20000, 80000) * (2.5 if i < 6 or i > per_day - 6 else 1)
                        * (1 + 30 * abs(c - o) / o))
                bl.append({"t": t0 + timedelta(minutes=i * TF_MIN[tf]), "o": o, "h": h, "l": l, "c": c, "v": v})
                price = c
        out[sym] = bl
    return out


def alpaca_history(c, days):
    b = Alpaca({**c, "mode": "paper"})
    now = datetime.now(timezone.utc) - timedelta(minutes=20)
    raw = b.bars(c["watchlist"], now, days=math.ceil(days * 7 / 5) + 3, keep=10**7)
    # keep only regular-session bars (09:30-16:00 ET)
    return {s: [x for x in bl if 570 <= minutes_et(x["t"]) < 960] for s, bl in raw.items()}


def backtest(source):
    c = load_criteria()
    st, risk, sch, sim = Strategy(c), c["risk"], c["schedule"], c.get("sim", {})
    tf = TF_MIN[c["timeframe"]]
    slip = sim.get("slippage_pct", 0.02) / 100
    data = alpaca_history(c, sim.get("days", 10)) if source == "alpaca" else \
        synthetic_bars(c["watchlist"], sim.get("days", 10), c["timeframe"], sim.get("seed", 42))
    equity = start_eq = float(sim.get("starting_equity", 25000))
    timeline = sorted({b["t"] for bl in data.values() for b in bl})
    idx = {s: {b["t"]: i for i, b in enumerate(bl)} for s, bl in data.items()}
    positions, trades, cooldown = {}, [], {}
    day, day_pnl, day_trades, halted = None, 0.0, 0, False
    peak, max_dd, daily = start_eq, 0.0, {}

    def close(sym, px, t, reason):
        nonlocal equity, day_pnl
        p = positions.pop(sym)
        px = px * (1 - slip) if p["side"] == "long" else px * (1 + slip)
        pnl = (px - p["entry_price"]) * p["qty"] * (1 if p["side"] == "long" else -1)
        equity += pnl
        day_pnl += pnl
        cooldown[sym] = t
        trades.append({**p, "exit_price": px, "exit_time": t.isoformat(), "pnl": pnl, "reason": reason})

    for t in timeline:
        close_t = t + timedelta(minutes=tf)
        d = t.astimezone(ET).date()
        if d != day:
            if day:
                daily[str(day)] = day_pnl
            day, day_pnl, day_trades, halted = d, 0.0, 0, False
        m = minutes_et(close_t)
        for sym in list(positions):
            i = idx[sym].get(t)
            if i is None:
                continue
            b, p = data[sym][i], positions[sym]
            long = p["side"] == "long"
            # hard stop/target intrabar (stop assumed first = conservative)
            if (long and b["l"] <= p["stop"]) or (not long and b["h"] >= p["stop"]):
                close(sym, p["stop"], close_t, "stop loss"); continue
            if (long and b["h"] >= p["target"]) or (not long and b["l"] <= p["target"]):
                close(sym, p["target"], close_t, "take profit"); continue
            p["best_price"] = max(p["best_price"], b["h"]) if long else min(p["best_price"], b["l"])
            why = st.exit_reason(data[sym][: i + 1][-c["lookback_bars"]:], p, close_t, include_hard=False)
            if why or m >= hhmm(sch["flatten_at"]):
                close(sym, b["c"], close_t, why or "end-of-day flatten")
        if not halted and day_pnl <= -risk["daily_loss_limit"]:
            for sym in list(positions):
                b = data[sym][idx[sym][t]] if t in idx[sym] else None
                if b:
                    close(sym, b["c"], close_t, "daily loss limit")
            halted = True
        window = hhmm(sch["market_open"]) + sch["no_entries_first_minutes"] <= m < hhmm(sch["no_new_entries_after"])
        target_hit = risk["daily_profit_target"] and day_pnl >= risk["daily_profit_target"]
        if window and not halted and not target_hit and day_trades < risk["max_trades_per_day"]:
            for sym in c["watchlist"]:
                if len(positions) >= risk["max_open_positions"] or sym in positions:
                    continue
                if sym in cooldown and (close_t - cooldown[sym]).total_seconds() < risk["cooldown_minutes"] * 60:
                    continue
                i = idx.get(sym, {}).get(t)
                if i is None:
                    continue
                side, rules, ind = st.evaluate(data[sym][: i + 1][-c["lookback_bars"]:])
                if not side:
                    continue
                px = ind["price"] * (1 + slip if side == "long" else 1 - slip)
                used = sum(p["qty"] * p["entry_price"] for p in positions.values())
                qty = st.size(equity, equity * 4 - used, px)  # 4x intraday margin
                if qty < 1:
                    continue
                stop, tgt = st.brackets(side, px)
                positions[sym] = {"symbol": sym, "side": side, "qty": qty, "entry_price": px, "best_price": px,
                                  "entry_time": close_t.isoformat(), "stop": stop, "target": tgt}
                day_trades += 1
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    if day:
        daily[str(day)] = day_pnl

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    print(f"Backtest ({source} data, {len(daily)} days, {c['timeframe']} bars, watchlist {len(c['watchlist'])})")
    print(f"  start ${start_eq:,.2f} -> end ${equity:,.2f}  ({(equity / start_eq - 1) * 100:+.2f}%)")
    print(f"  trades {len(trades)} | win rate {len(wins) / max(len(trades), 1) * 100:.1f}% | "
          f"avg win ${sum(t['pnl'] for t in wins) / max(len(wins), 1):,.2f} | "
          f"avg loss ${sum(t['pnl'] for t in losses) / max(len(losses), 1):,.2f} | max drawdown ${max_dd:,.2f}")
    reasons = {}
    for t in trades:
        reasons[t["reason"].split(" (")[0]] = reasons.get(t["reason"].split(" (")[0], 0) + 1
    print("  exit reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))
    print("  daily P&L: " + "  ".join(f"{k[5:]} {v:+.0f}" for k, v in daily.items()))
    if source == "synthetic":
        print("  NOTE: synthetic random-walk data only proves the logic runs. Use --source alpaca for real history.")


# --------------------------------------------------------------- commands
def cmd_check():
    c = load_criteria()
    print("criteria.yaml OK\n")
    print(yaml.safe_dump(c, sort_keys=False))


def cmd_scan():
    eng = Engine()
    _, now = eng.b.clock()
    bars = eng.b.bars(eng.c["watchlist"], now)
    for sym in eng.c["watchlist"]:
        side, rules, _ = eng.st.evaluate(bars.get(sym) or [])
        print(f"{sym:6} -> {('SIGNAL ' + side.upper()) if side else 'no signal'}")
        for s, rl in rules.items():
            print(f"   {s:5}: " + " | ".join(f"{'✓' if ok else '✗'} {n}{(' ' + d) if d else ''}" for n, ok, d in rl))


def cmd_status():
    eng = Engine()
    is_open, now = eng.b.clock()
    a = eng.b.account()
    s = load_state(now.astimezone(ET).date().isoformat())
    print(f"{now.astimezone(ET):%Y-%m-%d %H:%M} ET | market {'OPEN' if is_open else 'closed'} | mode {eng.c['mode']}")
    print(f"equity ${a['equity']:,.2f} | day P&L ${a['equity'] - a['last_equity']:+,.2f} | "
          f"buying power ${a['buying_power']:,.2f} | day trades (5d) {a['daytrade_count']} | PDT {a['pdt']}")
    print(f"bot trades today {s['trades_today']} | halted {s['halted']} {s['halt_reason']}")
    held = eng.b.positions()
    for sym, p in s["positions"].items():
        h = held.get(sym, {})
        print(f"  {sym} {p['side']} {p['qty']} @ {p['entry_price']:.2f} stop {p['stop']} target {p['target']} "
              f"now {h.get('price', 0):.2f} uPL ${h.get('upl', 0):+.2f}")
    for cl in s["closed"]:
        print(f"  closed {cl}")


def cmd_flatten():
    eng = Engine()
    _, now = eng.b.clock()
    s = load_state(now.astimezone(ET).date().isoformat())
    eng.flatten(s, "manual flatten")
    save_state(s)
    print("Flattened bot positions and halted for today.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["check", "scan", "once", "run", "status", "flatten", "backtest"])
    ap.add_argument("--source", choices=["synthetic", "alpaca"], default="synthetic", help="backtest data")
    a = ap.parse_args()
    load_env()
    if a.command == "check":
        cmd_check()
    elif a.command == "scan":
        cmd_scan()
    elif a.command == "once":
        Engine().cycle()
    elif a.command == "run":
        Engine().run()
    elif a.command == "status":
        cmd_status()
    elif a.command == "flatten":
        cmd_flatten()
    elif a.command == "backtest":
        backtest(a.source)


if __name__ == "__main__":
    sys.exit(main())
