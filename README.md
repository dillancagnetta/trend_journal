# Trend Trading Journal

A local, TradeZella-style journal for Bybit / Binance USDT perpetuals, built around the trend-line
process: playbook checklists, setup and mistake tags, stop / R-multiple, running P&L, a trade-review
template, a weekly plan, and an insights page that shows what is working and what is costing you.
Your data stays on your computer (`journal.db`, screenshots in `uploads/`), or in a Google Drive folder if you choose.

## Run

```bash
pip install -r requirements.txt
python app.py --demo      # try it with fake data first (demo.db)
python app.py             # your real journal
```

Open http://127.0.0.1:5055

| Command | What it does |
|---|---|
| `python app.py` | Your real journal (uses `.env`) |
| `python app.py --demo` | Fake data in `demo.db` / `demo_uploads/`, no keys needed |
| `python app.py --move-data` | One-off: copy `journal.db` and `uploads/` into `DATA_DIR` (see below) |

## Configuration (`.env`)

Copy `.env.example` to `.env`. Everything is optional.

| Setting | Default | Purpose |
|---|---|---|
| `BYBIT_API_KEY` / `BYBIT_API_SECRET` | – | Bybit sync. Use a **read-only** key, IP-restricted if you can |
| `BYBIT_BASE` | `https://api.bybit.com` | Use `https://api.bytick.com` if api.bybit.com is blocked |
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | – | Binance USDT-M sync (read-only key) |
| `SYMBOLS` | – | Binance only: always sync these, e.g. `BTCUSDT,ETHUSDT` |
| `SYNC_DAYS` | `90` | How far back the first sync goes |
| `PRICE_SOURCE` | `bybit` (`binance` if only a Binance key is set) | Where public candles for the running P&L come from (`bybit` / `binance`) |
| `DATA_DIR` | project folder | Where `journal.db`, `uploads/` and `backups/` live (e.g. a Google Drive folder) |

## Where your data lives

| What | Where |
|---|---|
| Trades, journal entries, settings, weekly plans | `journal.db` (SQLite) in `DATA_DIR` |
| Screenshots | `uploads/` in `DATA_DIR`. Files are named `<trade id>-<random>.<ext>`; the database only stores the file names |
| Daily database snapshots | `backups/journal-YYYY-MM-DD.db` in `DATA_DIR`, newest 14 kept |

To back up or move the journal, copy `journal.db` **and** `uploads/` together. To restore a snapshot,
stop the app, copy the backup over `journal.db`, and start it again.

`.env`, `journal.db`, `uploads/` and `backups/` are git-ignored, so pushing this repo never publishes your
keys, trades or screenshots. `demo.db` is committed (fake data only).

## Keep your data in Google Drive (optional)

1. Install [Google Drive for Desktop](https://www.google.com/drive/download/), create a folder such as
   `My Drive\TradeJournal`, right-click it → *Offline access → Available offline*.
2. In `.env` set `DATA_DIR=G:\My Drive\TradeJournal` (use your real drive letter / path).
3. Run `python app.py --move-data` once to copy `journal.db` and `uploads/` there (originals are kept).
4. Run `python app.py` as usual. It prints the data folder it is using and keeps a daily database
   snapshot in `backups/` (last 14 days).

Use it on one computer at a time - SQLite and Drive sync do not merge two copies edited at once.
If Drive is not running, the app refuses to start ("DATA_DIR not found") rather than opening an empty journal.
Once the app looks right from the Drive folder, you can delete the old `journal.db` and `uploads/` in the project folder.

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

**Stop moves** records how you managed the stop during the trade: *None*, *Manual Stop*, *Safety Line*
(trailed along a redrawn, steeper trend line) or *S/R Line*. Older free-text entries are kept and shown as
an extra option. It is included in CSV export/import as the "Stop moves" column.

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
