import json
import os
import time
from typing import Optional

STATE_FILE = os.getenv("STATE_FILE", "state.json")


def _read_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_state(state: dict) -> None:
    """Write state file with Windows-compatible atomic replacement."""
    tmp = f"{STATE_FILE}.tmp"
    
    # Write to temporary file
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    
    # Try to replace atomically with retry logic for Windows
    max_retries = 5
    retry_delay = 0.1  # 100ms
    
    for attempt in range(max_retries):
        try:
            # On Windows, os.replace() may fail if file is locked
            # Try to remove the old file first if it exists, then rename
            if os.path.exists(STATE_FILE):
                # Remove the old file first (Windows may need this)
                os.remove(STATE_FILE)
            # Rename the temp file
            os.rename(tmp, STATE_FILE)
            return  # Success!
        except (PermissionError, OSError) as e:
            if attempt < max_retries - 1:
                # Wait a bit before retrying
                time.sleep(retry_delay)
                retry_delay *= 2  # Exponential backoff
            else:
                # Last attempt failed - try a fallback approach
                try:
                    # Fallback: write directly (not atomic, but better than failing)
                    with open(STATE_FILE, "w", encoding="utf-8") as f:
                        json.dump(state, f, indent=2, ensure_ascii=False)
                    # Clean up temp file if it still exists
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                    return
                except Exception as final_error:
                    # If even the fallback fails, raise the original error
                    raise e from final_error


def get_last_id(chat: str) -> int:
    state = _read_state()
    return int(state.get("last_id:" + str(chat), 0))


def set_last_id(chat: str, message_id: int) -> None:
    state = _read_state()
    state["last_id:" + str(chat)] = int(message_id)
    _write_state(state)


def has_bet_for_message(chat: str, message_id: int) -> bool:
    state = _read_state()
    return bool(state.get(f"bet_done:{chat}:{int(message_id)}", False))


def mark_bet_for_message(chat: str, message_id: int) -> None:
    state = _read_state()
    state[f"bet_done:{chat}:{int(message_id)}"] = True
    _write_state(state)


# Cross-channel de-duplication using a deterministic bet signature
def has_bet_signature(signature: str) -> bool:
    state = _read_state()
    return bool(state.get(f"bet_sig:{signature}", False))


def mark_bet_signature(signature: str) -> None:
    state = _read_state()
    state[f"bet_sig:{signature}"] = True
    _write_state(state)