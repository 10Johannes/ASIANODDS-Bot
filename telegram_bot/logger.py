from typing import Any, Optional

# This will be set by the bot at runtime to avoid circular deps
async_log_sink = None


def format_bet_context(bet_info: Optional[dict[str, Any]]) -> str:
    """
    Best-effort context string to identify which bet a log line refers to.
    Example: "[ATP Sydney | Player A vs Player B]"
    """
    if not bet_info:
        return ""

    league = (bet_info.get("title") or bet_info.get("league") or bet_info.get("league_name") or "").strip()
    home = (bet_info.get("home") or "").strip()
    away = (bet_info.get("away") or "").strip()

    parts: list[str] = []
    if league:
        parts.append(league)
    if home or away:
        match = f"{home} vs {away}".strip()
        parts.append(match)

    if not parts:
        return ""

    return f"[{' | '.join(parts)}]"


async def log_message(message: str) -> None:
    print(message)
    if async_log_sink is None:
        return
    try:
        await async_log_sink(message)
    except Exception as exc:
        print(f"⚠️ Failed to send log to Telegram: {exc}")
