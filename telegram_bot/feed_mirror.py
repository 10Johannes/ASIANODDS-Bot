from __future__ import annotations
import asyncio
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .api import AsianOddsClient

# ---------------------------------------------------------------------------
# AsianOdds GetFeeds is a delta/cursor API: each response contains only the
# matches/lines that changed since the account's last read, unless the server
# decides the cursor is stale (then it sends a larger batch). A single call is
# therefore NOT a reliable full snapshot, and fixtures are frequently missing
# from "Match not listed" resolutions.
#
# This module maintains a continuously-accumulated mirror of every match/line
# ever returned, so the resolver can search the union of all feeds instead of
# one volatile response. A background poller keeps the mirror fresh.
# ---------------------------------------------------------------------------

# key: (sports_type, market_type_id, match_id, game_id) -> MatchGame dict
_MIRROR: Dict[Tuple[int, int, int, int], Dict[str, Any]] = {}
_LOCK = threading.RLock()

DEFAULT_SPORTS = (1, 3)  # Soccer, Tennis
DEFAULT_MARKET_TYPES = (0, 1, 2)  # Live, Today, Early
# NOTE on the poll interval: GetFeeds is a cursor/delta API. Frequent polling
# keeps the account cursor "fresh", so the server only returns tiny deltas and a
# full snapshot (which contains fixtures that have not changed recently) may
# never arrive. After ~4 minutes of idle the cursor goes stale and a single call
# returns the full fixture set (~4300+ matches), so poll at 240s to let that
# happen between cycles.
DEFAULT_INTERVAL_SECONDS = 240.0


def _entry_key(
    sports_type: int,
    market_type_id: int,
    match: Dict[str, Any],
) -> Optional[Tuple[int, int, int, int]]:
    match_id = match.get("MatchId")
    game_id = match.get("GameId")
    if match_id is None or game_id is None:
        return None
    return (int(sports_type), int(market_type_id), int(match_id), int(game_id))


def merge_feeds(sports_type: int, market_type_id: int, feeds_data: Dict[str, Any]) -> None:
    """Upsert every line from a GetFeeds response into the mirror; drop removals."""
    result = feeds_data.get("Result") or {}
    sports = result.get("Sports") or []
    with _LOCK:
        for sport_data in sports:
            for match in sport_data.get("MatchGames") or []:
                key = _entry_key(sports_type, market_type_id, match)
                if key is None:
                    continue
                if match.get("WillBeRemoved") or match.get("IsActive") is False:
                    _MIRROR.pop(key, None)
                else:
                    _MIRROR[key] = match


def query_matches(sports_type: int, market_type_id: int) -> List[Dict[str, Any]]:
    """Return all currently-known MatchGame entries for a sport/market."""
    with _LOCK:
        return [
            m
            for (st, mkt, _match_id, _game_id), m in _MIRROR.items()
            if st == sports_type and mkt == market_type_id
        ]


def total_entries() -> int:
    with _LOCK:
        return len(_MIRROR)


def clear() -> None:
    """Reset the mirror (used by tests and on forced refresh)."""
    with _LOCK:
        _MIRROR.clear()


def cleanup(max_age_hours: float = 6.0, *,
            past_hours: float = 6.0) -> int:
    """
    Drop entries the API flagged for removal and entries whose kickoff is long
    past (finished matches) to keep the mirror bounded. Returns count removed.
    """
    now_ms = time.time() * 1000.0
    removed = 0
    with _LOCK:
        for key, m in list(_MIRROR.items()):
            to_be_removed = m.get("ToBeRemovedOn")
            if to_be_removed:
                try:
                    if now_ms >= float(to_be_removed):
                        _MIRROR.pop(key, None)
                        removed += 1
                        continue
                except (TypeError, ValueError):
                    pass
            start_time = m.get("StartTime")
            if start_time and now_ms - float(start_time) > past_hours * 3600.0 * 1000.0:
                _MIRROR.pop(key, None)
                removed += 1
    return removed


async def run_feed_maintenance(
    client: AsianOddsClient,
    *,
    sports: Optional[Iterable[int]] = None,
    market_types: Optional[Iterable[int]] = None,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    config_loader: Optional[Callable[[], Dict[str, Any]]] = None,
) -> None:
    """
    Background task: continuously poll GetFeeds for the given sports/markets and
    merge every response into the shared mirror. GetFeeds enforces per-market
    rate limits internally, so this stays within the API's limits.

    When ``config_loader`` is provided, sports/market_types/interval are re-read
    from the returned config on every cycle so changes apply without a restart.
    """
    if sports is not None:
        sports = tuple(sports)
    if market_types is not None:
        market_types = tuple(market_types)

    while True:
        if config_loader is not None:
            try:
                cfg = config_loader() or {}
                sports = tuple(cfg.get("feed_mirror_sports") or DEFAULT_SPORTS)
                market_types = tuple(cfg.get("feed_mirror_market_types") or DEFAULT_MARKET_TYPES)
                interval = float(cfg.get("feed_mirror_interval", DEFAULT_INTERVAL_SECONDS))
                if interval <= 0:
                    interval = DEFAULT_INTERVAL_SECONDS
            except Exception as exc:
                print(f"⚠️ Feed mirror config reload failed: {exc}")
        sports = sports or DEFAULT_SPORTS
        market_types = market_types or DEFAULT_MARKET_TYPES

        try:
            for st in sports:
                for mkt in market_types:
                    try:
                        feeds_data = await client.get_feeds(
                            sports_type=int(st),
                            market_type_id=int(mkt),
                        )
                        merge_feeds(int(st), int(mkt), feeds_data)
                    except Exception as exc:
                        print(f"⚠️ Feed mirror poll failed (sport {st}, market {mkt}): {exc}")
            try:
                cleanup()
            except Exception:
                pass
        except Exception as exc:
            print(f"⚠️ Feed mirror maintenance error: {exc}")
        await asyncio.sleep(interval)
