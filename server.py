from flask import Flask, request, jsonify, send_from_directory
import pandas as pd
import numpy as np
from io import StringIO
from engine import predict,evaluate,backtest,wilson_interval
from pathlib import Path
from sportsdata import test_connection
ROOT=Path(__file__).parent
app=Flask(__name__,static_folder='static',static_url_path='')

def demo():
    rng=np.random.default_rng(19)
    now=pd.Timestamp.now(tz='UTC').floor('h')
    specs=[('NBA','Demo Guard A','points',25,22.5),('NBA','Demo Center B','rebounds',12,9.5),('NFL','Demo Receiver C','receptions',7,5.5),('NHL','Demo Skater D','shots',4,2.5),('MLB','Demo Pitcher E','strikeouts',8,5.5),('WNBA','Demo Guard F','assists',8,5.5),('Soccer','Demo Forward G','shots',4,2.5)]
    h=[];o=[]
    for i,(sport,player,stat,mean,line) in enumerate(specs):
        for j in range(40):
            h.append({'sport':sport,'player':player,'stat':stat,'date':(now-pd.Timedelta(days=42-j)).isoformat(),'value':max(0,int(rng.normal(mean,max(1,mean*.2))))})
        o.append({'sport':sport,'player':player,'stat':stat,'line':line,'as_of':now.isoformat(),'offer_id':f'demo-{i}','event_id':f'game-{i}'})
    return pd.DataFrame(h),pd.DataFrame(o)

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

@app.get('/api/demo')
def demo_endpoint():
    h,o=demo()
    return analyze(h,o,False,True)

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
        result={'demo':is_demo,'disclaimer':'Synthetic demonstration; uncalibrated estimates. No live lines, guarantees, or validated accuracy.',
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
def health():return jsonify(status='ok')
@app.get('/api/sportsdata-test')
def sportsdata_test():
    return jsonify(test_connection())
if __name__=='__main__':app.run(host='127.0.0.1',port=5000,debug=False)
