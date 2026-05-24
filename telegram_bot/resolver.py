from __future__ import annotations
import json
import re
import unicodedata
import difflib
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List
from .logger import log_message, format_bet_context
from .api import AsianOddsClient


def _strip_accents(s: Optional[str]) -> str:
    """
    Convert accented characters to their ASCII base (e.g., 'Clément' -> 'Clement').
    This is critical because APIs often use unaccented names while tipsters use accents.
    """
    if not s:
        return ""
    try:
        norm = unicodedata.normalize("NFKD", s)
        return "".join(ch for ch in norm if not unicodedata.combining(ch))
    except Exception:
        return s


def _normalize_league_name(name: Optional[str]) -> str:
    """Lowercase, remove punctuation, and drop round indicators for fuzzy matching."""
    if not name:
        return ""

    text = _strip_accents(name).lower()
    # Normalize separators to spaces for easier tokenization
    text = text.replace("•", " ")
    text = re.sub(r"[–—/:]", " ", text)
    
    # Tournament name synonyms — map alternate names to canonical names
    synonyms = [
        (r"\broland[\s\-]+garros\b", "french open"),
        (r"\bus\s+open\b", "us open"),  # already canonical, but normalize spacing
        (r"\bwimbledon\b", "wimbledon"),
        (r"\baustralian\s+open\b", "australian open"),
    ]
    for pattern, replacement in synonyms:
        text = re.sub(pattern, replacement, text)

    # Remove round/stage indicators (e.g., "- r16", "quarterfinal", "qualifying")
    text = re.sub(
        r"\b("
        r"r\d+|"
        r"\d+\s*/\s*\d+|"
        r"\d+(?:er|e|eme|ème)?|"
        r"round\s*(?:of\s*)?\d+|"
        r"round\s*[a-z]?|"
        r"qf|sf|gf|finals?|"
        r"quarter[-\s]?finals?|"
        r"semi[-\s]?finals?|"
        r"quart(?:s)?\s+de\s+finale|"
        r"demi[-\s]?finale(?:s)?|"
        r"huitieme(?:s)?\s+de\s+finale|"
        r"huiti[eè]me(?:s)?\s+de\s+finale|"
        r"seizieme(?:s)?\s+de\s+finale|"
        r"seizi[eè]me(?:s)?\s+de\s+finale|"
        r"de\s+finale|"
        r"qualifiers?|qualifying|"
        r"group\s+[a-z0-9]+"
        r")\b",
        " ",
        text,
    )

    # Collapse non-alphanumeric characters
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _league_name_matches(bet_title: str, league_name: str) -> bool:
    """Return True when normalized league names closely match."""
    bet_norm = _normalize_league_name(bet_title)
    league_norm = _normalize_league_name(league_name)

    if not bet_norm or not league_norm:
        return False

    # Avoid matching specialized leagues (e.g., Corners) unless explicitly requested
    special_tokens = ["corners", "cards", "bookings", "offsides", "penalties", "shots"]
    for token in special_tokens:
        if token in league_norm and token not in bet_norm:
            return False

    if bet_norm == league_norm:
        return True

    if bet_norm in league_norm or league_norm in bet_norm:
        return True

    bet_tokens = set(bet_norm.split())
    league_tokens = set(league_norm.split())
    overlap = bet_tokens & league_tokens

    if len(overlap) >= 2:
        return True

    if overlap:
        coverage = len(overlap) / max(1, len(bet_tokens))
        if coverage >= 0.6:
            return True

    # Special handling for tennis tournaments - be more lenient
    bet_has_tennis_org = "atp" in bet_tokens or "wta" in bet_tokens or "itf" in bet_tokens
    league_has_tennis_org = "atp" in league_tokens or "wta" in league_tokens or "itf" in league_tokens

    if bet_has_tennis_org and league_has_tennis_org:
        tennis_orgs = {"atp", "wta", "itf", "challenger"}
        bet_location_tokens = bet_tokens - tennis_orgs
        league_location_tokens = league_tokens - tennis_orgs

        if bet_location_tokens and league_location_tokens:
            location_overlap = bet_location_tokens & league_location_tokens
            if location_overlap:
                return True

    return False


def _parse_start_time(start_ms: Optional[int]) -> Optional[datetime]:
    """Parse milliseconds since epoch into aware datetime."""
    if not start_ms:
        return None
    try:
        return datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _normalize_participant_name(name: Optional[str]) -> str:
    """Normalize participant names for resilient matching (accents/punctuation/spacing)."""
    if not name:
        return ""
    s = _strip_accents(str(name)).lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _strip_api_team_name(name: Optional[str]) -> str:
    """Drop AsianOdds derivative-market suffixes (e.g. 'Genoa - No. of Corners')."""
    if not name:
        return ""
    s = str(name).strip()
    s = re.split(r"\s*-\s*(?:no\.?\s*of\s*)", s, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    return s


def _is_derivative_market_event(home: Optional[str], away: Optional[str]) -> bool:
    """Skip corners/bookings/special markets when resolving the main match."""
    combined = f"{home or ''} {away or ''}".lower()
    markers = (
        "no. of corners",
        "no. of bookings",
        "no. of cards",
        "offsides",
        "shots on target",
        "team total",
    )
    return any(marker in combined for marker in markers)


def _participant_names_match(a: Optional[str], b: Optional[str]) -> bool:
    """
    Match participant names with tolerance for abbreviations/hyphenation.
    Example: "Felix Auger-Aliassime" vs "F Auger Aliassime".
    """
    na = _normalize_participant_name(a)
    nb = _normalize_participant_name(b)
    if not na or not nb:
        return False
    if na == nb:
        return True

    # Substring containment can handle short/long variants.
    if (len(na) >= 6 and na in nb) or (len(nb) >= 6 and nb in na):
        return True

    ta = [t for t in na.split() if t]
    tb = [t for t in nb.split() if t]
    if not ta or not tb:
        return False

    # Last-name(s) often carry strongest identity in tennis feeds.
    if ta[-1] == tb[-1]:
        return True
    if len(ta) >= 2 and len(tb) >= 2 and ta[-2:] == tb[-2:]:
        return True

    # Token overlap fallback for multi-part names.
    overlap = set(ta) & set(tb)
    if len(overlap) >= 2:
        return True

    # Fuzzy fallback as a last resort.
    score = difflib.SequenceMatcher(None, na, nb).ratio()
    return score >= 0.82


def _extract_odds_from_bookie_string(bookie_odds_str: str, bookie: str) -> Optional[float]:
    """
    Extract odds for a specific bookie from a BookieOdds string.
    Format: "ISN=2.260,1.610;IBC=2.300,1.580;BEST=ISN 2.260,IBC 1.580"
    """
    if not bookie_odds_str or not bookie:
        return None
    
    # Split by semicolon to get bookie sections
    sections = bookie_odds_str.split(";")
    for section in sections:
        if section.startswith(f"{bookie}="):
            # Extract odds values
            odds_part = section.split("=", 1)[1]
            odds_values = odds_part.split(",")
            if odds_values:
                try:
                    return float(odds_values[0])
                except ValueError:
                    pass
    return None


_FEED_LINE_RANGE_RE = re.compile(r"^(?P<a>\d+(?:\.\d+)?)\s*-\s*(?P<b>\d+(?:\.\d+)?)$")


def _parse_feed_line_value(raw: Any) -> Optional[float]:
    """
    Parse AsianOdds Handicap/Goal fields.

    Handles single values (3.5) and line ranges (35.5-36.5) without confusing
  them with signed handicaps (-1.5).
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = _FEED_LINE_RANGE_RE.match(s)
    if m:
        try:
            return (float(m.group("a")) + float(m.group("b"))) / 2
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _line_matches_target(
    raw: Any,
    target: float,
    *,
    tolerance: float = 0.05,
    use_abs: bool = False,
) -> bool:
    val = _parse_feed_line_value(raw)
    if val is None:
        return False
    if use_abs:
        return abs(abs(val) - abs(float(target))) <= tolerance
    return abs(val - float(target)) <= tolerance


def _strip_tennis_unit_tag(name: Optional[str]) -> str:
    """Remove trailing (Games)/(Sets) tags from tipster or API names."""
    if not name:
        return ""
    s = str(name).strip()
    s = re.sub(
        r"\s*\(\s*(?:games|sets|jeux|manches)\s*\)\s*$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    return s.strip()


def _name_for_matching(name: Optional[str]) -> str:
    """Normalize a name for participant matching (strip unit tags and API suffixes)."""
    return _strip_api_team_name(_strip_tennis_unit_tag(name))


def _feed_entry_has_unit(home_raw: str, away_raw: str, preferred_unit: str) -> bool:
    combined = (home_raw + away_raw).lower()
    if preferred_unit == "sets":
        return "(sets)" in combined
    if preferred_unit == "games":
        return "(games)" in combined
    return True


def _collect_matching_games_for_bet(
    sport_data: Dict[str, Any],
    bet_info: Dict[str, Any],
    bet_title: Optional[str],
    *,
    allow_live: bool,
    allow_prematch: bool,
) -> List[Dict[str, Any]]:
    """Collect feed rows for the exact matchup (league + players + Games/Sets when specified)."""
    preferred_unit = (bet_info.get("preferred_resulting_unit") or "").strip().lower()
    tip_home = _name_for_matching(bet_info.get("home"))
    tip_away = _name_for_matching(bet_info.get("away"))
    games: List[Dict[str, Any]] = []

    for match in sport_data.get("MatchGames", []):
        home_raw = match.get("HomeTeam", {}).get("Name", "")
        away_raw = match.get("AwayTeam", {}).get("Name", "")
        home_name = _name_for_matching(home_raw)
        away_name = _name_for_matching(away_raw)
        if _is_derivative_market_event(home_name, away_name):
            continue
        if preferred_unit in ("sets", "games"):
            if not _feed_entry_has_unit(home_raw, away_raw, preferred_unit):
                continue
        league_name = match.get("LeagueName", "")
        if bet_title and not _league_name_matches(bet_title, league_name):
            continue
        if not (
            _participant_names_match(home_name, tip_home)
            and _participant_names_match(away_name, tip_away)
        ):
            continue
        is_live = match.get("IsLive", 0) == 1
        if is_live and not allow_live:
            continue
        if not is_live and not allow_prematch:
            continue
        if match.get("MatchId") is None or match.get("LeagueId") is None:
            continue
        games.append(match)
    return games


def _parse_hdp_abs_value(raw: Any) -> Optional[float]:
    """Absolute handicap value from feed (handles signed numbers and x-y ranges)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        if s.startswith("-") or s.startswith("+"):
            return abs(float(s))
    except ValueError:
        pass
    val = _parse_feed_line_value(s)
    if val is not None:
        return abs(val)
    try:
        return abs(float(s))
    except ValueError:
        return None


def _find_hdp_game(
    matching_games: List[Dict[str, Any]],
    target_handicap: float,
    selection_type: str,
    *,
    is_full_time: bool = True,
    tolerance: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Pick the HDP feed row matching tip line and favoured direction (no wrong-line fallback)."""
    sel_type = (selection_type or "home").strip().lower()
    expected_favoured = 0
    try:
        hdp_sign = float(target_handicap)
    except (TypeError, ValueError):
        return None
    if hdp_sign < 0:
        expected_favoured = 1 if sel_type == "home" else 2
    elif hdp_sign > 0:
        expected_favoured = 2 if sel_type == "home" else 1

    target_abs = abs(hdp_sign)
    hdp_key = "FullTimeHdp" if is_full_time else "HalfTimeHdp"
    fav_key = "FullTimeFavoured" if is_full_time else "HalfTimeFavoured"
    best_game: Optional[Dict[str, Any]] = None
    best_diff = float("inf")

    for game in matching_games:
        hdp = game.get(hdp_key, {})
        if not hdp.get("Handicap") or not hdp.get("BookieOdds"):
            continue
        favoured = game.get(fav_key, 0)
        if expected_favoured and favoured and favoured != expected_favoured:
            continue
        feed_abs = _parse_hdp_abs_value(hdp.get("Handicap"))
        if feed_abs is None:
            continue
        diff = abs(feed_abs - target_abs)
        if diff < best_diff:
            best_diff = diff
            best_game = game

    if best_game is not None and best_diff <= tolerance:
        return best_game
    return None


def verify_resolved_players(bet_info: Dict[str, Any]) -> Optional[str]:
    """Return an error message when the resolved API fixture doesn't match the tip players."""
    ao_home = bet_info.get("ao_home") or ""
    ao_away = bet_info.get("ao_away") or ""
    if not ao_home and not ao_away:
        return None
    tip_home = _name_for_matching(bet_info.get("home"))
    tip_away = _name_for_matching(bet_info.get("away"))
    api_home = _name_for_matching(ao_home)
    api_away = _name_for_matching(ao_away)
    if (
        _participant_names_match(api_home, tip_home)
        and _participant_names_match(api_away, tip_away)
    ) or (
        _participant_names_match(api_home, tip_away)
        and _participant_names_match(api_away, tip_home)
    ):
        return None
    return (
        f"Resolved wrong match in feeds: API has {ao_home} vs {ao_away}, "
        f"tip expects {bet_info.get('home')} vs {bet_info.get('away')}"
    )


def _find_ou_game(
    matching_games: List[Dict[str, Any]],
    target: float,
    *,
    is_full_time: bool = True,
    tolerance: float = 0.05,
) -> Optional[Dict[str, Any]]:
    """Pick the O/U feed row whose goal line matches the tip (no wrong-line fallback)."""
    ou_key = "FullTimeOu" if is_full_time else "HalfTimeOu"
    best_game: Optional[Dict[str, Any]] = None
    best_diff = float("inf")
    for game in matching_games:
        ou = game.get(ou_key, {})
        goal = ou.get("Goal")
        if not goal or not ou.get("BookieOdds"):
            continue
        # If the tip is a low line (<10), avoid accidentally matching games totals (~20-50).
        if float(target) < 10:
            val_check = _parse_feed_line_value(goal)
            if val_check is None or val_check >= 10:
                continue
        val = _parse_feed_line_value(goal)
        if val is None:
            continue
        diff = abs(val - float(target))
        if diff < best_diff:
            best_diff = diff
            best_game = game
    if best_game is not None and best_diff <= tolerance:
        return best_game
    return None


def _get_best_odds_from_bookie_string(bookie_odds_str: str) -> Optional[tuple]:
    """
    Extract best odds from a BookieOdds string.
    Returns (bookie, odds) tuple or None.
    Format: "ISN=2.260,1.610;BEST=ISN 2.260,IBC 1.580"
    """
    if not bookie_odds_str:
        return None
    
    # Look for BEST section
    sections = bookie_odds_str.split(";")
    for section in sections:
        if section.startswith("BEST="):
            best_part = section.split("=", 1)[1]
            # Format: "ISN 2.260,IBC 1.580" - first is home/over, second is away/under
            parts = best_part.split(",")
            if parts:
                first = parts[0].strip()
                match = re.match(r"(\w+)\s+([\d.]+)", first)
                if match:
                    return (match.group(1), float(match.group(2)))
    return None


def _get_away_odds_from_bookie_string(bookie_odds_str: str) -> Optional[float]:
    """
    Extract best away/under odds (second value) from a BookieOdds BEST section.
    Format: "BEST=PIN 1.826,PIN 2.090" -> returns 2.090
    """
    if not bookie_odds_str:
        return None
    
    sections = bookie_odds_str.split(";")
    for section in sections:
        if section.startswith("BEST="):
            best_part = section.split("=", 1)[1]
            parts = best_part.split(",")
            if len(parts) >= 2:
                second = parts[1].strip()
                match = re.match(r"(\w+)\s+([\d.]+)", second)
                if match:
                    return float(match.group(2))
    return None


def _get_odds_by_position(bookie_odds_str: str, position: int) -> Optional[tuple]:
    """
    Extract odds at a specific position (0=home, 1=away/draw, 2=away for 1X2) from BEST section.
    Returns (bookie, odds) or None.
    """
    if not bookie_odds_str:
        return None
    sections = bookie_odds_str.split(";")
    for section in sections:
        if section.startswith("BEST="):
            parts = section.split("=", 1)[1].split(",")
            if len(parts) > position:
                m = re.match(r"(\w+)\s+([\d.]+)", parts[position].strip())
                if m:
                    return (m.group(1), float(m.group(2)))
    return None


async def resolve_event_and_line(
    client: AsianOddsClient,
    bet_info: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
    *,
    silent: bool = False,
) -> Optional[Dict[str, Any]]:
    """
    Resolve event and line information from AsianOdds API.
    
    AsianOdds structure:
    - GetMatches: Get list of matches
    - GetFeeds: Get odds/lines for matches
    """
    try:
        sport_id = int(bet_info.get("sportId", 1) or 1)
    except (TypeError, ValueError):
        sport_id = 1
    
    # Get bet type preferences from config
    allow_prematch = True
    allow_live = True
    if config:
        allow_prematch = config.get("allow_prematch", config.get("allow_pregame", True))
        allow_live = config.get("allow_live", True)
        
        # Per-sport override
        try:
            bts = config.get("bettype_by_sport") or {}
            if isinstance(bts, dict):
                sport_key = str(bet_info.get("sport") or "").strip().lower()
                if sport_key == "football":
                    sport_key = "soccer"
                if sport_key in {"rugby", "rugbyunion", "rugby union"}:
                    sport_key = "rugby_union"
                override = bts.get(sport_key)
                if isinstance(override, str):
                    o = override.strip().lower()
                    if o == "prematch":
                        allow_prematch, allow_live = True, False
                    elif o == "live":
                        allow_prematch, allow_live = False, True
                    elif o == "both":
                        allow_prematch, allow_live = True, True
        except Exception:
            pass

    # Search all market types; apply live/prematch policy after a match is found.
    # Prioritize based on config: skip disallowed market types to reduce API calls.
    market_types_to_check: list = []
    if allow_prematch:
        market_types_to_check.append(1)  # Today (most common for pre-match tips)
        market_types_to_check.append(2)  # Early
    if allow_live:
        market_types_to_check.insert(0, 0)  # Live first if allowed (time-sensitive)
    if not market_types_to_check:
        market_types_to_check = [1, 2, 0]  # Fallback: check all

    bet_title = bet_info.get("title")

    # Strategy: Use GetFeeds directly to find the match AND get odds in one call.
    # This avoids separate GetMatches calls (saves 1-3 API round-trips).
    # GetFeeds returns HomeTeam, AwayTeam, LeagueName, MatchId, IsLive, etc.
    
    game_id = None
    league_id = None
    market_type_id = None
    matched_match = None
    matching_games: List[Dict[str, Any]] = []

    for mkt_type in market_types_to_check:
        try:
            feeds_data = client.get_feeds(
                sports_type=sport_id,
                market_type_id=mkt_type,
                since=0,  # Force full data (not incremental) to ensure match is found
            )
        except Exception as e:
            if not silent:
                print(f"⚠️ Error fetching feeds for market type {mkt_type}: {e}")
            continue

        result = feeds_data.get("Result", {})
        sports = result.get("Sports", [])

        for sport_data in sports:
            candidates = _collect_matching_games_for_bet(
                sport_data,
                bet_info,
                bet_title,
                allow_live=allow_live,
                allow_prematch=allow_prematch,
            )
            if not candidates:
                continue
            matching_games = candidates
            matched_match = candidates[0]
            game_id = candidates[0].get("MatchId")
            league_id = candidates[0].get("LeagueId")
            market_type_id = candidates[0].get("MarketTypeId", mkt_type)
            break

        if game_id is not None:
            # Save feeds for debug
            try:
                with open("debug_feeds_test.json", "w", encoding="utf-8") as f:
                    json.dump(feeds_data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
            break

    # If not found with league filter, retry without it
    if game_id is None and bet_title and not bet_info.get("_no_retry"):
        for mkt_type in market_types_to_check:
            try:
                feeds_data = client.get_feeds(
                    sports_type=sport_id,
                    market_type_id=mkt_type,
                    since=0,  # Force full data
                )
            except Exception:
                continue

            result = feeds_data.get("Result", {})
            sports = result.get("Sports", [])

            for sport_data in sports:
                candidates = _collect_matching_games_for_bet(
                    sport_data,
                    bet_info,
                    None,
                    allow_live=allow_live,
                    allow_prematch=allow_prematch,
                )
                if not candidates:
                    continue
                matching_games = candidates
                matched_match = candidates[0]
                game_id = candidates[0].get("MatchId")
                league_id = candidates[0].get("LeagueId")
                market_type_id = candidates[0].get("MarketTypeId", mkt_type)
                break

            if game_id is not None:
                try:
                    with open("debug_feeds_test.json", "w", encoding="utf-8") as f:
                        json.dump(feeds_data, f, indent=2, ensure_ascii=False)
                except Exception:
                    pass
                break

    if game_id is None and bet_info.get("_skip_reason"):
        bet_info["_no_retry"] = True
        if not silent:
            ctx = format_bet_context(bet_info)
            ctx_part = f" {ctx}" if ctx else ""
            reason = bet_info.get("_skip_reason") or "Bet type not allowed by config"
            await log_message(f"⚠️ {reason}.{ctx_part}")
        return None

    if game_id is None or league_id is None:
        if not silent:
            ctx = format_bet_context(bet_info)
            ctx_part = f" {ctx}" if ctx else ""
            msg = f"⚠️ No matching event found.{ctx_part}"
            print(msg)
            await log_message(msg)
        return None
    
    bet_info["gameId"] = game_id
    bet_info["eventId"] = game_id  # Alias for compatibility
    bet_info["matchId"] = game_id  # MatchId for feeds lookup (gameId will be updated to line-specific GameId)
    bet_info["leagueId"] = league_id
    bet_info["marketTypeId"] = market_type_id
    
    # Derive gameType from market_type for use by PlaceBet/GetPlacementInfo/duplicate check
    market_lower = (bet_info.get("market_type") or "").lower()
    if market_lower in ("ml match", "ml set 1"):
        bet_info["gameType"] = "X"
    elif market_lower in ("total points match", "team total points match"):
        bet_info["gameType"] = "O"
    else:
        bet_info["gameType"] = "H"
    
    # Derive oddsName for duplicate checking
    if bet_info["gameType"] == "X":
        if bet_info.get("selection_type") == "draw":
            bet_info["oddsName"] = "DrawOdds"
        elif bet_info.get("selection_type") == "home":
            bet_info["oddsName"] = "HomeOdds"
        else:
            bet_info["oddsName"] = "AwayOdds"
    elif bet_info["gameType"] == "O":
        side = (bet_info.get("side") or bet_info.get("selection_type", "OVER")).upper()
        bet_info["oddsName"] = "OverOdds" if side == "OVER" else "UnderOdds"
    else:
        bet_info["oddsName"] = "HomeOdds" if bet_info.get("selection_type") == "home" else "AwayOdds"
    
    # Verify team mapping
    if bet_info.get("selection_type") != "draw" and matched_match:
        ao_home = matched_match.get("HomeTeam", {}).get("Name", "").strip()
        ao_away = matched_match.get("AwayTeam", {}).get("Name", "").strip()
        
        selection_from_bet = bet_info.get("selection", "").strip()
        if not selection_from_bet:
            selection_from_bet = bet_info.get("home", "") if bet_info.get("selection_type") == "home" else bet_info.get("away", "")
        
        selection_norm = _normalize_participant_name(selection_from_bet)
        ao_home_norm = _normalize_participant_name(ao_home)
        ao_away_norm = _normalize_participant_name(ao_away)
        
        if selection_norm and ao_home_norm and _participant_names_match(selection_norm, ao_home_norm):
            bet_info["selection_type"] = "home"
        elif selection_norm and ao_away_norm and _participant_names_match(selection_norm, ao_away_norm):
            bet_info["selection_type"] = "away"
    
    # Extract odds from the already-fetched matching_games (no extra API call needed)
    if matching_games:
        market_lower = (bet_info.get("market_type") or "").lower()
        is_full_time = market_lower not in ("hdp set 1", "ml set 1")
        
        api_odds = None
        handicap = bet_info.get("handicap")
        bookie_odds_str = ""
        preferred_bookie = None
        best_game_for_id = None
        
        if market_lower in ("ml match", "ml set 1"):
            # Moneyline / 1X2 — use first game with non-empty 1X2 odds
            # BEST section format:
            #   Soccer 1X2: "BEST=HomeBookie HomeOdds,DrawBookie DrawOdds,AwayBookie AwayOdds"
            #   Tennis ML:  "BEST=HomeBookie HomeOdds,AwayBookie AwayOdds,Bookie " (no draw)
            selection_type = bet_info.get("selection_type", "home")
            
            for match in matching_games:
                key = "FullTimeOneXTwo" if is_full_time else "HalfTimeOneXTwo"
                one_x_two = match.get(key, {})
                odds_str = one_x_two.get("BookieOdds", "")
                if odds_str:
                    bookie_odds_str = odds_str
                    
                    # Determine if this is a 2-way (tennis) or 3-way (soccer) market
                    # by checking if the BEST section has a valid 3rd value
                    has_draw = False
                    for section in bookie_odds_str.split(";"):
                        if section.startswith("BEST="):
                            parts = section.split("=", 1)[1].split(",")
                            if len(parts) >= 3:
                                third = parts[2].strip()
                                # Valid if it has a bookie name AND odds value
                                if re.match(r"\w+\s+[\d.]+", third):
                                    has_draw = True
                            break
                    
                    # Map selection to position
                    if selection_type == "home":
                        odds_position = 0
                    elif selection_type == "draw":
                        odds_position = 1  # Draw is always position 1 in 3-way
                    elif selection_type == "away":
                        odds_position = 2 if has_draw else 1  # Away is pos 2 (3-way) or pos 1 (2-way)
                    else:
                        odds_position = 0
                    
                    result = _get_odds_by_position(bookie_odds_str, odds_position)
                    if result:
                        preferred_bookie, api_odds = result
                    else:
                        best = _get_best_odds_from_bookie_string(bookie_odds_str)
                        if best:
                            preferred_bookie, api_odds = best
                    best_game_for_id = match
                    break
                
        elif market_lower in ("hdp match", "hdp set 1"):
            sel_type = bet_info.get("selection_type", "home")
            if handicap is not None:
                best_game_for_id = _find_hdp_game(
                    matching_games,
                    float(handicap),
                    sel_type,
                    is_full_time=is_full_time,
                )
            if best_game_for_id:
                hdp_key = "FullTimeHdp" if is_full_time else "HalfTimeHdp"
                hdp = best_game_for_id.get(hdp_key, {})
                bookie_odds_str = hdp.get("BookieOdds", "")
                if sel_type == "home":
                    best = _get_best_odds_from_bookie_string(bookie_odds_str)
                    if best:
                        preferred_bookie, api_odds = best
                else:
                    away_val = _get_away_odds_from_bookie_string(bookie_odds_str)
                    if away_val:
                        api_odds = away_val
                        result_away = _get_odds_by_position(bookie_odds_str, 1)
                        if result_away:
                            preferred_bookie = result_away[0]
                    else:
                        best = _get_best_odds_from_bookie_string(bookie_odds_str)
                        if best:
                            preferred_bookie, api_odds = best
            else:
                unit = bet_info.get("preferred_resulting_unit") or "market"
                bet_info["_skip_reason"] = (
                    f"Could not find HDP line {handicap} in AsianOdds ({unit}) — "
                    "refusing to place on a different handicap/game row"
                )
                bet_info["_no_retry"] = True
                
        elif market_lower in ("total points match", "team total points match"):
            # Over/Under — only use a feed row that matches the tip line (e.g. 3.5, not 36)
            if handicap is not None:
                best_game_for_id = _find_ou_game(
                    matching_games,
                    float(handicap),
                    is_full_time=is_full_time,
                )
            if best_game_for_id:
                ou = best_game_for_id.get("FullTimeOu" if is_full_time else "HalfTimeOu", {})
                bookie_odds_str = ou.get("BookieOdds", "")
                goal_val = _parse_feed_line_value(ou.get("Goal"))
                if goal_val is not None:
                    bet_info["handicap"] = goal_val
                best = _get_best_odds_from_bookie_string(bookie_odds_str)
                if best:
                    preferred_bookie, api_odds = best
            else:
                # If we can't match the requested O/U line, do not fall back to a different market line.
                bet_info["_skip_reason"] = (
                    f"Could not find Total Points line {handicap} in AsianOdds feeds (to avoid placing a wrong 35.x games market)"
                )
                bet_info["_no_retry"] = True
        
        # Store odds info
        bet_info["api_odds"] = api_odds
        bet_info["bookie_odds"] = bookie_odds_str
        bet_info["preferred_bookie"] = preferred_bookie
        
        if best_game_for_id:
            bet_info["gameId"] = best_game_for_id.get("GameId")
            bet_info["eventId"] = best_game_for_id.get("GameId")
            bet_info["matchId"] = game_id  # Keep MatchId for feeds lookup
        
        # Store match info from the resolved line row (not the first feed row)
        display_match = best_game_for_id or matched_match or matching_games[0]
        bet_info["ao_home"] = display_match.get("HomeTeam", {}).get("Name", "")
        bet_info["ao_away"] = display_match.get("AwayTeam", {}).get("Name", "")
        bet_info["is_live"] = display_match.get("IsLive", 0) == 1
        bet_info["start_time"] = display_match.get("StartTime")

        if bet_info.get("_skip_reason") and bet_info.get("_no_retry"):
            if not silent:
                ctx = format_bet_context(bet_info)
                ctx_part = f" {ctx}" if ctx else ""
                await log_message(f"⚠️ {bet_info['_skip_reason']}.{ctx_part}")
            return None
    
    # Get placement info for accurate odds and stake limits
    try:
        from .betting import get_placement_info
        placement_result = get_placement_info(client, bet_info)
        
        # Debug: save placement info
        with open("debug_placement_test.json", "w", encoding="utf-8") as f:
            json.dump(placement_result, f, indent=2, ensure_ascii=False)
        
        placement_data = placement_result.get("Result", {}).get("OddsPlacementData", [])
        if placement_data:
            bet_info["placement_data"] = placement_data
            
            # Find best odds from placement data
            best_placement = None
            best_odds = 0
            for pd in placement_data:
                if pd.get("Rejected"):
                    continue
                price = pd.get("Price", 0)
                # For decimal odds, higher is better
                # For Malaysian odds, need to handle negative values
                if price > best_odds:
                    best_odds = price
                    best_placement = pd
            
            if best_placement:
                bet_info["api_odds"] = best_placement.get("Price")
                bet_info["preferred_bookie"] = best_placement.get("Bookie")
                bet_info["min_stake"] = best_placement.get("MinimumAmount", 1)
                bet_info["max_stake"] = best_placement.get("MaximumAmount", 1000)
                bet_info["currency"] = best_placement.get("Currency", "EUR")
                
                # Build bookie odds string for placement
                odds_parts = []
                for pd in placement_data:
                    if not pd.get("Rejected"):
                        bookie = pd.get("Bookie", "")
                        price = pd.get("Price", 0)
                        if bookie and price:
                            odds_parts.append(f"{bookie}:{price}")
                bet_info["bookie_odds"] = ",".join(odds_parts)
                
    except Exception as e:
        if not silent:
            await log_message(f"⚠️ Error getting placement info: {e}")
    
    return bet_info
