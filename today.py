"""Upcoming PrizePicks offers from The Odds API."""
import os
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from threading import Lock
from zoneinfo import ZoneInfo

import requests

BASE = 'https://api.the-odds-api.com/v4/sports'
BASKETBALL = {'player_points': 'PTS', 'player_rebounds': 'REB', 'player_assists': 'AST'}
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
        if exc.response.status_code in {401, 403}:
            raise FeedError('The Odds API rejected this key or feed entitlement.') from None
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
def event_offers(sport, event_id, _window):
    key, markets = SPORTS[sport]
    try:
        return get(f'/{key}/events/{event_id}/odds', bookmakers='prizepicks',
                   markets=','.join(markets))
    except FeedError as exc:
        return {'error': str(exc)}


def offers(sport, now, event_id=None):
    _, markets = SPORTS[sport]
    result = {}
    upcoming = [event for event in today_events(sport, now)
                if timestamp(event.get('commence_time')) > now]
    if event_id:
        upcoming = [event for event in upcoming if event['id'] == event_id]
    for event in upcoming[:1]:
        event_id = event['id']
        with LOCK:
            payload = event_offers(sport, event_id, bucket(60))
        if payload.get('error'):
            raise FeedError(payload['error'])
        for book in payload.get('bookmakers', []):
            if book.get('key') != 'prizepicks':
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
                    result[offer_id] = {'sport': sport, 'player': player, 'stat': stat,
                                        'line': line, 'as_of': now, 'offer_id': offer_id,
                                        'event_id': event_id}
    return list(result.values())
