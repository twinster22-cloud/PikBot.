from flask import Flask, request, jsonify, send_from_directory
import pandas as pd
import numpy as np
from io import StringIO
from engine import predict,evaluate,backtest,wilson_interval
from pathlib import Path
from sportsdata import test_connection
from live_props import fetch_nba_props, LivePropsError
import os
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
        result={'demo':is_demo,'disclaimer':'Research estimates from user-uploaded data; inputs are not independently verified. Probabilities are uncalibrated. No live lines, guarantees, or validated accuracy.',
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

@app.get('/health')
def health():
    return jsonify(status='ok')
    
@app.get('/api/sportsdata-test')
def sportsdata_test():
    return jsonify(test_connection())
    


@app.get('/api/nba/live-predictions')
def nba_live_predictions():
    """Read-only predictions; no demo fallback, no wagering, no secret disclosure."""
    try:
        # The baseline requires a sufficiently large, licensed historical data file.
        history_path = ROOT / 'data' / 'nba_history.csv'
        if not history_path.is_file():
            return jsonify(connected=False, live=False,
                           error='Historical NBA data file is not installed on the server'), 503
        history = pd.read_csv(history_path, usecols=['sport','player','stat','date','value'])
        if len(history) > 500000:
            return jsonify(error='Historical dataset exceeds supported size'), 503
        offers = fetch_nba_props()
        o = pd.DataFrame(offers)
        h = history[history['sport'].str.upper() == 'NBA']
        pred = predict(h, o)
        if pred.empty:
            return jsonify(connected=True, live=True, source='SportsDataIO',
                           updated_at=max(x['updated_at'] for x in offers),
                           offers=len(offers), picks=[], entries=[],
                           note='Fresh lines were received but none qualified for the model'), 200
        entries = evaluate(pred, min_prob=.75, max_entries=10)
        cols = ['sport','player','stat','line','side','p_raw','projection','games']
        return jsonify(connected=True, live=True, source='SportsDataIO',
                       updated_at=max(x['updated_at'] for x in offers),
                       offers=len(offers), picks=records(pred.sort_values('p_raw',ascending=False).head(60)[cols]),
                       entries=records(entries), disclaimer='Uncalibrated research estimates, not guaranteed results')
    except LivePropsError as exc:
        return jsonify(connected=False, live=False, error=str(exc)), 503
    except (ValueError, KeyError, pd.errors.ParserError):
        return jsonify(connected=False, live=False, error='Historical dataset is incompatible with the prediction engine'), 503

@app.get('/api/live-props-status')
def live_props_status():
    """Report feed readiness without making a paid API request or exposing secrets."""
    key_present = bool(os.environ.get('SPORTSDATAIO_API_KEY') or os.environ.get('SPORTSDATA_API_KEY'))
    group_present = bool(os.environ.get('SPORTSDATAIO_SPORTSBOOK_GROUP'))
    return jsonify(
        connected=False,
        live_props=False,
        api_key_configured=key_present,
        sportsbook_group_configured=group_present,
        reason='Live player-prop feed is not integrated or verified. Historical uploads are not live lines.',
    )

if __name__=='__main__':
    app.run(host='127.0.0.1',port=5000,debug=False)
