import itertools
import math
import numpy as np
import pandas as pd
from scipy.stats import norm

KEYS=['sport','player','stat']

def predict(history, offers, min_games=12, max_age_days=30):
    """Baseline predictions. Never use records on/after the offer's as_of timestamp."""
    h=history.copy(); o=offers.copy()
    for col in ['date']:
        h[col]=pd.to_datetime(h[col],errors='coerce',utc=True)
    o['as_of']=pd.to_datetime(o['as_of'],errors='coerce',utc=True)
    h['value']=pd.to_numeric(h['value'],errors='coerce'); o['line']=pd.to_numeric(o['line'],errors='coerce')
    output=[]
    for _,r in o.iterrows():
        if pd.isna(r['as_of']) or pd.isna(r['line']) or r['line']<0: continue
        s=h.copy()
        for k in KEYS:s=s[s[k]==r[k]]
        s=s[(s['date']<r['as_of']) & (s['date']>=r['as_of']-pd.Timedelta(days=365))].dropna(subset=['value','date']).sort_values('date')
        x=s['value'].tail(50).to_numpy(float)
        if len(x)<min_games or np.any(x<0):continue
        if (r['as_of']-s['date'].max()).days>max_age_days:continue
        mu=.2*np.mean(x[-5:])+.3*np.mean(x[-10:])+.5*np.mean(x)
        sd=max(float(np.std(x,ddof=1)),.2*max(mu,1),1)
        line=float(r['line'])
        integer=np.allclose(x,np.round(x))
        if integer:
            more=norm.sf((math.floor(line)+.5-mu)/sd)
            less=norm.cdf((math.ceil(line)-.5-mu)/sd)
            push=max(0,1-more-less)
        else:
            more=norm.sf((line-mu)/sd);less=1-more;push=0.
        for side,p in [('MORE',more),('LESS',less)]:
            output.append({**{k:r[k] for k in KEYS},'line':line,'as_of':r['as_of'], 'event_id':str(r.get('event_id','')),
                'side':side,'p_raw':float(p),'p_push':float(push),'projection':float(mu),'games':len(x),
                'actual':r.get('actual',np.nan),'offer_id':str(r.get('offer_id',f'{r["sport"]}|{r["player"]}|{r["stat"]}|{r["as_of"]}|{line}'))})
    return pd.DataFrame(output)

def outcome(actual,line,side):
    if pd.isna(actual):return None
    if actual==line:return 'PUSH'
    return 'WIN' if (actual>line)==(side=='MORE') else 'LOSS'

def evaluate(picks, min_prob=.75, max_entries=5, max_picks=18):
    """Enumerate independent-pick 2-leg candidates; reject same event as conservative correlation guard."""
    if picks.empty:return pd.DataFrame()
    x=picks[(picks.p_raw>=min_prob)&(picks.p_push<.01)].sort_values('p_raw',ascending=False)
    x=x.drop_duplicates('offer_id').head(max_picks)
    rows=[]
    for (_,a),(_,b) in itertools.combinations(x.iterrows(),2):
        if a['player']==b['player'] and a['sport']==b['sport']:continue
        if a['event_id'] and b['event_id'] and a['event_id']==b['event_id']:continue
        # Independence is an unverified approximation; don't represent as calibrated.
        prob=float(a.p_raw*b.p_raw)
        rows.append({'Pick 1':f'{a.player} {a.stat} {a.side} {a.line:g}', 'Pick 2':f'{b.player} {b.stat} {b.side} {b.line:g}',
                     'Estimated all-hit %':round(prob*100,2),'Pick 1 %':round(a.p_raw*100,2),'Pick 2 %':round(b.p_raw*100,2),
                     'offer_id_1':a.offer_id,'offer_id_2':b.offer_id})
    return pd.DataFrame(rows).sort_values('Estimated all-hit %',ascending=False).head(max_entries) if rows else pd.DataFrame()

def backtest(history,offers,min_games=12,min_prob=.75):
    """Walk-forward: each prediction uses only historical observations before as_of. Offers require actual outcomes."""
    o=offers.copy()
    if 'actual' not in o.columns:raise ValueError('Historical offers require actual column')
    pred=predict(history,o,min_games=min_games)
    if pred.empty:return pd.DataFrame(),pd.DataFrame()
    pred['result']=[outcome(r.actual,r.line,r.side) for _,r in pred.iterrows()]
    pred=pred[pred.result.isin(['WIN','LOSS'])].copy()
    pred['hit']=(pred.result=='WIN').astype(int)
    pred['brier']=(pred.p_raw-pred.hit)**2
    # Construct only from same as_of batch. Entries can share picks; dependence matters.
    entries=[]
    for stamp,g in pred.groupby('as_of'):
        candidates=evaluate(g,min_prob=min_prob,max_entries=1)
        if candidates.empty:continue
        c=candidates.iloc[0]
        a=g[g.offer_id==c.offer_id_1].iloc[0];b=g[g.offer_id==c.offer_id_2].iloc[0]
        entries.append({'as_of':stamp,'entry_win':int(a.hit==1 and b.hit==1),'estimated_probability':c['Estimated all-hit %']/100,
                        'pick_1':c['Pick 1'],'pick_2':c['Pick 2']})
    return pred,pd.DataFrame(entries)

def wilson_interval(wins,n,z=1.96):
    if n==0:return (float('nan'),float('nan'))
    p=wins/n; d=1+z*z/n
    center=(p+z*z/(2*n))/d
    spread=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return max(0,center-spread),min(1,center+spread)
