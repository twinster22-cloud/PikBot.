"""Refresh recent NBA and NFL history for the live projection endpoint."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd
import requests

from import_live_history import COLUMNS, nfl
from today import FeedError, bucket

BDL = 'https://api.balldontlie.io/v1'
NFLVERSE = 'https://github.com/nflverse/nflverse-data/releases/download'


def download(url, **kwargs):
    try:
        response = requests.get(url, timeout=5, **kwargs)
        response.raise_for_status()
        return response
    except requests.RequestException as exc:
        status = exc.response.status_code if exc.response is not None else None
        if status in {401, 403}:
            raise FeedError('The player stats provider denied access. Check the key and plan.') from None
        if status == 429:
            raise FeedError('The player stats provider quota has been reached.') from None
        raise FeedError('The player stats provider is temporarily unavailable.') from None


def nba_get(path, **params):
    key = os.getenv('BALLDONTLIE_API_KEY')
    if not key:
        raise FeedError('NBA history is waiting for a BALLDONTLIE API key with game stats access.')
    try:
        return download(BDL + path, params=params, headers={'Authorization': key}).json()
    except ValueError:
        raise FeedError('The NBA history provider returned an invalid response.') from None


@lru_cache(maxsize=512)
def nba_player(player, day, _window):
    def normalize(name):
        return ' '.join(name.casefold().split())

    search = nba_get('/players', search=player, per_page=100)
    ids = {row['id'] for row in search.get('data', [])
           if normalize(row['first_name'] + ' ' + row['last_name']) == normalize(player)}
    if len(ids) != 1:
        return []
    params = {'player_ids[]': next(iter(ids)), 'start_date': (day - timedelta(days=365)).isoformat(),
              'end_date': (day - timedelta(days=1)).isoformat(), 'per_page': 100}
    rows = []
    for _ in range(2):
        page = nba_get('/stats', **params)
        for row in page.get('data', []):
            game = row.get('game') or {}
            minutes = str(row.get('min') or '0').split(':')
            if str(game.get('status', '')).lower() != 'final' or not any(part.isdigit() and int(part) for part in minutes):
                continue
            for field, stat in (('pts', 'PTS'), ('reb', 'REB'), ('ast', 'AST')):
                value = row.get(field)
                if isinstance(value, (int, float)) and 0 <= value < float('inf'):
                    rows.append(('NBA', player, stat, game.get('date'), value))
        cursor = page.get('meta', {}).get('next_cursor')
        if not cursor:
            break
        params['cursor'] = cursor
    return rows


@lru_cache(maxsize=3)
def nfl_history(season, _window):
    urls = [f'{NFLVERSE}/stats_player/stats_player_week_{year}.csv'
            for year in (season - 1, season)]
    urls.append(f'{NFLVERSE}/schedules/games.csv')
    with ThreadPoolExecutor(max_workers=3) as pool:
        previous, current, schedule = pool.map(lambda url: download(url).content, urls)
    rows = pd.concat([pd.read_csv(BytesIO(previous)), pd.read_csv(BytesIO(current))], ignore_index=True)
    with TemporaryDirectory() as folder:
        stats, games = Path(folder) / 'stats.csv', Path(folder) / 'games.csv'
        rows.to_csv(stats, index=False)
        games.write_bytes(schedule)
        return nfl(stats, games)


def load_history(sport, offers, now):
    if sport == 'NBA':
        # Bound cold requests to stay within the provider's per-minute quota.
        players = offers['player'].drop_duplicates().head(8)
        with ThreadPoolExecutor(max_workers=8) as pool:
            logs = list(pool.map(lambda name: nba_player(name, now.date(), bucket(60)), players))
        return pd.DataFrame([row for log in logs for row in log], columns=COLUMNS).drop_duplicates()
    if sport == 'NFL':
        season = now.year - (now.month < 3)
        return nfl_history(season, bucket(360))
    raise FeedError('Automatic player history is unavailable for this sport.')
