import hashlib
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from zoneinfo import ZoneInfo

import psycopg
import stripe
from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

membership = Blueprint('membership', __name__)
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))


def database():
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row)


def init_accounts(app):
    app.config.update(
        SECRET_KEY=os.environ['PIKBOT_SESSION_SECRET'],
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
    )
    with database() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_accounts (
            email TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
            trial_ends TIMESTAMPTZ NOT NULL, customer_id TEXT UNIQUE
        )""")
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_login_attempts (
            bucket TEXT PRIMARY KEY, attempts INTEGER NOT NULL, expires TIMESTAMPTZ NOT NULL
        )''')
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_trial_picks (
            email TEXT NOT NULL REFERENCES pikbot_accounts(email) ON DELETE CASCADE,
            pick_day DATE NOT NULL,
            pick_key TEXT NOT NULL,
            PRIMARY KEY (email, pick_day, pick_key)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_trial_daily_board (
            email TEXT NOT NULL REFERENCES pikbot_accounts(email) ON DELETE CASCADE,
            pick_day DATE NOT NULL, payload JSONB NOT NULL,
            PRIMARY KEY (email, pick_day)
        )""")
    app.register_blueprint(membership)


def account():
    email = session.get('email')
    if email:
        with database() as conn:
            return conn.execute('SELECT * FROM pikbot_accounts WHERE email=%s', (email,)).fetchone()
    return None


def owner_account(record):
    configured = os.getenv('PIKBOT_PAID_TEST_EMAIL_SHA256', '')
    digest = hashlib.sha256(record['email'].strip().lower().encode()).hexdigest()
    return bool(configured) and secrets.compare_digest(configured, digest)


def private_paid_test(record):
    return session.get('owner_test') is True and owner_account(record)


def access(record):
    if private_paid_test(record):
        return 'subscriber'
    if record['customer_id']:
        if not os.getenv('STRIPE_SECRET_KEY') or not os.getenv('STRIPE_PRICE_ID'):
            return 'unavailable'
        try:
            client = stripe_client()
            subscriptions = client.v1.subscriptions.list({
                'customer': record['customer_id'], 'status': 'all',
                'expand': ['data.latest_invoice'],
            })
            payment_required = False
            for sub in subscriptions.auto_paging_iter():
                if not any(item.price.id == os.environ['STRIPE_PRICE_ID'] for item in sub['items'].data):
                    continue
                invoice = sub.get('latest_invoice')
                if isinstance(invoice, str):
                    invoice = client.v1.invoices.retrieve(invoice)
                if (sub.status == 'active' and not sub.get('pause_collection')
                        and invoice and invoice.get('status') == 'paid'):
                    return 'subscriber'
                # A failed, pending, paused or ended membership cannot fall back to a trial.
                payment_required = True
            if payment_required:
                return 'payment_required'
        except stripe.StripeError:
            return 'unavailable'
    if record['trial_ends'] > datetime.now(timezone.utc):
        return 'trial'
    return 'expired'


def stripe_client():
    return stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])


def csrf_token():
    return session.setdefault('csrf', secrets.token_urlsafe(32))


def check_csrf():
    expected = session.get('csrf')
    if not expected or not secrets.compare_digest(request.form.get('csrf', ''), expected):
        abort(400)


def allow_attempt(email):
    with database() as conn:
        conn.execute('DELETE FROM pikbot_login_attempts WHERE expires <= NOW()')
        for bucket, limit in (('ip:' + str(request.remote_addr), 60), ('email:' + email[:254], 12)):
            row = conn.execute(
                """INSERT INTO pikbot_login_attempts VALUES (%s, 1, NOW() + INTERVAL '15 minutes')
                   ON CONFLICT (bucket) DO UPDATE SET attempts = pikbot_login_attempts.attempts + 1
                   RETURNING attempts""", (bucket,),
            ).fetchone()
            if row['attempts'] > limit:
                return False
    return True


@membership.route('/login', methods=['GET', 'POST'])
@membership.route('/owner/login', methods=['GET', 'POST'])
def login():
    owner_mode = request.path == '/owner/login'
    error = None
    if request.method == 'POST':
        check_csrf()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not allow_attempt(email):
            return render_template('login.html', owner_mode=owner_mode, csrf=csrf_token(),
                                   error='Too many attempts. Try again in 15 minutes.'), 429
        if len(email) > 254 or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email) or not 12 <= len(password) <= 128:
            error = 'Enter a valid email and a password of 12 to 128 characters.'
        elif request.form.get('action') == 'register' and not owner_mode:
            with database() as conn:
                row = conn.execute(
                    """INSERT INTO pikbot_accounts (email, password_hash, trial_ends)
                       VALUES (%s, %s, NOW() + INTERVAL '7 days')
                       ON CONFLICT DO NOTHING RETURNING email""",
                    (email, generate_password_hash(password)),
                ).fetchone()
            if row:
                return sign_in(email)
            error = 'Unable to create an account. Try signing in.'
        else:
            with database() as conn:
                record = conn.execute('SELECT email, password_hash FROM pikbot_accounts WHERE email=%s', (email,)).fetchone()
            valid = check_password_hash(record['password_hash'] if record else DUMMY_HASH, password)
            if owner_mode:
                code = os.getenv('PIKBOT_SIGNUP_TEST_CODE', '')
                supplied = request.form.get('test_code', '').strip()
                valid_code = bool(code) and secrets.compare_digest(supplied.encode('utf-8'), code.encode('utf-8'))
                if record and valid and owner_account(record) and valid_code:
                    response = sign_in(email)
                    session['owner_test'] = True
                    return response
            elif record and valid:
                return sign_in(email)
            error = 'Login details were incorrect.'
    return render_template('login.html', owner_mode=owner_mode, csrf=csrf_token(), error=error), 400 if error else 200


@membership.after_request
def prevent_auth_caching(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def sign_in(email):
    session.clear()
    session['email'] = email
    session.permanent = True
    return redirect('/')


@membership.post('/logout')
def logout():
    check_csrf()
    session.clear()
    return redirect('/login')


@membership.get('/api/account')
def account_status():
    record = account()
    if not record:
        return jsonify(error='Sign in required'), 401
    return jsonify(
        private_paid_test=private_paid_test(record),
        access=access(record), csrf=csrf_token(), trial_ends=record['trial_ends'].isoformat(),
        billing_ready=os.getenv('PIKBOT_CHECKOUT_ENABLED') == '1' and all(os.getenv(key) for key in ('STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'PIKBOT_PUBLIC_URL')),
        has_customer=bool(record['customer_id']),
    )


def daily_trial_picks(predictions):
    email = session['email']
    day = datetime.now(ZoneInfo('America/New_York')).date()
    columns = ['sport', 'event_id', 'player', 'stat']
    ranked = predictions.sort_values('p_raw', ascending=False).drop_duplicates(columns)
    keys = [json.dumps(list(row), ensure_ascii=False)
            for row in ranked[columns].itertuples(index=False, name=None)]
    with database() as conn:
        conn.execute('SELECT email FROM pikbot_accounts WHERE email=%s FOR UPDATE', (email,))
        conn.execute('DELETE FROM pikbot_trial_picks WHERE email=%s AND pick_day<%s', (email, day))
        rows = conn.execute('SELECT pick_key FROM pikbot_trial_picks WHERE email=%s AND pick_day=%s',
                            (email, day)).fetchall()
        seen = {row['pick_key'] for row in rows}
        new = [key for key in keys if key not in seen][:max(0, 3 - len(seen))]
        for key in new:
            conn.execute('INSERT INTO pikbot_trial_picks VALUES (%s, %s, %s)', (email, day, key))
        seen.update(new)
    return ranked.loc[[key in seen for key in keys]]


def premium_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        record = account()
        if not record:
            return jsonify(error='Sign in required'), 401
        level = access(record)
        if level == 'unavailable':
            return jsonify(error='Subscription verification is temporarily unavailable.'), 503
        if level in {'expired', 'payment_required'}:
            return jsonify(error='No picks available. A successful subscription payment is required to unlock unlimited picks.', access=level), 402
        g.membership_access = level
        return fn(*args, **kwargs)
    return wrapped


@membership.post('/billing/<action>')
def billing(action):
    check_csrf()
    record = account()
    if not record:
        return jsonify(error='Sign in required'), 401
    if action not in {'checkout', 'portal'}:
        abort(404)
    if os.getenv('PIKBOT_CHECKOUT_ENABLED') != '1':
        return jsonify(error='Checkout is closed during launch testing.'), 503
    if not all(os.getenv(key) for key in ('STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'PIKBOT_PUBLIC_URL')):
        return jsonify(error='Billing is not configured.'), 503
    client = stripe_client()
    origin = os.environ['PIKBOT_PUBLIC_URL'].rstrip('/')
    try:
        if action == 'portal':
            if not record['customer_id']:
                return jsonify(error='No billing account found.'), 404
            result = client.v1.billing_portal.sessions.create({'customer': record['customer_id'], 'return_url': origin + '/'})
            return redirect(result.url, code=303)

        price = client.v1.prices.retrieve(os.environ['STRIPE_PRICE_ID'])
        if not (price.active and price.currency == 'usd' and price.unit_amount == 1500
                and price.recurring and price.recurring.interval == 'month'
                and price.recurring.interval_count == 1):
            return jsonify(error='The configured price must be $15 USD monthly.'), 503
        with database() as conn:
            row = conn.execute('SELECT customer_id FROM pikbot_accounts WHERE email=%s FOR UPDATE', (record['email'],)).fetchone()
            customer_id = row['customer_id']
            if not customer_id:
                customer = client.v1.customers.create({'email': record['email']})
                customer_id = customer.id
                conn.execute('UPDATE pikbot_accounts SET customer_id=%s WHERE email=%s', (customer_id, record['email']))
        with database() as conn:
            conn.execute('SELECT email FROM pikbot_accounts WHERE email=%s FOR UPDATE', (record['email'],))
            subscriptions = client.v1.subscriptions.list({'customer': customer_id})
            if any(sub.status in {'active', 'trialing', 'past_due', 'unpaid', 'paused', 'incomplete'}
                   for sub in subscriptions.auto_paging_iter()):
                return jsonify(error='Manage your existing subscription in billing.'), 409
            sessions = client.v1.checkout.sessions.list({'customer': customer_id, 'status': 'open', 'limit': 1})
            if sessions.data:
                return redirect(sessions.data[0].url, code=303)
            result = client.v1.checkout.sessions.create({
                'mode': 'subscription', 'customer': customer_id,
                'line_items': [{'price': price.id, 'quantity': 1}],
                'success_url': origin + '/', 'cancel_url': origin + '/',
            })
        return redirect(result.url, code=303)
    except stripe.StripeError:
        return jsonify(error='Stripe billing is temporarily unavailable.'), 502


def trial_daily_board(payload=None):
    """Only the first successful three-pick board may be published each Eastern day."""
    email = session['email']
    day = datetime.now(ZoneInfo('America/New_York')).date()
    with database() as conn:
        if payload is not None:
            conn.execute('SELECT email FROM pikbot_accounts WHERE email=%s FOR UPDATE', (email,))
            conn.execute('DELETE FROM pikbot_trial_daily_board WHERE email=%s AND pick_day<%s', (email, day))
            conn.execute('''INSERT INTO pikbot_trial_daily_board (email,pick_day,payload)
                            VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING''',
                         (email, day, json.dumps(payload)))
        row = conn.execute('SELECT payload FROM pikbot_trial_daily_board WHERE email=%s AND pick_day=%s',
                           (email, day)).fetchone()
    return row['payload'] if row else None
