import os
import re
from dataclasses import dataclass, field
from typing import List, Optional
from dotenv import load_dotenv
from pathlib import Path


@dataclass
class TelegramEnv:
    api_id: int
    api_hash: str
    channel: str
    forwarder_channel: Optional[str] = None  # first forwarder (back-compat)
    forwarder_channels: List[str] = field(default_factory=list)
    listener_channels: List[str] = field(default_factory=list)


@dataclass
class AsianOddsEnv:
    """AsianOdds API credentials."""
    username: str
    password: str
    odds_format: str = "00"  # 00=European/Decimal, MY=Malaysian, HK=Hong Kong
    default_bookies: str = "ALL"  # Comma-separated list or "ALL"
    api_login_url: str = ""  # Optional override; defaults to webapi.asianodds88.com


# Backwards compatibility alias
PS3838Env = AsianOddsEnv


@dataclass
class AppEnv:
    telegram: TelegramEnv
    asianodds: AsianOddsEnv
    
    # Backwards compatibility property
    @property
    def ps3838(self) -> AsianOddsEnv:
        return self.asianodds


def _clean_env_value(value: Optional[str]) -> str:
    """Strip whitespace and inline # comments from a dotenv value."""
    if not value:
        return ""
    s = value.strip()
    if not s or s.startswith("#"):
        return ""
    if "#" in s:
        s = s.split("#", 1)[0].rstrip()
    return s.strip()


def _normalize_channel_identifier(raw: str) -> str:
    """
    Normalize channel identifiers so env values can be usernames, URLs, or numeric IDs (e.g., -100123...).
    Returns a cleaned string suitable for Telethon chats parameter.
    """
    if not raw:
        return ""

    s = raw.strip()
    if not s:
        return ""

    # Strip protocol/domain prefixes (t.me/..., telegram.me/..., telegram.dog/...) with or without scheme
    s = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/", "", s, flags=re.IGNORECASE)
    # Remove query/fragment and trailing slash
    s = s.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    # Remove leading @ or leading "c/" segment
    s = re.sub(r"^@+", "", s)
    s = re.sub(r"^c/", "", s, flags=re.IGNORECASE)

    # If numeric (chat id), keep as-is
    if s.lstrip("+-").isdigit():
        return s

    if not s:
        return ""

    # Otherwise, ensure it starts with @ for clarity
    return f"@{s}"


def load_env() -> AppEnv:
    env_path = os.path.join(os.getcwd(), '.env')
    if not os.path.exists(env_path):
        raise FileNotFoundError(f"❌ .env file not found at: {env_path}")

    load_dotenv(env_path)

    # Read environment variables
    api_id_str = os.getenv("API_ID", "")
    api_hash = os.getenv("API_HASH", "")
    channel = _normalize_channel_identifier(_clean_env_value(os.getenv("TELEGRAM_CHANNEL", "")))
    # Allow both new multi-value vars and legacy single-value vars.
    # Merge both sources to avoid ambiguity when users have both set.
    forwarder_channels_raw_multi = _clean_env_value(os.getenv("FORWARDER_CHANNELS", ""))
    forwarder_channels_raw_single = _clean_env_value(os.getenv("FORWARDER_CHANNEL", ""))
    listener_channels_raw_multi = _clean_env_value(os.getenv("LISTENER_CHANNELS", ""))
    listener_channels_raw_single = _clean_env_value(os.getenv("LISTENER_CHANNEL", ""))

    # AsianOdds credentials (with backwards compatibility for PS3838 env vars)
    ao_user = (os.getenv("ASIANODDS_USERNAME") or os.getenv("PS3838_USERNAME", "")).strip()
    ao_pass = (os.getenv("ASIANODDS_PASSWORD") or os.getenv("PS3838_PASSWORD", "")).strip()
    ao_odds_format = os.getenv("ASIANODDS_ODDS_FORMAT", "00")  # 00=Decimal, MY=Malaysian, HK=Hong Kong
    ao_bookies = os.getenv("ASIANODDS_BOOKIES", "ALL")
    ao_api_login_url = _clean_env_value(
        os.getenv("ASIANODDS_API_LOGIN_URL") or os.getenv("ASIANODDS_API_BASE_URL", "")
    )

    # --- Validate Telegram credentials ---
    if not api_id_str or not api_id_str.isdigit() or not api_hash:
        raise ValueError(
            "❌ Missing or invalid Telegram credentials.\n"
            "Please set API_ID and API_HASH in your .env file."
        )

    api_id = int(api_id_str)

    if not ao_user or not ao_pass:
        raise ValueError(
            "❌ Missing AsianOdds API credentials.\n"
            "Set ASIANODDS_USERNAME and ASIANODDS_PASSWORD in your .env file.\n"
            "Use your plain API password (not an MD5 hash). API login details are "
            "separate from the AsianOdds website login — contact AsianOdds support "
            "if you do not have API credentials."
        )

    # --- Print debug info (mask sensitive data) ---
    print("✅ Environment variables loaded successfully!")
    print(f"  API_ID: {api_id}")
    print(f"  API_HASH: {'*' * len(api_hash)}")
    print(f"  TELEGRAM_CHANNEL (Listener): {channel or '(not set)'}")
    def _collect_channels(*raw_sources: str) -> List[str]:
        channels: List[str] = []
        for raw in raw_sources:
            if not raw:
                continue
            for ch in re.split(r"[,\s]+", raw):
                ch = _normalize_channel_identifier(ch)
                if not ch or ch == "@":
                    continue
                if ch not in channels:
                    channels.append(ch)
        return channels

    # Merge multi + legacy values, preserving order and removing duplicates.
    forwarder_channels = _collect_channels(forwarder_channels_raw_multi, forwarder_channels_raw_single)
    print(f"  FORWARDER_CHANNELS: {', '.join(forwarder_channels) if forwarder_channels else '(not set)'}")
    listener_channels = _collect_channels(listener_channels_raw_multi, listener_channels_raw_single)
    # Remove the main channel from additional listeners to avoid duplicates
    listener_channels = [ch for ch in listener_channels if ch and ch != channel]

    print(f"  LISTENER_CHANNELS (Additional): {', '.join(listener_channels) if listener_channels else '(not set)'}")
    print(f"  ASIANODDS_USERNAME: {ao_user}")
    print(f"  ASIANODDS_PASSWORD: {'*' * len(ao_pass)}")
    if len(ao_pass) == 32 and re.fullmatch(r"[0-9a-fA-F]{32}", ao_pass):
        print(
            "  ⚠️ ASIANODDS_PASSWORD looks like an MD5 hash. "
            "Set your plain API password instead; the bot hashes it for login."
        )
    print(f"  ASIANODDS_ODDS_FORMAT: {ao_odds_format}")
    print(f"  ASIANODDS_BOOKIES: {ao_bookies}")

    return AppEnv(
        telegram=TelegramEnv(
            api_id=api_id,
            api_hash=api_hash,
            channel=channel,
            forwarder_channel=forwarder_channels[0] if forwarder_channels else None,
            forwarder_channels=forwarder_channels,
            listener_channels=listener_channels,
        ),
        asianodds=AsianOddsEnv(
            username=ao_user,
            password=ao_pass,
            odds_format=ao_odds_format,
            default_bookies=ao_bookies,
            api_login_url=ao_api_login_url,
        ),
    )


# Example usage
if __name__ == "__main__":
    env = load_env()
    print("\n🎯 Environment loaded and ready to use!")
