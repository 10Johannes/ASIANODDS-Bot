from __future__ import annotations
import hashlib
import json
import time
from typing import Any, Dict, Optional, List
import requests


class AsianOddsClient:
    """
    Client for AsianOdds Web API.
    
    Authentication flow:
    1. Login (get AOToken, AOKey, and service URL)
    2. Register (authorize within 60 seconds)
    3. Use AOToken header for all subsequent requests
    
    Session timeout: 5 minutes of inactivity
    """
    
    LOGIN_URL = "https://webapi.asianodds88.com/AsianOddsService/Login"
    
    def __init__(
        self,
        username: str,
        password: str,
        session: Optional[requests.Session] = None,
        odds_format: str = "00",  # 00=European/Decimal, MY=Malaysian, HK=Hong Kong
        default_bookies: str = "ALL",
    ) -> None:
        self.username = username
        self.password = password
        self.password_md5 = hashlib.md5(password.encode()).hexdigest()
        self.session = session or requests.Session()
        self.odds_format = odds_format
        self.default_bookies = default_bookies
        
        # Auth state
        self._ao_token: Optional[str] = None
        self._ao_key: Optional[str] = None
        self._service_url: Optional[str] = None
        self._last_activity: float = 0
        self._is_registered: bool = False
    
    @property
    def is_authenticated(self) -> bool:
        """Check if we have valid auth tokens and haven't timed out (5 min inactivity)."""
        if not self._ao_token or not self._service_url or not self._is_registered:
            return False
        # Check for 5-minute inactivity timeout
        if time.time() - self._last_activity > 240:  # 4 minutes to be safe
            return False
        return True
    
    def _update_activity(self) -> None:
        """Update last activity timestamp."""
        self._last_activity = time.time()
    
    def _get_headers(self, include_auth: bool = True) -> Dict[str, str]:
        """Build request headers."""
        headers = {"Accept": "application/json"}
        if include_auth and self._ao_token:
            headers["AOToken"] = self._ao_token
        if include_auth and self._ao_key:
            headers["AOKey"] = self._ao_key
        return headers
    
    def _parse_response(self, resp: requests.Response, endpoint: str) -> Dict[str, Any]:
        """Parse JSON response with error handling."""
        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError as e:
            error_msg = f"Invalid JSON response from {endpoint}: {e}"
            if resp.text:
                error_msg += f"\nResponse text: {resp.text[:500]}"
            raise Exception(error_msg) from e
        
        # Check for API-level errors
        code = data.get("Code", 0)
        if code < 0:
            text_msg = data.get("Result", {}).get("TextMessage") if isinstance(data.get("Result"), dict) else str(data.get("Result"))
            raise Exception(f"AsianOdds API error (Code {code}): {text_msg}")
        
        return data
    
    def login(self) -> Dict[str, Any]:
        """
        Authenticate with AsianOdds and get tokens.
        Must call register() within 60 seconds after login.
        """
        params = {
            "username": self.username,
            "password": self.password_md5,
        }
        
        resp = self.session.get(
            self.LOGIN_URL,
            params=params,
            headers={"Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "Login")
        result = data.get("Result", {})
        
        if not result.get("SuccessfulLogin"):
            raise Exception(f"Login failed: {result.get('TextMessage', 'Unknown error')}")
        
        self._ao_token = result.get("Token")
        self._ao_key = result.get("Key")
        self._service_url = result.get("Url", "").rstrip("/")
        self._is_registered = False
        
        if not self._ao_token or not self._service_url:
            raise Exception("Login succeeded but missing Token or URL in response")
        
        self._update_activity()
        return data
    
    def register(self) -> Dict[str, Any]:
        """
        Complete authorization after login. Must be called within 60 seconds of login.
        """
        if not self._service_url or not self._ao_token or not self._ao_key:
            raise Exception("Must login before registering")
        
        url = f"{self._service_url}/Register"
        params = {"username": self.username}
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "Register")
        result = data.get("Result", {})
        
        if not result.get("Success"):
            raise Exception(f"Registration failed: {result.get('TextMessage', 'Unknown error')}")
        
        self._is_registered = True
        self._update_activity()
        return data
    
    def ensure_authenticated(self) -> None:
        """Ensure we have a valid authenticated session."""
        if not self.is_authenticated:
            self.login()
            self.register()
    
    def is_logged_in(self) -> Dict[str, Any]:
        """Check if session is still active. Also resets the 5-minute timeout."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/IsLoggedIn"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "IsLoggedIn")
        self._update_activity()
        return data
    
    def logout(self) -> Dict[str, Any]:
        """Logout and invalidate the session."""
        if not self._service_url:
            return {"Code": 0, "Result": {"Success": True}}
        
        url = f"{self._service_url}/Logout"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "Logout")
        
        # Clear auth state
        self._ao_token = None
        self._ao_key = None
        self._service_url = None
        self._is_registered = False
        
        return data
    
    # =========================================================================
    # Betting Methods
    # =========================================================================
    
    def get_sports(self) -> Dict[str, Any]:
        """Get list of available sports."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetSports"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetSports")
        self._update_activity()
        return data
    
    def get_leagues(
        self,
        *,
        sports_type: Optional[int] = None,
        market_type_id: int = 1,  # 0=Live, 1=Today, 2=Early
        bookies: Optional[str] = None,
        since: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get leagues for a sport."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetLeagues"
        params: Dict[str, Any] = {
            "marketTypeId": market_type_id,
        }
        if sports_type is not None:
            params["sportsType"] = sports_type
        if bookies:
            params["bookies"] = bookies
        elif self.default_bookies:
            params["bookies"] = self.default_bookies
        if since is not None:
            params["since"] = since
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetLeagues")
        self._update_activity()
        return data
    
    def get_matches(
        self,
        *,
        sports_type: Optional[int] = None,
        market_type_id: int = 1,  # 0=Live, 1=Today, 2=Early
        bookies: Optional[str] = None,
        leagues: Optional[str] = None,
        since: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Get matches/fixtures."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetMatches"
        params: Dict[str, Any] = {
            "marketTypeId": market_type_id,
        }
        if sports_type is not None:
            params["sportsType"] = sports_type
        if bookies:
            params["bookies"] = bookies
        elif self.default_bookies:
            params["bookies"] = self.default_bookies
        if leagues:
            params["leagues"] = leagues
        if since is not None:
            params["since"] = since
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetMatches")
        self._update_activity()
        return data
    
    def get_feeds(
        self,
        *,
        sports_type: int,
        market_type_id: int = 1,  # 0=Live, 1=Today, 2=Early
        bookies: Optional[str] = None,
        leagues: Optional[str] = None,
        odds_format: Optional[str] = None,
        since: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get odds/feeds for matches.
        
        Rate limits:
        - Live Market: 5 seconds
        - Today Market: 10 seconds
        - Early Market: 20 seconds
        """
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetFeeds"
        params: Dict[str, Any] = {
            "sportsType": sports_type,
            "marketTypeId": market_type_id,
            "oddsFormat": odds_format or self.odds_format,
        }
        if bookies:
            params["bookies"] = bookies
        elif self.default_bookies:
            params["bookies"] = self.default_bookies
        if leagues:
            params["leagues"] = leagues
        if since is not None:
            params["since"] = since
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetFeeds")
        self._update_activity()
        return data
    
    def get_placement_info(
        self,
        *,
        game_id: int,
        game_type: str,  # H=Handicap, O=OverUnder, X=1X2
        is_full_time: int,  # 1=FullTime, 0=HalfTime
        bookies: str,
        market_type_id: int,
        odds_format: Optional[str] = None,
        odds_name: str,  # HomeOdds, AwayOdds, OverOdds, UnderOdds, DrawOdds
        sports_type: int,
        timeout: int = 60,
    ) -> Dict[str, Any]:
        """
        Get placement info (min/max stake, current odds) before placing a bet.
        """
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetPlacementInfo"
        body = {
            "GameId": game_id,
            "GameType": game_type,
            "IsFullTime": is_full_time,
            "Bookies": bookies,
            "MarketTypeId": market_type_id,
            "OddsFormat": odds_format or self.odds_format,
            "OddsName": odds_name,
            "SportsType": sports_type,
            "Timeout": timeout,
        }
        
        resp = self.session.post(
            url,
            json=body,
            headers=self._get_headers(),
            timeout=timeout + 10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetPlacementInfo")
        self._update_activity()
        return data
    
    def place_bet(
        self,
        *,
        game_id: int,
        game_type: str,  # H=Handicap, O=OverUnder, X=1X2
        is_full_time: int,  # 1=FullTime, 0=HalfTime
        market_type_id: int,
        odds_format: Optional[str] = None,
        odds_name: str,  # HomeOdds, AwayOdds, OverOdds, UnderOdds, DrawOdds
        sports_type: int,
        bookie_odds: str,  # e.g., "ISN:-0.84,SBO:-0.75"
        amount: float,
        place_bet_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Place a single bet.
        
        Note: It's recommended to call get_placement_info() before placing a bet.
        """
        self.ensure_authenticated()
        
        url = f"{self._service_url}/PlaceBet"
        body: Dict[str, Any] = {
            "GameId": game_id,
            "GameType": game_type,
            "IsFullTime": is_full_time,
            "MarketTypeId": market_type_id,
            "OddsFormat": odds_format or self.odds_format,
            "OddsName": odds_name,
            "SportsType": sports_type,
            "BookieOdds": bookie_odds,
            "Amount": amount,
        }
        if place_bet_id:
            body["PlaceBetId"] = place_bet_id
        
        resp = self.session.post(
            url,
            json=body,
            headers=self._get_headers(),
            timeout=60,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "PlaceBet")
        self._update_activity()
        
        # Check for placement errors
        result = data.get("Result", {})
        if isinstance(result, dict) and result.get("PlacementData") is None and data.get("Code") != 0:
            raise Exception(f"Bet placement failed: {result}")
        
        return data
    
    def get_market_count(
        self,
        *,
        sports_type: int,
        market_type_id: int = 1,
    ) -> Dict[str, Any]:
        """Get count of available markets."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetMarketCount"
        params = {
            "sportsType": sports_type,
            "marketTypeId": market_type_id,
        }
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetMarketCount")
        self._update_activity()
        return data
    
    # =========================================================================
    # Bet Details Methods
    # =========================================================================
    
    def get_bets(self) -> Dict[str, Any]:
        """Get all bets (running and non-running). Max 150 bets returned."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetBets"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBets")
        self._update_activity()
        return data
    
    def get_running_bets(self) -> Dict[str, Any]:
        """Get currently running bets. Max 50 bets returned."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetRunningBets"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetRunningBets")
        self._update_activity()
        return data
    
    def get_non_running_bets(self) -> Dict[str, Any]:
        """Get non-running bets (pending, void, etc.)."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetNonRunningBets"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetNonRunningBets")
        self._update_activity()
        return data
    
    def get_bet_by_reference(self, reference: str) -> Dict[str, Any]:
        """Get bet details by placement reference."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetBetByReference"
        params = {"reference": reference}
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBetByReference")
        self._update_activity()
        return data
    
    # =========================================================================
    # Account Methods
    # =========================================================================
    
    def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary (credit, outstanding, P&L)."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetAccountSummary"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetAccountSummary")
        self._update_activity()
        return data
    
    def get_balance(self) -> Dict[str, Any]:
        """Alias for get_account_summary for compatibility."""
        return self.get_account_summary()
    
    def get_bookies(self) -> Dict[str, Any]:
        """Get list of available bookies for the account."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetBookies"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBookies")
        self._update_activity()
        return data
    
    def get_user_information(self) -> Dict[str, Any]:
        """Get user account information."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetUserInformation"
        resp = self.session.get(
            url,
            headers=self._get_headers(),
            timeout=10,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetUserInformation")
        self._update_activity()
        return data
    
    def get_history_statement(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get account history statement."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetHistoryStatement"
        params: Dict[str, Any] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetHistoryStatement")
        self._update_activity()
        return data
    
    def get_bet_history_summary(
        self,
        *,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Get bet history summary."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetBetHistorySummary"
        params: Dict[str, Any] = {}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        
        resp = self.session.get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBetHistorySummary")
        self._update_activity()
        return data


# Backwards compatibility alias
PS3838Client = AsianOddsClient
