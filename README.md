# Angel Trading — Automated Options Strategies

A Python-based automated trading system built on **Angel One SmartAPI** for executing options strategies on Indian indices (NIFTY, SENSEX, BANKNIFTY).

## 📋 Table of Contents

- [Features](#features)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Strategies](#strategies)
  - [Day Breakout Strategy](#1-day-breakout-strategy-live)
  - [Three Legs Strategy](#2-three-legs-strategy-live)
  - [Breakout Viewer](#3-breakout-viewer)
- [Backtesting](#backtesting)
- [Tests](#tests)
- [Project Structure](#project-structure)

---

## Features

- **Day Breakout Strategy** — Trades NIFTY/SENSEX based on previous-day range breakouts with 15-minute candle confirmation
- **Three Legs Strategy** — BANKNIFTY multi-leg hedged options strategy
- **Breakout Viewer** — Visual monitoring tool for breakout levels
- **Backtesting** — Historical backtesting with same rules as live trading
- **Risk Management** — Stop-loss, trailing profit, daily loss limits, cooldowns, and broker-side SL orders
- **Gap Analysis** — Overnight gap detection with adaptive entry rules
- **VIX Filters** — India VIX-based entry delays and reversal suppression

---

## Prerequisites

1. **Python 3.8+** installed on your machine
2. **Angel One Trading Account** with SmartAPI enabled
3. **Angel One SmartAPI Credentials**:
   - API Key (from [SmartAPI Dashboard](https://smartapi.angelbroking.com/))
   - Client Code (your Angel One login ID)
   - PIN (your trading password)
   - TOTP Secret Key (QR code value for 2FA)

---

## Installation

### 1. Clone the Repository

```bash
git clone https://github.com/Amara-Balakrishna1/angeltrading.git
cd angeltrading
```

### 2. Create a Virtual Environment

```bash
# Windows
python -m venv .venv
.venv\Scripts\activate

# Linux/Mac
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements_dev.txt
pip install pyotp logzero websocket-client
pip uninstall pycrypto -y        # Remove old pycrypto if present
pip install pycryptodome
```

---

## Configuration

### Create `config.py` in the project root

This file is **git-ignored** (it contains your secrets). Create it manually:

```python
# config.py (in project root — DO NOT commit this file)

api_key = 'YOUR_SMARTAPI_API_KEY'       # From SmartAPI dashboard
username = 'YOUR_CLIENT_CODE'           # Angel One login ID (e.g., 'A12345678')
pwd = 'YOUR_PIN'                        # Your trading PIN
token = 'YOUR_TOTP_SECRET_KEY'          # QR secret for TOTP 2FA

# Set to True if you face SSL certificate errors (e.g., corporate network)
disable_ssl = False
```

#### How to get these credentials:

| Credential | Where to find it |
|---|---|
| `api_key` | [SmartAPI Dashboard](https://smartapi.angelbroking.com/) → Create an App → Copy the API Key |
| `username` | Your Angel One **Client Code** (login ID, starts with a letter followed by numbers) |
| `pwd` | Your **PIN** used to log in to Angel One |
| `token` | When enabling TOTP on Angel One, you get a **QR code** — the secret key behind it goes here. You can also use the manual setup key shown during TOTP setup. |

> ⚠️ **Security**: Never commit `config.py` to git. The `.gitignore` already excludes it.

---

## Strategies

### 1. Day Breakout Strategy (Live)

Trades NIFTY or SENSEX options based on the **previous day's 12:30 PM – 3:15 PM range**. When the current day's 15-minute candle closes beyond that range, it enters a Bull Call Spread or Bear Put Spread (or naked option).

**Run from the project root:**

```bash
# Interactive mode (prompts for instrument & structure)
python live/daybreakout/main.py

# Direct CLI — NIFTY, paper trading (no real orders)
python live/daybreakout/main.py --instrument NIFTY --lots 10 --paper

# SENSEX, live trading, naked options
python live/daybreakout/main.py --instrument SENSEX --lots 5 --structure naked

# Follow breakouts instead of fading them (default is reverse/fade)
python live/daybreakout/main.py --instrument NIFTY --no-reverse

# Manual range levels
python live/daybreakout/main.py --instrument NIFTY --high 23550 --low 23350

# Auto-pick nearest expiry between NIFTY and SENSEX
python live/daybreakout/main.py --combination true
```

#### CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--instrument`, `-i` | prompt | `NIFTY` or `SENSEX` |
| `--structure`, `-s` | prompt | `spread` (2-leg) or `naked` (1-leg) |
| `--lots`, `-l` | `10` | Number of lots |
| `--high` | auto | Manual override for Range High |
| `--low` | auto | Manual override for Range Low |
| `--date` | auto | Previous trading day (`YYYY-MM-DD`) |
| `--paper` | off | Paper trading mode — no real orders |
| `--reverse` / `--no-reverse` | reverse ON | Fade breakouts (default) or follow them |
| `--combination` | `false` | Auto-pick nearest expiry instrument |
| `--tick-confirm` | off | Legacy 5-min LTP hold instead of 15m candle |

> 📖 For full strategy details, see [`live/daybreakout/README.md`](live/daybreakout/README.md)

---

### 2. Three Legs Strategy (Live)

A multi-leg hedged options strategy for **BANKNIFTY** with automatic spread adjustments based on P&L thresholds.

```bash
python live/threelegs/main.py
```

**Key settings** (in `live/threelegs/config.py`):
- Spread 1: ATM ± 1000 points
- Spread 2: ATM ± 2500 points
- Loss threshold for adjustment: Rs. 2,000
- Hard stop-loss: Rs. 10,000
- Entry: 09:30 | Exit: 15:10

---

### 3. Breakout Viewer

Visual monitoring tool for tracking breakout levels in real-time.

```bash
python live/breakoutstrategy/viewer.py
```

---

## Backtesting

Run historical backtests using the **same rules** as the live strategy:

```bash
# NIFTY backtest with spread structure
python backtest/backtest_daybreakout.py --instrument NIFTY

# SENSEX backtest with naked options
python backtest/backtest_daybreakout.py --instrument SENSEX --structure naked
```

**Output**: Trade-by-trade breakdown + CSV at `data/backtest_daybreakout_15m_<INSTRUMENT>_<STRUCTURE>.csv`

**Backtest arguments**: `--instrument/-i`, `--structure/-s`, `--days/-d`, `--lots/-l`, `--reverse`/`--no-reverse`

> ⚠️ Note: `days_back=35` and `lots=10` are currently hardcoded in `main()`. Edit those lines if you need different values.

---

## Tests

```bash
# Day Breakout unit tests
python -m unittest test.test_daybreakout -v

# Thread safety tests
python -m unittest test.test_thread_safety_breakout -v
```

---

## Project Structure

```
angeltrading/
├── config.py                  # ⚠️ YOUR CREDENTIALS (create manually, git-ignored)
├── requirements_dev.txt       # Python dependencies
├── setup.py                   # Package setup
├── SmartApi/                  # Angel One SmartAPI client library
│   ├── smartConnect.py        # REST API client
│   ├── smartWebSocketV2.py    # WebSocket client
│   └── ...
├── live/                      # Live trading strategies
│   ├── daybreakout/           # Day Breakout Strategy
│   │   ├── main.py            # Entry point / launcher
│   │   ├── strategy.py        # Core strategy logic
│   │   ├── executor.py        # Order execution & API calls
│   │   ├── config.py          # Strategy configuration
│   │   ├── signals.py         # Signal detection
│   │   ├── gap_analyzer.py    # Overnight gap analysis
│   │   ├── contract_finder.py # Option contract lookup
│   │   ├── range_calculator.py# Previous day range calculation
│   │   ├── thread_safety.py   # Thread synchronization
│   │   └── README.md          # Detailed strategy documentation
│   ├── threelegs/             # Three Legs Strategy
│   │   ├── main.py            # Entry point
│   │   ├── strategy.py        # Strategy logic
│   │   ├── executor.py        # Order execution
│   │   └── config.py          # Configuration
│   └── breakoutstrategy/      # Breakout Viewer
│       ├── viewer.py          # Visual monitoring
│       └── breakout.py        # Breakout logic
├── backtest/                  # Backtesting tools
│   ├── backtest_daybreakout.py# Day Breakout backtest
│   ├── backtest_utils.py      # Shared utilities
│   └── ...
├── test/                      # Unit tests
├── data/                      # Trade logs, CSVs (git-ignored)
└── example/                   # SmartAPI usage examples
```

---

## ⚡ Quick Start (TL;DR)

```bash
# 1. Clone & setup
git clone https://github.com/Amara-Balakrishna1/angeltrading.git
cd angeltrading
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements_dev.txt

# 2. Create config.py with your Angel One credentials (see Configuration section above)

# 3. Test with paper trading first!
python live/daybreakout/main.py --instrument NIFTY --paper

# 4. When ready, go live
python live/daybreakout/main.py --instrument NIFTY --lots 10
```

---

## ⚠️ Disclaimer

This software is for **educational purposes only**. Trading in the stock market involves substantial risk of loss. The authors are not responsible for any financial losses incurred through the use of this software. Always test with paper trading before using real money.