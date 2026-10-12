# Order Tracking Enhancement - Implementation Summary

## Overview
Comprehensive order lifecycle tracking has been added to track all order events including:
- Buy/Exit order placement status (PENDING, SUBMITTED, FILLED, REJECTED, etc.)
- Order IDs and execution prices
- Error messages and error codes
- Order timing (duration in milliseconds)
- Retry counts
- Trade ID linkage

## Changes Made

### 1. Config Updates (`config.py`)
- Added `ORDERS_FILE` path to config for storing order logs
- File: `data/orders.csv` (global) and `data/<DD-MM-YYYY>/orders_<SYMBOL>.csv` (daily)

### 2. New Functions in `executor.py`

#### `record_order_event()`
Logs detailed order lifecycle events to orders.csv
- Parameters:
  - `order_type`: ENTRY_BUY, ENTRY_SELL, EXIT_BUY, EXIT_SELL, EXIT_SELL_ROLLBACK
  - `status`: PENDING, SUBMITTED, FILLED, REJECTED, PARTIALLY_FILLED, CANCELLED, EXCEPTION, VALIDATION_ERROR
  - `order_id`: Broker order ID
  - `execution_price`: Fill price
  - `error_message`: Rejection reason
  - `error_code`: Broker error code
  - `trade_id`: Associated trade ID
  - `retry_count`: Retry attempt number
  - `duration_ms`: Order duration in milliseconds

#### `get_date_orders_file()`
Returns path to daily orders file based on date and symbol

### 3. Enhanced `place_order_with_validation()`
- Added new parameters:
  - `order_type_label`: For logging (ENTRY_BUY, ENTRY_SELL, EXIT_BUY, EXIT_SELL)
  - `trade_id`: Associated trade ID
  - `symbol_base`: Instrument symbol
  - `retry_count`: Retry attempt number
- Now logs all status transitions:
  1. PENDING (before validation)
  2. SUBMITTED (before API call)
  3. FILLED / REJECTED / EXCEPTION (after API response)
- Tracks order duration in milliseconds
- Records error codes and messages for failures

### 4. Updated Function Calls

#### `execute_spread_atomically()`
- Added `trade_id` and `symbol_base` parameters
- Passes them to `place_order_with_validation` for both legs
- Labels orders as ENTRY_BUY, ENTRY_SELL, EXIT_SELL_ROLLBACK

#### `square_off()`
- Added `trade_id` parameter
- Passes `trade_id` to `place_order_with_validation`
- Labels exit orders as EXIT_BUY (for SELL leg) and EXIT_SELL (for BUY leg)

#### `square_off_all()`
- Labels orders as EXIT_SELL or EXIT_BUY based on position
- Uses "EOD_SQUARE_OFF" as trade_id for EOD exits
- Passes `symbol_base` for tracking

#### `execute_daybreakout_spread()`
- Generates `trade_id` before order placement (moved earlier)
- Passes `trade_id` and `symbol_base` to all order functions
- Ensures consistent trade_id across all order events

#### `_consolidated_monitor_worker()`
- Passes `trade_id` to `square_off()` for proper exit order tracking

## Order Status Flow

### Entry Orders
1. **PENDING** → Order initiated, before validation
2. **VALIDATION_ERROR** → Pre-order validation failed (missing symbol, price out of range)
3. **SUBMITTED** → Order sent to broker
4. **FILLED** → Order successfully executed
5. **REJECTED** → Order rejected by broker (with error code and message)
6. **EXCEPTION** → Network/API error during placement

### Exit Orders
Same flow as entry orders, with labels:
- EXIT_BUY: Closing SELL leg (buying back)
- EXIT_SELL: Closing BUY leg (selling)
- EXIT_SELL_ROLLBACK: Rollback when SELL leg fails

## Order Log CSV Structure

```
date,time,order_type,symbol,symbol_token,transaction_type,quantity,status,order_id,execution_price,error_message,error_code,trade_id,retry_count,duration_ms,symbol_base
```

Example:
```
2026-10-08,09:45:09.123,ENTRY_BUY,SENSEX26O0872200CE,12345678,BUY,200,PENDING,,,SENSEX_20261008_094510,0,,SENSEX
2026-10-08,09:45:09.245,ENTRY_BUY,SENSEX26O0872200CE,12345678,BUY,200,SUBMITTED,12345678,,SENSEX_20261008_094510,0,122,SENSEX
2026-10-08,09:45:09.387,ENTRY_BUY,SENSEX26O0872200CE,12345678,BUY,200,FILLED,12345678,195.80,,SENSEX_20261008_094510,0,264,SENSEX
```

## Benefits

1. **Complete Audit Trail**: Every order event is logged with timestamp
2. **Failure Diagnostics**: Error codes and messages for rejected orders
3. **Performance Metrics**: Order duration tracking for latency analysis
4. **Retry Tracking**: Retry count helps identify transient vs permanent failures
5. **Trade Linkage**: All orders linked to trade_id for end-to-end traceability
6. **Real-time Monitoring**: Console emojis (⏳📤✅❌⚠️🚫💥) for quick status identification

## Future Enhancements

1. Add order status polling for PENDING → FILLED transitions
2. Implement retry logic with exponential backoff
3. Add partial fill handling
4. Create order analytics dashboard
5. Add order cancellation tracking
