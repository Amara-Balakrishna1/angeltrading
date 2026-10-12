"""
Verification script for all live strategies.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

# Try to use venv if available
venv_python = ROOT / ".venv" / "Scripts" / "python.exe"
if venv_python.exists():
    print(f"[*] Using virtual environment: {venv_python}")
    sys.executable = str(venv_python)

print("=" * 60)
print("VERIFICATION: All Live Strategies")
print("=" * 60)

# Check daybreakout
print("\n[1] Checking live/daybreakout...")
try:
    from live.daybreakout.config import INSTRUMENTS
    print(f"    [OK] Config loaded: {list(INSTRUMENTS.keys())}")
except Exception as e:
    print(f"    [FAIL] Config error: {e}")

try:
    from live.daybreakout.contract_finder import load_scrip_master
    print(f"    [OK] Contract finder imported")
except Exception as e:
    print(f"    [FAIL] Contract finder error: {e}")

# Check breakoutstrategy
print("\n[2] Checking live/breakoutstrategy...")
try:
    from live.breakoutstrategy.breakout import INDEX_CONFIGS
    print(f"    [OK] Config loaded: {list(INDEX_CONFIGS.keys())}")
except Exception as e:
    print(f"    [FAIL] Config error: {e}")

# Check threelegs
print("\n[3] Checking live/threelegs...")
try:
    from live.threelegs.config import INSTRUMENT_CONFIG
    print(f"    [OK] Config loaded: {list(INSTRUMENT_CONFIG.keys())}")
except Exception as e:
    print(f"    [FAIL] Config error: {e}")

try:
    from live.threelegs.instrument_map import download_instrument_map
    print(f"    [OK] Instrument map imported")
except Exception as e:
    print(f"    [FAIL] Instrument map error: {e}")

print("\n" + "=" * 60)
print("VERIFICATION COMPLETE")
print("=" * 60)
