from functools import lru_cache
import requests
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import today
import player_history
from membership import account, daily_trial_picks, init_accounts, premium_required
from flask import Flask, g, request, jsonify, send_from_directory, redirect
import pandas as pd
import numpy as np
from io import StringIO
from engine import predict,evaluate,backtest,wilson_interval
from pathlib import Path
from sportsdata import test_connection
ROOT=Path(__file__).parent
app=Flask(__name__, static_folder=None)
init_accounts(app)

@app.get('/<asset>')
def public_asset(asset):
    if asset not in {'sw.js', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png', 'splash.png', 'icon.svg'}:
        return jsonify(error='Not found'), 404
    return send_from_directory(ROOT/'static', asset)

@app.after_request
def prevent_private_caching(response):
    if request.path in {'/', '/login', '/api/account'}:
        response.headers['Cache-Control'] = 'no-store'
    return response

def parse_csv(upload,required):
    if not upload or not upload.filename.lower().endswith('.csv'):raise ValueError('Upload a .csv file')
    if request.content_length and request.content_length>4_000_000:raise ValueError('Upload too large (4 MB maximum)')
    df=pd.read_csv(StringIO(upload.read().decode('utf-8-sig')),low_memory=False)
    missing=set(required)-set(df.columns)
    if missing:raise ValueError('Missing columns: '+', '.join(sorted(missing)))
    if len(df)>25000:raise ValueError('Maximum 25,000 rows per file')
    return df

def records(df):
    if df.empty:return []
    return df.replace({np.nan:None}).to_dict(orient='records')

@app.get('/')
def index():
    if not account():
        return redirect('/login')
    return send_from_directory(ROOT/'static','index.html')

@app.post('/api/analyze')
@premium_required
def analyze_endpoint():
    run_backtest = request.form.get('backtest') == 'true'
    if g.membership_access == 'trial':
        return jsonify(error='Trial accounts get three live picks per day. CSV analysis requires a subscription.'), 402
    try:
        h=parse_csv(request.files.get('history'),['sport','player','stat','date','value'])
        o=parse_csv(request.files.get('offers'),['sport','player','stat','line','as_of','offer_id','event_id'])
        return analyze(h, o, run_backtest)
    except (ValueError,UnicodeError,pd.errors.ParserError) as e:return jsonify(error=str(e)),400

def analyze(h,o,run_backtest, *, trial_live=False, live=False):
    try:
        pred=predict(h,o,include_history=live)
        if trial_live and not pred.empty:
            pred = daily_trial_picks(pred)
            if pred.empty:
                return jsonify(error='Your three live picks for today have been used. The limit resets at midnight Eastern.'), 429
        entries=evaluate(pred,min_prob=.75,max_entries=10)
        picks = pred.sort_values('p_raw', ascending=False) if not pred.empty else pred
        if live and not picks.empty:
            picks = picks.drop_duplicates(['sport', 'event_id', 'player', 'stat', 'line'])
        picks = picks.head(60)
        result={'disclaimer':'Statistical estimates are uncalibrated and not guaranteed. Uploaded offers are not verified live lines.',
                'picks':records(picks[['sport','player','stat','line','side','p_raw','projection','games'] + (['event_id', 'p_push', 'recent_games'] if live else [])] if not picks.empty else picks),
                'entries':records(entries[['Pick 1','Pick 2','Estimated all-hit %','Pick 1 %','Pick 2 %']] if not entries.empty else entries),
                'count':len(pred) if trial_live else len(pred)//2,'backtest':None}
        if run_backtest:
            if 'actual' not in o.columns:raise ValueError('Backtest needs an actual column in offers.csv')
            p,e=backtest(h,o)
            wins=int(e.entry_win.sum()) if not e.empty else 0
            lo,hi=wilson_interval(wins,len(e))
            result['backtest']={'entries':len(e),'wins':wins,'win_rate':round(100*wins/len(e),2) if len(e) else None,'interval':[round(lo*100,2),round(hi*100,2)] if len(e) else None,'individual_picks':len(p),'brier':round(float(p.brier.mean()),4) if len(p) else None,'note':'Research-only; overlapping picks may invalidate independence-based confidence intervals.'}
        return jsonify(result)
    except (ValueError,KeyError,TypeError) as e:return jsonify(error=str(e)),400

@app.get('/api/today')
@premium_required
def todays_sports():
    now = datetime.now(timezone.utc)
    try:
        sports = today.active_sports(today.bucket(30))
        with ThreadPoolExecutor(max_workers=7) as pool:
            slates = list(pool.map(lambda sport: today.today_events(sport, now), sports))
    except today.FeedError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(sports=[{
        'name': sport, 'events': len(games),
        'matchups': [{'id': game['id'], 'home': game.get('home_team'), 'away': game.get('away_team'),
                      'starts_at': game.get('commence_time'),
                      'started': today.timestamp(game['commence_time']) <= now}
                     for game in sorted(games, key=lambda game: game['commence_time'])],
    } for sport, games in zip(sports, slates) if games])


@app.get('/api/today/predictions')
@premium_required
def todays_predictions():
    sport = request.args.get('sport', '')
    if sport not in today.SPORTS:
        return jsonify(error='Choose a supported sport.'), 400
    directory = os.getenv('PIKBOT_LIVE_HISTORY_DIR')
    key, _ = today.SPORTS[sport]
    path = Path(directory) / (key + '.csv') if directory else None
    try:
        now = datetime.now(timezone.utc)
        available = today.offers(sport, now, event_id=request.args.get('event_id'))
        if not available:
            return jsonify(error='No fresh PrizePicks lines are available for the covered game.'), 404
        offers = pd.DataFrame(available)
        history = (pd.read_csv(path) if path and path.is_file()
                   else player_history.load_history(sport, offers, now))
        if not set(player_history.COLUMNS).issubset(history.columns):
            return jsonify(error='The player history has invalid columns.'), 503
    except today.FeedError as exc:
        return jsonify(error=str(exc)), 503
    except (OSError, ValueError, KeyError):
        return jsonify(error='Recent player stats could not be read.'), 503
    if offers.empty:
        return jsonify(error='No current lines match the available player stats.'), 404
    response = app.make_response(analyze(history, offers, False, trial_live=g.membership_access == 'trial', live=True))
    if response.status_code != 200:
        return response
    if not response.is_json or not response.json.get('picks'):
        return jsonify(error='There is not enough matching recent player history.'), 404
    result = response.json
    stats_source = {'NBA': 'BALLDONTLIE', 'NFL': 'nflverse', 'COLLEGE FOOTBALL': 'CollegeFootballData', 'COLLEGE BASKETBALL': 'CollegeBasketballData'}.get(sport, '') if not (path and path.is_file()) else ''
    if not (path and path.is_file()) and sport in player_history.DUNK_LEAGUES and (os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')):
        stats_source = 'DunkAPI'
    matchups = {game['id']: game for game in today.today_events(sport, now)}
    for pick in result['picks']:
        game = matchups.get(pick['event_id'], {})
        pick['matchup'] = ' at '.join(filter(None, (game.get('away_team'), game.get('home_team'))))
        pick['starts_at'] = game.get('commence_time')
    result['source'] = 'PrizePicks via The Odds API' + (' · Stats: ' + stats_source if stats_source else '')
    result['disclaimer'] = 'Model estimates for current PrizePicks lines. Only players with enough recent history are shown.'
    return jsonify(result)


@app.get('/api/odds-feed-check')
def odds_feed_check():
    with today.LOCK:
        result = check_odds_feed(today.bucket(60))
    return jsonify(result)


@lru_cache(maxsize=2)
def check_odds_feed(_window):
    try:
        slate = today.events('NBA', today.bucket(30))
        now = datetime.now(timezone.utc)
        event = next((item for item in slate
                      if (start := today.timestamp(item.get('commence_time'))) and start > now), None)
        if not event:
            return {'key_accepted': True, 'prizepicks_verified': False, 'reason': 'No upcoming NBA game to test.'}
        payload = today.event_offers('NBA', event['id'], _window)
        if payload.get('error'):
            return {'key_accepted': True, 'prizepicks_verified': False, 'reason': payload['error']}
        count = sum(len(market.get('outcomes', []))
                    for book in payload.get('bookmakers', []) if book.get('key') == 'prizepicks'
                    for market in book.get('markets', []))
        return {'key_accepted': True, 'prizepicks_verified': bool(count), 'outcomes': count,
                'reason': None if count else 'No PrizePicks outcomes returned for the tested NBA game.'}
    except today.FeedError as exc:
        return {'key_accepted': False, 'prizepicks_verified': False, 'reason': str(exc)}


@lru_cache(maxsize=2)
def check_college_feeds(_window):
    key = os.getenv('COLLEGE_DATA_API_KEY')
    if not key:
        return {'configured': False}
    now = datetime.now(timezone.utc)
    # Check a completed window so provider access can be tested out of season.
    february = datetime(now.year - (now.month < 3), 2, 1, tzinfo=timezone.utc)
    targets = {
        'college_basketball': ('https://api.collegebasketballdata.com/games/players', {
            'startDateRange': february.isoformat(),
            'endDateRange': (february + timedelta(days=1)).isoformat(),
        }),
        'college_football': ('https://api.collegefootballdata.com/games/players', {
            'year': now.year - (now.month < 9), 'week': 1,
            'seasonType': 'regular', 'classification': 'fbs',
        }),
    }

    def check(item):
        name, (url, params) = item
        try:
            response = requests.get(url, params=params, headers={'Authorization': 'Bearer ' + key}, timeout=10)
            data = response.json() if response.ok else None
            records = sum(isinstance(row, dict) and bool(row.get('players') or row.get('teams'))
                          for row in data) if isinstance(data, list) else 0
            return name, {'http_status': response.status_code, 'box_score_records': records,
                          'verified': bool(records)}
        except (requests.RequestException, ValueError):
            return name, {'verified': False, 'error': 'Provider request failed or returned invalid JSON.'}

    with ThreadPoolExecutor(max_workers=2) as pool:
        return {'configured': True, **dict(pool.map(check, targets.items()))}


@app.get('/api/college-feed-check')
def college_feed_check():
    with today.LOCK:
        return jsonify(check_college_feeds(today.bucket(60)))


@lru_cache(maxsize=1)
def check_sports_coverage(_day):
    now = datetime.now(timezone.utc)
    active = set(today.active_sports(today.bucket(30)))

    def check(sport):
        if sport not in active:
            return sport, {'status': 'not_in_active_catalog'}
        try:
            slate = today.events(sport, today.bucket(30))
            event = next((item for item in slate
                          if (start := today.timestamp(item.get('commence_time'))) and start > now), None)
            if not event:
                return sport, {'status': 'no_upcoming_game'}
            payload = today.event_offers(sport, event['id'], today.bucket(60))
            if payload.get('error'):
                return sport, {'status': 'provider_error', 'reason': payload['error']}
            count = sum(len(market.get('outcomes', []))
                        for book in payload.get('bookmakers', []) if book.get('key') == 'prizepicks'
                        for market in book.get('markets', []))
            return sport, {'status': 'lines_found' if count else 'no_lines_for_tested_game',
                           'outcomes': count, 'game_time': event.get('commence_time')}
        except today.FeedError as exc:
            return sport, {'status': 'provider_error', 'reason': str(exc)}

    with ThreadPoolExecutor(max_workers=7) as pool:
        return dict(pool.map(check, today.SPORTS))


@app.get('/api/sports-coverage-check')
def sports_coverage_check():
    try:
        with today.LOCK:
            coverage = check_sports_coverage(datetime.now(timezone.utc).date())
        return jsonify(coverage)
    except today.FeedError as exc:
        return jsonify(error=str(exc)), 503


@lru_cache(maxsize=1)
def check_dunk_history(day):
    if not (os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')):
        return {'configured': False, 'verified': False}
    try:
        player = player_history.dunk_player_id('nba', 'Jayson Tatum')
        if not player:
            return {'configured': True, 'verified': False, 'reason': 'Player lookup returned no unique match.'}
        now = datetime.now(timezone.utc)
        season = now.year + int(now.month >= 9)
        games = player_history.dunk_games('nba', player, season, day)
        if not games:
            games = player_history.dunk_games('nba', player, season - 1, day)
        count = sum(game.get('result') in {'W','L'} and not game.get('did_not_play')
                    and isinstance(game.get('pts'), (int,float)) for game in games)
        return {'configured': True, 'verified': bool(count), 'completed_games': count}
    except today.FeedError as exc:
        return {'configured': True, 'verified': False, 'reason': str(exc)}


@app.get('/api/dunk-feed-check')
@premium_required
def dunk_feed_check():
    with today.LOCK:
        return jsonify(check_dunk_history(datetime.now(timezone.utc).date().isoformat()))


@lru_cache(maxsize=8)
def check_next_projection(sport, _window):
    now = datetime.now(timezone.utc)
    try:
        offers = pd.DataFrame(today.offers(sport, now, next_slate=True))
        if offers.empty:
            return {'verified': False, 'reason': 'No fresh PrizePicks lines for the next upcoming matchup.'}
        history = player_history.load_history(sport, offers, now)
        modeled = predict(history, offers, include_history=True)
        if modeled.empty:
            return {'verified': False, 'lines': len(offers),
                    'reason': 'No lines matched enough recent player history.'}
        return {'verified': True, 'lines': len(offers),
                'modeled_offers': int(modeled['offer_id'].nunique()),
                'history_chart_verified': bool(modeled.iloc[0]['recent_games'])}
    except today.FeedError as exc:
        return {'verified': False, 'reason': str(exc)}
    except (OSError, ValueError, TypeError, KeyError):
        return {'verified': False, 'reason': 'The provider data could not be matched or scored.'}


@app.get('/api/next-projection-check')
@premium_required
def next_projection_check():
    sport = request.args.get('sport', 'NBA')
    if sport not in {'NBA', 'NFL'}:
        return jsonify(error='Choose NBA or NFL.'), 400
    return jsonify(check_next_projection(sport, today.bucket(60)))


@app.get('/health')
def health():return jsonify(status='ok')
@app.get('/api/sportsdata-test')
@premium_required
def sportsdata_test():
    return jsonify(test_connection())
@app.get('/api/live-props-status')
def live_props_status():
    return jsonify(connected=False, live_props=False, reason='SportsDataIO props endpoint and sportsbook group have not been validated for this API subscription. Historical uploads are not live betting lines.'), 503

if __name__=='__main__':app.run(host='0.0.0.0',port=int(__import__('os').environ.get('PORT',5000)),debug=False)

