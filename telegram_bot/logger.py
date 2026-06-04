from typing import Any, Optional
import asyncio
import time

# This will be set by the bot at runtime to avoid circular deps
async_log_sink = None

# In-memory coalescing buffer for identical messages to avoid flooding
# Keyed by the message text; entries hold count and first-seen time.
_pending: dict[str, dict[str, Any]] = {}


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
    """Print and send a log message via the configured async sink.

    Identical messages sent repeatedly within a short window are coalesced
    and delivered once with a repeat count to avoid flooding the chat.
    """
    print(message)
    if async_log_sink is None:
        return

    key = (message or "").strip()
    if not key:
        try:
            await async_log_sink(message)
        except Exception as exc:
            print(f"⚠️ Failed to send log to Telegram: {exc}")
        return

    # Coalesce identical messages within a short window
    entry = _pending.get(key)
    if entry is None:
        # Create pending entry and schedule a flush
        _pending[key] = {"count": 1, "first_ts": time.time(), "message": message}

        async def _flush(key_local: str, delay: float = 3.0) -> None:
            await asyncio.sleep(delay)
            ent = _pending.pop(key_local, None)
            if not ent:
                return
            try:
                if ent["count"] <= 1:
                    await async_log_sink(ent["message"])
                else:
                    await async_log_sink(f"{ent['message']}\n\n(Repeated {ent['count']} times)")
            except Exception as exc:
                # Avoid crashing logging; print locally instead
                print(f"⚠️ Failed to send coalesced log to Telegram: {exc}")

        try:
            asyncio.create_task(_flush(key))
        except RuntimeError:
            # If event loop not running, send immediately
            try:
                await async_log_sink(message)
            except Exception as exc:
                print(f"⚠️ Failed to send log to Telegram: {exc}")
    else:
        # Increment count; flushing task will pick up the updated count
        try:
            entry["count"] += 1
        except Exception:
            _pending[key] = {"count": 1, "first_ts": time.time(), "message": message}
