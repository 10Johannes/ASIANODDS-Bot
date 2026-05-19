from __future__ import annotations
import json
import uuid
from typing import Any, Dict, Optional

from .api import AsianOddsClient


def _get_odds_index(odds_name: str, game_type: str) -> int:
    """
    Get the index of the odds value in the BookieOdds string based on selection.
    
    For Handicap (H): "BOOKIE=HomeOdds,AwayOdds" -> HomeOdds=0, AwayOdds=1
    For OverUnder (O): "BOOKIE=OverOdds,UnderOdds" -> OverOdds=0, UnderOdds=1
    For 1X2 (X): "BOOKIE=HomeOdds,DrawOdds,AwayOdds" -> HomeOdds=0, DrawOdds=1, AwayOdds=2
    """
    if game_type == "X":
        if odds_name == "HomeOdds":
            return 0
        elif odds_name == "DrawOdds":
            return 1
        elif odds_name == "AwayOdds":
            return 2
    elif game_type == "H":
        if odds_name == "HomeOdds":
            return 0
        elif odds_name == "AwayOdds":
            return 1
    elif game_type == "O":
        if odds_name == "OverOdds":
            return 0
        elif odds_name == "UnderOdds":
            return 1
    return 0


def _apply_confidence_filter(bookie_odds: str, confidence: str) -> str:
    """
    Filter bookie odds based on confidence level.
    
    - "high": use only the highest odds bookie (best price, higher rejection risk)
    - "medium": use the middle odds bookie
    - "low": use only the lowest odds bookie (safest, most likely accepted)
    
    Input format: "PIN:2.14,SBT:2.18,IBC:2.10"
    Output: single "BOOKIE:ODDS" based on confidence selection
    """
    if not bookie_odds or ":" not in bookie_odds:
        return bookie_odds
    
    confidence = (confidence or "high").strip().lower()
    
    # Parse all bookie:odds pairs
    pairs = []
    for part in bookie_odds.split(","):
        part = part.strip()
        if ":" not in part:
            continue
        bookie, odds_str = part.split(":", 1)
        try:
            odds_val = float(odds_str.strip())
            pairs.append((bookie.strip(), odds_val))
        except (ValueError, TypeError):
            continue
    
    if not pairs:
        return bookie_odds
    
    # Sort by odds value (ascending)
    pairs.sort(key=lambda x: x[1])
    
    if confidence == "low":
        # Lowest odds = safest
        selected = pairs[0]
    elif confidence == "medium":
        # Middle odds
        mid_idx = len(pairs) // 2
        selected = pairs[mid_idx]
    else:
        # "high" = highest odds (default)
        selected = pairs[-1]
    
    return f"{selected[0]}:{selected[1]}"


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
    
    # Build bookie odds string for PlaceBet API
    # PlaceBet expects format: "BOOKIE:ODDS,BOOKIE:ODDS" (e.g., "PIN:2.14,SBT:2.18")
    bookie_odds = ""
    
    # First priority: use placement_data from GetPlacementInfo (already in correct format)
    placement_data = bet_info.get("placement_data", [])
    if placement_data:
        odds_parts = []
        for pd in placement_data:
            bookie = pd.get("Bookie", "")
            price = pd.get("Price", 0)
            if bookie and price and not pd.get("Rejected"):
                odds_parts.append(f"{bookie}:{price}")
        bookie_odds = ",".join(odds_parts)
    
    # Second priority: parse raw BookieOdds from feeds and extract correct selection odds
    if not bookie_odds:
        raw_bookie_odds = bet_info.get("bookie_odds", "")
        if raw_bookie_odds and "=" in raw_bookie_odds:
            # Raw feeds format: "PIN=1.534,2.550;SBT=1.6,2.3;BEST=PIN 1.534,PIN 2.550"
            # or for 1X2: "PIN=3.57,2.14,3.30;SBT=3.633,2.18,3.144;BEST=..."
            # Need to extract the correct odds position based on odds_name
            odds_index = _get_odds_index(odds_name, game_type)
            odds_parts = []
            for section in raw_bookie_odds.split(";"):
                if "=" not in section or section.startswith("BEST"):
                    continue
                bookie_name, values_str = section.split("=", 1)
                values = values_str.split(",")
                if odds_index < len(values):
                    try:
                        price = float(values[odds_index].strip())
                        odds_parts.append(f"{bookie_name}:{price}")
                    except (ValueError, IndexError):
                        pass
            bookie_odds = ",".join(odds_parts)
        elif raw_bookie_odds and ":" in raw_bookie_odds and "=" not in raw_bookie_odds:
            # Already in correct format (BOOKIE:ODDS)
            bookie_odds = raw_bookie_odds
    
    # Third priority: use preferred bookie and api_odds
    if not bookie_odds:
        preferred_bookie = bet_info.get("preferred_bookie", "")
        api_odds = bet_info.get("api_odds", 0)
        if preferred_bookie and api_odds:
            bookie_odds = f"{preferred_bookie}:{api_odds}"
    
    # Apply confidence filter: select which bookie odds to use
    # high = best (highest) odds, medium = middle, low = lowest (safest)
    bookie_odds = _apply_confidence_filter(bookie_odds, bet_info.get("_confidence", "high"))
    
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
        # Save the full response for debugging
        try:
            with open("debug_placebet_response.json", "w", encoding="utf-8") as f:
                json.dump({"payload": payload, "response": result}, f, indent=2, ensure_ascii=False)
        except Exception:
            pass
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
    
    # Get bookies to query - extract from bookie_odds string or use ALL
    bookies = bet_info.get("bookies", "ALL")
    if isinstance(bookies, list):
        bookies = ",".join(bookies) if bookies else "ALL"
    if not bookies:
        bookies = "ALL"
    
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
