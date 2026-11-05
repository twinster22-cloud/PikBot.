import hashlib
import os
import time
import unittest
from unittest.mock import MagicMock, patch

import owner_dashboard as admin
from test_security import server


class OwnerDashboardTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.env = patch.dict(os.environ, {'STRIPE_SECRET_KEY': 'sk_test_unit_only',
            'STRIPE_PRICE_ID': 'price_plan', 'STRIPE_WEBHOOK_SECRET': 'whsec_unit_only'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def owner_session(self, age=0):
        with self.client.session_transaction() as session:
            session['csrf'] = 'unit-csrf'
            session['email'] = 'owner@example.com'
            session['owner_authenticated_at'] = time.time() - age

    def test_members_cannot_read_business_data_or_issue_refunds(self):
        routes = [('get','/api/owner/dashboard'), ('get','/api/owner/list/members'),
            ('get','/api/owner/list/payments'), ('get','/api/owner/list/feedback'),
            ('post','/api/owner/sync'), ('post','/api/owner/expenses'),
            ('post','/api/owner/expenses/1/paid'), ('post','/api/owner/feedback/1/reviewed'),
            ('post','/api/owner/refunds/prepare'), ('post','/api/owner/refunds/confirm')]
        with patch('membership.account', return_value={'email':'member@example.com'}), \
             patch('membership.private_paid_test', return_value=False), \
             patch('owner_dashboard.stripe_client') as stripe:
            for method, path in routes:
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 403, path)
            stripe.assert_not_called()

    def test_owner_page_is_separate_and_private(self):
        with patch('owner_dashboard.account', return_value=None):
            self.assertEqual(self.client.get('/owner').headers['Location'], '/owner/login')
        with patch('owner_dashboard.account', return_value={'email':'member@example.com'}), \
             patch('owner_dashboard.private_paid_test', return_value=False):
            self.assertEqual(self.client.get('/owner').status_code, 403)
        with patch('owner_dashboard.account', return_value={'email':'owner@example.com'}), \
             patch('owner_dashboard.private_paid_test', return_value=True):
            response = self.client.get('/owner')
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Payments &amp; refunds', response.data.replace(b'Payments & refunds',b'Payments &amp; refunds'))
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertIn(b'OWNER ONLY', response.data)

    def test_signed_in_owner_app_opens_business_dashboard(self):
        with patch('server.account',return_value={'email':'owner@example.com'}), \
             patch('server.private_paid_test',return_value=True):
            self.assertEqual(self.client.get('/').headers['Location'],'/owner')
            self.assertEqual(self.client.get('/?view=picks').status_code,200)

    def test_owner_mutations_need_csrf(self):
        with patch('membership.account', return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test', return_value=True):
            for path in ['/api/owner/sync','/api/owner/expenses','/api/owner/refunds/prepare']:
                self.assertEqual(self.client.post(path).status_code, 400)

    def test_old_owner_session_cannot_refund(self):
        self.owner_session(age=901)
        with patch('membership.account', return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test', return_value=True), patch('owner_dashboard.stripe_client') as stripe:
            self.assertEqual(self.client.post('/api/owner/refunds/prepare',data={'csrf':'unit-csrf'}).status_code,403)
            stripe.assert_not_called()

    def test_money_values_cannot_be_negative_or_fractional_cents(self):
        for value in ['-1','0','1.001','NaN','Infinity','1e99999999']:
            with self.assertRaises(ValueError):
                admin.cents(value)
        self.assertEqual(admin.cents('15.25'),1525)

    def test_unrelated_invoices_cannot_be_refunded(self):
        client, db = MagicMock(), MagicMock()
        client.v1.invoices.retrieve.return_value = {'id':'in_other','livemode':False,'customer':'cus_other','currency':'usd'}
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = None
        with patch('owner_dashboard.database', db):
            with self.assertRaisesRegex(ValueError,'not a PikBot'):
                admin.owned_invoice(client,'in_other')

    def test_feedback_is_private_and_rate_limited(self):
        self.owner_session()
        db = MagicMock()
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = {'n':5}
        with patch('owner_dashboard.account',return_value={'email':'member@example.com'}), patch('owner_dashboard.database',db):
            response=self.client.post('/api/feedback',data={'csrf':'unit-csrf','kind':'comment','body':'Help with picks'})
            self.assertEqual(response.status_code,429)
        with patch('owner_dashboard.account',return_value=None):
            self.assertEqual(self.client.post('/api/feedback').status_code,401)

    def token(self, **changes):
        payload={'invoice':'in_plan','intent':'pi_plan','cents':1500,'currency':'usd',
            'owner':'owner@example.com','nonce':'unique-unit-request',
            'key':hashlib.sha256(b'sk_test_unit_only').hexdigest()}
        payload.update(changes)
        with server.app.app_context():
            return admin.refund_serializer().dumps(payload)

    def test_tampered_confirmation_never_calls_stripe(self):
        self.owner_session()
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True),patch('owner_dashboard.stripe_client') as stripe:
            for token, word in [('tampered-token','REFUND'),(self.token(),'YES'),(self.token(owner='other@example.com'),'REFUND')]:
                response=self.client.post('/api/owner/refunds/confirm',data={'csrf':'unit-csrf','token':token,'confirm':word})
                self.assertEqual(response.status_code,400)
            stripe.assert_not_called()

    def test_refund_uses_signed_amount_and_stable_idempotency(self):
        self.owner_session()
        client,db=MagicMock(),MagicMock()
        invoice={'id':'in_plan','amount_paid':1500,'customer':'cus_member'}
        result={'id':'re_once','amount':1500,'currency':'usd','status':'pending','livemode':False}
        client.v1.refunds.create.return_value=result
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value=None
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True), \
             patch('owner_dashboard.stripe_client',return_value=client),patch('owner_dashboard.database',db), \
             patch('owner_dashboard.owned_invoice',return_value=(invoice,'member@example.com')), \
             patch('owner_dashboard.paid_charges',return_value=[]), \
             patch('owner_dashboard.refund_payment',return_value=(invoice,'member@example.com',{'id':'pi_plan'},1500)):
            token=self.token()
            for _ in range(2):
                response=self.client.post('/api/owner/refunds/confirm',data={'csrf':'unit-csrf','token':token,'confirm':'REFUND','amount':'999999'})
                self.assertEqual(response.status_code,200)
            for call in client.v1.refunds.create.call_args_list:
                self.assertEqual(call.args[0]['amount'],1500)
                self.assertEqual(call.kwargs['options']['idempotency_key'],'pikbot-refund-unique-unit-request')

    def test_replayed_refund_returns_existing_receipt(self):
        self.owner_session()
        client,db=MagicMock(),MagicMock()
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'refund_id':'re_once'}
        client.v1.refunds.retrieve.return_value={'id':'re_once','amount':1500,'currency':'usd','status':'succeeded','livemode':False}
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True), \
             patch('owner_dashboard.stripe_client',return_value=client),patch('owner_dashboard.database',db):
            response=self.client.post('/api/owner/refunds/confirm',data={'csrf':'unit-csrf','token':self.token(),'confirm':'REFUND'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['refund_id'],'re_once')
            client.v1.refunds.create.assert_not_called()

    def test_over_refund_is_rejected(self):
        self.owner_session()
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True), \
             patch('owner_dashboard.refund_payment',return_value=({'id':'in_plan'},'member@example.com',{'id':'pi_plan'},500)):
            response=self.client.post('/api/owner/refunds/prepare',data={'csrf':'unit-csrf','invoice_id':'in_plan','amount':'15.00'})
            self.assertEqual(response.status_code,400)
            self.assertIn('exceeds',response.json['error'])

    def test_reviewing_refund_never_creates_a_refund(self):
        self.owner_session()
        client = MagicMock()
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True), \
             patch('owner_dashboard.stripe_client',return_value=client), \
             patch('owner_dashboard.refund_payment',return_value=({'id':'in_plan'},'member@example.com',{'id':'pi_plan','livemode':False},1500)):
            response=self.client.post('/api/owner/refunds/prepare',data={'csrf':'unit-csrf','invoice_id':'in_plan','amount':'10.00'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['amount'],1000)
            self.assertEqual(response.json['mode'],'test')
            client.v1.refunds.create.assert_not_called()

    def test_cash_ledger_uses_successful_refunds_and_real_fee_records(self):
        db=MagicMock()
        invoice={'id':'in_plan','customer':'cus_member','created':1700000000,'currency':'usd',
                 'status':'paid','amount_paid':1500,'livemode':False}
        payment={'id':'inpay_unit','amount_paid':1500,'created':1700000000}
        charge={'amount_captured':1500,'balance_transaction':{'id':'txn_fee','fee':75,'created':1700000000,'currency':'usd'}}
        refunds=[{'id':'re_success','amount':500,'status':'succeeded','created':1700000010,
                  'balance_transaction':{'id':'txn_refund','fee':0,'created':1700000010,'currency':'usd'}},
                 {'id':'re_fail','amount':500,'status':'failed','created':1700000020}]
        with patch('owner_dashboard.database',db),patch('owner_dashboard.paid_charges',return_value=[(payment,{},charge,refunds)]):
            admin.sync_invoice(MagicMock(),invoice,'member@example.com')
        calls=db.return_value.__enter__.return_value.execute.call_args_list
        cash=[call.args[1] for call in calls if 'INSERT INTO pikbot_cash_events' in call.args[0]]
        self.assertIn(('payment:inpay_unit','in_plan','payment',1500,1700000000,False),cash)
        self.assertIn(('refund:re_success','in_plan','refund',500,1700000010,False),cash)
        self.assertFalse(any(row[0]=='refund:re_fail' for row in cash))
        self.assertTrue(all(row[-1] is False for row in cash))

    def test_owner_login_redirects_to_private_dashboard(self):
        self.owner_session()
        db=MagicMock()
        db.return_value.__enter__.return_value.execute.return_value.fetchone.return_value={'email':'owner@example.com','password_hash':'unit-hash'}
        with patch.dict(os.environ,{'PIKBOT_SIGNUP_TEST_CODE':'unit-owner-code'}), \
             patch('membership.database',db),patch('membership.allow_attempt',return_value=True), \
             patch('membership.owner_account',return_value=True),patch('membership.check_password_hash',return_value=True):
            response=self.client.post('/owner/login',data={'csrf':'unit-csrf','email':'owner@example.com',
                'password':'unit-password-123','test_code':'unit-owner-code','action':'login'})
            self.assertEqual(response.status_code,302)
            self.assertEqual(response.headers['Location'],'/owner')

    def test_dashboard_reports_collections_refunds_and_bills_separately(self):
        db=MagicMock()
        def execute(sql,params=()):
            cursor=MagicMock()
            cursor.fetchall.return_value=[]
            if 'COUNT(*) AS total' in sql:
                cursor.fetchone.return_value={'total':3,'trial':2,'paid_last_synced':1}
            elif 'SELECT kind,SUM' in sql:
                self.assertIs(params[-1],False)
                cursor.fetchall.return_value=[{'kind':'payment','cents':3000},{'kind':'refund','cents':500},{'kind':'fee','cents':150}]
            elif 'WHERE NOT fees_complete' in sql:
                cursor.fetchone.return_value={'count':0}
            elif 'AS outstanding' in sql:
                cursor.fetchone.return_value={'paid':400,'outstanding':600,'due':200}
            elif 'SELECT * FROM pikbot_owner_sync' in sql:
                cursor.fetchone.return_value={'livemode':False,'last_finished':None,'complete':False,'account_balance':None}
            return cursor
        db.return_value.__enter__.return_value.execute.side_effect=execute
        with patch('membership.account',return_value={'email':'owner@example.com'}), \
             patch('membership.private_paid_test',return_value=True),patch('owner_dashboard.database',db):
            response=self.client.get('/api/owner/dashboard?month=2026-11')
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['revenue']['net_after_known_fees'],2350)
        self.assertEqual(response.json['expenses']['paid'],400)
        self.assertEqual(response.json['expenses']['due'],200)


if __name__=='__main__':
    unittest.main()
