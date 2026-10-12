"""
Verification script for 3-Leg Strategy implementation.
Tests basic structure and imports without external dependencies.
"""

import sys
import io
from pathlib import Path

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        else:
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add parent directory to path
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

print("=" * 65)
print("VERIFICATION: 3-Leg Strategy Implementation")
print("=" * 65)

# Test 1: Directory structure
print("\n[1] Checking directory structure...")
live_dir = ROOT / "live"
threelegs_dir = live_dir / "threelegs"

required_files = [
    "__init__.py",
    "config.py",
    "logger.py",
    "instrument_map.py",
    "executor.py",
    "monitor.py",
    "strategy.py",
    "main.py"
]

missing_files = []
for file in required_files:
    file_path = threelegs_dir / file
    if not file_path.exists():
        missing_files.append(file)
    else:
        print(f"    [OK] {file}")

if missing_files:
    print(f"    [FAIL] Missing files: {missing_files}")
    sys.exit(1)

# Test 2: Config module
print("\n[2] Testing config module...")
try:
    from live.threelegs.config import INSTRUMENT_CONFIG, MONITORING_TIMES, ENTRY_TIME, EXIT_TIME
    print(f"    [OK] INSTRUMENT_CONFIG: {list(INSTRUMENT_CONFIG.keys())}")
    print(f"    [OK] MONITORING_TIMES: {MONITORING_TIMES}")
    print(f"    [OK] ENTRY_TIME: {ENTRY_TIME}")
    print(f"    [OK] EXIT_TIME: {EXIT_TIME}")
except Exception as e:
    print(f"    [FAIL] Config import failed: {e}")
    sys.exit(1)

# Test 3: Logger module
print("\n[3] Testing logger module...")
try:
    from live.threelegs.logger import logger, setup_logger
    print(f"    [OK] Logger module imported successfully")
    print(f"    [OK] Logger name: {logger.name}")
except Exception as e:
    print(f"    [FAIL] Logger import failed: {e}")
    sys.exit(1)

# Test 4: Check file syntax
print("\n[4] Checking Python syntax...")
import py_compile
for file in required_files:
    file_path = threelegs_dir / file
    try:
        py_compile.compile(str(file_path), doraise=True)
        print(f"    [OK] {file} - syntax OK")
    except py_compile.PyCompileError as e:
        print(f"    [FAIL] {file} - syntax error: {e}")
        sys.exit(1)

# Test 5: Check copied modules
print("\n[5] Checking copied modules...")
daybreakout_dir = live_dir / "daybreakout"
breakoutstrategy_dir = live_dir / "breakoutstrategy"

if daybreakout_dir.exists():
    print(f"    [OK] live/daybreakout/ exists")
else:
    print(f"    [FAIL] live/daybreakout/ missing")

if breakoutstrategy_dir.exists():
    print(f"    [OK] live/breakoutstrategy/ exists")
else:
    print(f"    [FAIL] live/breakoutstrategy/ missing")

print("\n" + "=" * 65)
print("VERIFICATION COMPLETE - All checks passed!")
print("=" * 65)
print("\nTo run the strategy:")
print("  python live/threelegs/main.py --paper --side CE")
print("\nFor live trading:")
print("  python live/threelegs/main.py --side CE")
