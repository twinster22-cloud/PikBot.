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

class FeedQuotaError(FeedError):
    """A provider rejected a request because of its rate or credit limit."""


BDL = 'https://api.balldontlie.io/v1'
NFLVERSE = 'https://github.com/nflverse/nflverse-data/releases/download'


def download(url, **kwargs):
    try:
        response = requests.get(url, timeout=(5, 25), **kwargs)
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
    needed = {'season', 'week', 'team', 'recent_team', 'opponent_team',
              'player_id', 'player_display_name', 'passing_yards',
              'rushing_yards', 'receiving_yards'}
    rows = pd.concat([pd.read_csv(BytesIO(data), usecols=lambda col: col in needed)
                      for data in (previous, current)], ignore_index=True)
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
        except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
            retry_delay = 21600
            error = 'College provider quota reached.' if 'HTTP 429' in str(exc) else 'The college history refresh failed. It will retry later.'
        finally:
            with COLLEGE_LOCK:
                COLLEGE_STATE[league] = (monotonic() + retry_delay, error)

    with COLLEGE_LOCK:
        next_refresh, error = COLLEGE_STATE.get(league, (0, None))
        if monotonic() >= next_refresh:
            COLLEGE_STATE[league] = (float('inf'), None)
            Thread(target=refresh, name='pikbot-college-' + league, daemon=True).start()
            error = None
    if error and 'quota reached' in error:
        raise FeedQuotaError(error)
    if not path.is_file():
        raise FeedError(error or 'College game history is loading. Try again shortly.')
    return pd.read_csv(path)


DUNK_LEAGUES = {'NBA': 'nba', 'WNBA': 'wnba'}


def sportsdata_post(provider, host, key, endpoint, **body):
    try:
        response = requests.post(host + '/v1/' + endpoint,
                                 json=body, headers={'x-api-key': key}, timeout=(5, 20))
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get('data'), dict):
            raise ValueError()
        return payload['data']
    except requests.HTTPError as exc:
        status = exc.response.status_code
        if status == 429 or (provider == 'DunkAPI' and status == 402):
            raise FeedQuotaError(f'{provider} request or credit limit reached.') from None
        raise FeedError(f'{provider} denied the stats request (HTTP {status}). Check key and credits.') from None
    except (requests.RequestException, ValueError):
        raise FeedError(f'{provider} player history is temporarily unavailable.') from None


def dunk_get(endpoint, **body):
    key = os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')
    return sportsdata_post('DunkAPI', 'https://mcp.dunkapi.com', key, endpoint, **body)


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


ADDITIONAL_FEEDS = {
    'NHL': ('PuckAPI', 'https://mcp.puckapi.com', 'PUCKAPI_API_KEY', 'query', 'id', 'get_player_game_log'),
    'MLB': ('FastballAPI', 'https://mcp.fastballapi.com', 'FASTBALLAPI_API_KEY', 'name', 'player_id', 'get_player_games'),
}


@lru_cache(maxsize=512)
def additional_player_id(sport, player):
    provider, host, env, search_field, id_field, _ = ADDITIONAL_FEEDS[sport]
    rows = sportsdata_post(provider, host, os.environ[env], 'search_players',
                           **{search_field: player}).get('players', [])
    matches = {row[id_field] for row in rows if row.get(id_field)
               and str(row.get('name', '')).casefold() == player.casefold()}
    return next(iter(matches)) if len(matches) == 1 else None


@lru_cache(maxsize=512)
def additional_games(sport, player_id, season, _day):
    provider, host, env, _, _, endpoint = ADDITIONAL_FEEDS[sport]
    return sportsdata_post(provider, host, os.environ[env], endpoint,
                           player_id=player_id, season=season, limit=50)


def additional_history(sport, offers, now):
    provider, _, env, *_ = ADDITIONAL_FEEDS[sport]
    if not os.getenv(env):
        raise FeedError(f'{sport} player history requires a {provider} API key.')
    day = now.date().isoformat()
    season_start = now.year - int(now.month < 9)

    def player_rows(player):
        player_id = additional_player_id(sport, player)
        if not player_id:
            return []
        if sport == 'NHL':
            fields = (('shots', 'SOG'), ('points', 'PTS'))
            date_field = 'gameDate'
            games = additional_games(sport, player_id, f'{season_start}{season_start + 1}', day).get('games', [])
            if len(games) < 12:
                previous = additional_games(sport, player_id, f'{season_start - 1}{season_start}', day)
                games = games + previous.get('games', [])
        else:
            fields = (('h', 'H'),)
            date_field = 'date'
            games = additional_games(sport, player_id, now.year, day).get('batting', [])
            if len(games) < 12:
                games = games + additional_games(sport, player_id, now.year - 1, day).get('batting', [])
        return [(sport, player, stat, game[date_field], game[field])
                for game in games if game.get(date_field) and game[date_field] < day
                for field, stat in fields
                if isinstance(game.get(field), (int, float)) and 0 <= game[field] < float('inf')]

    players = offers['player'].drop_duplicates().head(3)
    with ThreadPoolExecutor(max_workers=3) as pool:
        rows = list(pool.map(player_rows, players))
    return pd.DataFrame([row for player in rows for row in player], columns=COLUMNS)



RESERVE_LOCK = Lock()
DUNK_RESERVE_UNTIL = 0


@lru_cache(maxsize=2)
def reserve_nba_season(season, _day):
    url = ('https://github.com/sportsdataverse/sportsdataverse-data/releases/download/'
           f'espn_nba_player_boxscores/player_box_{season}.csv')
    try:
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        columns = ['game_id', 'game_date', 'athlete_id', 'athlete_display_name',
                   'did_not_play', 'minutes', 'points', 'rebounds', 'assists',
                   'athlete_headshot_href']
        return pd.read_csv(BytesIO(response.content), usecols=columns,
                           dtype={'athlete_id': str, 'game_id': str})
    except (requests.RequestException, ValueError):
        raise FeedError('The free NBA reserve history is temporarily unavailable.') from None


def reserve_nba_history(offers, now):
    season = now.year + int(now.month >= 9)
    with RESERVE_LOCK:
        current = reserve_nba_season(season, now.date().isoformat())
        newest = pd.to_datetime(current['game_date'], utc=True, errors='coerce').max()
        if pd.isna(newest) or now - newest.to_pydatetime() > timedelta(days=7):
            raise FeedError('The free NBA reserve has not published sufficiently recent games.')
        previous = reserve_nba_season(season - 1, now.date().isoformat())
    frame = pd.concat([previous, current], ignore_index=True)
    frame['date'] = pd.to_datetime(frame['game_date'], utc=True, errors='coerce')
    frame = frame[(frame['date'] < now.replace(hour=0, minute=0, second=0, microsecond=0))
                  & (frame['date'] >= now - timedelta(days=365))]
    frame = frame[(frame['did_not_play'].astype(str).str.lower() == 'false')
                  & (pd.to_numeric(frame['minutes'], errors='coerce') > 0)]
    rows, photos = [], {}
    for name in offers['player'].drop_duplicates():
        matches = frame[frame['athlete_display_name'].str.casefold() == str(name).casefold()]
        if matches['athlete_id'].nunique() != 1:
            continue
        photo = matches['athlete_headshot_href'].dropna()
        if len(photo):
            photos[name] = str(photo.iloc[-1])
        for field, stat in (('points', 'PTS'), ('rebounds', 'REB'), ('assists', 'AST')):
            games = matches[['game_id', 'date', field]].copy()
            games[field] = pd.to_numeric(games[field], errors='coerce')
            conflicts = games.groupby('game_id')[field].nunique()
            games = games[~games['game_id'].isin(conflicts[conflicts > 1].index)]
            for _, game in games.drop_duplicates('game_id').iterrows():
                value = game[field]
                if pd.notna(value) and 0 <= value < float('inf'):
                    rows.append(('NBA', name, stat, game['date'].isoformat(), float(value)))
    result = pd.DataFrame(rows, columns=COLUMNS)
    result.attrs['source'] = 'SportsDataverse / hoopR (reserve)'
    result.attrs['photos'] = photos
    return result


def _primary_history(sport, offers, now):
    if sport in ADDITIONAL_FEEDS:
        return additional_history(sport, offers, now)
    if sport in DUNK_LEAGUES and (os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')):
        global DUNK_RESERVE_UNTIL
        if sport == 'NBA' and monotonic() < DUNK_RESERVE_UNTIL:
            return reserve_nba_history(offers, now)
        try:
            return dunk_history(sport, offers, now)
        except FeedQuotaError:
            if sport != 'NBA':
                raise
            DUNK_RESERVE_UNTIL = monotonic() + 600
            return reserve_nba_history(offers, now)
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


RESERVE_COOLDOWN = {}


def load_history(sport, offers, now):
    from reserve_feeds import reserve_history
    if sport in RESERVE_COOLDOWN and monotonic() < RESERVE_COOLDOWN[sport]:
        return reserve_history(sport, offers, now)
    try:
        return _primary_history(sport, offers, now)
    except FeedQuotaError:
        if sport not in {'WNBA', 'NHL', 'MLB', 'COLLEGE FOOTBALL', 'COLLEGE BASKETBALL'}:
            raise
        RESERVE_COOLDOWN[sport] = monotonic() + 600
        return reserve_history(sport, offers, now)

