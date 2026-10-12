# Order Retry & Polling Improvements - Implementation Summary

## Overview
This document summarizes the improvements made to the Day Breakout Strategy for order retry logic, status polling, and manual exit price fetching.

## Configuration (config.py - RISK_CONFIG)

```python
# Order execution settings
"enable_order_retry": True,        # Enable retry logic for rejected orders
"order_retry_max_attempts": 3,     # Maximum retry attempts for rejected orders
"order_retry_initial_delay": 180.0,  # Initial retry delay in seconds (3 minutes)
"enable_order_polling": True,      # Enable order status polling
"order_poll_interval": 15.0,       # Poll interval in seconds (15-20s)
"order_poll_max_time": 120.0,      # Maximum polling time in seconds (2 minutes)
"enable_manual_exit_price_fetch": True,  # Enable historical price fetch for manual exits
```

## 1. Login Retry Logic (executor.py - login_smart_api)

**Function:** `login_smart_api(max_retries=5, retry_delay=2.5)`

**Features:**
- Retries login up to 5 times with exponential backoff
- Initial delay: 2.5 seconds
- Subsequent delays: 5s, 7.5s, 10s, 12.5s
- Detects rate limit/network errors and applies 2x backoff multiplier
- Logs each attempt with detailed error messages
- Follows the same pattern as `_safe_get_candle_data`

**Error Detection:**
- Rate limit: "exceeding access rate", "Too many requests", "Access denied"
- Network: "timeout", "connection", "network"
- Applies double backoff (2x) for rate limit/network errors

## 2. Order Status Polling (executor.py - poll_order_status)

**Function:** `poll_order_status(smart_api, order_id, symbol, symbol_token, exchange, poll_interval=15.0, max_poll_time=120.0, trade_id, symbol_base)`

**Features:**
- Polls order status every 15-20 seconds (configurable)
- Continues polling until filled/rejected or timeout (default 120 seconds)
- Records all status transitions to orders.csv
- Returns filled quantity and average price
- Logs each poll attempt

**Status Handling:**
- `COMPLETE`, `FILLED`, `EXECUTED` → Returns filled data
- `REJECTED`, `CANCELLED`, `ERROR` → Returns error details
- `PENDING`, `OPEN`, `TRIGGER PENDING`, `ORDER PLACED` → Continues polling
- Timeout → Returns `POLL_TIMEOUT` status

## 3. Order Retry Logic (executor.py - retry_order_with_backoff)

**Function:** `retry_order_with_backoff(smart_api, trading_symbol, symbol_token, transaction_type, quantity, exchange, order_type, product_type, max_retries=3, initial_delay=180.0, order_type_label, trade_id, symbol_base)`

**Features:**
- Retries rejected orders with exponential backoff
- Initial delay: 3 minutes (180 seconds)
- Subsequent delays: 6min, 9min (exponential)
- Max retries: 3 attempts
- Detects transient vs permanent errors
- Skips retry for validation errors (permanent)
- Records all retry attempts

**Transient Error Detection:**
- Rate limit: "exceeding access rate", "Too many requests"
- Network: "timeout", "connection", "network", "temporary"

**Permanent Errors (No Retry):**
- Validation errors
- Price validation failures
- Insufficient margin
- Invalid symbol/token

## 4. Manual Exit Price Fetching (executor.py - fetch_manual_exit_price)

**Function:** `fetch_manual_exit_price(smart_api, symbol, symbol_token, exchange, exit_time, trade_id, symbol_base)`

**Features:**
- Fetches historical 5-minute candle data when manual exit is detected
- Uses `_safe_get_candle_data` with 5 retries (2.5s delay)
- Calculates actual P&L based on fetched historical price
- Records the fetched price to orders.csv
- Fallback to 0 P&L if price fetch fails

**Process:**
1. Detects manual exit (positions not found for 2 consecutive checks)
2. Estimates exit time (midpoint of 2 checks)
3. Fetches 5-minute candle data for that time
4. Uses close price for P&L calculation
5. Records to orders.csv

## 5. Integration Points

### Entry Orders (execute_daybreakout_spread)

**Spread Orders (execute_spread_atomically):**
- BUY leg: `place_order_with_validation` with retry & polling
- SELL leg: `place_order_with_validation` with retry & polling
- Rollback: `place_order_with_validation` with retry & polling

**Naked Orders:**
- BUY leg: `place_order_with_validation` with retry & polling

### Exit Orders (square_off)

**Regular Square-off:**
- SELL leg (for spread): `place_order_with_validation` with retry & polling
- BUY leg: `place_order_with_validation` with retry & polling

**EOD Square-off (square_off_all):**
- All positions: `place_order_with_validation` with retry & polling

## 6. Order Lifecycle Tracking

All order events are recorded to `orders.csv` with the following fields:
- date, time, order_type, symbol, symbol_token, transaction_type
- quantity, status, order_id, execution_price
- error_message, error_code, trade_id, retry_count, duration_ms, symbol_base

**Status Transitions:**
1. PENDING → SUBMITTED → FILLED/REJECTED
2. FILLED → (optional) FILLED_POLL (after polling)
3. REJECTED → (optional) RETRY_ATTEMPT_1/2/3 → FILLED/REJECTED
4. MANUAL_EXIT → PRICE_FETCHED

## 7. Configuration Settings

All settings are in `config.py` under `RISK_CONFIG`:

```python
RISK_CONFIG = {
    # Order execution settings
    "enable_order_retry": True,              # Enable/disable retry logic
    "order_retry_max_attempts": 3,           # Max retry attempts
    "order_retry_initial_delay": 180.0,      # Initial delay (3 min)
    "enable_order_polling": True,            # Enable/disable polling
    "order_poll_interval": 15.0,             # Poll interval (15s)
    "order_poll_max_time": 120.0,            # Max poll time (2 min)
    "enable_manual_exit_price_fetch": True,  # Enable manual exit price fetch
}
```

## 8. Testing & Verification

### Manual Testing Steps:
1. Run strategy in paper trading mode to test login retry
2. Simulate rate limit by making rapid API calls
3. Verify retry logs in daybreakout-logs/
4. Check orders.csv for complete order lifecycle
5. Verify manual exit price fetching works correctly

### Log Verification:
- Login attempts: Look for "SmartAPI login attempt X/5 failed"
- Order polling: Look for "[Poll #X] Order {order_id} still {status}"
- Retry attempts: Look for "[Retry X/3] Transient error, will retry in Xs"
- Manual exit price: Look for "Fetched manual exit price for {symbol}"

## 9. Benefits

1. **Reliability**: Automatic retry for transient failures
2. **Transparency**: Complete order lifecycle tracking in orders.csv
3. **Accuracy**: Historical price fetching for manual exits
4. **Configurability**: All settings in config.py
5. **Consistency**: Same retry pattern across all API calls (login, candle data, orders)
6. **Safety**: No retry for permanent errors (validation, margin, etc.)

## 10. Files Modified

1. `executor.py`:
   - Added retry logic to `login_smart_api()`
   - Added `poll_order_status()` function
   - Added `retry_order_with_backoff()` function
   - Added `fetch_manual_exit_price()` function
   - Updated `place_order_with_validation()` to call retry & polling
   - Updated `execute_spread_atomically()` to pass retry & polling flags
   - Updated `square_off()` to use retry & polling
   - Updated `square_off_all()` to use retry & polling
   - Fixed strategy.py variable reference error

2. `config.py`:
   - Added order retry configuration settings
   - Added order polling configuration settings
   - Added manual exit price fetch setting

3. `strategy.py`:
   - Fixed variable reference error (current_time → now_dt)

## 11. Future Enhancements

Potential improvements for future:
- Add exponential backoff configuration to config.py
- Add retry for position fetching (get_open_positions)
- Add webhook alerts for critical failures
- Add dashboard for monitoring retry statistics
- Add automatic retry for position verification failures
