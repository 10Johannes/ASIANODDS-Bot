from __future__ import annotations
import asyncio
import hashlib
import json
import socket
import time
from typing import Any, Dict, Iterable, List, Optional
import re
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Prefer IPv4 — avoids intermittent getaddrinfo failures on Windows with broken IPv6 DNS.
try:
    import urllib3.util.connection as _urllib3_connection

    def _allowed_gai_family() -> int:
        return socket.AF_INET

    _urllib3_connection.allowed_gai_family = _allowed_gai_family
except Exception:
    pass

DEFAULT_LOGIN_URL = "https://webapi.asianodds88.com/AsianOddsService/Login"


def _raise_if_maintenance_response(data: Dict[str, Any], endpoint: str) -> None:
    from .maintenance import AsianOddsMaintenanceError, response_indicates_maintenance

    is_maint, reason = response_indicates_maintenance(data)
    if is_maint:
        raise AsianOddsMaintenanceError(
            reason or f"AsianOdds API maintenance ({endpoint})"
        )


def _build_http_session() -> requests.Session:
    """HTTP session with retries for transient connection/DNS failures."""
    session = requests.Session()
    retry = Retry(
        total=5,
        connect=5,
        read=3,
        backoff_factor=1.5,
        status_forcelist=(429, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def _wrap_network_error(exc: requests.exceptions.RequestException) -> Exception:
    err = str(exc)
    if "getaddrinfo failed" in err or "NameResolutionError" in err or "Failed to resolve" in err:
        return Exception(
            "Cannot reach AsianOdds API (DNS lookup failed for webapi.asianodds88.com). "
            "Check your internet connection, disable VPN/proxy if misconfigured, or try "
            "flushing DNS (ipconfig /flushdns) and using DNS 8.8.8.8 / 1.1.1.1."
        )
    if isinstance(exc, requests.exceptions.Timeout):
        return Exception("AsianOdds API request timed out. Check your network and try again.")
    return exc

_MD5_HEX_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _password_md5_digest(password: str) -> str:
    """
    Return the MD5 hex digest required by the AsianOdds Login endpoint.

    Accepts either a plain-text password (hashed here) or a value that is
    already a 32-character MD5 hex string (used as-is, lowercased).
    """
    value = password.strip()
    if _MD5_HEX_RE.fullmatch(value):
        return value.lower()
    return hashlib.md5(value.encode()).hexdigest()


class AsianOddsClient:
    """
    Client for AsianOdds Web API.
    
    Authentication flow:
    1. Login (get AOToken, AOKey, and service URL)
    2. Register (authorize within 60 seconds)
    3. Use AOToken header for all subsequent requests
    
    Session timeout: 5 minutes of inactivity
    
    Rate limits for GetFeeds:
    - Live Market (0): minimum 5 seconds between calls
    - Today Market (1): minimum 10 seconds between calls
    - Early Market (2): minimum 20 seconds between calls
    """
    
    LOGIN_URL = DEFAULT_LOGIN_URL

    # Minimum interval (seconds) between GetFeeds calls per market type
    _FEEDS_RATE_LIMITS: Dict[int, float] = {
        0: 5.0,   # Live
        1: 10.0,  # Today
        2: 20.0,  # Early
    }
    
    def __init__(
        self,
        username: str,
        password: str,
        session: Optional[requests.Session] = None,
        odds_format: str = "00",  # 00=European/Decimal, MY=Malaysian, HK=Hong Kong
        default_bookies: str = "ALL",
        login_url: Optional[str] = None,
    ) -> None:
        self.username = username.strip()
        self.password = password
        self.password_md5 = _password_md5_digest(password)
        self.session = session or _build_http_session()
        self.login_url = (login_url or self.LOGIN_URL).strip()
        self.odds_format = odds_format
        self.default_bookies = default_bookies
        
        # Auth state
        self._ao_token: Optional[str] = None
        self._ao_key: Optional[str] = None
        self._service_url: Optional[str] = None
        self._last_activity: float = 0
        self._is_registered: bool = False

        # Rate limiting: track last GetFeeds call time per market type
        self._last_feeds_call: Dict[int, float] = {}
        # Short-lived GetFeeds cache to avoid duplicate calls (resolver + enrich)
        self._feeds_cache: Dict[tuple, tuple[float, Dict[str, Any]]] = {}
    
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

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """HTTP request with clearer errors for DNS/network failures."""
        try:
            return self.session.request(method, url, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise _wrap_network_error(exc) from exc

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def _post(self, url: str, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, **kwargs)
    
    def _parse_response(self, resp: requests.Response, endpoint: str) -> Dict[str, Any]:
        """Parse JSON response with error handling."""
        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError as e:
            error_msg = f"Invalid JSON response from {endpoint}: {e}"
            if resp.text:
                error_msg += f"\nResponse text: {resp.text[:500]}"
            raise Exception(error_msg) from e
        
        _raise_if_maintenance_response(data, endpoint)

        if not isinstance(data, dict):
            raise Exception(
                f"Unexpected response type from {endpoint}: expected dict, got {type(data).__name__}"
                f"\nResponse: {str(data)[:500]}"
            )

        # Check for API-level errors
        code = data.get("Code", 0)
        if code < 0:
            result = data.get("Result")
            text_msg = None
            if isinstance(result, dict):
                text_msg = (
                    result.get("TextMessage")
                    or result.get("Message")
                    or result.get("Error")
                    or result.get("Reason")
                    or result.get("detail")
                    or result.get("message")
                )
            elif result is not None:
                text_msg = str(result)
            if not text_msg:
                text_msg = data.get("Message") or data.get("TextMessage") or data.get("error") or data.get("detail")
            if not text_msg:
                try:
                    text_msg = json.dumps(data, ensure_ascii=False)
                except Exception:
                    text_msg = str(data)
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
        
        resp = self._get(
            self.login_url,
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
        
        resp = self._get(
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

    def relogin(self) -> None:
        """Force a fresh login + register, ignoring the client-side TTL.

        The server can expire our session before the 4-minute inactivity window
        the client assumes, so callers that observe a 401 must re-authenticate
        unconditionally instead of relying on ``ensure_authenticated``.
        """
        self.login()
        self.register()
    
    def is_logged_in(self) -> Dict[str, Any]:
        """Check if session is still active. Also resets the 5-minute timeout."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/IsLoggedIn"
        resp = self._get(
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
        resp = self._get(
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
        resp = self._get(
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
        if since is not None and since > 0:
            params["since"] = since
        
        resp = self._get(
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
        if since is not None and since > 0:
            params["since"] = since
        
        resp = self._get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetMatches")
        self._update_activity()
        return data
    
    async def _wait_for_feeds_rate_limit(self, market_type_id: int) -> None:
        """Sleep if needed to respect AsianOdds GetFeeds rate limits."""
        min_interval = self._FEEDS_RATE_LIMITS.get(market_type_id, 10.0)
        last_call = self._last_feeds_call.get(market_type_id, 0)
        elapsed = time.time() - last_call
        if elapsed < min_interval:
            wait = min_interval - elapsed
            await asyncio.sleep(wait)

    async def get_feeds(
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
        
        Rate limits (enforced automatically):
        - Live Market (0): 5 seconds between calls
        - Today Market (1): 10 seconds between calls
        - Early Market (2): 20 seconds between calls
        """
        self.ensure_authenticated()

        cache_key = (sports_type, market_type_id, bookies or self.default_bookies, leagues, odds_format or self.odds_format, since)
        cached = self._feeds_cache.get(cache_key)
        if cached is not None:
            cached_at, cached_data = cached
            ttl = self._FEEDS_RATE_LIMITS.get(market_type_id, 10.0)
            if time.time() - cached_at < ttl:
                return cached_data

        # Respect rate limits before making the request
        await self._wait_for_feeds_rate_limit(market_type_id)
        
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
        if since is not None and since > 0:
            params["since"] = since
        
        # Retry logic for 429 rate limit responses and expired sessions.
        max_retries = 3
        auth_retried = False
        while True:
            resp = None
            for attempt in range(max_retries + 1):
                resp = self._get(
                    url,
                    params=params,
                    headers=self._get_headers(),
                    timeout=60,
                )

                if resp.status_code == 429:
                    if attempt < max_retries:
                        # Back off: use the rate limit interval + extra buffer
                        backoff = self._FEEDS_RATE_LIMITS.get(market_type_id, 10.0) * (attempt + 2)
                        await asyncio.sleep(backoff)
                        continue
                    if cached is not None:
                        self._last_feeds_call[market_type_id] = time.time()
                        return cached[1]
                    break
                break

            # Server rejected our session; force a fresh login and retry once.
            if resp.status_code in (401, 403) and not auth_retried:
                auth_retried = True
                self.relogin()
                continue
            break

        # Record the time of this successful call for rate limiting
        self._last_feeds_call[market_type_id] = time.time()

        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetFeeds")
        self._feeds_cache[cache_key] = (time.time(), data)
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
        timeout: int = 15,
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
        
        resp = self._post(
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
        
        resp = self._post(
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
        
        resp = self._get(
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
        resp = self._get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBets")
        self._update_activity()
        return data
    
    def get_running_bets(self) -> Dict[str, Any]:
        """
        Get currently running bets. Max 50 bets returned.
        
        Response: {"Code": 0, "Data": [{bet}, ...]}
        Bet fields: HomeName, AwayName, BetType, Bookie, Odds, Stake, Status,
                    HdpOrGoal, GameType, LeagueName, KickoffTime, ReferenceNumber,
                    BetPlacementReference, Currency, SportsType, Term
        """
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetRunningBets"
        resp = self._get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetRunningBets")
        self._update_activity()
        return data
    
    def parse_running_bets(self, data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Extract the list of running bets from a GetRunningBets or GetBets response.
        
        Response format: {"Code": 0, "Data": [...]} or {"Code": 300, "Data": null} (empty)
        BetType values:
          HDP: "HDP Home", "HDP Away"
          1X2: "1" (Home), "2" (Away), "X" (Draw)
          O/U: "Over", "Under"
        """
        # Code 300 = empty (no bets)
        if data.get("Code") == 300:
            return []
        # Documented format: {"Code": 0, "Data": [...]}
        if isinstance(data.get("Data"), list):
            return data["Data"]
        # Legacy/fallback: {"Result": {"RunningBets": [...]}}
        result = data.get("Result", {})
        if isinstance(result, dict):
            bets = result.get("RunningBets") or result.get("Bets") or []
            if isinstance(bets, list):
                return bets
        return []
    
    def get_non_running_bets(self) -> Dict[str, Any]:
        """
        Get non-running bets (pending, void, settled, etc.). Max 100 returned.

        See: https://ac88dev.atlassian.net/wiki/spaces/AWA/pages/352157772/2.4.+GetNonRunningBets

        Response: {"Code": 0, "Data": [{bet}, ...]} or {"Code": 300, "Data": null} (empty)
        Status values include: Pending, Won, Lost, Void, Rejected, Cancelled
        """
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetNonRunningBets"
        resp = self._get(
            url,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetNonRunningBets")
        self._update_activity()
        return data

    def get_non_running_bet_list(self) -> List[Dict[str, Any]]:
        """Parsed list from GetNonRunningBets."""
        return self.parse_running_bets(self.get_non_running_bets())

    def get_settled_bet_list(self) -> List[Dict[str, Any]]:
        """Non-running bets with a final result (Won / Lost / Void, etc.)."""
        return filter_bets_by_status(self.get_non_running_bet_list(), settled_only=True)
    
    def get_bet_by_reference(self, reference: str) -> Dict[str, Any]:
        """Get bet details by placement reference."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetBetByReference"
        params = {"reference": reference}
        
        resp = self._get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        
        data = self._parse_response(resp, "GetBetByReference")
        self._update_activity()
        return data

    def get_bet_by_reference_optional(self, reference: str) -> Optional[Dict[str, Any]]:
        """
        Look up a bet by placement reference without raising when it is not found yet.

        Returns the bet dict when Code is 0, otherwise None (e.g. Code -200 while in transit).
        """
        self.ensure_authenticated()

        url = f"{self._service_url}/GetBetByReference"
        params = {"reference": reference}

        resp = self._get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()

        try:
            data = resp.json()
        except requests.exceptions.JSONDecodeError:
            return None

        code = data.get("Code", 0)
        if code != 0:
            return None

        self._update_activity()
        if isinstance(data.get("Data"), dict):
            return data["Data"]
        if isinstance(data.get("Data"), list) and data["Data"]:
            first = data["Data"][0]
            return first if isinstance(first, dict) else None
        result = data.get("Result")
        if isinstance(result, dict):
            return result
        return None

    def find_bet_by_placement_reference(self, reference: str) -> Optional[Dict[str, Any]]:
        """Search running and non-running bet lists for a BetPlacementReference."""
        ref = (reference or "").strip()
        if not ref:
            return None

        bet = self.get_bet_by_reference_optional(ref)
        if bet:
            return bet

        for fetch in (self.get_running_bets, self.get_non_running_bets):
            try:
                payload = fetch()
            except Exception:
                continue
            for row in self.parse_running_bets(payload):
                if str(row.get("BetPlacementReference") or "").strip() == ref:
                    return row
        return None
    
    # =========================================================================
    # Account Methods
    # =========================================================================
    
    def get_account_summary(self) -> Dict[str, Any]:
        """Get account summary (credit, outstanding, P&L)."""
        self.ensure_authenticated()
        
        url = f"{self._service_url}/GetAccountSummary"
        resp = self._get(
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
        resp = self._get(
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
        resp = self._get(
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
        from_date: str,
        to_date: str,
        bookies: Optional[str] = None,
        hide_transactions: bool = False,
    ) -> Dict[str, Any]:
        """
        Get betting statement history (same data as AsianOdds web History).

        Dates must be mm/dd/yyyy strings. See GetHistoryStatement API docs.
        """
        self.ensure_authenticated()

        url = f"{self._service_url}/GetHistoryStatement"
        # Normalize bookies default to 'all' (case-insensitive handling for legacy "ALL")
        bookies_val = (bookies or self.default_bookies or "all").strip()
        if bookies_val.upper() == "ALL":
            bookies_val = "all"
        params: Dict[str, Any] = {
            "from": from_date,
            "to": to_date,
            "bookies": bookies_val,
            "shouldHideTransactionData": "true" if hide_transactions else "false",
        }

        headers = self._get_headers()
        headers["Accept"] = "application/json"

        resp = self._get(
            url,
            params=params,
            headers=headers,
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

        resp = self._get(
            url,
            params=params,
            headers=self._get_headers(),
            timeout=30,
        )
        resp.raise_for_status()

        data = self._parse_response(resp, "GetBetHistorySummary")
        self._update_activity()
        return data


# Final results from GetNonRunningBets (excludes Pending, Rejected, Cancelled).
SETTLED_BET_STATUSES = frozenset(
    {
        "won",
        "lost",
        "void",
        "settled",
        "half won",
        "half lost",
        "half-won",
        "half-lost",
    }
)

NON_RESULT_BET_STATUSES = frozenset(
    {
        "pending",
        "rejected",
        "cancelled",
        "running",
    }
)


def filter_bets_by_status(
    bets: List[Dict[str, Any]],
    *,
    settled_only: bool = False,
    statuses: Optional[Iterable[str]] = None,
    exclude_statuses: Optional[Iterable[str]] = None,
) -> List[Dict[str, Any]]:
    """Filter bet rows by Status (case-insensitive)."""
    if statuses is not None:
        allowed = {str(s).strip().lower() for s in statuses if str(s).strip()}
    elif settled_only:
        allowed = SETTLED_BET_STATUSES
    else:
        allowed = None

    excluded = {str(s).strip().lower() for s in (exclude_statuses or ()) if str(s).strip()}
    if settled_only and not excluded:
        excluded = NON_RESULT_BET_STATUSES

    out: List[Dict[str, Any]] = []
    for bet in bets:
        if not isinstance(bet, dict):
            continue
        status = str(bet.get("Status") or "").strip().lower()
        if allowed is not None and status not in allowed:
            continue
        if status in excluded:
            continue
        out.append(bet)
    return out


def parse_account_summary_fields(result: Any) -> Dict[str, float | str]:
    """
    Normalize GetAccountSummary Result fields.

    AsianOdds returns Credit (not AvailableCredit), TodayPnL, YesterdayPnL, etc.
    """
    if not isinstance(result, dict):
        result = {}

    def _num(*keys: str) -> float:
        for key in keys:
            if key in result and result[key] is not None:
                try:
                    return float(result[key])
                except (TypeError, ValueError):
                    pass
        return 0.0

    credit = _num("Credit", "AvailableCredit")
    return {
        "credit": credit,
        "currency": str(result.get("CreditCurrency") or result.get("Currency") or "").strip(),
        "outstanding": _num("Outstanding"),
        "today_pnl": _num("TodayPnL", "TodayPL"),
        "yesterday_pnl": _num("YesterdayPnL", "YesterdayPL"),
    }


def format_history_statement_date(dt: Any) -> str:
    """Format a date for GetHistoryStatement query params (mm/dd/yyyy)."""
    from datetime import date, datetime

    if isinstance(dt, datetime):
        return dt.strftime("%m/%d/%Y")
    if isinstance(dt, date):
        return dt.strftime("%m/%d/%Y")
    return str(dt).strip()


def _parse_statement_amount(value: Any) -> float:
    if value is None:
        return 0.0
    text = str(value).strip().replace(" ", "").replace(",", "")
    if not text:
        return 0.0
    try:
        return float(text)
    except (TypeError, ValueError):
        return 0.0


def parse_history_statement(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize GetHistoryStatement response.

    Docs: https://ac88dev.atlassian.net/wiki/spaces/AWA/pages/352092205/3.2.+GetHistoryStatement
    """
    result = data.get("Result")
    payload: Dict[str, Any] = data
    if isinstance(result, dict):
        payload = result

    items: list[Dict[str, Any]] = []
    raw_items = payload.get("BetHistoryStatementItems")
    if isinstance(raw_items, list):
        items = [row for row in raw_items if isinstance(row, dict)]

    def _tot(key: str) -> float:
        if payload.get(key) is not None:
            return _parse_statement_amount(payload[key])
        if data.get(key) is not None:
            return _parse_statement_amount(data[key])
        return 0.0

    normalized_items: list[Dict[str, Any]] = []
    for row in items:
        normalized_items.append(
            {
                "date_day": str(row.get("DateDay") or "").strip(),
                "date_day_name": str(row.get("DateDayName") or "").strip(),
                "remark": str(row.get("Remark") or "").strip(),
                "turnover": _parse_statement_amount(row.get("Turnover") or row.get("TurnOver")),
                "win_loss": _parse_statement_amount(row.get("WinLoss")),
                "commission": _parse_statement_amount(row.get("Commission")),
                "balance": _parse_statement_amount(row.get("Balance")),
            }
        )

    return {
        "items": normalized_items,
        "total_commission": _tot("TotalCommission"),
        "total_turnover": _tot("TotalTurnover") or _tot("TotalTurnOver"),
        "total_win_loss": _tot("TotalWinLoss"),
    }


# Backwards compatibility alias
PS3838Client = AsianOddsClient
