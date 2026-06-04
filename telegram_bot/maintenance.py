from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from .api import AsianOddsClient


class AsianOddsMaintenanceError(Exception):
    """Raised when the API indicates scheduled maintenance or downtime."""


_MAINTENANCE_PATTERNS = (
    r"\bmaintenance\b",
    r"\bunder\s+maintenance\b",
    r"\bsystem\s+down\b",
    r"\bscheduled\s+downtime\b",
    r"\bservice\s+unavailable\b",
    r"\btemporarily\s+unavailable\b",
    r"\bapi\s+maintenance\b",
    r"\bserver\s+maintenance\b",
)

_MAINTENANCE_CODE_HINTS = frozenset({-503, -999, -1000})


def text_indicates_maintenance(text: Optional[str]) -> bool:
    if not text:
        return False
    lowered = str(text).strip().lower()
    if not lowered:
        return False
    return any(re.search(pat, lowered) for pat in _MAINTENANCE_PATTERNS)


def response_indicates_maintenance(data: Any) -> Tuple[bool, str]:
    """Return (is_maintenance, reason) from a parsed AsianOdds JSON body."""
    if not isinstance(data, dict):
        return False, ""

    code = data.get("Code")
    try:
        code_int = int(code) if code is not None else 0
    except (TypeError, ValueError):
        code_int = 0

    parts = [str(data.get("Message") or "")]
    result = data.get("Result")
    if isinstance(result, dict):
        parts.append(str(result.get("TextMessage") or ""))
        parts.append(str(result.get("Message") or ""))
    elif result is not None:
        parts.append(str(result))

    combined = " ".join(p for p in parts if p).strip()
    if text_indicates_maintenance(combined):
        return True, combined or "API maintenance"

    if code_int in _MAINTENANCE_CODE_HINTS:
        return True, combined or f"API code {code_int}"

    return False, ""


def exception_indicates_maintenance(exc: BaseException) -> Tuple[bool, str]:
    if isinstance(exc, AsianOddsMaintenanceError):
        return True, str(exc)

    msg = str(exc or "").strip()
    if text_indicates_maintenance(msg):
        return True, msg
    if "503" in msg and ("unavailable" in msg.lower() or "service" in msg.lower()):
        return True, msg
    return False, ""


def check_api_maintenance(client: "AsianOddsClient") -> Tuple[bool, str]:
    """
    Lightweight probe: True when AsianOdds appears to be in maintenance.
    """
    try:
        data = client.is_logged_in()
        is_maint, reason = response_indicates_maintenance(data)
        if is_maint:
            return True, reason
        return False, ""
    except Exception as exc:
        is_maint, reason = exception_indicates_maintenance(exc)
        if is_maint:
            return True, reason
        return False, ""
