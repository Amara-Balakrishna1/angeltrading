"""
Logging utility for 3-Leg Options Strategy.
Writes all logs to data/threelegs-logs folder with daily date filenames.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import logging
import sys
from datetime import datetime

# Paths
DATA_DIR = ROOT / "data"
LOG_DIR = DATA_DIR / "threelegs-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# Get today's date for log filename
today = datetime.now().strftime("%Y-%m-%d")
log_file = LOG_DIR / f"threelegs-{today}.log"


def setup_logger(name: str = "threelegs", level: int = logging.INFO) -> logging.Logger:
    """
    Sets up and returns a logger that writes to both console and file.

    Args:
        name: Logger name (default: "threelegs")
        level: Logging level (default: logging.INFO)

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Avoid adding duplicate handlers if logger already has them
    if logger.handlers:
        return logger

    # Create formatters
    file_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(name)s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    console_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-8s | %(message)s',
        datefmt='%H:%M:%S'
    )

    # File handler - writes to daily log file
    file_handler = logging.FileHandler(log_file, encoding='utf-8')
    file_handler.setLevel(level)
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    # Console handler - writes to stdout
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    return logger


# Create default logger instance
logger = setup_logger()


def get_logger(name: str = "threelegs") -> logging.Logger:
    """
    Returns a logger instance with the specified name.

    Args:
        name: Logger name (default: "threelegs")

    Returns:
        Logger instance
    """
    return logging.getLogger(name)
