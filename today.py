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


def get(path, **params):
    key = os.getenv('ODDS_API_KEY')
    if not key:
        raise FeedError('The live feed is waiting for an Odds API key.')
    try:
        response = requests.get(BASE + path, params={'apiKey': key, **params}, timeout=5)
        response.raise_for_status()
        return response.json()
    except requests.HTTPError as exc:
        if exc.response.status_code == 401:
            raise FeedError('The Odds API returned 401: the key configured on the server was not accepted. Check the Render ODDS_API_KEY value.') from None
        if exc.response.status_code == 403:
            raise FeedError('The Odds API returned 403: this account is not authorized for the requested endpoint, bookmaker, or player-props market.') from None
        if exc.response.status_code == 429:
            raise FeedError('The live feed quota has been reached.') from None
        raise FeedError('The live provider is temporarily unavailable.') from None
    except (requests.RequestException, ValueError):
        raise FeedError('The live provider is temporarily unavailable.') from None


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
        return get(f'/{key}/events/{event_id}/odds', bookmakers=bookmaker,
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
    with LOCK:
        payload = event_offers(sport,event_id,bucket(5),'pinnacle',expanded)
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

