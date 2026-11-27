#!/usr/bin/env python3
"""
Day Trader desktop app: a local web dashboard connected to the market through Alpaca.

    python app.py            then open http://localhost:8000
    python app.py --port 9000 --open

It shows live quotes, a chart with the strategy's indicators, the entry rules each symbol
passes or fails, open positions, and the trade log. You can edit criteria.yaml in a form,
run backtests, and Start, Stop or Flatten the bot. With no API keys yet, "Demo market"
streams a simulated market so you can try everything first.

The server listens only on 127.0.0.1 and rejects cross-site requests, because it can place orders.
"""
import argparse
import io
import json
import os
import random
import re
import threading
import time
import traceback
import webbrowser
from collections import deque
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import yaml

import bot
from bot import (CRITERIA, ET, HERE, TF_MIN, CriteriaError, Engine, Strategy, ema,
                 load_criteria, load_env, load_state, save_state, synthetic_bars)

STATIC = HERE / "static"
DEFAULT_STATE, DEFAULT_JOURNAL = bot.STATE, bot.JOURNAL


# ------------------------------------------------------------ demo market
class DemoBroker:
    """A simulated exchange: replays a synthetic market one bar per tick, fills orders, runs brackets."""
    kind, mode = "demo", "demo"

    def __init__(self, c, seed=None):
        self.c, self.tf = c, TF_MIN[c["timeframe"]]
        self.seed = seed if seed is not None else random.randrange(10**6)
        self.days, self.data = 30, {}
        self.per_day = 390 // self.tf
        self.i = self.per_day * 3 + 30 // self.tf  # 10:00 on day 4, so indicators have history
        self.cash = self.last_equity = 25000.0
        self.pos = {}
        self._ensure(c["watchlist"])
        self.day = self._t().astimezone(ET).date()

    def _ensure(self, syms):
        for s in syms:
            if s not in self.data:
                self.data[s] = synthetic_bars([s], self.days, self.c["timeframe"], self.seed + sum(map(ord, s)))[s]

    def _t(self):
        return next(iter(self.data.values()))[self.i]["t"]

    def _px(self, s):
        self._ensure([s])
        return self.data[s][self.i]["c"]

    def advance(self):
        if self.i >= len(next(iter(self.data.values()))) - 1:
            return
        self.i += 1
        for s, p in list(self.pos.items()):
            b, long = self.data[s][self.i], p["side"] == "long"
            if (long and b["l"] <= p["stop"]) or (not long and b["h"] >= p["stop"]):
                self._fill_close(s, p["stop"])
            elif (long and b["h"] >= p["target"]) or (not long and b["l"] <= p["target"]):
                self._fill_close(s, p["target"])
        d = self._t().astimezone(ET).date()
        if d != self.day:
            self.day, self.last_equity = d, self.account()["equity"]

    def clock(self):
        now = self._t() + timedelta(minutes=self.tf)
        return 570 <= bot.minutes_et(self._t()) < 960, now

    def account(self):
        eq = self.cash + sum((1 if p["side"] == "long" else -1) * p["qty"] * self._px(s) for s, p in self.pos.items())
        gross = sum(p["qty"] * self._px(s) for s, p in self.pos.items())
        return {"equity": eq, "last_equity": self.last_equity, "buying_power": max(eq * 4 - gross, 0),
                "pdt": False, "daytrade_count": 0}

    def positions(self):
        out = {}
        for s, p in self.pos.items():
            px = self._px(s)
            upl = (px - p["avg_entry"]) * p["qty"] * (1 if p["side"] == "long" else -1)
            out[s] = {"qty": p["qty"], "side": p["side"], "avg_entry": p["avg_entry"], "price": px, "upl": upl}
        return out

    def bars(self, symbols, now=None, **_):
        self._ensure(symbols)
        n = self.c["lookback_bars"]
        return {s: self.data[s][: self.i + 1][-n:] for s in symbols}

    def asset(self, s):
        return {"symbol": s, "name": "demo stock", "tradable": True, "shortable": True}

    def open_bracket(self, s, side, qty, stop, target):
        px = self._px(s)
        self.cash += -qty * px if side == "long" else qty * px
        self.pos[s] = {"side": side, "qty": qty, "avg_entry": px, "stop": stop, "target": target}
        return {"id": f"demo-{s}-{self.i}"}

    def _fill_close(self, s, px):
        p = self.pos.pop(s)
        self.cash += p["qty"] * px if p["side"] == "long" else -p["qty"] * px

    def close(self, s):
        if s in self.pos:
            self._fill_close(s, self._px(s))


# ----------------------------------------------------------- criteria form
_KEY = re.compile(r"^(\s*)([A-Za-z_]\w*):(.*)$")
_VAL = re.compile(r"^([ \t]*)(.*?)([ \t]+#.*)?$")


def parse_line(line):
    """-> (indent, key, space, value, comment) or None"""
    m = _KEY.match(line)
    if not m:
        return None
    indent, key, rest = m.groups()
    if rest.strip() == "" or rest.strip().startswith("#"):
        sp = rest[: len(rest) - len(rest.lstrip())]
        return indent, key, sp, "", rest.lstrip() and " " + rest.lstrip()
    return (indent, key) + _VAL.match(rest).groups()


def criteria_fields(text):
    """Turn criteria.yaml into form fields, using its comments as help text."""
    data = yaml.safe_load(text)
    fields, section, pending = [], None, []
    for line in text.splitlines():
        st = line.strip()
        if st.startswith("#"):
            t = st.lstrip("#").strip()
            if t and not set(t) <= set("=-"):
                pending.append(t)
            continue
        if not st:
            pending = []
            continue
        m = parse_line(line)
        if not m:
            continue
        indent, key, _, val, com = m
        help_ = " ".join(pending + ([com.strip().lstrip("#").strip()] if com else []))
        pending = []
        if not indent and val == "":
            section = key
            fields.append({"section": key, "header": True, "help": help_})
            continue
        if not indent:
            section = None
        path = f"{section}.{key}" if indent and section else key
        value = data[section][key] if indent and section else data[key]
        typ = "bool" if isinstance(value, bool) else "number" if isinstance(value, (int, float)) else \
            "list" if isinstance(value, list) else "text"
        fields.append({"path": path, "section": section or "general", "key": key, "value": value,
                       "type": typ, "help": help_})
    return fields


def fmt_scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(fmt_scalar(x) for x in v) + "]"
    s = str(v)
    return json.dumps(s) if re.search(r"[:#\[\]{},&*!|>'\"%@`]|^\s|\s$", s) or s == "" else s


def update_yaml_text(text, values):
    """Replace values in place so comments and layout survive form edits."""
    out, section = [], None
    for line in text.splitlines():
        m = parse_line(line)
        if m and not line.strip().startswith("#"):
            indent, key, sp, val, com = m
            if not indent and val == "":
                section = key
            elif not indent:
                section = None
            path = f"{section}.{key}" if indent and section else key
            if path in values and val != "":
                new = fmt_scalar(values[path])
                pad = new.ljust(len(val)) if com else new
                line = f"{indent}{key}:{sp or ' '}{pad}{com or ''}"
        out.append(line)
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------- app
class App:
    def __init__(self):
        self.lock = threading.RLock()
        self.broker = self.engine = self.thread = None
        self.running, self.error = False, ""
        self.log = deque(maxlen=500)
        self.cache = {}
        self.demo_speed = 1.5

    # -- helpers
    def say(self, msg):
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in str(msg).rstrip().splitlines():
            self.log.append(f"{stamp}  {line}")

    def cached(self, key, ttl, fn):
        if self.broker and self.broker.kind == "demo":
            return fn()
        hit = self.cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        val = fn()
        self.cache[key] = (time.time(), val)
        return val

    # -- connection
    def connect(self, kind, key=None, secret=None):
        self.stop()
        with self.lock:
            self.cache.clear()
            c = load_criteria()
            if kind == "demo":
                bot.STATE, bot.JOURNAL = HERE / "demo_state.json", HERE / "demo_journal.jsonl"
                for f in (bot.STATE, bot.JOURNAL):
                    f.unlink(missing_ok=True)
                self.broker = DemoBroker(c)
                self.error = ""
                self.say("Connected to the DEMO market (simulated prices, fake money).")
                return
            new = {"ALPACA_API_KEY": key.strip(), "ALPACA_API_SECRET": secret.strip()} if key and secret else None
            old = {k: os.environ.get(k) for k in ("ALPACA_API_KEY", "ALPACA_API_SECRET")}
            if new:
                os.environ.update(new)  # try them first; only saved to .env if Alpaca accepts them
            bot.STATE, bot.JOURNAL = DEFAULT_STATE, DEFAULT_JOURNAL
            try:
                b = bot.Alpaca(c)
                a = b.account()
                if new:
                    write_env(new)
            except (SystemExit, RuntimeError) as err:
                if new:
                    for k, v in old.items():
                        os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
                self.broker = None
                if isinstance(err, SystemExit):
                    self.error = str(err)
                    raise RuntimeError(self.error)
                self.error = "Alpaca rejected the connection. Check that the keys match the mode " \
                             f"(paper keys for paper). Details: {err}"
                raise RuntimeError(self.error)
            self.broker, self.error = b, ""
            self.say(f"Connected to Alpaca {c['mode'].upper()} account. Equity ${a['equity']:,.2f}.")

    def try_autoconnect(self):
        load_env()
        if os.environ.get("ALPACA_API_KEY"):
            try:
                c = load_criteria()
                if c["mode"] in ("paper", "live"):
                    self.connect("alpaca")
            except Exception as err:
                self.error = str(err)

    # -- bot control
    def start(self):
        with self.lock:
            if self.running:
                return
            if not self.broker:
                raise RuntimeError("Connect to a market first.")
            c = load_criteria()
            if self.broker.kind == "alpaca" and self.broker.mode != c["mode"]:
                self.say(f"Mode changed to {c['mode']}; reconnecting.")
                self.connect("alpaca")
            self.engine = Engine(self.broker)
            self.running = True
            self.thread = threading.Thread(target=self._loop, daemon=True)
            self.thread.start()
            self.say("Bot STARTED.")

    def stop(self):
        if self.running:
            self.running = False
            self.say("Bot STOPPED. Open positions keep their broker stop and target.")

    def flatten(self):
        with self.lock:
            if not self.broker:
                raise RuntimeError("Not connected.")
            eng = self.engine or Engine(self.broker)
            _, now = self.broker.clock()
            eng.now = now
            s = load_state(now.astimezone(ET).date().isoformat())
            buf = io.StringIO()
            with redirect_stdout(buf):
                eng.flatten(s, "manual flatten from app")
            save_state(s)
            self.say(buf.getvalue() or "Nothing to flatten.")
            self.say("Flattened and halted for today.")
        self.stop()

    def _loop(self):
        while self.running:
            t0 = time.time()
            with self.lock:
                buf = io.StringIO()
                try:
                    with redirect_stdout(buf):
                        res = self.engine.cycle()
                except Exception as err:
                    res = "error"
                    buf.write(f"ERROR: {err}\n")
                    bot.journal("error", error=str(err))
                text = buf.getvalue()
                demo = self.broker.kind == "demo"
                if demo:
                    self.broker.advance()
                # keep the log readable: demo after-hours ticks are silent
                if not (demo and res in ("closed", "eod")):
                    self.say(text)
                self.cache.clear()
            if res == "stopped":
                self.running = False
                self.say("STOP file found: bot halted.")
                return
            try:
                wait = self.demo_speed if demo else max(15, load_criteria()["poll_seconds"])
                if demo and res in ("closed", "eod"):
                    wait = 0.05  # fast-forward the demo through the night
            except CriteriaError as err:
                self.say(f"criteria.yaml has a problem; holding: {err}")
                wait = 10
            while self.running and time.time() - t0 < wait:
                time.sleep(0.05)

    # -- views
    def state(self):
        c = load_criteria()
        out = {"connected": bool(self.broker), "kind": self.broker.kind if self.broker else None,
               "mode": c["mode"], "error": self.error, "running": self.running,
               "stop_file": bot.STOPFILE.exists(), "log": list(self.log)[-200:],
               "live_allowed": os.environ.get("DAYTRADER_ALLOW_LIVE") == "yes",
               "has_keys": bool(os.environ.get("ALPACA_API_KEY"))}
        if not self.broker:
            return out
        if self.broker.kind == "alpaca":
            out["mode"] = self.broker.mode

        def fetch():
            with self.lock:
                is_open, now = self.broker.clock()
                a, held = self.broker.account(), self.broker.positions()
                return is_open, now, a, held
        is_open, now, a, held = self.cached("acct", 4, fetch)
        s = load_state(now.astimezone(ET).date().isoformat())
        pos = []
        for sym, p in s["positions"].items():
            h = held.get(sym, {})
            pos.append({"symbol": sym, **p, "price": h.get("price"), "upl": h.get("upl")})
        out.update({
            "market_open": is_open, "now_et": now.astimezone(ET).strftime("%a %b %d  %H:%M ET"),
            "account": {**a, "day_pnl": a["equity"] - a["last_equity"]},
            "bot": {"trades_today": s["trades_today"], "halted": s["halted"], "halt_reason": s["halt_reason"],
                    "positions": pos, "closed": s["closed"]},
            "other_positions": [k for k in held if k not in s["positions"]],
            "journal": [j for j in read_journal(300)
                        if datetime.fromisoformat(j["ts"]).astimezone(ET).date() == now.astimezone(ET).date()][-60:],
            "limits": {k: c["risk"][k] for k in ("max_open_positions", "max_trades_per_day", "daily_loss_limit")},
        })
        return out

    def watchlist(self, action, symbol):
        sym = re.sub(r"[^A-Z0-9.]", "", str(symbol).upper())
        if not sym or len(sym) > 10:
            raise RuntimeError("Enter a ticker symbol like AAPL.")
        wl = list(load_criteria()["watchlist"])
        if action == "add":
            if sym in wl:
                raise RuntimeError(f"{sym} is already on the watchlist.")
            info = None
            if self.broker:
                with self.lock:
                    info = self.broker.asset(sym)
                if not info:
                    raise RuntimeError(f"{sym} isn't a symbol Alpaca knows.")
                if not info["tradable"]:
                    raise RuntimeError(f"{sym} ({info['name']}) isn't tradable on Alpaca.")
            wl.append(sym)
            msg = f"Added {sym}" + (f" ({info['name']})" if info and info.get("name") else "") + " to the watchlist."
        elif action == "remove":
            if sym not in wl:
                raise RuntimeError(f"{sym} isn't on the watchlist.")
            if len(wl) == 1:
                raise RuntimeError("The watchlist needs at least one symbol.")
            wl.remove(sym)
            msg = f"Removed {sym} from the watchlist" + \
                  (" (the bot will still manage its open position)." if sym in load_state(
                      datetime.now(ET).date().isoformat())["positions"] else ".")
        else:
            raise RuntimeError("unknown action")
        text = update_yaml_text(CRITERIA.read_text(), {"watchlist": wl})
        load_criteria(text)
        CRITERIA.write_text(text)
        self.cache.clear()
        self.say(msg)
        return {"ok": True, "message": msg, "watchlist": wl}

    def scan(self):
        if not self.broker:
            raise RuntimeError("Not connected.")
        c = load_criteria()
        st = Strategy(c)

        def fetch():
            with self.lock:
                _, now = self.broker.clock()
                return self.broker.bars(c["watchlist"], now)
        bars = self.cached("scan:" + ",".join(c["watchlist"]), 20, fetch)
        rows = []
        for sym in c["watchlist"]:
            bl = bars.get(sym) or []
            side, rules, ind = st.evaluate(bl)
            last = bl[-1]["c"] if bl else None
            prev = prev_close(bl)
            rows.append({
                "symbol": sym, "price": last, "change_pct": (last / prev - 1) * 100 if last and prev else None,
                "signal": side, "rsi": ind["rsi"] if ind else None,
                "rules": {k: [{"name": n, "ok": ok, "detail": d} for n, ok, d in v] for k, v in rules.items()},
                "spark": [b["c"] for b in bl[-40:]],
            })
        return rows

    def chart(self, sym):
        if not self.broker:
            raise RuntimeError("Not connected.")
        c = load_criteria()

        def fetch():
            with self.lock:
                _, now = self.broker.clock()
                return self.broker.bars([sym], now)[sym]
        bl = self.cached("chart:" + sym, 20, fetch)
        closes = [b["c"] for b in bl]
        vw, day, pv, vol = [], None, 0.0, 0.0
        for b in bl:
            d = b["t"].astimezone(ET).date()
            if d != day:
                day, pv, vol = d, 0.0, 0.0
            pv += (b["h"] + b["l"] + b["c"]) / 3 * b["v"]
            vol += b["v"]
            vw.append(pv / vol if vol else b["c"])
        marks = [j for j in read_journal(500) if j.get("symbol") == sym and j["event"] in ("entry", "exit")]
        return {
            "symbol": sym, "timeframe": c["timeframe"],
            "bars": [{"t": b["t"].astimezone(ET).strftime("%m-%d %H:%M"), "ts": b["t"].isoformat(),
                      "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"], "v": b["v"]} for b in bl],
            "ema_fast": ema(closes, c["entry"]["ema_fast"]), "ema_slow": ema(closes, c["entry"]["ema_slow"]),
            "vwap": vw, "ema_fast_n": c["entry"]["ema_fast"], "ema_slow_n": c["entry"]["ema_slow"],
            "marks": marks,
        }


def prev_close(bl):
    if not bl:
        return None
    today = bl[-1]["t"].astimezone(ET).date()
    for b in reversed(bl):
        if b["t"].astimezone(ET).date() != today:
            return b["c"]
    return bl[0]["o"]


def read_journal(n):
    if not bot.JOURNAL.exists():
        return []
    lines = bot.JOURNAL.read_text().splitlines()[-n:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass
    return out


def write_env(values):
    f = HERE / ".env"
    lines = f.read_text().splitlines() if f.exists() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    keep += [f"{k}={v}" for k, v in values.items()]
    f.write_text("\n".join(keep) + "\n")
    os.chmod(f, 0o600)
    os.environ.update(values)


APP = App()


# -------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "DayTrader/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("localhost", "127.0.0.1")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, {"error": "forbidden host"})
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, APP.state())
            if u.path == "/api/scan":
                return self._send(200, APP.scan())
            if u.path == "/api/chart":
                return self._send(200, APP.chart(q.get("symbol", ["SPY"])[0].upper()))
            if u.path == "/api/criteria":
                text = CRITERIA.read_text()
                return self._send(200, {"text": text, "fields": criteria_fields(text)})
            return self._send(404, {"error": "not found"})
        except Exception as err:
            return self._send(400, {"error": str(err)})

    def do_POST(self):
        self._mutate()

    def do_PUT(self):
        self._mutate()

    def _mutate(self):
        # custom header => browsers must preflight cross-site requests, which we never approve
        if not self._host_ok() or self.headers.get("X-Requested-With") != "daytrader":
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path).path
        try:
            b = self._body()
            if u == "/api/connect":
                APP.connect(b.get("kind", "demo"), b.get("key"), b.get("secret"))
                return self._send(200, {"ok": True})
            if u == "/api/start":
                APP.start()
                return self._send(200, {"ok": True})
            if u == "/api/stop":
                APP.stop()
                return self._send(200, {"ok": True})
            if u == "/api/flatten":
                APP.flatten()
                return self._send(200, {"ok": True})
            if u == "/api/criteria":
                text = b["text"] if "text" in b else update_yaml_text(CRITERIA.read_text(), b.get("values", {}))
                load_criteria(text)  # validate before saving
                CRITERIA.write_text(text)
                APP.cache.clear()
                APP.say("criteria.yaml saved (applies on the next cycle).")
                return self._send(200, {"ok": True, "text": text, "fields": criteria_fields(text)})
            if u == "/api/backtest":
                src = b.get("source", "synthetic")
                return self._send(200, bot.backtest(src, quiet=True))
            if u == "/api/watchlist":
                return self._send(200, APP.watchlist(b.get("action"), b.get("symbol", "")))
            if u == "/api/demo_speed":
                APP.demo_speed = max(0.2, min(10, float(b.get("seconds", 1.5))))
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except CriteriaError as err:
            return self._send(422, {"error": str(err)})
        except Exception as err:
            traceback.print_exc()
            return self._send(400, {"error": str(err)})


def main():
    ap = argparse.ArgumentParser(description="Day Trader dashboard")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the browser")
    a = ap.parse_args()
    load_env()
    APP.try_autoconnect()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    url = f"http://localhost:{a.port}"
    print(f"Day Trader running at {url}  (Ctrl-C to quit)")
    if a.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        APP.stop()
        print("\nBye. Open positions keep their broker stop and target orders.")


if __name__ == "__main__":
    main()
