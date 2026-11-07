"""Security regression checks with no live database, providers or Stripe calls."""
import hashlib
import importlib
import os
from pathlib import Path
import re
import time
import unittest
from unittest.mock import patch

import membership
import owner_dashboard

def configure_test_app(app):
    app.config.update(TESTING=True, SECRET_KEY='test-only-secret',
                      MAX_CONTENT_LENGTH=4_000_000,
                      MAX_FORM_MEMORY_SIZE=128_000, MAX_FORM_PARTS=20)
    app.register_blueprint(membership.membership)

with patch('membership.init_accounts', configure_test_app), \
     patch('owner_dashboard.init_owner', lambda app: app.register_blueprint(owner_dashboard.owner)), \
     patch('prediction_audit.init_audit', lambda app: app.register_blueprint(__import__('prediction_audit').audit)):
    server = importlib.import_module('server')

class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()

    def test_login_options_and_public_signup_need_no_code(self):
        response = self.client.get('/login?signup=1')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'/owner/login', response.data)
        self.assertNotIn(b'/owner/login', self.client.get('/login').data)
        self.assertIn(b'href="/login"', response.data)
        self.assertNotIn(b'name="test_code"', response.data)
        self.assertIn(b'name="test_code"', self.client.get('/owner/login').data)

    def test_security_headers(self):
        response = self.client.get('/login')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertIn('no-store', response.headers['Cache-Control'])
        self.assertIn("frame-ancestors 'none'", response.headers['Content-Security-Policy'])
        self.assertNotIn("script-src 'self' 'unsafe-inline'", response.headers['Content-Security-Policy'])

    def test_dashboard_script_nonce_matches_policy(self):
        with patch('server.account', return_value={'email': 'test@example.com'}):
            response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        nonce = re.search(r'<script nonce="([^"]+)">', response.get_data(as_text=True)).group(1)
        self.assertIn("'nonce-" + nonce + "'", response.headers['Content-Security-Policy'])
        self.assertNotIn('<script>', response.get_data(as_text=True))

    def test_public_diagnostics_are_denied_without_provider_calls(self):
        paths = ['odds-feed-check', 'college-feed-check', 'sports-coverage-check',
                 'dunk-feed-check', 'next-projection-check', 'additional-feed-check',
                 'sportsdata-test', 'live-props-status']
        with patch('membership.account', return_value=None):
            for path in paths:
                self.assertEqual(self.client.get('/api/' + path).status_code, 401, path)
        with patch('membership.account', return_value={'email': 'test@example.com'}), \
             patch('membership.private_paid_test', return_value=False):
            for path in paths:
                self.assertEqual(self.client.get('/api/' + path).status_code, 403, path)

    def test_paid_members_cannot_use_owner_uploads(self):
        with patch('membership.account', return_value={'email': 'member@example.com'}), \
             patch('membership.access', return_value='subscriber'), \
             patch('membership.private_paid_test', return_value=False):
            self.assertEqual(self.client.post('/api/analyze').status_code, 403)

    def test_owner_upload_requires_csrf(self):
        with patch('membership.account', return_value={'email': 'owner@example.com'}), \
             patch('membership.private_paid_test', return_value=True):
            self.assertEqual(self.client.post('/api/analyze').status_code, 400)

    def test_membership_does_not_grant_owner_permissions(self):
        from datetime import datetime, timezone
        record = {'email': 'member@example.com', 'customer_id': 'cus_test',
                  'trial_ends': datetime.now(timezone.utc)}
        for owner, expected in [(False, 'member'), (True, 'owner')]:
            with patch('membership.account', return_value=record), \
                 patch('membership.access', return_value='subscriber'), \
                 patch('membership.private_paid_test', return_value=owner):
                response = self.client.get('/api/account')
                self.assertEqual(response.json['role'], expected)
                self.assertEqual(response.json['permissions']['owner_tools'], owner)

    def test_paid_members_keep_read_access_to_sports(self):
        with patch('membership.account', return_value={'email': 'member@example.com'}), \
             patch('membership.access', return_value='subscriber'), \
             patch('today.active_sports', return_value=[]):
            self.assertEqual(self.client.get('/api/today').status_code, 200)

    def test_expired_and_unpaid_accounts_cannot_fetch_picks(self):
        with patch('membership.account', return_value={'email': 'test@example.com'}):
            for level in ['expired', 'payment_required']:
                with patch('membership.access', return_value=level):
                    response = self.client.get('/api/today/predictions?sport=NBA')
                    self.assertEqual(response.status_code, 402)
                    self.assertNotIn('picks', response.json)

    def test_unicode_csrf_returns_bad_request(self):
        self.client.get('/login')
        self.assertEqual(self.client.post('/login', data={'csrf': '\u2603'}).status_code, 400)

    def test_owner_access_expires_and_is_bound_to_current_code(self):
        email, code = 'owner@example.com', 'test-owner-code'
        with patch.dict(os.environ, {
            'PIKBOT_PAID_TEST_EMAIL_SHA256': hashlib.sha256(email.encode()).hexdigest(),
            'PIKBOT_SIGNUP_TEST_CODE': code,
        }), server.app.test_request_context('/'):
            membership.session.update(owner_test=True, owner_authenticated_at=time.time(),
                owner_code_digest=hashlib.sha256(code.encode()).hexdigest())
            self.assertTrue(membership.private_paid_test({'email': email}))
            self.assertFalse(membership.private_paid_test({'email': 'other@example.com'}))
            membership.session['owner_authenticated_at'] = time.time() - 7201
            self.assertFalse(membership.private_paid_test({'email': email}))
            membership.session['owner_authenticated_at'] = time.time()
            membership.session['owner_code_digest'] = 'old-code'
            self.assertFalse(membership.private_paid_test({'email': email}))

if __name__ == '__main__':
    unittest.main()

