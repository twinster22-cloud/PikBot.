"""Fetch completed college box scores into PikBot's live history directory."""
import argparse
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pandas as pd
import requests

from import_live_history import format_history

FOOTBALL = 'https://api.collegefootballdata.com'
BASKETBALL = 'https://api.collegebasketballdata.com'


def fetch(host, path, **params):
    key = os.getenv('COLLEGE_DATA_API_KEY')
    if not key:
        raise RuntimeError('Set COLLEGE_DATA_API_KEY before refreshing.')
    try:
        response = requests.get(host + path, params=params,
                                headers={'Authorization': 'Bearer ' + key}, timeout=25)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError('Expected a list.')
        return data
    except requests.HTTPError as exc:
        raise RuntimeError(f'College data request failed (HTTP {exc.response.status_code}).') from None
    except (requests.RequestException, ValueError):
        raise RuntimeError('College data request failed or returned an invalid response.') from None


def basketball_rows(games, boxes):
    dates = {game['id']: game['startDate'] for game in games if game.get('status') == 'final'}
    for box in boxes:
        if box['gameId'] not in dates:
            continue
        for player in box['players']:
            if not (player.get('minutes') or 0) > 0:
                continue
            yield {'player': player['name'], 'date': dates[box['gameId']],
                   'points': player.get('points'), 'assists': player.get('assists'),
                   'rebounds': (player.get('rebounds') or {}).get('total')}


def football_rows(dates, boxes):
    categories = {'passing': 'pass', 'rushing': 'rush'}
    for box in boxes:
        if box['id'] not in dates:
            continue
        for team in box['teams']:
            players = {}
            for category in team['categories']:
                stat = categories.get(category['name'].lower())
                if not stat:
                    continue
                for kind in category['types']:
                    if kind['name'].upper() != 'YDS':
                        continue
                    for athlete in kind['athletes']:
                        player = players.setdefault(athlete['id'], {
                            'player': athlete['name'], 'date': dates[box['id']],
                        })
                        player[stat] = athlete['stat']
            yield from players.values()


def basketball(start, end):
    while start < end:
        stop = min(start + timedelta(days=7), end)
        query = {'startDateRange': start.isoformat(), 'endDateRange': stop.isoformat()}
        games = fetch(BASKETBALL, '/games', status='final', **query)
        if games:
            boxes = fetch(BASKETBALL, '/games/players', **query)
            if len(games) >= 3000 or len({box['gameId'] for box in boxes}) >= 1000:
                raise RuntimeError('Basketball response reached the provider cap. Use a smaller date range.')
            yield from basketball_rows(games, boxes)
        start = stop


def football(start, end):
    for year in range(start.year - 1, end.year + 1):
        for season_type in ('regular', 'postseason'):
            games = fetch(FOOTBALL, '/games', year=year, seasonType=season_type,
                          classification='fbs')
            weeks = {}
            for game in games:
                if not game.get('completed') or not game.get('startDate'):
                    continue
                date = datetime.fromisoformat(game['startDate'])
                if start <= date < end:
                    weeks.setdefault(game['week'], {})[game['id']] = game['startDate']
            for week, dates in weeks.items():
                boxes = fetch(FOOTBALL, '/games/players', year=year, week=week,
                              seasonType=season_type, classification='fbs')
                yield from football_rows(dates, boxes)


def refresh_college(output_dir, league, days):
    end = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    if league == 'basketball':
        rows, sport, filename = basketball(start, end), 'COLLEGE BASKETBALL', 'basketball_ncaab.csv'
        stats = {'points': 'PTS', 'rebounds': 'REB', 'assists': 'AST'}
    else:
        rows, sport, filename = football(start, end), 'COLLEGE FOOTBALL', 'americanfootball_ncaaf.csv'
        stats = {'pass': 'PASS_YDS', 'rush': 'RUSH_YDS'}
    frame = pd.DataFrame(rows).reindex(columns=['player', 'date', *stats])
    result = format_history(frame, sport, stats, skip_conflicts=True)
    destination = output_dir / filename
    if destination.is_file() and days < 365:
        prior = pd.read_csv(destination)
        dates = pd.to_datetime(prior['date'], utc=True, errors='coerce')
        retained = prior.loc[(dates >= end - timedelta(days=365)) & (dates < start.replace(hour=0, minute=0, second=0, microsecond=0))]
        result = pd.concat([retained, result], ignore_index=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode='w', dir=output_dir, delete=False) as temporary:
        path = Path(temporary.name)
        try:
            result.to_csv(temporary, index=False)
            temporary.close()
            path.replace(destination)
        finally:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('league', choices=('basketball', 'football'))
    parser.add_argument('--days', type=int, default=365, choices=range(1, 366), metavar='1–365')
    args = parser.parse_args()
    refresh_college(args.output_dir, args.league, args.days)


if __name__ == '__main__':
    main()
