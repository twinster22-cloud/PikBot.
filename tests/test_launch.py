import hashlib
import hmac
import json
import os
import time
import unittest
from concurrent.futures import Future
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import stripe
import history_loader
import membership
from test_security import server


class ImmediatePool:
    def submit(self, fn):
        future = Future()
        try:
            future.set_result(fn())
        except Exception as exc:
            future.set_exception(exc)
        return future


class HistoryTests(unittest.TestCase):
    def setUp(self):
        history_loader.JOBS.clear()
        self.now = datetime(2026, 11, 5, tzinfo=timezone.utc)
        self.offers = pd.DataFrame({'player': ['A']})
        self.frame = pd.DataFrame([{'player': 'A', 'sport': 'NBA', 'stat': 'PTS',
                                   'date': '2026-11-04', 'value': 20}])

    def test_one_pending_job_is_reused(self):
        pool = MagicMock()
        pool.submit.return_value = Future()
        with patch.object(history_loader, 'POOL', pool):
            for _ in range(3):
                with self.assertRaisesRegex(Exception, 'is loading'):
                    history_loader.load('NBA', self.offers, self.now, MagicMock())
        self.assertEqual(pool.submit.call_count, 1)

    def test_all_seven_sports_use_same_bounded_loader(self):
        with patch.object(history_loader, 'POOL', ImmediatePool()):
            for sport in ['NBA', 'WNBA', 'NFL', 'NHL', 'MLB', 'COLLEGE FOOTBALL', 'COLLEGE BASKETBALL']:
                provider = MagicMock(return_value=self.frame)
                result = history_loader.load(sport, self.offers, self.now, provider)
                self.assertEqual(len(result), 1)
                provider.assert_called_once()

    def test_persistent_cache_skips_provider(self):
        provider = MagicMock()
        result = history_loader.load('NBA', self.offers, self.now, provider,
                                     read_cache=lambda key: self.frame)
        self.assertEqual(len(result), 1)
        provider.assert_not_called()

    def test_failed_jobs_retry_after_fixed_cooldown(self):
        future = Future()
        future.set_exception(history_loader.FeedError('Provider unavailable'))
        key = history_loader.cache_key('NBA', self.offers, self.now)
        history_loader.JOBS[key] = (future, 1000)
        with patch.object(history_loader, 'monotonic', return_value=100):
            with self.assertRaises(Exception):
                history_loader.load('NBA', self.offers, self.now, MagicMock())
        expiry = history_loader.JOBS[key][1]
        with patch.object(history_loader, 'monotonic', return_value=105):
            with self.assertRaises(Exception):
                history_loader.load('NBA', self.offers, self.now, MagicMock())
        self.assertEqual(expiry, history_loader.JOBS[key][1])
        with patch.object(history_loader, 'monotonic', return_value=116), \
             patch.object(history_loader, 'POOL', ImmediatePool()):
            self.assertEqual(len(history_loader.load('NBA', self.offers, self.now,
                            lambda *args: self.frame)), 1)

    def test_cache_roundtrip_keeps_source_and_photos(self):
        self.frame.attrs = {'source': 'Provider', 'photos': {'A': 'https://example.com/a.png'}}
        restored = history_loader.decode(json.loads(history_loader.encode(self.frame)))
        self.assertEqual(restored.attrs, self.frame.attrs)


class BillingTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.env = patch.dict(os.environ, {'STRIPE_SECRET_KEY': 'sk_test_unit_only',
            'STRIPE_PRICE_ID': 'price_plan', 'STRIPE_WEBHOOK_SECRET': 'whsec_unit_only',
            'PIKBOT_PUBLIC_URL': 'https://example.com', 'PIKBOT_BILLING_MODE': 'live',
            'PIKBOT_CHECKOUT_ENABLED': '1'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_test_keys_do_not_open_live_checkout(self):
        self.assertFalse(membership.billing_ready())
        with patch.dict(os.environ, {'STRIPE_SECRET_KEY': 'rk_live_unit_only'}):
            self.assertTrue(membership.billing_ready())
        with patch.dict(os.environ, {'STRIPE_SECRET_KEY': 'rk_live_unit_only', 'STRIPE_WEBHOOK_SECRET': ''}):
            self.assertFalse(membership.billing_ready())

    def test_only_active_membership_with_paid_invoice_unlocks(self):
        for status, invoice, expected in [
            ('active', 'paid', 'subscriber'), ('active', 'open', 'payment_required'),
            ('active', 'void', 'payment_required'), ('past_due', 'paid', 'payment_required'),
            ('unpaid', 'open', 'payment_required'), ('incomplete', 'open', 'payment_required'),
            ('canceled', 'paid', 'payment_required')]:
            client = MagicMock()
            sub = stripe.StripeObject.construct_from({'status': status,
                'items': {'data': [{'price': {'id': 'price_plan'}}]},
                'latest_invoice': {'status': invoice}}, 'unit')
            client.v1.subscriptions.list.return_value.auto_paging_iter.return_value = iter([sub])
            with patch('membership.stripe_client', return_value=client):
                self.assertEqual(membership.subscription_access('cus_test'), expected)

    def test_failed_payment_cannot_fall_back_to_trial(self):
        record = {'customer_id': 'cus_test', 'trial_ends': datetime.now(timezone.utc) + timedelta(days=5)}
        with patch('membership.private_paid_test', return_value=False), \
             patch('membership.subscription_access', return_value='payment_required'):
            self.assertEqual(membership.access(record), 'payment_required')

    def test_risk_hold_blocks_even_a_paid_member(self):
        with patch('membership.private_paid_test', return_value=False), \
             patch('membership.subscription_access', return_value='subscriber') as paid:
            self.assertEqual(membership.access({'billing_hold': 'charge.dispute.created'}), 'payment_required')
            paid.assert_not_called()

    def signed(self, event):
        body = json.dumps(event).encode()
        stamp = str(int(time.time()))
        signature = hmac.new(b'whsec_unit_only', stamp.encode() + b'.' + body, hashlib.sha256).hexdigest()
        return body, {'Stripe-Signature': 't=' + stamp + ',v1=' + signature}

    def test_unsigned_webhook_is_rejected_without_database_access(self):
        with patch('membership.database') as db:
            response = self.client.post('/billing/webhook', json={'type': 'invoice.paid'})
            self.assertEqual(response.status_code, 400)
            db.assert_not_called()

    def test_wrong_mode_signed_webhook_is_rejected(self):
        body, headers = self.signed({'id': 'evt_mode', 'livemode': True, 'type': 'invoice.paid',
                                    'data': {'object': {'customer': 'cus_test'}}})
        self.assertEqual(self.client.post('/billing/webhook', data=body, headers=headers,
                                         content_type='application/json').status_code, 400)

    def test_duplicate_delivery_does_not_apply_updates_twice(self):
        body, headers = self.signed({'id': 'evt_duplicate', 'livemode': False, 'type': 'invoice.paid',
                                    'data': {'object': {'customer': 'cus_test'}}})
        conn = MagicMock()
        conn.execute.return_value.fetchone.return_value = None
        db = MagicMock()
        db.return_value.__enter__.return_value = conn
        with patch('membership.database', db), patch('membership.subscription_access', return_value='subscriber'):
            response = self.client.post('/billing/webhook', data=body, headers=headers, content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(conn.execute.call_count, 1)

    def test_expanded_stripe_ids_are_resolved(self):
        self.assertEqual(membership.stripe_id(stripe.StripeObject.construct_from({'id': 'cus_test'}, 'unit')), 'cus_test')

    def test_trial_payload_never_contains_paid_details(self):
        result = server.trial_public_board({'picks': [{'player': 'A', 'line': 20,
            'reason': 'private', 'p_raw': .75, 'recent_games': [1]}] * 5})
        self.assertEqual(len(result['picks']), 3)
        self.assertNotIn('reason', result['picks'][0])
        self.assertNotIn('p_raw', result['picks'][0])

    def test_launch_status_requires_owner(self):
        with patch('membership.account', return_value={'email': 'member@example.com'}), \
             patch('membership.private_paid_test', return_value=False):
            self.assertEqual(self.client.get('/api/owner/launch-status').status_code, 403)


if __name__ == '__main__':
    unittest.main()
