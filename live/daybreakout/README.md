# Day Breakout Strategy (Live)

The **Day Breakout Strategy** marks the **12:30 PM → 3:15 PM** range (High and Low) of the previous trading day from **15-minute candles**, and waits for a **completed 15-minute candle close** beyond that range. It then executes a defined-risk option position (2-leg spread or 1-leg naked) with full risk management.

> **Read this first — default direction is REVERSED.** The launcher runs with `--reverse` **ON by default**, so the strategy *fades* breakouts instead of following them:
> - Close **above** previous-day High → **Bear Put Spread** *(a fade — follow mode would buy a Bull Call Spread)*
> - Close **below** previous-day Low → **Bull Call Spread**
>
> Use `--no-reverse` to follow breakouts (High → Bull Call, Low → Bear Put).

---

## 📈 Strategy Rules

1. **Step 1 — Mark Range**:
   - Time window: **12:30 PM → 3:15 PM** of the previous trading day.
   - Automatically queries **FIFTEEN_MINUTE** historical candles from Angel One SmartAPI (full session 09:15–15:30, then filtered to the window) to record:
     - **Range High** (highest spot high between 12:30 and 15:15)
     - **Range Low** (lowest spot low between 12:30 and 15:15)
   - Steps back up to **7 days** to skip weekends/holidays, unless `--date` is supplied.

2. **Step 2 — Wait for Breakout (15-Minute Candle Close)**:
   - Default live path matches the backtest: act on the **close of a completed 15-minute candle**.
   - **Bullish signal**: 15-minute candle **closes above** previous-day High.
   - **Bearish signal**: 15-minute candle **closes below** previous-day Low.
   - **Recovery** (`RECOVERY` trade type, LARGE / VERY_LARGE gaps only): candle **closes back inside** the range after price traded in gap territory.
   - **Entry Window**: candles starting **09:30 or later** (the 09:15 candle is never traded).
   - **Last entry candle**: **14:45**. Any candle starting after 14:45 is skipped.
   - Candles are read only after a **5-second buffer** past each 15-minute boundary, so partial candles are never evaluated.
   - Legacy mode: `--tick-confirm` switches to a 5-minute live LTP hold instead of candle-close evaluation.

3. **Step 3 — Strikes & Structure**:
   - ATM strike = `round(spot / strike_step) * strike_step`.
   - Expiry = **nearest weekly expiry** from `OpenAPIScripMaster.json` (NIFTY/BANKNIFTY expire **Tuesday**, SENSEX expires **Thursday**, with holiday fallback).
   - Lot size is read **dynamically from the scrip master** — no hardcoded quantity.

   **`--structure spread` (default) — 2 legs:**

   | Executed direction | Structure | BUY leg | SELL leg |
   |---|---|---|---|
   | BULLISH | Bull Call Spread | ATM CE | ATM + width CE |
   | BEARISH | Bear Put Spread | ATM PE | ATM − width PE |

   Spread width: **NIFTY 300 pts** (step 50, lot 25) · **SENSEX 1,000 pts** (step 100, lot 25).

   **`--structure naked` — 1 leg:**
   - BULLISH → Naked **Call** (ATM CE only) · BEARISH → Naked **Put** (ATM PE only).
   - No defined-risk cap from the short leg — risk comes from the stop-loss in Step 4.

4. **Risk Management (`broken_call` rules, `RISK_CONFIG`)**:
   - **Max Loss**: `Rs. -5,000` (auto square-off based on P&L)
   - **Max Profit**: `Rs. 50,000` (target reached exit)
   - **Profit Threshold**: `Rs. 3,000` (activates trailing stop)
   - **Min Profit Step**: `Rs. 2,000` (locks profit in increments of Rs. 2,000)
   - **Broker-side Stop Loss** (optional): Places a STOPLOSS order with broker at entry - NOT tracked in P&L monitoring
     - **Fixed Loss SL**: Fixed monetary loss (e.g., Rs. 9,000) - trigger calculated as entry - (loss/quantity)
     - **Premium-based SL**: Percentage-based (e.g., 30% below entry) - trigger calculated as entry × (1 - pct/100)
   - **P&L check interval**: every **300 s (5 min)** in the consolidated monitor
   - **Daily Trade Limit**: maximum **4** trades per day
   - **Daily Max Loss**: halts trading when daily P&L ≤ `Rs. -10,000`
   - **Daily Max Profit**: halts trading when daily P&L ≥ `Rs. 35,000`
   - **Stop-Loss Cooldown**: after a stop-loss, no new entry for **45 minutes**
   - **Consecutive-Loss Circuit Breaker**: **3** consecutive stop-losses stops trading for the day
   - **Max Position Hold**: force square-off after **120 minutes** in a trade
   - **Exit-Type Cooldowns**: trailing-profit exit **15 min**, max-profit exit **30 min**, manual exit **5 min**; EOD / daily-max-loss / EOD square-off block re-entry for the day

   | Exit reason | Re-entry allowed after |
   |---|---|
   | `EXIT_MAX_LOSS` (P&L-based stop-loss) | 45 min |
   | `EXIT_TRAILING_PROFIT` | 15 min |
   | `EXIT_MAX_PROFIT` | 30 min |
   | `MANUAL_EXIT` | 5 min |
   | `EXIT_DAILY_MAX_LOSS` | No re-entry (day over) |
   | `EXIT_EOD` / `EXIT_EOD_SQUARE_OFF` | No re-entry (day over) |

   **Broker-side Stop Loss:**
   - When enabled, places a STOPLOSS order with the broker at trade entry
   - **Fixed Loss SL** (takes precedence if enabled):
     - Trigger price = entry premium - (fixed_loss_amount / quantity)
     - Example: Entry = 200, lots = 2 (50 qty), fixed_loss = 9000 → trigger = 200 - (9000/50) = 20
     - Ensures fixed monetary loss regardless of premium level
   - **Premium-based SL** (used if fixed loss SL is disabled):
     - Trigger price = entry premium × (1 - premium_stop_loss_pct/100)
     - Example: Entry premium = 100, premium_stop_loss_pct = 30% → SL order placed at trigger = 70
   - The broker executes this automatically when premium drops to trigger price
   - This is **NOT tracked** in P&L monitoring - it's a pure broker-side safety order
   - Cancelled automatically when the monitoring system exits the trade for any reason

5. **Execution Safety**:
   - BUY leg is submitted first for margin efficiency.
   - SELL leg is submitted second (spreads only).
   - If SELL leg fails, the BUY leg is **immediately rolled back** (squared off) to prevent unhedged long exposure.
   - On exit, the SELL leg is squared off first, then the BUY leg.
   - Only **one trade at a time** per instrument — blocked by both local monitoring state and broker open positions.
   - On startup, existing open positions are **recovered** and brought under monitoring.
   - A daemon **EOD watchdog** thread force-squares off at **15:10** (or **14:45** on expiry day) regardless of the other threads' state.

---

## 🌡️ India VIX Filters

Live VIX LTP is compared against the **previous day's VIX close** (`India VIX`, token `99926017`) on every confirmed signal.

### 1. Reversal Suppression

When `--reverse` is active, an extreme VIX move cancels the flip and the original breakout direction is kept:

| Breakout direction | VIX condition | Action |
|---|---|---|
| BEARISH (below Low) | VIX **> +8%** (spike) | Do **not** reverse — stay BEARISH |
| BULLISH (above High) | VIX **≤ −6%** (crush) | Do **not** reverse — stay BULLISH |

If VIX cannot be fetched, the filter is skipped and normal rules apply.

### 2. Non-Blocking Entry Delay

| Breakout direction | VIX band | Action |
|---|---|---|
| BEARISH | **+2% to +8%** | Wait **30 minutes**, then re-verify |
| BULLISH | **−6% to −2%** | Wait **30 minutes**, then re-verify |

The delay is **non-blocking**: the signal is parked, persisted to `data/vix_delay_state.json` (so it survives a restart), and re-checked after the window. If the delay would push entry past the **14:45** last-entry time, it is **skipped** and the trade is taken immediately.

**Additional Features:**

- **Maximum stall time cap**: Absolute maximum of **45 minutes** (configurable via `vix_max_stall_minutes`) to prevent indefinite stalling
- **VIX re-verification**: After delay expires, VIX conditions are re-checked:
  - If VIX still in delay range → delay extended by another cycle
  - If VIX normalized → proceed with price verification
- **Timeout protection**: All VIX LTP fetches have a **5-second timeout** to prevent hanging

### 3. Forced Decision at Maximum Stall Time

When the **45-minute maximum stall time** is reached, the system analyzes the last **3** 15-minute candles (configurable via `vix_candle_check_count`) to make an intelligent direction decision:

|| Original Breakout | Candle Check | Result | Final Direction |
||---|---|---|---|
|| BULLISH (High) | LOW breakdown (next candle low < current candle low) | Trend reversing down | **BEARISH** |
|| BULLISH (High) | No LOW breakdown | Uptrend intact | **BULLISH** |
|| BEARISH (Low) | HIGH breakdown (next candle high > current candle high) | Trend reversing up | **BULLISH** |
|| BEARISH (Low) | No HIGH breakdown | Downtrend intact | **BEARISH** |

**Logic:**
- **BULLISH breakout with VIX -2% to -6%**: Check if recent candles show **LOW breakdown** → switch to BEARISH, else keep BULLISH
- **BEARISH breakout with VIX +2% to +8%**: Check if recent candles show **HIGH breakdown** → switch to BULLISH, else keep BEARISH

This ensures trades are not stalled indefinitely while still using actual price action to determine the optimal direction when forced to decide.

---

## 🌅 Gap & Opening Rules

Before applying the breakout strategy, assess the **overnight gap** size. The gap determines the level of caution and which spread to execute when a directional move confirms.

### Gap Size Classification

| Gap Size | Range | Caution |
|---|---|---|
| ➖ Flat | ≤ ±0.05% | 1 (treated as flat open — no gap direction) |
| 🟢 Small | < 0.30% | 1 |
| 🟡 Medium | 0.30% – 0.60% | 2 |
| 🔥 Large | 0.60% – 1.00% | 3 (+ no-chase, + very selective on >1%) |
| 🔴 Very Large | > 1.00% | 4 (very selective) |

**LARGE and VERY_LARGE gaps** additionally set `no_chase_same_direction = True`: the first 15-minute candle in the same direction as the gap is skipped, and an extra **300 s** wait is applied in tick mode.

---

### 📋 Gap Rules Table

| Gap Situation | Market Behavior | Action |
|---|---|---|
| 🟢 **Small Gap-Up** < 0.30% | Continues UP | Wait for previous-day High breakout (15m close) → execute breakout direction |
| 🟢 **Small Gap-Up** | Reverses DOWN | Wait for previous-day Low breakdown (15m close) → execute breakout direction |
| 🟢 **Small Gap-Up** | Sideways | **NO TRADE** |
| 🟡 **Medium Gap-Up** 0.30–0.60% | Continues UP | **Bull Call Spread**, after confirmation |
| 🟡 **Medium Gap-Up** | Reverses DOWN | **Bear Put Spread**, after confirmation |
| 🟡 **Medium Gap-Up** | Sideways | **NO TRADE** |
| 🔥 **Large Gap-Up** 0.60–1.00% | Continues UP | Don't chase immediately; wait for consolidation/breakout confirmation → **Bull Call Spread** |
| 🔥 **Large Gap-Up** | Starts recovering DOWN | Look for confirmed reversal → **Bear Put Spread** |
| 🔥 **Large Gap-Up** | Sideways | **NO TRADE** |
| 🔴 **Very Large Gap-Up** > 1% | Any direction | **Be very selective; don't chase the opening move.** Wait for clear confirmation |
| 🔻 **Small Gap-Down** < 0.30% | Continues DOWN | Previous-day Low breakdown (15m close) → execute breakout direction |
| 🔻 **Small Gap-Down** | Recovers UP | Confirm recovery → **Bull Call Spread** |
| 🔻 **Small Gap-Down** | Sideways | **NO TRADE** |
| 🔻 **Medium Gap-Down** 0.30–0.60% | Continues DOWN | **Bear Put Spread**, after confirmation |
| 🔻 **Medium Gap-Down** | Recovers UP | Early recovery confirmation → **Bull Call Spread**; add as recovery strengthens |
| 🔻 **Medium Gap-Down** | Sideways | **NO TRADE** |
| 🔥 **Large Gap-Down** 0.60–1.00% | Continues DOWN | Don't chase immediately; wait for confirmation → **Bear Put Spread** |
| 🔥 **Large Gap-Down** | Strong recovery | Confirm recovery → **Bull Call Spread** |
| 🔥 **Large Gap-Down** | Sideways | **NO TRADE** |
| 🔴 **Very Large Gap-Down** > 1% | Any direction | **Be very selective; wait for the market to establish direction** |

### ⚠️ Key Principles

- **Never chase the opening move** on Large or Very Large gaps — wait for the market to settle and direction to confirm.
- **The 15-minute close confirmation still applies** regardless of gap size; gap rules only add an extra layer of caution (no-chase skips the first opening 15m candle in the gap direction).
- **Sideways action = NO TRADE** in all gap scenarios — an indecisive market does not provide a tradeable edge.
- **Very Large gaps (> 1%)** require the highest selectivity; skipping the trade entirely is a valid (and often correct) decision.
- Gap size affects **when** you enter, not **which** structure — the executed direction is always decided by the breakout confirmation plus `--reverse` (see the warning at the top).

---

## 🚀 How to Run

Run all commands from the **repository root** (the package imports are `live.daybreakout.*`).

### 1. Interactive Mode
The launcher prompts for instrument and option structure:
```bash
python live/daybreakout/main.py
```

### 2. Direct CLI Options
```bash
# NIFTY, 10 lots (default), paper trading
python live/daybreakout/main.py --instrument NIFTY --lots 10 --paper

# SENSEX, 5 lots, live money
python live/daybreakout/main.py --instrument SENSEX --lots 5

# Naked Call/Put structure instead of a spread
python live/daybreakout/main.py --instrument NIFTY --structure naked

# Follow breakouts instead of fading them
python live/daybreakout/main.py --instrument NIFTY --no-reverse

# Manual range levels / specific previous trading day
python live/daybreakout/main.py --instrument NIFTY --high 23550 --low 23350
python live/daybreakout/main.py --instrument NIFTY --date 2026-09-10
```

### Command-line Arguments (`main.py`)
| Argument | Default | Description |
|---|---|---|
| `--instrument`, `-i` | prompt | `NIFTY` or `SENSEX` |
| `--structure`, `-s` | prompt | `spread` or `naked` |
| `--lots`, `-l` | `10` | Number of lots (qty = lots × dynamic lot size) |
| `--high` | auto | Manual override for Range High |
| `--low` | auto | Manual override for Range Low |
| `--date` | auto | Previous trading day override (`YYYY-MM-DD`) |
| `--confirmation`, `-c` | `300` | Tick-mode confirmation seconds — ignored unless `--tick-confirm` |
| `--paper` | off | Paper trading (signals logged, no orders placed) |
| `--tick-confirm` | off | Use the legacy 5-minute LTP hold instead of 15m candle close |
| `--reverse` / `--no-reverse` | **reverse ON** | Fade breakouts (default) / follow breakouts |

### 3. Running the 15-Minute Candle Backtest
```bash
python backtest/backtest_daybreakout.py --instrument NIFTY
python backtest/backtest_daybreakout.py --instrument SENSEX --structure naked
```

The backtest shares `signals.py`, `gap_analyzer.py`, and the same `RISK_CONFIG` / `VIX_CONFIG`, so live and backtest use identical rules.

> ⚠️ In `backtest_daybreakout.py`, the `main()` function currently **hardcodes** `days_back=35` and `lots=10`, ignoring the `--days` / `--lots` arguments. Edit those two lines in `main()` if you want different values.

Output: a trade-by-trade breakdown table with performance metrics, plus a detailed CSV at
`data/backtest_daybreakout_15m_<INSTRUMENT>_<STRUCTURE>.csv`.

**Backtest arguments:** `--instrument/-i`, `--structure/-s`, `--days/-d`, `--lots/-l`, `--vix-bearish-thresh`, `--vix-bullish-thresh`, `--first-entry`, `--daily-max-profit`, `--sl-cooldown`, `--reverse` / `--no-reverse` (reverse **ON** by default, matching live).

> **Note:** The broker-side premium stop loss is **not applicable to backtesting** since it's a live order placement feature. Backtesting uses only the P&L-based stop loss.

---

## 🧵 Threads

| Thread | Role |
|---|---|
| Signal monitor (main) | Fetches completed 15m candles and evaluates breakout/recovery signals |
| Consolidated P&L monitor | Loops all active trades every 300 s — stop-loss, trailing profit, target, manual-exit detection |
| EOD watchdog (daemon) | Force `square_off_all()` at 15:10 (14:45 on expiry day), independent of the other threads |

Shared state is guarded by `thread_safety.py` (`smart_api_lock` for the single SmartAPI session, `FlattenCoordinator` so a second EOD flatten for the same symbol/day is skipped).

---

## 📂 Outputs

| Path | Contents |
|---|---|
| `data/daybreakout-logs/daybreakout-<YYYY-MM-DD>.log` | Full run log |
| `data/trades.csv` | All closed trades (global) |
| `data/<DD-MM-YYYY>/trades_<SYMBOL>.csv` | Closed trades per day |
| `data/triggered_trades.csv` + `data/<DD-MM-YYYY>/triggered_trades_<SYMBOL>.csv` | Entry-time trade log |
| `data/vix_delay_state.json` | Persisted VIX delay state (survives restart) |
| `data/backtest_daybreakout_15m_<INSTRUMENT>_<STRUCTURE>.csv` | Backtest trade log |

---

## 🧪 Tests

```bash
python -m unittest test.test_daybreakout -v
python -m unittest test.test_thread_safety_breakout -v
```
