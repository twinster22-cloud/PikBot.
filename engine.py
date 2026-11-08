import itertools
import math
import numpy as np
import pandas as pd
from scipy.stats import norm
from scipy.special import ndtr

KEYS=['sport','player','stat']

def weighted_recent(values):
    return .2 * np.mean(values[-5:]) + .3 * np.mean(values[-10:]) + .5 * np.mean(values)


def nba_playing_time(history):
    if 'minutes' not in history:
        return None
    minutes = pd.to_numeric(history['minutes'].tail(50), errors='coerce').to_numpy(dtype=float)
    if not np.isfinite(minutes).all() or (minutes <= 0).any():
        return None
    baseline = weighted_recent(minutes)
    recent = np.mean(minutes[-3:])
    projected = .7 * recent + .3 * np.mean(minutes[-10:])
    return {
        'baseline': float(baseline),
        'recent_average': float(recent),
        'projected': float(projected),
        'adjusted': bool(abs(recent - baseline) >= 5),
        'stable': bool(np.std(minutes[-5:]) / max(np.mean(minutes[-5:]), 1) <= .25
                       and abs(recent - baseline) / max(baseline, 1) <= .2),
    }


BASKETBALL_COMBOS = {
    'PRA': ['PTS', 'REB', 'AST'],
    'PR': ['PTS', 'REB'],
    'PA': ['PTS', 'AST'],
    'RA': ['REB', 'AST'],
}


def add_nba_combo_history(history, offers):
    requested = set(offers.loc[offers['sport'] == 'NBA', 'stat']) & BASKETBALL_COMBOS.keys()
    if not requested:
        return history
    keys = ['sport', 'player', 'date']
    base = history.loc[(history['sport'] == 'NBA') & history['stat'].isin(['PTS', 'REB', 'AST'])]
    base = base.loc[base['value'].ge(0) & np.isfinite(base['value'])].dropna(subset=keys)
    base = base.drop_duplicates(keys + ['stat'], keep=False)
    values = base.pivot(index=keys, columns='stat', values='value')
    additions = []
    for stat, components in BASKETBALL_COMBOS.items():
        if stat not in requested or not set(components).issubset(values.columns):
            continue
        totals = values[components].sum(axis=1, min_count=len(components)).dropna().rename('value')
        existing = history.loc[history['stat'].eq(stat), keys]
        totals = totals.loc[~totals.index.isin(pd.MultiIndex.from_frame(existing))]
        if totals.empty:
            continue
        rows = totals.reset_index().assign(stat=stat)
        if 'minutes' in base.columns:
            minutes = base.loc[base['stat'].eq(components[0]), keys + ['minutes']]
            rows = rows.merge(minutes, on=keys, how='left', validate='one_to_one')
        additions.append(rows)
    return pd.concat([history, *additions], ignore_index=True) if additions else history


def predict(history, offers, min_games=12, max_age_days=30, *, include_history=False):
    """Baseline predictions. Never use records on/after the offer's as_of timestamp."""
    h=history.copy(); o=offers.copy()
    for col in ['date']:
        h[col]=pd.to_datetime(h[col],errors='coerce',utc=True)
    o['as_of']=pd.to_datetime(o['as_of'],errors='coerce',utc=True)
    h['value']=pd.to_numeric(h['value'],errors='coerce'); o['line']=pd.to_numeric(o['line'],errors='coerce')
    if include_history:
        h = add_nba_combo_history(h, o)
    output=[]
    for _,r in o.iterrows():
        if pd.isna(r['as_of']) or pd.isna(r['line']) or r['line']<0: continue
        s=h.copy()
        for k in KEYS:s=s[s[k]==r[k]]
        # Date-only box scores lack completion timestamps. Exclude the entire offer day.
        s=s[(s['date']<r['as_of'].normalize()) & (s['date']>=r['as_of']-pd.Timedelta(days=365))].dropna(subset=['value','date']).sort_values('date')
        s=s.drop_duplicates(['date','value'])
        if s['date'].duplicated().any():continue
        x=s['value'].tail(50).to_numpy(float)
        if len(x)<min_games or np.any(x<0):continue
        if (r['as_of']-s['date'].max()).days>max_age_days:continue
        baseline = weighted_recent(x)
        mu = baseline
        playing_time = nba_playing_time(s) if include_history and r['sport'] == 'NBA' else None
        if playing_time and playing_time['adjusted']:
            minutes_adjusted = mu * playing_time['projected'] / playing_time['baseline']
            mu = (mu + minutes_adjusted) / 2
        sd=max(float(np.std(x,ddof=1)),.2*max(mu,1),1)
        line=float(r['line'])
        integer=np.allclose(x,np.round(x))
        if integer:
            more_cutoff = math.floor(line) + .5
            less_cutoff = math.ceil(line) - .5
            more = norm.sf((more_cutoff - mu) / sd)
            less = norm.cdf((less_cutoff - mu) / sd)
            if include_history and r['sport'] == 'NBA' and r['stat'] in {'PTS', 'REB', 'AST'}:
                # Align historical results with the current playing-time projection.
                adjusted_history = x + (mu - baseline)
                more = .75 * more + .25 * weighted_recent(ndtr((adjusted_history - more_cutoff) / .8))
                less = .75 * less + .25 * weighted_recent(ndtr((less_cutoff - adjusted_history) / .8))
            push=max(0,1-more-less)
        else:
            more=norm.sf((line-mu)/sd);less=1-more;push=0.
        # Conservative finite-sample adjustment: raw normal-tail estimates from
        # 12-50 games can be badly overconfident. Pull estimates toward 50%.
        # This is a heuristic, NOT empirically calibrated win probability.
        reliability = min(1.0, len(x) / 50.0)
        more = .5 + (float(more) - .5) * reliability
        less = .5 + (float(less) - .5) * reliability
        recent = ({'recent_games': [
            {'date': date.date().isoformat(), 'value': float(value)}
            for date, value in s[['date', 'value']].tail(10).itertuples(index=False, name=None)
        ], 'playing_time': playing_time} if include_history else {})
        for side,p in [('MORE',more),('LESS',less)]:
            output.append({**recent, **{k:r[k] for k in KEYS},'line':line,'as_of':r['as_of'], 'event_id':str(r.get('event_id','')),
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
                     'offer_id_1':a.offer_id,'offer_id_2':b.offer_id,'side_1':a.side,'side_2':b.side})
    return pd.DataFrame(rows).sort_values('Estimated all-hit %',ascending=False).head(max_entries) if rows else pd.DataFrame()

def backtest(history,offers,min_games=12,min_prob=.75):
    """Walk-forward: each prediction uses only historical observations before as_of. Offers require actual outcomes."""
    o=offers.copy()
    if 'actual' not in o.columns:raise ValueError('Historical offers require actual column')
    pred=predict(history,o,min_games=min_games,include_history=True)
    if pred.empty:return pd.DataFrame(),pd.DataFrame()
    # Select the side before looking at actual results; never score both sides as picks.
    pred=pred.sort_values('p_raw',ascending=False).drop_duplicates(['offer_id','as_of'])
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
        a=g[(g.offer_id==c.offer_id_1)&(g.side==c.side_1)].iloc[0]
        b=g[(g.offer_id==c.offer_id_2)&(g.side==c.side_2)].iloc[0]
        entries.append({'as_of':stamp,'entry_win':int(a.hit==1 and b.hit==1),'estimated_probability':c['Estimated all-hit %']/100,
                        'pick_1':c['Pick 1'],'pick_2':c['Pick 2']})
    return pred,pd.DataFrame(entries)

def wilson_interval(wins,n,z=1.96):
    if n==0:return (float('nan'),float('nan'))
    p=wins/n; d=1+z*z/n
    center=(p+z*z/(2*n))/d
    spread=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/d
    return max(0,center-spread),min(1,center+spread)

