"""Trend Trading Journal - a local TradeZella-style journal for Bybit / Binance USDT perpetuals.

Run:  python app.py            -> real data (.env with READ-ONLY API keys, and/or CSV import)
      python app.py --demo     -> fake data in demo.db, no keys needed

Everything stays on your machine: trades in journal.db, screenshots in uploads/.
"""
import bisect
import csv
import hashlib
import hmac
import io
import json
import math
import os
import random
import re
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(HERE, ".env"))

DEMO = "--demo" in sys.argv
DB_PATH = os.path.join(HERE, "demo.db" if DEMO else "journal.db")
UPLOAD_DIR = os.path.join(HERE, "demo_uploads" if DEMO else "uploads")
os.makedirs(UPLOAD_DIR, exist_ok=True)

BINANCE_BASE = "https://fapi.binance.com"
BYBIT_BASE = os.getenv("BYBIT_BASE", "https://api.bybit.com")
BINANCE_KEY = os.getenv("BINANCE_API_KEY", "")
BINANCE_SECRET = os.getenv("BINANCE_API_SECRET", "")
BYBIT_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET = os.getenv("BYBIT_API_SECRET", "")
EXTRA_SYMBOLS = [s.strip().upper() for s in os.getenv("SYMBOLS", "").split(",") if s.strip()]
PRICE_SOURCE = os.getenv("PRICE_SOURCE", "bybit" if (BYBIT_KEY or not BINANCE_KEY) else "binance").lower()
SYNC_DAYS = int(os.getenv("SYNC_DAYS", "90"))
STABLES = {"USDT", "USDC", "BUSD", "USD"}
DAY = 86_400_000

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024


# ============================================================ config / database
DEFAULT_CONFIG = {
    "starting_balance": 0,       # account balance before your first journaled trade (for risk % and balance columns)
    "risk_rule_pct": 2.0,        # your max risk per trade, % of balance
    "setup_tags": ["Trend line break", "3-touch break", "2-touch break", "4+ touch break", "Break and retest",
                   "Bounce", "Double confirmation", "Key level break", "Multi-week line", "High risk", "Low risk"],
    "mistake_tags": ["Heavy position", "Over-leveraged", "No stop on exchange", "Revenge trade", "Impulsive entry",
                     "FOMO", "Bored", "Held through news", "Exited early (exhaustion)", "Moved stop",
                     "Ignored consolidation gate", "Weekend hold"],
    "exit_reasons": ["Take-profit", "Stop-loss", "Opposing line break", "Safety line crossed",
                     "Support/resistance hit", "Manual", "Liquidated", "Weekend / time"],
    "timeframes": ["15m", "1h", "4h", "1D"],
    "playbooks": [
        {"id": "pb-3touch", "name": "3-touch trend line break",
         "entry": ["Trend line has 3+ touch points", "Line has at least a week of data",
                   "Stop sits beyond the opposing / safety line (low risk)", "Stop order placed on the exchange",
                   "Risk within my % rule"],
         "market": ["Trending market (not consolidating)", "Higher-timeframe line agrees"],
         "exit": ["Opposing or safety line crossed", "Support / resistance level hit"]},
        {"id": "pb-retest", "name": "Break and retest",
         "entry": ["3+ touches on the line before the break", "Break = 15m candle close beyond the line",
                   "Retest holds (line flips to support / resistance)", "Stop order placed on the exchange",
                   "Risk within my % rule"],
         "market": ["Trending, or double confirmation if consolidating (96 candles inside 1.5%)"],
         "exit": ["Take-profit at nearest multi-touch S/R", "Safety line crossed"]},
        {"id": "pb-bounce", "name": "Bounce on the line",
         "entry": ["Line has 2+ touches", "Entry on the touch, stop beyond the line",
                   "Stop order placed on the exchange", "Risk within my % rule"],
         "market": ["Trending market"],
         "exit": ["Opposing line broken", "Take-profit at S/R"]},
    ],
}

# journal column -> kind: s=text, i=int, f=float
JOURNAL_FIELDS = {
    "setup": "s", "mistakes": "s", "rating": "i", "notes": "s",
    "source": "s", "timeframe": "s", "grade": "s", "grade_note": "s", "touches": "i", "line_age": "f",
    "tolerance": "i", "htf": "s", "market": "s", "double_conf": "i",
    "stop_price": "f", "planned_tp": "f", "atr": "f", "leverage": "f", "margin": "f", "balance_before": "f",
    "exit_reason": "s", "rules_followed": "s", "rule_broken": "s", "stop_moves": "s",
    "playbook": "s", "checklist": "s", "title": "s", "overview": "s", "entry_notes": "s", "size_notes": "s",
    "exit_notes": "s", "lesson": "s", "missed": "s", "links": "s", "images": "s",
}
SQL_TYPES = {"s": "TEXT", "i": "INTEGER", "f": "REAL"}


def table_cols(con, table):
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS fills(
            symbol TEXT, id TEXT, time INTEGER, side TEXT, price REAL, qty REAL,
            realized_pnl REAL DEFAULT 0, commission REAL, commission_asset TEXT, order_id TEXT,
            closed_size REAL DEFAULT 0, exchange TEXT DEFAULT 'binance',
            PRIMARY KEY(symbol, id));
        CREATE TABLE IF NOT EXISTS funding(
            symbol TEXT, time INTEGER, amount REAL, asset TEXT,
            PRIMARY KEY(symbol, time, asset));
        CREATE TABLE IF NOT EXISTS journal(trade_id TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS excursions(trade_id TEXT PRIMARY KEY, mae REAL, mfe REAL, ok INTEGER);
        CREATE TABLE IF NOT EXISTS candles(trade_id TEXT PRIMARY KEY, interval_ms INTEGER, data TEXT);
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS plans(
            id INTEGER PRIMARY KEY AUTOINCREMENT, week TEXT, symbol TEXT, bias TEXT, levels TEXT,
            worst TEXT, likely TEXT, best TEXT, outcome TEXT);
        """
    )
    have = table_cols(con, "journal")
    for f, kind in JOURNAL_FIELDS.items():
        if f not in have:
            con.execute(f"ALTER TABLE journal ADD COLUMN {f} {SQL_TYPES[kind]}")
    fc = table_cols(con, "fills")
    for col, ddl in (("closed_size", "REAL DEFAULT 0"), ("exchange", "TEXT DEFAULT 'binance'")):
        if col not in fc:
            con.execute(f"ALTER TABLE fills ADD COLUMN {col} {ddl}")
    return con


def get_config(con):
    row = con.execute("SELECT v FROM settings WHERE k='config'").fetchone()
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if row:
        try:
            cfg.update(json.loads(row["v"]))
        except ValueError:
            pass
    return cfg


def save_config(con, cfg):
    con.execute("INSERT INTO settings(k,v) VALUES('config',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (json.dumps(cfg),))
    con.commit()


def clean_list(v, cap=60):
    out, seen = [], set()
    for x in v or []:
        x = re.sub(r"\s+", " ", str(x).replace(",", " ")).strip()[:cap]
        if x and x.lower() not in seen:
            seen.add(x.lower())
            out.append(x)
    return out


# ============================================================ exchanges
def binance_get(path, params):
    if not (BINANCE_KEY and BINANCE_SECRET):
        raise RuntimeError("Missing BINANCE_API_KEY / BINANCE_API_SECRET in .env")
    params = dict(params, timestamp=int(time.time() * 1000), recvWindow=10_000)
    qs = urlencode(params)
    sig = hmac.new(BINANCE_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    r = requests.get(f"{BINANCE_BASE}{path}?{qs}&signature={sig}", headers={"X-MBX-APIKEY": BINANCE_KEY}, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Binance {r.status_code}: {r.text[:200]}")
    return r.json()


def bybit_get(path, params):
    ts, rw = str(int(time.time() * 1000)), "10000"
    qs = urlencode(params)
    sign = hmac.new(BYBIT_SECRET.encode(), (ts + BYBIT_KEY + rw + qs).encode(), hashlib.sha256).hexdigest()
    r = requests.get(f"{BYBIT_BASE}{path}?{qs}", timeout=20, headers={
        "X-BAPI-API-KEY": BYBIT_KEY, "X-BAPI-TIMESTAMP": ts, "X-BAPI-SIGN": sign, "X-BAPI-RECV-WINDOW": rw})
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Bybit {r.status_code}: {r.text[:200]}")
    if r.status_code != 200 or j.get("retCode") != 0:
        raise RuntimeError(f"Bybit {r.status_code}: {j.get('retMsg', r.text[:200])}")
    return j["result"]


def sync_binance(con, now):
    last = con.execute("SELECT MAX(time) FROM fills WHERE exchange='binance'").fetchone()[0]
    since = (last - DAY) if last else now - SYNC_DAYS * DAY
    symbols, new = set(EXTRA_SYMBOLS), 0
    start = max(since, now - 89 * DAY)  # income history is only kept ~3 months
    while True:
        rows = binance_get("/fapi/v1/income", {"startTime": start, "limit": 1000})
        for r in rows:
            if r.get("symbol"):
                symbols.add(r["symbol"])
            if r["incomeType"] == "FUNDING_FEE":
                con.execute("INSERT OR IGNORE INTO funding VALUES(?,?,?,?)",
                            (r["symbol"], r["time"], float(r["income"]), r["asset"]))
        if len(rows) < 1000:
            break
        start = rows[-1]["time"] + 1
    for sym in sorted(symbols):
        t = since
        while t < now:
            end = min(t + 7 * DAY - 1, now)
            rows = binance_get("/fapi/v1/userTrades", {"symbol": sym, "startTime": t, "endTime": end, "limit": 1000})
            for r in rows:
                cur = con.execute(
                    "INSERT OR IGNORE INTO fills(symbol,id,time,side,price,qty,realized_pnl,commission,"
                    "commission_asset,order_id,closed_size,exchange) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sym, str(r["id"]), r["time"], r["side"], float(r["price"]), float(r["qty"]),
                     float(r["realizedPnl"]), float(r["commission"]), r["commissionAsset"], str(r["orderId"]), 0,
                     "binance"))
                new += cur.rowcount
            t = rows[-1]["time"] + 1 if len(rows) == 1000 else end + 1
    return new


def sync_bybit(con, now):
    last = con.execute("SELECT MAX(time) FROM fills WHERE exchange='bybit'").fetchone()[0]
    since = (last - DAY) if last else now - SYNC_DAYS * DAY
    new, warnings = 0, []
    t = since
    while t < now:  # executions: max 7-day window, 100 per page
        end = min(t + 7 * DAY - 1, now)
        cursor = None
        while True:
            p = {"category": "linear", "settleCoin": "USDT", "startTime": t, "endTime": end, "limit": 100}
            if cursor:
                p["cursor"] = cursor
            res = bybit_get("/v5/execution/list", p)
            rows = res.get("list") or []
            for r in rows:
                if r.get("execType") not in (None, "", "Trade", "AdlTrade", "BustTrade"):
                    continue
                cur = con.execute(
                    "INSERT OR IGNORE INTO fills(symbol,id,time,side,price,qty,realized_pnl,commission,"
                    "commission_asset,order_id,closed_size,exchange) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["symbol"], r["execId"], int(r["execTime"]), r["side"].upper(), float(r["execPrice"]),
                     float(r["execQty"]), 0, float(r.get("execFee") or 0), r.get("feeCurrency") or "USDT",
                     r.get("orderId", ""), float(r.get("closedSize") or 0), "bybit"))
                new += cur.rowcount
            cursor = res.get("nextPageCursor")
            if not cursor or not rows:
                break
        t = end + 1
    try:  # funding payments (unified accounts): positive = received
        t = since
        while t < now:
            end = min(t + 7 * DAY - 1, now)
            cursor = None
            while True:
                p = {"accountType": "UNIFIED", "category": "linear", "type": "SETTLEMENT",
                     "startTime": t, "endTime": end, "limit": 50}
                if cursor:
                    p["cursor"] = cursor
                res = bybit_get("/v5/account/transaction-log", p)
                rows = res.get("list") or []
                for r in rows:
                    if r.get("symbol") and r.get("funding") not in (None, ""):
                        con.execute("INSERT OR IGNORE INTO funding VALUES(?,?,?,?)",
                                    (r["symbol"], int(r["transactionTime"]), float(r["funding"]),
                                     r.get("currency") or "USDT"))
                cursor = res.get("nextPageCursor")
                if not cursor or not rows:
                    break
            t = end + 1
    except Exception as e:  # funding is a nice-to-have; don't fail the whole sync
        warnings.append(f"Funding not synced: {e}")
    return new, warnings


def reconcile_manual(con):
    """A trade imported/added by hand and later synced from the exchange is the same trade:
    keep the exchange version and carry the journal notes over to it."""
    manual = build_trades(con, only="manual")
    if not manual:
        return 0
    real = build_trades(con, exclude="manual")
    merged = 0
    for m in manual:
        match = next((t for t in real if t["symbol"] == m["symbol"] and t["side"] == m["side"]
                      and abs(t["entry_time"] - m["entry_time"]) <= 5 * 60_000), None)
        if not match:
            continue
        con.execute("DELETE FROM fills WHERE exchange='manual' AND symbol=? AND time>=? AND time<=?",
                    (m["symbol"], m["entry_time"], m["exit_time"] or 9e15))
        old, new = m["id"], match["id"]
        con.execute("INSERT OR IGNORE INTO journal(trade_id) VALUES(?)", (new,))
        o = con.execute("SELECT * FROM journal WHERE trade_id=?", (old,)).fetchone()
        n = con.execute("SELECT * FROM journal WHERE trade_id=?", (new,)).fetchone()
        if o:
            for f in JOURNAL_FIELDS:  # keep anything already written on the exchange trade, fill the gaps
                if n[f] in (None, "") and o[f] not in (None, ""):
                    con.execute(f"UPDATE journal SET {f}=? WHERE trade_id=?", (o[f], new))
            if old != new:
                con.execute("DELETE FROM journal WHERE trade_id=?", (old,))
        con.execute("DELETE FROM excursions WHERE trade_id IN (?,?)", (old, new))
        con.execute("DELETE FROM candles WHERE trade_id IN (?,?)", (old, new))
        merged += 1
    return merged


def sync():
    con = db()
    now = int(time.time() * 1000)
    out, warnings, new = [], [], 0
    if BYBIT_KEY and BYBIT_SECRET:
        n, w = sync_bybit(con, now)
        new += n
        warnings += w
        out.append("bybit")
    if BINANCE_KEY and BINANCE_SECRET:
        new += sync_binance(con, now)
        out.append("binance")
    if not out:
        raise RuntimeError("No API keys found. Add BYBIT_API_KEY/SECRET or BINANCE_API_KEY/SECRET to .env "
                           "(read-only keys), or import a CSV instead.")
    merged = reconcile_manual(con)
    con.commit()
    return {"exchanges": out, "new_fills": new, "warnings": warnings, "merged_manual": merged}


# ---- running P&L (max adverse / favourable excursion) from public candles
INTERVALS = [("1", "1m", 60_000), ("5", "5m", 300_000), ("15", "15m", 900_000), ("60", "1h", 3_600_000),
             ("240", "4h", 14_400_000)]


def kline_bars(symbol, start, end):
    """Public candles covering the trade: (interval_ms, [[bar_start, high, low, close], ...]) oldest first."""
    dur = max(end - start, 60_000)
    code, bcode, ms = next((i for i in INTERVALS if dur / i[2] <= 900), INTERVALS[-1])
    s = start - (start % ms)  # include the candle the trade was opened in
    if PRICE_SOURCE == "bybit":
        r = requests.get(f"{BYBIT_BASE}/v5/market/kline", timeout=20, params={
            "category": "linear", "symbol": symbol, "interval": code, "start": s, "end": end, "limit": 1000})
        j = r.json()
        if j.get("retCode") != 0:
            raise RuntimeError(j.get("retMsg", "kline error"))
        raw = j["result"]["list"]
    else:
        r = requests.get(f"{BINANCE_BASE}/fapi/v1/klines", timeout=20, params={
            "symbol": symbol, "interval": bcode, "startTime": s, "endTime": end, "limit": 1000})
        if r.status_code != 200:
            raise RuntimeError(r.text[:120])
        raw = r.json()
    bars = sorted([int(x[0]), float(x[2]), float(x[3]), float(x[4])] for x in raw)
    bars = [b for b in bars if b[0] + ms > start and b[0] <= end]
    if not bars:
        raise RuntimeError("no candles")
    return ms, bars


def candle_ids(con):
    return {r["trade_id"] for r in con.execute("SELECT trade_id FROM candles")} | \
           {r["trade_id"] for r in con.execute("SELECT trade_id FROM excursions WHERE ok=0")}


def compute_excursions(limit=25):
    if DEMO:
        return {"done": 0, "pending": 0, "errors": []}
    con = db()
    trades = build_trades(con)
    have = candle_ids(con)
    todo = [t for t in trades if t["status"] == "closed" and t["id"] not in have]
    done, errors = 0, []
    for t in todo[:limit]:
        try:
            ms, bars = kline_bars(t["symbol"], t["entry_time"], t["exit_time"])
            hi, lo = max(b[1] for b in bars), min(b[2] for b in bars)
            q, e = t["qty"], t["entry_price"]
            if t["side"] == "LONG":
                mfe, mae = (hi - e) * q, (e - lo) * q
            else:
                mfe, mae = (e - lo) * q, (hi - e) * q
            con.execute("INSERT OR REPLACE INTO excursions VALUES(?,?,?,1)", (t["id"], max(0, mae), max(0, mfe)))
            con.execute("INSERT OR REPLACE INTO candles VALUES(?,?,?)", (t["id"], ms, json.dumps(bars)))
            done += 1
        except Exception as ex:
            errors.append(f'{t["symbol"]}: {ex}')
            con.execute("INSERT OR REPLACE INTO excursions VALUES(?,?,?,0)", (t["id"], None, None))
    con.commit()
    return {"done": done, "pending": max(0, len(todo) - limit), "errors": errors[:3]}


# ============================================================ trades
def fee_usd(f):
    return f["commission"] if (f["commission_asset"] or "USDT") in STABLES else 0.0


def build_trades(con, only=None, exclude=None):
    """Group fills into round-trip trades (flat -> position -> flat); P&L from average entry price."""
    by_symbol = defaultdict(list)
    where, args = "", []
    if only:
        where, args = " WHERE exchange = ?", [only]
    elif exclude:
        where, args = " WHERE exchange != ?", [exclude]
    for f in con.execute("SELECT * FROM fills" + where + " ORDER BY symbol, time, id", args):
        by_symbol[f["symbol"]].append(f)
    funding = defaultdict(list)
    for r in con.execute("SELECT symbol, time, amount FROM funding WHERE asset IN ('USDT','USDC','BUSD','USD')"):
        funding[r["symbol"]].append((r["time"], r["amount"]))
    trades = []

    def finish(cur, exit_time, status):
        qty = cur["entry_qty"]
        t = {"symbol": cur["symbol"], "side": cur["side"], "exchange": cur["exchange"],
             "entry_time": cur["entry_time"], "exit_time": exit_time,
             "entry_price": cur["entry_notional"] / qty if qty else 0,
             "exit_price": cur["exit_notional"] / cur["exit_qty"] if cur["exit_qty"] else None,
             "qty": qty, "pnl": cur["pnl"], "fees": cur["fees"], "status": status,
             "partial": cur.get("partial", False)}
        end = exit_time if exit_time else float("inf")
        t["funding"] = sum(a for ts, a in funding[cur["symbol"]] if t["entry_time"] <= ts <= end)
        t["net"] = t["pnl"] - t["fees"] + t["funding"]
        t["hold_sec"] = ((exit_time - t["entry_time"]) / 1000) if exit_time else None
        t["id"] = f'{t["symbol"]}-{t["entry_time"]}'
        trades.append(t)

    skipped = 0
    for sym, fills in by_symbol.items():
        pos, avg, cur = 0.0, 0.0, None
        for f in fills:
            sgn = 1 if f["side"] == "BUY" else -1
            rem, qty = f["qty"], f["qty"]
            fee = fee_usd(f)
            realized = f["realized_pnl"] or 0
            while rem > 1e-12:
                if abs(pos) < 1e-12:
                    if realized != 0:  # Binance closing fill whose opening predates our data: estimate the entry
                        side = "SHORT" if sgn > 0 else "LONG"
                        entry = f["price"] - realized / rem if side == "LONG" else f["price"] + realized / rem
                        finish({"symbol": sym, "side": side, "exchange": f["exchange"], "entry_time": f["time"],
                                "entry_notional": entry * rem, "entry_qty": rem, "exit_notional": f["price"] * rem,
                                "exit_qty": rem, "pnl": realized, "fees": fee * rem / qty, "partial": True},
                               f["time"], "closed")
                        rem = 0
                    elif (f["closed_size"] or 0) > 0 and f["exchange"] == "bybit":
                        skipped += 1  # closes a position opened before the synced window: entry unknown
                        rem = 0
                    else:
                        cur = {"symbol": sym, "side": "LONG" if sgn > 0 else "SHORT", "exchange": f["exchange"],
                               "entry_time": f["time"], "entry_notional": f["price"] * rem, "entry_qty": rem,
                               "exit_notional": 0.0, "exit_qty": 0.0, "pnl": 0.0, "fees": fee * rem / qty}
                        pos, avg, rem = sgn * rem, f["price"], 0
                elif (pos > 0) == (sgn > 0):  # scale in
                    avg = (avg * abs(pos) + f["price"] * rem) / (abs(pos) + rem)
                    cur["entry_notional"] += f["price"] * rem
                    cur["entry_qty"] += rem
                    cur["fees"] += fee * rem / qty
                    pos += sgn * rem
                    rem = 0
                else:  # reduce / close
                    q = min(rem, abs(pos))
                    cur["exit_notional"] += f["price"] * q
                    cur["exit_qty"] += q
                    cur["pnl"] += (f["price"] - avg) * q * (1 if pos > 0 else -1)
                    cur["fees"] += fee * q / qty
                    pos += sgn * q
                    rem -= q
                    if abs(pos) < 1e-12:
                        finish(cur, f["time"], "closed")
                        cur, pos = None, 0.0
        if cur:
            finish(cur, None, "open")
    trades.sort(key=lambda t: t["exit_time"] or t["entry_time"])
    if not only and not exclude:
        build_trades.skipped = skipped
    return trades


def split_tags(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def overlaps_weekend(a, b):
    """Does the trade overlap Fri 20:00 UTC -> Sun 20:00 UTC?"""
    a_dt = datetime.fromtimestamp(a / 1000, tz=timezone.utc)
    b_dt = datetime.fromtimestamp((b or time.time() * 1000) / 1000, tz=timezone.utc)
    d = (a_dt - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    while d <= b_dt:
        if d.weekday() == 4:
            ws = d.replace(hour=20)
            if a_dt < ws + timedelta(hours=48) and b_dt > ws:
                return True
        d += timedelta(days=1)
    return False


def load_json(s, default):
    try:
        v = json.loads(s) if s else default
        return v if isinstance(v, type(default)) else default
    except ValueError:
        return default


def enrich(con, trades, cfg):
    """Join journal fields + running P&L and derive risk / R / plan-adherence numbers."""
    journal = {r["trade_id"]: dict(r) for r in con.execute("SELECT * FROM journal")}
    exc = {r["trade_id"]: (r["mae"], r["mfe"]) for r in con.execute("SELECT * FROM excursions WHERE ok=1")}
    playbooks = {p["id"]: p for p in cfg["playbooks"]}
    start_bal = float(cfg.get("starting_balance") or 0)
    rule = float(cfg.get("risk_rule_pct") or 0)
    closed = sorted((t for t in trades if t["status"] == "closed"), key=lambda t: t["exit_time"])
    exit_times = [t["exit_time"] for t in closed]
    cum = []
    run = 0.0
    for t in closed:
        run += t["net"]
        cum.append(run)

    for t in trades:
        j = journal.get(t["id"], {})
        g = lambda k, d=None: (j.get(k) if j.get(k) not in (None, "") else d)
        t["tags"], t["mistakes"] = split_tags(g("setup")), split_tags(g("mistakes"))
        t["rating"] = g("rating", 0) or 0
        t["source"] = g("source") or "manual"
        for k in ("timeframe", "grade", "grade_note", "htf", "market", "exit_reason", "rule_broken", "stop_moves",
                  "playbook", "title", "overview", "entry_notes", "size_notes", "exit_notes", "lesson", "missed",
                  "links", "notes", "rules_followed"):
            t[k] = g(k, "")
        for k in ("touches", "line_age", "tolerance", "double_conf", "stop_price", "planned_tp", "atr", "leverage",
                  "margin"):
            t[k] = g(k)
        t["images"] = load_json(g("images"), [])
        t["checklist"] = load_json(g("checklist"), [])
        # playbook checklist score
        pb = playbooks.get(t["playbook"])
        t["checklist_score"] = None
        if pb:
            items = pb["entry"] + pb["market"] + pb["exit"]
            if items:
                t["checklist_score"] = round(100 * sum(1 for i in items if i in t["checklist"]) / len(items))
        # balance, risk, R
        i = bisect.bisect_right(exit_times, t["entry_time"])
        bal = g("balance_before")
        t["balance_before_calc"] = (start_bal + (cum[i - 1] if i else 0.0)) if start_bal > 0 else None
        t["balance_override"] = bal
        t["balance_before"] = bal if bal is not None else t["balance_before_calc"]
        t["balance_after"] = (t["balance_before"] + t["net"]) if (t["balance_before"] is not None
                                                                  and t["status"] == "closed") else None
        dist = abs(t["entry_price"] - t["stop_price"]) if t["stop_price"] else None
        t["stop_dist"] = dist
        t["stop_atr"] = (dist / t["atr"]) if (dist and t["atr"]) else None
        t["risk_usd"] = dist * t["qty"] if dist else None
        t["risk_pct"] = (100 * t["risk_usd"] / t["balance_before"]) if (t["risk_usd"] and t["balance_before"]) else None
        t["r_multiple"] = (t["net"] / t["risk_usd"]) if (t["risk_usd"] and t["status"] == "closed") else None
        t["risk_class"] = None
        if t["risk_pct"] is not None and rule > 0:
            t["risk_class"] = "Low" if t["risk_pct"] <= rule else ("Medium" if t["risk_pct"] <= 3 * rule else "High")
        t["weekend"] = overlaps_weekend(t["entry_time"], t["exit_time"])
        mae, mfe = exc.get(t["id"], (None, None))
        t["mae"], t["mfe"] = mae, mfe
        t["journaled"] = bool(t["tags"] or t["rating"] or t["title"] or t["notes"] or t["overview"] or t["lesson"])
    return trades


# ============================================================ analytics
def mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def summ(rows):
    n = len(rows)
    nets = [t["net"] for t in rows]
    wins = sum(1 for x in nets if x > 0)
    rs = [t["r_multiple"] for t in rows]
    return {"trades": n, "wins": wins, "net": round(sum(nets), 2), "win_rate": round(100 * wins / n, 1) if n else 0,
            "expectancy": round(sum(nets) / n, 2) if n else 0,
            "avg_r": round(mean(rs), 2) if mean(rs) is not None else None}


def group_by(rows, keyfn):
    g = defaultdict(list)
    for t in rows:
        ks = keyfn(t)
        for k in (ks if isinstance(ks, list) else [ks]):
            g[k].append(t)
    out = [{"name": str(k), **summ(v)} for k, v in g.items()]
    return sorted(out, key=lambda r: -r["net"])


def bucket_checklist(t):
    s = t["checklist_score"]
    if s is None:
        return "(no playbook)"
    return "100% - all boxes" if s >= 100 else ("60-99%" if s >= 60 else "under 60%")


def bucket_hold(t):
    h = t["hold_sec"] or 0
    return ("under 1h" if h < 3600 else "1-4h" if h < 14400 else "4-24h" if h < 86400
            else "1-3 days" if h < 259200 else "3+ days")


def yes_no(v, unset="(not set)"):
    return unset if v in (None, "") else ("Yes" if v in (1, "1", "yes", True) else "No")


def make_dims(tz):
    local = lambda ms: datetime.fromtimestamp((ms - tz * 60000) / 1000, tz=timezone.utc)
    return [
        ("setup", "Setup tag", lambda t: t["tags"] or ["(untagged)"]),
        ("mistake", "Mistake tag", lambda t: t["mistakes"] or ["(none)"]),
        ("playbook", "Playbook", lambda t: t["playbook"] and next((p["name"] for p in CFG_NOW["playbooks"]
                                                                  if p["id"] == t["playbook"]), t["playbook"])
         or "(no playbook)"),
        ("checklist", "Checklist score", bucket_checklist),
        ("market", "Market condition", lambda t: t["market"] or "(not set)"),
        ("double_conf", "Double confirmation", lambda t: yes_no(t["double_conf"])),
        ("grade", "Setup grade", lambda t: t["grade"] or "(not set)"),
        ("rating", "Execution rating", lambda t: f'{t["rating"]} star' if t["rating"] else "(unrated)"),
        ("rules", "Rules followed", lambda t: {"yes": "Yes", "no": "No"}.get(t["rules_followed"], "(not set)")),
        ("source", "Source", lambda t: t["source"]),
        ("exit_reason", "Exit reason", lambda t: t["exit_reason"] or "(not set)"),
        ("risk_class", "Risk class", lambda t: t["risk_class"] or "(no stop recorded)"),
        ("touches", "Touches before break", lambda t: "(not set)" if t["touches"] is None else
         ("4+" if t["touches"] >= 4 else str(t["touches"]))),
        ("weekend", "Held over weekend", lambda t: "Yes" if t["weekend"] else "No"),
        ("hold", "Hold time", bucket_hold),
        ("weekday", "Weekday of entry", lambda t: local(t["entry_time"]).strftime("%a")),
        ("hour", "Hour of entry", lambda t: f"{(local(t['entry_time']).hour // 4) * 4:02d}-"
                                           f"{(local(t['entry_time']).hour // 4) * 4 + 3:02d}h"),
        ("symbol", "Symbol", lambda t: t["symbol"]),
        ("side", "Side", lambda t: t["side"]),
        ("timeframe", "Timeframe", lambda t: t["timeframe"] or "(not set)"),
    ]


CFG_NOW = DEFAULT_CONFIG  # replaced per request


def stats(closed, tz):
    n = len(closed)
    if not n:
        return {"trades": 0}
    asc = sorted(closed, key=lambda t: t["exit_time"])
    nets = [t["net"] for t in asc]
    wins = [x for x in nets if x > 0]
    losses = [x for x in nets if x < 0]
    gw, gl = sum(wins), -sum(losses)
    cum, peak, max_dd = 0.0, 0.0, 0.0
    equity = [[asc[0]["entry_time"], 0.0, "", "", 0.0]]
    daily = defaultdict(float)
    for t in asc:
        cum += t["net"]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        equity.append([t["exit_time"], round(cum, 2), t["symbol"], t["side"], round(t["net"], 2)])
        day = datetime.fromtimestamp((t["exit_time"] - tz * 60000) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        daily[day] += t["net"]
    holds = [t["hold_sec"] for t in asc if t["hold_sec"] is not None]
    rs = [t["r_multiple"] for t in asc if t["r_multiple"] is not None]
    win_rows = [t for t in asc if t["net"] > 0]
    capture = [min(1.0, max(0.0, t["pnl"] / t["mfe"])) for t in win_rows if t["mfe"]]
    left = [max(0.0, t["mfe"] - t["pnl"]) for t in win_rows if t["mfe"] is not None]
    answered = [t for t in asc if t["rules_followed"] in ("yes", "no")]
    dims = make_dims(tz)
    groups = {k: group_by(asc, fn) for k, _, fn in dims}
    overall = summ(asc)
    return {
        "trades": n, "net": round(sum(nets), 2), "gross": round(sum(t["pnl"] for t in asc), 2),
        "fees": round(sum(t["fees"] for t in asc), 2), "funding": round(sum(t["funding"] for t in asc), 2),
        "win_rate": overall["win_rate"], "profit_factor": round(gw / gl, 2) if gl else None,
        "avg_win": round(gw / len(wins), 2) if wins else 0, "avg_loss": round(-gl / len(losses), 2) if losses else 0,
        "expectancy": overall["expectancy"], "avg_r": round(mean(rs), 2) if rs else None, "r_count": len(rs),
        "best": round(max(nets), 2), "worst": round(min(nets), 2), "max_drawdown": round(max_dd, 2),
        "avg_hold_sec": round(sum(holds) / len(holds)) if holds else None,
        "avg_mae_win": round(mean([t["mae"] for t in win_rows]), 2) if mean([t["mae"] for t in win_rows]) is not None else None,
        "capture": round(100 * mean(capture)) if capture else None,
        "left_on_table": round(mean(left), 2) if left else None,
        "adherence": round(100 * sum(1 for t in answered if t["rules_followed"] == "yes") / len(answered))
        if answered else None, "adherence_n": len(answered),
        "journaled": sum(1 for t in asc if t["journaled"]),
        "avg_risk_pct": round(mean([t["risk_pct"] for t in asc]), 1) if mean([t["risk_pct"] for t in asc]) is not None else None,
        "equity": equity, "daily": {k: round(v, 2) for k, v in sorted(daily.items())},
        "groups": groups, "dims": [{"key": k, "label": lbl} for k, lbl, _ in dims],
        "insights": make_insights(groups, dims, overall),
    }


def make_insights(groups, dims, overall):
    labels = {k: lbl for k, lbl, _ in dims}
    cands = []
    for key, rows in groups.items():
        if key in ("exit_reason", "rating"):
            continue  # these describe the outcome / are judged after the fact, so they would only echo win vs loss
        for r in rows:
            need = 8 if key in ("symbol", "weekday", "hour") else 3  # time/symbol slices are noisy
            if r["name"].startswith("(") or r["trades"] < need or r["trades"] >= overall["trades"]:
                continue
            delta = r["expectancy"] - overall["expectancy"]
            cands.append({"dim": key, "label": labels[key], **r, "overall_expectancy": overall["expectancy"],
                          "delta": round(delta, 2), "score": delta * math.sqrt(r["trades"]),
                          "low_sample": r["trades"] < 10})
    good = sorted([c for c in cands if c["delta"] > 0], key=lambda c: -c["score"])[:4]
    bad = sorted([c for c in cands if c["delta"] < 0], key=lambda c: c["score"])[:4]
    return {"good": good, "bad": bad}


# ============================================================ journal save
def coerce(field, kind, v):
    if v is None or v == "":
        return None if kind != "s" else ""
    if kind == "i":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    if kind == "f":
        try:
            return float(str(v).replace(",", ""))
        except (TypeError, ValueError):
            return None
    if field in ("setup", "mistakes"):
        return ", ".join(clean_list(v if isinstance(v, list) else str(v).split(",")))[:600]
    if isinstance(v, (list, dict)):
        return json.dumps(v)
    if field == "rules_followed":
        return v if v in ("yes", "no") else ""
    return str(v)[:8000]


def save_journal(con, trade_id, data):
    con.execute("INSERT OR IGNORE INTO journal(trade_id) VALUES(?)", (trade_id,))
    sets, vals = [], []
    for f, kind in JOURNAL_FIELDS.items():
        if f in data:
            sets.append(f"{f}=?")
            vals.append(coerce(f, kind, data[f]))
    if sets:
        con.execute(f"UPDATE journal SET {', '.join(sets)} WHERE trade_id=?", vals + [trade_id])


def learn_tags(con, cfg, data):
    """Tags typed in the journal become part of the pickable lists."""
    changed = False
    for field, key in (("setup", "setup_tags"), ("mistakes", "mistake_tags"), ("exit_reason", "exit_reasons"),
                       ("timeframe", "timeframes")):
        if field not in data:
            continue
        vals = data[field] if isinstance(data[field], list) else str(data[field] or "").split(",")
        for v in clean_list(vals):
            if v.lower() not in {x.lower() for x in cfg[key]}:
                cfg[key].append(v)
                changed = True
    if changed:
        save_config(con, cfg)


# ============================================================ manual trades + CSV
def parse_time(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if re.fullmatch(r"\d{12,13}", s):
        return int(s)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int((dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp() * 1000)
    except ValueError:
        return None


def num(s):
    if s is None:
        return None
    s = re.sub(r"[,$%\s]", "", str(s))
    try:
        return float(s)
    except ValueError:
        return None


def clean_symbol(s):
    s = re.sub(r"\b(perp|perpetual|swap)\b", "", str(s or ""), flags=re.I)
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def add_manual_fills(con, symbol, side, entry_time, entry_price, qty, exit_time=None, exit_price=None, fees=0.0):
    buy, sell = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
    legs = [(buy, entry_time, entry_price)]
    if exit_time and exit_price:
        legs.append((sell, exit_time, exit_price))
    per = (fees or 0) / len(legs)
    for sd, ts, px in legs:
        con.execute("INSERT INTO fills(symbol,id,time,side,price,qty,realized_pnl,commission,commission_asset,"
                    "order_id,closed_size,exchange) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (symbol, "m-" + uuid.uuid4().hex[:12], ts, sd, px, qty, 0, per, "USDT", "", 0, "manual"))
    return f"{symbol}-{entry_time}"


HEADER_MAP = [
    (r"^trade( no)?$", None), (r"^entry time", "entry_time"), (r"^exit time", "exit_time"),
    (r"^(instrument|symbol|pair|ticker)", "symbol"), (r"^timeframe", "timeframe"), (r"^source", "source"),
    (r"^(direction|side)", "side"), (r"^setup", "setup"), (r"^grade", "grade"), (r"^touches", "touches"),
    (r"^line age", "line_age"), (r"^relied on", "tolerance"), (r"^higher timeframe", "htf"),
    (r"^trending or", "market"), (r"^double confirmation", "double_conf"), (r"^entry price", "entry_price"),
    (r"^stop price", "stop_price"), (r"^exit price", "exit_price"), (r"^planned take profit", "planned_tp"),
    (r"^atr", "atr"), (r"^leverage", "leverage"), (r"^margin", "margin"), (r"^balance before", "balance_before"),
    (r"^fees", "fees"), (r"^net p", "net"), (r"^(qty|quantity|size|contracts)", "qty"),
    (r"^exit reason", "exit_reason"), (r"^rules followed", "rules_followed"), (r"^rule broken", "rule_broken"),
    (r"^stop moves", "stop_moves"), (r"^(entry|exit) screenshot", "link"), (r"^(links|screenshots)", "link"),
    (r"^notes", "notes"), (r"^what i would do", "lesson"), (r"^mistakes?", "mistakes"), (r"^rating", "rating"),
    (r"^playbook", "playbook"), (r"^title", "title"),
]


def map_header(h):
    n = re.sub(r"[^a-z0-9]+", " ", h.lower()).strip()
    for rx, field in HEADER_MAP:
        if re.search(rx, n):
            return field
    return None


def yn(v):
    s = str(v or "").strip().lower()
    return 1 if s.startswith(("y", "1", "true")) else (0 if s.startswith(("n", "0", "false")) else None)


def import_csv(con, text, cfg):
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        raise ValueError("The file is empty")
    fields = [map_header(h) for h in rows[0]]
    existing = build_trades(con)
    created = attached = 0
    errors = []
    for ln, raw in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in raw):
            continue
        r = {}
        links = []
        for f, cell in zip(fields, raw):
            cell = cell.strip()
            if not f or not cell:
                continue
            if f == "link":
                links.append(cell)
            else:
                r[f] = cell
        sym, entry_time = clean_symbol(r.get("symbol")), parse_time(r.get("entry_time"))
        side = {"l": "LONG", "b": "LONG", "s": "SHORT"}.get(str(r.get("side", "")).strip().lower()[:1])
        entry_price, exit_price = num(r.get("entry_price")), num(r.get("exit_price"))
        exit_time = parse_time(r.get("exit_time"))
        if not (sym and entry_time and side and entry_price):
            errors.append(f"row {ln}: needs instrument, direction, entry time and entry price")
            continue
        match = next((t for t in existing if t["symbol"] == sym and t["side"] == side
                      and abs(t["entry_time"] - entry_time) <= 5 * 60_000), None)
        if match:
            trade_id, attached = match["id"], attached + 1  # already synced from the exchange: just add journal data
        else:
            qty, fees, net = num(r.get("qty")), num(r.get("fees")) or 0.0, num(r.get("net"))
            if not qty and net is not None and exit_price and exit_price != entry_price:
                qty = abs((net + fees) / (exit_price - entry_price))  # back out size from net P&L + fees
            if not qty:
                errors.append(f"row {ln}: couldn't work out the position size - add a Qty column")
                continue
            if exit_time and not exit_price or exit_price and not exit_time:
                exit_time = exit_price = None  # incomplete exit -> import as open
            trade_id = add_manual_fills(con, sym, side, entry_time, entry_price, round(qty, 8), exit_time,
                                        exit_price, fees)
            created += 1
        j = {}
        for k in ("timeframe", "htf", "exit_reason", "rule_broken", "stop_moves", "notes", "lesson", "title",
                  "playbook"):
            if r.get(k):
                j[k] = r[k]
        if r.get("source"):
            j["source"] = r["source"].strip().lower()[:12]
        if r.get("setup"):
            j["setup"] = r["setup"]
        if r.get("mistakes"):
            j["mistakes"] = r["mistakes"]
        if r.get("grade"):
            m = re.match(r"^\s*([A-Fa-f][+-]?)(?![A-Za-z])", r["grade"])
            j["grade"] = m.group(1).upper() if m else ""
            j["grade_note"] = r["grade"][:200] if (not m or len(r["grade"].strip()) > len(m.group(1))) else ""
        for k in ("touches", "line_age", "stop_price", "planned_tp", "atr", "leverage", "margin", "balance_before",
                  "rating"):
            if num(r.get(k)) is not None:
                j[k] = num(r[k])
        for k in ("tolerance", "double_conf"):
            if yn(r.get(k)) is not None:
                j[k] = yn(r[k])
        if yn(r.get("rules_followed")) is not None:
            j["rules_followed"] = "yes" if yn(r["rules_followed"]) else "no"
        if r.get("market"):
            m = r["market"].lower()
            j["market"] = "Trending" if m.startswith("trend") else "Consolidating" if m.startswith("consol") \
                else "Ranging" if m.startswith("rang") else ""
        if links:
            j["links"] = "\n".join(links)
        save_journal(con, trade_id, j)
        learn_tags(con, cfg, j)
    con.commit()
    return {"created": created, "attached": attached, "errors": errors}


EXPORT_COLS = [
    ("Trade #", lambda t, i: i), ("Entry time (UTC)", lambda t, i: ts_str(t["entry_time"])),
    ("Exit time (UTC)", lambda t, i: ts_str(t["exit_time"])), ("Instrument", lambda t, i: t["symbol"]),
    ("Timeframe", lambda t, i: t["timeframe"]), ("Source (manual/bot/paper)", lambda t, i: t["source"]),
    ("Direction", lambda t, i: t["side"].title()), ("Setup", lambda t, i: ", ".join(t["tags"])),
    ("Grade", lambda t, i: t["grade"]), ("Touches before break", lambda t, i: t["touches"]),
    ("Line age (closed 15m candles)", lambda t, i: t["line_age"]),
    ("Relied on 0.15 ATR tolerance?", lambda t, i: yes_no(t["tolerance"], "")),
    ("Higher-timeframe context", lambda t, i: t["htf"]), ("Trending or consolidating", lambda t, i: t["market"]),
    ("Double confirmation?", lambda t, i: yes_no(t["double_conf"], "")),
    ("Entry price", lambda t, i: r6(t["entry_price"])), ("Stop price", lambda t, i: t["stop_price"]),
    ("Exit price", lambda t, i: r6(t["exit_price"])), ("Planned take-profit", lambda t, i: t["planned_tp"]),
    ("ATR(15m) at entry", lambda t, i: t["atr"]), ("Stop distance (pts)", lambda t, i: r6(t["stop_dist"])),
    ("Stop in ATR", lambda t, i: r6(t["stop_atr"])), ("Risk class", lambda t, i: t["risk_class"]),
    ("Leverage", lambda t, i: t["leverage"]), ("Margin (USD)", lambda t, i: t["margin"]),
    ("Balance before (USD)", lambda t, i: r6(t["balance_before"])),
    ("Balance after (USD)", lambda t, i: r6(t["balance_after"])),
    ("Fees + funding (USD)", lambda t, i: r6(t["fees"] - t["funding"])), ("Net P&L (USD)", lambda t, i: r6(t["net"])),
    ("Risk % of capital at stop", lambda t, i: r6(t["risk_pct"])), ("R multiple", lambda t, i: r6(t["r_multiple"])),
    ("Exit reason", lambda t, i: t["exit_reason"]), ("Weekend flag", lambda t, i: "Yes" if t["weekend"] else "No"),
    ("Rules followed?", lambda t, i: {"yes": "Yes", "no": "No"}.get(t["rules_followed"], "")),
    ("Rule broken", lambda t, i: t["rule_broken"]), ("Stop moves", lambda t, i: t["stop_moves"]),
    ("Links", lambda t, i: t["links"].replace("\n", " ")), ("Notes", lambda t, i: t["notes"]),
    ("What I would do differently", lambda t, i: t["lesson"]), ("Mistakes", lambda t, i: ", ".join(t["mistakes"])),
    ("Rating", lambda t, i: t["rating"] or ""), ("Playbook", lambda t, i: t["playbook"]),
    ("Checklist score %", lambda t, i: t["checklist_score"]), ("Qty", lambda t, i: t["qty"]),
    ("Max adverse excursion (USD)", lambda t, i: r6(t["mae"])), ("Max favourable excursion (USD)", lambda t, i: r6(t["mfe"])),
    ("Title", lambda t, i: t["title"]), ("Overview", lambda t, i: t["overview"]),
]


def ts_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ms else ""


def r6(x):
    return "" if x is None else round(x, 6)


# ============================================================ demo data
def seed_demo():
    con = db()
    if con.execute("SELECT COUNT(*) FROM fills").fetchone()[0]:
        return
    rnd = random.Random(11)
    cfg = get_config(con)
    cfg["starting_balance"], cfg["risk_rule_pct"] = 1000, 2.0
    save_config(con, cfg)
    base = {"BTCUSDT": 81000, "ETHUSDT": 3900, "SOLUSDT": 190, "XRPUSDT": 2.4}
    pbs = {p["name"]: p for p in cfg["playbooks"]}
    t = int(time.time() * 1000) - 55 * DAY
    last_exit, bal = defaultdict(int), 1000.0
    tiers = [  # quality tiers: win probability, tags, risk %, checklist coverage
        ("high", 0.74, [["3-touch break"], ["3-touch break", "Multi-week line"], ["4+ touch break"]], (0.6, 1.4), 1.0),
        ("mid", 0.56, [["2-touch break"], ["Break and retest"], ["Bounce"]], (1.0, 2.2), 0.7),
        ("low", 0.36, [["Trend line break"], ["Trend line break", "High risk"]], (2.5, 5.0), 0.4),
    ]
    fid = 0
    for n in range(64):
        tier = rnd.choices(tiers, weights=[4, 4, 3])[0]
        name, p_win, tag_opts, risk_rng, cover = tier
        sym = rnd.choice(list(base))
        t += rnd.randint(3, 22) * 3_600_000
        entry_t = max(t, last_exit[sym] + 3_600_000)
        hold = rnd.randint(40, 60 * 60) * 60_000
        exit_t = entry_t + hold
        last_exit[sym] = exit_t
        t = entry_t
        side = rnd.choice(["LONG", "SHORT"])
        price = base[sym] * rnd.uniform(0.93, 1.07)
        atr = price * rnd.uniform(0.0025, 0.004)
        stop_atr = rnd.uniform(0.8, 1.6) if name != "low" else rnd.uniform(1.4, 2.4)
        stop_dist = atr * stop_atr
        risk_pct = rnd.uniform(*risk_rng)
        risk_usd = max(5, bal * risk_pct / 100)
        qty = round(risk_usd / stop_dist, 4 if price > 1000 else 2 if price > 10 else 0)
        market = "Trending" if (name == "high" and rnd.random() < .85) or (name == "mid" and rnd.random() < .6) \
            else "Consolidating"
        if market == "Consolidating":
            p_win -= 0.06
        win = rnd.random() < p_win
        r = rnd.uniform(1.0, 3.4) if win else -rnd.uniform(0.9, 1.3)
        move = r * stop_dist
        exit_price = price + move if side == "LONG" else price - move
        stop = price - stop_dist if side == "LONG" else price + stop_dist
        fee = qty * price * 0.00055 + qty * exit_price * 0.00055
        buy, sell = ("BUY", "SELL") if side == "LONG" else ("SELL", "BUY")
        for sd, ts, px in ((buy, entry_t, price), (sell, exit_t, exit_price)):
            fid += 1
            con.execute("INSERT INTO fills(symbol,id,time,side,price,qty,realized_pnl,commission,commission_asset,"
                        "order_id,closed_size,exchange) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                        (sym, f"d{fid}", ts, sd, round(px, 4), qty, 0, fee / 2, "USDT", "", 0, "bybit"))
        net = qty * (exit_price - price) * (1 if side == "LONG" else -1) - fee
        bal += net
        tid = f"{sym}-{entry_t}"
        mistakes = []
        if name == "low":
            mistakes = rnd.sample(["Heavy position", "No stop on exchange", "Impulsive entry", "FOMO",
                                   "Ignored consolidation gate", "Over-leveraged"], rnd.choice([1, 2]))
        elif name == "mid" and rnd.random() < .3:
            mistakes = [rnd.choice(["Exited early (exhaustion)", "Moved stop", "Held through news"])]
        if overlaps_weekend(entry_t, exit_t) and rnd.random() < .5:
            mistakes.append("Weekend hold")
        tags = rnd.choice(tag_opts)
        pb = pbs["3-touch trend line break"] if ("3-touch break" in tags or "4+ touch break" in tags) \
            else pbs["Break and retest"] if "Break and retest" in tags else pbs["Bounce on the line"]
        items = pb["entry"] + pb["market"] + pb["exit"]
        checked = [i for i in items if rnd.random() < cover]
        grade = {"high": rnd.choice(["A+", "A", "A"]), "mid": rnd.choice(["B", "B", "C"]), "low": rnd.choice(["C", "D", "F"])}[name]
        rating = {"high": rnd.choice([4, 5, 5]), "mid": rnd.choice([3, 3, 4]), "low": rnd.choice([1, 2, 2])}[name]
        if win and rating < 5 and rnd.random() < .3:
            rating += 1
        save_journal(con, tid, {
            "setup": tags, "mistakes": mistakes, "rating": rating, "grade": grade, "source": rnd.choice(
                ["manual", "manual", "manual", "bot"]), "timeframe": "15m", "touches": 4 if "4+ touch break" in tags
            else 3 if "3-touch break" in tags else 2, "market": market,
            "double_conf": 1 if (market == "Consolidating" and rnd.random() < .3) else 0,
            "stop_price": round(stop, 4), "atr": round(atr, 4), "leverage": rnd.choice([5, 10, 10, 25]),
            "playbook": pb["id"], "checklist": checked, "exit_reason": rnd.choices(
                ["Take-profit", "Opposing line break", "Manual"] if win else ["Stop-loss", "Opposing line break", "Manual"],
                weights=[7, 2, 1] if win else [6, 2, 2])[0],
            "rules_followed": "yes" if (name == "high" or (name == "mid" and rnd.random() < .7)) else "no",
            "title": "", "notes": ""})
        nb = max(24, min(240, hold // 900_000))  # synthetic candles: a noisy bridge from entry to exit
        step = hold / nb
        sig = stop_dist * 0.55 / math.sqrt(nb)
        w = [0.0]
        for _ in range(nb):
            w.append(w[-1] + rnd.gauss(0, sig))
        closes = [price + (exit_price - price) * i / nb + (w[i] - w[nb] * i / nb) for i in range(1, nb + 1)]
        closes[-1] = exit_price
        bars, prev = [], price
        for i, cl in enumerate(closes):
            hi_ = max(prev, cl) + abs(rnd.gauss(0, sig * 0.5))
            lo_ = min(prev, cl) - abs(rnd.gauss(0, sig * 0.5))
            bars.append([int(entry_t + i * step), round(hi_, 4), round(lo_, 4), round(cl, 4)])
            prev = cl
        hi_all, lo_all = max(b[1] for b in bars), min(b[2] for b in bars)
        if side == "LONG":
            mfe, mae = (hi_all - price) * qty, (price - lo_all) * qty
        else:
            mfe, mae = (price - lo_all) * qty, (hi_all - price) * qty
        con.execute("INSERT INTO excursions VALUES(?,?,?,1)", (tid, max(0, mae), max(0, mfe)))
        con.execute("INSERT INTO candles VALUES(?,?,?)", (tid, int(step), json.dumps(bars)))
    # one open position
    fid += 1
    entry_t = int(time.time() * 1000) - 5 * 3_600_000
    con.execute("INSERT INTO fills(symbol,id,time,side,price,qty,realized_pnl,commission,commission_asset,order_id,"
                "closed_size,exchange) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                ("ETHUSDT", f"d{fid}", entry_t, "BUY", 3900.0, 0.8, 0, 1.7, "USDT", "", 0, "bybit"))
    monday = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    con.execute("INSERT INTO plans(week,symbol,bias,levels,worst,likely,best,outcome) VALUES(?,?,?,?,?,?,?,?)",
                (monday.strftime("%Y-%m-%d"), "BTCUSDT", "Bearish",
                 "Resistance 82,000 (3 touches). Action line from Oct 12 high, safety line under 80,800.",
                 "Break up, immediately retraces below the line: planned loss at the safety line.",
                 "Chop under 82,000 for days: consolidation gate stays on, skip unless double confirmation.",
                 "Clean break below 80,800, retest holds, trail the stop along the new line.", ""))
    con.commit()


# ============================================================ routes
@app.route("/")
def index():
    with open(os.path.join(HERE, "templates", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__DEMO__", "true" if DEMO else "false")
    return Response(html, mimetype="text/html")


@app.route("/api/data")
def api_data():
    global CFG_NOW
    since = int(request.args.get("since", 0))
    symbol, source = request.args.get("symbol", ""), request.args.get("source", "")
    tz = int(request.args.get("tz", 0))
    con = db()
    cfg = CFG_NOW = get_config(con)
    allt = enrich(con, build_trades(con), cfg)
    shown = [t for t in allt if (t["status"] == "open" or (t["exit_time"] or 0) >= since)
             and (not symbol or t["symbol"] == symbol) and (not source or t["source"] == source)]
    closed = [t for t in shown if t["status"] == "closed"]
    have = candle_ids(con)
    pending = sum(1 for t in allt if t["status"] == "closed" and t["id"] not in have)
    return jsonify({
        "trades": list(reversed(shown)), "stats": stats(closed, tz), "config": cfg, "demo": DEMO,
        "symbols": sorted({t["symbol"] for t in allt}), "sources": sorted({t["source"] for t in allt}),
        "keys": {"bybit": bool(BYBIT_KEY and BYBIT_SECRET), "binance": bool(BINANCE_KEY and BINANCE_SECRET)},
        "pending_excursions": 0 if DEMO else pending, "skipped_orphans": getattr(build_trades, "skipped", 0),
        "total_trades": len(allt)})


@app.route("/api/sync", methods=["POST"])
def api_sync():
    try:
        return jsonify(sync())
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/excursions", methods=["POST"])
def api_excursions():
    try:
        return jsonify(compute_excursions())
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/candles/<trade_id>")
def api_candles(trade_id):
    row = db().execute("SELECT interval_ms, data FROM candles WHERE trade_id=?", (trade_id,)).fetchone()
    if not row:
        return jsonify({"interval_ms": 0, "bars": []})
    return jsonify({"interval_ms": row["interval_ms"], "bars": json.loads(row["data"])})


@app.route("/api/journal/<trade_id>", methods=["POST"])
def api_journal(trade_id):
    d = request.get_json(force=True)
    con = db()
    save_journal(con, trade_id, d)
    learn_tags(con, get_config(con), d)
    con.commit()
    return jsonify(ok=True)


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    con = db()
    cfg = get_config(con)
    if request.method == "POST":
        d = request.get_json(force=True)
        cfg["starting_balance"] = num(d.get("starting_balance")) or 0
        cfg["risk_rule_pct"] = num(d.get("risk_rule_pct")) or 0
        for k in ("setup_tags", "mistake_tags", "exit_reasons", "timeframes"):
            if k in d:
                cfg[k] = clean_list(d[k])
        if "playbooks" in d:
            pbs = []
            for p in d["playbooks"]:
                name = str(p.get("name", "")).strip()[:80]
                if name:
                    pbs.append({"id": p.get("id") or "pb-" + uuid.uuid4().hex[:8], "name": name,
                                "entry": clean_list(p.get("entry"), 140), "market": clean_list(p.get("market"), 140),
                                "exit": clean_list(p.get("exit"), 140)})
            cfg["playbooks"] = pbs
        save_config(con, cfg)
    return jsonify(cfg)


@app.route("/api/manual", methods=["POST"])
def api_manual():
    d = request.get_json(force=True)
    sym, side = clean_symbol(d.get("symbol")), str(d.get("side", "")).upper()
    et, xt = parse_time(d.get("entry_time")), parse_time(d.get("exit_time"))
    ep, xp, qty = num(d.get("entry_price")), num(d.get("exit_price")), num(d.get("qty"))
    if not (sym and side in ("LONG", "SHORT") and et and ep and qty and qty > 0):
        return jsonify({"error": "Need symbol, side, entry time, entry price and quantity"}), 400
    if bool(xt) != bool(xp):
        return jsonify({"error": "Give both exit time and exit price, or neither (open trade)"}), 400
    if xt and xt <= et:
        return jsonify({"error": "Exit time must be after entry time"}), 400
    con = db()
    tid = add_manual_fills(con, sym, side, et, ep, qty, xt, xp, num(d.get("fees")) or 0.0)
    con.commit()
    return jsonify({"id": tid})


@app.route("/api/manual/<trade_id>", methods=["DELETE"])
def api_manual_delete(trade_id):
    con = db()
    t = next((x for x in build_trades(con) if x["id"] == trade_id), None)
    if not t or t["exchange"] != "manual":
        return jsonify({"error": "Only manually added / imported trades can be deleted"}), 400
    con.execute("DELETE FROM fills WHERE exchange='manual' AND symbol=? AND time>=? AND time<=?",
                (t["symbol"], t["entry_time"], t["exit_time"] or 9e15))
    con.execute("DELETE FROM journal WHERE trade_id=?", (trade_id,))
    con.execute("DELETE FROM excursions WHERE trade_id=?", (trade_id,))
    con.execute("DELETE FROM candles WHERE trade_id=?", (trade_id,))
    con.commit()
    return jsonify(ok=True)


@app.route("/api/import", methods=["POST"])
def api_import():
    f = request.files.get("file")
    if not f:
        return jsonify({"error": "No file uploaded"}), 400
    try:
        text = f.read().decode("utf-8-sig")
        if "\t" in text.split("\n", 1)[0] and "," not in text.split("\n", 1)[0]:
            text = text.replace("\t", ",")  # pasted from Sheets as TSV
        con = db()
        return jsonify(import_csv(con, text, get_config(con)))
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/export.csv")
def api_export():
    con = db()
    cfg = get_config(con)
    closed_first = enrich(con, build_trades(con), cfg)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([c for c, _ in EXPORT_COLS])
    for i, t in enumerate(closed_first, 1):
        w.writerow(["" if (v := fn(t, i)) is None else v for _, fn in EXPORT_COLS])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=trade-journal.csv"})


ALLOWED_IMG = {"png", "jpg", "jpeg", "gif", "webp"}


@app.route("/api/image/<trade_id>", methods=["POST"])
def api_image(trade_id):
    f = request.files.get("file")
    ext = (f.filename.rsplit(".", 1)[-1].lower() if f and "." in f.filename else "png")
    if not f or ext not in ALLOWED_IMG:
        return jsonify({"error": "Upload a png, jpg, gif or webp image"}), 400
    name = f'{re.sub(r"[^A-Za-z0-9_-]", "_", trade_id)}-{uuid.uuid4().hex[:8]}.{ext}'
    f.save(os.path.join(UPLOAD_DIR, name))
    con = db()
    row = con.execute("SELECT images FROM journal WHERE trade_id=?", (trade_id,)).fetchone()
    imgs = load_json(row["images"] if row else "", [])
    imgs.append({"file": name, "kind": request.form.get("kind", "Analysis")[:20]})
    save_journal(con, trade_id, {"images": imgs})
    con.commit()
    return jsonify({"images": imgs})


@app.route("/api/image/<trade_id>/<name>", methods=["DELETE"])
def api_image_delete(trade_id, name):
    con = db()
    row = con.execute("SELECT images FROM journal WHERE trade_id=?", (trade_id,)).fetchone()
    imgs = [i for i in load_json(row["images"] if row else "", []) if i["file"] != name]
    save_journal(con, trade_id, {"images": imgs})
    con.commit()
    try:
        os.remove(os.path.join(UPLOAD_DIR, os.path.basename(name)))
    except OSError:
        pass
    return jsonify({"images": imgs})


@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_DIR, name)


@app.route("/api/plans", methods=["GET", "POST"])
def api_plans():
    con = db()
    if request.method == "POST":
        d = request.get_json(force=True)
        vals = [str(d.get(k, ""))[:4000] for k in ("week", "symbol", "bias", "levels", "worst", "likely", "best",
                                                   "outcome")]
        if d.get("id"):
            con.execute("UPDATE plans SET week=?,symbol=?,bias=?,levels=?,worst=?,likely=?,best=?,outcome=? "
                        "WHERE id=?", vals + [int(d["id"])])
        else:
            con.execute("INSERT INTO plans(week,symbol,bias,levels,worst,likely,best,outcome) "
                        "VALUES(?,?,?,?,?,?,?,?)", vals)
        con.commit()
    return jsonify([dict(r) for r in con.execute("SELECT * FROM plans ORDER BY week DESC, id DESC")])


@app.route("/api/plans/<int:pid>", methods=["DELETE"])
def api_plan_delete(pid):
    con = db()
    con.execute("DELETE FROM plans WHERE id=?", (pid,))
    con.commit()
    return jsonify(ok=True)


if __name__ == "__main__":
    if DEMO:
        seed_demo()
    else:
        db()
    print(f"Trade journal running on http://127.0.0.1:5055  ({'DEMO data' if DEMO else 'real data'})")
    app.run(host="127.0.0.1", port=5055, debug=False)
