import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import today
import player_history
from membership import account, init_accounts, premium_required
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
    trial = g.membership_access == 'trial'
    if trial and run_backtest:
        return jsonify(error='Backtesting is available with a paid subscription.'), 402
    try:
        h=parse_csv(request.files.get('history'),['sport','player','stat','date','value'])
        o=parse_csv(request.files.get('offers'),['sport','player','stat','line','as_of','offer_id','event_id'])
        if trial and len(o) > 5:
            return jsonify(error='Free trials can analyze up to 5 offers per upload.'), 402
        return analyze(h, o, run_backtest)
    except (ValueError,UnicodeError,pd.errors.ParserError) as e:return jsonify(error=str(e)),400

def analyze(h,o,run_backtest):
    try:
        pred=predict(h,o)
        entries=evaluate(pred,min_prob=.75,max_entries=10)
        picks=pred.sort_values('p_raw',ascending=False).head(60) if not pred.empty else pred
        result={'disclaimer':'Statistical estimates are uncalibrated and not guaranteed. Uploaded offers are not verified live lines.',
                'picks':records(picks[['sport','player','stat','line','side','p_raw','projection','games']] if not picks.empty else picks),
                'entries':records(entries[['Pick 1','Pick 2','Estimated all-hit %','Pick 1 %','Pick 2 %']] if not entries.empty else entries),
                'count':len(pred)//2,'backtest':None}
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
            counts = list(pool.map(lambda sport: len(today.today_events(sport, now)), sports))
    except today.FeedError as exc:
        return jsonify(error=str(exc)), 503
    return jsonify(sports=[{'name': sport, 'events': count}
                           for sport, count in zip(sports, counts) if count])


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
        available = today.offers(sport, now)
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
    if g.membership_access == 'trial':
        offers = offers.merge(history[['sport', 'player', 'stat']].drop_duplicates(),
                              on=['sport', 'player', 'stat']).head(5)
    if offers.empty:
        return jsonify(error='No current lines match the available player stats.'), 404
    response = app.make_response(analyze(history, offers, False))
    if not response.is_json or not response.json.get('picks'):
        return jsonify(error='There is not enough matching recent player history.'), 404
    result = response.json
    stats_source = {'NBA': 'BALLDONTLIE', 'NFL': 'nflverse', 'COLLEGE FOOTBALL': 'CollegeFootballData', 'COLLEGE BASKETBALL': 'CollegeBasketballData'}.get(sport, '') if not (path and path.is_file()) else ''
    result['source'] = 'PrizePicks via The Odds API' + (' · Stats: ' + stats_source if stats_source else '')
    result['disclaimer'] = 'Model estimates for current PrizePicks lines. Coverage is limited to one upcoming game per sport.'
    return jsonify(result)


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

