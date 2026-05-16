from __future__ import annotations
import json
from typing import Any, Dict, Optional

from .api import AsianOddsClient


def enrich_from_odds(client: AsianOddsClient, bet_info: Dict[str, Any]) -> bool:
    """
    Enrich bet_info with current odds data from AsianOdds.
    
    For AsianOdds, we use GetFeeds to get current odds for the match.
    """
    sport_id = bet_info.get("sportId")
    game_id = bet_info.get("gameId")
    market_type_id = bet_info.get("marketTypeId", 1)  # 0=Live, 1=Today, 2=Early
    
    if not sport_id or not game_id:
        return False
    
    try:
        feeds_data = client.get_feeds(
            sports_type=sport_id,
            market_type_id=market_type_id,
        )
        
        # Debug: save feeds data
        with open("debug_feeds_test.json", "w", encoding="utf-8") as f:
            json.dump(feeds_data, f, indent=2, ensure_ascii=False)
        
        # Find the matching game in feeds
        result = feeds_data.get("Result", {})
        sports = result.get("Sports", [])
        
        found_game = None
        for sport in sports:
            for league in sport.get("Leagues", []):
                for match in league.get("Matches", []):
                    if match.get("GameId") == game_id:
                        found_game = match
                        break
                if found_game:
                    break
            if found_game:
                break
        
        if not found_game:
            return False
        
        # Extract odds based on market type
        market_type = (bet_info.get("market_type") or "").lower()
        game_type = bet_info.get("gameType", "H")  # H=Handicap, O=OverUnder, X=1X2
        
        # Get the appropriate odds from the game
        if game_type == "H":  # Handicap
            hdp = found_game.get("Hdp", {})
            bet_info["homeOdds"] = hdp.get("HomeOdds")
            bet_info["awayOdds"] = hdp.get("AwayOdds")
            bet_info["handicap"] = hdp.get("Hdp")
        elif game_type == "O":  # Over/Under
            ou = found_game.get("OU", {})
            bet_info["overOdds"] = ou.get("OverOdds")
            bet_info["underOdds"] = ou.get("UnderOdds")
            bet_info["handicap"] = ou.get("Line")
        elif game_type == "X":  # 1X2 (Moneyline)
            x12 = found_game.get("X12", {})
            bet_info["homeOdds"] = x12.get("HomeOdds")
            bet_info["awayOdds"] = x12.get("AwayOdds")
            bet_info["drawOdds"] = x12.get("DrawOdds")
        
        # Store bookie info
        bet_info["bookies"] = found_game.get("Bookies", [])
        
        return True
        
    except Exception as e:
        print(f"Error enriching from odds: {e}")
        return False


def is_duplicate_running_bet(client: AsianOddsClient, bet_info: Dict[str, Any]) -> bool:
    """
    Check if there's already a running bet that matches this selection.
    Uses GetRunningBets to check for duplicates.
    """
    target_game_id = bet_info.get("gameId")
    target_game_type = bet_info.get("gameType", "H")  # H=Handicap, O=OverUnder, X=1X2
    target_odds_name = bet_info.get("oddsName", "")  # HomeOdds, AwayOdds, OverOdds, UnderOdds
    market_type = (bet_info.get("market_type") or "").lower()
    
    # Debug logging for Draw bets
    is_draw = bet_info.get("selection_type") == "draw"
    if is_draw:
        print(f"DEBUG: Checking for Draw bet duplicate - gameId: {target_game_id}, gameType: {target_game_type}, oddsName: {target_odds_name}")
    
    # 1) Check running bets
    try:
        running = client.get_running_bets()
        result = running.get("Result", {})
        bets = result.get("RunningBets", [])
        
        for b in bets:
            if is_draw:
                print(f"DEBUG: Running bet check - found bet: gameId={b.get('GameId')}, gameType={b.get('GameType')}, oddsName={b.get('OddsName')}")
            
            # Match on GameId and GameType
            if b.get("GameId") == target_game_id and b.get("GameType") == target_game_type:
                # For 1X2 (moneyline), also check the specific selection (Home/Away/Draw)
                if target_game_type == "X":
                    if b.get("OddsName") == target_odds_name:
                        if is_draw:
                            print(f"DEBUG: Found duplicate Draw bet in running bets!")
                        return True
                else:
                    # For Handicap/OU, match on OddsName
                    if b.get("OddsName") == target_odds_name:
                        return True
    except Exception as e:
        if is_draw:
            print(f"DEBUG: Running bet check failed: {e}")
        pass
    
    # 2) Check all bets (includes pending/recent)
    try:
        all_bets = client.get_bets()
        result = all_bets.get("Result", {})
        bets = result.get("Bets", [])
        
        for b in bets:
            if is_draw:
                print(f"DEBUG: All bets check - found bet: gameId={b.get('GameId')}, gameType={b.get('GameType')}, oddsName={b.get('OddsName')}")
            
            # Match on GameId and GameType
            if b.get("GameId") == target_game_id and b.get("GameType") == target_game_type:
                # For 1X2 (moneyline), also check the specific selection
                if target_game_type == "X":
                    if b.get("OddsName") == target_odds_name:
                        if is_draw:
                            print(f"DEBUG: Found duplicate Draw bet in all bets!")
                        return True
                else:
                    if b.get("OddsName") == target_odds_name:
                        return True
    except Exception as e:
        if is_draw:
            print(f"DEBUG: All bets check failed: {e}")
        pass
    
    return False
