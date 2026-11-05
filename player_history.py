"""Refresh recent NBA and NFL history for the live projection endpoint."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock, Thread
from time import monotonic

from refresh_college_history import refresh_college

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


COLLEGE = {
    'COLLEGE BASKETBALL': ('basketball', 'basketball_ncaab.csv'),
    'COLLEGE FOOTBALL': ('football', 'americanfootball_ncaaf.csv'),
}
COLLEGE_LOCK = Lock()
COLLEGE_STATE = {}


def college_history(sport):
    if not os.getenv('COLLEGE_DATA_API_KEY'):
        raise FeedError('College player stats require the shared college API key.')
    league, filename = COLLEGE[sport]
    directory = Path(os.getenv('PIKBOT_COLLEGE_CACHE_DIR', '/tmp/pikbot-college-history'))
    path = directory / filename

    def refresh():
        retry_delay = 86400
        error = None
        try:
            refresh_college(directory, league, 7 if path.is_file() else 365)
        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
            retry_delay = 21600
            error = 'The college history refresh failed. It will retry later.'
        finally:
            with COLLEGE_LOCK:
                COLLEGE_STATE[league] = (monotonic() + retry_delay, error)

    with COLLEGE_LOCK:
        next_refresh, error = COLLEGE_STATE.get(league, (0, None))
        if monotonic() >= next_refresh:
            COLLEGE_STATE[league] = (float('inf'), None)
            Thread(target=refresh, name='pikbot-college-' + league, daemon=True).start()
            error = None
    if not path.is_file():
        raise FeedError(error or 'College game history is loading. Try again shortly.')
    return pd.read_csv(path)


DUNK_LEAGUES = {'NBA': 'nba', 'WNBA': 'wnba'}


def dunk_get(endpoint, **body):
    key = os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')
    try:
        response = requests.post('https://mcp.dunkapi.com/v1/' + endpoint,
                                 json=body, headers={'x-api-key': key}, timeout=5)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise ValueError()
        return payload['data']
    except requests.HTTPError as exc:
        raise FeedError(f'DunkAPI denied the stats request (HTTP {exc.response.status_code}). Check key and credits.') from None
    except (requests.RequestException, ValueError):
        raise FeedError('DunkAPI player history is temporarily unavailable.') from None


@lru_cache(maxsize=512)
def dunk_player_id(league, player):
    matches = {str(row['player_id']) for row in dunk_get('search_players', league=league, name=player).get('players', [])
               if row.get('player_id') and str(row.get('name', '')).casefold() == player.casefold()}
    return next(iter(matches)) if len(matches) == 1 else None


@lru_cache(maxsize=512)
def dunk_games(league, player_id, season, _day):
    return dunk_get('get_player_games', league=league, player_id=player_id,
                    season=season, limit=50).get('games', [])


def dunk_history(sport, offers, now):
    league = DUNK_LEAGUES[sport]
    season = now.year + int(league == 'nba' and now.month >= 9)
    def player_rows(player):
        player_id = dunk_player_id(league, player)
        if not player_id:
            return []
        day = now.date().isoformat()
        games = dunk_games(league, player_id, season, day)
        def completed(rows):
            return {row['game_id']: row for row in rows
                    if row.get('game_id') and row.get('result') in {'W', 'L'}
                    and not row.get('did_not_play') and row.get('date') and row['date'] < day}
        valid = completed(games)
        if len(valid) < 12:
            valid = {**completed(dunk_games(league, player_id, season - 1, day)), **valid}
        return [(sport, player, stat, row['date'], row[field])
                for row in valid.values()
                for field, stat in (('pts', 'PTS'), ('reb', 'REB'), ('ast', 'AST'))
                if isinstance(row.get(field), (int, float)) and 0 <= row[field] < float('inf')]
    players = offers['player'].drop_duplicates().head(3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(player_rows, players))
    return pd.DataFrame([row for player in rows for row in player], columns=COLUMNS).drop_duplicates()


def load_history(sport, offers, now):
    if sport in DUNK_LEAGUES and (os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')):
        return dunk_history(sport, offers, now)
    if sport in COLLEGE:
        return college_history(sport)
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
