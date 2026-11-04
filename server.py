from datetime import datetime, timezone
from functools import lru_cache
import today
from flask import Flask, request, jsonify, send_from_directory
import pandas as pd
import numpy as np
from io import StringIO
from engine import predict,evaluate,backtest,wilson_interval
from pathlib import Path
from sportsdata import test_connection
ROOT=Path(__file__).parent
app=Flask(__name__,static_folder='static',static_url_path='')

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
def index():return send_from_directory(ROOT/'static','index.html')

@app.post('/api/analyze')
def analyze_endpoint():
    try:
        h=parse_csv(request.files.get('history'),['sport','player','stat','date','value'])
        o=parse_csv(request.files.get('offers'),['sport','player','stat','line','as_of','offer_id','event_id'])
        return analyze(h,o,request.form.get('backtest')=='true',False)
    except (ValueError,UnicodeError,pd.errors.ParserError) as e:return jsonify(error=str(e)),400

def analyze(h,o,run_backtest,is_demo):
    try:
        pred=predict(h,o)
        entries=evaluate(pred,min_prob=.75,max_entries=10)
        picks=pred.sort_values('p_raw',ascending=False).head(60) if not pred.empty else pred
        result={'demo':False,'disclaimer':'Statistical estimates are uncalibrated and not guaranteed. Uploaded offers are not verified live lines.',
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


@app.get('/health')
def health():return jsonify(status='ok')
@app.get('/api/sportsdata-test')
def sportsdata_test():
    return jsonify(test_connection())
@app.get('/api/live-props-status')
def live_props_status():
    return jsonify(connected=False, live_props=False, reason='SportsDataIO props endpoint and sportsbook group have not been validated for this API subscription. Historical uploads are not live betting lines.'), 503

if __name__=='__main__':app.run(host='0.0.0.0',port=int(__import__('os').environ.get('PORT',5000)),debug=False)

