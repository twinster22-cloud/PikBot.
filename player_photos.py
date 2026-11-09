"""Resolve player portraits by exact player identity; photos never gate picks."""
import csv
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from functools import lru_cache
from io import StringIO
import os
from threading import Lock
import unicodedata
from urllib.parse import urlparse

import player_history as history

NFL_LOCK = Lock()
PHOTO_LOCK = Lock()
PHOTO_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix='pikbot-photos')
PHOTO_BATCH = {}


def normalize(value):
    return ' '.join(unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode().casefold().split())


def espn_photo(league, player_id):
    value = str(player_id)
    return f'https://a.espncdn.com/i/headshots/{league}/players/full/{value}.png' if value.isdigit() else None


def trusted_image(value):
    if not isinstance(value, str):
        return None
    url = urlparse(value)
    return value if url.scheme == 'https' and url.hostname in {
        'a.espncdn.com', 'assets.nhle.com', 'static.www.nfl.com',
    } else None


@lru_cache(maxsize=2)
def nfl_players(_week):
    response = history.download(history.NFLVERSE + '/players/players.csv')
    return list(csv.DictReader(StringIO(response.text)))


@lru_cache(maxsize=1024)
def player_photo(sport, player, stat, week):
    try:
        if sport == 'NFL':
            with NFL_LOCK:
                rows = [p for p in nfl_players(week) if normalize(p.get('display_name')) == normalize(player)]
            # A quarterback and a defensive back can share the same name.
            if stat == 'PASS_YDS':
                rows = [p for p in rows if p.get('position') == 'QB']
            if len(rows) == 1:
                return trusted_image(rows[0].get('headshot')) or espn_photo('nfl', rows[0].get('espn_id'))
        elif sport in {'NBA', 'WNBA', 'COLLEGE BASKETBALL'}:
            if os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY'):
                league = {'NBA': 'nba', 'WNBA': 'wnba', 'COLLEGE BASKETBALL': 'ncaam'}[sport]
                player_id = history.dunk_player_id(league, player)
                image_league = 'mens-college-basketball' if league == 'ncaam' else league
                return espn_photo(image_league, player_id)
        elif sport in {'NHL', 'MLB'}:
            env = history.ADDITIONAL_FEEDS[sport][2]
            if os.getenv(env):
                player_id = history.additional_player_id(sport, player)
                if sport == 'NHL' and str(player_id).isdigit():
                    return f'https://assets.nhle.com/mugs/nhl/latest/{player_id}.png'
                if sport == 'MLB':
                    return espn_photo('mlb', player_id)
        elif sport == 'COLLEGE FOOTBALL' and os.getenv('COLLEGE_DATA_API_KEY'):
            response = history.download('https://api.collegefootballdata.com/player/search',
                params={'searchTerm': player, 'year': datetime.now(timezone.utc).year},
                headers={'Authorization': 'Bearer ' + os.environ['COLLEGE_DATA_API_KEY']})
            rows = response.json()
            ids = {str(p['id']) for p in rows if isinstance(p, dict) and p.get('id')
                   and normalize(p.get('name')) == normalize(player)} if isinstance(rows, list) else set()
            if len(ids) == 1:
                return espn_photo('college-football', next(iter(ids)))
    except (history.FeedError, OSError, ValueError, TypeError, KeyError):
        return None
    return None


def attach_photos(picks):
    week = int(datetime.now(timezone.utc).timestamp() // (7 * 86400))
    identities = list(dict.fromkeys((p['sport'], p['player'], p['stat']) for p in picks))[:24]
    with PHOTO_LOCK:
        # Keep a single bounded batch in flight. Slow photo lookups continue warming the cache.
        if all(future.done() for future in PHOTO_BATCH.values()):
            PHOTO_BATCH.clear()
            PHOTO_BATCH.update({key: PHOTO_POOL.submit(player_photo, *key, week) for key in identities})
        futures = {key: PHOTO_BATCH[key] for key in identities if key in PHOTO_BATCH}
    completed, _ = wait(futures.values(), timeout=1)
    urls = {key: future.result() for key, future in futures.items()
            if future in completed and future.exception() is None}
    for pick in picks:
        pick['photo_url'] = urls.get((pick['sport'], pick['player'], pick['stat']))
