# Dual-Mode Day Breakout

This is the focused guide to the dual live launcher. For the full strategy flow, risk configuration, viewer, file locations, and troubleshooting, see [README.md](README.md).

## What dual mode runs

`main_dual.py` launches two instances of the same Day Breakout signal logic:

- **REVERSE** — fades a confirmed range breakout, subject to the strategy's filters.
- **NO-REVERSE** — follows a confirmed range breakout, subject to the strategy's filters.

The REVERSE thread starts first. NO-REVERSE starts **30 seconds later** to stagger initial API requests. The threads then run concurrently. They share the selected instrument, range, SmartAPI session, base lot setting, and config, while their direction, trade records, daily trade counts, and risk tracking are mode-scoped. Both instances may enter positions; the startup margin check considers two per-mode positions, but actual entries depend on signals and broker checks.

## Run

Run from the repository root. The launcher defaults to live order execution; add `--paper` for a no-order run.

```powershell
# Paper run with NIFTY and the default spread structure
python live/daybreakout/main_dual.py --instrument NIFTY --paper

# Live SENSEX run, 5 base lots per strategy mode
python live/daybreakout/main_dual.py -i SENSEX --lots 5

# Select whichever instrument has the nearer expiry
python live/daybreakout/main_dual.py --combination true --paper

# Choose structure, range, or confirmation path
python live/daybreakout/main_dual.py -i NIFTY --structure naked --paper
python live/daybreakout/main_dual.py -i NIFTY --high 23550 --low 23350 --paper
python live/daybreakout/main_dual.py -i NIFTY --tick-confirm --paper

# Override the configured x_factor
python live/daybreakout/main_dual.py -i NIFTY --lots 10 --x-factor 0.3 --paper
```

`--combination` accepts `true` or `false`; `--combination true` enables nearest-expiry selection. Omit it to select the instrument directly or use the interactive prompt.

## Position sizing and risk

`--lots` is the base number of lots **for each mode**. Current `RISK_CONFIG.x_factor` is `0.2`, so 10 base lots are sized to 2 lots per mode (`max(1, int(base_lots x x_factor))`). A one-lot minimum applies. With both modes entering, combined exposure can reach twice the per-mode size. The executor also scales configured monetary risk thresholds by `x_factor`. Check startup output and `config.py` to confirm effective size and limits before live execution.

Each mode tracks its own trade count and P&L limits. The two modes share one account and broker session, so broker positions, margin, and contract conflicts can still affect order placement. Do not interpret independent strategy statistics as separate account balances or guaranteed independent executions.

## Monitoring and files

Order P&L monitoring begins after a fill is confirmed by the broker (or when an existing broker position is recovered). The consolidated monitor checks every **15 minutes** with the current config. The dated daily log is `data/daybreakout-logs/daybreakout-<YYYY-MM-DD>.log`. Mode-specific triggered/completed CSVs are under `data/trades/<YYYY-MM-DD>/`. The read-only dashboard runs with `python live/daybreakout/viewer.py` at [http://localhost:8766](http://localhost:8766).

See [README.md](README.md) for all output filenames and more on what the viewer displays.
