"""
Logging utility for Day Breakout Strategy.
Writes all logs to data/daybreakout-logs folder with daily date filenames.
"""

import logging
import sys
from datetime import datetime
from pathlib import Path
from live.daybreakout.config import DATA_DIR

# Create logs directory
LOGS_DIR = DATA_DIR / "daybreakout-logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Get today's date for log filename
today = datetime.now().strftime("%Y-%m-%d")
log_file = LOGS_DIR / f"daybreakout-{today}.log"


def setup_logger(name: str = "daybreakout", level: int = logging.INFO) -> logging.Logger:
    """
    Sets up and returns a logger that writes to both console and file.
    
    Args:
        name: Logger name (default: "daybreakout")
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


def get_logger(name: str = "daybreakout") -> logging.Logger:
    """
    Returns a logger instance with the specified name.
    
    Args:
        name: Logger name (default: "daybreakout")
    
    Returns:
        Logger instance
    """
    return logging.getLogger(name)
