from __future__ import annotations
import json
import os
import re
import sys
import asyncio
import tempfile
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Iterable, Union

from telethon import TelegramClient, events

from .config import load_config, save_config
from .env import load_env, _normalize_channel_identifier


def _to_peer(identifier: Optional[str]):
    """
    Convert a normalized channel identifier into a Telethon-friendly peer.
    Supports usernames and numeric IDs (e.g., -100...).
    """
    if not identifier:
        return None
    s = identifier.strip()
    if not s:
        return None
    if s.lstrip("+-").isdigit():
        try:
            return int(s)
        except Exception:
            return None
    return s


def _require_peer(name: str, identifier: Optional[str]):
    """
    Convert identifier to peer; if missing/invalid, return (None, error_message).
    """
    peer = _to_peer(identifier)
    if peer is None:
        return None, f"⚠️ {name} is not set or invalid; skipping."
    return peer, None


_API_SPORT_ID_CACHE: dict[str, int] = {}


def _normalize_sport_key(name: Optional[str]) -> str:
    if not name:
        return ""
    k = re.sub(r"[^a-z0-9]+", " ", str(name).lower()).strip()
    k = re.sub(r"\s+", " ", k)
    return k


def _build_api_sport_id_cache(client: AsianOddsClient) -> dict[str, int]:
    """
    Build a canonical sport-name -> sportId map from AsianOdds GetSports.
    Adds useful aliases (e.g., football->soccer and rugby union variants).
    """
    try:
        data = client.get_sports()
        result = data.get("Result", {})
        sports = result.get("Sports", [])
        cache: dict[str, int] = {}
        for s in sports:
            try:
                sid = int(s.get("SportsType"))
            except Exception:
                continue
            if sid <= 0:
                continue
            name = _normalize_sport_key(s.get("SportsName"))
            if not name:
                continue
            cache[name] = sid
            if name == "soccer":
                cache.setdefault("football", sid)
            if name in {"rugby union", "rugbyunion", "rugby"}:
                cache.setdefault("rugby union", sid)
                cache.setdefault("rugbyunion", sid)
                cache.setdefault("rugby_union", sid)
                cache.setdefault("rugby", sid)
        return cache
    except Exception:
        return {}


def _get_sport_id(client: AsianOddsClient, sport_name: str, fallback: Optional[int] = None) -> Optional[int]:
    global _API_SPORT_ID_CACHE
    if not _API_SPORT_ID_CACHE:
        _API_SPORT_ID_CACHE = _build_api_sport_id_cache(client)
    sid = _API_SPORT_ID_CACHE.get(_normalize_sport_key(sport_name))
    if sid:
        return sid
    return fallback


def _is_main_channel(chat_identifier: str, main_channel: str) -> bool:
    """
    Check if a chat identifier matches the main channel.

    We compare normalized forms (supports @username, t.me links, numeric IDs),
    and we also handle Telethon's different numeric representations:
    - event.chat.id is often a *positive* entity id
    - event.chat_id is often a *negative* peer id like -100123...
    """
    if not chat_identifier or not main_channel:
        return False

    normalized_chat = _normalize_channel_identifier(str(chat_identifier))
    normalized_main = _normalize_channel_identifier(str(main_channel))
    if normalized_chat == normalized_main:
        return True

    # Numeric equivalence: treat "-1001234567890" and "1234567890" as the same channel.
    def _canonical_numeric_id(s: str) -> Optional[int]:
        if not s:
            return None
        t = s.strip()
        if not t:
            return None
        if not t.lstrip("+-").isdigit():
            return None
        # Strip "+", then map "-100..." peer ids to the underlying channel id.
        if t.startswith("+"):
            t = t[1:]
        if t.startswith("-100") and t[4:].isdigit():
            try:
                return int(t[4:])
            except Exception:
                return None
        try:
            return abs(int(t))
        except Exception:
            return None

    a = _canonical_numeric_id(normalized_chat)
    b = _canonical_numeric_id(normalized_main)
    return (a is not None and b is not None and a == b)


# ---- Per-channel stake overrides (by channel name/title/username) ----
_STAKE_OVERRIDE_KEYS = {"base_stake", "min_stake", "max_stake", "min_unit", "max_unit"}
_CHANNEL_NAME_CACHE: dict[str, str] = {}


def _strip_accents(text: Optional[str]) -> str:
    if not text:
        return ""
    try:
        norm = unicodedata.normalize("NFKD", str(text))
        return "".join(ch for ch in norm if not unicodedata.combining(ch))
    except Exception:
        return str(text)


def _channel_key_variants(raw: Optional[str]) -> list[str]:
    """
    Generate matching variants for channel titles/headers:
    - strip emojis
    - strip accents
    - remove trailing bracket tags like "[ATP]" or "(ATP)"
    - collapse whitespace
    """
    if not raw:
        return []
    s0 = str(raw).strip()
    if not s0:
        return []
    s0 = _strip_emojis(s0)
    s0 = _strip_accents(s0)
    s0 = re.sub(r"\s+", " ", s0).strip()

    variants = [s0]
    # Remove trailing bracket tag(s)
    s1 = re.sub(r"\s*[\[\(][^\]\)]+[\]\)]\s*$", "", s0).strip()
    if s1 and s1 != s0:
        variants.append(s1)

    # Some channels prepend/append decorative punctuation
    s2 = re.sub(r"^[\W_]+", "", s1).strip()
    s2 = re.sub(r"[\W_]+$", "", s2).strip()
    if s2 and s2 not in variants:
        variants.append(s2)

    # De-dupe preserving order
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _looks_like_channel_title(raw: str) -> bool:
    """
    Heuristic: treat strings with spaces (and not obviously an @username/url/numeric id) as a channel title.
    Titles should not be passed through _normalize_channel_identifier (which would incorrectly add '@').
    """
    if not raw:
        return False
    s = str(raw).strip()
    if not s:
        return False
    if s.lstrip("+-").isdigit():
        return False
    if s.startswith("@"):
        return False
    if re.search(r"(?:^|//)(?:t\.me|telegram\.me|telegram\.dog)/", s, flags=re.IGNORECASE):
        return False
    return bool(re.search(r"\s", s))


async def _resolve_channel_settings_keys(
    client: Optional[TelegramClient], raw_channel: str
) -> tuple[str, list[str], Optional[str]]:
    """
    Resolve a user-supplied channel argument into:
      - canonical_key: the best display key to store under cfg['channel_settings'] (prefer @username, else title)
      - aliases: other keys that should match (e.g. normalized -100 id)
      - normalized_identifier: best-effort normalized identifier to reach the channel via Telethon (may be None)
    """
    raw = (raw_channel or "").strip()
    if not raw:
        return "", [], None

    normalized_identifier: Optional[str] = None
    # Preserve titles with spaces as-is; otherwise normalize (@username/t.me/id).
    if _looks_like_channel_title(raw):
        normalized_identifier = None
        canonical_key = raw
    else:
        normalized_identifier = _normalize_channel_identifier(raw)
        canonical_key = await _resolve_channel_display_name(client, normalized_identifier or raw)

    # Build alias list (preserve order, de-dupe)
    candidates = [canonical_key, normalized_identifier, raw]
    aliases: list[str] = []
    seen: set[str] = set()
    for c in candidates:
        if not c:
            continue
        cc = str(c).strip()
        if not cc or cc in seen:
            continue
        seen.add(cc)
        aliases.append(cc)

    # canonical is first; remainder are aliases
    canonical_key = aliases[0] if aliases else ""
    return canonical_key, aliases[1:], normalized_identifier


def _normalize_channel_settings_key(key: Optional[str]) -> str:
    """
    Normalize keys used under cfg["channel_settings"].
    - Usernames like "@mychannel" are compared case-insensitively.
    - Titles are compared case-insensitively with whitespace collapsed.
    """
    if not key:
        return ""
    # Normalize: strip emojis + accents, collapse whitespace, and ignore trailing bracket tags.
    raw = str(key).strip()
    if not raw:
        return ""
    # Preserve usernames verbatim (case-insensitive)
    if raw.startswith("@"):
        return f"@{raw[1:].strip().lower()}"

    s = _strip_emojis(raw)
    s = _strip_accents(s)
    s = re.sub(r"\s+", " ", s).strip()
    # Remove common trailing tags like "[ATP]" which often change over time
    s = re.sub(r"\s*[\[\(][^\]\)]+[\]\)]\s*$", "", s).strip()
    if not s:
        return ""
    return s.lower()


def _normalize_forward_mode(raw: Any) -> str:
    """
    Normalize forwarding mode strings.
    Supported:
      - "all"  -> forward every message
      - "bet"  -> forward only bet/tip messages (default/current behavior)
    """
    if raw is None:
        return "bet"
    s = str(raw).strip().lower()
    if s in {"all", "everything", "any", "messages"}:
        return "all"
    if s in {"bet", "bets", "betonly", "bet-only", "tip", "tips", "tiponly", "tip-only"}:
        return "bet"
    return "bet"


def _get_channel_forward_mode(cfg: Dict[str, Any], *, chat_identifier: Optional[str], channel_display_name: Optional[str]) -> str:
    """
    Return forwarding mode for a source channel. Looks up cfg["channel_forwarding"] by:
      - resolved display name (e.g. "@mychannel" or "My Channel")
      - normalized chat identifier (e.g. "@mychannel" or "-100123...")
      - raw chat identifier
    """
    channel_forwarding = cfg.get("channel_forwarding") or {}
    if not isinstance(channel_forwarding, dict) or not channel_forwarding:
        return "bet"

    candidates: list[str] = []
    if channel_display_name:
        candidates.append(_normalize_channel_settings_key(channel_display_name))
    if chat_identifier:
        candidates.append(_normalize_channel_settings_key(_normalize_channel_identifier(chat_identifier)))
        candidates.append(_normalize_channel_settings_key(chat_identifier))

    normalized_map: dict[str, Any] = {}
    for k, v in channel_forwarding.items():
        nk = _normalize_channel_settings_key(k)
        if nk and nk not in normalized_map:
            normalized_map[nk] = v

    for cand in candidates:
        if cand in normalized_map:
            v = normalized_map[cand]
            if isinstance(v, dict):
                return _normalize_forward_mode(v.get("mode"))
            return _normalize_forward_mode(v)
    return "bet"


async def _hydrate_channel_rules_aliases(
    cfg: Dict[str, Any],
    *,
    client: Optional[TelegramClient],
    channel_identifiers: list[str],
) -> None:
    """
    Ensure per-channel rules work whether a tip is received directly from the channel id/@username
    or forwarded as text with a "Source Channel" header line (usually the channel title).
    
    We do this by adding in-memory alias keys for cfg["channel_settings"] and cfg["channel_forwarding"]:
    - If the config has a rule keyed by "-100..." id, also add the resolved channel title as an alias key.
    - Also add a bracket-tag-stripped alias (e.g., "Arthur Tennis Prono [ATP]" -> "Arthur Tennis Prono").
    
    This does not write to disk; it only affects the running process.
    """
    if not cfg or not channel_identifiers:
        return

    for dict_key in ("channel_settings", "channel_forwarding"):
        d = cfg.get(dict_key)
        if d is None:
            cfg[dict_key] = {}
            d = cfg[dict_key]
        if not isinstance(d, dict):
            continue

    def _find_existing_value(d: dict, candidates: list[str]) -> tuple[Optional[str], Any]:
        """Find a matching key in d given candidate identifiers (using normalized key matching)."""
        normalized_candidates = [_normalize_channel_settings_key(c) for c in candidates if c]
        for existing_key in list(d.keys()):
            nk = _normalize_channel_settings_key(existing_key)
            if nk and nk in normalized_candidates:
                return existing_key, d.get(existing_key)
        return None, None

    for raw_ident in channel_identifiers:
        if not raw_ident:
            continue
        ident = str(raw_ident).strip()
        if not ident:
            continue

        normalized_ident = _normalize_channel_identifier(ident)
        # Resolve best-effort display name (prefer @username, else title)
        display = await _resolve_channel_display_name(client, normalized_ident or ident)
        if not display:
            continue

        alias_titles = _channel_key_variants(display)
        if not alias_titles:
            continue

        # Candidates that may already exist as keys in config
        id_candidates = [ident, normalized_ident, _normalize_channel_identifier(ident)]

        # Hydrate channel_settings aliases
        ch_settings = cfg.get("channel_settings") or {}
        if isinstance(ch_settings, dict) and ch_settings:
            existing_key, val = _find_existing_value(ch_settings, id_candidates)
            if isinstance(val, dict) and existing_key:
                for title in alias_titles:
                    if _normalize_channel_settings_key(title) not in {
                        _normalize_channel_settings_key(k) for k in ch_settings.keys()
                    }:
                        ch_settings[title] = val
                cfg["channel_settings"] = ch_settings

        # Hydrate channel_forwarding aliases
        ch_fwd = cfg.get("channel_forwarding") or {}
        if isinstance(ch_fwd, dict) and ch_fwd:
            existing_key, val = _find_existing_value(ch_fwd, id_candidates)
            if existing_key is not None and val is not None:
                for title in alias_titles:
                    if _normalize_channel_settings_key(title) not in {
                        _normalize_channel_settings_key(k) for k in ch_fwd.keys()
                    }:
                        ch_fwd[title] = val
                cfg["channel_forwarding"] = ch_fwd


def _channel_rules_has_key(cfg: Dict[str, Any], candidate: Optional[str]) -> bool:
    """
    Return True if candidate matches a key in either cfg['channel_settings'] or cfg['channel_forwarding'].
    Used to detect embedded "Source Channel" headers.
    """
    if not candidate:
        return False
    cand = _normalize_channel_settings_key(candidate)
    if not cand:
        return False

    for dict_key in ("channel_settings", "channel_forwarding"):
        d = cfg.get(dict_key) or {}
        if not isinstance(d, dict) or not d:
            continue
        for k in d.keys():
            if cand == _normalize_channel_settings_key(k):
                return True
    return False


async def _resolve_channel_display_name(client: Optional[TelegramClient], chat_identifier: Optional[str]) -> str:
    """
    Best-effort channel name resolution for a chat identifier (id or @username).
    Prefers @username when available, otherwise uses title, otherwise falls back to chat_identifier.
    """
    if not client or not chat_identifier:
        return str(chat_identifier or "")

    cache_key = str(chat_identifier)
    cached = _CHANNEL_NAME_CACHE.get(cache_key)
    if cached:
        return cached

    display = cache_key
    try:
        peer = _to_peer(chat_identifier)
        if peer is not None:
            entity = await client.get_entity(peer)
            username = getattr(entity, "username", None)
            title = getattr(entity, "title", None)
            if username:
                display = f"@{username}"
            elif title:
                display = _strip_emojis(str(title))
    except Exception:
        display = cache_key

    _CHANNEL_NAME_CACHE[cache_key] = display
    return display


# Clear the channel name cache to apply emoji stripping to existing cached entries
_CHANNEL_NAME_CACHE.clear()


def _apply_channel_stake_overrides(cfg: Dict[str, Any], *, chat_identifier: Optional[str], channel_display_name: Optional[str]) -> Dict[str, Any]:
    """
    Return a shallow-copied cfg with stake-related fields (and other per-channel fields like odds_tolerance)
    overridden from cfg["channel_settings"] if a matching key is found.

    Matching order:
    - resolved display name (e.g. "@mychannel" or "My Channel")
    - normalized chat identifier (e.g. "@mychannel" or "-100123...")
    - raw chat identifier
    """
    channel_settings = cfg.get("channel_settings") or {}
    if not isinstance(channel_settings, dict) or not channel_settings:
        return cfg

    candidates: list[str] = []
    if channel_display_name:
        for v in _channel_key_variants(channel_display_name):
            candidates.append(_normalize_channel_settings_key(v))
    if chat_identifier:
        for v in _channel_key_variants(_normalize_channel_identifier(chat_identifier)):
            candidates.append(_normalize_channel_settings_key(v))
        for v in _channel_key_variants(chat_identifier):
            candidates.append(_normalize_channel_settings_key(v))

    normalized_map: dict[str, tuple[str, Any]] = {}
    for k, v in channel_settings.items():
        nk = _normalize_channel_settings_key(k)
        if nk and nk not in normalized_map:
            normalized_map[nk] = (str(k), v)

    overrides = None
    matched_key: Optional[str] = None
    for cand in candidates:
        if cand in normalized_map:
            matched_key, overrides = normalized_map[cand]
            break

    if not overrides or not isinstance(overrides, dict):
        return cfg

    merged = cfg.copy()
    for k in _STAKE_OVERRIDE_KEYS:
        if k in overrides:
            merged[k] = overrides[k]
    # Odds tolerance override (stored alongside stake overrides in channel_settings)
    if "odds_tolerance" in overrides:
        try:
            merged["odds_tolerance"] = float(overrides["odds_tolerance"])
            merged["_odds_tolerance_overrides_source"] = "channel"
        except Exception:
            # Ignore invalid stored values
            pass
    # Marker for downstream parsing: channel overrides must take precedence over tipster settings
    merged["_stake_overrides_source"] = "channel"
    if matched_key:
        merged["_stake_overrides_key"] = matched_key
    return merged


def _strip_emojis(text: str) -> str:
    """
    Remove emojis and other unicode symbols from text.
    """
    import re
    # Remove emoji characters (including variations and ZWJ sequences)
    emoji_pattern = re.compile(
        "["
        "\U0001F600-\U0001F64F"  # emoticons
        "\U0001F300-\U0001F5FF"  # symbols & pictographs
        "\U0001F680-\U0001F6FF"  # transport & map symbols
        "\U0001F1E0-\U0001F1FF"  # flags (iOS)
        "\U00002700-\U000027BF"  # dingbats
        "\U0001f926-\U0001f937"  # gestures
        "\U00010000-\U0010ffff"  # other unicode
        "\u2640-\u2642"  # gender symbols
        "\u2600-\u2B55"  # misc symbols
        "\u200d"  # zero width joiner
        "\u23cf"  # eject symbol
        "\u23e9"  # fast forward
        "\u231a"  # watch
        "\ufe0f"  # variation selector
        "\u3030"  # wavy dash
        "]+",
        flags=re.UNICODE
    )
    return emoji_pattern.sub('', text).strip()


def _channel_settings_has_key(cfg: Dict[str, Any], candidate: Optional[str]) -> bool:
    """Return True if candidate matches a key in cfg['channel_settings'] after normalization."""
    if not candidate:
        return False
    channel_settings = cfg.get("channel_settings") or {}
    if not isinstance(channel_settings, dict) or not channel_settings:
        return False
    cand = _normalize_channel_settings_key(candidate)
    if not cand:
        return False
    for k in channel_settings.keys():
        if cand == _normalize_channel_settings_key(k):
            return True
    return False
from .api import AsianOddsClient
from .parser import parse_bet_message, set_runtime_api_sport_ids
from .resolver import resolve_event_and_line
from .validation import enrich_from_odds, is_duplicate_running_bet
from .betting import place_bet, build_place_bet_payload
from .export import export_bets_to_excel
from .logger import log_message, async_log_sink, format_bet_context
from .state import get_last_id, set_last_id, has_bet_for_message, mark_bet_for_message, has_bet_signature, mark_bet_signature


def _extract_bets_for_export(payload: Optional[Dict[str, Any]]) -> list[Dict[str, Any]]:
    """Normalize AsianOdds bet payloads into a single list."""
    if not payload:
        return []

    bets: list[Dict[str, Any]] = []
    
    # AsianOdds format: Result.Bets or Result.RunningBets
    result = payload.get("Result", {})
    
    # Try different possible locations for bets
    bet_list = result.get("Bets") or result.get("RunningBets") or result.get("NonRunningBets")
    if isinstance(bet_list, list):
        bets.extend(bet_list)
    
    # Fallback for direct bets array (backwards compatibility)
    if not bets:
        straight = payload.get("bets") or payload.get("Bets")
        if isinstance(straight, list):
            bets.extend(straight)

    return bets


def _update_env_file(key: str, value: str) -> bool:
    """Update a key-value pair in the .env file."""
    env_path = os.path.join(os.getcwd(), '.env')
    if not os.path.exists(env_path):
        return False
    
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        # Look for the key and update it
        updated = False
        for i, line in enumerate(lines):
            # Match lines that start with the key (with or without spaces)
            if re.match(rf'^\s*{re.escape(key)}\s*=', line):
                lines[i] = f"{key}={value}\n"
                updated = True
                break
        
        # If not found, append it
        if not updated:
            lines.append(f"{key}={value}\n")
        
        with open(env_path, 'w', encoding='utf-8') as f:
            f.writelines(lines)
        
        return True
    except Exception as e:
        print(f"Error updating .env file: {e}")
        return False


async def forward_bet_info(
    client: TelegramClient,
    original_message: str,
    forwarder_channels: Optional[Union[str, Iterable[str]]] = None,
    *,
    source_channel_name: Optional[str] = None,
) -> None:
    """Forward the original tip message to one or more Telegram channels (optionally prefixed with source channel)."""
    if not forwarder_channels or not original_message:
        return

    message_to_send = original_message
    if source_channel_name:
        src = str(source_channel_name).strip()
        if src:
            # Avoid double-prefixing if the message already starts with the same header + blank line.
            if not re.match(rf"^\s*{re.escape(src)}\s*\n\s*\n", original_message):
                message_to_send = f"{src}\n\n{original_message}"

    # Normalize to iterable
    channels: list[str] = []
    if isinstance(forwarder_channels, str):
        channels = [forwarder_channels]
    else:
        channels = [ch for ch in forwarder_channels]

    seen: set[str] = set()
    for ch in channels:
        peer = _to_peer(ch)
        dedupe_key = str(peer) if peer is not None else ""
        if not peer or dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        try:
            # Resolve entity so send works even when peer is not in cache (e.g. numeric channel IDs).
            # Without this, forwarding only works when the channel was already seen (e.g. manual use).
            entity = await client.get_entity(peer)
            await client.send_message(entity, message_to_send)
        except Exception as exc:
            await log_message(f"⚠️ Error forwarding tip message to {ch}: {exc}")


async def _process_bet_text(message_text: str, *, cfg: Dict[str, Any], client_api: AsianOddsClient, chat: Optional[str] = None, message_id: Optional[int] = None, client: Optional[TelegramClient] = None, forwarder_channels: Optional[Union[str, Iterable[str]]] = None, is_outgoing: Optional[bool] = None) -> None:
    # Resolve the current chat display name once (used for default stake overrides + forwarding header).
    channel_display_name = await _resolve_channel_display_name(client, chat)

    # If the *whole message* starts with a "Source Channel" header (line 1) + blank line,
    # remember it so forwarding can prefix the original source channel name.
    message_level_source_hint: Optional[str] = None
    if message_text:
        m0 = re.match(r"^\s*([^\n]+?)\s*\n\s*\n", message_text)
        if m0:
            header0 = (m0.group(1) or "").strip()
            # Accept headers that match by title variants (e.g., stripping [ATP]) or by hydrated id aliases.
            if _channel_rules_has_key(cfg, header0):
                message_level_source_hint = header0

    # Parse the full message first (many tips include blank lines for formatting).
    # If full-message parsing fails, fall back to splitting by blank lines to support
    # messages that contain multiple independent tips.
    segments: list[str] = []
    if message_text:
        full = message_text.strip()
        if full:
            full_bet = parse_bet_message(full, cfg)
            if full_bet:
                segments = [full]
            else:
                # Split on blank lines or separator lines (dashes, equals, etc.)
                # Split on substantial separators (lines with many dashes/equals) but keep blank lines within segments
                separator_pattern = r"^[\s]*[-—═]{5,}[-—═\s]*$"
                segments = []
                current_segment = []

                for line in full.split('\n'):
                    if re.match(separator_pattern, line):
                        # Found a separator, save current segment if not empty
                        if current_segment:
                            segments.append('\n'.join(current_segment).strip())
                            current_segment = []
                    else:
                        current_segment.append(line)

                # Add the last segment
                if current_segment:
                    segments.append('\n'.join(current_segment).strip())

                segments = [seg for seg in segments if seg.strip()]
    if not segments:
        segments = [message_text]

    forwarded_once = False
    for seg in segments:
        # Per-segment source header support:
        # Allow pasting multiple forwarded tips in one message like:
        #   Channel A\n\n<tip>\n\nChannel B\n\n<tip>
        seg_source_hint: Optional[str] = None
        seg_text = seg
        if seg_text:
            m = re.match(r"^\s*([^\n]+?)\s*\n\s*\n", seg_text)
            if m:
                header = (m.group(1) or "").strip()
                if _channel_rules_has_key(cfg, header):
                    seg_source_hint = header
                    seg_text = seg_text[m.end():]

        # Apply per-channel stake overrides for this segment.
        cfg_for_seg = cfg
        if seg_source_hint:
            # Prefer embedded source header over current chat (forwarder channel).
            cfg_for_seg = _apply_channel_stake_overrides(cfg, chat_identifier=None, channel_display_name=seg_source_hint)
        else:
            cfg_for_seg = _apply_channel_stake_overrides(cfg, chat_identifier=chat, channel_display_name=channel_display_name)

        bet_info = parse_bet_message(seg_text, cfg_for_seg)
        if not bet_info:
            # Silently ignore non-bet or out-of-scope segments
            continue

        # Persist best-effort source so success/failure logs can show Tipster or Channel.
        # Prefer embedded source headers over the current chat title.
        src = seg_source_hint or message_level_source_hint or channel_display_name
        if src:
            bet_info["source_channel"] = src
        elif chat:
            # Fallback to chat identifier (may be numeric id)
            bet_info["source_channel"] = str(chat)

        # Build a deterministic signature to avoid duplicates across main and listener channels
        try:
            sig_parts = [
                str(bet_info.get("sportId", "")),
                (bet_info.get("title") or "").strip().lower(),
                (bet_info.get("home") or "").strip().lower(),
                (bet_info.get("away") or "").strip().lower(),
                (bet_info.get("market_type") or "").strip().lower(),
                (bet_info.get("selection_type") or "").strip().lower(),
                (bet_info.get("side") or "").strip().upper(),
                str(round(float(bet_info.get("handicap")) if bet_info.get("handicap") is not None else 0.0, 2)),
            ]
            bet_sig = "|".join(sig_parts)
            bet_info["bet_signature"] = bet_sig
            # If forcing incoming bets and this is incoming, allow processing even if seen
            forcing_incoming = bool(cfg.get("force_incoming", False)) and not bool(is_outgoing)
            forcing_outgoing = bool(cfg.get("force_outgoing", False)) and bool(is_outgoing)
            # If this signature has already been processed, skip completely unless forcing
            if has_bet_signature(bet_sig) and not (forcing_incoming or forcing_outgoing):
                continue
        except Exception:
            bet_sig = None

        # Forward the original tip message if forwarder channel(s) are configured (outgoing suppressed by caller)
        if client and forwarder_channels and not forwarded_once:
            # Prefix the source channel (prefer message-level header if present).
            prefix = message_level_source_hint or channel_display_name
            await forward_bet_info(client, message_text, forwarder_channels, source_channel_name=prefix)
            forwarded_once = True

        # Attach context flags
        bet_info["is_outgoing"] = bool(is_outgoing)
        bet_info["force_incoming"] = bool(cfg.get("force_incoming", False)) and not bool(is_outgoing)
        bet_info["force_outgoing"] = bool(cfg.get("force_outgoing", False)) and bool(is_outgoing)

        # Try immediate resolution first (silent; retry loop will emit combined log line)
        resolved = await resolve_event_and_line(client_api, bet_info, cfg, silent=True)
        if not resolved:
            # Some resolver failures are intentional skips (e.g., odds outside tolerance).
            # In those cases, the resolver already logged the reason and we should not retry.
            if bet_info.get("_no_retry"):
                continue
            # Quick retries: some feeds lag by a few seconds; this avoids requiring manual resend.
            quick_attempts = int(max(0, cfg.get("quick_retry_attempts", 2)))
            quick_delay_seconds = max(0.0, float(cfg.get("quick_retry_delay_seconds", 8.0)))
            for _ in range(quick_attempts):
                if quick_delay_seconds > 0:
                    await asyncio.sleep(quick_delay_seconds)
                resolved = await resolve_event_and_line(client_api, bet_info, cfg, silent=True)
                if resolved:
                    break
            attempts_left = int(max(0, cfg.get("retry_attempts", 0)))
            if resolved:
                # Continue normal flow below using the resolved payload.
                pass
            elif attempts_left <= 0:
                ctx = format_bet_context(bet_info)
                ctx_part = f" {ctx}" if ctx else ""
                await log_message(f"⚠️ No event ID or League ID found.{ctx_part}")
                continue
            elif not resolved:
                # If immediate + quick retries fail, start background retry task
                asyncio.create_task(_retry_resolve_event(client_api, bet_info, cfg, chat, message_id, message_text, client, forwarder_channels))
                continue

        # Try immediate odds enrichment
        ok = enrich_from_odds(client_api, resolved)
        if not ok:
            # Quick retries for odds feed lag before minute-based retries.
            quick_attempts = int(max(0, cfg.get("quick_retry_attempts", 2)))
            quick_delay_seconds = max(0.0, float(cfg.get("quick_retry_delay_seconds", 8.0)))
            for _ in range(quick_attempts):
                if quick_delay_seconds > 0:
                    await asyncio.sleep(quick_delay_seconds)
                ok = enrich_from_odds(client_api, resolved)
                if ok:
                    break
            if not ok:
                # If immediate + quick retries fail, start background retry task
                asyncio.create_task(_retry_enrich_odds(client_api, resolved, bet_info, cfg, chat, message_id, message_text, client, forwarder_channels))
                continue

        # If we get here, both resolution and odds enrichment succeeded immediately
        await _place_bet_immediately(client_api, resolved, cfg, chat, message_id, message_text)


async def _retry_resolve_event(client_api: AsianOddsClient, bet_info: Dict[str, Any], cfg: Dict[str, Any], chat: Optional[str], message_id: Optional[int], original_message: Optional[str] = None, client: Optional[TelegramClient] = None, forwarder_channels: Optional[Union[str, Iterable[str]]] = None) -> None:
    """Background task to retry event resolution without blocking new messages"""
    attempts_left = int(max(0, cfg.get("retry_attempts", 0)))
    interval_minutes = max(0, float(cfg.get("retry_interval_minutes", 0)))
    ctx = format_bet_context(bet_info)
    ctx_part = f" {ctx}" if ctx else ""
    
    if attempts_left <= 0:
        await log_message(f"⚠️ No matching event/league found in fixtures after retries{ctx_part}")
        return
    
    import asyncio
    for attempt in range(attempts_left):
        await log_message(
            f"⚠️ No event ID or League ID found.{ctx_part} "
            f"⏳ Retrying to resolve event/line in {interval_minutes} minute(s)... (attempt {attempt + 1}/{attempts_left})"
        )
        await asyncio.sleep(interval_minutes * 60)
        
        resolved = await resolve_event_and_line(client_api, bet_info, cfg, silent=True)
        if resolved:
            # Now try odds enrichment
            ok = enrich_from_odds(client_api, resolved)
            if ok:
                await _place_bet_immediately(client_api, resolved, cfg, chat, message_id, original_message)
            else:
                # Start odds retry task
                asyncio.create_task(_retry_enrich_odds(client_api, resolved, bet_info, cfg, chat, message_id, original_message, client, forwarder_channels))
            return
    
    await log_message(f"⚠️ No matching event/league found in fixtures after retries{ctx_part}")


async def _retry_enrich_odds(client_api: AsianOddsClient, resolved: Dict[str, Any], bet_info: Dict[str, Any], cfg: Dict[str, Any], chat: Optional[str], message_id: Optional[int], original_message: Optional[str] = None, client: Optional[TelegramClient] = None, forwarder_channels: Optional[Union[str, Iterable[str]]] = None) -> None:
    """Background task to retry odds enrichment without blocking new messages"""
    attempts_left = int(max(0, cfg.get("retry_attempts", 0)))
    interval_minutes = max(0, float(cfg.get("retry_interval_minutes", 0)))
    
    if attempts_left <= 0:
        await log_message(f"⚠️ Event {resolved['eventId']} not found in odds response or missing periods after retries")
        return
    
    import asyncio
    for attempt in range(attempts_left):
        await log_message(f"⏳ Retrying to fetch odds/line in {interval_minutes} minute(s)... (attempt {attempt + 1}/{attempts_left})")
        await asyncio.sleep(interval_minutes * 60)
        
        ok = enrich_from_odds(client_api, resolved)
        if ok:
            await _place_bet_immediately(client_api, resolved, cfg, chat, message_id, original_message)
            return
    
    await log_message(f"⚠️ Event {resolved['eventId']} not found in odds response or missing periods after retries")


async def _refresh_line_before_retry(client_api: AsianOddsClient, bet_info: Dict[str, Any], cfg: Dict[str, Any]) -> bool:
    """Re-resolve fixtures/odds so each retry gets a fresh lineId and price."""
    try:
        refreshed = await resolve_event_and_line(client_api, bet_info, cfg)
    except Exception as exc:
        await log_message(f"⚠️ Bet retry: exception refreshing event/line: {exc}")
        return False

    if not refreshed:
        await log_message("⚠️ Bet retry: unable to refresh event/line from fixtures.")
        return False

    try:
        if not enrich_from_odds(client_api, bet_info):
            await log_message("⚠️ Bet retry: odds refresh failed for current event.")
            return False
    except Exception as exc:
        await log_message(f"⚠️ Bet retry: exception refreshing odds: {exc}")
        return False

    return True


def _bet_type_label(resolved: Dict[str, Any], straight: Optional[dict] = None) -> str:
    if straight and straight.get("betType"):
        return str(straight.get("betType"))
    market = (resolved.get("market_type") or "").lower()
    if market == "ml match":
        return "MONEYLINE"
    if market == "hdp match":
        return "SPREAD"
    if market in {"total points match", "team total points match"}:
        return "TOTAL_POINTS"
    return (resolved.get("market_type") or "UNKNOWN").upper()


def _sport_emoji(sport: str) -> str:
    s = (sport or "").strip().lower()
    if s in {"tennis"}:
        return "🎾"
    if s in {"soccer", "football"}:
        # In this bot, "Football" tips are treated as Soccer
        return "⚽"
    if s in {"basketball"}:
        return "🏀"
    if s in {"baseball"}:
        return "⚾"
    if s in {"hockey", "ice hockey"}:
        return "🏒"
    return "🏟️"


def _extract_failure_reason(*, result: Optional[Dict[str, Any]] = None, straight: Optional[dict] = None, exc: Optional[BaseException] = None) -> str:
    """
    Best-effort extraction of a human-readable failure reason from:
    - HTTP exceptions with a response body
    - AsianOdds API response (Result field)
    - PlacementData fields that may contain error info
    """
    def _pick(d: Any, keys: list[str]) -> Optional[str]:
        if not isinstance(d, dict):
            return None
        for k in keys:
            v = d.get(k)
            if v is None:
                continue
            if isinstance(v, (str, int, float)):
                s = str(v).strip()
                if s:
                    return s
            # Commonly errors can be nested/arrays
            if isinstance(v, (list, dict)):
                try:
                    return json.dumps(v, ensure_ascii=False)
                except Exception:
                    return str(v)
        return None

    # 1) Exception with response body
    if exc is not None:
        try:
            resp = getattr(exc, "response", None)
            if resp is not None:
                try:
                    body = resp.json()
                except Exception:
                    body = None
                if body:
                    return (
                        _pick(body, ["errorMessage", "message", "detail", "error", "errors", "TextMessage"])
                        or _pick(body, ["errorCode", "code", "status", "Code"])
                        or str(body)
                    )
        except Exception:
            pass
        # Fallback: exception text
        return str(exc).strip() or "Unknown error"

    # 2) AsianOdds Result field
    if result:
        ao_result = result.get("Result", {})
        reason = _pick(ao_result, ["TextMessage", "errorMessage", "message", "detail", "error", "errors"])
        if reason:
            return reason
        reason = _pick(ao_result, ["errorCode", "code", "status", "Code"])
        if reason:
            return reason

    # 3) PlacementData info (straight is now placement_data for AsianOdds)
    if straight:
        if isinstance(straight, list) and straight:
            pd = straight[0]
            reason = _pick(pd, ["ErrorMessage", "RejectReason", "Status"])
            if reason:
                return reason
        elif isinstance(straight, dict):
            reason = _pick(straight, ["ErrorMessage", "RejectReason", "Status", "errorMessage", "message"])
            if reason:
                return reason

    reason = _pick(result, ["errorMessage", "message", "detail", "error", "errors", "TextMessage"])
    if reason:
        return reason
    reason = _pick(result, ["errorCode", "code", "status", "Code"])
    if reason:
        return reason

    return "Unknown reason"


def _is_bet_successful_ao(result: Optional[Dict[str, Any]]) -> bool:
    """Check if AsianOdds bet placement was successful."""
    if not result:
        return False
    
    ao_result = result.get("Result", {})
    placement_data = ao_result.get("PlacementData", [])
    
    # Check for successful placement in PlacementData
    if placement_data:
        for pd in placement_data:
            if pd.get("Status") == "Success" or pd.get("BetId"):
                return True
            # Check for rejection
            if pd.get("Rejected") or pd.get("Status") == "Rejected":
                return False
    
    # Check Code field (0 = success)
    if result.get("Code") == 0 and placement_data:
        return True
    
    return False


def _format_place_message_ao(result: Dict[str, Any], resolved: Dict[str, Any]) -> str:
    """Format success message for AsianOdds bet placement."""
    ao_result = result.get("Result", {})
    placement_data = ao_result.get("PlacementData", [])
    
    sport = resolved.get('sport') or 'Unknown Sport'
    sport_icon = _sport_emoji(sport)
    league = resolved.get('title') or 'Unknown League'
    team1 = resolved.get('home') or 'Team 1'
    team2 = resolved.get('away') or 'Team 2'
    risk = resolved.get('stake') or 'N/A'
    price = resolved.get('api_odds') or resolved.get('odds') or 'N/A'
    team_name = resolved.get('selection') or 'Unknown Selection'
    market = resolved.get('market_type') or 'Unknown'
    bet_type = 'TOTAL_POINTS' if 'total' in market.lower() else ('MONEYLINE' if 'ml' in market.lower() else 'HANDICAP')
    tip_or_channel = _format_tipster_or_channel_line(resolved)
    
    # Extract info from placement data if available
    bet_id = 'Pending...'
    bookie = resolved.get('preferred_bookie') or 'N/A'
    if placement_data:
        pd = placement_data[0]
        bet_id = pd.get('BetId') or pd.get('Reference') or 'Pending...'
        bookie = pd.get('Bookie') or bookie
        if pd.get('Price'):
            price = pd.get('Price')
        if pd.get('Amount'):
            risk = pd.get('Amount')
    
    # Calculate potential win
    try:
        win_amt = round(float(risk) * (float(price) - 1), 2) if float(price) > 0 else 'N/A'
    except:
        win_amt = 'N/A'
    
    msg_parts = ["✅ *Bet Placed Successfully!*"]
    if tip_or_channel:
        msg_parts.append(tip_or_channel)
    msg_parts.append("")
    msg_parts.extend(
        [
            f"{sport_icon} *{sport}* - {league}",
            f"🆚 {team1} vs {team2}",
            "",
            f"💰 Stake: {risk}",
            f"📈 Odds: {price}",
            f"📊 To Win: {win_amt}",
            "",
            f"📌 Selection: *{team_name}*",
            f"📝 Bet Type: {bet_type}",
            f"🏦 Bookie: {bookie}",
        ]
    )
    
    if bet_id and bet_id != 'Pending...':
        msg_parts.append(f"🏷️ Bet ID: {bet_id}")
    
    return "\n".join(msg_parts)


def _is_duplicate_unique_request_reason(reason: Optional[str]) -> bool:
    r = str(reason or "").strip().upper()
    return "DUPLICATE_UNIQUE_REQUEST_ID" in r


def _renew_unique_request_id(bet_info: Dict[str, Any]) -> None:
    """Generate a fresh uniqueRequestId to avoid duplicate-id rejection on retries."""
    bet_info["uuid"] = str(uuid.uuid4())


def _format_tipster_or_channel_line(info: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    Return a single context line identifying the origin for user-facing logs.
    Preference order:
      1) Tipster (when present and not "default")
      2) Source channel (when available)
    """
    if not info:
        return None

    tipster_raw = info.get("tipster")
    tipster = str(tipster_raw).strip() if tipster_raw is not None else ""
    if tipster and tipster.lower() not in {"default", "unknown", "n/a", "na", "none"}:
        return f"👤 Tipster: {tipster}"

    channel_raw = info.get("source_channel") or info.get("channel") or info.get("channel_name")
    channel = str(channel_raw).strip() if channel_raw is not None else ""
    if channel:
        return f"📣 Channel: {channel}"

    return None


def _format_failed_bet_message(
    resolved: Dict[str, Any],
    *,
    reason: str,
    straight: Optional[dict] = None,
    retry_minutes: Optional[float] = None,
    retry_attempt: Optional[int] = None,
    retry_total: Optional[int] = None,
) -> str:
    sport = (straight or {}).get("sportName") or resolved.get("sport") or "Unknown Sport"
    sport_icon = _sport_emoji(sport)
    league = (straight or {}).get("leagueName") or resolved.get("title") or "Unknown League"
    team1 = (straight or {}).get("team1") or resolved.get("home") or "Team 1"
    team2 = (straight or {}).get("team2") or resolved.get("away") or "Team 2"
    stake = (straight or {}).get("risk") or resolved.get("stake") or "N/A"
    odds = (straight or {}).get("price") or resolved.get("odds") or "N/A"
    selection = (straight or {}).get("teamName") or resolved.get("selection") or "Unknown Selection"
    bet_type = _bet_type_label(resolved, straight)
    ctx = format_bet_context(resolved)
    tip_or_channel = _format_tipster_or_channel_line(resolved)

    msg_parts = ["❌ *Bet Failed!*"]
    if tip_or_channel:
        msg_parts.append(tip_or_channel)
    msg_parts.append("")
    msg_parts.extend(
        [
            f"{sport_icon} *{sport}* - {league}",
            f"🆚 {team1} vs {team2}",
            "",
            f"💰 Stake: €{stake}",
            f"📈 Odds: {odds}",
            "",
            f"📌 Selection: *{selection}*",
            f"📝 Bet Type: {bet_type}",
        ]
    )
    if ctx:
        msg_parts.append(f"🏷️ {ctx}")
    msg_parts.append(f"🚫 Reason: {reason}")

    if retry_minutes is not None and retry_attempt is not None and retry_total is not None:
        msg_parts.append(
            f"⏳ Retrying in {retry_minutes} minute(s)... (attempt {retry_attempt}/{retry_total})"
        )

    return "\n".join(msg_parts)


async def _retry_bet_placement(client_api: AsianOddsClient, resolved: Dict[str, Any], cfg: Dict[str, Any], chat: Optional[str], message_id: Optional[int], original_message: Optional[str] = None, first_reason: Optional[str] = None) -> None:
    """Background task to retry bet placement without blocking new messages"""
    attempts_left = int(max(0, cfg.get("place_result_retry_attempts", 0)))
    interval_place = max(0, float(cfg.get("place_result_retry_interval_minutes", 0)))
    
    if attempts_left <= 0:
        await log_message(
            _format_failed_bet_message(
                resolved,
                reason=first_reason or "Bet placement failed and no retries configured",
            )
        )
        return
    
    import asyncio
    for attempt in range(attempts_left):
        # Before logging a new retry failure message, check API running bets.
        # This prevents noisy "retake" failure messages when the bet is already placed.
        try:
            if not (resolved.get("force_incoming", False) or resolved.get("force_outgoing", False)) and is_duplicate_running_bet(client_api, resolved):
                await log_message(
                    f"⚠️ Bet already placed for event {resolved.get('eventId')}, stopping retries."
                )
                if chat is not None and message_id is not None:
                    try:
                        mark_bet_for_message(chat, int(message_id))
                    except Exception:
                        pass
                try:
                    if resolved.get("bet_signature"):
                        mark_bet_signature(resolved["bet_signature"])
                except Exception:
                    pass
                return
        except Exception:
            # Non-fatal; continue with regular retry flow.
            pass

        await log_message(
            _format_failed_bet_message(
                resolved,
                reason=first_reason or "Bet placement failed",
                retry_minutes=interval_place,
                retry_attempt=attempt + 1,
                retry_total=attempts_left,
            )
        )
        await asyncio.sleep(interval_place * 60)
        
        try:
            # If bet already appears in running bets, stop retrying and mark as done
            try:
                if not (resolved.get("force_incoming", False) or resolved.get("force_outgoing", False)) and is_duplicate_running_bet(client_api, resolved):
                    await log_message(
                        f"⚠️ Bet already placed for event {resolved.get('eventId')}, stopping retries."
                    )
                    if chat is not None and message_id is not None:
                        try:
                            mark_bet_for_message(chat, int(message_id))
                        except Exception:
                            pass
                    # Mark signature too to avoid cross-channel duplicates
                    try:
                        if resolved.get("bet_signature"):
                            mark_bet_signature(resolved["bet_signature"])
                    except Exception:
                        pass
                    return
            except Exception:
                # Non-fatal; continue with balance check and retry
                pass

            # Refresh line/odds so we don't keep reusing stale lineIds after a failure
            refreshed = await _refresh_line_before_retry(client_api, resolved, cfg)
            if not refreshed:
                # Could not refresh line info this attempt; try again on next loop
                continue

            # Re-run duplicate check with the refreshed lineId to avoid double-bets if a manual bet slipped in
            try:
                if not (resolved.get("force_incoming", False) or resolved.get("force_outgoing", False)) and is_duplicate_running_bet(client_api, resolved):
                    await log_message(
                        f"⚠️ Bet already placed for event {resolved.get('eventId')} (after refresh), stopping retries."
                    )
                    if chat is not None and message_id is not None:
                        try:
                            mark_bet_for_message(chat, int(message_id))
                        except Exception:
                            pass
                    try:
                        if resolved.get("bet_signature"):
                            mark_bet_signature(resolved["bet_signature"])
                    except Exception:
                        pass
                    return
            except Exception:
                pass

            # Balance check before retrying bet placement
            try:
                balance_data = client_api.get_account_summary()
                result = balance_data.get("Result", {})
                available = float(result.get("AvailableCredit", 0.0))
            except Exception:
                available = 0.0

            stake_required = float(resolved.get("stake", 0.0))
            min_stake_allowed = float(cfg.get("min_stake", 0.0))

            if available < stake_required:
                await log_message(
                    _format_failed_bet_message(
                        resolved,
                        reason=f"Insufficient balance (have {available}, need {stake_required}).",
                    )
                )
                return
            if (available - stake_required) < min_stake_allowed:
                await log_message(
                    _format_failed_bet_message(
                        resolved,
                        reason=f"Remaining balance would drop below min_stake ({min_stake_allowed}).",
                    )
                )
                return

            # Always use a fresh request id for each retry placement attempt.
            _renew_unique_request_id(resolved)
            result = place_bet(client_api, resolved)
            
            # AsianOdds response format: Result.PlacementData contains bet info
            ao_result = result.get("Result", {})
            placement_data = ao_result.get("PlacementData", [])
            
            # Check if bet placement was successful
            def _is_bet_successful_ao(res: dict, placements: list) -> bool:
                # Check for successful placement
                if placements:
                    for pd in placements:
                        if pd.get("Status") == "Success" or pd.get("BetId"):
                            return True
                # Also check Code field
                if res.get("Code") == 0:
                    return True
                return False
            
            if _is_bet_successful_ao(result):
                # Success! Format and send message
                msg = _format_place_message_ao(result, resolved)
                await log_message(msg)
                
                # Mark this message as processed for betting purposes
                if chat is not None and message_id is not None:
                    try:
                        mark_bet_for_message(chat, int(message_id))
                    except Exception:
                        pass
                # Also mark signature to prevent duplicates from other channels
                try:
                    if resolved.get("bet_signature"):
                        mark_bet_signature(resolved["bet_signature"])
                except Exception:
                    pass
                return
        except Exception as exc:
            reason = _extract_failure_reason(exc=exc)
            first_reason = reason
            await log_message(_format_failed_bet_message(resolved, reason=reason))
    
    await log_message(
        _format_failed_bet_message(
            resolved,
            reason=first_reason or "Bet placement failed after all retry attempts",
        )
    )


async def _place_bet_immediately(client_api: AsianOddsClient, resolved: Dict[str, Any], cfg: Dict[str, Any], chat: Optional[str], message_id: Optional[int], original_message: Optional[str] = None) -> None:
    """Place bet immediately after successful resolution and odds enrichment"""

    try:
        # Skip duplicate-running check if forcing (incoming/outgoing)
        if not (resolved.get("force_incoming", False) or resolved.get("force_outgoing", False)):
            try:
                is_duplicate = is_duplicate_running_bet(client_api, resolved)
                if is_duplicate:
                    await log_message(
                        f"⚠️ Duplicate bet detected on event {resolved['eventId']}, skipping..."
                    )
                    # Mark this message as processed to prevent catch-up from retrying it again
                    if chat is not None and message_id is not None:
                        try:
                            mark_bet_for_message(chat, int(message_id))
                        except Exception:
                            pass
                    # Mark signature too to avoid cross-channel duplicates
                    try:
                        if resolved.get("bet_signature"):
                            mark_bet_signature(resolved["bet_signature"])
                    except Exception:
                        pass
                    return
            except Exception as dup_check_exc:
                await log_message(f"⚠️ Running bet duplicate check failed: {dup_check_exc}")
    except Exception:
        # Non-fatal
        pass

    try:
        import asyncio

        # Balance check before placing the bet
        try:
            balance_data = client_api.get_account_summary()
            result = balance_data.get("Result", {})
            available = float(result.get("AvailableCredit", 0.0))
        except Exception:
            available = 0.0

        stake_required = float(resolved.get("stake", 0.0))
        min_stake_allowed = float(cfg.get("min_stake", 0.0))

        if available < stake_required:
            await log_message(
                _format_failed_bet_message(
                    resolved,
                    reason=f"Insufficient balance (have {available}, need {stake_required}).",
                )
            )
            return
        if (available - stake_required) < min_stake_allowed:
            await log_message(
                _format_failed_bet_message(
                    resolved,
                    reason=f"Remaining balance would drop below min_stake ({min_stake_allowed}).",
                )
            )
            return

        # Read retry configuration upfront so it's available if first placement fails
        attempts_left_place = int(max(0, cfg.get("place_result_retry_attempts", 0)))
        interval_place = max(0, float(cfg.get("place_result_retry_interval_minutes", 0)))
        quick_place_attempts = int(max(0, cfg.get("quick_place_retry_attempts", 2)))
        quick_place_delay_seconds = max(0.0, float(cfg.get("quick_place_retry_delay_seconds", 5.0)))

        # Check if bet placement was successful by looking for success indicators
        def _is_bet_successful(st: dict) -> bool:
            # Treat bookmaker status as primary truth:
            # - ACCEPTED → definitely successful
            # - PENDING_ACCEPTANCE → request reached the book and is in-progress; do NOT re-send
            status = str(st.get("betStatus") or st.get("status") or "").strip().upper()
            if status in {"ACCEPTED", "PENDING_ACCEPTANCE"}:
                return True
            if status in {"REJECTED", "CANCELLED", "DECLINED"}:
                return False

            # Fallback: basic success indicators - if we have stake/risk and odds/price and no obvious error markers,
            # we assume the bet was placed.
            has_stake = st.get("risk") is not None and st.get("risk") != ""
            has_odds = st.get("price") is not None and st.get("price") != ""
            has_error = "error" in str(st).lower() or "failed" in str(st).lower()
            return has_stake and has_odds and not has_error

        async def _already_running_stop(note: str) -> bool:
            """
            Before any re-take/retry placement, confirm via API whether bet is already running.
            """
            if resolved.get("force_incoming", False) or resolved.get("force_outgoing", False):
                return False
            try:
                if is_duplicate_running_bet(client_api, resolved):
                    await log_message(
                        f"⚠️ Bet already placed for event {resolved.get('eventId')} ({note}), skipping re-take."
                    )
                    if chat is not None and message_id is not None:
                        try:
                            mark_bet_for_message(chat, int(message_id))
                        except Exception:
                            pass
                    try:
                        if resolved.get("bet_signature"):
                            mark_bet_signature(resolved["bet_signature"])
                    except Exception:
                        pass
                    return True
            except Exception:
                # Non-fatal duplicate check error.
                pass
            return False

        # Place bet and check if it was successful
        result = None
        first_reason: Optional[str] = None
        try:
            result = place_bet(client_api, resolved)
        except Exception as exc:
            first_reason = _extract_failure_reason(exc=exc)
            # If the first call reached the API but id was already consumed, retry immediately with a fresh id.
            if _is_duplicate_unique_request_reason(first_reason):
                if await _already_running_stop("duplicate request id check"):
                    return
                try:
                    _renew_unique_request_id(resolved)
                    result = place_bet(client_api, resolved)
                except Exception as dup_exc:
                    first_reason = _extract_failure_reason(exc=dup_exc)
                    result = None

        # If initial placement fails or returns an unsuccessful payload, do short quick retries first.
        if result is None or not _is_bet_successful_ao(result):
            if result is not None:
                first_reason = _extract_failure_reason(result=result)
            
            for _ in range(quick_place_attempts):
                if quick_place_delay_seconds > 0:
                    await asyncio.sleep(quick_place_delay_seconds)
                refreshed = await _refresh_line_before_retry(client_api, resolved, cfg)
                if not refreshed:
                    continue
                if await _already_running_stop("quick retry pre-check"):
                    return
                try:
                    _renew_unique_request_id(resolved)
                    result = place_bet(client_api, resolved)
                    if _is_bet_successful_ao(result):
                        break
                    first_reason = _extract_failure_reason(result=result)
                    result = None
                except Exception as quick_exc:
                    first_reason = _extract_failure_reason(exc=quick_exc)
                    result = None

        if result is None or not _is_bet_successful_ao(result):
            # Start minute-based retry if configured
            if attempts_left_place > 0:
                asyncio.create_task(_retry_bet_placement(client_api, resolved, cfg, chat, message_id, original_message, first_reason))
            else:
                await log_message(_format_failed_bet_message(resolved, reason=first_reason or "Bet placement failed"))
            return

        # Bet was successful - format and send message
        msg = _format_place_message_ao(result, resolved)
        await log_message(msg)
        # Mark this message as processed for betting purposes
        if chat is not None and message_id is not None:
            try:
                mark_bet_for_message(chat, int(message_id))
            except Exception:
                pass
        # Also mark signature to prevent duplicates from other channels
        try:
            if resolved.get("bet_signature"):
                mark_bet_signature(resolved["bet_signature"])
        except Exception:
            pass
    except Exception as exc:
        await log_message(f"Error placing bet {resolved['selection']}: {exc}")


def run() -> None:
    cfg = load_config()
    env = load_env()

    client_api = AsianOddsClient(env.asianodds.username, env.asianodds.password)
    # Discover account-available sports from AsianOdds API (used by parser for sportId mapping, incl. Rugby Union).
    try:
        global _API_SPORT_ID_CACHE
        _API_SPORT_ID_CACHE = _build_api_sport_id_cache(client_api)
        set_runtime_api_sport_ids(_API_SPORT_ID_CACHE)
    except Exception:
        pass

    # Check balance at startup (log to console; channel log will initialize after client starts)
    try:
        balance_data = client_api.get_account_summary()
        result = balance_data.get("Result", {})
        available = float(result.get("AvailableCredit", 0.0))
        outstanding = float(result.get("Outstanding", 0.0))
        if available < float(cfg.get("min_stake", 0.0)):
            print(f"⚠️ Available credit {available} is below min_stake {cfg.get('min_stake')}.")
        else:
            print(f"💰 Available credit: {available}, Outstanding: {outstanding}.")
    except Exception as e:
        print(f"⚠️ Failed to fetch balance at startup: {e}")

    client = TelegramClient("session_asianodds", env.telegram.api_id, env.telegram.api_hash)

    async def telegram_sink(message: str) -> None:
        try:
            peer, err = _require_peer("TELEGRAM_CHANNEL", env.telegram.channel)
            if peer is None:
                print(err or "⚠️ TELEGRAM_CHANNEL invalid; cannot send log message.")
                return
            await client.send_message(peer, message)
        except Exception:
            # Avoid crashing logging
            pass

    # connect logger sink lazily
    import telegram_bot.logger as logger_mod

    logger_mod.async_log_sink = telegram_sink

    # Determine which channels to listen to (main channel plus any additional listeners)
    channels_to_listen = []
    for ch in [env.telegram.channel, *env.telegram.listener_channels]:
        peer = _to_peer(ch)
        if peer is None:
            continue
        if peer not in channels_to_listen:
            channels_to_listen.append(peer)
    
    @client.on(events.NewMessage(chats=channels_to_listen))
    async def handler(event):
        message_text = (event.message.message or "").strip()
        # Ignore our own outgoing messages to prevent feedback loops, unless explicitly forced; always allow commands
        try:
            is_outgoing = getattr(event.message, "out", False)
            if is_outgoing and not message_text.startswith("/") and not cfg.get("force_outgoing", False):
                # If an outgoing message looks like a betting tip, log a hint (common when users paste tips manually).
                try:
                    looks_like_tip = False
                    t = message_text
                    if t:
                        looks_like_tip = bool(
                            re.search(r"\bPARI\b|\bPRONO\b|\bML\b|\bHDP\b|\bTotal\s+Points\b|\bMise\b|@\s*[0-9]", t, re.IGNORECASE)
                            or ("\U0001F3BE" in t)  # 🎾
                            or ("\u26BD" in t)      # ⚽
                            or ("\U0001F3C0" in t)  # 🏀
                        )
                    if looks_like_tip:
                        await log_message("ℹ️ Outgoing message ignored (sent by this Telegram account). If you want the bot to process pasted/forwarded tips you send, use `/forceoutgoing on`.")
                except Exception:
                    pass
                return
        except Exception:
            is_outgoing = False

        # Commands
        if message_text.startswith("/"):
            parts = message_text.split()
            cmd = parts[0].lower()

            if cmd == "/help":
                help_text = (
                    "📖 *Help / Commands*\n\n"
                    "🧰 *General Management:*\n"
                    "/help → Show this help message\n"
                    "/balance → Show current account balance\n"
                    "/showconfig → Show current configuration\n"
                    "/exportwagers [days|YYYY-MM-DD YYYY-MM-DD] [running|settled|all] [excel|json] → Export wager history (default 7 days, max 30-day span)\n\n"
                    "💰 *Betting Settings:*\n"
                    "/stake <value> → Set base stake (minimum 5 EUR)\n"
                    "/minstake <value> → Set minimum stake\n"
                    "/maxstake <value> → Set maximum stake\n"
                    "/minunit <value> → Set minimum unit size\n"
                    "/maxunit <value> → Set maximum unit size\n"
                    "/sports <tennis|soccer|football|basketball|rugby|rugbyunion|both|all> → Enable betting on sports\n"
                    "/leagues <tennis|soccer|football|basketball|rugby|rugbyunion> [filter] → List AsianOdds leagues for a sport (optional name filter)\n"
                    "/bettype <prematch|live|both> → Set global bet type preference (pre-match vs live)\n"
                    "/bettype <tennis|soccer|football|basketball|rugby|rugbyunion> <prematch|live|both|clear> → Set per-sport bet type preference\n"
                    "/bettype list → Show global + per-sport bet type preferences\n"
                    "/odds <tolerance> → Set odds tolerance (e.g. 0.05)\n"
                    "/minglobalodds [value] → Set or show global minimum odds (default 1.15); tips and API below this are skipped. Use 0 to disable floor.\n\n"
                    "📡 *Channel Management:*\n"
                    "/setchannel [channel|none|blank|clear] → Set main channel (TELEGRAM_CHANNEL - for listening and logging). Use without argument or 'none'/'blank'/'clear' to clear.\n"
                    "/setforwarder [channel(s)|none|blank|clear] → Set forwarder channel(s) (comma/space separated). Supports @username, -100 chat IDs, or t.me links. Use without argument or 'none'/'blank'/'clear' to clear.\n"
                    "/setlistener [channel(s)|none|blank|clear] → Set additional listener channel(s) (comma/space separated; TELEGRAM_CHANNEL is always listened). Supports @username, -100 chat IDs, or t.me links. Use without argument or 'none'/'blank'/'clear' to clear.\n"
                    "/showchannels → Show current channel configuration\n\n"
                    "🏷️ *Channel Overrides & Forwarding:*\n"
                    "/channelstake <channel> <base|min|max|minunit|maxunit> <value> → Set per-channel stake settings (channel can be @username, -100 id, or channel title)\n"
                    "/channelstakelist → List all configured channel stake overrides\n"
                    "/channelstakeremove <channel> → Remove a channel stake override\n"
                    "/channelodds <channel> <tolerance> → Set per-channel odds tolerance\n"
                    "/channeloddslist → List per-channel odds tolerance overrides\n"
                    "/channeloddsremove <channel> → Remove a per-channel odds tolerance override\n"
                    "/channelforward <channel> <all|bet> → Forward all messages or only bet/tip messages for that source channel\n"
                    "/channelforwardlist → List all configured per-channel forwarding modes\n"
                    "/channelforwardremove <channel> → Remove a per-channel forwarding mode (reverts to bet-only)\n\n"
                    "🎯 *Tipster Management:*\n"
                    "/tipsterstake <tipster> <base|min|max|minunit|maxunit> <value> → Set tipster-specific settings\n"
                    "/tipsterodds <tipster> <tolerance> → Set tipster-specific odds tolerance\n"
                    "/tipsteroddslist → List tipster-specific odds tolerance overrides\n"
                    "/tipsteroddsremove <tipster> → Remove a tipster odds tolerance override\n"
                    "/tipsterlist → List all configured tipsters\n"
                    "/tipsterremove <tipster> → Remove tipster settings\n\n"
                    "🛠️ *System Management:*\n"
                    "/forceoutgoing <on|off> → Force placing outgoing bet messages (never forwarded)\n"
                    "/forceincoming <on|off> → Force placing incoming bet messages (bypasses duplicate-running checks; balance checks remain)\n"
                    "/catchup <on|off> → Enable/disable catch-up on startup\n"
                    "/catchuplimit <n> → Set catch-up message scan limit\n"
                    "/retry <attempts> <minutes> → Event retry (resolving event/line)\n"
                    "/betretry <attempts> <minutes> → Bet retry (bet placement failures)\n"
                    "/restart → Restart the bot\n"
                )
                await event.reply(help_text, parse_mode="markdown")
                return

            elif cmd == "/stake" and len(parts) > 1:
                try:
                    stake = float(parts[1])
                    if stake < cfg["min_stake"]:
                        await event.reply(f"⚠️ Minimum stake is {cfg['min_stake']} EUR.")
                    elif "max_stake" in cfg and stake > cfg["max_stake"]:
                        await event.reply(f"⚠️ Maximum stake is {cfg['max_stake']} EUR.")
                    else:
                        cfg["base_stake"] = stake
                        save_config(cfg)
                        await event.reply(f"✅ Base stake updated to €{stake}")
                except ValueError:
                    await event.reply("⚠️ Invalid stake amount.")
                return

            elif cmd == "/minstake" and len(parts) > 1:
                try:
                    min_stake = float(parts[1])
                    if "max_stake" in cfg and min_stake > cfg["max_stake"]:
                        await event.reply("⚠️ Minimum stake cannot be higher than maximum stake.")
                    else:
                        cfg["min_stake"] = min_stake
                        save_config(cfg)
                        await event.reply(f"✅ Minimum stake updated to €{min_stake}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/maxstake" and len(parts) > 1:
                try:
                    max_stake = float(parts[1])
                    if "min_stake" in cfg and max_stake < cfg["min_stake"]:
                        await event.reply("⚠️ Maximum stake cannot be lower than minimum stake.")
                    else:
                        cfg["max_stake"] = max_stake
                        save_config(cfg)
                        await event.reply(f"✅ Maximum stake updated to €{max_stake}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/minunit" and len(parts) > 1:
                try:
                    min_unit = float(parts[1])
                    if "max_unit" in cfg and min_unit > cfg["max_unit"]:
                        await event.reply("⚠️ Minimum unit cannot be higher than maximum unit.")
                    else:
                        cfg["min_unit"] = min_unit
                        save_config(cfg)
                        await event.reply(f"✅ Minimum unit updated to {min_unit}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/maxunit" and len(parts) > 1:
                try:
                    max_unit = float(parts[1])
                    if "min_unit" in cfg and max_unit < cfg["min_unit"]:
                        await event.reply("⚠️ Maximum unit cannot be lower than minimum unit.")
                    else:
                        cfg["max_unit"] = max_unit
                        save_config(cfg)
                        await event.reply(f"✅ Maximum unit updated to {max_unit}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/sports" and len(parts) > 1:
                choice = parts[1].lower()
                # Initialize allow_soccer if not present (for backwards compatibility)
                if "allow_soccer" not in cfg:
                    cfg["allow_soccer"] = cfg.get("allow_football", True)
                if "allow_rugby_union" not in cfg:
                    cfg["allow_rugby_union"] = True
                
                if choice == "tennis":
                    cfg["allow_tennis"] = True
                    cfg["allow_soccer"] = False
                    cfg["allow_football"] = False
                    cfg["allow_basketball"] = False
                    cfg["allow_rugby_union"] = False
                elif choice in ("soccer", "football"):
                    cfg["allow_tennis"] = False
                    cfg["allow_soccer"] = True
                    cfg["allow_football"] = True  # keep in sync
                    cfg["allow_basketball"] = False
                    cfg["allow_rugby_union"] = False
                elif choice == "basketball":
                    cfg["allow_tennis"] = False
                    cfg["allow_soccer"] = False
                    cfg["allow_football"] = False
                    cfg["allow_basketball"] = True
                    cfg["allow_rugby_union"] = False
                elif choice in ("rugby", "rugbyunion", "rugby_union"):
                    cfg["allow_tennis"] = False
                    cfg["allow_soccer"] = False
                    cfg["allow_football"] = False
                    cfg["allow_basketball"] = False
                    cfg["allow_rugby_union"] = True
                elif choice == "both":
                    # Backwards-compat: both = tennis + soccer/football
                    cfg["allow_tennis"] = True
                    cfg["allow_soccer"] = True
                    cfg["allow_football"] = True
                    cfg["allow_basketball"] = False
                    cfg["allow_rugby_union"] = False
                elif choice == "all":
                    cfg["allow_tennis"] = True
                    cfg["allow_soccer"] = True
                    cfg["allow_football"] = True
                    cfg["allow_basketball"] = True
                    cfg["allow_rugby_union"] = True
                else:
                    await event.reply("⚠️ Use: /sports tennis | soccer | football | basketball | rugby | rugbyunion | both | all")
                    return
                save_config(cfg)
                await event.reply(
                    f"✅ Sports updated: Tennis={cfg['allow_tennis']} Soccer={cfg['allow_soccer']} Football={cfg['allow_football']} Basketball={cfg['allow_basketball']} RugbyUnion={cfg.get('allow_rugby_union', True)}"
                )
                return

            elif cmd in ("/leagues", "/league"):
                usage = "⚠️ Use: /leagues <tennis|soccer|football|basketball|rugby|rugbyunion> [filter]"
                if len(parts) < 2:
                    await event.reply(usage)
                    return

                sport_key = parts[1].lower()
                sport_map = {
                    "tennis": ("Tennis", _get_sport_id(client_api, "tennis", 33)),
                    "soccer": ("Soccer", _get_sport_id(client_api, "soccer", 29)),
                    "football": ("Soccer", _get_sport_id(client_api, "soccer", 29)),
                    "basketball": ("Basketball", _get_sport_id(client_api, "basketball", 4)),
                    "rugby": ("Rugby Union", _get_sport_id(client_api, "rugby union")),
                    "rugbyunion": ("Rugby Union", _get_sport_id(client_api, "rugby union")),
                    "rugby_union": ("Rugby Union", _get_sport_id(client_api, "rugby union")),
                }
                sport_entry = sport_map.get(sport_key)
                if not sport_entry:
                    await event.reply(usage)
                    return

                sport_label, sport_id = sport_entry
                if not sport_id:
                    await event.reply("⚠️ Rugby Union is not available in your current AsianOdds API sports list.")
                    return
                filter_text = " ".join(parts[2:]).strip().lower()
                temp_path = None

                try:
                    leagues_resp = client_api.get_leagues(sport_id)
                    leagues = leagues_resp.get("leagues") or leagues_resp.get("league") or []
                    if not leagues:
                        await event.reply(f"⚠️ No leagues found for {sport_label}.")
                        return

                    league_rows = []
                    for lg in leagues:
                        name = (lg.get("name") or "Unknown League").strip()
                        lid = lg.get("id")
                        if filter_text and filter_text not in name.lower():
                            continue
                        # Deduplicate by league id (keep last name seen)
                        league_rows.append((lid, name))

                    # Apply deduplication after filtering
                    dedup: Dict[Any, str] = {}
                    for lid, name in league_rows:
                        dedup[lid] = name

                    if not dedup:
                        if filter_text:
                            await event.reply(f"⚠️ No leagues matched '{filter_text}' for {sport_label}.")
                        else:
                            await event.reply(f"⚠️ No leagues found for {sport_label}.")
                        return

                    lines = [f"🏟️ Leagues for {sport_label} ({len(dedup)} found):"]
                    for lid, name in sorted(dedup.items(), key=lambda kv: (str(kv[1]).lower(), kv[0])):
                        lines.append(f"- {name} (ID: {lid})")

                    output = "\n".join(lines)
                    if len(output) > 3500:
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w", encoding="utf-8") as tmp:
                            tmp.write(output)
                            temp_path = tmp.name
                        await client.send_file(
                            event.chat_id,
                            temp_path,
                            caption=f"🏟️ Leagues for {sport_label}",
                            reply_to=event.id,
                        )
                    else:
                        await event.reply(output)
                except Exception as exc:
                    await event.reply(f"⚠️ Failed to fetch leagues for {sport_label}: {exc}")
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                return

            elif cmd == "/bettype":
                # Usage:
                #   /bettype prematch|live|both
                #   /bettype <tennis|soccer|football|basketball|rugby|rugbyunion> prematch|live|both|clear
                #   /bettype list
                if len(parts) <= 1:
                    await event.reply(
                        "⚠️ Use:\n"
                        "- /bettype prematch | live | both\n"
                        "- /bettype <tennis|soccer|football|basketball|rugby|rugbyunion> prematch | live | both | clear\n"
                        "- /bettype list"
                    )
                    return

                sub = parts[1].lower().strip()

                # Initialize allow_prematch and allow_live if not present (for backwards compatibility)
                # Also migrate old allow_pregame to allow_prematch
                if "allow_pregame" in cfg and "allow_prematch" not in cfg:
                    cfg["allow_prematch"] = cfg.pop("allow_pregame")
                if "allow_prematch" not in cfg:
                    cfg["allow_prematch"] = True
                if "allow_live" not in cfg:
                    cfg["allow_live"] = True
                if "bettype_by_sport" not in cfg or not isinstance(cfg.get("bettype_by_sport"), dict):
                    cfg["bettype_by_sport"] = {}

                if sub == "list":
                    bts = cfg.get("bettype_by_sport") or {}
                    lines = [
                        f"🎲 Global bettype: Pre-match={cfg['allow_prematch']} Live={cfg['allow_live']}",
                        "🎯 Per-sport overrides:",
                    ]
                    if isinstance(bts, dict) and bts:
                        for k in sorted(bts.keys()):
                            lines.append(f"- {k}: {bts.get(k)}")
                    else:
                        lines.append("- (none)")
                    await event.reply("\n".join(lines))
                    return

                # Global mode: /bettype prematch|live|both
                if len(parts) == 2 and sub in {"prematch", "live", "both"}:
                    if sub == "prematch":
                        cfg["allow_prematch"] = True
                        cfg["allow_live"] = False
                    elif sub == "live":
                        cfg["allow_prematch"] = False
                        cfg["allow_live"] = True
                    else:  # both
                        cfg["allow_prematch"] = True
                        cfg["allow_live"] = True
                    save_config(cfg)
                    await event.reply(
                        f"✅ Global bettype updated: Pre-match={cfg['allow_prematch']} Live={cfg['allow_live']}"
                    )
                    return

                # Per-sport mode: /bettype <sport> <choice>
                if len(parts) >= 3:
                    sport_raw = sub
                    choice = parts[2].lower().strip()
                    sport_key = sport_raw
                    if sport_key == "football":
                        sport_key = "soccer"
                    if sport_key in {"rugby", "rugbyunion", "rugby_union"}:
                        sport_key = "rugby_union"
                    if sport_key not in {"tennis", "soccer", "basketball", "rugby_union"}:
                        await event.reply("⚠️ Unknown sport. Use: tennis | soccer | football | basketball | rugby | rugbyunion")
                        return
                    if choice == "clear":
                        cfg["bettype_by_sport"].pop(sport_key, None)
                        save_config(cfg)
                        await event.reply(f"✅ Cleared per-sport bettype override for {sport_key}.")
                        return
                    if choice not in {"prematch", "live", "both"}:
                        await event.reply("⚠️ Use: prematch | live | both | clear")
                        return
                    cfg["bettype_by_sport"][sport_key] = choice
                    save_config(cfg)
                    await event.reply(f"✅ Per-sport bettype updated: {sport_key} = {choice}")
                    return

                await event.reply("⚠️ Use: /bettype prematch | live | both  OR  /bettype <sport> <prematch|live|both|clear>  OR  /bettype list")
                return

            elif cmd == "/odds" and len(parts) > 1:
                try:
                    tol = float(parts[1])
                    cfg["odds_tolerance"] = tol
                    save_config(cfg)
                    await event.reply(f"✅ Odds tolerance updated to {tol}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/minglobalodds":
                if len(parts) == 1:
                    cur = float(cfg.get("global_min_odds", 1.15))
                    await event.reply(
                        f"📊 Global minimum odds: {cur}\n"
                        f"(Tips below this are ignored; API price must be ≥ max(this, tipster text min). "
                        f"Send `/minglobalodds 1.20` to change, or `/minglobalodds 0` to disable.)"
                    )
                    return
                try:
                    v = float(parts[1])
                except ValueError:
                    await event.reply("⚠️ Invalid number. Example: /minglobalodds 1.15")
                    return
                if v < 0:
                    await event.reply("⚠️ Use a non-negative value, or 0 to disable the global floor.")
                    return
                cfg["global_min_odds"] = v
                save_config(cfg)
                if v == 0:
                    await event.reply("✅ Global minimum odds disabled (only tipster text mins apply).")
                else:
                    await event.reply(f"✅ Global minimum odds set to {v}")
                return

            elif cmd == "/channelodds" and len(parts) >= 3:
                # Usage (supports channel titles with spaces):
                #   /channelodds <channel...> <tolerance>
                try:
                    value = float(parts[-1])
                    raw_channel_key = " ".join(parts[1:-1]).strip()
                    if not raw_channel_key:
                        await event.reply("⚠️ Usage: /channelodds <channel> <tolerance>")
                        return

                    cfg.setdefault("channel_settings", {})
                    if not isinstance(cfg.get("channel_settings"), dict):
                        cfg["channel_settings"] = {}

                    channel_settings: Dict[str, Any] = cfg["channel_settings"]
                    canonical_key, aliases, normalized_identifier = await _resolve_channel_settings_keys(client, raw_channel_key)
                    if not canonical_key:
                        await event.reply("⚠️ Usage: /channelodds <channel> <tolerance>")
                        return

                    keys_to_set = [canonical_key, *aliases]
                    for k in keys_to_set:
                        if k not in channel_settings or not isinstance(channel_settings.get(k), dict):
                            channel_settings[k] = {}
                        channel_settings[k]["odds_tolerance"] = value

                    save_config(cfg)
                    note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                    await event.reply(f"✅ {canonical_key}{note} odds tolerance set to {value}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/channeloddslist":
                channel_settings = cfg.get("channel_settings") or {}
                if not isinstance(channel_settings, dict) or not channel_settings:
                    await event.reply("ℹ️ No channel settings configured.")
                    return

                rows: list[str] = []
                for ch_key, settings in channel_settings.items():
                    if not isinstance(settings, dict):
                        continue
                    if "odds_tolerance" in settings:
                        rows.append(f"🔹 *{ch_key}* → {settings.get('odds_tolerance')}")

                if not rows:
                    await event.reply("ℹ️ No per-channel odds tolerance overrides configured.")
                    return

                response = "🎯 *Per-Channel Odds Tolerance Overrides:*\n\n" + "\n".join(rows)
                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/channeloddsremove" and len(parts) > 1:
                raw_channel_key = " ".join(parts[1:]).strip()
                channel_settings = cfg.get("channel_settings") or {}
                if not isinstance(channel_settings, dict) or not channel_settings:
                    await event.reply("ℹ️ No channel settings configured.")
                    return

                canonical_key, aliases, normalized_identifier = await _resolve_channel_settings_keys(client, raw_channel_key)
                candidates_norm = {
                    _normalize_channel_settings_key(canonical_key),
                    *(_normalize_channel_settings_key(a) for a in aliases),
                }
                candidates_norm = {c for c in candidates_norm if c}

                touched: list[str] = []
                for existing_key in list(channel_settings.keys()):
                    if _normalize_channel_settings_key(existing_key) in candidates_norm:
                        settings = channel_settings.get(existing_key)
                        if isinstance(settings, dict) and "odds_tolerance" in settings:
                            del settings["odds_tolerance"]
                            touched.append(existing_key)
                            if not settings:
                                del channel_settings[existing_key]
                            else:
                                channel_settings[existing_key] = settings

                if touched:
                    cfg["channel_settings"] = channel_settings
                    save_config(cfg)
                    note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                    await event.reply(f"✅ Removed per-channel odds tolerance override for: {canonical_key}{note}")
                else:
                    await event.reply("⚠️ No odds tolerance override found for that channel. Use /channeloddslist to see keys.")
                return

            elif cmd == "/tipsterodds" and len(parts) >= 3:
                try:
                    tipster = parts[1]
                    value = float(parts[2])

                    if tipster not in cfg.get("tipster_settings", {}):
                        cfg["tipster_settings"][tipster] = {
                            "base_stake": cfg["base_stake"],
                            "min_stake": cfg["min_stake"],
                            "max_stake": cfg["max_stake"],
                            "min_unit": cfg["min_unit"],
                            "max_unit": cfg["max_unit"],
                        }

                    cfg["tipster_settings"][tipster]["odds_tolerance"] = value
                    save_config(cfg)
                    await event.reply(f"✅ {tipster} odds tolerance set to {value}")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/tipsteroddslist":
                tipster_settings = cfg.get("tipster_settings", {})
                if not isinstance(tipster_settings, dict) or not tipster_settings:
                    await event.reply("ℹ️ No tipster settings configured.")
                    return

                rows: list[str] = []
                for tipster, settings in tipster_settings.items():
                    if not isinstance(settings, dict):
                        continue
                    if "odds_tolerance" in settings:
                        rows.append(f"🔹 *{tipster}* → {settings.get('odds_tolerance')}")

                if not rows:
                    await event.reply("ℹ️ No tipster-specific odds tolerance overrides configured.")
                    return

                response = "🎯 *Tipster Odds Tolerance Overrides:*\n\n" + "\n".join(rows)
                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/tipsteroddsremove" and len(parts) > 1:
                tipster = parts[1]
                tipster_settings = cfg.get("tipster_settings", {})
                if not isinstance(tipster_settings, dict) or tipster not in tipster_settings:
                    await event.reply("⚠️ Tipster not found. Use /tipsterlist to see configured tipsters.")
                    return

                settings = tipster_settings.get(tipster)
                if not isinstance(settings, dict) or "odds_tolerance" not in settings:
                    await event.reply("ℹ️ No odds tolerance override set for that tipster.")
                    return

                del settings["odds_tolerance"]
                tipster_settings[tipster] = settings
                cfg["tipster_settings"] = tipster_settings
                save_config(cfg)
                await event.reply(f"✅ Removed odds tolerance override for tipster: {tipster}")
                return

            elif cmd == "/catchup" and len(parts) > 1:
                choice = parts[1].lower()
                if choice in ["on", "true", "1", "yes"]:
                    ok = _update_env_file("CATCH_UP", "true")
                    msg = "✅ Catch-up enabled on startup."
                elif choice in ["off", "false", "0", "no"]:
                    ok = _update_env_file("CATCH_UP", "false")
                    msg = "✅ Catch-up disabled on startup."
                else:
                    await event.reply("⚠️ Usage: /catchup <on|off>")
                    return
                await event.reply(msg + "\n⚠️ Restart bot to apply changes.")
                return

            elif cmd == "/catchuplimit" and len(parts) > 1:
                try:
                    limit = int(parts[1])
                    if limit < 0:
                        await event.reply("⚠️ Limit must be non-negative.")
                        return
                    ok = _update_env_file("CATCH_UP_LIMIT", str(limit))
                    await event.reply(f"✅ Catch-up limit set to {limit}.\n⚠️ Restart bot to apply changes.")
                except ValueError:
                    await event.reply("⚠️ Usage: /catchuplimit <number>")
                return

            elif cmd == "/showconfig":
                cfg_text = json.dumps(cfg, indent=2)
                await event.reply(f"📌 Current Config:\n<pre>{cfg_text}</pre>", parse_mode="html")
                return

            elif cmd == "/balance":
                try:
                    data = client_api.get_account_summary()
                    result = data.get("Result", {})
                    available = result.get("AvailableCredit", 0.0)
                    outstanding = result.get("Outstanding", 0.0)
                    today_pl = result.get("TodayPL", 0.0)
                    yesterday_pl = result.get("YesterdayPL", 0.0)
                    await event.reply(
                        f"💰 *Account Summary:*\n"
                        f"Available Credit: {available}\n"
                        f"Outstanding: {outstanding}\n"
                        f"Today P&L: {today_pl}\n"
                        f"Yesterday P&L: {yesterday_pl}",
                        parse_mode="markdown",
                    )
                except Exception as exc:
                    await event.reply(f"⚠️ Balance check failed: {exc}")
                return

            elif cmd == "/exportwagers":
                fmt_input = "%Y-%m-%d"
                fmt_api = "%Y-%m-%dT%H:%M:%SZ"
                max_span_days = 30
                allowed_lists = {"running": "RUNNING", "settled": "SETTLED", "all": "ALL", "both": "ALL"}
                output_styles = {"excel": "excel", "json": "json"}

                def _parse_date(text: str) -> Optional[datetime]:
                    try:
                        return datetime.strptime(text, fmt_input).replace(tzinfo=timezone.utc)
                    except ValueError:
                        return None

                from_dt: Optional[datetime] = None
                to_dt: Optional[datetime] = None
                betlist_choice = "ALL"
                output_style = "excel"

                args_only = parts[1:]
                # Optional output style at the end (excel|json)
                if args_only and args_only[-1].lower() in output_styles:
                    output_style = output_styles[args_only.pop(-1).lower()]
                # Optional betlist arg before that
                if args_only and args_only[-1].lower() in allowed_lists:
                    betlist_choice = allowed_lists[args_only.pop(-1).lower()]

                if not args_only:
                    to_dt = datetime.now(timezone.utc)
                    from_dt = to_dt - timedelta(days=7)
                elif len(args_only) == 1:
                    days_raw = args_only[0]
                    days = None
                    try:
                        days = int(days_raw)
                    except ValueError:
                        await event.reply("⚠️ Usage: /exportwagers [days<=30] [running|settled|all] or /exportwagers YYYY-MM-DD YYYY-MM-DD [running|settled|all]")
                        return
                    if days <= 0 or days > max_span_days:
                        await event.reply(f"⚠️ Days must be between 1 and {max_span_days}.")
                        return
                    to_dt = datetime.now(timezone.utc)
                    from_dt = to_dt - timedelta(days=days)
                elif len(args_only) == 2:
                    from_dt = _parse_date(args_only[0])
                    to_dt = _parse_date(args_only[1])
                    if not from_dt or not to_dt:
                        await event.reply("⚠️ Dates must be in YYYY-MM-DD format.")
                        return
                    if from_dt > to_dt:
                        await event.reply("⚠️ From-date must be before to-date.")
                        return
                    if (to_dt - from_dt).days > max_span_days:
                        await event.reply(f"⚠️ Date range cannot exceed {max_span_days} days.")
                        return
                    # Extend to include the entire to-date
                    to_dt = to_dt + timedelta(days=1) - timedelta(seconds=1)
                else:
                    await event.reply("⚠️ Usage: /exportwagers [days<=30] [running|settled|all] [excel|json] or /exportwagers YYYY-MM-DD YYYY-MM-DD [running|settled|all] [excel|json]")
                    return

                now_utc = datetime.now(timezone.utc)
                if to_dt > now_utc:
                    await event.reply("⚠️ To-date cannot be in the future.")
                    return
                if from_dt > to_dt:
                    await event.reply("⚠️ From-date must be before to-date.")
                    return

                status_msg = None
                temp_path = None
                try:
                    range_desc = f"{from_dt.strftime(fmt_input)} → {to_dt.strftime(fmt_input)}"
                    list_desc = betlist_choice.capitalize()
                    status_msg = await event.reply(f"⏳ Fetching {list_desc.lower()} wager history for {range_desc} ({output_style})...")
                    bets_payload = client_api.get_bets(
                        betlist=betlist_choice,
                        from_date=from_dt.strftime(fmt_api),
                        to_date=to_dt.strftime(fmt_api),
                    )
                    if output_style == "json":
                        import json as _json
                        pretty = _json.dumps(bets_payload, indent=2, ensure_ascii=False)
                        # Telegram has ~4k char limit per message; send as file if large
                        if len(pretty) > 3500:
                            with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="w", encoding="utf-8") as tmp:
                                tmp.write(pretty)
                                temp_path = tmp.name
                            await client.send_file(
                                event.chat_id,
                                temp_path,
                                caption=f"📄 Raw JSON wager data\nRange: {range_desc}\nType: {list_desc}",
                                reply_to=event.id,
                            )
                        else:
                            await event.reply(f"```json\n{pretty}\n```", parse_mode="markdown")
                        if status_msg:
                            await status_msg.edit("✅ Wager history JSON ready.")
                    else:
                        bets = _extract_bets_for_export(bets_payload)
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as tmp:
                            temp_path = tmp.name
                        export_bets_to_excel(bets, temp_path)
                        caption = (
                            "📊 *Wager History Export*\n"
                            f"Range: {range_desc}\n"
                            f"Type: {list_desc}\n"
                            f"Rows: {len(bets)}"
                        )
                        await client.send_file(
                            event.chat_id,
                            temp_path,
                            caption=caption,
                            parse_mode="markdown",
                            reply_to=event.id,
                        )
                        if status_msg:
                            await status_msg.edit("✅ Wager history ready.")
                except Exception as exc:
                    resp_detail = ""
                    resp = getattr(exc, "response", None)
                    if resp is not None:
                        try:
                            resp_detail = "\nAPI response:\n" + json.dumps(resp.json(), indent=2)
                        except Exception:
                            if resp.text:
                                resp_detail = f"\nAPI response text:\n{resp.text[:500]}"
                    if status_msg:
                        await status_msg.edit(f"⚠️ Export failed: {exc}{resp_detail}")
                    else:
                        await event.reply(f"⚠️ Export failed: {exc}{resp_detail}")
                finally:
                    if temp_path and os.path.exists(temp_path):
                        try:
                            os.remove(temp_path)
                        except Exception:
                            pass
                return

            elif cmd == "/forceoutgoing" and len(parts) > 1:
                choice = parts[1].lower()
                if choice in ["on", "true", "1", "yes"]:
                    cfg["force_outgoing"] = True
                    save_config(cfg)
                    await event.reply("✅ Force placing outgoing bet messages is now ON\n⚠️ Duplicate/running-bet checks will be bypassed for outgoing messages (balance checks remain).\n📤 Outgoing messages are never forwarded.")
                elif choice in ["off", "false", "0", "no"]:
                    cfg["force_outgoing"] = False
                    save_config(cfg)
                    await event.reply("✅ Force placing outgoing bet messages is now OFF")
                else:
                    await event.reply("⚠️ Usage: /forceoutgoing <on|off>")
                return

            elif cmd == "/retry" and len(parts) >= 3:
                try:
                    attempts = int(parts[1])
                    minutes = float(parts[2])
                    if attempts < 0 or minutes < 0:
                        await event.reply("⚠️ Attempts and minutes must be non-negative.")
                    else:
                        cfg["retry_attempts"] = attempts
                        cfg["retry_interval_minutes"] = minutes
                        save_config(cfg)
                        await event.reply(f"✅ Retry configured: attempts={attempts}, every={minutes} minute(s)")
                except ValueError:
                    await event.reply("⚠️ Usage: /retry <attempts> <minutes>")
                return

            elif cmd == "/betretry" and len(parts) >= 3:
                try:
                    attempts = int(parts[1])
                    minutes = float(parts[2])
                    if attempts < 0 or minutes < 0:
                        await event.reply("⚠️ Attempts and minutes must be non-negative.")
                    else:
                        cfg["place_result_retry_attempts"] = attempts
                        cfg["place_result_retry_interval_minutes"] = minutes
                        save_config(cfg)
                        await event.reply(f"✅ Bet retry configured: attempts={attempts}, every={minutes} minute(s)")
                except ValueError:
                    await event.reply("⚠️ Usage: /betretry <attempts> <minutes>")
                return

            elif cmd == "/forceincoming" and len(parts) > 1:
                choice = parts[1].lower()
                if choice in ["on", "true", "1", "yes"]:
                    cfg["force_incoming"] = True
                    save_config(cfg)
                    await event.reply("✅ Force placing incoming bet messages is now ON\n⚠️ Duplicate/running-bet checks will be bypassed for incoming messages (balance checks remain).")
                elif choice in ["off", "false", "0", "no"]:
                    cfg["force_incoming"] = False
                    save_config(cfg)
                    await event.reply("✅ Force placing incoming bet messages is now OFF")
                else:
                    await event.reply("⚠️ Usage: /forceincoming <on|off>")
                return

            elif cmd == "/tipsterstake" and len(parts) >= 4:
                try:
                    tipster = parts[1]
                    stake_type = parts[2].lower()
                    value = float(parts[3])
                    
                    if tipster not in cfg.get("tipster_settings", {}):
                        cfg["tipster_settings"][tipster] = {
                            "base_stake": cfg["base_stake"],
                            "min_stake": cfg["min_stake"],
                            "max_stake": cfg["max_stake"],
                            "min_unit": cfg["min_unit"],
                            "max_unit": cfg["max_unit"]
                        }
                    
                    if stake_type == "base":
                        cfg["tipster_settings"][tipster]["base_stake"] = value
                        await event.reply(f"✅ {tipster} base stake set to €{value}")
                    elif stake_type == "min":
                        cfg["tipster_settings"][tipster]["min_stake"] = value
                        await event.reply(f"✅ {tipster} minimum stake set to €{value}")
                    elif stake_type == "max":
                        cfg["tipster_settings"][tipster]["max_stake"] = value
                        await event.reply(f"✅ {tipster} maximum stake set to €{value}")
                    elif stake_type == "minunit":
                        cfg["tipster_settings"][tipster]["min_unit"] = value
                        await event.reply(f"✅ {tipster} minimum unit set to {value}")
                    elif stake_type == "maxunit":
                        cfg["tipster_settings"][tipster]["max_unit"] = value
                        await event.reply(f"✅ {tipster} maximum unit set to {value}")
                    else:
                        await event.reply("⚠️ Usage: /tipsterstake <tipster> <base|min|max|minunit|maxunit> <value>")
                        return
                    
                    save_config(cfg)
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/channelstake" and len(parts) >= 4:
                # Usage (supports channel titles with spaces):
                #   /channelstake <channel...> <base|min|max|minunit|maxunit> <value>
                try:
                    stake_type = parts[-2].lower()
                    value = float(parts[-1])
                    raw_channel_key = " ".join(parts[1:-2]).strip()
                    if not raw_channel_key:
                        await event.reply("⚠️ Usage: /channelstake <channel> <base|min|max|minunit|maxunit> <value>")
                        return

                    cfg.setdefault("channel_settings", {})
                    if not isinstance(cfg.get("channel_settings"), dict):
                        cfg["channel_settings"] = {}

                    channel_settings: Dict[str, Any] = cfg["channel_settings"]
                    canonical_key, aliases, normalized_identifier = await _resolve_channel_settings_keys(client, raw_channel_key)
                    if not canonical_key:
                        await event.reply("⚠️ Usage: /channelstake <channel> <base|min|max|minunit|maxunit> <value>")
                        return

                    # Store under canonical key, and also under alias keys so listener-forwarded
                    # messages (which often carry a channel name/@username) can still match rules
                    # configured via numeric chat IDs.
                    keys_to_set = [canonical_key, *aliases]
                    for k in keys_to_set:
                        if k not in channel_settings or not isinstance(channel_settings.get(k), dict):
                            channel_settings[k] = {}

                    if stake_type == "base":
                        for k in keys_to_set:
                            channel_settings[k]["base_stake"] = value
                        save_config(cfg)
                        note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                        await event.reply(f"✅ {canonical_key}{note} base stake set to €{value}")
                    elif stake_type == "min":
                        for k in keys_to_set:
                            channel_settings[k]["min_stake"] = value
                        save_config(cfg)
                        note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                        await event.reply(f"✅ {canonical_key}{note} minimum stake set to €{value}")
                    elif stake_type == "max":
                        for k in keys_to_set:
                            channel_settings[k]["max_stake"] = value
                        save_config(cfg)
                        note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                        await event.reply(f"✅ {canonical_key}{note} maximum stake set to €{value}")
                    elif stake_type == "minunit":
                        for k in keys_to_set:
                            channel_settings[k]["min_unit"] = value
                        save_config(cfg)
                        note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                        await event.reply(f"✅ {canonical_key}{note} minimum unit set to {value}")
                    elif stake_type == "maxunit":
                        for k in keys_to_set:
                            channel_settings[k]["max_unit"] = value
                        save_config(cfg)
                        note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                        await event.reply(f"✅ {canonical_key}{note} maximum unit set to {value}")
                    else:
                        await event.reply("⚠️ Usage: /channelstake <channel> <base|min|max|minunit|maxunit> <value>")
                except ValueError:
                    await event.reply("⚠️ Invalid number.")
                return

            elif cmd == "/channelstakelist":
                channel_settings = cfg.get("channel_settings") or {}
                if not isinstance(channel_settings, dict) or not channel_settings:
                    await event.reply("ℹ️ No channel stake overrides configured.")
                    return

                response = "🏷️ *Channel Stake Overrides:*\n\n"
                for ch_key, settings in channel_settings.items():
                    if not isinstance(settings, dict):
                        continue
                    parts_out: list[str] = []
                    if "base_stake" in settings:
                        parts_out.append(f"base=€{settings.get('base_stake')}")
                    if "min_stake" in settings:
                        parts_out.append(f"min=€{settings.get('min_stake')}")
                    if "max_stake" in settings:
                        parts_out.append(f"max=€{settings.get('max_stake')}")
                    if "min_unit" in settings:
                        parts_out.append(f"minunit={settings.get('min_unit')}")
                    if "max_unit" in settings:
                        parts_out.append(f"maxunit={settings.get('max_unit')}")
                    summary = ", ".join(parts_out) if parts_out else "(no stake fields set)"
                    response += f"🔹 *{ch_key}*\n  {summary}\n\n"

                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/channelstakeremove" and len(parts) > 1:
                raw_channel_key = " ".join(parts[1:]).strip()
                channel_settings = cfg.get("channel_settings") or {}
                if not isinstance(channel_settings, dict) or not channel_settings:
                    await event.reply("ℹ️ No channel stake overrides configured.")
                    return

                canonical_key, aliases, normalized_identifier = await _resolve_channel_settings_keys(client, raw_channel_key)
                # Remove any matching keys by normalization, so removing by id also removes the stored name/@username variant.
                candidates_norm = {
                    _normalize_channel_settings_key(canonical_key),
                    *(_normalize_channel_settings_key(a) for a in aliases),
                }
                candidates_norm = {c for c in candidates_norm if c}

                removed: list[str] = []
                for existing_key in list(channel_settings.keys()):
                    if _normalize_channel_settings_key(existing_key) in candidates_norm:
                        removed.append(existing_key)
                        del channel_settings[existing_key]

                if removed:
                    cfg["channel_settings"] = channel_settings
                    save_config(cfg)
                    note = f" (from {normalized_identifier})" if normalized_identifier and normalized_identifier != canonical_key else ""
                    if len(removed) == 1:
                        await event.reply(f"✅ Removed channel stake override for: {removed[0]}")
                    else:
                        await event.reply(f"✅ Removed channel stake overrides for: {canonical_key}{note}\nRemoved keys: {', '.join(removed)}")
                else:
                    await event.reply("⚠️ Channel key not found in channel_settings. Use /channelstakelist to see keys.")
                return

            elif cmd == "/channelforward" and len(parts) >= 3:
                # Usage (supports channel titles with spaces):
                #   /channelforward <channel...> <all|bet>
                mode_raw = parts[-1].strip().lower()
                channel_key = " ".join(parts[1:-1]).strip()
                if not channel_key:
                    await event.reply("⚠️ Usage: /channelforward <channel> <all|bet>")
                    return

                allowed = {"all", "bet", "bets", "betonly", "bet-only", "tip", "tips", "tiponly", "tip-only"}
                if mode_raw not in allowed:
                    await event.reply("⚠️ Usage: /channelforward <channel> <all|bet>")
                    return

                mode = _normalize_forward_mode(mode_raw)
                cfg.setdefault("channel_forwarding", {})
                if not isinstance(cfg.get("channel_forwarding"), dict):
                    cfg["channel_forwarding"] = {}
                cfg["channel_forwarding"][channel_key] = mode
                save_config(cfg)
                await event.reply(f"✅ {channel_key} forwarding mode set to: {mode}")
                return

            elif cmd == "/channelforwardlist":
                channel_forwarding = cfg.get("channel_forwarding") or {}
                if not isinstance(channel_forwarding, dict) or not channel_forwarding:
                    await event.reply("ℹ️ No per-channel forwarding modes configured (default is bet-only).")
                    return

                response = "📤 *Per-Channel Forwarding Modes:*\n\n"
                for ch_key, mode_val in channel_forwarding.items():
                    mode = _normalize_forward_mode(mode_val if not isinstance(mode_val, dict) else mode_val.get("mode"))
                    response += f"🔹 *{ch_key}* → `{mode}`\n"
                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/channelforwardremove" and len(parts) > 1:
                channel_key = " ".join(parts[1:]).strip()
                channel_forwarding = cfg.get("channel_forwarding") or {}
                if not isinstance(channel_forwarding, dict) or not channel_forwarding:
                    await event.reply("ℹ️ No per-channel forwarding modes configured.")
                    return

                if channel_key in channel_forwarding:
                    del channel_forwarding[channel_key]
                    cfg["channel_forwarding"] = channel_forwarding
                    save_config(cfg)
                    await event.reply(f"✅ Removed per-channel forwarding mode for: {channel_key}")
                else:
                    await event.reply("⚠️ Channel key not found in channel_forwarding. Use /channelforwardlist to see keys.")
                return

            elif cmd == "/tipsterlist":
                tipster_settings = cfg.get("tipster_settings", {})
                if not tipster_settings:
                    await event.reply("📋 No tipster-specific settings configured.")
                    return
                
                response = "📋 *Configured Tipsters:*\n\n"
                for tipster, settings in tipster_settings.items():
                    response += f"🔹 *{tipster}*\n"
                    response += f"  Base Stake: €{settings.get('base_stake', 'N/A')}\n"
                    response += f"  Min Stake: €{settings.get('min_stake', 'N/A')}\n"
                    response += f"  Max Stake: €{settings.get('max_stake', 'N/A')}\n"
                    response += f"  Min Unit: {settings.get('min_unit', 'N/A')}\n"
                    response += f"  Max Unit: {settings.get('max_unit', 'N/A')}\n\n"
                
                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/tipsterremove" and len(parts) > 1:
                tipster = parts[1]
                if tipster in cfg.get("tipster_settings", {}):
                    del cfg["tipster_settings"][tipster]
                    save_config(cfg)
                    await event.reply(f"✅ Removed tipster '{tipster}' settings")
                else:
                    await event.reply(f"⚠️ Tipster '{tipster}' not found in settings")
                return

            elif cmd == "/setforwarder":
                raw_forwarders = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
                # Allow clearing with keywords: none, blank, clear, or empty
                if raw_forwarders.lower() in ["none", "blank", "clear", ""]:
                    forwarders: list[str] = []
                    message = "✅ Forwarder channels cleared (set to blank)\n⚠️ Restart bot to apply changes."
                else:
                    parsed = [_normalize_channel_identifier(ch) for ch in re.split(r"[,\s]+", raw_forwarders) if ch.strip()]
                    forwarders = []
                    for ch in parsed:
                        if not ch:
                            continue
                        if ch not in forwarders:
                            forwarders.append(ch)
                    message = f"✅ Forwarder channels set to: {', '.join(forwarders)}\n⚠️ Restart bot to apply changes."

                forwarders_value = ",".join(forwarders)
                # Write new multi-forwarder env var; keep legacy FORWARDER_CHANNEL in sync with the first value for backwards compatibility
                updated = _update_env_file("FORWARDER_CHANNELS", forwarders_value)
                _update_env_file("FORWARDER_CHANNEL", forwarders[0] if forwarders else "")

                if updated:
                    await event.reply(message)
                else:
                    await event.reply("⚠️ Failed to update .env file. Make sure it exists and is writable.")
                return

            elif cmd == "/setlistener":
                raw_listeners = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
                # Allow clearing with keywords: none, blank, clear, or empty
                if raw_listeners.lower() in ["none", "blank", "clear", ""]:
                    listeners: list[str] = []
                    message = "✅ Listener channels cleared (set to blank)\n⚠️ Restart bot to apply changes."
                else:
                    parsed = [_normalize_channel_identifier(ch) for ch in re.split(r"[,\s]+", raw_listeners) if ch.strip()]
                    listeners = []
                    for ch in parsed:
                        if not ch:
                            continue
                        if ch not in listeners:
                            listeners.append(ch)
                    message = f"✅ Listener channels set to: {', '.join(listeners)}\n⚠️ Restart bot to apply changes."

                listeners_value = ",".join(listeners)
                # Write new multi-listener env var; keep legacy LISTENER_CHANNEL in sync with the first value for backwards compatibility
                updated = _update_env_file("LISTENER_CHANNELS", listeners_value)
                _update_env_file("LISTENER_CHANNEL", listeners[0] if listeners else "")

                if updated:
                    await event.reply(message)
                else:
                    await event.reply("⚠️ Failed to update .env file. Make sure it exists and is writable.")
                return

            elif cmd == "/setchannel":
                if len(parts) > 1:
                    main_channel = parts[1].lower()
                    # Allow clearing with keywords: none, blank, clear, or empty
                    if main_channel in ["none", "blank", "clear", ""]:
                        main_channel = ""
                        message = "✅ Main channel cleared (set to blank)\n⚠️ Restart bot to apply changes."
                    else:
                        # Use original value if not a clearing keyword
                        main_channel = parts[1]
                        message = f"✅ Main channel set to: {main_channel}\n⚠️ Restart bot to apply changes."
                else:
                    main_channel = ""
                    message = "✅ Main channel cleared (set to blank)\n⚠️ Restart bot to apply changes."
                
                if _update_env_file("TELEGRAM_CHANNEL", main_channel):
                    await event.reply(message)
                else:
                    await event.reply("⚠️ Failed to update .env file. Make sure it exists and is writable.")
                return

            elif cmd == "/showchannels":
                env_current = load_env()
                response = "📡 *Channel Configuration:*\n\n"
                response += f"👂 *Listener Channel (TELEGRAM_CHANNEL):* {env_current.telegram.channel or '(not set)'}\n"
                forwarders_display = ", ".join(env_current.telegram.forwarder_channels) if env_current.telegram.forwarder_channels else "(not set)"
                response += f"📤 *Forwarder Channels:* {forwarders_display}\n"
                listeners_display = ", ".join(env_current.telegram.listener_channels) if env_current.telegram.listener_channels else "(not set)"
                response += f"👂 *Additional Listener Channels:* {listeners_display}\n"
                response += f"📝 *Note:* TELEGRAM_CHANNEL is used for both listening to bets and sending log messages.\n"
                await event.reply(response, parse_mode="markdown")
                return

            elif cmd == "/restart":
                await event.reply("🔄 Restarting bot in 2 seconds...")
                
                async def restart_bot():
                    """Gracefully disconnect and restart the bot"""
                    await asyncio.sleep(2)
                    try:
                        await client.disconnect()
                    except Exception:
                        pass
                    
                    # Try to restart automatically
                    try:
                        # Get the Python executable and script path
                        python = sys.executable
                        script = sys.argv[0]
                        
                        # Restart the process
                        os.execv(python, [python] + sys.argv)
                    except Exception:
                        # If automatic restart fails, exit with code 2
                        # (wrapper scripts can catch this and restart)
                        os._exit(2)
                
                # Schedule restart task
                asyncio.create_task(restart_bot())
                return

        # Determine which channel this message came from.
        # Prefer @username when available; otherwise prefer Telethon's chat_id (-100...) over entity id.
        try:
            chat_username = None
            chat_entity_id = None
            if hasattr(event, "chat") and event.chat:
                chat_username = getattr(event.chat, "username", None)
                chat_entity_id = getattr(event.chat, "id", None)
            chat_peer_id = getattr(event, "chat_id", None)

            if chat_username:
                current_chat = f"@{chat_username}"
            elif chat_peer_id is not None:
                current_chat = str(chat_peer_id)
            elif chat_entity_id is not None:
                current_chat = str(chat_entity_id)
            else:
                current_chat = env.telegram.channel
        except Exception:
            current_chat = env.telegram.channel
        
        # Bet messages — skip if already bet for this message id
        try:
            if has_bet_for_message(current_chat, int(event.message.id)):
                # Still advance the last_id pointer
                set_last_id(current_chat, int(event.message.id))
                return
        except Exception:
            pass

        # Do not forward outgoing messages to forwarder channels even if set.
        # Also do not forward messages from the main channel, only from listener channels.
        #
        # We check multiple representations because Telethon can expose the same channel as:
        # - @username
        # - event.chat_id (often -100...)
        # - event.chat.id (often positive entity id)
        identifiers_to_check = [current_chat]
        try:
            if getattr(event, "chat_id", None) is not None:
                identifiers_to_check.append(str(event.chat_id))
            if hasattr(event, "chat") and event.chat and getattr(event.chat, "id", None) is not None:
                identifiers_to_check.append(str(event.chat.id))
            if hasattr(event, "chat") and event.chat and getattr(event.chat, "username", None):
                identifiers_to_check.append(f"@{event.chat.username}")
        except Exception:
            pass

        is_main = any(_is_main_channel(ident, env.telegram.channel) for ident in identifiers_to_check if ident)
        forwarders_for_this_message = [] if (is_outgoing or is_main) else env.telegram.forwarder_channels

        # Per-channel forwarding mode (by channel name/@username/-100 id stored in config):
        # - "bet": forward only when a bet is detected (existing behavior, handled inside _process_bet_text)
        # - "all": forward every message (done here, and then we suppress bet-forwarding to avoid duplicates)
        channel_display_name_for_forward = await _resolve_channel_display_name(client, current_chat)
        forward_mode = _get_channel_forward_mode(cfg, chat_identifier=current_chat, channel_display_name=channel_display_name_for_forward)
        forwarders_for_bet_processing = forwarders_for_this_message
        if client and forwarders_for_this_message and forward_mode == "all":
            await forward_bet_info(
                client,
                message_text,
                forwarders_for_this_message,
                source_channel_name=channel_display_name_for_forward,
            )
            forwarders_for_bet_processing = []

        await _process_bet_text(
            message_text,
            cfg=cfg,
            client_api=client_api,
            chat=current_chat,
            message_id=int(event.message.id),
            client=client,
            forwarder_channels=forwarders_for_bet_processing,
            is_outgoing=is_outgoing,
        )

        # Persist last processed message id
        try:
            set_last_id(current_chat, int(event.message.id))
        except Exception:
            pass

    async def catch_up_if_needed() -> None:
        enabled = os.getenv("CATCH_UP", "true").lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return
        limit = int(os.getenv("CATCH_UP_LIMIT", "5"))
        catchup_channels = [ch for ch in [env.telegram.channel, *env.telegram.listener_channels] if ch]
        if not catchup_channels:
            return

        any_processed = False
        for chat_id in catchup_channels:
            peer = _to_peer(chat_id)
            if peer is None:
                await log_message(f"⚠️ Catch-up skipped; invalid chat id {chat_id}")
                continue

            # Get channel name for logging
            channel_name = chat_id
            try:
                entity = await client.get_entity(peer)
                channel_name = getattr(entity, "title", None) or getattr(entity, "username", None) or chat_id
            except Exception:
                # If we can't get the entity, fall back to chat_id
                channel_name = chat_id

            try:
                last_id = int(get_last_id(chat_id))
            except Exception:
                last_id = 0

            processed_for_chat = False
            try:
                async for msg in client.iter_messages(
                    peer,
                    min_id=last_id,
                    reverse=False,
                    limit=limit,
                ):
                    text = (getattr(msg, "message", None) or "").strip()
                    # Skip empty, command, or our own outgoing messages
                    if getattr(msg, "out", False) or not text or text.startswith("/"):
                        try:
                            set_last_id(chat_id, int(msg.id))
                        except Exception:
                            pass
                        continue
                    # Skip if a bet has already been recorded for this message
                    try:
                        if has_bet_for_message(chat_id, int(msg.id)):
                            set_last_id(chat_id, int(msg.id))
                            continue
                    except Exception:
                        pass

                    # Do not forward messages from the main channel, only from listener channels
                    is_main = _is_main_channel(chat_id, env.telegram.channel)
                    forwarders_for_catchup = [] if is_main else env.telegram.forwarder_channels

                    channel_display_name_for_forward = await _resolve_channel_display_name(client, chat_id)
                    forward_mode = _get_channel_forward_mode(cfg, chat_identifier=chat_id, channel_display_name=channel_display_name_for_forward)
                    forwarders_for_bet_processing = forwarders_for_catchup
                    if client and forwarders_for_catchup and forward_mode == "all":
                        await forward_bet_info(
                            client,
                            text,
                            forwarders_for_catchup,
                            source_channel_name=channel_display_name_for_forward,
                        )
                        forwarders_for_bet_processing = []
                    
                    await _process_bet_text(
                        text,
                        cfg=cfg,
                        client_api=client_api,
                        chat=chat_id,
                        message_id=int(msg.id),
                        client=client,
                        forwarder_channels=forwarders_for_bet_processing,
                        is_outgoing=False,
                    )
                    try:
                        set_last_id(chat_id, int(msg.id))
                    except Exception:
                        pass
                    processed_for_chat = True
                    any_processed = True
            except Exception as exc:
                await log_message(f"⚠️ Catch-up skipped; could not read channel {channel_name}: {exc}")
                continue

            if processed_for_chat:
                await log_message(f"✅ Catch-up completed for missed messages in {channel_name}")

        if any_processed:
            await log_message("✅ Catch-up completed for missed messages")

    print("AsianOdds bot started, waiting for messages...")
    client.start()

    async def send_startup_help():
        # Hydrate per-channel alias keys so channel_settings/channel_forwarding work for forwarded messages too
        # (forwarded tips often include the source channel title as a header line).
        try:
            idents: list[str] = []
            for ch in [env.telegram.channel, *env.telegram.listener_channels]:
                if ch:
                    idents.append(str(ch))
            # Also include any configured rule keys that are not plain titles (e.g. "-100..." ids, "@user")
            for dict_key in ("channel_settings", "channel_forwarding"):
                d = cfg.get(dict_key) or {}
                if isinstance(d, dict):
                    for k in d.keys():
                        kk = str(k or "").strip()
                        if not kk:
                            continue
                        if _looks_like_channel_title(kk):
                            continue
                        idents.append(kk)
            # De-dupe while preserving order
            seen: set[str] = set()
            idents = [x for x in idents if x and not (x in seen or seen.add(x))]
            await _hydrate_channel_rules_aliases(cfg, client=client, channel_identifiers=idents)
        except Exception:
            pass

        await catch_up_if_needed()
        await log_message("🤖 Bot started and ready!")
        peer, err = _require_peer("TELEGRAM_CHANNEL", env.telegram.channel)
        if peer is not None:
            try:
                await client.send_message(peer, get_help_text(), parse_mode="markdown")
            except Exception as exc:
                await log_message(f"⚠️ Failed to send startup help to main channel: {exc}")
        else:
            await log_message(err or "⚠️ TELEGRAM_CHANNEL invalid; cannot send startup help.")

    client.loop.run_until_complete(send_startup_help())
    client.run_until_disconnected()


def get_help_text() -> str:
    return (
        "🤖 *Help / Commands*\n\n"
        "🧰 *General Management:*\n"
        "❓ `/help` — Display this help message again\n"
        "💰 `/balance` — Show current AsianOdds account balance\n"
        "📋 `/showconfig` — Display current configuration values\n"
        "📊 `/exportwagers [days|YYYY-MM-DD YYYY-MM-DD] [running|settled|all] [excel|json]` — Send a wager history export (default 7 days; max span 30 days)\n\n"
        "💰 *Betting Settings:*\n"
        "💶 `/stake <value>` — Set your base stake (minimum €5)\n"
        "📉 `/minstake <value>` — Set minimum allowed stake\n"
        "📈 `/maxstake <value>` — Set maximum allowed stake\n"
        "🔹 `/minunit <value>` — Set minimum unit size (e.g. 0.5)\n"
        "🔸 `/maxunit <value>` — Set maximum unit size (e.g. 5)\n"
        "⚽ `/sports <tennis|soccer|football|basketball|rugby|rugbyunion|both|all>` — Enable betting for specific sports\n"
        "🏟️ `/leagues <tennis|soccer|football|basketball|rugby|rugbyunion> [filter]` — List AsianOdds leagues for a sport (optional name filter)\n"
        "🎲 `/bettype <prematch|live|both>` — Set global bet type preference (pre-match vs live)\n"
        "🎲 `/bettype <tennis|soccer|football|basketball|rugby|rugbyunion> <prematch|live|both|clear>` — Set per-sport bet type preference\n"
        "🎲 `/bettype list` — Show global + per-sport bet type preferences\n"
        "🎯 `/odds <tolerance>` — Set allowed odds difference (e.g. `/odds 0.05`)\n"
        "📊 `/minglobalodds [value]` — Show or set global minimum odds (default 1.15); `/minglobalodds 0` disables\n\n"
        "📡 *Channel Management:*\n"
        "📢 `/setchannel [channel|none|blank|clear]` — Set main channel (TELEGRAM_CHANNEL - for listening and logging). Use without argument or 'none'/'blank'/'clear' to clear.\n"
        "📤 `/setforwarder [channel(s)|none|blank|clear]` — Set forwarder channel(s) (comma/space separated). Supports @username, -100 chat IDs, or t.me links. Use without argument or 'none'/'blank'/'clear' to clear.\n"
        "👂 `/setlistener [channel(s)|none|blank|clear]` — Set additional listener channel(s) (comma/space separated; TELEGRAM_CHANNEL is always listened). Supports @username, -100 chat IDs, or t.me links. Use without argument or 'none'/'blank'/'clear' to clear.\n"
        "📡 `/showchannels` — Show current channel configuration\n\n"
        "🏷️ *Channel Overrides & Forwarding:*\n"
        "💰 `/channelstake <channel> <base|min|max|minunit|maxunit> <value>` — Set per-channel stake settings (channel can be @username, -100 id, or channel title)\n"
        "📋 `/channelstakelist` — List all configured channel stake overrides\n"
        "🗑️ `/channelstakeremove <channel>` — Remove a channel stake override\n"
        "🎯 `/channelodds <channel> <tolerance>` — Set per-channel odds tolerance override\n"
        "📋 `/channeloddslist` — List per-channel odds tolerance overrides\n"
        "🗑️ `/channeloddsremove <channel>` — Remove a per-channel odds tolerance override\n"
        "📤 `/channelforward <channel> <all|bet>` — Forward all messages or only bet/tip messages for that source channel\n"
        "📋 `/channelforwardlist` — List all configured per-channel forwarding modes\n"
        "🗑️ `/channelforwardremove <channel>` — Remove a per-channel forwarding mode (reverts to bet-only)\n\n"
        "🎯 *Tipster Management:*\n"
        "💰 `/tipsterstake <tipster> <base|min|max|minunit|maxunit> <value>` — Set tipster-specific settings\n"
        "🎯 `/tipsterodds <tipster> <tolerance>` — Set tipster-specific odds tolerance override\n"
        "📋 `/tipsteroddslist` — List tipster-specific odds tolerance overrides\n"
        "🗑️ `/tipsteroddsremove <tipster>` — Remove a tipster odds tolerance override\n"
        "📋 `/tipsterlist` — List all configured tipsters\n"
        "🗑️ `/tipsterremove <tipster>` — Remove tipster settings\n\n"
        "🛠️ *System Management:*\n"
        "✉️ `/forceoutgoing <on|off>` — Force place outgoing bet messages (never forwarded)\n"
        "🚨 `/forceincoming <on|off>` — Force place incoming bet messages (bypasses duplicate-running checks; balance still enforced)\n"
        "⏪ `/catchup <on|off>` — Enable/disable catch-up on startup\n"
        "🔢 `/catchuplimit <n>` — Set catch-up message scan limit\n"
        "🔁 `/retry <attempts> <minutes>` — Event retry (resolving event/line)\n"
        "🔄 `/betretry <attempts> <minutes>` — Bet retry (bet placement failures)\n"
        "🔄 `/restart` — Restart the bot\n\n"
        "Tip: You can adjust these settings anytime to fine-tune your betting strategy."
    )
