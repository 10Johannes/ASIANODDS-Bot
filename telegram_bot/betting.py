from __future__ import annotations
import json
import uuid
from typing import Any, Dict, Optional

from .api import AsianOddsClient


def build_place_bet_payload(bet_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build the payload for AsianOdds PlaceBet API.
    
    AsianOdds uses:
    - GameType: H (Handicap), O (OverUnder), X (1X2/Moneyline)
    - OddsName: HomeOdds, AwayOdds, OverOdds, UnderOdds, DrawOdds
    - IsFullTime: 1 (Full Time), 0 (Half Time)
    """
    market = (bet_info.get("market_type") or "").lower()
    sport_id = bet_info.get("sportId", 1)  # Default to Football (1)
    
    # Determine GameType and OddsName based on market type
    if market in ("ml match", "ml set 1"):
        game_type = "X"  # 1X2/Moneyline
        if bet_info.get("selection_type") == "draw":
            odds_name = "DrawOdds"
        elif bet_info.get("selection_type") == "home":
            odds_name = "HomeOdds"
        else:
            odds_name = "AwayOdds"
    elif market in ("hdp match", "hdp set 1"):
        game_type = "H"  # Handicap
        if bet_info.get("selection_type") == "home":
            odds_name = "HomeOdds"
        else:
            odds_name = "AwayOdds"
    elif market in ("total points match", "team total points match"):
        game_type = "O"  # OverUnder
        side = (bet_info.get("side") or bet_info.get("selection_type", "OVER")).upper()
        if side == "OVER":
            odds_name = "OverOdds"
        else:
            odds_name = "UnderOdds"
    else:
        # Default to Handicap
        game_type = "H"
        if bet_info.get("selection_type") == "home":
            odds_name = "HomeOdds"
        else:
            odds_name = "AwayOdds"
    
    # Determine if full time or half time
    is_full_time = 1
    if market in ("hdp set 1", "ml set 1"):
        is_full_time = 0  # Half time / First period
    
    # Build bookie odds string (e.g., "ISN:-0.84,SBO:-0.75")
    bookie_odds = bet_info.get("bookie_odds", "")
    if not bookie_odds:
        # Build from placement info if available
        placement_data = bet_info.get("placement_data", [])
        if placement_data:
            odds_parts = []
            for pd in placement_data:
                bookie = pd.get("Bookie", "")
                price = pd.get("Price", 0)
                if bookie and price:
                    odds_parts.append(f"{bookie}:{price}")
            bookie_odds = ",".join(odds_parts)
        else:
            # Fallback: use preferred bookie and odds from bet_info
            preferred_bookie = bet_info.get("preferred_bookie", "")
            api_odds = bet_info.get("api_odds", 0)
            if preferred_bookie and api_odds:
                bookie_odds = f"{preferred_bookie}:{api_odds}"
    
    payload = {
        "game_id": bet_info.get("gameId") or bet_info.get("eventId"),
        "game_type": game_type,
        "is_full_time": is_full_time,
        "market_type_id": bet_info.get("marketTypeId", 1),  # 0=Live, 1=Today, 2=Early
        "odds_name": odds_name,
        "sports_type": sport_id,
        "bookie_odds": bookie_odds,
        "amount": bet_info.get("stake", 5),
        "place_bet_id": bet_info.get("uuid") or str(uuid.uuid4())[:40],
    }
    
    return payload


def place_bet(client: AsianOddsClient, bet_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Place a bet using the AsianOdds API.
    
    Steps:
    1. Get placement info (current odds, min/max stake)
    2. Place the bet
    """
    payload = build_place_bet_payload(bet_info)
    
    try:
        result = client.place_bet(
            game_id=payload["game_id"],
            game_type=payload["game_type"],
            is_full_time=payload["is_full_time"],
            market_type_id=payload["market_type_id"],
            odds_name=payload["odds_name"],
            sports_type=payload["sports_type"],
            bookie_odds=payload["bookie_odds"],
            amount=payload["amount"],
            place_bet_id=payload["place_bet_id"],
        )
        return result
    except Exception as e:
        # Log the payload and error for debugging
        print(f"Bet placement error - Payload: {json.dumps(payload, indent=2)}")
        print(f"Bet placement error - Error: {e}")
        raise


def get_placement_info(client: AsianOddsClient, bet_info: Dict[str, Any]) -> Dict[str, Any]:
    """
    Get placement info before placing a bet.
    Returns min/max stake, current odds, etc.
    """
    market = (bet_info.get("market_type") or "").lower()
    sport_id = bet_info.get("sportId", 1)
    
    # Determine GameType and OddsName
    if market in ("ml match", "ml set 1"):
        game_type = "X"
        if bet_info.get("selection_type") == "draw":
            odds_name = "DrawOdds"
        elif bet_info.get("selection_type") == "home":
            odds_name = "HomeOdds"
        else:
            odds_name = "AwayOdds"
    elif market in ("hdp match", "hdp set 1"):
        game_type = "H"
        if bet_info.get("selection_type") == "home":
            odds_name = "HomeOdds"
        else:
            odds_name = "AwayOdds"
    elif market in ("total points match", "team total points match"):
        game_type = "O"
        side = (bet_info.get("side") or bet_info.get("selection_type", "OVER")).upper()
        odds_name = "OverOdds" if side == "OVER" else "UnderOdds"
    else:
        game_type = "H"
        odds_name = "HomeOdds" if bet_info.get("selection_type") == "home" else "AwayOdds"
    
    is_full_time = 1
    if market in ("hdp set 1", "ml set 1"):
        is_full_time = 0
    
    # Get bookies to query
    bookies = bet_info.get("bookies", "ALL")
    
    result = client.get_placement_info(
        game_id=bet_info.get("gameId") or bet_info.get("eventId"),
        game_type=game_type,
        is_full_time=is_full_time,
        bookies=bookies,
        market_type_id=bet_info.get("marketTypeId", 1),
        odds_name=odds_name,
        sports_type=sport_id,
    )
    
    return result
