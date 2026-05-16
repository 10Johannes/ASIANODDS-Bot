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
    sport_id = bet_info.get("sportId", 1)
    
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

    # Determine which market types to query
    market_types_to_check = []
    if allow_live:
        market_types_to_check.append(0)  # Live
    if allow_prematch:
        market_types_to_check.append(1)  # Today
        market_types_to_check.append(2)  # Early
    
    if not market_types_to_check:
        market_types_to_check = [1]  # Default to Today
    
    bet_title = bet_info.get("title")
    game_id = None
    league_id = None
    market_type_id = None
    matched_match = None
    
    # Try each market type
    for mkt_type in market_types_to_check:
        try:
            matches_data = client.get_matches(
                sports_type=sport_id,
                market_type_id=mkt_type,
            )
            
            # Debug: save matches data
            with open("debug_matches_test.json", "w", encoding="utf-8") as f:
                json.dump(matches_data, f, indent=2, ensure_ascii=False)
            
            result = matches_data.get("Result", {})
            event_sports = result.get("EventSportsTypes", [])
            
            for sport_data in event_sports:
                if sport_data.get("SportsType") != sport_id:
                    continue
                
                events = sport_data.get("Events", [])
                for event in events:
                    # Check league name match
                    league_name = event.get("LeagueName", "")
                    if bet_title and not _league_name_matches(bet_title, league_name):
                        continue
                    
                    # Check team names match
                    home_name = event.get("Home", "")
                    away_name = event.get("Away", "")
                    
                    if (
                        _participant_names_match(home_name, bet_info["home"]) and
                        _participant_names_match(away_name, bet_info["away"])
                    ):
                        # Check live status
                        is_live = event.get("IsLive", 0) == 1
                        
                        if is_live and not allow_live:
                            continue
                        if not is_live and not allow_prematch:
                            continue
                        
                        game_id = event.get("MatchId")
                        league_id = event.get("LeagueId")
                        market_type_id = event.get("MarketTypeId", mkt_type)
                        matched_match = event
                        break
                
                if game_id:
                    break
            
            if game_id:
                break
                
        except Exception as e:
            if not silent:
                await log_message(f"⚠️ Error fetching matches for market type {mkt_type}: {e}")
            continue
    
    if not game_id or not league_id:
        if not silent:
            ctx = format_bet_context(bet_info)
            ctx_part = f" {ctx}" if ctx else ""
            msg = f"⚠️ No matching event found.{ctx_part}"
            print(msg)
            await log_message(msg)
        return None
    
    bet_info["gameId"] = game_id
    bet_info["eventId"] = game_id  # Alias for compatibility
    bet_info["leagueId"] = league_id
    bet_info["marketTypeId"] = market_type_id
    
    # Verify team mapping
    if bet_info.get("selection_type") != "draw" and matched_match:
        ao_home = matched_match.get("Home", "").strip()
        ao_away = matched_match.get("Away", "").strip()
        
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
    
    # Now get the feeds/odds for this match
    try:
        feeds_data = client.get_feeds(
            sports_type=sport_id,
            market_type_id=market_type_id,
        )
        
        # Debug: save feeds data
        with open("debug_feeds_test.json", "w", encoding="utf-8") as f:
            json.dump(feeds_data, f, indent=2, ensure_ascii=False)
        
        result = feeds_data.get("Result", {})
        sports = result.get("Sports", [])
        
        for sport_data in sports:
            match_games = sport_data.get("MatchGames", [])
            
            for match in match_games:
                if match.get("GameId") != game_id:
                    continue
                
                # Found our match - extract odds based on market type
                market_lower = (bet_info.get("market_type") or "").lower()
                is_full_time = market_lower not in ("hdp set 1", "ml set 1")
                
                api_odds = None
                handicap = bet_info.get("handicap")
                bookie_odds_str = ""
                preferred_bookie = None
                
                if market_lower in ("ml match", "ml set 1"):
                    # Moneyline / 1X2
                    if is_full_time:
                        one_x_two = match.get("FullTimeOneXTwo", {})
                    else:
                        one_x_two = match.get("HalfTimeOneXTwo", {})
                    
                    bookie_odds_str = one_x_two.get("BookieOdds", "")
                    
                    # Get best odds
                    best = _get_best_odds_from_bookie_string(bookie_odds_str)
                    if best:
                        preferred_bookie, api_odds = best
                        
                elif market_lower in ("hdp match", "hdp set 1"):
                    # Handicap/Spread
                    if is_full_time:
                        hdp = match.get("FullTimeHdp", {})
                    else:
                        hdp = match.get("HalfTimeHdp", {})
                    
                    bookie_odds_str = hdp.get("BookieOdds", "")
                    handicap_from_feed = hdp.get("Handicap")
                    
                    if handicap_from_feed:
                        try:
                            bet_info["handicap"] = float(handicap_from_feed)
                        except ValueError:
                            pass
                    
                    # Get best odds
                    best = _get_best_odds_from_bookie_string(bookie_odds_str)
                    if best:
                        preferred_bookie, api_odds = best
                        
                elif market_lower in ("total points match", "team total points match"):
                    # Over/Under
                    if is_full_time:
                        ou = match.get("FullTimeOu", {})
                    else:
                        ou = match.get("HalfTimeOu", {})
                    
                    bookie_odds_str = ou.get("BookieOdds", "")
                    goal_from_feed = ou.get("Goal")
                    
                    if goal_from_feed:
                        try:
                            bet_info["handicap"] = float(goal_from_feed)
                        except ValueError:
                            pass
                    
                    # Get best odds
                    best = _get_best_odds_from_bookie_string(bookie_odds_str)
                    if best:
                        preferred_bookie, api_odds = best
                
                # Store odds info
                bet_info["api_odds"] = api_odds
                bet_info["bookie_odds"] = bookie_odds_str
                bet_info["preferred_bookie"] = preferred_bookie
                bet_info["bookies"] = match.get("Bookies", [])
                
                # Store match info for reference
                bet_info["ao_home"] = match.get("HomeTeam", {}).get("Name", "")
                bet_info["ao_away"] = match.get("AwayTeam", {}).get("Name", "")
                bet_info["is_live"] = match.get("IsLive", 0) == 1
                bet_info["start_time"] = match.get("StartTime")
                
                break
        
    except Exception as e:
        if not silent:
            await log_message(f"⚠️ Error fetching feeds: {e}")
    
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
