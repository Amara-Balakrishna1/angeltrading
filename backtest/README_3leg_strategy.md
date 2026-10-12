# 3-Leg Options Strategy Backtest

## Overview

This backtest engine implements a 3-leg options strategy for BANKNIFTY and SENSEX indices using historical 1-minute option OHLC data from Angel One SmartAPI.

## Strategy Details

**Entry:** 9:30 AM  
**Exit:** 3:10 PM  
**Dynamic Exit:** If loss > 2,000, move Spread 2 closer by 500 points towards ATM. If Spread 2 touches/crosses Spread 1, exit position.  
**Hedging:** At 11:00 AM, if position is negative, buy hedge contract (2x multiplier). Main 3 legs only monitored during hours 12, 13, 14.

### Decision Modes (`--side {AUTO,CE,PE}`)

- **`AUTO` (Default):** Evaluates trend at 9:30 AM:
  - If `Spot(09:30) >= Open(09:15)`: Bullish $\rightarrow$ execute **CE Strategy** with **PE Hedge**
  - If `Spot(09:30) < Open(09:15)`: Bearish $\rightarrow$ execute **PE Strategy** with **CE Hedge**
- **`CE` (Bullish):** Always trade CE ratio spread with PE hedge
- **`PE` (Bearish):** Always trade PE ratio spread with CE hedge

### Position Structure

| Leg | CE Strategy (Bullish) | PE Strategy (Bearish) |
| :--- | :--- | :--- |
| **Leg 1 (Long)** | BUY ATM CE | BUY ATM PE |
| **Leg 2 (Spread 1 Short)** | SELL ATM + 1000 CE | SELL ATM - 1000 PE |
| **Leg 3 (Spread 2 Short)** | SELL ATM + 2500 CE | SELL ATM - 2500 PE |
| **Hedge Leg (11:00 if P&L < 0)** | BUY ATM - 1000 PE | BUY ATM + 1000 CE |
| **Spread 2 Adjustment** | Move -500 pts down | Move +500 pts up |
| **Spread Adjustment Exit** | `Spread 2 <= Spread 1` | `Spread 2 >= Spread 1` |

## Features

- **Hourly P&L Monitoring:** Calculates strategy P&L at 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, and 15:10
- **Leg-wise Analysis:** Breaks down P&L by individual option legs
- **Risk Analysis:** Tracks maximum hourly profit/loss, drawdowns, and MTM extremes
- **30-Day Statistics:** Comprehensive performance metrics including win rate, profit factor, consecutive wins/losses
- **Combined Results:** Aggregated analysis for BANKNIFTY + SENSEX
- **Custom Date Support:** Run backtests for specific historical periods
- **Data Quality Checks:** Handles missing data and marks unavailable data appropriately

## Usage

### Basic Usage

Run backtest for both BANKNIFTY and SENSEX for the last 30 trading days:

```bash
python backtest/backtest_3leg_strategy.py
```

### Side / Decision Options

```bash
# Automated decision at 9:30 AM (default):
python backtest/backtest_3leg_strategy.py --side AUTO

# Force CE 3-leg strategy (with PE hedge):
python backtest/backtest_3leg_strategy.py --side CE

# Force PE 3-leg strategy (with CE hedge):
python backtest/backtest_3leg_strategy.py --side PE
```

### Instrument-Specific Backtest

Run backtest for only BANKNIFTY:

```bash
python backtest/backtest_3leg_strategy.py --instrument BANKNIFTY
```

Run backtest for only SENSEX:

```bash
python backtest/backtest_3leg_strategy.py --instrument SENSEX
```

### Custom Number of Days

Run backtest for a specific number of trading days:

```bash
python backtest/backtest_3leg_strategy.py --days 15
```

### Custom Lot Size

Run backtest with a specific number of lots (default: 1 lot):

```bash
# 5 lots for BANKNIFTY (5 × 15 = 75 quantity)
python backtest/backtest_3leg_strategy.py --instrument BANKNIFTY --lots 5

# 10 lots for SENSEX (10 × 20 = 200 quantity)
python backtest/backtest_3leg_strategy.py --instrument SENSEX --lots 10
```

### Custom Date Range

Run backtest ending on a specific date:

```bash
python backtest/backtest_3leg_strategy.py --custom-date 2026-09-15
```

### Combined Options

Run 15-day backtest for BANKNIFTY ending on 2026-09-10:

```bash
python backtest/backtest_3leg_strategy.py --instrument BANKNIFTY --days 15 --custom-date 2026-09-10
```

## Output

### Console Output

The script provides the following tables and summaries:

1. **Daily Results Table:** Shows entry prices and hourly P&L for each trading day
2. **Leg-wise P&L Table:** Breaks down final P&L by individual option legs
3. **Hourly Risk Analysis:** Shows maximum profit/loss, drawdowns, and extreme MTM for each day
4. **30-Day Summary:** Comprehensive statistics including:
   - Total P&L
   - Average/Median Daily P&L
   - Win Rate
   - Maximum Single-Day Profit/Loss
   - Maximum Drawdown
   - Profit Factor
   - Consecutive Winning/Losing Days
5. **Hourly Performance Summary:** Shows average P&L, win rate, and best/worst performance for each hour
6. **Combined Results:** Aggregated metrics when running both instruments

### CSV Output

Detailed results are saved to `data/backtest_3leg_{INSTRUMENT}_{DATE}.csv` with the following columns:

- Date, Instrument, ATM Strike
- Entry prices for all three legs
- Hourly P&L (10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 15:10)
- Final P&L
- Risk metrics (max hourly profit/loss, drawdown, etc.)
- Lot size

## Configuration

### Instrument Settings

Instrument configurations are defined in `INSTRUMENTS_CONFIG`:

- **BANKNIFTY:** Lot size 15, strike step 100, spreads 1000/1500
- **SENSEX:** Lot size 20, strike step 100, spreads 1000/1500

### Strategy Parameters

- **Entry Time:** 9:30 AM
- **Exit Time:** 3:10 PM
- **Monitoring Times:** 10:00, 11:00, 12:00, 13:00, 14:00, 15:00, 15:10
- **Strike Spreads:** +1000 and +1500 points from ATM

## Data Requirements

- Angel One SmartAPI credentials (configured in `config.py`)
- Historical 1-minute OHLC data for spot indices
- Historical 1-minute OHLC data for required option contracts
- Instrument master file for option contract mapping

### Instrument Master Download

The script downloads Angel One's complete instrument master file (~151,599 records) which includes:
- All exchange segments (NSE, BSE, NFO, BFO, MCX, etc.)
- All instrument types (equities, options, futures, commodities, currencies)
- All tradable symbols

**Why so many records?** Angel One provides a single master file containing all tradable instruments across all exchanges. This is unavoidable when using their API.

**Optimization:** The script implements intelligent caching:
- First run: Downloads and caches the full master file (~10-30 seconds)
- Subsequent runs: Uses cached file (instant loading)
- The script automatically filters to only the relevant instruments (e.g., 927 BANKNIFTY options vs 151,599 total records)

**Cache location:** `backtest/cache_3leg/instrument_master.json`

## Data Quality

The script implements strict data quality rules:

- Does not use missing or interpolated option prices
- Marks days with insufficient data as "DATA UNAVAILABLE"
- Uses actual listed strikes available for each expiry
- Verifies all contracts belong to the same expiry
- Uses historical lot sizes applicable on each date
- No future information leakage in strike/expiry selection

## Performance

- **Caching:** Option data is cached locally to avoid repeated API calls
- **Rate Limiting:** Built-in rate limiting for API requests
- **Chunked Data Fetching:** Option data fetched in time chunks to manage API limits

## Error Handling

- API connection failures with retry logic
- Missing option contract handling
- Data validation and quality checks
- Graceful degradation when data is unavailable

## Example Output

```
================================================================================
  3-LEG OPTIONS STRATEGY BACKTEST: BANKNIFTY
================================================================================
  Strategy: BUY ATM CE, SELL ATM+1000 CE, SELL ATM+1500 CE
  Entry: 9:30 AM | Exit: 3:10 PM
  No stop-loss or target
================================================================================

[1/5] Authenticating with Angel One SmartAPI...
      ✓ Connected successfully.
[2/5] Determining trading dates...
      ✓ Selected 30 trading days:
        2026-08-14  --->  2026-09-18
...
```

## Notes

- The script uses the actual option prices from the market, not theoretical pricing models
- All P&L calculations are based on the close price of each hourly candle
- The strategy uses nearest weekly expiry available on each trading day
- No stop-loss or target is applied - pure time-based exit at 3:10 PM
- Results can be reproduced using the saved CSV files