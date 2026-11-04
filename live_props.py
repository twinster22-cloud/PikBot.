"""Conservative SportsDataIO NBA player-prop adapter.

Only verified, fresh provider records become offers. Never substitute archived lines.
Requires SPORTS DATA key, SPORTSBOOK group, and a list of current game IDs.
"""
import os
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://api.sportsdata.io/v3/nba/odds/json"
STAT_NAMES = {
    "points": "Points", "playerpoints": "Points", "rebounds": "Rebounds", "playerrebounds": "Rebounds", "assists": "Assists", "playerassists": "Assists",
    "threepointersmade": "Three Pointers Made", "steals": "Steals",
    "blockedshots": "Blocks", "blocks": "Blocks",
}

class LivePropsError(Exception):
    pass

def _get_key():
    return os.getenv("SPORTSDATAIO_API_KEY") or os.getenv("SPORTSDATA_API_KEY")

def _timestamp(value):
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None

def normalize_prop(record, game_id, now=None):
    """Fail closed on ambiguous schemas, missing freshness, or archived markets."""
    if not isinstance(record, dict):
        return None
    now = now or datetime.now(timezone.utc)
    player = record.get("PlayerName")
    stat_raw = record.get("BetType") or record.get("StatType")
    line = record.get("Value") if record.get("Value") is not None else record.get("Line")
    updated = _timestamp(record.get("Updated") or record.get("UpdatedAt") or record.get("LastUpdated"))
    if not isinstance(player, str) or not player.strip() or not isinstance(stat_raw, str):
        return None
    stat = STAT_NAMES.get("".join(c for c in stat_raw.lower() if c.isalnum()))
    if not stat or not updated or not (now-timedelta(minutes=30) <= updated <= now+timedelta(minutes=2)):
        return None
    try:
        line = float(line)
    except (ValueError, TypeError):
        return None
    if not (0 <= line <= 200) or line != line:
        return None
    if record.get("IsActive") is False or record.get("IsArchived") is True:
        return None
    return {"sport":"NBA", "player":player.strip(), "stat":stat, "line":line,
            "as_of":updated.isoformat(), "event_id":str(game_id),
            "offer_id":str(record.get("BettingMarketID") or record.get("BettingOutcomeID") or f"{game_id}:{player}:{stat}:{line}"),
            "source":"SportsDataIO", "updated_at":updated.isoformat()}

def fetch_nba_props(game_ids=None, session=None):
    key = _get_key()
    group = os.getenv("SPORTSDATAIO_SPORTSBOOK_GROUP", "").strip()
    ids = game_ids if game_ids is not None else os.getenv("SPORTSDATAIO_NBA_GAME_IDS", "").split(",")
    ids = [str(g).strip() for g in ids if str(g).strip()]
    if not key:
        raise LivePropsError("SportsDataIO API key is not configured")
    if not group:
        raise LivePropsError("SportsDataIO sportsbook group is not configured or authorized")
    if not ids:
        raise LivePropsError("Current NBA game IDs are not configured")
    if len(ids) > 15 or any(not g.isdigit() for g in ids) or not group.isdigit():
        raise LivePropsError("Invalid game IDs or sportsbook group")
    get = (session or requests.Session()).get
    results = []
    for game_id in ids:
        try:
            response = get(f"{BASE}/BettingPlayerPropsByGameID/{game_id}/{group}",
                           headers={"Ocp-Apim-Subscription-Key":key}, timeout=15)
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise LivePropsError(f"Provider request failed (HTTP {status or 'network error'}); check entitlement and sportsbook group") from None
        except ValueError:
            raise LivePropsError("Provider returned invalid JSON") from None
        if not isinstance(payload, list):
            raise LivePropsError("Unrecognized player-props response; no lines accepted")
        for record in payload:
            item = normalize_prop(record, game_id)
            if item:
                results.append(item)
    # Do not claim success for an unrecognized schema or stale data.
    if not results:
        raise LivePropsError("No fresh, recognized NBA player props returned; verify provider fields and access")
    return results
