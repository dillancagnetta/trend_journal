# Trend Trading Journal

A local, TradeZella-style journal for Bybit / Binance USDT perpetuals, built around the trend-line
process: playbook checklists, setup and mistake tags, stop / R-multiple, running P&L, a trade-review
template, a weekly plan, and an insights page that shows what is working and what is costing you.
Your data stays on your computer (`journal.db`, screenshots in `uploads/`).

## Run

```bash
pip install -r requirements.txt
python app.py --demo      # try it with fake data first (demo.db)
python app.py             # your real journal
```

Open http://127.0.0.1:5055

## Get your trades in (pick any)

1. **Import your Google Sheet.** File → Download → Comma-separated values (.csv), then
   *Trades & journal → Import CSV*. The columns of your "Trend Trading Journal" sheet are recognised
   as they are (Entry time, Instrument, Direction, Setup, Grade, Touches, Entry/Stop/Exit price, Net P&L, Notes…).
   Position size is backed out of Net P&L + fees if there is no Qty column. If a trade is already synced
   from the exchange, the sheet row is attached to it instead of creating a duplicate.
2. **Sync from the exchange.** Copy `.env.example` to `.env`, add a *read-only* key, click *Sync*.
   Bybit and Binance are both supported; every fill is grouped into round-trip trades automatically.
3. **Add a trade by hand** (paper trades, other venues).

*Export CSV* writes the same columns back out, so you can keep a copy in Sheets.

## What it tracks (and where)

| TradeZella / Tori | Here |
|---|---|
| Trades, P&L, fees, funding, hold time | Automatic from fills |
| Initial stop, risk %, R multiple | Stop price in the journal; risk, risk class and R are calculated |
| Playbooks with entry / market / exit checklists | *Playbooks & settings*; tick the boxes per trade, score shown as % |
| Setup tags and mistake tags | Chips in the journal (type a new one and it is saved for next time) |
| Star rating | Execution rating (stars) plus a setup grade (A+ to F) |
| Running P&L (how far down / up it went) | Line chart of profit / loss over the life of each trade (with your stop and target levels), plus worst dip and peak, from exchange candles |
| Trade review template | Title, overview, setup details, position size, exit, takeaways, missed opportunity |
| Chart markup screenshots | Upload or paste images per trade, plus links (Drive folders, TradingView) |
| "What do winners / losers have in common" | *Insights*: auto findings plus a breakdown by 19 dimensions |
| Sunday routine (worst / likely / best case) | *Weekly plan* tab |

From your own sheet: source (manual / bot / paper), touches before break, line age, ATR tolerance,
higher-timeframe context, trending vs consolidating, double confirmation, stop in ATR, leverage, margin,
balance before / after, weekend flag (Fri 20:00 → Sun 20:00 UTC, automatic), rules followed, rule broken, stop moves.

Set your **starting balance** and **max risk %** in *Playbooks & settings* so balance columns, risk % and risk class work.

## Changing timeframe (for example 15m to 1h)

- Set **Timeframe** on each trade; *Insights → Timeframe* then compares 15m and 1h results separately, so keep old trades tagged 15m rather than mixing them.
- Edit the playbook wording in *Playbooks & settings* (for example "15m candle close" or "96 candles inside 1.5%") to match your new rules. "Line age" and "ATR at entry" are in whatever timeframe you trade.
- The running P&L chart picks its own candle size from how long the trade lasted, so it does not depend on your trading timeframe.
- Fewer, longer trades means insights need more calendar time to reach a useful sample.

## How trades are built

Fills are grouped per symbol into round trips (flat → position → flat). Scaling in or out stays inside one
trade; a flip splits into two. Gross P&L uses your average entry price. Net = gross − fees + funding.

## Limits

- One-way (not hedge) position mode is assumed.
- Fees paid in a non-stable coin (e.g. BNB) are not converted, so net is slightly overstated if you use that discount.
- Positions opened before the synced window are skipped on Bybit (a note appears); raise `SYNC_DAYS` and sync again.
- Running P&L uses candle highs and lows and your average entry, so it is an estimate.
- Insights are descriptive. Expect small samples to mislead; the page flags groups with fewer than 10 trades.
- The exchange sync was written from the official Bybit / Binance API docs. Check your first sync against the
  exchange's own trade history. CSV import, manual trades and everything else were tested.
