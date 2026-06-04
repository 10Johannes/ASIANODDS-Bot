from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional

from .api import AsianOddsClient
from .resolver import _find_hdp_game, _find_ou_game, _parse_feed_line_value


def enrich_from_odds(client: AsianOddsClient, bet_info: Dict[str, Any]) -> bool:
    """
    Enrich bet_info with current odds data from AsianOdds.
    
    For AsianOdds, we use GetFeeds to get current odds for the match.
    If the resolver already populated odds (api_odds/placement_data), skip the redundant call.
    """
    # If resolver already populated odds successfully, no need to call GetFeeds again
    # (avoids 429 rate limits and "since"-based incremental responses returning empty)
    if bet_info.get("api_odds") or bet_info.get("placement_data"):
        return True
    
    sport_id = bet_info.get("sportId")
    # Use matchId (the match-level identifier) for feeds lookup
    # gameId may be the line-specific GameId after resolution
    match_id = bet_info.get("matchId") or bet_info.get("gameId")
    market_type_id = bet_info.get("marketTypeId", 1)  # 0=Live, 1=Today, 2=Early
    
    if not sport_id or not match_id:
        return False
    
    try:
        try:
            feeds_data = client.get_feeds(
                sports_type=sport_id,
                market_type_id=market_type_id,
                since=0,  # Force full data to ensure match is found
            )
        except Exception as exc:
            if "429" in str(exc) and bet_info.get("handicap") and bet_info.get("odds"):
                print(f"⚠️ GetFeeds rate-limited; using parsed tip line/odds: {exc}")
                return True
            raise
        
        # Debug: save feeds data
        with open("debug_feeds_test.json", "w", encoding="utf-8") as f:
            json.dump(feeds_data, f, indent=2, ensure_ascii=False)
        
        # Find the matching game in feeds
        # Feeds structure: Result.Sports[].MatchGames[]
        # Match by MatchId since GameId varies per handicap line
        # Multiple MatchGames can share the same MatchId (different lines)
        result = feeds_data.get("Result", {})
        sports = result.get("Sports", [])
        
        matching_games = []
        for sport in sports:
            for match in sport.get("MatchGames", []):
                if match.get("MatchId") == match_id:
                    matching_games.append(match)
        
        # If MatchId didn't find games, try matching by team names
        # (AsianOdds can have multiple MatchIds for the same physical match)
        if not matching_games and bet_info.get("home") and bet_info.get("away"):
            from .resolver import _participant_names_match, _strip_api_team_name
            for sport in sports:
                for match in sport.get("MatchGames", []):
                    home_name = _strip_api_team_name(match.get("HomeTeam", {}).get("Name", ""))
                    away_name = _strip_api_team_name(match.get("AwayTeam", {}).get("Name", ""))
                    if (_participant_names_match(home_name, bet_info["home"])
                            and _participant_names_match(away_name, bet_info["away"])):
                        matching_games.append(match)
        
        if not matching_games:
            return False
        
        # Tennis (sportId 3): same Games/Sets row filter as resolver when many lines share MatchId
        preferred_unit = (bet_info.get("preferred_resulting_unit") or "").strip().lower()
        try:
            sid = int(bet_info.get("sportId") or 0)
        except (TypeError, ValueError):
            sid = 0
        if sid == 3 and preferred_unit in ("sets", "games"):
            filtered: list = []
            for m in matching_games:
                h = m.get("HomeTeam", {}).get("Name", "")
                a = m.get("AwayTeam", {}).get("Name", "")
                comb = (h + a).lower()
                if preferred_unit == "sets" and "(sets)" not in comb:
                    continue
                if preferred_unit == "games" and "(games)" not in comb:
                    continue
                filtered.append(m)
            if filtered:
                matching_games = filtered
        
        # Extract odds based on game type
        game_type = bet_info.get("gameType", "H")  # H=Handicap, O=OverUnder, X=1X2
        handicap = bet_info.get("handicap")
        
        if game_type == "H":  # Handicap
            best_game = None
            if handicap is not None:
                best_game = _find_hdp_game(
                    matching_games,
                    float(handicap),
                    bet_info.get("selection_type", "home"),
                    is_full_time=True,
                )

            if best_game:
                hdp = best_game.get("FullTimeHdp", {})
                bookie_odds_str = hdp.get("BookieOdds", "")
                _parse_bookie_odds_to_bet_info(bookie_odds_str, bet_info, "home_away")
                bet_info["bookie_odds"] = bookie_odds_str
                bet_info["gameId"] = best_game.get("GameId")
                bet_info["eventId"] = best_game.get("GameId")
            else:
                return False
                
        elif game_type == "O":  # Over/Under
            best_game = None
            if handicap is not None:
                best_game = _find_ou_game(matching_games, float(handicap), is_full_time=True)

            if best_game:
                ou = best_game.get("FullTimeOu", {})
                bookie_odds_str = ou.get("BookieOdds", "")
                goal_val = _parse_feed_line_value(ou.get("Goal"))
                if goal_val is not None:
                    bet_info["handicap"] = goal_val
                _parse_bookie_odds_to_bet_info(bookie_odds_str, bet_info, "over_under")
                bet_info["bookie_odds"] = bookie_odds_str
                bet_info["gameId"] = best_game.get("GameId")
                bet_info["eventId"] = best_game.get("GameId")
            else:
                # Avoid overwriting the tip line with an unrelated games total (e.g., 35.5-36.5 -> 36)
                return False
                
        elif game_type == "X":  # 1X2 (Moneyline)
            # Use first game with non-empty 1X2 odds
            for game in matching_games:
                x12 = game.get("FullTimeOneXTwo", {})
                bookie_odds_str = x12.get("BookieOdds", "")
                if bookie_odds_str:
                    _parse_bookie_odds_to_bet_info(bookie_odds_str, bet_info, "1x2")
                    bet_info["bookie_odds"] = bookie_odds_str
                    # Update gameId to the correct line-specific GameId
                    bet_info["gameId"] = game.get("GameId")
                    bet_info["eventId"] = game.get("GameId")
                    break
        
        return True
        
    except Exception as e:
        print(f"Error enriching from odds: {e}")
        return False


def _parse_bookie_odds_to_bet_info(bookie_odds_str: str, bet_info: Dict[str, Any], market: str) -> None:
    """
    Parse AsianOdds BookieOdds string and populate bet_info with odds.
    
    Format examples:
    - "PIN=1.456,2.889;BEST=PIN 1.456,PIN 2.889"
    - "SBT=2.109,3.481,3.532;PIN=2.07,3.46,3.58;BEST=SBT 2.109,SBT 3.481,PIN 3.58"
    """
    if not bookie_odds_str:
        return
    
    # Look for BEST section for best available odds
    sections = bookie_odds_str.split(";")
    for section in sections:
        if not section.startswith("BEST="):
            continue
        best_part = section.split("=", 1)[1]
        parts = best_part.split(",")
        
        if market == "home_away" and len(parts) >= 2:
            home_match = re.match(r"(\w+)\s+([\d.]+)", parts[0].strip())
            away_match = re.match(r"(\w+)\s+([\d.]+)", parts[1].strip())
            if home_match:
                bet_info["homeOdds"] = float(home_match.group(2))
            if away_match:
                bet_info["awayOdds"] = float(away_match.group(2))
        elif market == "over_under" and len(parts) >= 2:
            over_match = re.match(r"(\w+)\s+([\d.]+)", parts[0].strip())
            under_match = re.match(r"(\w+)\s+([\d.]+)", parts[1].strip())
            if over_match:
                bet_info["overOdds"] = float(over_match.group(2))
            if under_match:
                bet_info["underOdds"] = float(under_match.group(2))
        elif market == "1x2" and len(parts) >= 3:
            home_match = re.match(r"(\w+)\s+([\d.]+)", parts[0].strip())
            draw_match = re.match(r"(\w+)\s+([\d.]+)", parts[1].strip())
            away_match = re.match(r"(\w+)\s+([\d.]+)", parts[2].strip())
            if home_match:
                bet_info["homeOdds"] = float(home_match.group(2))
            if draw_match:
                bet_info["drawOdds"] = float(draw_match.group(2))
            if away_match:
                bet_info["awayOdds"] = float(away_match.group(2))
        break


def is_duplicate_running_bet(client: AsianOddsClient, bet_info: Dict[str, Any]) -> bool:
    """
    Check if there's already a running bet that matches this selection.
    
    GetRunningBets/GetBets response fields (actual API):
      HomeName, AwayName, BetType, GameType, HdpOrGoal, Odds, Status,
      ReferenceNumber, BetPlacementReference, Bookie, Stake, Currency
    
    BetType values:
      HDP: "HDP Home", "HDP Away"
      1X2: "1" (Home), "2" (Away), "X" (Draw)
      O/U: "Over", "Under"
    """
    home = (bet_info.get("ao_home") or bet_info.get("home") or "").strip().lower()
    away = (bet_info.get("ao_away") or bet_info.get("away") or "").strip().lower()
    selection_type = (bet_info.get("selection_type") or "home").lower()
    game_type = (bet_info.get("gameType") or "H")
    
    # Map to API BetType strings
    if game_type == "H":
        target_bet_types = {"HDP Home"} if selection_type == "home" else {"HDP Away"}
    elif game_type == "O":
        target_bet_types = {"Over"} if selection_type in ("over", "home") else {"Under"}
    elif game_type == "X":
        if selection_type == "draw":
            target_bet_types = {"X", "1X2 Draw"}
        elif selection_type == "home":
            target_bet_types = {"1", "1X2 Home"}
        else:
            target_bet_types = {"2", "1X2 Away"}
    else:
        target_bet_types = set()
    
    try:
        running = client.get_running_bets()
        bets = client.parse_running_bets(running)
        
        for b in bets:
            b_home = (b.get("HomeName") or "").strip().lower()
            b_away = (b.get("AwayName") or "").strip().lower()
            b_bet_type = (b.get("BetType") or "").strip()
            b_status = (b.get("Status") or "").strip().lower()
            
            # Only check running bets
            if b_status and b_status not in ("running", ""):
                continue
            
            # Strip (Sets)/(Games) suffix for comparison
            import re as _re
            b_home_clean = _re.sub(r"\s*\([^)]+\)\s*$", "", b_home).strip()
            b_away_clean = _re.sub(r"\s*\([^)]+\)\s*$", "", b_away).strip()
            
            if not (b_home_clean and b_away_clean and home and away):
                continue
            if b_home_clean not in home and home not in b_home_clean:
                continue
            if b_away_clean not in away and away not in b_away_clean:
                continue
            
            # Match on bet type
            if not target_bet_types or b_bet_type in target_bet_types:
                return True
                
    except Exception:
        pass
    
    return False
