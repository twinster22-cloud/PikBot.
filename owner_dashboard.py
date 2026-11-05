"""Private business dashboard. No member-facing route grants business permissions."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import secrets
import time

import psycopg
import stripe
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from membership import (account, database, owner_required, private_paid_test, check_csrf,
                        csrf_token, stripe_client, stripe_data, stripe_id, matches_price,
                        subscription_access)

owner = Blueprint('owner_dashboard', __name__)
SYNC_POOL = ThreadPoolExecutor(max_workers=1, thread_name_prefix='pikbot-bookkeeping')


@owner.after_request
def private_responses(response):
    response.headers['Cache-Control'] = 'no-store, private'
    return response


def init_owner(app):
    with database() as conn:
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_checked_at TIMESTAMPTZ')
        conn.execute('ALTER TABLE pikbot_accounts ADD COLUMN IF NOT EXISTS billing_mode BOOLEAN')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_feedback (
            id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL REFERENCES pikbot_accounts(email),
            kind TEXT NOT NULL CHECK (kind IN ('review','comment')), rating INTEGER CHECK (rating BETWEEN 1 AND 5),
            body TEXT NOT NULL, reviewed BOOLEAN NOT NULL DEFAULT FALSE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_expenses (
            id BIGSERIAL PRIMARY KEY, title TEXT NOT NULL, cents BIGINT NOT NULL CHECK (cents>0),
            due_date DATE NOT NULL, paid_at TIMESTAMPTZ, archived BOOLEAN NOT NULL DEFAULT FALSE)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_payment_records (
            invoice_id TEXT PRIMARY KEY, email TEXT NOT NULL, customer_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
            paid BIGINT NOT NULL, refunded BIGINT NOT NULL, fees_complete BOOLEAN NOT NULL, livemode BOOLEAN NOT NULL,
            synced_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_cash_events (
            event_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, kind TEXT NOT NULL,
            cents BIGINT NOT NULL, currency TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL, livemode BOOLEAN NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_owner_sync (
            id INTEGER PRIMARY KEY CHECK (id=1), running_until TIMESTAMPTZ,
            last_finished TIMESTAMPTZ, message TEXT, complete BOOLEAN NOT NULL DEFAULT FALSE,
            account_balance JSONB, livemode BOOLEAN)''')
        conn.execute('INSERT INTO pikbot_owner_sync (id) VALUES (1) ON CONFLICT DO NOTHING')
        conn.execute('''CREATE TABLE IF NOT EXISTS pikbot_refund_audit (
            refund_id TEXT PRIMARY KEY, invoice_id TEXT NOT NULL, owner_email TEXT NOT NULL,
            cents BIGINT NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
            request_key TEXT UNIQUE, livemode BOOLEAN NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW())''')
    app.register_blueprint(owner)


def cents(value):
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number <= 1_000_000 or number != number.quantize(Decimal('.01')):
            raise ValueError()
        amount = int(number * 100)
        if amount > 100_000_000:
            raise ValueError()
        return amount
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError('Enter a positive dollar amount with no more than two decimal places.') from None


def serial(row):
    return {key: value.isoformat() if isinstance(value, (datetime, date)) else value
            for key, value in row.items()}


def invoice_subscription(invoice):
    parent = invoice.get('parent') or {}
    return stripe_id((parent.get('subscription_details') or {}).get('subscription')) or stripe_id(invoice.get('subscription'))


def assert_mode(obj):
    live = os.getenv('STRIPE_SECRET_KEY', '').startswith(('sk_live_', 'rk_live_'))
    if bool(obj.get('livemode')) != live:
        raise ValueError('Payment environment mismatch.')


def owned_invoice(client, invoice_id):
    invoice = stripe_data(client.v1.invoices.retrieve(invoice_id))
    assert_mode(invoice)
    customer = stripe_id(invoice.get('customer'))
    with database() as conn:
        member = conn.execute('SELECT email FROM pikbot_accounts WHERE customer_id=%s', (customer,)).fetchone()
    subscription = invoice_subscription(invoice)
    if not member or not subscription or not matches_price(client.v1.subscriptions.retrieve(subscription)):
        raise ValueError('This invoice is not a PikBot membership payment.')
    if invoice.get('currency') != 'usd':
        raise ValueError('Only USD PikBot membership payments are supported here.')
    return invoice, member['email']


def paid_charges(client, invoice):
    payments = client.v1.invoice_payments.list({'invoice': invoice['id'], 'status': 'paid', 'limit': 100})
    for raw in payments.auto_paging_iter():
        payment = stripe_data(raw)
        detail = payment.get('payment') or {}
        if detail.get('type') != 'payment_intent':
            continue
        intent = stripe_data(client.v1.payment_intents.retrieve(stripe_id(detail['payment_intent']),
            {'expand': ['latest_charge.balance_transaction']}))
        assert_mode(intent)
        if stripe_id(intent.get('customer')) != stripe_id(invoice.get('customer')):
            raise ValueError('The payment customer does not match the invoice.')
        charge = intent.get('latest_charge')
        if not isinstance(charge, dict) or not charge.get('paid') or not charge.get('captured'):
            continue
        refunds = [stripe_data(item) for item in client.v1.refunds.list({
            'charge': charge['id'], 'limit': 100, 'expand': ['data.balance_transaction'],
        }).auto_paging_iter()]
        yield payment, intent, charge, refunds


def sync_invoice(client, invoice, email):
    """Record actual paid invoice allocations and actual Stripe fee/refund transactions."""
    events, refund_total, fees_complete = [], 0, True
    for payment, intent, charge, refunds in paid_charges(client, invoice):
        allocated = int(payment.get('amount_paid') or 0)
        stamp = (payment.get('status_transitions') or {}).get('paid_at') or payment['created']
        events.append(('payment:' + payment['id'], 'payment', allocated, stamp))
        # Mixed invoice charges need manual reconciliation; never invent fee allocations.
        if allocated != int(charge.get('amount_captured') or charge.get('amount') or 0):
            fees_complete = False
            continue
        balance = charge.get('balance_transaction')
        if isinstance(balance, dict) and balance.get('currency') == 'usd':
            events.append(('fee:' + balance['id'], 'fee', int(balance['fee']), balance['created']))
        else:
            fees_complete = False
        for refund in refunds:
            if refund.get('status') == 'succeeded':
                amount = int(refund['amount'])
                refund_total += amount
                events.append(('refund:' + refund['id'], 'refund', amount, refund['created']))
                balance = refund.get('balance_transaction')
                if isinstance(balance, dict) and balance.get('currency') == 'usd':
                    events.append(('fee:' + balance['id'], 'fee', int(balance['fee']), balance['created']))
                else:
                    fees_complete = False
            elif refund.get('status') in {'pending', 'requires_action'}:
                fees_complete = False
    with database() as conn:
        conn.execute('''INSERT INTO pikbot_payment_records
            (invoice_id,email,customer_id,created_at,currency,status,paid,refunded,fees_complete,livemode)
            VALUES (%s,%s,%s,to_timestamp(%s),%s,%s,%s,%s,%s,%s)
            ON CONFLICT (invoice_id) DO UPDATE SET status=EXCLUDED.status,paid=EXCLUDED.paid,
            refunded=EXCLUDED.refunded,fees_complete=EXCLUDED.fees_complete,synced_at=NOW()''',
            (invoice['id'], email, stripe_id(invoice['customer']), invoice['created'],
             invoice['currency'], invoice['status'], invoice.get('amount_paid') or 0, refund_total, fees_complete, bool(invoice['livemode'])))
        for event_id, kind, amount, stamp in events:
            conn.execute('''INSERT INTO pikbot_cash_events VALUES (%s,%s,%s,%s,'usd',to_timestamp(%s),%s)
                ON CONFLICT (event_id) DO UPDATE SET cents=EXCLUDED.cents''',
                (event_id, invoice['id'], kind, amount, stamp, bool(invoice['livemode'])))


def refresh_bookkeeping():
    complete, balance, message = False, None, 'Payment sync did not finish.'
    try:
        client = stripe_client()
        with database() as conn:
            members = {row['customer_id']: row['email'] for row in conn.execute(
                'SELECT email,customer_id FROM pikbot_accounts WHERE customer_id IS NOT NULL').fetchall()}
        checked, subscriptions = 0, {}
        for index, raw in enumerate(client.v1.invoices.list({'limit': 100}).auto_paging_iter()):
            if index >= 500:
                message = 'Only the newest 500 Stripe invoices were checked; totals may be incomplete.'
                break
            invoice = stripe_data(raw)
            customer, subscription = stripe_id(invoice.get('customer')), invoice_subscription(invoice)
            if customer not in members or not subscription or invoice.get('currency') != 'usd':
                continue
            assert_mode(invoice)
            if subscription not in subscriptions:
                subscriptions[subscription] = matches_price(client.v1.subscriptions.retrieve(subscription))
            if subscriptions[subscription]:
                sync_invoice(client, invoice, members[customer])
                checked += 1
        else:
            complete = True
            message = f'Synced {checked} PikBot invoices. Amounts are USD and exclude tax liabilities.'
        for customer in members:
            status = subscription_access(customer)
            if status == 'unavailable':
                complete = False
                message = 'Some subscription states could not be verified. Sync again before relying on the paid-member count.'
            with database() as conn:
                conn.execute('UPDATE pikbot_accounts SET billing_status=%s,billing_checked_at=NOW(),billing_mode=%s WHERE customer_id=%s',
                             (status or 'no_subscription', os.environ['STRIPE_SECRET_KEY'].startswith(('sk_live_', 'rk_live_')), customer))
        raw = stripe_data(client.v1.balance.retrieve())
        # Balances belong to the whole Stripe account, not just PikBot invoices.
        balance = {key: [{'currency': item['currency'], 'amount': item['amount']} for item in raw.get(key, [])]
                   for key in ('available', 'pending')}
        balance['payouts'] = [{key: stripe_data(item).get(key) for key in ('amount','currency','arrival_date','status')}
                             for item in client.v1.payouts.list({'limit': 10}).data]
        with database() as conn:
            pending = conn.execute("SELECT refund_id FROM pikbot_refund_audit WHERE status IN ('pending','requires_action') AND livemode=%s LIMIT 50",
                (os.environ['STRIPE_SECRET_KEY'].startswith(('sk_live_', 'rk_live_')),)).fetchall()
        for item in pending:
            result = stripe_data(client.v1.refunds.retrieve(item['refund_id']))
            with database() as conn:
                conn.execute('UPDATE pikbot_refund_audit SET status=%s WHERE refund_id=%s', (result['status'], item['refund_id']))
    except (stripe.StripeError, psycopg.Error, ValueError, KeyError, TypeError):
        complete = False
        message = 'Sync was interrupted. Check Stripe permissions and retry. Existing records were retained.'
    finally:
        with database() as conn:
            conn.execute('''UPDATE pikbot_owner_sync SET running_until=NULL,last_finished=NOW(),
                complete=%s,message=%s,account_balance=%s::jsonb,livemode=%s WHERE id=1''',
                (complete, message, json.dumps(balance) if balance is not None else None,
                 os.getenv('STRIPE_SECRET_KEY','').startswith(('sk_live_', 'rk_live_'))))


@owner.get('/owner')
def owner_page():
    record = account()
    if not record:
        return redirect('/owner/login')
    if not private_paid_test(record):
        return 'Owner access required.', 403
    return render_template('owner.html', csrf=csrf_token())


@owner.get('/api/owner/dashboard')
@owner_required
def dashboard():
    live = os.getenv('STRIPE_SECRET_KEY', '').startswith(('sk_live_', 'rk_live_'))
    month = request.args.get('month', datetime.now(timezone.utc).strftime('%Y-%m'))
    try:
        start = datetime.strptime(month, '%Y-%m').replace(tzinfo=timezone.utc)
        stop = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    except ValueError:
        return jsonify(error='Choose a valid month.'), 400
    with database() as conn:
        counts = conn.execute('''SELECT COUNT(*) AS total,
            COUNT(*) FILTER (WHERE trial_ends>NOW() AND customer_id IS NULL) AS trial,
            COUNT(*) FILTER (WHERE billing_status='subscriber' AND billing_hold IS NULL AND billing_mode=%s) AS paid_last_synced
            FROM pikbot_accounts''', (live,)).fetchone()
        money = {row['kind']: int(row['cents']) for row in conn.execute('''SELECT kind,SUM(cents) AS cents
            FROM pikbot_cash_events WHERE currency='usd' AND occurred_at>=%s AND occurred_at<%s AND livemode=%s GROUP BY kind''', (start, stop, live)).fetchall()}
        fees_missing = conn.execute('SELECT COUNT(*) AS count FROM pikbot_payment_records WHERE NOT fees_complete AND paid>0 AND livemode=%s', (live,)).fetchone()['count']
        expenses = conn.execute('''SELECT COALESCE(SUM(cents) FILTER (WHERE paid_at>=%s AND paid_at<%s),0) AS paid,
            COALESCE(SUM(cents) FILTER (WHERE paid_at IS NULL),0) AS outstanding,
            COALESCE(SUM(cents) FILTER (WHERE paid_at IS NULL AND due_date<=CURRENT_DATE),0) AS due
            FROM pikbot_expenses WHERE NOT archived''', (start, stop)).fetchone()
        sync = conn.execute('SELECT * FROM pikbot_owner_sync WHERE id=1').fetchone()
        if sync and sync.get('livemode') != live:
            sync = None
        members = conn.execute('''SELECT email,trial_ends,billing_status,billing_hold,billing_checked_at
            FROM pikbot_accounts ORDER BY email LIMIT 50''').fetchall()
        payments = conn.execute('SELECT * FROM pikbot_payment_records WHERE livemode=%s ORDER BY created_at DESC LIMIT 50', (live,)).fetchall()
        feedback = conn.execute('SELECT * FROM pikbot_feedback ORDER BY created_at DESC LIMIT 50').fetchall()
        bills = conn.execute('SELECT * FROM pikbot_expenses WHERE NOT archived ORDER BY paid_at NULLS FIRST,due_date,id LIMIT 50').fetchall()
        refunds = conn.execute('SELECT * FROM pikbot_refund_audit WHERE livemode=%s ORDER BY created_at DESC LIMIT 30', (live,)).fetchall()
    paid, refunded, fees = money.get('payment', 0), money.get('refund', 0), money.get('fee', 0)
    return jsonify(month=month, counts=counts, revenue=dict(collected=paid, refunded=refunded, fees=fees,
        net_after_known_fees=paid-refunded-fees, fees_missing=fees_missing),
        expenses={key: int(value) for key, value in expenses.items()},
        stripe_configured=bool(os.getenv('STRIPE_SECRET_KEY') and os.getenv('STRIPE_PRICE_ID')),
        mode='live' if os.getenv('STRIPE_SECRET_KEY', '').startswith(('sk_live_', 'rk_live_')) else 'test / not configured',
        sync=serial(sync) if sync else None, members=[serial(row) for row in members],
        payments=[serial(row) for row in payments], feedback=[serial(row) for row in feedback],
        bills=[serial(row) for row in bills], refunds=[serial(row) for row in refunds])


@owner.get('/api/owner/list/<kind>')
@owner_required
def list_records(kind):
    queries = {
        'members': 'SELECT email,trial_ends,billing_status,billing_hold,billing_checked_at,billing_mode FROM pikbot_accounts ORDER BY email',
        'payments': 'SELECT * FROM pikbot_payment_records WHERE livemode=%s ORDER BY created_at DESC,invoice_id',
        'feedback': 'SELECT * FROM pikbot_feedback ORDER BY created_at DESC,id DESC',
        'bills': 'SELECT * FROM pikbot_expenses WHERE NOT archived ORDER BY paid_at NULLS FIRST,due_date,id',
    }
    if kind not in queries:
        return jsonify(error='Unknown list.'), 404
    try:
        page = int(request.args.get('page', '0'))
        if not 0 <= page <= 10000:
            raise ValueError()
    except ValueError:
        return jsonify(error='Invalid page.'), 400
    with database() as conn:
        params = (os.getenv('STRIPE_SECRET_KEY','').startswith(('sk_live_', 'rk_live_')), page*50) if kind=='payments' else (page*50,)
        rows = conn.execute(queries[kind] + ' LIMIT 51 OFFSET %s', params).fetchall()
    return jsonify(rows=[serial(row) for row in rows[:50]], page=page, has_more=len(rows)>50)


@owner.post('/api/owner/sync')
@owner_required
def start_sync():
    check_csrf()
    if not os.getenv('STRIPE_SECRET_KEY') or not os.getenv('STRIPE_PRICE_ID'):
        return jsonify(error='Stripe is not configured. Member and feedback lists still work.'), 503
    with database() as conn:
        row = conn.execute('''UPDATE pikbot_owner_sync SET running_until=NOW()+INTERVAL '15 minutes',message='Syncing Stripe records…'
            WHERE id=1 AND (running_until IS NULL OR running_until<NOW()) RETURNING id''').fetchone()
    if row:
        SYNC_POOL.submit(refresh_bookkeeping)
    return jsonify(syncing=True), 202


@owner.post('/api/feedback')
def submit_feedback():
    record = account()
    if not record:
        return jsonify(error='Sign in required.'), 401
    check_csrf()
    body = request.form.get('body', '').strip()
    kind = request.form.get('kind', 'comment')
    try:
        rating = int(request.form['rating']) if request.form.get('rating') else None
    except ValueError:
        return jsonify(error='Choose a rating between 1 and 5.'), 400
    if not 1 <= len(body) <= 2000 or kind not in {'comment', 'review'} or (rating is not None and rating not in range(1, 6)):
        return jsonify(error='Enter a comment up to 2,000 characters and a valid rating.'), 400
    with database() as conn:
        conn.execute('SELECT email FROM pikbot_accounts WHERE email=%s FOR UPDATE', (record['email'],))
        count = conn.execute("SELECT COUNT(*) AS n FROM pikbot_feedback WHERE email=%s AND created_at>NOW()-INTERVAL '24 hours'", (record['email'],)).fetchone()['n']
        if count >= 5:
            return jsonify(error='You can send five feedback messages per day.'), 429
        conn.execute('INSERT INTO pikbot_feedback(email,kind,rating,body) VALUES (%s,%s,%s,%s)', (record['email'], kind, rating, body))
    return jsonify(message='Your feedback was sent privately to PikBot.'), 201


@owner.post('/api/owner/feedback/<int:item_id>/reviewed')
@owner_required
def review_feedback(item_id):
    check_csrf()
    with database() as conn:
        conn.execute('UPDATE pikbot_feedback SET reviewed=TRUE WHERE id=%s', (item_id,))
    return jsonify(saved=True)


@owner.post('/api/owner/expenses')
@owner_required
def create_expense():
    check_csrf()
    try:
        amount = cents(request.form.get('amount'))
        due = date.fromisoformat(request.form.get('due_date', ''))
        title = request.form.get('title', '').strip()
        if not 1 <= len(title) <= 120:
            raise ValueError('Enter a bill name up to 120 characters.')
    except ValueError as exc:
        return jsonify(error=str(exc)), 400
    with database() as conn:
        conn.execute('INSERT INTO pikbot_expenses(title,cents,due_date) VALUES (%s,%s,%s)', (title, amount, due))
    return jsonify(saved=True), 201


@owner.post('/api/owner/expenses/<int:item_id>/<action>')
@owner_required
def change_expense(item_id, action):
    check_csrf()
    statements = {'paid': 'UPDATE pikbot_expenses SET paid_at=NOW() WHERE id=%s AND paid_at IS NULL',
                  'unpaid': 'UPDATE pikbot_expenses SET paid_at=NULL WHERE id=%s',
                  'archive': 'UPDATE pikbot_expenses SET archived=TRUE WHERE id=%s'}
    if action not in statements:
        return jsonify(error='Unknown bill action.'), 400
    with database() as conn:
        conn.execute(statements[action], (item_id,))
    return jsonify(saved=True)


def recent_refund_auth():
    check_csrf()
    issued = session.get('owner_authenticated_at', 0)
    if not isinstance(issued, (int, float)) or not 0 <= time.time() - issued < 900:
        return False
    return True


def refund_payment(client, invoice_id, intent_id=None):
    invoice, email = owned_invoice(client, invoice_id)
    candidates = []
    for payment, intent, charge, refunds in paid_charges(client, invoice):
        if intent_id and intent['id'] != intent_id:
            continue
        captured = int(charge.get('amount_captured') or 0)
        if int(payment.get('amount_paid') or 0) != captured:
            continue  # Split invoices must be reconciled in Stripe before refunding.
        reserved = sum(int(r['amount']) for r in refunds if r.get('status') not in {'failed', 'canceled'})
        remaining = captured - reserved
        if remaining > 0 and not charge.get('disputed'):
            candidates.append((invoice, email, intent, remaining))
    if len(candidates) != 1:
        raise ValueError('No single refundable payment is available. Review split payments or disputes in Stripe.')
    return candidates[0]


def refund_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='pikbot-owner-refund')


def refund_receipt(result, data, invoice=None, full=False):
    with database() as conn:
        conn.execute('''INSERT INTO pikbot_refund_audit
            (refund_id,invoice_id,owner_email,cents,currency,status,request_key,livemode)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (refund_id) DO UPDATE SET status=EXCLUDED.status''',
            (result['id'], data['invoice'], data['owner'], result['amount'], result['currency'], result['status'], data['nonce'], bool(result.get('livemode'))))
        if invoice and full and result['status'] == 'succeeded':
            conn.execute('UPDATE pikbot_accounts SET billing_hold=%s WHERE customer_id=%s',
                         ('charge.refunded', stripe_id(invoice['customer'])))
    return jsonify(refund_id=result['id'], status=result['status'], amount=result['amount'], currency=result['currency'])


@owner.post('/api/owner/refunds/prepare')
@owner_required
def prepare_refund():
    if not recent_refund_auth():
        return jsonify(error='Sign in again through Owner login before issuing a refund.'), 403
    if not os.getenv('STRIPE_WEBHOOK_SECRET'):
        return jsonify(error='Configure signed Stripe payment notifications before issuing refunds.'), 503
    try:
        amount = cents(request.form.get('amount'))
        invoice, email, intent, remaining = refund_payment(stripe_client(), request.form.get('invoice_id', ''))
        if amount > remaining:
            raise ValueError('The refund exceeds the remaining refundable amount.')
        data = {'invoice': invoice['id'], 'intent': intent['id'], 'cents': amount,
                'currency': 'usd', 'owner': session['email'], 'nonce': secrets.token_urlsafe(24),
                'key': hashlib.sha256(os.environ['STRIPE_SECRET_KEY'].encode()).hexdigest()}
        return jsonify(token=refund_serializer().dumps(data), amount=amount, currency='usd',
            member=email, invoice=invoice['id'], mode='live' if intent.get('livemode') else 'test')
    except (ValueError, KeyError) as exc:
        return jsonify(error=str(exc) if isinstance(exc, ValueError) else 'Stripe is not configured.'), 400
    except stripe.StripeError:
        return jsonify(error='Stripe could not verify this payment. No refund was submitted.'), 503


@owner.post('/api/owner/refunds/confirm')
@owner_required
def confirm_refund():
    if not recent_refund_auth():
        return jsonify(error='Sign in again through Owner login before issuing a refund.'), 403
    if not os.getenv('STRIPE_WEBHOOK_SECRET'):
        return jsonify(error='Configure signed Stripe payment notifications before issuing refunds.'), 503
    try:
        data = refund_serializer().loads(request.form.get('token', ''), max_age=600)
        if (request.form.get('confirm') != 'REFUND' or data['owner'] != session['email']
                or data['key'] != hashlib.sha256(os.environ['STRIPE_SECRET_KEY'].encode()).hexdigest()):
            raise ValueError('Refund confirmation did not match. Nothing was submitted.')
        client = stripe_client()
        with database() as conn:
            previous = conn.execute('SELECT refund_id FROM pikbot_refund_audit WHERE request_key=%s AND owner_email=%s',
                                    (data['nonce'], data['owner'])).fetchone()
        if previous:
            return refund_receipt(stripe_data(client.v1.refunds.retrieve(previous['refund_id'])), data)
        # Recover a refund accepted by Stripe if the earlier DB write or response failed.
        original_invoice, _ = owned_invoice(client, data['invoice'])
        for _, prior_intent, _, prior_refunds in paid_charges(client, original_invoice):
            for prior in prior_refunds:
                if (prior_intent['id'] == data['intent']
                        and (prior.get('metadata') or {}).get('pikbot_request') == data['nonce']):
                    return refund_receipt(prior, data)
        invoice, email, intent, remaining = refund_payment(client, data['invoice'], data['intent'])
        # Stable Stripe idempotency protects retried confirmation requests.
        key = 'pikbot-refund-' + data['nonce']
        if not isinstance(data['cents'], int) or not 0 < data['cents'] <= remaining:
            raise ValueError('The refundable balance changed. Review the payment again.')
        result = stripe_data(client.v1.refunds.create({'payment_intent': data['intent'],
            'amount': data['cents'], 'reason': 'requested_by_customer',
            'metadata': {'pikbot_request': data['nonce']}},
            options={'idempotency_key': key}))
        return refund_receipt(result, data, invoice, data['cents'] == remaining)
    except (BadSignature, SignatureExpired):
        return jsonify(error='Refund confirmation expired or was changed. Review it again.'), 400
    except (ValueError, KeyError) as exc:
        return jsonify(error=str(exc) if isinstance(exc, ValueError) else 'Refund configuration is unavailable.'), 400
    except (stripe.StripeError, psycopg.Error):
        return jsonify(error='The refund could not be confirmed. Retrying the same confirmation is safe.'), 503
