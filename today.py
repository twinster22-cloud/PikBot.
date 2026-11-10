"""Upcoming player prop offers from The Odds API."""
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from zoneinfo import ZoneInfo

import requests

BASE = 'https://api.the-odds-api.com/v4/sports'
DEFAULT_BOOK = 'prizepicks'
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
    key, markets = SPORTS[sport]
    if expanded and sport == 'NBA':
        markets = {**markets, **NBA_EXTRA}
    try:
        return get(f'/{key}/events/{event_id}/odds', timeout=2 if bookmaker == 'pinnacle' else None,
                   bookmakers=bookmaker,
                   markets=','.join(markets), oddsFormat='decimal')
    except FeedError as exc:
        return {'error': str(exc)}


def offers(sport, now, event_id=None, *, next_slate=False, bookmaker=DEFAULT_BOOK, expanded=False):
    _, markets = SPORTS[sport]
    expanded = expanded and sport == 'NBA'
    if expanded:
        markets = {**markets, **NBA_EXTRA}
    result = {}
    offered_sides = {}
    slate = events(sport, bucket(30)) if next_slate else today_events(sport, now)
    upcoming = sorted((event for event in slate
                       if (start := timestamp(event.get('commence_time'))) and start > now),
                      key=lambda event: timestamp(event['commence_time']))
    if event_id:
        upcoming = [event for event in upcoming if event['id'] == event_id]
    for event in upcoming[:1]:
        event_id = event['id']
        with LOCK:
            window = bucket(5)
            if expanded:
                payload = event_offers(sport, event_id, window, bookmaker, True)
            elif bookmaker == DEFAULT_BOOK:
                payload = event_offers(sport, event_id, window)
            else:
                payload = event_offers(sport, event_id, window, bookmaker)
        if payload.get('error'):
            raise FeedError(payload['error'])
        for book in payload.get('bookmakers', []):
            if book.get('key') != bookmaker:
                continue
            for market in book.get('markets', []):
                stat = markets.get(market.get('key'))
                updated = timestamp(market.get('last_update') or book.get('last_update'))
                if not stat or not updated or not (now-timedelta(minutes=60) <= updated <= now+timedelta(minutes=2)):
                    continue
                for outcome in market.get('outcomes', []):
                    player = outcome.get('description')
                    line = outcome.get('point')
                    if not player or not isinstance(line, (float, int)) or not 0 <= line < float('inf'):
                        continue
                    offer_id = f'{event_id}:{player}:{stat}:{line}'
                    offered_sides.setdefault(offer_id, set()).add(str(outcome.get('name', '')).casefold())
                    prices = result.get(offer_id, {}).get('prices', {})
                    side = str(outcome.get('name', '')).casefold()
                    if side in {'over','more','under','less'}:
                        prices['MORE' if side in {'over','more'} else 'LESS'] = outcome.get('price')
                    result[offer_id] = {'sport': sport, 'player': player, 'stat': stat,
                                        'line': line, 'as_of': now, 'offer_id': offer_id,
                                        'event_id': event_id, 'starts_at': event['commence_time'],
                                        'line_updated_at': updated.isoformat(), 'prices':prices}
    if bookmaker == DEFAULT_BOOK:
        return list(result.values())
    return [offer for offer_id, offer in result.items()
            if offered_sides[offer_id] & {'over', 'more'}
            and offered_sides[offer_id] & {'under', 'less'}]


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

