"""Auditable statistical reviewers, not independent AI models or win guarantees."""
from datetime import datetime, timezone
import math

MODEL_VERSION = 'team-review-v1'
SPORT_CONTEXT = {
    'NBA':'confirmed lineups, injuries, teammate usage splits, pace and rest',
    'WNBA':'confirmed lineups, injuries, teammate usage splits and rest',
    'NFL':'injuries, confirmed roles, snap/target shares and weather',
    'COLLEGE FOOTBALL':'confirmed roster, injuries, opportunity shares and weather',
    'COLLEGE BASKETBALL':'confirmed roster, injuries, rotation and pace',
    'MLB':'confirmed batting order, starting pitcher, platoon splits and park/weather',
    'NHL':'confirmed goalie, line assignments, special teams and rest',
}


def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def review(pick, offer, now):
    """All checks share historical data; agreement is not extra probability."""
    checks = []

    def check(name, passed, detail):
        checks.append({'reviewer': name, 'passed': bool(passed), 'detail': detail})

    updated, start = stamp(offer.get('line_updated_at')), stamp(offer.get('starts_at'))
    fresh = updated and 0 <= (now - updated).total_seconds() <= 900
    check('Data reviewer', fresh and start and start > now,
          'Requires a source line updated within 15 minutes and a game not yet started.')
    check('Projection reviewer', math.isfinite(pick['p_raw']) and pick['p_raw'] >= .75
          and pick['games'] >= 20,
          'Requires 20 completed games and an experimental model score of at least 0.75.')
    games = pick.get('recent_games') or []
    values = [g['value'] for g in games if isinstance(g.get('value'), (float, int))
              and math.isfinite(g['value']) and g['value'] >= 0]
    hits = sum(v > pick['line'] if pick['side'] == 'MORE' else v < pick['line'] for v in values)
    check('Trend reviewer', len(values) >= 10 and hits >= 7,
          f'{hits} of {len(values)} recent games finished on this side of the current line. This is historical frequency, not future probability.')
    minutes = pick.get('playing_time')
    check('Playing-time reviewer', minutes and minutes.get('stable')
          and minutes.get('projected', 0) >= 15,
          'Requires available, stable recent playing time. Missing opportunity data blocks approval in every sport.')
    # No current lineup feed or calibrated forward test exists. Never fabricate approval.
    check('Availability reviewer', False,
          'Missing verified '+SPORT_CONTEXT.get(pick['sport'],'current player availability')+'; recommendation withheld.')
    check('Validation reviewer', False,
          'This model version has no validated 75% forward-test claim; recommendation withheld.')
    return {'version': MODEL_VERSION, 'status': 'research_only', 'approved': False,
            'screened': all(c['passed'] for c in checks[:4]), 'checks': checks,
            'note': 'Rule-based specialists share data. Their agreement does not prove a pick will pass.'}


def accuracy_summary(rows):
    """Summarize every logged row, including pending, pushes and voids."""
    from engine import wilson_interval

    def summarize(group):
        wins = sum(r.get('result') == 'WIN' for r in group)
        losses = sum(r.get('result') == 'LOSS' for r in group)
        settled = wins + losses
        scored = [r for r in group if r.get('result') in {'WIN', 'LOSS'}
                  and isinstance(r.get('p_raw'), (int, float)) and math.isfinite(r['p_raw'])]
        lo, hi = wilson_interval(wins, settled)
        return {'logged': len(group), 'wins': wins, 'losses': losses,
                'pending': sum(r.get('result', 'PENDING') == 'PENDING' for r in group),
                'pushes': sum(r.get('result') == 'PUSH' for r in group),
                'voids': sum(r.get('result') == 'VOID' for r in group),
                'win_rate': round(100 * wins / settled, 2) if settled else None,
                'interval': [round(100 * lo, 2), round(100 * hi, 2)] if settled else None,
                'brier': round(sum((r['p_raw'] - (r['result'] == 'WIN')) ** 2 for r in scored) / len(scored), 4) if scored else None}

    bins = []
    for low, high in [(0, .6), (.6, .7), (.7, .8), (.8, .9), (.9, 1.01)]:
        group = [r for r in rows if isinstance(r.get('p_raw'), (int, float)) and low <= r['p_raw'] < high]
        bins.append({'label': f'{int(low*100)}–{min(100,int(high*100))}% raw score', **summarize(group)})
    return {'overall': summarize(rows), 'score_bands': bins,
            'sports': [{'sport': sport, **summarize([r for r in rows if r['sport'] == sport])}
                       for sport in sorted({r['sport'] for r in rows})],
            'versions': [{'version': version, **summarize([r for r in rows if r['model_version'] == version])}
                         for version in sorted({r['model_version'] for r in rows})],
            'claim_supported': False,
            'note': 'Prospective research records only, starting with this release. Repeated views count once. Different lines and sources can be correlated. Intervals assume independence and may be too narrow. No 75% accuracy claim is validated.'}
