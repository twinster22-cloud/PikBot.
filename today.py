"""Upcoming player prop offers from The Odds API."""
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from zoneinfo import ZoneInfo

import requests

BASE = 'https://api.the-odds-api.com/v4/sports'
DEFAULT_BOOK = 'prizepicks'
PRIZEPICKS_MAX_LINE_AGE = timedelta(minutes=5)
BOOKMAKERS = {
    DEFAULT_BOOK: 'PrizePicks',
    'underdog': 'Underdog Fantasy',
    'pick6': 'DraftKings Pick6',
    'draftkings': 'DraftKings Sportsbook',
    'fanduel': 'FanDuel',
    'betmgm': 'BetMGM',
}
BASKETBALL = {'player_points': 'PTS', 'player_rebounds': 'REB', 'player_assists': 'AST'}
NBA_EXTRA = {
    'player_points_rebounds_assists': 'PRA', 'player_points_rebounds': 'PR',
    'player_points_assists': 'PA', 'player_rebounds_assists': 'RA',
    'player_threes': '3PM', 'player_blocks': 'BLK', 'player_steals': 'STL',
}
SPORTS = {
    'NBA': ('basketball_nba', BASKETBALL),
    'WNBA': ('basketball_wnba', BASKETBALL),
    'COLLEGE BASKETBALL': ('basketball_ncaab', BASKETBALL),
    'NFL': ('americanfootball_nfl', {'player_pass_yds': 'PASS_YDS', 'player_rush_yds': 'RUSH_YDS', 'player_reception_yds': 'REC_YDS'}),
    'COLLEGE FOOTBALL': ('americanfootball_ncaaf', {'player_pass_yds': 'PASS_YDS', 'player_rush_yds': 'RUSH_YDS'}),
    'MLB': ('baseball_mlb', {'batter_hits': 'H', 'pitcher_strikeouts': 'SO'}),
    'NHL': ('icehockey_nhl', {'player_shots_on_goal': 'SOG', 'player_points': 'PTS'}),
}
LOCK = Lock()


class FeedError(Exception):
    pass


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def get(path, *, timeout=None, **params):
    """Use one coherent event-ID namespace per request cycle.

    PropLine is primary when configured, avoiding cross-provider event ID
    mismatches. The Odds API is used only when PropLine isn't configured.
    """
    def clean(value):
        return (value or '').strip().strip('"').strip("'")

    propline = clean(os.getenv('PROPLINE_API_KEY'))
    if propline and propline.lower() not in {'your_api_key', 'replace_me', 'none', 'null'}:
        try:
            response = requests.get(
                'https://api.prop-line.com/v1/sports' + path,
                headers={'X-API-Key': propline},
                params={k: v for k, v in params.items() if k != 'oddsFormat'},
                timeout=8 if timeout is None else timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            code = exc.response.status_code
            if code == 401:
                raise FeedError('PropLine authentication failed. Check PROPLINE_API_KEY in Render.') from None
            if code == 403:
                raise FeedError('PropLine free tier does not allow this requested market or endpoint.') from None
            if code == 429:
                raise FeedError('PropLine request limit reached. Try again after the quota resets.') from None
            raise FeedError('PropLine could not retrieve verified player-prop data.') from None
        except (requests.RequestException, ValueError):
            raise FeedError('PropLine is temporarily unavailable.') from None

    keys = [clean(os.getenv('ODDS_API_KEY')), clean(os.getenv('ODDS_API_BACKUP_KEY'))]
    keys = [key for key in dict.fromkeys(keys) if key and key.lower() not in
            {'your_api_key', 'your-odds-api-key', 'replace_me', 'none', 'null'}]
    if not keys:
        raise FeedError('No active odds feed configured. Set PROPLINE_API_KEY in Render.')
    last_status = None
    for key in keys:
        try:
            response = requests.get(BASE + path, params={'apiKey': key, **params},
                                    timeout=5 if timeout is None else timeout)
            response.raise_for_status()
            return response.json()
        except requests.HTTPError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                last_status = status
                continue
            if status == 429:
                raise FeedError('Odds API request quota reached.') from None
            raise FeedError('The live odds provider is temporarily unavailable.') from None
        except (requests.RequestException, ValueError):
            raise FeedError('The live odds provider is temporarily unavailable.') from None
    raise FeedError('Odds API credentials rejected (' + str(last_status or 401) +
                    '). Set PROPLINE_API_KEY in Render to use the independent provider.')

def bucket(minutes):
    return int(datetime.now(timezone.utc).timestamp()) // (minutes * 60)


@lru_cache(maxsize=4)
def active_sports(_window):
    active = {sport['key'] for sport in get('') if sport.get('active')}
    return [name for name, (key, _) in SPORTS.items() if key in active]


@lru_cache(maxsize=40)
def events(sport, _window):
    key, _ = SPORTS[sport]
    return get(f'/{key}/events')


def today_events(sport, now):
    eastern = ZoneInfo('America/New_York')
    date = now.astimezone(eastern).date()
    return [event for event in events(sport, bucket(30))
            if (start := timestamp(event.get('commence_time')))
            and start.astimezone(eastern).date() == date]


@lru_cache(maxsize=100)
def event_offers(sport, event_id, _window, bookmaker=DEFAULT_BOOK, expanded=False):
    key, base_markets = SPORTS[sport]
    markets = ({**base_markets, **NBA_EXTRA}
               if expanded and sport in {'NBA', 'COLLEGE BASKETBALL', 'WNBA'}
               else base_markets)
    params = dict(bookmakers=bookmaker, markets=','.join(markets), oddsFormat='decimal')
    try:
        payload = get(f'/{key}/events/{event_id}/odds', **params)
        if expanded and not payload.get('bookmakers') and markets != base_markets:
            return get(f'/{key}/events/{event_id}/odds', bookmakers=bookmaker,
                       markets=','.join(base_markets), oddsFormat='decimal')
        return payload
    except FeedError as exc:
        if expanded and markets != base_markets:
            try:
                return get(f'/{key}/events/{event_id}/odds', bookmakers=bookmaker,
                           markets=','.join(base_markets), oddsFormat='decimal')
            except FeedError:
                pass
        return {'error': str(exc)}


def offers(sport, now, event_id=None, *, next_slate=False, bookmaker=DEFAULT_BOOK, expanded=False):
    _, markets = SPORTS[sport]
    expanded = expanded and sport in {'NBA', 'COLLEGE BASKETBALL', 'WNBA'}
    if expanded:
        markets = {**markets, **NBA_EXTRA}
    result = {}
    offered_sides = {}
    latest_snapshot = {}
    prizepicks = bookmaker == DEFAULT_BOOK
    max_age = PRIZEPICKS_MAX_LINE_AGE if prizepicks else timedelta(minutes=60)
    slate = events(sport, bucket(30)) if next_slate else today_events(sport, now)
    upcoming = sorted((event for event in slate
                       if (start := timestamp(event.get('commence_time'))) and start > now),
                      key=lambda event: timestamp(event['commence_time']))
    if event_id:
        upcoming = [event for event in upcoming if event['id'] == event_id]
    # Inspect every upcoming matchup, not just the first game in the slate.\n    # NFL Sundays can have a dozen concurrent matchups with different prop boards.\n    for event in upcoming[:16]:
        event_id = event['id']
        with LOCK:
            window = bucket(1 if prizepicks else 5)
            if expanded:
                payload = event_offers(sport, event_id, window, bookmaker, True)
            elif bookmaker == DEFAULT_BOOK:
                payload = event_offers(sport, event_id, window)
            else:
                payload = event_offers(sport, event_id, window, bookmaker)
        if payload.get('error'):
            raise FeedError(payload['error'])
        if prizepicks and payload.get('id') is not None and str(payload['id']) != str(event_id):
            raise FeedError('The PrizePicks feed returned a different matchup. Please refresh.')
        if prizepicks and (start := timestamp(payload.get('commence_time'))) and start <= now:
            continue
        for book in payload.get('bookmakers', []):
            if book.get('key') != bookmaker:
                continue
            for market in book.get('markets', []):
                stat = markets.get(market.get('key'))
                updated = timestamp(market.get('last_update') or book.get('last_update'))
                if not stat or not updated or not (now-max_age <= updated <= now+timedelta(minutes=2)):
                    continue
                if prizepicks and market.get('line_type') not in (None, 'main'):
                    continue
                for outcome in market.get('outcomes', []):
                    player = outcome.get('description')
                    line = outcome.get('point')
                    if not player or not isinstance(line, (float, int)) or not 0 <= line < float('inf'):
                        continue
                    side = str(outcome.get('name', '')).casefold()
                    if prizepicks:
                        if (outcome.get('dfs_odds_type') not in (None, 'standard')
                                or outcome.get('line_type') not in (None, 'main')
                                or side not in {'over', 'more', 'under', 'less'}):
                            continue
                        player_stat = (player.casefold(), stat)
                        latest_snapshot[player_stat] = max(updated, latest_snapshot.get(player_stat, updated))
                        if market.get('suspended_at'):
                            continue
                        if outcome.get('last_seen_at') is not None:
                            last_seen = timestamp(outcome['last_seen_at'])
                            if not last_seen or not updated <= last_seen <= now + timedelta(minutes=2):
                                continue
                    offer_id = f'{event_id}:{player}:{stat}:{line}'
                    # Never pair opposite sides from different PrizePicks snapshots.
                    key = (offer_id, updated) if prizepicks else offer_id
                    offered_sides.setdefault(key, set()).add(side)
                    prices = result.get(key, {}).get('prices', {})
                    if side in {'over','more','under','less'}:
                        prices['MORE' if side in {'over','more'} else 'LESS'] = outcome.get('price')
                    result[key] = {'sport': sport, 'player': player, 'stat': stat,
                                        'line': line, 'as_of': now, 'offer_id': offer_id,
                                        'event_id': event_id, 'starts_at': event['commence_time'],
                                        'line_updated_at': updated.isoformat(), 'prices':prices}
    if prizepicks:
        latest = {}
        for key, offer in result.items():
            player_stat = (offer['player'].casefold(), offer['stat'])
            if key[1] == latest_snapshot[player_stat]:
                latest.setdefault(player_stat, []).append(key)
        # Ambiguous or partially withdrawn lines cannot be matched to the board.
        result = {keys[0]: result[keys[0]] for keys in latest.values() if len(keys) == 1}
    return [offer for key, offer in result.items()
            if offered_sides[key] & {'over', 'more'}
            and offered_sides[key] & {'under', 'less'}]


def benchmark_lines(sport, event_id, now, expanded=False):
    """Optional exact-line reference from existing API entitlement; absence is explicit."""
    _, markets = SPORTS[sport]
    if expanded and sport == 'NBA':
        markets = {**markets, **NBA_EXTRA}
    if not LOCK.acquire(blocking=False):
        return {}
    try:
        payload = event_offers(sport,event_id,bucket(5),'pinnacle',expanded)
    finally:
        LOCK.release()
    if payload.get('error') or payload.get('id') != event_id:
        return {}
    result = {}
    for book in payload.get('bookmakers',[]):
        if book.get('key') != 'pinnacle':continue
        for market in book.get('markets',[]):
            stat = markets.get(market.get('key'))
            updated = market.get('last_update') or book.get('last_update')
            if not stat:continue
            for outcome in market.get('outcomes',[]):
                side = str(outcome.get('name','')).casefold()
                if side not in {'over','under'}:continue
                key = (outcome.get('description'),stat,outcome.get('point'))
                row = result.setdefault(key,{'last_update':updated})
                label = 'MORE' if side == 'over' else 'LESS'
                if label in row:  # Ambiguous duplicate market: never choose arbitrarily.
                    row['ambiguous'] = True
                row[label] = outcome.get('price')
    return {key:row for key,row in result.items() if not row.get('ambiguous')}

