from functools import lru_cache
import requests
import secrets
from hashlib import sha256
import prediction_team
import prediction_audit
import market_math
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import today
import player_history
import history_loader
import pick_results
import psycopg
from player_photos import attach_photos
from membership import trial_daily_board, account, daily_trial_picks, init_accounts, premium_required, owner_required, check_csrf, database, private_paid_test, broadcast_entitlement
from flask import Flask, g, request, jsonify, send_from_directory, redirect, session
import pandas as pd
import numpy as np
from io import StringIO
from engine import predict,evaluate,backtest,wilson_interval
from pathlib import Path
from sportsdata import test_connection
from owner_dashboard import init_owner
ROOT=Path(__file__).parent
app=Flask(__name__, static_folder=None)
init_accounts(app)
init_owner(app)
prediction_audit.init_audit(app)

@app.get('/<asset>')
def public_asset(asset):
    if asset not in {
        'sw.js', 'manifest.webmanifest', 'icon-192.png', 'icon-512.png', 'apple-touch-icon.png',
        'splash.png', 'icon.svg', 'owner.webmanifest', 'owner-icon-192.png', 'owner-icon-512.png',
    }:
        return jsonify(error='Not found'), 404
    return send_from_directory(ROOT/'static', asset)

@app.after_request
def prevent_private_caching(response):
    if request.path in ('/', '/owner') or request.path.startswith(('/api/', '/login', '/owner/', '/billing/', '/logout')):
        response.headers['Cache-Control'] = 'no-store, private'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    response.headers['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=(), payment=()'
    nonce = secrets.token_urlsafe(24)
    if response.mimetype == 'text/html' and response.status_code == 200:
        response.direct_passthrough = False
        response.set_data(response.get_data(as_text=True).replace('<script>', '<script nonce="' + nonce + '">'))
        response.headers.pop('ETag', None)
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self' 'nonce-" + nonce + "'; "
        "style-src 'self' 'unsafe-inline'; img-src 'self' https: data:; "
        "connect-src 'self'; worker-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; "
        "form-action 'self' https://checkout.stripe.com https://billing.stripe.com"
    )
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


def read_history_cache(key):
    try:
        with database() as conn:
            row = conn.execute('SELECT payload FROM pikbot_history_cache WHERE cache_key=%s AND expires>NOW()', (key,)).fetchone()
        return history_loader.decode(row['payload']) if row else None
    except (psycopg.Error, ValueError, KeyError):
        app.logger.warning('History cache read unavailable')
        return None


def write_history_cache(key, frame):
    try:
        payload = history_loader.encode(frame)
        with database() as conn:
            conn.execute('DELETE FROM pikbot_history_cache WHERE expires<=NOW()')
            conn.execute('''INSERT INTO pikbot_history_cache VALUES (%s,%s::jsonb,NOW()+INTERVAL '6 hours')
                ON CONFLICT (cache_key) DO UPDATE SET payload=EXCLUDED.payload,expires=EXCLUDED.expires''', (key, payload))
    except (psycopg.Error, ValueError, TypeError):
        app.logger.warning('History cache write unavailable')

@app.get('/')
def index():
    record = account()
    if not record:
        return redirect('/login')
    if private_paid_test(record) and request.args.get('view') != 'picks':
        return redirect('/owner')
    return send_from_directory(ROOT/'static','index.html')

@app.get('/owner/picks')
def owner_picks():
    record = account()
    if not record or not private_paid_test(record):
        return redirect('/owner/login')
    return send_from_directory(ROOT/'static', 'index.html')


@app.post('/api/analyze')
@owner_required
def analyze_endpoint():
    check_csrf()
    run_backtest = request.form.get('backtest') == 'true'
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
        if not live or trial_live:
            picks = picks.head(60)
        result={'disclaimer':'Statistical estimates are uncalibrated and not guaranteed. Uploaded offers are not verified live lines.',
                'picks':records(picks[['sport','player','stat','line','side','p_raw','projection','games'] + (['event_id', 'p_push', 'recent_games', 'playing_time'] if live else [])] if not picks.empty else picks),
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
    if g.membership_access == 'trial':
        board = trial_daily_board()
        if board:
            return jsonify(error='Today’s saved trial selections were issued by the earlier unvalidated model. No team-approved replacements are available.'), 404
    sport = request.args.get('sport', '')
    if sport not in today.SPORTS:
        return jsonify(error='Choose a supported sport.'), 400
    bookmaker = (today.DEFAULT_BOOK if g.membership_access == 'trial'
                 else request.args.get('bookmaker', today.DEFAULT_BOOK))
    if bookmaker not in today.BOOKMAKERS:
        return jsonify(error='Choose a supported line source.'), 400
    book_label = today.BOOKMAKERS[bookmaker]
    directory = os.getenv('PIKBOT_LIVE_HISTORY_DIR')
    key, _ = today.SPORTS[sport]
    path = Path(directory) / (key + '.csv') if directory else None
    try:
        now = datetime.now(timezone.utc)
        available = today.offers(sport, now, event_id=request.args.get('event_id'), bookmaker=bookmaker,
                                 expanded=g.membership_access != 'trial')
        if not available:
            return jsonify(error=f'No fresh {book_label} lines are available for the covered game.'), 404
        offers = pd.DataFrame(available)
        history = (pd.read_csv(path) if path and path.is_file()
                   else history_loader.load(sport, offers, now, player_history.load_history,
                                            read_history_cache, write_history_cache))
        if not set(player_history.COLUMNS).issubset(history.columns):
            return jsonify(error='The player history has invalid columns.'), 503
    except today.FeedError as exc:
        return jsonify(error=str(exc)), 503
    except (OSError, ValueError, KeyError):
        return jsonify(error='Recent player stats could not be read.'), 503
    if offers.empty:
        return jsonify(error='No current lines match the available player stats.'), 404
    # Review the complete board before spending trial allowance or publishing selections.
    response = app.make_response(analyze(history, offers, False, live=True))
    if response.status_code != 200:
        return response
    if not response.is_json or not response.json.get('picks'):
        return jsonify(error='There is not enough matching recent player history.'), 404
    result = response.json
    stats_source = {'NBA': 'BALLDONTLIE', 'NFL': 'nflverse', 'COLLEGE FOOTBALL': 'CollegeFootballData', 'COLLEGE BASKETBALL': 'CollegeBasketballData'}.get(sport, '') if not (path and path.is_file()) else ''
    if not (path and path.is_file()) and sport in player_history.DUNK_LEAGUES and (os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')):
        stats_source = 'DunkAPI'
    if not (path and path.is_file()) and sport in player_history.ADDITIONAL_FEEDS:
        stats_source = player_history.ADDITIONAL_FEEDS[sport][0]
    stats_source = history.attrs.get('source', stats_source)
    if 'reserve' not in stats_source and g.membership_access != 'trial':
        attach_photos(result['picks'])
    for pick in result['picks']:
        if not pick.get('photo_url'):
            pick['photo_url'] = history.attrs.get('photos', {}).get(pick['player'])
    matchups = {game['id']: game for game in today.today_events(sport, now)}
    history_digest = sha256(history.to_csv(index=False).encode()).hexdigest()
    benchmarks = today.benchmark_lines(sport,result['picks'][0]['event_id'],now,
                                      expanded=g.membership_access != 'trial')
    for pick in result['picks']:
        game = matchups.get(pick['event_id'], {})
        pick['matchup'] = ' at '.join(filter(None, (game.get('away_team'), game.get('home_team'))))
        pick['starts_at'] = game.get('commence_time')
        pick['bookmaker'] = book_label
        offer = next((row for row in available if row['event_id'] == pick['event_id']
                      and row['player'] == pick['player'] and row['stat'] == pick['stat']
                      and row['line'] == pick['line']), {})
        pick['line_updated_at'] = offer.get('line_updated_at')
        pick['stats_source'] = stats_source
        pick['history_digest'] = history_digest
        pick['team_review'] = prediction_team.review(pick, offer, now)
        pick['market_review'] = market_math.comparison(pick,offer,
            benchmarks.get((pick['player'],pick['stat'],pick['line']),{}),now)
        pick['reason'] = pick_reason(pick)
    result['entries'] = []  # Unvalidated independence-based entries are research, not recommendations.
    result['team_version'] = prediction_team.MODEL_VERSION
    result['approved_count'] = sum(p['team_review']['approved'] for p in result['picks'])
    try:
        prediction_audit.archive(result['picks'], datetime.now(timezone.utc))
    except (psycopg.Error, ValueError):
        return jsonify(error='Predictions could not be recorded before game time. Please refresh the matchup.'), 503
    result['source'] = f'{book_label} via The Odds API' + (' · Stats: ' + stats_source if stats_source else '')
    result['disclaimer'] = f'Research projections for {book_label} lines. Team review is active; no picks are approved as validated recommendations. Raw scores are not verified win probabilities.'
    if 'SportsDataverse' in stats_source:
        result['disclaimer'] += ' Reserve stats: SportsDataverse contributors, CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/). Player game records transformed into PikBot estimates.'
    if g.membership_access == 'trial':
        result['picks'] = [p for p in result['picks'] if p['team_review']['approved']][:3]
        if not result['picks']:
            return jsonify(error='No team-approved picks are available. PikBot will not force daily recommendations while lineup checks and accuracy validation are incomplete.'), 404
        result['entries'] = []
        result['daily_board'] = True
        result['selected_at'] = now.isoformat()
        result['disclaimer'] += ' Your fixed daily selections. Lines and estimates were captured at the selection time; no additional trial picks are revealed today.'
        result = trial_daily_board(trial_public_board(result))
        result = trial_public_board(result)
    else:
        try:
            save_member_pick_board(result['picks'], now, bookmaker)
        except psycopg.Error:
            app.logger.warning('Could not save the live pick board for results.')
            return jsonify(error='Pick history is temporarily unavailable. Please try again shortly.'), 503
    return jsonify(result)



@app.post('/api/research/edge-screen')
@premium_required
def optional_edge_screen():
    """Separate, non-persistent manual research check; does not issue picks."""
    import edge_guardrails
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify(error='Provide a JSON object.'), 400
    if len(str(payload)) > 4096:
        return jsonify(error='Input too large.'), 400
    return jsonify(edge_guardrails.screen(payload))



def gamecast_plus_required(fn):
    """GameCast is limited to verified owner and paid Plus accounts."""
    from functools import wraps
    @wraps(fn)
    def wrapped(*args, **kwargs):
        record = account()
        if not record:
            return jsonify(error='Sign in required.'), 401
        if not broadcast_entitlement(record):
            return jsonify(error='GameCast requires PikBot Plus or owner access.'), 403
        return fn(*args, **kwargs)
    return wrapped


@app.get('/api/providers/basketball/status')
@owner_required
def basketball_provider_status():
    """Check BasketballAPI credentials without exposing the secret or consuming game quota."""
    key = os.getenv('BASKETBALL_API_KEY', '').strip()
    if not key:
        return jsonify(configured=False, connected=False,
                       message='BASKETBALL_API_KEY is missing in Render.'), 503
    try:
        response = requests.get(
            'https://api.basketballapi.net/v1/usage',
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
            timeout=8,
        )
        if response.status_code in (401, 403):
            return jsonify(configured=True, connected=False,
                           message='Provider rejected the API key.'), 502
        if not response.ok:
            return jsonify(configured=True, connected=False,
                           message='Provider returned HTTP ' + str(response.status_code)), 502
        payload = response.json().get('data') or {}
        return jsonify(configured=True, connected=True,
                       plan=payload.get('plan'), remaining=payload.get('remaining'),
                       calls_today=payload.get('calls_today'),
                       message='BasketballAPI authentication succeeded.')
    except (requests.RequestException, ValueError, TypeError, AttributeError):
        return jsonify(configured=True, connected=False,
                       message='BasketballAPI is temporarily unreachable.'), 503


@app.get('/api/live/games')
@gamecast_plus_required
def live_games_browser():
    """Observed ESPN schedule and scores, including yesterday's finished games."""
    from zoneinfo import ZoneInfo
    from datetime import date as date_type
    sport = request.args.get('sport', 'NBA').upper()
    if sport not in pick_results.LEAGUES:
        return jsonify(error='Unsupported sport.'), 400
    day = request.args.get('day', 'today')
    if day not in ('today', 'yesterday'):
        return jsonify(error='Choose today or yesterday.'), 400
    local = datetime.now(ZoneInfo('America/New_York')).date()
    if day == 'yesterday':
        local -= timedelta(days=1)
    try:
        league = pick_results.LEAGUES[sport][0]
        events = pick_results.scoreboard(league, local.strftime('%Y%m%d'), int(datetime.now(timezone.utc).timestamp())//60)
        games = []
        for event in events:
            competition = (event.get('competitions') or [{}])[0]
            competitors = competition.get('competitors') or []
            teams = [{'name': c.get('team',{}).get('displayName','Unknown'), 'score':c.get('score'),
                      'home_away':c.get('homeAway')} for c in competitors]
            state = event.get('status',{}).get('type',{})
            games.append({'id':str(event.get('id','')), 'name':event.get('name','Game'),
                          'starts_at':event.get('date'), 'status':state.get('description','Scheduled'),
                          'state':state.get('state','pre'), 'teams':teams,
                          'official_recap':next((link.get('href') for link in event.get('links',[])
                             if str(link.get('href','')).startswith('https://www.espn.com/')),None)})
        return jsonify(sport=sport,day=day,games=games)
    except (requests.RequestException,ValueError,KeyError,TypeError,AttributeError):
        return jsonify(error='Live scoreboard provider is temporarily unavailable.'), 503


@app.get('/api/live/gamecast')
@gamecast_plus_required
def live_gamecast():
    """Observed game events from ESPN summary; no video or inferred tracking coordinates."""
    sport = request.args.get('sport', '').upper()
    event_id = request.args.get('event_id', '')
    if sport not in pick_results.LEAGUES or not event_id.isdigit() or len(event_id) > 20:
        return jsonify(error='Invalid GameCast selection.'), 400
    try:
        league = pick_results.LEAGUES[sport][0]
        summary = pick_results.fetch(league, 'summary', event=event_id)
        header = (summary.get('header', {}).get('competitions') or [{}])[0]
        status = header.get('status', {})
        competitors = header.get('competitors') or []
        teams = [{'name': t.get('team', {}).get('displayName', 'Team'),
                  'score': t.get('score'), 'home_away': t.get('homeAway')}
                 for t in competitors]
        raw = summary.get('plays') or []
        if isinstance(raw, dict):
            raw = raw.get('items') or raw.get('plays') or []
        plays = []
        for play in raw[-80:]:
            if not isinstance(play, dict):
                continue
            clock = play.get('clock') or {}
            period = play.get('period') or {}
            text_value = play.get('text') or play.get('shortText') or ''
            if not text_value:
                continue
            # Coordinates are optional provider facts, never estimated from play text.
            coord = play.get('coordinate') or play.get('coordinates') or {}
            point = None
            if isinstance(coord, dict):
                try:
                    x, y = float(coord.get('x')), float(coord.get('y'))
                    if 0 <= x <= 100 and 0 <= y <= 100:
                        point = {'x': x, 'y': y}
                except (TypeError, ValueError):
                    pass
            plays.append({'coordinate': point, 'id': str(play.get('id', ''))[:80],
                          'text': str(text_value)[:450],
                          'clock': clock.get('displayValue') if isinstance(clock, dict) else '',
                          'period': period.get('number') if isinstance(period, dict) else period,
                          'scoring': bool(play.get('scoringPlay'))})
        return jsonify(sport=sport, event_id=event_id, teams=teams,
                       status=status.get('type', {}).get('description', 'Unknown') if isinstance(status.get('type'), dict) else status.get('displayClock', 'Unknown'),
                       clock=status.get('displayClock'), period=status.get('period'),
                       plays=plays[::-1], observed_at=datetime.now(timezone.utc).isoformat(),
                       note='Provider-reported updates; timing and coverage vary. Coordinates shown only when explicitly supplied by provider.')
    except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, IndexError):
        return jsonify(error='GameCast provider is temporarily unavailable.'), 503


@app.get('/api/live/game-stats')
@gamecast_plus_required
def live_game_stats_browser():
    sport = request.args.get('sport','').upper()
    event_id = request.args.get('event_id','')
    if sport not in pick_results.LEAGUES or not event_id.isdigit() or len(event_id)>20:
        return jsonify(error='Invalid game selection.'),400
    try:
        league, supported = pick_results.LEAGUES[sport]
        summary = pick_results.fetch(league,'summary',event=event_id)
        competitions = summary.get('header',{}).get('competitions',[])
        if len(competitions)!=1:
            return jsonify(error='Box score not available.'),404
        game = competitions[0]
        box = summary.get('boxscore',{}).get('players',[])
        names = sorted({r.get('athlete',{}).get('displayName') for team in box
                        for group in team.get('statistics',[])
                        for r in group.get('athletes',[])
                        if r.get('athlete',{}).get('displayName')})
        players = []
        for name in names:
            stats = {stat:pick_results.actual_stat({'sport':sport,'stat':stat,'player':name},box)
                     for stat in supported}
            stats = {key:value for key,value in stats.items() if value is not None}
            if stats:
                players.append({'player':name,'stats':stats})
        competitors=game.get('competitors',[])
        return jsonify(status=game.get('status',{}).get('type',{}).get('description','Unknown'),
                       teams=[{'team':t.get('team',{}).get('displayName','Team'),'score':t.get('score')} for t in competitors],
                       players=players,observed_at=datetime.now(timezone.utc).isoformat(),
                       note='Verified provider box score. Stats may lag live play.')
    except (requests.RequestException,ValueError,KeyError,TypeError,AttributeError):
        return jsonify(error='Box score provider unavailable.'),503


@app.get('/api/today/performance')
@premium_required
def current_performance():
    if g.membership_access == 'trial':
        return jsonify(error='Live performance research requires paid membership.'), 403
    sport = request.args.get('sport', '')
    if sport not in today.SPORTS:
        return jsonify(error='Choose a supported sport.'), 400
    try:
        now = datetime.now(timezone.utc)
        game = next((r for r in today.today_events(sport,now)
                     if r['id'] == request.args.get('event_id')), None)
        if not game:
            return jsonify(error='Choose a matchup from today’s schedule.'), 404
        matchup = ' at '.join((game['away_team'],game['home_team']))
        data = pick_results.live_performance(sport,matchup,game['commence_time'],int(now.timestamp())//30)
        if not data:
            return jsonify(error='A unique current box score is not available for this matchup.'), 404
        return jsonify(data)
    except (today.FeedError,requests.RequestException,ValueError,KeyError,TypeError):
        return jsonify(error='Current performance data is unavailable. No live pick has been generated.'), 503


PICK_SNAPSHOT_FIELDS = {'sport', 'player', 'stat', 'line', 'side', 'event_id', 'matchup', 'starts_at'}
# Repeated views of an identical prediction share one result.
PICK_IDENTITY_SQL = """jsonb_build_array(
    pick->>'sport', pick->>'event_id', COALESCE(pick->>'bookmaker', 'PrizePicks'),
    pick->>'player', pick->>'stat', pick->'line', pick->>'side'
)"""


def save_member_pick_board(picks, now, bookmaker=today.DEFAULT_BOOK):
    event_id = picks[0]['event_id']
    if not event_id or not today.timestamp(picks[0].get('starts_at')):
        return
    email = g.member_email
    day = now.astimezone(pick_results.EASTERN).date()
    fields = PICK_SNAPSHOT_FIELDS | {'bookmaker', 'p_raw', 'team_review', 'line_updated_at', 'market_review'}
    board_key = event_id if bookmaker == today.DEFAULT_BOOK else f'{event_id}:{bookmaker}'
    payload = {'picks': [{key: value for key, value in pick.items() if key in fields}
                         for pick in picks if pick['event_id'] == event_id]}
    with database() as conn:
        conn.execute('DELETE FROM pikbot_member_pick_boards WHERE email=%s AND pick_day<%s',
                     (email, day - timedelta(days=30)))
        conn.execute(f"""INSERT INTO pikbot_member_pick_boards AS board (email, pick_day, event_id, payload)
            VALUES (%s,%s,%s,%s)
            ON CONFLICT (email, pick_day, event_id) DO UPDATE
            SET payload = jsonb_set(board.payload, '{{picks}}', (
                SELECT jsonb_agg(pick ORDER BY pick_order)
                FROM (
                    SELECT DISTINCT ON ({PICK_IDENTITY_SQL}) pick, pick_order
                    FROM jsonb_array_elements((board.payload->'picks') || (EXCLUDED.payload->'picks'))
                    WITH ORDINALITY AS saved(pick, pick_order)
                    ORDER BY {PICK_IDENTITY_SQL}, (pick ? 'p_raw') DESC, pick_order
                ) unique_picks
            ))
            WHERE NOT ((board.payload->'picks') @> (EXCLUDED.payload->'picks'))""",
                     (email, day, board_key, psycopg.types.json.Jsonb(payload)))


@app.get('/api/owner/pick-results')
@owner_required
def previous_pick_results():
    page = request.args.get('page', 0, type=int)
    if not 0 <= page <= (2**63 - 1) // pick_results.PAGE_SIZE:
        return jsonify(error='Invalid results page.'), 400
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=2)
    first_day = cutoff.astimezone(pick_results.EASTERN).date()
    with database() as conn:
        rows = conn.execute(f"""WITH boards AS (
                SELECT pick_day, payload FROM pikbot_trial_daily_board WHERE pick_day >= %s
                UNION ALL
                SELECT pick_day, payload FROM pikbot_member_pick_boards WHERE pick_day >= %s
            ), candidates AS (
                SELECT pick, pick_day, pick_order, {PICK_IDENTITY_SQL} AS identity,
                       NULLIF(pick->>'starts_at', '')::timestamptz AS starts_at
                FROM boards
                CROSS JOIN LATERAL jsonb_array_elements(payload->'picks')
                    WITH ORDINALITY AS saved(pick, pick_order)
            ), unique_picks AS (
                SELECT DISTINCT ON (identity) pick, identity, starts_at
                FROM candidates WHERE starts_at BETWEEN %s AND %s
                ORDER BY identity, (pick ? 'p_raw') DESC, pick_day, pick_order, pick
            )
            SELECT pick FROM unique_picks ORDER BY starts_at DESC, identity
            LIMIT %s OFFSET %s""", (first_day, first_day, cutoff, now,
                                    pick_results.PAGE_SIZE + 1, page * pick_results.PAGE_SIZE)).fetchall()
    picks = [row['pick'] for row in rows[:pick_results.PAGE_SIZE]]
    return jsonify(picks=pick_results.grade(picks, now), has_more=len(rows) > pick_results.PAGE_SIZE)


def trial_public_board(board):
    return {
        'picks': [{key: value for key, value in pick.items() if key in PICK_SNAPSHOT_FIELDS}
                  for pick in board.get('picks', [])[:3]],
        'entries': [], 'daily_board': True, 'selected_at': board.get('selected_at'),
        'source': 'PikBot daily selections',
        'disclaimer': 'Your daily trial selections. Lines are captured at selection time. Full analysis is available with paid access.',
    }


def pick_reason(pick):
    line, projection = float(pick['line']), float(pick['projection'])
    side = pick['side']
    games = pick.get('recent_games') or []
    hits = sum(float(game['value']) > line if side == 'MORE' else float(game['value']) < line for game in games)
    comparison = 'above' if projection > line else 'below' if projection < line else 'at'
    text = (f"{side} {line:g} {pick['stat']}: the weighted model projects {projection:.1f}, "
            f"{abs(projection-line):.1f} {comparison} the listed line, using {pick['games']} completed games.")
    if games:
        text += f" In the last {len(games)} games, {hits} finished on this side of that line."
    playing_time = pick.get('playing_time')
    if playing_time:
        text += (f" Playing time averaged {playing_time['recent_average']:.1f} minutes over the last three games. "
                 f"The model estimates {playing_time['projected']:.1f} minutes.")
        if playing_time['adjusted']:
            text += " The stat projection adjusts for the recent change in minutes."
    return text + ' Research only. Team review withholds recommendation approval because confirmed injuries, lineups, and forward accuracy validation are missing.'


@app.get('/api/odds-feed-check')
@owner_required
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
@owner_required
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
@owner_required
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
@owner_required
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
@owner_required
def next_projection_check():
    sport = request.args.get('sport', 'NBA')
    if sport not in today.SPORTS:
        return jsonify(error='Choose a supported sport.'), 400
    return jsonify(check_next_projection(sport, today.bucket(60)))


@lru_cache(maxsize=4)
def check_additional_history(sport, _day):
    name = {'NHL': 'Connor McDavid', 'MLB': 'Bryce Harper'}[sport]
    try:
        history = player_history.additional_history(
            sport, pd.DataFrame({'player': [name]}), datetime.now(timezone.utc))
        return {'verified': not history.empty, 'records': len(history),
                'stats': sorted(history['stat'].unique().tolist())}
    except today.FeedError as exc:
        return {'verified': False, 'reason': str(exc)}


@app.get('/api/additional-feed-check')
@owner_required
def additional_feed_check():
    sport = request.args.get('sport', '')
    if sport not in player_history.ADDITIONAL_FEEDS:
        return jsonify(error='Choose NHL or MLB.'), 400
    return jsonify(check_additional_history(sport, datetime.now(timezone.utc).date().isoformat()))


@app.get('/health')
def health():return jsonify(status='ok')
@app.get('/api/sportsdata-test')
@owner_required
def sportsdata_test():
    return jsonify(test_connection())
@app.get('/api/live-props-status')
@owner_required
def live_props_status():
    # Configuration is not evidence that a provider has passed a live test.
    configured = {
        'NBA': bool(os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY') or os.getenv('BALLDONTLIE_API_KEY')),
        'WNBA': bool(os.getenv('DUNKAPI_API_KEY') or os.getenv('DUNKAPI_KEY')),
        'NFL': True,
        'NHL': bool(os.getenv('PUCKAPI_API_KEY')),
        'MLB': bool(os.getenv('FASTBALLAPI_API_KEY')),
        'COLLEGE BASKETBALL': bool(os.getenv('COLLEGE_DATA_API_KEY')),
        'COLLEGE FOOTBALL': bool(os.getenv('COLLEGE_DATA_API_KEY')),
    }
    return jsonify(
        odds_configured=bool(os.getenv('ODDS_API_KEY')),
        history_configured=configured,
        verified=False,
        reason='Configuration only. Current lines and matching recent player history must be verified per matchup.',
    )

if __name__=='__main__':app.run(host='0.0.0.0',port=int(__import__('os').environ.get('PORT',5000)),debug=False)


