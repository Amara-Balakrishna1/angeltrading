"""
VIX Delay State Persistence Module.
Handles saving and loading VIX delay state to survive process restarts.
"""

import json
import os
from datetime import datetime
from typing import Dict, Any, Optional
from pathlib import Path

from live.daybreakout.config import DATA_DIR
from live.daybreakout.logger import logger

DELAY_STATE_FILE = DATA_DIR / "vix_delay_state.json"


def save_delay_state(vix_delay_active: Dict[str, Any]) -> bool:
    """
    Save VIX delay state to JSON file.
    Args:
        vix_delay_active: Dictionary of active delays
    Returns:
        True if saved successfully, False otherwise
    """
    try:
        # Convert datetime objects to strings for JSON serialization
        serializable_state = {}
        for key, value in vix_delay_active.items():
            serializable_value = value.copy()
            if isinstance(serializable_value.get("start_time"), datetime):
                serializable_value["start_time"] = serializable_value["start_time"].isoformat()
            serializable_state[key] = serializable_value

        # Create directory if it doesn't exist
        DELAY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

        with open(DELAY_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(serializable_state, f, indent=2)

        logger.info(f"VIX delay state saved to {DELAY_STATE_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to save VIX delay state: {e}")
        return False


def load_delay_state() -> Dict[str, Any]:
    """
    Load VIX delay state from JSON file.
    Returns:
        Dictionary of active delays with datetime objects restored
    """
    try:
        if not DELAY_STATE_FILE.exists():
            logger.info(f"No existing VIX delay state file found at {DELAY_STATE_FILE}")
            return {}

        with open(DELAY_STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Convert string timestamps back to datetime objects
        restored_state = {}
        current_time = datetime.now()
        delay_minutes_default = 30

        for key, value in state.items():
            restored_value = value.copy()
            if "start_time" in value:
                try:
                    restored_value["start_time"] = datetime.fromisoformat(value["start_time"])
                except (ValueError, TypeError):
                    logger.warning(f"Invalid start_time format for delay {key}, skipping")
                    continue

            # Check if delay has already expired
            if "start_time" in restored_value:
                elapsed_mins = (current_time - restored_value["start_time"]).total_seconds() / 60.0
                delay_mins = restored_value.get("delay_minutes", delay_minutes_default)
                if elapsed_mins >= delay_mins:
                    logger.info(f"Skipping expired delay {key} (elapsed {elapsed_mins:.0f}m >= {delay_mins}m)")
                    continue

            restored_state[key] = restored_value

        logger.info(f"Loaded {len(restored_state)} active VIX delays from {DELAY_STATE_FILE}")
        return restored_state
    except Exception as e:
        logger.error(f"Failed to load VIX delay state: {e}")
        return {}


def clear_delay_state() -> bool:
    """
    Clear the VIX delay state file.
    Returns:
        True if cleared successfully, False otherwise
    """
    try:
        if DELAY_STATE_FILE.exists():
            os.remove(DELAY_STATE_FILE)
            logger.info(f"VIX delay state file cleared: {DELAY_STATE_FILE}")
        return True
    except Exception as e:
        logger.error(f"Failed to clear VIX delay state: {e}")
        return False


def is_delay_stale(delay_info: Dict[str, Any], max_age_minutes: int = 60) -> bool:
    """
    Check if a delay entry is stale (older than max_age_minutes).
    Args:
        delay_info: Delay information dictionary
        max_age_minutes: Maximum age in minutes before considering stale
    Returns:
        True if stale, False otherwise
    """
    if "start_time" not in delay_info:
        return True

    if not isinstance(delay_info["start_time"], datetime):
        return True

    elapsed_mins = (datetime.now() - delay_info["start_time"]).total_seconds() / 60.0
    return elapsed_mins > max_age_minutes
