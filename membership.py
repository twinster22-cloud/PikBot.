import hashlib
import json
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps
from html import escape
from threading import BoundedSemaphore, Thread
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo
import time

import psycopg
import requests
import stripe
from flask import Blueprint, abort, current_app, g, jsonify, redirect, render_template, request, session
from itsdangerous import BadSignature, URLSafeTimedSerializer
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

membership = Blueprint('membership', __name__)
DUMMY_HASH = generate_password_hash(secrets.token_urlsafe(32))
RESET_EMAIL_SLOTS = BoundedSemaphore(4)
RESET_LINK_SECONDS = 30 * 60
LOGGER = logging.getLogger(__name__)


def database():
    return psycopg.connect(os.environ['DATABASE_URL'], row_factory=dict_row)


def init_accounts(app):
    app.config.update(
        SECRET_KEY=os.environ['PIKBOT_SESSION_SECRET'],
        SESSION_COOKIE_SECURE=True,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=7),
        SESSION_REFRESH_EACH_REQUEST=False,
        MAX_CONTENT_LENGTH=4_000_000,
        MAX_FORM_MEMORY_SIZE=128_000,
        MAX_FORM_PARTS=20,
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
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_member_pick_boards (
            email TEXT NOT NULL REFERENCES pikbot_accounts(email) ON DELETE CASCADE,
            pick_day DATE NOT NULL, event_id TEXT NOT NULL, payload JSONB NOT NULL,
            PRIMARY KEY (email, pick_day, event_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_history_cache (
            cache_key TEXT PRIMARY KEY, payload JSONB NOT NULL,
            expires TIMESTAMPTZ NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_complimentary_plus (
            email TEXT PRIMARY KEY REFERENCES pikbot_accounts(email) ON DELETE CASCADE,
            granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
        # One-time owner-requested complimentary Plus grant; no charge or owner privileges.
        conn.execute("""INSERT INTO pikbot_complimentary_plus(email)
            SELECT email FROM pikbot_accounts
            WHERE lower(email) = %s
            ON CONFLICT (email) DO NOTHING""", ('ddburley@icloud.com',))
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_hold TEXT')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_status TEXT')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_checked_at TIMESTAMPTZ')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_mode BOOLEAN')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS password_version INTEGER NOT NULL DEFAULT 0')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS owner_recovery_used TEXT')
        conn.execute("""CREATE TABLE IF NOT EXISTS pikbot_billing_events (
            event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )""")
    app.register_blueprint(membership)


def account():
    email = session.get('email')
    if email:
        with database() as conn:
            record = conn.execute('SELECT * FROM pikbot_accounts WHERE email=%s', (email,)).fetchone()
        if record and record.get('password_version', 0) == session.get('password_version', 0):
            return record
        session.clear()
    return None


def owner_account(record):
    configured = os.getenv('PIKBOT_PAID_TEST_EMAIL_SHA256', '')
    digest = hashlib.sha256(record['email'].strip().lower().encode()).hexdigest()
    return bool(configured) and secrets.compare_digest(configured, digest)


def private_paid_test(record):
    code = os.getenv('PIKBOT_SIGNUP_TEST_CODE', '')
    issued = session.get('owner_authenticated_at')
    bound_code = session.get('owner_code_digest', '')
    return (session.get('owner_test') is True and owner_account(record)
            and isinstance(issued, (int, float)) and 0 <= time.time() - issued < 7200
            and bool(code) and secrets.compare_digest(
                bound_code, hashlib.sha256(code.encode('utf-8')).hexdigest()))


def complimentary_plus(record):
    with database() as conn:
        return conn.execute('SELECT 1 FROM pikbot_complimentary_plus WHERE email=%s',
                            (record['email'],)).fetchone() is not None


def access(record):
    if complimentary_plus(record):
        return 'subscriber'
    # Private, explicitly authorized complimentary subscriber account.
    configured = os.getenv('PIKBOT_COMPLIMENTARY_EMAIL_SHA256', '').strip().lower()
    digest = hashlib.sha256(record['email'].strip().lower().encode()).hexdigest()
    if len(configured) == 64 and secrets.compare_digest(configured, digest):
        return 'subscriber'
    if private_paid_test(record):
        return 'subscriber'
    if record.get('billing_hold'):
        return 'payment_required'
    paid = subscription_access(record.get('customer_id'))
    if paid is not None:
        return paid
    if record['trial_ends'] > datetime.now(timezone.utc):
        return 'trial'
    return 'expired'


def subscription_access(customer_id):
    """Stripe, rather than a browser redirect or event order, decides payment state."""
    if customer_id:
        if not os.getenv('STRIPE_SECRET_KEY') or not os.getenv('STRIPE_PRICE_ID'):
            return 'unavailable'
        try:
            client = stripe_client()
            subscriptions = client.v1.subscriptions.list({
                'customer': customer_id, 'status': 'all',
                'expand': ['data.latest_invoice'],
            })
            payment_required = False
            for sub in subscriptions.auto_paging_iter():
                sub = stripe_data(sub)
                if not matches_price(sub):
                    continue
                invoice = sub.get('latest_invoice')
                if isinstance(invoice, str):
                    invoice = stripe_data(client.v1.invoices.retrieve(invoice))
                if (sub.get('status') == 'active' and not sub.get('pause_collection')
                        and invoice and invoice.get('status') == 'paid'):
                    return 'subscriber'
                # A failed, pending, paused or ended membership cannot fall back to a trial.
                payment_required = True
            if payment_required:
                return 'payment_required'
        except stripe.StripeError:
            return 'unavailable'
    return None


def stripe_client():
    return stripe.StripeClient(os.environ['STRIPE_SECRET_KEY'])


def billing_ready():
    key = os.getenv('STRIPE_SECRET_KEY', '')
    mode = os.getenv('PIKBOT_BILLING_MODE', 'live')
    prefix = ('sk_live_', 'rk_live_') if mode == 'live' else ('sk_test_', 'rk_test_')
    return (mode in {'live', 'test'} and os.getenv('PIKBOT_CHECKOUT_ENABLED') == '1' and key.startswith(prefix)
            and all(os.getenv(name) for name in
                    ('STRIPE_PRICE_ID', 'STRIPE_WEBHOOK_SECRET', 'PIKBOT_PUBLIC_URL')))


def csrf_token():
    return session.setdefault('csrf', secrets.token_urlsafe(32))


def check_csrf():
    expected = session.get('csrf')
    if not expected or not secrets.compare_digest(request.form.get('csrf', '').encode('utf-8'), expected.encode('utf-8')):
        abort(400)


def allow_attempt(email, purpose='login'):
    limits = (60, 12) if purpose == 'login' else (30, 3)
    prefix = '' if purpose == 'login' else purpose + ':'
    with database() as conn:
        conn.execute('DELETE FROM pikbot_login_attempts WHERE expires <= NOW()')
        for bucket, limit in (('ip:' + str(request.remote_addr), limits[0]), ('email:' + email[:254], limits[1])):
            row = conn.execute(
                """INSERT INTO pikbot_login_attempts VALUES (%s, 1, NOW() + INTERVAL '15 minutes')
                   ON CONFLICT (bucket) DO UPDATE SET attempts = pikbot_login_attempts.attempts + 1
                   RETURNING attempts""", (prefix + bucket,),
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
                record = conn.execute('SELECT email, password_hash, password_version FROM pikbot_accounts WHERE email=%s', (email,)).fetchone()
            valid = check_password_hash(record['password_hash'] if record else DUMMY_HASH, password)
            if owner_mode:
                code = os.getenv('PIKBOT_SIGNUP_TEST_CODE', '')
                supplied = request.form.get('test_code', '').strip()
                valid_code = bool(code) and secrets.compare_digest(supplied.encode('utf-8'), code.encode('utf-8'))
                if record and valid and owner_account(record) and valid_code:
                    response = sign_in(email, record.get('password_version', 0))
                    session['owner_test'] = True
                    session['owner_authenticated_at'] = time.time()
                    session['owner_code_digest'] = hashlib.sha256(code.encode('utf-8')).hexdigest()
                    response.headers['Location'] = '/owner'
                    return response
            elif record and valid:
                return sign_in(email, record.get('password_version', 0))
            error = 'Login details were incorrect.'
    return render_template('login.html', owner_mode=owner_mode, csrf=csrf_token(), error=error), 400 if error else 200


def reset_email_config():
    origin = os.getenv('PIKBOT_PUBLIC_URL', '').rstrip('/')
    api_key = os.getenv('PIKBOT_BREVO_API_KEY')
    sender = os.getenv('PIKBOT_RESET_FROM_EMAIL')
    parsed = urlsplit(origin)
    if (api_key and sender and parsed.scheme == 'https' and parsed.hostname
            and not (parsed.username or parsed.path or parsed.query or parsed.fragment)):
        return origin, api_key, sender
    return None


def send_password_reset(email, serializer, origin, api_key, sender, owner_email=False):
    try:
        with database() as conn:
            record = conn.execute(
                'SELECT email, password_version FROM pikbot_accounts WHERE email=%s', (email,),
            ).fetchone()
        if not record or owner_account(record) != owner_email:
            return
        token = serializer.dumps([email, record['password_version']])
        reset_path = '/owner/reset-password/email' if owner_email else '/reset-password'
        reset_page = origin + reset_path
        reset_link = f'{reset_page}#{token}'
        expiration = f'This reset expires in {RESET_LINK_SECONDS // 60} minutes.'
        ignore = 'If you did not request this, you can ignore this email.'
        response = requests.post(
            'https://api.brevo.com/v3/smtp/email', headers={'api-key': api_key},
            json={'sender': {'email': sender, 'name': 'PikBot'}, 'to': [{'email': email}],
                  'subject': 'Reset your PikBot password',
                  'textContent': f'Reset your PikBot password\n\n{reset_link}\n\n{expiration}\n\n'
                                 f'If the link will not open, go to {reset_page} and paste the reset code below.\n\n'
                                 f'{token}\n\n{ignore}',
                  'htmlContent': '<html><body><h2>Reset your PikBot password</h2>'
                                 f'<p><a href="{escape(reset_link)}" style="display:inline-block;'
                                 'background:#136d3a;color:#fff;padding:12px 18px;border-radius:6px;'
                                 'font-weight:bold">Reset password</a></p>'
                                 f'<p>{expiration}</p>'
                                 f'<p>If the button will not open, go to {escape(reset_page)} '
                                 'and paste the reset code below.</p>'
                                 f'<p style="overflow-wrap:anywhere;word-break:break-all"><code>{escape(token)}</code></p>'
                                 f'<p>{ignore}</p></body></html>'},
            timeout=(3, 8),
        )
        response.raise_for_status()
    except (requests.RequestException, psycopg.Error):
        LOGGER.warning('Password reset email could not be sent.')
    finally:
        RESET_EMAIL_SLOTS.release()


@membership.route('/owner/reset-password', methods=['GET', 'POST'])
def reset_owner_password():
    recovery_key = os.getenv('PIKBOT_OWNER_RECOVERY_KEY', '')
    owner_code = os.getenv('PIKBOT_SIGNUP_TEST_CODE', '')
    enabled = len(recovery_key) >= 32 and bool(owner_code)
    error = None
    if request.method == 'POST' and enabled:
        check_csrf()
        email = request.form.get('email', '').strip().lower()
        password = request.form.get('password', '')
        if not email or len(email) > 254 or not allow_attempt(email, 'owner-recovery'):
            error = 'Unable to reset the owner password. Check your details or try again later.'
        elif not (
            owner_account({'email': email})
            and secrets.compare_digest(request.form.get('test_code', '').strip().encode(), owner_code.encode())
            and secrets.compare_digest(request.form.get('recovery_key', '').strip().encode(), recovery_key.encode())
        ):
            error = 'Unable to reset the owner password. Check your details or try again later.'
        elif not 12 <= len(password) <= 128 or password != request.form.get('confirmation'):
            error = 'Enter matching passwords of 12 to 128 characters.'
        else:
            digest = hashlib.sha256(recovery_key.encode()).hexdigest()
            with database() as conn:
                changed = conn.execute(
                    """UPDATE pikbot_accounts SET password_hash=%s, password_version=password_version+1,
                       owner_recovery_used=%s WHERE email=%s AND owner_recovery_used IS DISTINCT FROM %s
                       RETURNING email""",
                    (generate_password_hash(password), digest, email, digest),
                ).fetchone()
            if changed:
                session.clear()
                return redirect('/owner/login?password_reset=1', code=303)
            error = 'This recovery key has already been used or the reset could not be completed.'
    return render_template('login.html', reset_view='owner', csrf=csrf_token(), error=error,
                           owner_recovery_enabled=enabled)


@membership.route('/forgot-password', methods=['GET', 'POST'])
@membership.route('/owner/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    owner_email = request.path.startswith('/owner/')
    error = notice = None
    if request.method == 'POST':
        check_csrf()
        email = request.form.get('email', '').strip().lower()
        config = reset_email_config()
        if not config:
            error = 'Password reset email is temporarily unavailable. Please try again later.'
        else:
            notice = 'If that email matches an eligible account, PikBot will send a reset link and code.'
            if (len(email) <= 254 and re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email)
                    and allow_attempt(email, 'password-reset') and RESET_EMAIL_SLOTS.acquire(blocking=False)):
                serializer = password_reset_serializer(owner_email)
                try:
                    Thread(target=send_password_reset, args=(email, serializer, *config, owner_email),
                           daemon=True).start()
                except RuntimeError:
                    RESET_EMAIL_SLOTS.release()
                    LOGGER.warning('Password reset email worker could not start.')
    return render_template('login.html', reset_view='request', csrf=csrf_token(), error=error,
                           notice=notice, owner_email_reset=owner_email)


def password_reset_serializer(owner_email):
    salt = 'pikbot-owner-email-reset' if owner_email else 'pikbot-password-reset'
    return URLSafeTimedSerializer(current_app.secret_key, salt=salt)


@membership.route('/reset-password', methods=['GET', 'POST'])
@membership.route('/owner/reset-password/email', methods=['GET', 'POST'])
def reset_password():
    owner_email = request.path.startswith('/owner/')
    token = error = None
    if request.method == 'POST':
        check_csrf()
        submitted = request.form.get('token', '')
        try:
            if len(submitted) > 2048:
                raise BadSignature('Invalid reset token')
            token = ''.join(submitted.split())
            if token.startswith('https://'):
                pasted = urlsplit(token)
                token = pasted.fragment if pasted.path == request.path else ''
            if not token or len(token) > 1024:
                raise BadSignature('Invalid reset token')
            serializer = password_reset_serializer(owner_email)
            email, version = serializer.loads(token, max_age=RESET_LINK_SECONDS)
            if owner_account({'email': email}) != owner_email:
                raise BadSignature('Invalid reset account')
        except (BadSignature, ValueError):
            error = 'This reset link or code is invalid or expired. Request a new reset email.'
            token = None
        else:
            password = request.form.get('password', '')
            owner_code = os.getenv('PIKBOT_SIGNUP_TEST_CODE', '')
            if owner_email and not (
                allow_attempt(email, 'owner-email-reset') and owner_code
                and secrets.compare_digest(request.form.get('test_code', '').strip().encode(), owner_code.encode())
            ):
                error = 'Unable to verify the owner code. Check it or try again later.'
            elif not 12 <= len(password) <= 128 or password != request.form.get('confirmation'):
                error = 'Enter matching passwords of 12 to 128 characters.'
            else:
                with database() as conn:
                    changed = conn.execute(
                        """UPDATE pikbot_accounts SET password_hash=%s, password_version=password_version+1
                           WHERE email=%s AND password_version=%s RETURNING email""",
                        (generate_password_hash(password), email, version),
                    ).fetchone()
                if changed:
                    session.clear()
                    login_path = '/owner/login' if owner_email else '/login'
                    return redirect(f'{login_path}?password_reset=1', code=303)
                error = 'This reset link or code has expired or already been used. Request a new reset email.'
                token = None
    return render_template('login.html', reset_view='change', csrf=csrf_token(), error=error,
                           token=token, owner_email_reset=owner_email)


@membership.after_request
def prevent_auth_caching(response):
    response.headers['Cache-Control'] = 'no-store'
    return response


def sign_in(email, password_version=0):
    session.clear()
    session['email'] = email
    session['password_version'] = password_version
    session.permanent = True
    return redirect('/')


@membership.post('/logout')
@membership.post('/owner/logout')
def logout():
    check_csrf()
    session.clear()
    return redirect('/owner/login' if request.path == '/owner/logout' else '/login')


def broadcast_entitlement(record):
    if complimentary_plus(record):
        return 'plus'
    """Broadcast access is owner-only or backed by a paid Plus price."""
    if private_paid_test(record):
        return 'owner'
    price_id = os.getenv('STRIPE_PLUS_PRICE_ID', '').strip()
    customer_id = record.get('customer_id')
    if not price_id or not customer_id or not os.getenv('STRIPE_SECRET_KEY'):
        return None
    try:
        client = stripe_client()
        subscriptions = client.v1.subscriptions.list({
            'customer': customer_id, 'status': 'all',
            'expand': ['data.latest_invoice'],
        })
        for item in subscriptions.auto_paging_iter():
            sub = stripe_data(item)
            if sub.get('status') != 'active' or sub.get('pause_collection'):
                continue
            items = sub.get('items', {}).get('data', [])
            if not any((stripe_data(i.get('price')).get('id') == price_id) for i in items):
                continue
            invoice = sub.get('latest_invoice')
            if isinstance(invoice, str):
                invoice = stripe_data(client.v1.invoices.retrieve(invoice))
            if invoice and invoice.get('status') == 'paid':
                return 'plus'
    except (stripe.StripeError, KeyError, TypeError, AttributeError):
        return None
    return None


@membership.get('/api/account')
def account_status():
    record = account()
    if not record:
        return jsonify(error='Sign in required'), 401
    is_owner = private_paid_test(record)
    return jsonify(
        role='owner' if is_owner else 'member',
        permissions={'owner_tools': is_owner, 'view_members': is_owner,
                     'view_finances': is_owner, 'issue_refunds': is_owner},
        private_paid_test=is_owner,
        broadcast_access=bool(broadcast_entitlement(record)),
        membership_tier='owner' if is_owner else ('plus' if broadcast_entitlement(record) == 'plus' else 'regular_or_trial'),
        access=access(record), csrf=csrf_token(), trial_ends=record['trial_ends'].isoformat(),
        billing_ready=billing_ready(),
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


def owner_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        record = account()
        if not record:
            return jsonify(error='Sign in required'), 401
        if not private_paid_test(record):
            return jsonify(error='Private owner login required.'), 403
        return fn(*args, **kwargs)
    return wrapped


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
        g.member_email = record['email']
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
    if action == 'checkout' and os.getenv('PIKBOT_CHECKOUT_ENABLED') != '1':
        return jsonify(error='Checkout is closed during launch testing.'), 503
    if action == 'checkout' and not billing_ready():
        return jsonify(error='Verified payment configuration is required before checkout opens.'), 503
    # Existing customers must be able to manage or cancel even when new checkout is disabled.
    required = ('STRIPE_SECRET_KEY', 'PIKBOT_PUBLIC_URL') if action == 'portal' else (
        'STRIPE_SECRET_KEY', 'STRIPE_PRICE_ID', 'STRIPE_WEBHOOK_SECRET', 'PIKBOT_PUBLIC_URL')
    if not all(os.getenv(key) for key in required):
        return jsonify(error='Billing management is not configured.'), 503
    client = stripe_client()
    origin = os.environ['PIKBOT_PUBLIC_URL'].rstrip('/')
    try:
        if action == 'portal':
            if not record['customer_id']:
                return jsonify(error='No billing account found.'), 404
            result = client.v1.billing_portal.sessions.create({'customer': record['customer_id'], 'return_url': origin + '/'})
            return redirect(result.url, code=303)

        price = client.v1.prices.retrieve(os.environ['STRIPE_PRICE_ID'])
        if not (bool(price.livemode) == os.environ['STRIPE_SECRET_KEY'].startswith(('sk_live_', 'rk_live_'))
                and price.active and price.currency == 'usd' and price.unit_amount == 1500
                and price.recurring and price.recurring.interval == 'month'
                and price.recurring.interval_count == 1):
            return jsonify(error='The configured price must be $15 USD monthly.'), 503
        with database() as conn:
            row = conn.execute('SELECT customer_id FROM pikbot_accounts WHERE email=%s FOR UPDATE', (record['email'],)).fetchone()
            customer_id = row['customer_id']
            if not customer_id:
                customer = client.v1.customers.create({'email': record['email']},
                    options={'idempotency_key': 'pikbot-customer-' + hashlib.sha256(record['email'].encode()).hexdigest()})
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
                'integration_identifier': 'pikbot_' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz') for _ in range(8)),
                'line_items': [{'price': price.id, 'quantity': 1}],
                'success_url': origin + '/', 'cancel_url': origin + '/',
            })
        return redirect(result.url, code=303)
    except stripe.StripeError:
        return jsonify(error='Stripe billing is temporarily unavailable.'), 502


WEBHOOK_EVENTS = {
    'customer.subscription.created', 'customer.subscription.updated',
    'customer.subscription.deleted', 'customer.subscription.paused',
    'customer.subscription.resumed', 'invoice.paid', 'invoice.payment_failed',
    'invoice.payment_action_required', 'checkout.session.completed',
    'checkout.session.async_payment_succeeded', 'checkout.session.async_payment_failed',
    'charge.refunded', 'charge.dispute.created', 'radar.early_fraud_warning.created',
}


def stripe_id(value):
    value = stripe_data(value)
    return value.get('id') if isinstance(value, dict) else value


def stripe_data(value):
    # Stripe SDK 15 objects deliberately no longer implement dict.get().
    return value.to_dict() if hasattr(value, 'to_dict') else value


def matches_price(sub):
    sub = stripe_data(sub)
    return any(item['price']['id'] == os.environ['STRIPE_PRICE_ID']
               for item in sub.get('items', {}).get('data', []))


def risk_customers(event_type, obj, client):
    """Resolve affected subscriptions through Stripe's invoice-payment object graph."""
    if event_type == 'charge.refunded' and not obj.get('refunded'):
        return set()  # Partial refunds do not automatically suspend a membership.
    intent = stripe_id(obj.get('payment_intent'))
    if not intent:
        charge = obj if event_type == 'charge.refunded' else stripe_data(client.v1.charges.retrieve(stripe_id(obj['charge'])))
        intent = stripe_id(charge.get('payment_intent'))
    if not intent:
        return set()
    payments = client.v1.invoice_payments.list({
        'payment': {'type': 'payment_intent', 'payment_intent': intent}, 'status': 'paid',
    })
    customers = set()
    for payment in payments.auto_paging_iter():
        invoice = stripe_data(client.v1.invoices.retrieve(stripe_id(payment['invoice'])))
        parent = invoice.get('parent') or {}
        subscription_id = stripe_id((parent.get('subscription_details') or {}).get('subscription'))
        subscription_id = subscription_id or stripe_id(invoice.get('subscription'))
        if not subscription_id:
            continue
        sub = stripe_data(client.v1.subscriptions.retrieve(subscription_id))
        if matches_price(sub):
            customers.add(stripe_id(sub['customer']))
    return customers


@membership.post('/billing/webhook')
def stripe_webhook():
    signing_secret = os.getenv('STRIPE_WEBHOOK_SECRET')
    key = os.getenv('STRIPE_SECRET_KEY', '')
    if not signing_secret or not key or not os.getenv('STRIPE_PRICE_ID'):
        return jsonify(error='Webhook is not configured.'), 503
    try:
        event = stripe_data(stripe.Webhook.construct_event(request.get_data(),
                    request.headers.get('Stripe-Signature', ''), signing_secret))
    except (ValueError, stripe.SignatureVerificationError):
        return jsonify(error='Invalid webhook signature.'), 400
    if bool(event.get('livemode')) != key.startswith(('sk_live_', 'rk_live_')):
        return jsonify(error='Payment environment mismatch.'), 400
    event_type = event['type']
    if event_type not in WEBHOOK_EVENTS:
        return jsonify(received=True)
    obj = event['data']['object']
    try:
        client = stripe_client()
        risk = event_type in {'charge.refunded', 'charge.dispute.created', 'radar.early_fraud_warning.created'}
        customers = risk_customers(event_type, obj, client) if risk else {stripe_id(obj.get('customer'))}
        states = {customer: subscription_access(customer) for customer in customers if customer}
        if any(state == 'unavailable' for state in states.values()):
            return jsonify(error='Payment verification unavailable; retry delivery.'), 503
        with database() as conn:
            inserted = conn.execute('''INSERT INTO pikbot_billing_events (event_id,event_type)
                VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING event_id''',
                (event['id'], event_type)).fetchone()
            if inserted:
                for customer, state in states.items():
                    conn.execute('UPDATE pikbot_accounts SET billing_status=%s,billing_checked_at=NOW(),billing_mode=%s WHERE customer_id=%s',
                                 (state or 'payment_required', bool(event['livemode']), customer))
                    if risk:
                        conn.execute('UPDATE pikbot_accounts SET billing_hold=%s WHERE customer_id=%s',
                                     (event_type, customer))
        return jsonify(received=True)
    except (stripe.StripeError, psycopg.Error, KeyError, TypeError):
        return jsonify(error='Payment notification could not be processed; retry delivery.'), 503


@membership.get('/api/owner/launch-status')
@owner_required
def launch_status():
    key = os.getenv('STRIPE_SECRET_KEY', '')
    return jsonify(
        checkout_enabled=os.getenv('PIKBOT_CHECKOUT_ENABLED') == '1',
        live_key_configured=key.startswith(('sk_live_', 'rk_live_')),
        price_configured=bool(os.getenv('STRIPE_PRICE_ID')),
        webhook_configured=bool(os.getenv('STRIPE_WEBHOOK_SECRET')),
        billing_ready=billing_ready(),
    )


def trial_daily_board(payload=None):
    """Only the first successful three-pick board may be published each Eastern day."""
    email = session['email']
    day = datetime.now(ZoneInfo('America/New_York')).date()
    with database() as conn:
        if payload is not None:
            conn.execute('SELECT email FROM pikbot_accounts WHERE email=%s FOR UPDATE', (email,))
            conn.execute('DELETE FROM pikbot_trial_daily_board WHERE email=%s AND pick_day<%s',
                         (email, day - timedelta(days=30)))
            conn.execute('''INSERT INTO pikbot_trial_daily_board (email,pick_day,payload)
                            VALUES (%s,%s,%s::jsonb) ON CONFLICT DO NOTHING''',
                         (email, day, json.dumps(payload)))
        row = conn.execute('SELECT payload FROM pikbot_trial_daily_board WHERE email=%s AND pick_day=%s',
                           (email, day)).fetchone()
    return row['payload'] if row else None



@membership.post('/api/owner/complimentary-plus')
@owner_required
def grant_complimentary_plus():
    """Owner-authorized complimentary Plus grant; never grants owner privileges."""
    if not request.is_json:
        return jsonify(error='JSON body required'), 415
    email = str((request.get_json(silent=True) or {}).get('email', '')).strip().lower()
    if not re.fullmatch(r'[^@\\s]+@[^@\\s]+\\.[^@\\s]+', email):
        return jsonify(error='Valid email required'), 400
    with database() as conn:
        row = conn.execute('SELECT 1 FROM pikbot_accounts WHERE email=%s', (email,)).fetchone()
        if not row:
            return jsonify(error='User must create a PikBot account first'), 404
        conn.execute('INSERT INTO pikbot_complimentary_plus(email) VALUES (%s) ON CONFLICT DO NOTHING', (email,))
    return jsonify(ok=True, email=email, membership_tier='plus', complimentary=True)


