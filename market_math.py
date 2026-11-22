"""Exact-line, two-sided market estimates. Not a source of true probabilities."""
import math
from today import timestamp

DFS = {'PrizePicks','Underdog Fantasy','DraftKings Pick6'}


def decimal_odds(value, fmt='decimal'):
    if isinstance(value, bool) or not isinstance(value, (float,int)) or not math.isfinite(value):
        raise ValueError('Odds must be finite numbers.')
    if fmt == 'american':
        if abs(value) < 100:
            raise ValueError('American odds must have magnitude at least 100.')
        return 1 + (value/100 if value > 0 else 100/abs(value))
    if fmt != 'decimal' or value <= 1:
        raise ValueError('Decimal odds must exceed 1.')
    return float(value)


def devig(more, less, fmt='decimal'):
    over, under = 1/decimal_odds(more,fmt), 1/decimal_odds(less,fmt)
    total = over+under
    return {'MORE':over/total,'LESS':under/total,'overround':total-1}


def entry_ev(probabilities, payouts):
    """Return net EV per unit under independence and no pushes/voids.

    Payout multipliers are TOTAL returns, including original stake. Caller must
    enter the exact displayed entry table. No defaults for platform payouts.
    """
    if not 1 <= len(probabilities) <= 6:
        raise ValueError('Enter 1–6 probabilities.')
    distribution = [1.]
    for p in probabilities:
        if isinstance(p,bool) or not isinstance(p,(int,float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError('Each probability must be between 0 and 1.')
        next_distribution = [0.]*(len(distribution)+1)
        for wins, mass in enumerate(distribution):
            next_distribution[wins] += mass*(1-p)
            next_distribution[wins+1] += mass*p
        distribution = next_distribution
    for k,v in payouts.items():
        if not isinstance(k,int) or not 0 <= k <= len(probabilities) or isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0 <= v <= 10000:
            raise ValueError('Invalid total-return payout table.')
    return sum(distribution[k]*v for k,v in payouts.items())-1


def comparison(pick, offer, benchmark, now):
    updated = timestamp(benchmark.get('last_update'))
    target_updated = timestamp(offer.get('line_updated_at'))
    if (not updated or not target_updated or not 0 <= (now-updated).total_seconds() <= 900
            or not 0 <= (now-target_updated).total_seconds() <= 900
            or abs((updated-target_updated).total_seconds()) > 300):
        return {'available':False,'note':'No fresh exact-line two-sided benchmark was returned.'}
    try:
        fair = devig(benchmark.get('MORE'),benchmark.get('LESS'))
    except ValueError:
        return {'available':False,'note':'Both valid benchmark prices are required.'}
    p = fair[pick['side']]
    result = {'available':True,'reference':'Pinnacle','method':'Proportional two-sided devig',
              'reference_probability':p,'overround':fair['overround'],
              'updated_at':updated.isoformat(),'net_ev_per_unit':None,
              'note':'Market-implied estimate, not true odds. Same player, event, stat and line only. Settlement-rule equivalence is unverified.'}
    # DFS odds are indicative, not single-leg payouts. Do not fabricate a per-leg EV.
    if pick.get('bookmaker') not in DFS and not float(pick['line']).is_integer():
        try:
            d = decimal_odds(offer.get('prices',{}).get(pick['side']))
            result['net_ev_per_unit'] = p*d-1
            result['note'] += ' Estimated EV assumes equivalent settlement rules and accurate benchmark probabilities; not an approved recommendation.'
        except ValueError:
            pass
    return result


def sportsbook_kelly(probability, odds, fmt='decimal', fraction=0.25, cap=0.02):
    """Conservative fractional Kelly; model probability is an estimate, not truth."""
    if isinstance(probability,bool) or not isinstance(probability,(int,float)) or not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError('Probability must be between zero and one.')
    if not 0 <= fraction <= 1 or not 0 <= cap <= 1:
        raise ValueError('Invalid fractional Kelly settings.')
    b = decimal_odds(odds,fmt)-1
    full = max(0., (b*probability-(1-probability))/b)
    return {'full_kelly':full,'fractional_kelly':min(cap,full*fraction),
            'expected_value':probability*(b+1)-1,
            'note':'Estimated, not guaranteed. Default quarter Kelly capped at 2%.'}


def flex_kelly(probabilities, payouts, fraction=0.25, cap=0.02):
    """Optimize expected log growth across complete DFS return outcomes.

    Assumes independent legs, no pushes or voids, and verified total-return
    multipliers for every winning-leg count. Not valid for correlated slips.
    """
    if not 0 <= fraction <= 1 or not 0 <= cap <= 1:
        raise ValueError('Invalid fractional Kelly settings.')
    # Validate inputs using the existing payout and probability checks.
    ev = entry_ev(probabilities,payouts)
    n=len(probabilities)
    if any(k not in payouts for k in range(n+1)):
        raise ValueError('Complete payout table required, including zero-return outcomes.')
    distribution=[1.]
    for p in probabilities:
        nxt=[0.]*(len(distribution)+1)
        for k,mass in enumerate(distribution):
            nxt[k]+=mass*(1-p)
            nxt[k+1]+=mass*p
        distribution=nxt
    if ev <= 0:
        return {'full_kelly':0.,'fractional_kelly':0.,'expected_value':ev,
                'note':'No positive estimated edge; no stake indicated.'}
    def derivative(f):
        return sum(mass*(payouts[k]-1)/(1+f*(payouts[k]-1))
                   for k,mass in enumerate(distribution))
    # Keep a buffer away from total ruin for zero-return outcomes.
    lo,hi=0.,1.-1e-10
    if derivative(hi)>0:
        full=hi
    else:
        for _ in range(75):
            mid=(lo+hi)/2
            if derivative(mid)>0:lo=mid
            else:hi=mid
        full=(lo+hi)/2
    return {'full_kelly':full,'fractional_kelly':min(cap,full*fraction),
            'expected_value':ev,
            'note':'Assumes independent legs and exact payout table; correlated entries require a joint-outcome model.'}
