from __future__ import annotations
import re
import json
import uuid
import unicodedata
import difflib
from typing import Any, Dict, Optional

_RUNTIME_API_SPORT_IDS: Dict[str, int] = {}


def set_runtime_api_sport_ids(sport_ids: Dict[str, int]) -> None:
    """Set API sport ids discovered at runtime (e.g. from /v3/sports)."""
    global _RUNTIME_API_SPORT_IDS
    try:
        normalized: Dict[str, int] = {}
        for k, v in (sport_ids or {}).items():
            kk = str(k or "").strip().lower()
            if not kk:
                continue
            try:
                sid = int(v)
            except Exception:
                continue
            if sid > 0:
                normalized[kk] = sid
        _RUNTIME_API_SPORT_IDS = normalized
    except Exception:
        _RUNTIME_API_SPORT_IDS = {}


def parse_bet_message(message_text: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lines = [line.strip() for line in message_text.splitlines() if line.strip()]

    # ---- Detect Tipster ----
    # IMPORTANT: don't require the exact arrow emoji sequence (some clients omit VS16 or use a different glyph).
    # Prefer line-based parsing so we don't depend on multi-line regex behavior.
    tipster = "default"
    for line in lines:
        m_tip = re.search(r"(?:➡️|➡)?\s*New tip from\s+(.+?)\s*$", line, re.IGNORECASE)
        if m_tip:
            tipster = (m_tip.group(1) or "").strip() or "default"
            break

    def _extract_min_odds(text: str) -> float:
        # English + French phrasing for minimum recommended odds
        patterns = [
            r"No bet under\s+([0-9.,]+)",
            r"Cote\s+minimale\s+recommand[eé]e\s*[:\-]?\s*([0-9.,]+)",
            r"Cote\s+minimale\s*[:\-]?\s*([0-9.,]+)",
            r"\bNB\s*[:\-]?\s*([0-9.,]+)",
            # Many tipsters use "Min: 1.25" (often with emojis)
            r"\bMin\s*[:\-]?\s*([0-9.,]+)",
            # Some tipsters write "Mini: 1.54"
            r"\bMini\s*[:\-]?\s*([0-9.,]+)",
        ]
        for pat in patterns:
            match = re.search(pat, text, re.IGNORECASE)
            if match:
                try:
                    return float(match.group(1).replace(",", "."))
                except Exception:
                    return 0.0
        return 0.0

    def _detect_resulting_unit(*texts: Optional[str]) -> Optional[str]:
        """
        Best-effort detection of Tennis bet unit preference.
        Returns "Games", "Sets", or None.
        """
        combined = " ".join([(t or "") for t in texts if t]).strip()
        if not combined:
            return None

        # Explicit parenthetical qualifiers used by some tipsters / Bet2Invest exports
        if re.search(r"\(\s*games\s*\)", combined, re.IGNORECASE):
            return "Games"
        if re.search(r"\(\s*sets?\s*\)", combined, re.IGNORECASE):
            return "Sets"

        # Keyword-based (French + English). Keep these lower priority than explicit tags.
        if re.search(r"\b(jeux|games?)\b", combined, re.IGNORECASE):
            return "Games"
        if re.search(r"\b(sets?|manches?)\b", combined, re.IGNORECASE):
            return "Sets"

        return None

    def _normalize_name_for_compare(text: str) -> str:
        """
        Normalize player/team names for resilient comparisons:
        - lowercase
        - remove diacritics (Marozsán -> marozsan)
        - drop punctuation/extra whitespace
        """
        if not text:
            return ""
        s = unicodedata.normalize("NFKD", text)
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        s = s.lower()
        # Keep letters/numbers/spaces only
        s = re.sub(r"[^a-z0-9\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    def _best_name_match(selection_name: str, home_name: str, away_name: str) -> Optional[str]:
        """
        Returns "home" / "away" when selection is a close-enough match to either side.
        Useful when tipsters misspell names slightly (e.g., Marozsan vs Maroszan).
        """
        sel = _normalize_name_for_compare(selection_name)
        h = _normalize_name_for_compare(home_name)
        a = _normalize_name_for_compare(away_name)
        if not sel or (not h and not a):
            return None

        # Exact (normalized) match wins
        if sel == h:
            return "home"
        if sel == a:
            return "away"

        # Fuzzy fallback
        h_score = difflib.SequenceMatcher(None, sel, h).ratio() if h else 0.0
        a_score = difflib.SequenceMatcher(None, sel, a).ratio() if a else 0.0
        best = max(h_score, a_score)
        if best < 0.78:
            return None
        return "home" if h_score >= a_score else "away"

    def _parse_tennis_spread_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle tips like:
        Player A - Player B
        LEAGUE NAME
        2025/11/13 20:30
        Player A-1.5 sets
        @ 1.505
        Cote minimale recommandee: 1.45

        And game handicaps:
        Player A-3.5 games
        @ 1.336
        Cote minimale recommandee: 1.30
        """
        selection_line = None
        for line in lines_list:
            if re.search(r"\b(?:sets?|games?)\b", line, re.IGNORECASE):
                selection_line = line
                break
        if not selection_line:
            return None

        # Remove common wrappers/prefixes used by tipsters:
        # e.g. "---- 🎯 PARI :  Ryan Peniston -1.5 - Sets  ----"
        cleaned = selection_line.strip()
        cleaned = re.sub(r"^[\s\-–—_]+", "", cleaned).strip()
        cleaned = re.sub(r"[\s\-–—_]+$", "", cleaned).strip()
        # Strip emojis and "PARI:" label
        cleaned = re.sub(r"(?i)\bpari\s*:\s*", "", cleaned).strip()
        cleaned = re.sub(r"^[^\wA-Za-zÀ-ÿ]+", "", cleaned).strip()

        # Accept both:
        # - "Player A-1.5 sets"  (common: hyphen directly implies a negative handicap)
        # - "Player A -1.5 - Sets"
        # - "Player A - 1.5 games"
        #
        # IMPORTANT: many player names contain hyphens (e.g. Jean-Pierre). We therefore
        # parse by anchoring on the trailing "<number> <unit>" instead of splitting on
        # the first hyphen.
        tail = re.search(
            r"(?P<hcap>[+-]?\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*)?(?P<unit>sets?|games?)(?:\s*\([^)]*\))?\s*$",
            cleaned,
            re.IGNORECASE,
        )

        # Check for ML format: "Player - Sets" or "Player - Games" without handicap number
        ml_tail = None
        if not tail:
            ml_tail = re.search(
                r"(.+?)\s*-\s*(sets?|games?)\s*$",
                cleaned,
                re.IGNORECASE,
            )

        if not tail and not ml_tail:
            return None

        if tail:
            # Handicap bet
            handicap_raw = (tail.group("hcap") or "").strip()
            unit_word = (tail.group("unit") or "").strip().lower()
            selection_part = cleaned[: tail.start("hcap")].strip()

            # If the handicap has no explicit sign, treat a trailing dash as "minus"
            # (e.g. "Emerson Jones-3.5 games" => -3.5).
            if handicap_raw and not handicap_raw.startswith(("+", "-")):
                if re.search(r"[-–—]\s*$", selection_part):
                    handicap_raw = f"-{handicap_raw}"
                    selection_part = re.sub(r"[-–—]\s*$", "", selection_part).strip()

            # Clean up any decorative trailing separators that might remain.
            selection_name = re.sub(r"[-–—]\s*$", "", selection_part).strip()
            if not selection_name:
                return None

            preferred_unit = "Games" if unit_word.startswith("game") else "Sets"
        else:
            # ML bet
            selection_name = (ml_tail.group(1) or "").strip()
            unit_word = (ml_tail.group(2) or "").strip().lower()

            # For ML, no handicap
            handicap_raw = None
            preferred_unit = "Games" if unit_word.startswith("game") else "Sets"

        odds_match = re.search(r"@\s*([0-9]+(?:[.,][0-9]+)?)", message_text)
        if not odds_match:
            return None

        odds_raw = odds_match.group(1)

        # Stake (units). Many tips provide "Mise : 1.5u" on its own line.
        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"\bMise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u\b", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break

        return {
            "selection": selection_name,
            "handicap_raw": handicap_raw,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
            "preferred_resulting_unit": preferred_unit,
        }

    def _parse_fr_pari_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle French-formatted tips like:
        PARI: Player h-2.5 jeux
        Prono: @1.80
        Mise: 1u
        NB: 1.70
        """
        selection_line = None
        for line in lines_list:
            if re.search(r"\bPARI\s*:", line, re.IGNORECASE):
                selection_line = line
                break
        if not selection_line:
            return None

        selection_name: Optional[str] = None
        handicap_raw: Optional[str] = None
        preferred_unit: Optional[str] = None

        # Format A (existing): "PARI: Player h-2.5 jeux"
        sel_match = re.search(
            r"PARI\s*:\s*([A-Za-zÀ-ÿ'().\s-]+?)\s*h\s*([+-]?\d+(?:[.,]\d+)?)\s*(?:jeux|games?)",
            selection_line,
            re.IGNORECASE,
        )
        if sel_match:
            selection_name = sel_match.group(1).strip()
            handicap_raw = sel_match.group(2).strip()
            preferred_unit = "Games"

        # Format B (new): "---- 🎯 PARI : Player (Jeux) +4.0 ----"
        # Also accept "(Games)" / "(Sets)" / "(Manches)".
        if selection_name is None or handicap_raw is None:
            cleaned = selection_line.strip()
            cleaned = re.sub(r"^[\s\-–—_]+", "", cleaned).strip()
            cleaned = re.sub(r"[\s\-–—_]+$", "", cleaned).strip()
            cleaned = re.sub(r"(?i)\bpari\s*:\s*", "", cleaned).strip()
            cleaned = re.sub(r"^[^\wA-Za-zÀ-ÿ]+", "", cleaned).strip()

            m2 = re.match(
                r"^(?P<sel>.+?)\s*\(\s*(?P<unit>jeux|games?|sets?|manches?)\s*\)\s*(?P<hcap>[+-]?\d+(?:[.,]\d+)?)\s*$",
                cleaned,
                re.IGNORECASE,
            )
            if m2:
                selection_name = (m2.group("sel") or "").strip()
                handicap_raw = (m2.group("hcap") or "").strip()
                unit_word = (m2.group("unit") or "").strip().lower()
                if unit_word in {"jeux", "game", "games"}:
                    preferred_unit = "Games"
                elif unit_word in {"set", "sets", "manche", "manches"}:
                    preferred_unit = "Sets"

        if not selection_name or not handicap_raw:
            return None

        odds_raw = None
        for line in lines_list:
            om = re.search(r"@\s*([0-9]+(?:[.,][0-9]+)?)", line)
            if om:
                odds_raw = om.group(1)
                break
        if not odds_raw:
            return None

        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"Mise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break

        return {
            "selection": selection_name,
            "handicap_raw": handicap_raw,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
            "preferred_resulting_unit": preferred_unit,
        }

    def _parse_fr_pari_ml_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle French moneyline tips like:

        — — 🎯 PARI:  Daniil Medvedev - ML — —
        ➡️ Prono: @1.3
        💰Mise: 1.25u
        📈 Min: 1.25

        Returns selection + odds + stake. (No handicap for ML.)
        """
        selection_line = None
        for line in lines_list:
            if re.search(r"\bPARI\s*:", line, re.IGNORECASE) and re.search(r"\bML\b", line, re.IGNORECASE):
                selection_line = line
                break
        if not selection_line:
            return None

        sel_match = re.search(
            r"PARI\s*:\s*(.+?)\s*[-–—]\s*ML\b",
            selection_line,
            re.IGNORECASE,
        )
        if not sel_match:
            return None

        selection_name = sel_match.group(1).strip()
        # Trim decorative separators that some channels wrap around the line
        selection_name = re.sub(r"^[—–\-\s]+", "", selection_name).strip()
        selection_name = re.sub(r"[—–\-\s]+$", "", selection_name).strip()

        odds_raw = None
        for line in lines_list:
            # Accept: "Prono: @1.3", "Prono: 1.3", "@ 1.3"
            om = re.search(r"(?:Prono\s*:\s*)?@?\s*([0-9]+(?:[.,][0-9]+)?)", line, re.IGNORECASE)
            if om and ("prono" in line.lower() or "@" in line):
                odds_raw = om.group(1)
                break
        if not odds_raw:
            return None

        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"Mise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u\b", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break

        return {
            "selection": selection_name,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
        }

    def _parse_fr_pari_totals_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle French totals tips like:

        ---- 🎯 PARI : Plus de 3.5 - Sets ----
        ➡️ Prono : @1.571
        💰 Mise : 1u

        Also accepts:
        - "Moins de" (UNDER)
        - units: Sets/Manches, Jeux/Games
        """
        selection_line = None
        for line in lines_list:
            if re.search(r"\bPARI\s*:", line, re.IGNORECASE) and re.search(r"\b(plus\s+de|moins\s+de)\b", line, re.IGNORECASE):
                selection_line = line
                break
        if not selection_line:
            return None

        cleaned = selection_line.strip()
        cleaned = re.sub(r"^[\s\-–—_]+", "", cleaned).strip()
        cleaned = re.sub(r"[\s\-–—_]+$", "", cleaned).strip()
        cleaned = re.sub(r"(?i)\bpari\s*:\s*", "", cleaned).strip()
        cleaned = re.sub(r"^[^\wA-Za-zÀ-ÿ]+", "", cleaned).strip()

        m = re.search(
            r"(?i)^(?P<dir>plus\s+de|moins\s+de)\s*(?P<hcap>\d+(?:[.,]\d+)?)\s*(?:[-–—]\s*)?(?P<unit>sets?|manches?|jeux|games?)\b",
            cleaned,
        )
        if not m:
            return None

        direction = (m.group("dir") or "").strip().lower()
        handicap_raw = (m.group("hcap") or "").strip()
        unit_word = (m.group("unit") or "").strip().lower()

        side = "OVER" if "plus" in direction else "UNDER"
        preferred_unit = None
        if unit_word in {"jeux", "game", "games"}:
            preferred_unit = "Games"
        elif unit_word in {"set", "sets", "manche", "manches"}:
            preferred_unit = "Sets"

        odds_raw = None
        for line in lines_list:
            om = re.search(r"@\s*([0-9]+(?:[.,][0-9]+)?)", line)
            if om:
                odds_raw = om.group(1)
                break
        if not odds_raw:
            return None

        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"\bMise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u\b", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break

        return {
            "side": side,
            "handicap_raw": handicap_raw,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
            "preferred_resulting_unit": preferred_unit,
        }

    def _parse_fr_set_ml_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle French set moneyline tips like:
        
        🎾 MATCH 523 : Luca van Assche -vs- Alexis Galarneau
        🏆 Tournoi : ATP Indian Wells - Qualifiers
        ---- 🎯 PARI : Luca van Assche - 1er set Money Line  ----
        ➡️ Prono : @1.469
        💰 Mise : 1.25u
        📈 Mini : 1.42
        """
        # Look for PARI line with "1er set Money Line" or similar set ML patterns
        selection_line = None
        for line in lines_list:
            if re.search(r"\bPARI\s*:", line, re.IGNORECASE) and re.search(r"\b(?:1er\s+set|first\s+set).*(?:money\s*line|ml)\b", line, re.IGNORECASE):
                selection_line = line
                break
        
        if not selection_line:
            return None
        
        # Extract player name from "PARI : Player Name - 1er set Money Line"
        sel_match = re.search(
            r"PARI\s*:\s*(.+?)\s*-\s*(?:1er\s+set|first\s+set).*(?:money\s*line|ml)",
            selection_line,
            re.IGNORECASE,
        )
        
        if not sel_match:
            return None
        
        selection_name = sel_match.group(1).strip()
        # Clean decorative separators
        selection_name = re.sub(r"^[—–\-\s]+", "", selection_name).strip()
        selection_name = re.sub(r"[—–\-\s]+$", "", selection_name).strip()
        
        # Look for odds in "Prono : @1.469" format
        odds_raw = None
        for line in lines_list:
            om = re.search(r"Prono\s*:\s*@?\s*([0-9]+(?:[.,][0-9]+)?)", line, re.IGNORECASE)
            if om:
                odds_raw = om.group(1)
                break
        
        if not odds_raw:
            return None
        
        # Look for stake in "Mise : 1.25u" format
        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"Mise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u\b", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break
        
        return {
            "selection": selection_name,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
            "market_type": "ML Set 1",  # Special market type for first set ML
        }

    def _parse_fr_set_hdp_format(lines_list: list[str]) -> Optional[Dict[str, str]]:
        """
        Handle French set handicap tips like:
        
        🎾 MATCH 523 : Luca van Assche -vs- Alexis Galarneau
        🏆 Tournoi : ATP Indian Wells - Qualifiers
        ---- 🎯 PARI : Luca van Assche - 1er set -2.5 ----
        ➡️ Prono : @1.85
        💰 Mise : 1.0u
        📈 Mini : 1.75
        """
        # Look for PARI line with "1er set" and a handicap number
        selection_line = None
        for line in lines_list:
            if (re.search(r"\bPARI\s*:", line, re.IGNORECASE) and 
                re.search(r"\b(?:1er\s+set|first\s+set)", line, re.IGNORECASE) and
                re.search(r"[+-]?\d+(?:[.,]\d+)?", line) and
                not re.search(r"money\s*line|ml", line, re.IGNORECASE)):
                selection_line = line
                break
        
        if not selection_line:
            return None
        
        # Extract player name and handicap from "PARI : Player Name - 1er set -2.5"
        sel_match = re.search(
            r"PARI\s*:\s*(.+?)\s*-\s*(?:1er\s+set|first\s+set)\s*([+-]?\d+(?:[.,]\d+)?)",
            selection_line,
            re.IGNORECASE,
        )
        
        if not sel_match:
            return None
        
        selection_name = sel_match.group(1).strip()
        handicap_raw = sel_match.group(2).strip()
        
        # Clean decorative separators
        selection_name = re.sub(r"^[—–\-\s]+", "", selection_name).strip()
        selection_name = re.sub(r"[—–\-\s]+$", "", selection_name).strip()
        
        # Look for odds in "Prono : @1.85" format
        odds_raw = None
        for line in lines_list:
            om = re.search(r"Prono\s*:\s*@?\s*([0-9]+(?:[.,][0-9]+)?)", line, re.IGNORECASE)
            if om:
                odds_raw = om.group(1)
                break
        
        if not odds_raw:
            return None
        
        # Look for stake in "Mise : 1.0u" format
        stake_units_raw = "1"
        for line in lines_list:
            sm = re.search(r"Mise\s*:\s*([0-9]+(?:[.,][0-9]+)?)\s*u\b", line, re.IGNORECASE)
            if sm:
                stake_units_raw = sm.group(1)
                break
        
        return {
            "selection": selection_name,
            "handicap_raw": handicap_raw,
            "odds_raw": odds_raw,
            "stake_units_raw": stake_units_raw,
            "market_type": "HDP Set 1",  # Special market type for first set HDP
        }

    # ---- Detect Sport ----
    sport = "Other"
    text_upper = message_text.upper()

    # Use unicode escape sequences to avoid editor/linter issues with emoji literals.
    tennis_emoji = "\U0001F3BE"      # 🎾
    football_emoji = "\u26BD"        # ⚽
    basketball_emoji = "\U0001F3C0"  # 🏀
    rugby_emoji = "\U0001F3C9"       # 🏉

    if tennis_emoji in message_text or re.search(
        r"\b(TENNIS|ATP|WTA|CHALLENGER|ITF|ROLAND GARROS|WIMBLEDON|US OPEN|AUSTRALIAN OPEN|FRENCH OPEN|DAVIS CUP)\b",
        text_upper,
    ):
        sport = "Tennis"
    elif (
        football_emoji in message_text
        or "SOCCER" in text_upper
        or "FOOTBALL" in text_upper
        or re.search(r"\b(FOOTBALL|SOCCER|FUTBOL|FÚTBOL)\b", text_upper)
    ):
        # Treat Football as Soccer (same sportId)
        sport = "Soccer"
    elif basketball_emoji in message_text or "BASKETBALL" in text_upper:
        sport = "Basketball"
    elif (
        rugby_emoji in message_text
        or "RUGBY UNION" in text_upper
        or re.search(r"\b(RUGBY|RUGBY\s+UNION|SUPER\s+RUGBY|SIX\s+NATIONS|RUGBY\s+CHAMPIONSHIP)\b", text_upper)
    ):
        sport = "Rugby Union"

    allow_soccer_cfg = config.get("allow_soccer")
    if allow_soccer_cfg is None:
        allow_soccer_cfg = config.get("allow_football", True)
    allow_rugby_union_cfg = config.get("allow_rugby_union", True)

    if (sport == "Tennis" and not config.get("allow_tennis", True)) or \
       (sport == "Soccer" and not allow_soccer_cfg) or \
       (sport == "Basketball" and not config.get("allow_basketball", True)) or \
       (sport == "Rugby Union" and not allow_rugby_union_cfg) or \
       sport == "Other":
        print(f"Ignored bet - sport not allowed: {sport}")
        return None

    def _is_probable_matchup_line(line: str) -> bool:
        """
        Heuristic to detect 'Team/Player A - Team/Player B' matchup lines.
        Avoids common headers like 'Channel Name - 2026' by rejecting digit-heavy sides.
        """
        if not line:
            return False
        s = line.strip()
        if not s:
            return False
        # Skip obvious non-match lines
        if "@" in s:
            return False
        if re.search(r"\b(?:cote|min|mise|prono|pari|analysis|nb)\b", s, re.IGNORECASE):
            return False
        if re.search(r"\b\d{4}[/-]\d{2}[/-]\d{2}\b", s):
            return False
        if s.startswith("⚠️") or s.startswith("🔗"):
            return False

        if not re.search(r"\s[-–—]\s", s):
            return False

        left, right = re.split(r"\s[-–—]\s", s, maxsplit=1)
        left = left.strip()
        right = right.strip()
        if not left or not right:
            return False

        # Reject numeric-only or digit-heavy sides (e.g., '2026', 'Match 2', etc.)
        def _ok_side(x: str) -> bool:
            if not x:
                return False
            if re.search(r"\d", x):
                # allow short seed markers like '(1)' but reject years/IDs
                if re.fullmatch(r"\(\s*\d+\s*\)", x.strip()):
                    return True
                return False
            # must contain at least one letter
            return bool(re.search(r"[A-Za-zÀ-ÿ]", x))

        if not (_ok_side(left) and _ok_side(right)):
            return False

        # Avoid picking league headers like "ATP - HONG KONG" (rare but possible)
        if left.upper() in {"ATP", "WTA", "ITF"} or right.upper() in {"ATP", "WTA", "ITF"}:
            return False

        return True

    # ---- Detect Players (vs OR versus) ----
    # Accept separators like "Player A -vs- Player B" in addition to "Player A vs Player B".
    # IMPORTANT: ensure we don't match "vs" inside team names like "AVS" (Portugal).
    vs_pat = r"(?<!\w)(?:vs|v\.?s\.?|versus|-vs-)(?!\w)"
    players_match = re.search(rf"(.+?)\s*[-–—]*\s*{vs_pat}\s*[-–—]*\s*(.+)", message_text, re.IGNORECASE)
    players_line_idx: Optional[int] = None
    
    # Check for French MATCH format first: "🎾 MATCH 523 : Player A -vs- Player B"
    if not players_match:
        match_line_match = re.search(rf"MATCH\s+\d+\s*:\s*(.+?)\s*{vs_pat}\s*(.+)", message_text, re.IGNORECASE)
        if match_line_match:
            players_match = match_line_match
    
    # For compact single-line messages, try to extract just the team names around "versus"
    if players_match:
        home_raw, away_raw = players_match.groups()
        home_raw, away_raw = home_raw.strip(), away_raw.strip()
        
        # For compact messages, try to extract clean team names
        # Look for team names after common prefixes and before common suffixes
        home_clean = home_raw
        away_clean = away_raw
        
        # Clean home team: extract after emojis and common prefixes
        home_match = re.search(r"(?:🔴|⚽|🎾|🏀)?\s*([A-Za-zÀ-ÿ\s\-\'.]+?)(?:\s+versus|\s*$)", home_raw, re.IGNORECASE)
        if home_match:
            home_clean = home_match.group(1).strip()
        else:
            # Fallback: take last meaningful part before "versus"
            home_parts = re.split(r'[🔴⚽🎾🏀]', home_raw)
            if home_parts:
                home_clean = home_parts[-1].strip()
        
        # Clean away team: extract before common suffixes like "League •"
        away_match = re.search(r'^([A-Za-zÀ-ÿ\s\-\'.]+?)(?:\s*League\s*•|\s*Jan\s+\d+|\s*\d{4}[/-]\d{2}[/-]\d{2}|\s*@|\s*Total\s+Points)', away_raw, re.IGNORECASE)
        if away_match:
            away_clean = away_match.group(1).strip()
        else:
            # Fallback: take first meaningful part
            away_parts = re.split(r'(?:League\s*•|Jan\s+\d+|\d{4}[/-]\d{2}[/-]\d{2}|@|Total\s+Points)', away_raw, 1)
            if away_parts:
                away_clean = away_parts[0].strip()
        
        home, away = home_clean, away_clean
    
    if not players_match and lines:
        # Fallback for labeled match line: "MATCH: Player A vs Player B"
        for line in lines:
            labeled = re.search(rf"match\s*\d*\s*:\s*(.+?)\s*[-–—]*\s*{vs_pat}\s*[-–—]*\s*(.+)", line, re.IGNORECASE)
            if labeled:
                players_match = labeled
                break
        if not players_match:
            # Fallback for formats using a dash: "Player A - Player B"
            for idx, line in enumerate(lines):
                if _is_probable_matchup_line(line):
                    dash_match = re.match(r"(.+?)\s*[-–—]\s*(.+)", line)
                    if dash_match:
                        players_match = dash_match
                        players_line_idx = idx
                        break
    if not players_match:
        return None

    # Only extract from groups if we haven't already cleaned the names above
    if 'home' not in locals() or 'away' not in locals():
        home, away = players_match.groups()
        home, away = home.strip(), away.strip()
    
    # Cleanup common prefixes like "🎾 MATCH 4:" that appear in some channels
    home = re.sub(r"^.*?\bmatch\s*\d*\s*:\s*", "", home, flags=re.IGNORECASE).strip()
    home = re.sub(r"^[\W_]+", "", home).strip()
    away = re.sub(r"^[\W_]+", "", away).strip()

    # ---- Detect League/Title (line with "League • ...") ----
    title: Optional[str] = None

    # Prefer an explicit "League ..." or "Tournoi ..." line if present. Support different separators:
    # - "League • Brazil - Paulista A3"
    # - "League: Brazil - Paulista A3"
    # - "League - Brazil - Paulista A3"
    # - "🏆 Tournoi : ATP Indian Wells - Qualifiers"
    # Some forwarded/copy-pasted messages replace "•" with other middle dots.
    for line in lines:
        m_league = re.match(r"^(?:🏆\s*)?(?:League|Ligue|Tournoi)\s*(?:[•\u2022\u00B7:\-–—]\s*)?(?P<name>.+?)\s*$", line, re.IGNORECASE)
        if m_league:
            title = (m_league.group("name") or "").strip()
            break

    # Fallback to a multi-line scan (kept for backwards compatibility with compact one-liners)
    if not title:
        title_match = re.search(
            r"(?:League|Ligue|Tournoi)\s*[•\u2022\u00B7:\-–—]\s*(.+?)(?:\s*Jan\s+\d+|\s*\d{4}[/-]\d{2}[/-]\d{2}|\s*Total\s+Points|\s*@|\s*🔗|$)",
            message_text,
        )
        title = title_match.group(1).strip() if title_match else None

    # Guardrail: never use obvious headers as the league title (this breaks resolver league filtering).
    if title and re.search(r"\bnew\s+tip\s+from\b", title, re.IGNORECASE):
        title = None
    if title and re.search(r"\bfootball\s+in\s+live\b", title, re.IGNORECASE):
        title = None
    if title:
        # Some copy/paste flows replace "•" (or other separators) with garbled bytes, leaving leading junk.
        # Strip any leading non-word characters so resolver league filtering doesn't get poisoned.
        title = re.sub(r"^[\W_]+", "", title).strip() or None
    if not title:
        tour_match = re.search(r"Tournoi\s*:\s*(.+)", message_text, re.IGNORECASE)
        if tour_match:
            title = tour_match.group(1).strip()
    if not title:
        # Prefer a plausible league line occurring AFTER the matchup line (common for tennis tips)
        start_idx = (players_line_idx + 1) if players_line_idx is not None else 0
        for candidate_title in lines[start_idx:]:
            if not candidate_title:
                continue
            if candidate_title.startswith("⚠️") or candidate_title.startswith("🔗"):
                continue
            if re.match(r"\d{4}[/-]\d{2}[/-]\d{2}", candidate_title):
                continue
            if re.search(r"@\s*\d", candidate_title):
                continue
            # Avoid reusing the matchup line itself
            if _is_probable_matchup_line(candidate_title):
                continue
            # Skip obvious informational headers
            if re.search(r"\b(?:paris|mise|cote|minimale|recommand[eé]e|analysis|nb)\b", candidate_title, re.IGNORECASE):
                continue
            # Tennis leagues are often short uppercase lines like "ATP HONG KONG"
            title = candidate_title
            break

    # ---- Find Bet Line (ML / HDP / TOTAL POINTS / TEAM TOTAL POINTS) ----
    bet = re.search(
        r"(ML\s+(?:Match|Game)|HDP\s+(?:Match|Game))\s*:\s*(.+?)\s+([+-]?\d+(?:\.\d+)?)?\s*@\s*([0-9.,]+)\s*\(([0-9.]+)\s*U\)",
        message_text,
        re.IGNORECASE,
    )

    totals = None
    team_totals = None
    french_spread = None
    fr_pari = None
    fr_pari_ml = None
    fr_pari_totals = None
    if not bet:
        # Try Team Totals: "Team Total Points Match : Denis Shapovalov (Games) OVER 12.50 @ 1.86 (1.5 U)"
        team_totals = re.search(
            r"(Team\s+Total\s+Points\s+(?:Match|Game))\s*:\s*(.+?)\s+(OVER|UNDER)\s+([+-]?\d+(?:\.\d+)?)\s*@\s*([0-9.,]+)\s*\(([0-9.]+)\s*U\)",
            message_text,
            re.IGNORECASE,
        )
        if not team_totals:
            # Try regular Totals: "Total Points Match : OVER 23.00 @ 1.89 (1 U)"
            # Also handle compact format without line breaks
            totals = re.search(
                r"(Total\s+Points\s+(?:Match|Game))\s*:\s*(OVER|UNDER)\s+([+-]?\d+(?:\.\d+)?)\s*@\s*([0-9.,]+)\s*\(([0-9.]+)\s*U\)",
                message_text,
                re.IGNORECASE,
            )
    if not bet and not team_totals and not totals:
        # Try French set ML PARI format first (e.g. "PARI: Player - 1er set Money Line")
        fr_set_ml = _parse_fr_set_ml_format(lines)
        if not fr_set_ml:
            # Try French set HDP PARI format (e.g. "PARI: Player - 1er set -2.5")
            fr_set_hdp = _parse_fr_set_hdp_format(lines)
            if not fr_set_hdp:
                # Try French ML PARI format (e.g. "PARI: Player - ML")
                fr_pari_ml = _parse_fr_pari_ml_format(lines)
                if not fr_pari_ml:
                    # Try French totals format before spread, to avoid mis-parsing
                    # "Plus de / Moins de X - Sets/Jeux" as a handicap bet.
                    fr_pari_totals = _parse_fr_pari_totals_format(lines)
                    if not fr_pari_totals:
                        french_spread = _parse_tennis_spread_format(lines)
                        if not french_spread:
                            fr_pari = _parse_fr_pari_format(lines)
                            if not fr_pari:
                                return None

    if bet:
        market_type, selection, handicap_raw, odds_raw, stake_units_raw = bet.groups()
        # Normalize market type label to *Match
        market_type = re.sub(r"\bGame\b", "Match", market_type, flags=re.IGNORECASE)
        side = None
    elif team_totals:
        market_type, team_name, side, handicap_raw, odds_raw, stake_units_raw = team_totals.groups()
        market_type = re.sub(r"\bGame\b", "Match", market_type, flags=re.IGNORECASE)
        side = side.upper()
        selection = team_name.strip()  # For team totals, selection is the team name
    elif totals:
        market_type, side, handicap_raw, odds_raw, stake_units_raw = totals.groups()
        market_type = re.sub(r"\bGame\b", "Match", market_type, flags=re.IGNORECASE)
        side = side.upper()
        selection = side  # For totals, selection is OVER/UNDER
    elif fr_pari_totals:
        market_type = "Total Points Match"
        side = (fr_pari_totals.get("side") or "OVER").upper()
        selection = side
        handicap_raw = fr_pari_totals["handicap_raw"]
        odds_raw = fr_pari_totals["odds_raw"]
        stake_units_raw = fr_pari_totals["stake_units_raw"]
    elif french_spread:
        # Check if it's ML (no handicap) or HDP
        if french_spread["handicap_raw"] is None:
            market_type = "ML Match"
        else:
            market_type = "HDP Match"
        selection = french_spread["selection"]
        handicap_raw = french_spread["handicap_raw"]
        odds_raw = french_spread["odds_raw"]
        stake_units_raw = french_spread["stake_units_raw"]
        side = None
    elif fr_pari:
        market_type = "HDP Match"
        selection = fr_pari["selection"]
        handicap_raw = fr_pari["handicap_raw"]
        odds_raw = fr_pari["odds_raw"]
        stake_units_raw = fr_pari["stake_units_raw"]
        side = None
    elif fr_set_ml:
        market_type = fr_set_ml["market_type"]  # "ML Set 1"
        selection = fr_set_ml["selection"]
        handicap_raw = None
        odds_raw = fr_set_ml["odds_raw"]
        stake_units_raw = fr_set_ml["stake_units_raw"]
        side = None
    elif fr_set_hdp:
        market_type = fr_set_hdp["market_type"]  # "HDP Set 1"
        selection = fr_set_hdp["selection"]
        handicap_raw = fr_set_hdp["handicap_raw"]
        odds_raw = fr_set_hdp["odds_raw"]
        stake_units_raw = fr_set_hdp["stake_units_raw"]
        side = None
    elif fr_pari_ml:
        market_type = "ML Match"
        selection = fr_pari_ml["selection"]
        handicap_raw = None
        odds_raw = fr_pari_ml["odds_raw"]
        stake_units_raw = fr_pari_ml["stake_units_raw"]
        side = None
    else:
        return None

    # ---- Determine Tennis unit preference (Games vs Sets) ----
    selection_raw = (selection or "").strip()
    preferred_unit: Optional[str] = None
    if sport == "Tennis":
        preferred_unit = _detect_resulting_unit(home, away, selection_raw)
        # French tennis spread parser explicitly indicates the unit
        if not preferred_unit and isinstance(french_spread, dict):
            preferred_unit = french_spread.get("preferred_resulting_unit")
        # French "PARI:" parser may also provide it (e.g. "(Jeux)" / "(Sets)")
        if not preferred_unit and isinstance(fr_pari, dict):
            preferred_unit = fr_pari.get("preferred_resulting_unit")
        # French totals "Plus de / Moins de" may provide it too (Sets vs Games)
        if not preferred_unit and isinstance(fr_pari_totals, dict):
            preferred_unit = fr_pari_totals.get("preferred_resulting_unit")
        # Bet2Invest exports typically tag Games explicitly but often omit Sets.
        # If we didn't detect Games, default HDP Match to Sets to avoid placing the wrong market.
        if not preferred_unit:
            if market_type.lower() == "hdp match":
                preferred_unit = "Sets"
            elif market_type.lower() in ("total points match", "team total points match"):
                preferred_unit = "Games"

    # ---- Cleanup Selection (used for team matching; keep raw for unit detection) ----
    # NOTE: we intentionally keep home/away as-is (including "(Games)") to preserve context
    # for logging/display; matching in resolver normalizes by stripping parentheses.
    selection_clean = re.sub(r"\(.*?\)", "", selection_raw).strip()

    odds = float(odds_raw.replace(",", "."))
    stake_units = float(stake_units_raw)
    
    # ---- Get Tipster-Specific Settings ----
    # IMPORTANT: channel overrides must take precedence over tipster settings.
    # `_apply_channel_stake_overrides()` sets `_stake_overrides_source="channel"` in cfg_for_seg.
    stake_override_source = str(config.get("_stake_overrides_source") or "").strip().lower()
    tipster_settings = config.get("tipster_settings", {}).get(tipster, {})

    base_stake = config["base_stake"]
    min_stake = config["min_stake"]
    max_stake = config["max_stake"]
    min_unit = config["min_unit"]
    max_unit = config["max_unit"]

    if stake_override_source != "channel":
        base_stake = tipster_settings.get("base_stake", base_stake)
        min_stake = tipster_settings.get("min_stake", min_stake)
        max_stake = tipster_settings.get("max_stake", max_stake)
        min_unit = tipster_settings.get("min_unit", min_unit)
        max_unit = tipster_settings.get("max_unit", max_unit)

    # ---- Odds tolerance overrides (channel > tipster > global) ----
    effective_odds_tolerance = float(config.get("odds_tolerance", 0.0) or 0.0)
    odds_override_source = str(config.get("_odds_tolerance_overrides_source") or "").strip().lower()
    if odds_override_source != "channel":
        try:
            tip_tol = tipster_settings.get("odds_tolerance", None)
            if tip_tol is not None:
                effective_odds_tolerance = float(tip_tol)
        except Exception:
            pass
    
    stake_eur = base_stake * stake_units
    handicap = float(handicap_raw) if handicap_raw else None

    # ---- Handle Stake Limits ----
    if stake_units < min_unit:
        stake_eur = base_stake * min_unit
    if stake_units > max_unit:
        stake_eur = base_stake * max_unit
    if stake_eur < min_stake:
        stake_eur = min_stake
    if stake_eur > max_stake:
        stake_eur = max_stake

    # ---- Analysis: "No bet under X" + config global minimum ----
    min_odds_message = _extract_min_odds(message_text)
    try:
        global_min_odds = float(config.get("global_min_odds", 1.15))
    except Exception:
        global_min_odds = 1.15
    if global_min_odds < 0:
        global_min_odds = 0.0
    effective_min_odds = max(min_odds_message, global_min_odds)

    if effective_min_odds > 0 and odds + effective_odds_tolerance < effective_min_odds:
        print(
            f"Odds too low (tip {odds} < required min {effective_min_odds}; "
            f"message_min={min_odds_message}, global_min={global_min_odds}), ignored"
        )
        return None

    # Determine selection_type safely, preserving player names with parentheses
    if bet or french_spread or fr_pari or fr_pari_ml:
        sel_norm = selection_clean.strip().lower()
        # Check for Draw first (for ML Match)
        if sel_norm == "draw":
            selection_type_val = "draw"
        else:
            home_norm = re.sub(r"\(.*?\)", "", home).strip().lower()
            away_norm = re.sub(r"\(.*?\)", "", away).strip().lower()
            if sel_norm == home_norm:
                selection_type_val = "home"
            elif sel_norm == away_norm:
                selection_type_val = "away"
            else:
                fuzzy = _best_name_match(selection_clean, home, away)
                selection_type_val = fuzzy or "home"
    elif team_totals:
        # For team totals, determine if the team is home or away
        home_norm = re.sub(r"\(.*?\)", "", home).strip().lower()
        away_norm = re.sub(r"\(.*?\)", "", away).strip().lower()
        sel_norm = selection_clean.strip().lower()
        if sel_norm == home_norm:
            selection_type_val = "home"
        elif sel_norm == away_norm:
            selection_type_val = "away"
        else:
            fuzzy = _best_name_match(selection_clean, home, away)
            selection_type_val = fuzzy or "home"
    else:
        selection_type_val = selection_clean.upper()

    # Map sport to AsianOdds sportId (SportsType).
    # Prefer API-resolved ids from config["api_sport_ids"] when provided by runtime.
    # Fallbacks are used for known sports.
    api_sport_ids_cfg = config.get("api_sport_ids") if isinstance(config.get("api_sport_ids"), dict) else {}
    api_ids_norm = {str(k).strip().lower(): v for k, v in api_sport_ids_cfg.items()}
    # Runtime map has precedence over static config map.
    api_ids_norm.update(_RUNTIME_API_SPORT_IDS)

    def _api_sport_id(*aliases: str) -> Optional[int]:
        for alias in aliases:
            raw = api_ids_norm.get(alias)
            try:
                if raw is None:
                    continue
                sid = int(raw)
                if sid > 0:
                    return sid
            except Exception:
                continue
        return None

    if sport == "Tennis":
        sport_id = _api_sport_id("tennis") or 33
    elif sport == "Soccer":
        sport_id = _api_sport_id("soccer", "football") or 29
    elif sport == "Basketball":
        sport_id = _api_sport_id("basketball") or 4
    elif sport == "Rugby Union":
        sport_id = _api_sport_id("rugby union", "rugby_union", "rugbyunion", "rugby")
    else:
        sport_id = 0

    if sport == "Rugby Union" and not sport_id:
        print("Ignored bet - Rugby Union sportId not available in API sports list")
        return None

    bet_info = {
        "uuid": str(uuid.uuid4()),
        "tipster": tipster,
        "sport": sport,
        "sportId": sport_id,
        "home": home,
        "away": away,
        "title": title,
        "market_type": market_type,
        "selection": selection_clean,
        # Tennis-only hint for resolver when multiple events exist (Games vs Sets)
        "preferred_resulting_unit": preferred_unit,
        # For ML/HDP, selection_type denotes home/away; for Totals we store OVER/UNDER
        "selection_type": selection_type_val,
        # For Totals we also store side explicitly
        "side": side,
        "handicap": handicap,
        "odds": odds,
        "stake": round(stake_eur, 2),
        # Tipster text-only minimum (Tennis resolver uses this to choose strict min-only vs tolerance path)
        "min_odds": min_odds_message if min_odds_message > 0 else None,
        "odds_tolerance": effective_odds_tolerance
    }

    # ---- Draw Bet Minimum Stake Override ----
    # Some bookies require higher minimum stakes for Draw bets
    # Apply this minimum for Draw bets to prevent minimum stake errors
    if selection_type_val == "draw" and sport in ["Soccer", "Football"]:
        draw_min_stake = 18.0  # Minimum for Draw bets
        if bet_info["stake"] < draw_min_stake:
            bet_info["stake"] = draw_min_stake

    print(json.dumps(bet_info, indent=4))
    return bet_info
