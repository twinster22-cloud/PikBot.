"""Grade saved PikBot selections against completed player box scores."""
import math
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from unicodedata import normalize
from zoneinfo import ZoneInfo

import requests

from engine import outcome
from today import bucket, timestamp

BASE = 'https://site.api.espn.com/apis/site/v2/sports/'
EASTERN = ZoneInfo('America/New_York')
BASKETBALL = {'PTS': ((), ('points',)), 'REB': ((), ('rebounds',)), 'AST': ((), ('assists',))}
FOOTBALL = {'PASS_YDS': (('passing',), ('passingYards',)), 'RUSH_YDS': (('rushing',), ('rushingYards',))}
SKATERS = ('forwards', 'defenses', 'skaters')
LEAGUES = {
    'NBA': ('basketball/nba', BASKETBALL),
    'WNBA': ('basketball/wnba', BASKETBALL),
    'COLLEGE BASKETBALL': ('basketball/mens-college-basketball', BASKETBALL),
    'NFL': ('football/nfl', {**FOOTBALL, 'REC_YDS': (('receiving',), ('receivingYards',))}),
    'COLLEGE FOOTBALL': ('football/college-football', FOOTBALL),
    'MLB': ('baseball/mlb', {'H': (('batting',), ('hits',)), 'SO': (('pitching',), ('strikeouts',))}),
    'NHL': ('hockey/nhl', {'SOG': (SKATERS, ('shotsTotal',)), 'PTS': (SKATERS, ('goals', 'assists'))}),
}


def name_key(value):
    return ''.join(c.casefold() for c in normalize('NFKD', value or '') if c.isalnum())


def fetch(league, resource, **params):
    response = requests.get(f'{BASE}{league}/{resource}', params=params, timeout=(1.5, 2.5))
    response.raise_for_status()
    return response.json()


@lru_cache(maxsize=100)
def scoreboard(league, day, _window):
    params = {'dates': day, 'limit': 500}
    if league == 'basketball/mens-college-basketball':
        params['groups'] = 50
    elif league == 'football/college-football':
        params['groups'] = 80
    return fetch(league, 'scoreboard', **params).get('events', [])


@lru_cache(maxsize=100)
def final_boxscore(league, event_id, _window):
    summary = fetch(league, 'summary', event=event_id)
    games = summary.get('header', {}).get('competitions', [])
    if len(games) == 1 and games[0].get('status', {}).get('type', {}).get('completed') is True:
        return summary.get('boxscore', {}).get('players', [])
    return []


def game_stats(sport, matchup, starts_at, now):
    start = timestamp(starts_at)
    sides = matchup.split(' at ', 1)
    if not start or start >= now or sport not in LEAGUES or len(sides) != 2:
        return []
    league, _ = LEAGUES[sport]
    expected = dict(zip(('away', 'home'), map(name_key, sides)))
    matches = []
    window = bucket(30)
    for event in scoreboard(league, start.astimezone(EASTERN).strftime('%Y%m%d'), window):
        games = event.get('competitions', [])
        kickoff = timestamp(event.get('date'))
        if len(games) != 1 or not kickoff or abs((kickoff - start).total_seconds()) > 3600:
            continue
        teams = {entry.get('homeAway'): name_key(entry.get('team', {}).get('displayName'))
                 for entry in games[0].get('competitors', [])}
        if teams == expected:
            matches.append(event)
    if len(matches) != 1 or matches[0].get('status', {}).get('type', {}).get('completed') is not True:
        return []
    return final_boxscore(league, matches[0]['id'], window)


def actual_stat(pick, teams):
    categories, stats_keys = LEAGUES[pick['sport']][1].get(pick['stat'], ((), ()))
    if not stats_keys:
        return None
    player = name_key(pick['player'])
    values = set()
    for team in teams:
        for group in team.get('statistics', []):
            keys = group.get('keys', [])
            if (categories and (group.get('name') or group.get('type')) not in categories
                    or any(key not in keys for key in stats_keys)):
                continue
            indexes = [keys.index(key) for key in stats_keys]
            for row in group.get('athletes', []):
                athlete = row.get('athlete', {})
                stats = row.get('stats', [])
                if row.get('didNotPlay') or row.get('inactive') or max(indexes) >= len(stats):
                    continue
                if athlete.get('id') and player == name_key(athlete.get('displayName')):
                    try:
                        value = sum(float(stats[index]) for index in indexes)
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        values.add((athlete['id'], value))
    return next(iter(values))[1] if len(values) == 1 else None


def grade(picks, now):
    games = {(pick['sport'], pick.get('matchup') or '', pick.get('starts_at')) for pick in picks}

    def lookup(game):
        try:
            return game_stats(*game, now)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            return []

    with ThreadPoolExecutor(max_workers=4) as workers:
        boxscores = dict(zip(games, workers.map(lookup, games)))
    for pick in picks:
        game = (pick['sport'], pick.get('matchup') or '', pick.get('starts_at'))
        actual = actual_stat(pick, boxscores[game]) if boxscores[game] else None
        pick['actual'] = actual
        pick['result'] = outcome(actual, pick['line'], pick['side']) if actual is not None else 'PENDING'
    return picks
