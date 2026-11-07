"""Optional edge-quality research screen. Does not alter predictions, history, or billing."""
import math

DEFAULTS = {'min_ev_percent': 3.0, 'max_hold_percent': 6.0,
            'min_american_odds': -140, 'max_american_odds': 130,
            'min_independent_books': 2, 'min_audit_sample': 250}

def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (ValueError, TypeError):
        return None

def screen(inputs):
    """Missing verification produces an unknown/withheld status, never approval."""
    checks = []
    def add(name, status, detail):
        checks.append({'name': name, 'status': status, 'detail': detail})
    ev = _number(inputs.get('net_ev_percent'))
    add('Calibrated net EV', 'pass' if ev is not None and ev >= 3 else 'fail' if ev is not None else 'unknown',
        'Requires calibrated payout-adjusted EV of at least +3%. Raw model confidence is not EV.')
    hold = _number(inputs.get('market_hold_percent'))
    add('Market hold', 'pass' if hold is not None and 0 <= hold <= 6 else 'fail' if hold is not None else 'unknown',
        'Requires a valid two-sided market hold no higher than 6%. Not directly applicable to fantasy entries.')
    odds = _number(inputs.get('american_odds'))
    add('Price range', 'pass' if odds is not None and (odds <= -100 or odds >= 100) and -140 <= odds <= 130 else 'fail' if odds is not None else 'unknown',
        'Research preference -140 to +130; not a guaranteed advantage.')
    books = _number(inputs.get('independent_books'))
    add('Independent book agreement', 'pass' if books is not None and books >= 2 else 'fail' if books is not None else 'unknown',
        'Requires two independent, timely sources with comparable lines.')
    availability = inputs.get('availability_confirmed')
    add('Official lineup and injury confirmation', 'pass' if availability is True else 'fail' if availability is False else 'unknown',
        'Questionable/GTD or unverified lineup means withhold.')
    calibrated = inputs.get('calibration_verified')
    add('Prospective calibration', 'pass' if calibrated is True else 'fail' if calibrated is False else 'unknown',
        'Calibrated out-of-sample probabilities required; no accuracy promise.')
    return {'mode':'optional_add_on','status':'screen_passed' if all(c['status']=='pass' for c in checks) else 'withheld',
            'checks':checks,'defaults':DEFAULTS,
            'note':'This is a separate research screen, not a bet trigger. Original predictions and saved records are unchanged. Track CLV and 250–500 settled picks before major model revisions.'}
