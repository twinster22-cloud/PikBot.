"""Convert locally supplied player game logs to PikBot's live history format."""
import argparse
from pathlib import Path

import pandas as pd

COLUMNS = ['sport', 'player', 'stat', 'date', 'value']


def format_history(rows, sport, stats, *, skip_conflicts=False):
    output = rows.melt(id_vars=['player', 'date'], value_vars=list(stats),
                       var_name='stat', value_name='value')
    output['stat'] = output['stat'].map(stats)
    output['sport'] = sport
    output['date'] = pd.to_datetime(output['date'], errors='coerce', utc=True).dt.strftime('%Y-%m-%d')
    output['value'] = pd.to_numeric(output['value'], errors='coerce')
    output = output.dropna(subset=['player', 'date', 'value'])
    output = output[(output['player'] != '') & (output['value'] >= 0)
                    & (output['value'] < float('inf'))]
    output = output[COLUMNS].drop_duplicates()
    conflicts = output.duplicated(['sport', 'player', 'stat', 'date'], keep=False)
    if conflicts.any():
        if not skip_conflicts:
            raise ValueError('Conflicting observations found for the same player, stat, and date.')
        output = output.loc[~conflicts]
    if output.empty:
        raise ValueError('No valid game observations in the supplied files.')
    return output.sort_values(['player', 'stat', 'date'])


def nba(path):
    stats = {'points': 'PTS', 'reboundsTotal': 'REB', 'assists': 'AST'}
    columns = pd.read_csv(path, nrows=0).columns
    date_column = 'gameDate' if 'gameDate' in columns else 'gameDateTimeEst'
    rows = pd.read_csv(path, usecols=['firstName', 'lastName', date_column, 'numMinutes', *stats])
    minutes = pd.to_numeric(rows['numMinutes'], errors='coerce')
    rows = rows[minutes.gt(0)].copy()
    rows['player'] = rows['firstName'].str.strip() + ' ' + rows['lastName'].str.strip()
    return format_history(rows.rename(columns={date_column: 'date'}), 'NBA', stats)


def nfl(path, schedule_path):
    stats = {'passing_yards': 'PASS_YDS', 'rushing_yards': 'RUSH_YDS', 'receiving_yards': 'REC_YDS'}
    rows = pd.read_csv(path, low_memory=False)
    if 'team' not in rows and 'recent_team' in rows:
        rows = rows.rename(columns={'recent_team': 'team'})
    games = pd.read_csv(schedule_path).dropna(subset=['home_score', 'away_score'])
    keys = ['season', 'week']
    home = games[keys + ['gameday', 'home_team', 'away_team']].rename(
        columns={'home_team': 'team', 'away_team': 'opponent_team'})
    away = games[keys + ['gameday', 'home_team', 'away_team']].rename(
        columns={'away_team': 'team', 'home_team': 'opponent_team'})
    dated = rows.merge(pd.concat([home, away]), on=keys + ['team', 'opponent_team'],
                       how='inner', validate='many_to_one')
    dated = dated.rename(columns={'gameday': 'date', 'player_display_name': 'player'})
    dated['player'] = dated['player'].str.strip()
    if 'player_id' in dated:
        identities = dated.groupby('player')['player_id'].nunique()
        dated = dated[dated['player'].isin(identities[identities == 1].index)]
    return format_history(dated, 'NFL', stats, skip_conflicts=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('sport', choices=['nba', 'nfl'])
    parser.add_argument('player_stats', type=Path)
    parser.add_argument('output_dir', type=Path)
    parser.add_argument('--schedule', type=Path)
    args = parser.parse_args()
    if args.sport == 'nba':
        data, filename = nba(args.player_stats), 'basketball_nba.csv'
    else:
        if not args.schedule:
            parser.error('--schedule is required for NFL')
        data, filename = nfl(args.player_stats, args.schedule), 'americanfootball_nfl.csv'
    args.output_dir.mkdir(parents=True, exist_ok=True)
    destination = args.output_dir / filename
    temp = destination.with_suffix('.tmp')
    data.to_csv(temp, index=False)
    temp.replace(destination)


if __name__ == '__main__':
    main()
