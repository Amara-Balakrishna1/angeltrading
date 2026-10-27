# Live Day Breakout Strategy

This folder contains the live intraday Day Breakout strategy. The recommended live launcher is `main_dual.py`, which runs two independent strategy modes: **REVERSE** (fade a confirmed breakout) and **NO-REVERSE** (follow it). `main.py` runs one mode and is available when only one strategy instance is wanted.

> **Live orders:** `--paper` is not enabled by default. Starting either launcher without `--paper` can place real Angel One orders. Confirm the account, instrument, quantity, and risk settings before running it.

## Strategy flow

1. Load the previous trading day's 15-minute candles and calculate the range from **12:30 to 15:15**. The range can be supplied manually with `--high` and `--low`.
2. Watch completed 15-minute candles for a close above the range high or below the range low. The normal entry window starts at **09:30** and the last eligible candle starts at **14:45**. A five-second buffer is used after a candle boundary. `--tick-confirm` selects the legacy live-price confirmation path instead.
3. Select the option contract using the current scrip master, instrument strike step and expiry data. The default structure is a two-leg spread; `--structure naked` selects one long option.
4. In dual mode, the two modes share the same range, instrument, API session, and configured base lots, but evaluate direction and track their own trades and daily risk statistics. REVERSE and NO-REVERSE threads start **30 seconds apart** to stagger their initial API requests. Both can later be active; check broker positions and available margin for the combined exposure.

Large opening gaps apply additional no-chase/selectivity rules. VIX checks and delayed signal handling are part of the strategy; exact thresholds are maintained in `config.py` (`GAP_CONFIG` and `VIX_CONFIG`).

## Start the dual live strategy

Run commands from the repository root. Use `--paper` for a no-order run.

```powershell
# Paper run with NIFTY, spread structure
python live/daybreakout/main_dual.py --instrument NIFTY --paper

# Live SENSEX run: 5 base lots per mode
python live/daybreakout/main_dual.py --instrument SENSEX --lots 5

# Auto-select the instrument with the nearest expiry
python live/daybreakout/main_dual.py --combination true --paper

# Naked options, manual range, or tick confirmation
python live/daybreakout/main_dual.py -i NIFTY -s naked --paper
python live/daybreakout/main_dual.py -i NIFTY --high 23550 --low 23350 --paper
python live/daybreakout/main_dual.py -i NIFTY --tick-confirm --paper
```

### Dual launcher arguments

| Argument | Default | Description |
|---|---|---|
| `--instrument`, `-i` | prompt / NIFTY | `NIFTY` or `SENSEX` |
| `--combination [true/false]` | false | Automatically choose the instrument with the nearest expiry; specify `true` to enable |
| `--structure`, `-s` | prompt / spread | `spread` or `naked` |
| `--lots`, `-l` | 10 | Base lots supplied to each mode |
| `--x-factor`, `-x` | `RISK_CONFIG.x_factor` (currently 0.2) | Overrides the configured size and monetary-risk multiplier for this process |
| `--high`, `--low` | calculated | Manually override the previous-day range levels |
| `--date` | today | Trading date / previous-day-range calculation date, `YYYY-MM-DD` |
| `--paper` | off | Do not submit live orders |
| `--tick-confirm` | off | Use the legacy tick/LTP confirmation path instead of completed 15-minute candles |

**Position sizing:** effective lots per mode are `max(1, int(--lots x x_factor))`. At the current `x_factor=0.2`, the default 10 base lots therefore become 2 lots for each mode. The minimum-one-lot rule means small base quantities do not scale below one lot. If both modes enter, total exposure can be twice the per-mode quantity. Monetary risk thresholds are scaled by `x_factor` in the executor; inspect the startup output and `config.py` before a live run.

The single-mode runner has a separate CLI, including `--reverse` / `--no-reverse`:

```powershell
python live/daybreakout/main.py --instrument NIFTY --no-reverse --paper
```

## Orders, fills, and P&L monitoring

- The executor checks order status with the broker and treats a trade as placed only after a fill is confirmed (or the broker position check confirms it). An accepted/submitted but unconfirmed order is not treated as a filled trade for P&L monitoring.
- After a confirmed fill, the position is registered with the consolidated P&L monitor. Existing broker positions can also be recovered on startup for monitoring.
- The monitor checks open positions every **15 minutes** (`RISK_CONFIG.check_interval = 900` seconds) and applies configured loss, profit, trailing, and time-based exit rules. This is a polling interval; it does not guarantee broker execution at the threshold price.
- The current configured base risk values and controls are in `config.py` under `RISK_CONFIG`. The EOD watchdog force-exits at **15:10**, or **14:45 on expiry day**. Review this config before changing size or using the live launcher.
- Dual mode runs two threads against one SmartAPI session. API access is coordinated in `thread_safety.py`; the 30-second startup stagger does not serialize later signal checks or prevent both modes from trading.

## Live viewer

Start the read-only dashboard in a separate terminal:

```powershell
python live/daybreakout/viewer.py
```

Open [http://localhost:8766](http://localhost:8766). It reads the current day's live log and trade/order files only. The dashboard shows the underlying 15-minute candles, range levels, per-trade P&L by time from **09:00 to 15:30**, trade details on hover, mode summaries, and available open-position P&L samples. Open-position P&L is refreshed from log samples written by the monitor; completed trade P&L comes from today's live trade records/log. It does not connect to the broker or place orders.

## Data and logs

| Path | Contents |
|---|---|
| `data/daybreakout-logs/daybreakout-<YYYY-MM-DD>.log` | Daily strategy, order, fill, and P&L monitor log |
| `data/trades/<YYYY-MM-DD>/triggered_trades_<INSTRUMENT>_reverse.csv` | REVERSE mode triggered trades |
| `data/trades/<YYYY-MM-DD>/triggered_trades_<INSTRUMENT>_no-reverse.csv` | NO-REVERSE mode triggered trades |
| `data/trades/<YYYY-MM-DD>/trades_<INSTRUMENT>_reverse.csv` | REVERSE mode completed trades and realized P&L |
| `data/trades/<YYYY-MM-DD>/trades_<INSTRUMENT>_no-reverse.csv` | NO-REVERSE mode completed trades and realized P&L |
| `data/<DD-MM-YYYY>/orders_<INSTRUMENT>.csv` | Dated order events, including confirmed fills |

## Key files

- `main_dual.py` - dual-mode live launcher and 30-second thread stagger.
- `main.py` - single-mode launcher.
- `strategy.py`, `strategy_patched.py` - signal processing, mode selection, and strategy loop.
- `executor.py` - order placement/fill confirmation, position recovery, exits, and consolidated P&L monitoring.
- `config.py` - instrument, range, entry-time, risk, order-polling, gap, and VIX settings.
- `tracking_dual.py` - per-mode trade CSVs and summaries.
- `viewer.py` - local read-only live dashboard.

## Troubleshooting

- **No order / no P&L tracking:** check today's log for an entry signal, order status, and fill confirmation. Monitoring starts only after a confirmed fill (or recovered broker position).
- **Only one mode entered:** the two modes can produce different direction decisions, and each can be prevented from entering by its own signal, risk limit, or broker-position checks. The 30-second stagger only delays startup; it does not force both modes to trade.
- **Viewer shows no trades:** confirm the viewer date matches the machine's India time/date and that today's log/order/trade files are being written under the paths above.
