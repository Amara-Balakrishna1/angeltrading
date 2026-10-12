# Breakout & Spread Strategy

An automated intraday breakout detection and 2-leg spread trading system for **SENSEX** and **BANKNIFTY** built on Angel One SmartAPI.

---

## 1. If SENSEX is Selected (Default)

When SENSEX is chosen (or Enter is pressed at startup), the system monitors BSE spot index (`99919000`) and dynamically identifies the **current active expiry** for BFO option contracts. Between 09:15 and 15:00 IST, it calculates the ATM strike every 30 minutes and tracks 5 Call (CE) and 5 Put (PE) strikes every minute, recording prices to `SENSEX-DD-MM-YYYY.csv`.

Every 3 minutes, it evaluates price breakouts ($\ge$ ₹3 above session high). A breakout is only confirmed if the **underlying spot moves in the same direction** (UP for CE, DOWN for PE) and the **option premium remains above the breakout level on the next check**. Once confirmed and verifying daily risk limits (**max 4 trades/day, max loss -₹10,000**) and zero open positions, it executes **5 lots (100 quantity)**. It buys the broke-out strike (lowest premium) first for margin relief, and sells the hedged leg (+1000 strike for CE or -1000 strike for PE). It tracks live P&L, locks trailing profit at ₹2,000 steps after ₹3,000, and auto-squares off upon hitting stop-loss (-₹5,000) or target profit.

---

## 2. If BANKNIFTY is Selected

When BANKNIFTY is chosen, the system monitors NSE spot index (`99926009`) and dynamically discovers the **current active expiry** for NFO option contracts. During market hours (09:15 to 15:00 IST), it calculates the ATM strike every 30 minutes and monitors 5 Call (CE) and 5 Put (PE) strikes every minute, logging data to `BN-DD-MM-YYYY.csv`.

Every 3 minutes, it checks for breakouts ($\ge$ ₹3 above session high). A breakout is confirmed only when the **underlying spot confirms direction** (UP for CE, DOWN for PE) and the **option premium stays at or above breakout level on the following check**. After checking daily safety limits (**max 4 trades/day, max loss -₹10,000**) and zero open positions, it executes **5 lots (75 quantity)**. It buys the broke-out strike first, and sells the protective leg (+1000 strike for CE or -1000 strike for PE). It manages combined live P&L, locks trailing profits above ₹3,000 in ₹2,000 steps, and automatically squares off both legs upon reaching stop-loss (-₹5,000) or target profit.

---

## Risk & Trade Management

- **Daily Max Loss Limit**: -₹10,000 (trading halts for the day if daily realized loss hits ₹10,000).
- **Daily Max Trades Limit**: 4 trades per day.
- **Underlying & Premium Validation**: Filters false surges by requiring spot direction confirmation and premium retention.

---

## How to Run

```bash
.venv\Scripts\python.exe breakoutstrategy\breakout.py
```
- Press **Enter** for **SENSEX** (Default: 5 lots = 100 qty, dynamic current expiry).
- Type **`2`** or **`BANKNIFTY`** for **BANKNIFTY** (5 lots = 75 qty, dynamic current expiry).

### Web Dashboard
```bash
.venv\Scripts\python.exe breakoutstrategy\viewer.py
```
Open **`http://localhost:8765`** for the live light-theme dashboard, Activity Log, and closed Positions with cumulative P&L.
