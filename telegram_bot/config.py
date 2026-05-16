import json
import os
from typing import Dict, Any

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
CONFIG_FILE = os.path.join(_PROJECT_ROOT, "config.json")
DEFAULT_CONFIG: Dict[str, Any] = {
    "base_stake": 5,
    "min_stake": 5,
    "max_stake": 20,
    "min_unit": 1,
    "max_unit": 2,
    # Optional per-channel overrides. Keys can be channel usernames (e.g. "@mychannel"),
    # channel titles (e.g. "My Channel Name"), or numeric chat IDs (e.g. "-100123...").
    # Values can override stake-related settings like base_stake/min_stake/max_stake/min_unit/max_unit.
    "channel_settings": {},
    # Optional per-channel forwarding mode. Keys can be channel usernames (e.g. "@mychannel"),
    # channel titles (e.g. "My Channel Name"), or numeric chat IDs (e.g. "-100123...").
    # Values can be "all" (forward every message) or "bet" (forward only bet/tip messages; default behavior).
    "channel_forwarding": {},
    "odds_tolerance": 0.01,
    # Minimum decimal odds for any bet (tip line and API price). Tips below this are ignored;
    # API price must also be at least this (and at least max(this, tipster "min odds" text if any)).
    "global_min_odds": 1.15,
    "allow_tennis": True,
    "allow_soccer": True,
    "allow_football": True,  # kept for backwards compatibility
    "allow_basketball": True,
    "allow_rugby_union": True,
    # Bet type settings: allow pre-match and/or live bets
    "allow_prematch": True,  # allow pre-match bets (liveStatus 0, status I or O)
    "allow_live": True,     # allow live bets (liveStatus 1 or 2, status O)
    # Optional per-sport overrides for bet type preference.
    # Keys: "tennis" | "soccer" | "basketball" | "rugby_union"
    # Values: "prematch" | "live" | "both"
    # Example: {"soccer": "live", "tennis": "prematch"}
    "bettype_by_sport": {},
    # Retry settings for fixture/line not found cases
    "retry_attempts": 3,                 # number of additional retries (after initial attempt)
    "retry_interval_minutes": 2,         # wait time between retries in minutes
    # Fast retries in seconds (before minute-based retries). Helps with short feed lag.
    "quick_retry_attempts": 2,
    "quick_retry_delay_seconds": 8.0,
    # Retry settings for incomplete place-bet result message
    "place_result_retry_attempts": 3,
    "place_result_retry_interval_minutes": 1,
    # Fast retries in seconds for place-bet failures (e.g., stale line right after tip arrives).
    "quick_place_retry_attempts": 2,
    "quick_place_retry_delay_seconds": 5.0,
    # Force modes
    "force_outgoing": False,
    "force_incoming": False,
    # Tipster-specific settings
    "tipster_settings": {
        "default": {
            "base_stake": 5,
            "min_stake": 5,
            "max_stake": 20,
            "min_unit": 1,
            "max_unit": 2
        }
    }
}


def _coerce_bool(value: Any, default: bool) -> bool:
    """Robust bool parser for values that may come from manual config edits."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in {"1", "true", "yes", "on"}:
            return True
        if v in {"0", "false", "no", "off"}:
            return False
    return default


def _normalize_bettype_by_sport(raw: Any) -> Dict[str, str]:
    """
    Canonicalize per-sport bettype overrides:
    - map football -> soccer
    - keep only tennis/soccer/basketball/rugby_union
    - keep only prematch/live/both
    """
    if not isinstance(raw, dict):
        return {}
    normalized: Dict[str, str] = {}
    for key, value in raw.items():
        k = str(key or "").strip().lower()
        if k == "football":
            k = "soccer"
        if k in {"rugby", "rugbyunion", "rugby union"}:
            k = "rugby_union"
        if k not in {"tennis", "soccer", "basketball", "rugby_union"}:
            continue
        v = str(value or "").strip().lower()
        if v not in {"prematch", "live", "both"}:
            continue
        normalized[k] = v
    return normalized


def load_config() -> Dict[str, Any]:
    # Load existing config if present
    data: Dict[str, Any]
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}

    # Merge defaults without overwriting user-specified values
    merged: Dict[str, Any] = DEFAULT_CONFIG.copy()
    merged.update(data)

    # Normalize sport toggles and keep soccer/football alias in sync.
    merged["allow_tennis"] = _coerce_bool(merged.get("allow_tennis", DEFAULT_CONFIG["allow_tennis"]), DEFAULT_CONFIG["allow_tennis"])
    if "allow_soccer" in merged:
        merged["allow_soccer"] = _coerce_bool(merged.get("allow_soccer"), DEFAULT_CONFIG["allow_soccer"])
    else:
        merged["allow_soccer"] = _coerce_bool(merged.get("allow_football", DEFAULT_CONFIG["allow_soccer"]), DEFAULT_CONFIG["allow_soccer"])
    merged["allow_football"] = merged["allow_soccer"]
    merged["allow_basketball"] = _coerce_bool(merged.get("allow_basketball", DEFAULT_CONFIG["allow_basketball"]), DEFAULT_CONFIG["allow_basketball"])
    merged["allow_rugby_union"] = _coerce_bool(merged.get("allow_rugby_union", DEFAULT_CONFIG["allow_rugby_union"]), DEFAULT_CONFIG["allow_rugby_union"])
    
    # Backward compatibility: migrate allow_pregame to allow_prematch
    if "allow_pregame" in merged and "allow_prematch" not in merged:
        merged["allow_prematch"] = merged.pop("allow_pregame")

    # Normalize global bet-type toggles.
    merged["allow_prematch"] = _coerce_bool(merged.get("allow_prematch", DEFAULT_CONFIG["allow_prematch"]), DEFAULT_CONFIG["allow_prematch"])
    merged["allow_live"] = _coerce_bool(merged.get("allow_live", DEFAULT_CONFIG["allow_live"]), DEFAULT_CONFIG["allow_live"])

    # Normalize per-sport bet-type overrides.
    merged["bettype_by_sport"] = _normalize_bettype_by_sport(merged.get("bettype_by_sport"))

    # Normalize retry/force settings so runtime behavior is deterministic.
    try:
        merged["retry_attempts"] = int(max(0, merged.get("retry_attempts", DEFAULT_CONFIG["retry_attempts"])))
    except Exception:
        merged["retry_attempts"] = DEFAULT_CONFIG["retry_attempts"]
    try:
        merged["retry_interval_minutes"] = float(max(0, merged.get("retry_interval_minutes", DEFAULT_CONFIG["retry_interval_minutes"])))
    except Exception:
        merged["retry_interval_minutes"] = DEFAULT_CONFIG["retry_interval_minutes"]
    try:
        merged["quick_retry_attempts"] = int(max(0, merged.get("quick_retry_attempts", DEFAULT_CONFIG["quick_retry_attempts"])))
    except Exception:
        merged["quick_retry_attempts"] = DEFAULT_CONFIG["quick_retry_attempts"]
    try:
        merged["quick_retry_delay_seconds"] = float(max(0, merged.get("quick_retry_delay_seconds", DEFAULT_CONFIG["quick_retry_delay_seconds"])))
    except Exception:
        merged["quick_retry_delay_seconds"] = DEFAULT_CONFIG["quick_retry_delay_seconds"]
    try:
        merged["place_result_retry_attempts"] = int(max(0, merged.get("place_result_retry_attempts", DEFAULT_CONFIG["place_result_retry_attempts"])))
    except Exception:
        merged["place_result_retry_attempts"] = DEFAULT_CONFIG["place_result_retry_attempts"]
    try:
        merged["place_result_retry_interval_minutes"] = float(max(0, merged.get("place_result_retry_interval_minutes", DEFAULT_CONFIG["place_result_retry_interval_minutes"])))
    except Exception:
        merged["place_result_retry_interval_minutes"] = DEFAULT_CONFIG["place_result_retry_interval_minutes"]
    try:
        merged["quick_place_retry_attempts"] = int(max(0, merged.get("quick_place_retry_attempts", DEFAULT_CONFIG["quick_place_retry_attempts"])))
    except Exception:
        merged["quick_place_retry_attempts"] = DEFAULT_CONFIG["quick_place_retry_attempts"]
    try:
        merged["quick_place_retry_delay_seconds"] = float(max(0, merged.get("quick_place_retry_delay_seconds", DEFAULT_CONFIG["quick_place_retry_delay_seconds"])))
    except Exception:
        merged["quick_place_retry_delay_seconds"] = DEFAULT_CONFIG["quick_place_retry_delay_seconds"]
    merged["force_outgoing"] = bool(merged.get("force_outgoing", DEFAULT_CONFIG["force_outgoing"]))
    merged["force_incoming"] = bool(merged.get("force_incoming", DEFAULT_CONFIG["force_incoming"]))

    try:
        gmo = float(merged.get("global_min_odds", DEFAULT_CONFIG["global_min_odds"]))
    except Exception:
        gmo = float(DEFAULT_CONFIG["global_min_odds"])
    if gmo < 0:
        gmo = 0.0
    merged["global_min_odds"] = gmo

    return merged


def save_config(cfg: Dict[str, Any]) -> None:
    # Normalize sport toggles and keep soccer/football aliases in sync.
    cfg["allow_tennis"] = _coerce_bool(cfg.get("allow_tennis", DEFAULT_CONFIG["allow_tennis"]), DEFAULT_CONFIG["allow_tennis"])
    if "allow_soccer" in cfg:
        cfg["allow_soccer"] = _coerce_bool(cfg.get("allow_soccer"), DEFAULT_CONFIG["allow_soccer"])
    else:
        cfg["allow_soccer"] = _coerce_bool(cfg.get("allow_football", DEFAULT_CONFIG["allow_soccer"]), DEFAULT_CONFIG["allow_soccer"])
    cfg["allow_football"] = cfg["allow_soccer"]
    cfg["allow_basketball"] = _coerce_bool(cfg.get("allow_basketball", DEFAULT_CONFIG["allow_basketball"]), DEFAULT_CONFIG["allow_basketball"])
    cfg["allow_rugby_union"] = _coerce_bool(cfg.get("allow_rugby_union", DEFAULT_CONFIG["allow_rugby_union"]), DEFAULT_CONFIG["allow_rugby_union"])

    # Normalize global and per-sport bettype settings.
    cfg["allow_prematch"] = _coerce_bool(cfg.get("allow_prematch", DEFAULT_CONFIG["allow_prematch"]), DEFAULT_CONFIG["allow_prematch"])
    cfg["allow_live"] = _coerce_bool(cfg.get("allow_live", DEFAULT_CONFIG["allow_live"]), DEFAULT_CONFIG["allow_live"])
    cfg["bettype_by_sport"] = _normalize_bettype_by_sport(cfg.get("bettype_by_sport"))
    
    # Migrate old allow_pregame to allow_prematch when saving
    if "allow_pregame" in cfg:
        if "allow_prematch" not in cfg:
            cfg["allow_prematch"] = cfg["allow_pregame"]
        del cfg["allow_pregame"]

    try:
        gmo = float(cfg.get("global_min_odds", DEFAULT_CONFIG["global_min_odds"]))
    except Exception:
        gmo = float(DEFAULT_CONFIG["global_min_odds"])
    if gmo < 0:
        gmo = 0.0
    cfg["global_min_odds"] = gmo

    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=4)
