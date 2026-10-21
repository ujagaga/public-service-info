"""Offline regression tests; isolated settings, temporary SQLite, mocked OAuth/SMTP/HTTP."""
import datetime
import io
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qsl
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
settings = types.ModuleType('appsettings')
exec((ROOT / 'appsettings.py.example').read_text(), settings.__dict__)
initial_db = tempfile.TemporaryDirectory()
settings.DB_NAME = str(Path(initial_db.name) / 'initial.db')
sys.modules['appsettings'] = settings
# Skip loading real OAuth credentials during module initialization only.
with patch.dict(os.environ, {'FLASK_DEBUG': '1'}):
    import index
import checker
import database
import helper
index.application.config.update(TESTING=True, DEBUG=False, WTF_CSRF_ENABLED=False)


def hold_check_lock(started, release):
    def held_run(today, unsubscribe_url):
        started.set()
        release.wait(10)
        return {}
    with patch.object(checker, '_run_checks', side_effect=held_run):
        checker.run_checks()


class RegressionTests(unittest.TestCase):
    real_send_email = staticmethod(helper.send_email)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(database, 'db_path', str(Path(self.temp.name) / 'test.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        database.setup_initial_db()
        self.client = index.application.test_client()
        self.mail = self.enterContext(patch.object(helper, 'send_email'))
        # Unexpected real network calls fail rather than contact external services.
        self.enterContext(patch.object(checker.httpx, 'get', side_effect=AssertionError('Unexpected HTTP GET')))
        self.enterContext(patch.object(checker.httpx, 'post', side_effect=AssertionError('Unexpected HTTP POST')))
        self.tomorrow = datetime.datetime.now(ZoneInfo('Europe/Belgrade')).date() + datetime.timedelta(days=1)

    def db(self):
        conn = database.open_db()
        self.addCleanup(conn.close)
        return conn

    def user(self, email='user@example.com', authorized=0, token=None, approval='approve'):
        conn = self.db()
        database.add_user(conn, email, token, approval_token=approval)
        database.update_user(conn, email, authorized=authorized)
        database.set_subscription(conn, email, 'Test 12', [email])
        return email

    def oauth(self, email, verified=True):
        google = Mock()
        google.get.return_value.json.return_value = {'email': email, 'email_verified': verified}
        with patch.object(index, 'google', google):
            return self.client.get('/oauth2callback')

    def test_keyless_address_picker_is_available_on_home(self):
        self.user(authorized=1, token='login')
        self.client.set_cookie('token', 'login')
        response = self.client.get('/')
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('data-address-picker', html)
        self.assertIn('data-map-search', html)
        self.assertIn('data-map-frame', html)
        self.assertIn('js/address_picker.js', html)
        self.assertIn('css/address_picker.css', html)
        self.assertNotIn('data-maps-key', html)
        self.assertNotIn('maps.googleapis.com', html)
        self.assertNotIn(settings.GEMINI_API_KEY, html)

    def test_admin_home_is_user_management_without_nav_link(self):
        self.user(authorized=2, token='admin-test')
        self.client.set_cookie('token', 'admin-test')
        self.assertTrue(self.client.get('/').location.endswith('/manage_users'))
        html = self.client.get('/manage_users').get_data(as_text=True)
        self.assertNotIn('Korisnici', html)
        self.user(email='other@example.com', authorized=1)
        html = self.client.get('/manage_users').get_data(as_text=True)
        approved = html.split('id="approved-title"')[1]
        self.assertIn('other@example.com', approved)
        self.assertNotIn('user@example.com', approved)

    def test_account_manages_multiple_addresses_with_recipients(self):
        email = self.user(authorized=1, token='login')
        self.client.set_cookie('token', 'login')
        html = self.client.get('/').get_data(as_text=True)
        self.assertNotIn('data-map-link', html)
        response = self.client.post('/', data={'address': ' Manual address 12 ',
                                               'emails': 'a@example.com, b@example.com\na@example.com'})
        self.assertEqual(response.status_code, 302)
        self.client.post('/', data={'address': 'Test 12', 'emails': 'tenant@example.com'})
        self.assertEqual(database.get_subscriptions(self.db(), email),
                         {'Manual address 12': ['a@example.com', 'b@example.com'],
                          'Test 12': ['tenant@example.com']})
        html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Manual address 12', html)
        self.assertIn('tenant@example.com', html)
        self.client.post('/', data={'address': 'Test 12', 'action': 'remove'})
        for emails in ('', 'not-an-email', 'ok@example.com bad@'):
            self.client.post('/', data={'address': 'Other 1', 'emails': emails})
        self.assertEqual(list(database.get_subscriptions(self.db(), email)), ['Manual address 12'])
        database.delete_user(self.db(), email)
        self.assertEqual(database.get_subscriptions(self.db()), {})

    def test_new_google_user_is_registered_without_address_and_admin_notified(self):
        self.assertTrue(self.oauth('verified@example.com').location.endswith('/login'))
        user = database.get_user(self.db(), email='verified@example.com')
        self.assertEqual(user['authorized'], 0)
        self.assertIsNone(user['token'])
        self.assertTrue(user['approval_token'])
        self.assertEqual(database.get_subscriptions(self.db(), 'verified@example.com'), {})
        self.mail.assert_called_once()
        self.assertEqual(self.mail.call_args.kwargs['recipient'], settings.ADMIN_EMAIL)
        self.assertIn('verified@example.com', self.mail.call_args.kwargs['body'])
        self.oauth('verified@example.com')
        self.mail.assert_called_once()

    def test_unverified_google_is_not_registered(self):
        self.assertTrue(self.oauth('bad@example.com', verified=False).location.endswith('/login'))
        self.assertIsNone(database.get_user(self.db(), email='bad@example.com'))
        self.mail.assert_not_called()

    def test_duplicate_add_user_preserves_account_and_token(self):
        email = self.user(authorized=2, token='existing')
        self.assertFalse(database.add_user(self.db(), email, 'other'))
        user = database.get_user(self.db(), email=email)
        self.assertEqual((user['authorized'], user['token']), (2, 'existing'))

    def test_pending_user_cannot_access_or_update_home_even_with_login_token(self):
        email = self.user(token='pending-login')
        self.client.set_cookie('token', 'pending-login')
        for response in (self.client.get('/'),
                         self.client.post('/', data={'address': 'Replacement', 'emails': email})):
            self.assertTrue(response.location.endswith('/login'))
        self.assertEqual(database.get_subscriptions(self.db(), email), {'Test 12': [email]})

    def test_pending_oauth_does_not_rotate_approval_or_issue_login_token(self):
        email = self.user()
        self.oauth(email)
        user = database.get_user(self.db(), email=email)
        self.assertEqual(user['approval_token'], 'approve')
        self.assertIsNone(user['token'])

    def test_approval_link_consumes_separate_token_once(self):
        email = self.user(token='legacy-login')
        self.assertEqual(self.client.get('/approve_user', query_string={'email': email, 'token': 'legacy-login'}).status_code, 403)
        self.assertEqual(self.client.get('/approve_user', query_string={'email': email, 'token': 'approve'}).status_code, 200)
        user = database.get_user(self.db(), email=email)
        self.assertEqual(user['authorized'], 1)
        self.assertIsNone(user['token'])
        self.assertIsNone(user['approval_token'])
        self.assertEqual(self.client.get('/approve_user', query_string={'email': email, 'token': 'approve'}).status_code, 403)
        self.mail.assert_called_once()

    def test_admin_approval_matches_email_approval_and_cannot_demote_admin(self):
        email = self.user()
        database.update_user(self.db(), settings.ADMIN_EMAIL, token='admin-login')
        self.client.set_cookie('token', 'admin-login')
        for _ in range(2):
            self.client.post('/manage_users', data={'email': email, 'action': 'authorize'})
        user = database.get_user(self.db(), email=email)
        self.assertEqual(user['authorized'], 1)
        self.assertIsNone(user['approval_token'])
        self.mail.assert_called_once()
        self.client.post('/manage_users', data={'email': settings.ADMIN_EMAIL, 'action': 'authorize'})
        self.assertEqual(database.get_user(self.db(), email=settings.ADMIN_EMAIL)['authorized'], 2)

    def test_subscription_start_moves_late_days_to_next_month(self):
        start = database.subscription_start
        self.assertEqual(start(datetime.date(2026, 1, 28)), datetime.date(2026, 1, 28))
        self.assertEqual(start(datetime.date(2026, 1, 29)), datetime.date(2026, 2, 1))
        self.assertEqual(start(datetime.date(2026, 4, 30)), datetime.date(2026, 5, 1))
        self.assertEqual(start(datetime.date(2026, 12, 31)), datetime.date(2027, 1, 1))
        self.assertEqual(database.next_due('2026-11-15', 2), datetime.date(2027, 2, 15))
        self.assertEqual(database.missed_payments('2026-01-10', 0, datetime.date(2026, 2, 9)), 0)
        self.assertEqual(database.missed_payments('2026-01-10', 0, datetime.date(2026, 3, 10)), 2)
        self.assertEqual(database.missed_payments('2026-01-10', 1, datetime.date(2026, 3, 10)), 1)

    def test_approval_sets_subscription_date_and_admin_records_payments(self):
        email = self.user()
        database.update_user(self.db(), settings.ADMIN_EMAIL, token='admin-login')
        self.client.set_cookie('token', 'admin-login')
        with patch.object(index, 'today', return_value=datetime.date(2026, 1, 30)):
            self.client.post('/manage_users', data={'email': email, 'action': 'authorize'})
        self.assertEqual(database.get_user(self.db(), email=email)['subscribed_on'], '2026-02-01')
        self.client.post('/manage_users', data={'email': email, 'action': 'set_subscribed_on', 'subscribed_on': '31.03.2026'})
        self.assertEqual(database.get_user(self.db(), email=email)['subscribed_on'], '2026-04-01')
        self.client.post('/manage_users', data={'email': email, 'action': 'add_payment', 'amount': '1500,50', 'paid_on': '2.5.2026'})
        for bad in ({'amount': '0', 'paid_on': '02.05.2026'}, {'amount': 'x', 'paid_on': '02.05.2026'}, {'amount': '10', 'paid_on': ''}, {'amount': '10', 'paid_on': '2026-05-02'}):
            self.client.post('/manage_users', data={'email': email, 'action': 'add_payment', **bad})
        [payment] = database.get_payments(self.db())[email]
        self.assertEqual((payment['amount'], payment['paid_on']), (1500.5, '2026-05-02'))
        html = self.client.get('/manage_users').get_data(as_text=True)
        self.assertIn('od 01.04.2026 · sledeća uplata 01.06.2026', html)
        self.assertIn('value="1500.50"', html)
        self.assertIn('value="02.05.2026"', html)
        self.assertNotIn('type="date" name=', html)
        edit = {'email': email, 'payment_id': payment['id'], 'action': 'update_payment', 'amount': '2000', 'paid_on': '03.05.2026'}
        self.client.post('/manage_users', data=dict(edit, email='admin2@example.com'))
        self.client.post('/manage_users', data=dict(edit, amount='-1'))
        self.assertEqual(database.get_payments(self.db())[email][0]['amount'], 1500.5)
        self.client.post('/manage_users', data=edit)
        self.assertEqual(database.get_payments(self.db())[email][0], {'id': payment['id'], 'amount': 2000, 'paid_on': '2026-05-03'})
        self.client.post('/manage_users', data={'email': 'other@example.com', 'payment_id': payment['id'], 'action': 'delete_payment'})
        self.assertIn(email, database.get_payments(self.db()))
        self.client.post('/manage_users', data={'email': email, 'payment_id': payment['id'], 'action': 'delete_payment'})
        self.assertEqual(database.get_payments(self.db()), {})
        database.add_payment(self.db(), email, 1, datetime.date(2026, 5, 1))
        database.delete_user(self.db(), email)
        self.assertEqual(database.get_payments(self.db()), {})

    def test_home_shows_subscription_status_to_regular_users_only(self):
        email = self.user(authorized=1, token='login')
        self.client.set_cookie('token', 'login')
        self.assertIn('Datum pretplate još nije postavljen', self.client.get('/').get_data(as_text=True))
        database.set_subscribed_on(self.db(), email, datetime.date(2026, 1, 10))
        database.add_payment(self.db(), email, 1000, datetime.date(2026, 2, 10))
        with patch.object(index, 'today', return_value=datetime.date(2026, 4, 10)):
            html = self.client.get('/').get_data(as_text=True)
        self.assertIn('Aktivna od 10.01.2026 · sledeća uplata 10.03.2026', html)
        self.assertIn('Dospelo · neplaćeno: 2', html)
        self.assertNotIn('Šta pratimo za vas', html)
        database.update_user(self.db(), email, authorized=2)
        self.assertTrue(self.client.get('/').location.endswith('/manage_users'))

    def test_monthly_fee_is_price_per_address_times_address_count(self):
        email = self.user(authorized=1, token='login')
        database.set_subscription(self.db(), email, 'Test 24', ['x@example.com', 'y@example.com'])
        database.set_subscribed_on(self.db(), email, datetime.date(2026, 1, 10))
        self.assertIsNone(database.monthly_fee(self.db(), email))
        database.update_user(self.db(), settings.ADMIN_EMAIL, token='admin-login')
        self.client.set_cookie('token', 'admin-login')
        for bad in ('-1', 'x'):
            self.client.post('/manage_users', data={'email': email, 'action': 'set_price', 'price': bad})
        self.assertIsNone(database.monthly_fee(self.db(), email))
        self.client.post('/manage_users', data={'email': email, 'action': 'set_price', 'price': '750000,5'})
        self.assertEqual(database.monthly_fee(self.db(), email), 1500001)
        with patch.object(index, 'today', return_value=datetime.date(2026, 4, 10)):
            admin_html = self.client.get('/manage_users').get_data(as_text=True)
            self.client.set_cookie('token', 'login')
            home_html = self.client.get('/').get_data(as_text=True)
            self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN})
        self.assertIn('RSD × 2 adr. = 1500001 RSD', admin_html)
        self.assertIn('Dospelo · neplaćeno: 3 (4500003 RSD)', admin_html)
        self.assertIn('Dospelo · neplaćeno: 3 (4500003 RSD)', home_html)
        self.assertIn('Mesečno: 1500001 RSD (2 adr.)', home_html)
        self.assertIn(f'{email}: 10.02.2026 - 1500001 RSD', self.mail.call_args.kwargs['body'])

    def test_payment_due_reminder_sent_once_per_due_date_and_retried_on_failure(self):
        email = self.user(authorized=1)
        database.set_subscription(self.db(), email, 'Test 12', [])
        database.set_subscribed_on(self.db(), email, datetime.date(2026, 1, 10))
        self.user(email='admin2@example.com', authorized=2)
        query = {'token': settings.TRIGGER_TOKEN}
        with patch.object(index, 'today', return_value=datetime.date(2026, 3, 10)):
            self.mail.side_effect = RuntimeError('SMTP down')
            self.client.get('/run_check', query_string=query)
            self.mail.reset_mock(side_effect=True)
            self.client.get('/run_check', query_string=query)
            self.assertEqual({c.kwargs['recipient'] for c in self.mail.call_args_list}, {settings.ADMIN_EMAIL, 'admin2@example.com'})
            body = self.mail.call_args.kwargs['body']
            self.assertIn(f'{email}: 10.02.2026', body)
            self.assertIn(f'{email}: 10.03.2026', body)
            self.mail.reset_mock()
            self.client.get('/run_check', query_string=query)
            self.mail.assert_not_called()
        database.add_payment(self.db(), email, 1000, datetime.date(2026, 3, 11))
        database.add_payment(self.db(), email, 1000, datetime.date(2026, 3, 11))
        with patch.object(index, 'today', return_value=datetime.date(2026, 4, 10)):
            self.client.get('/run_check', query_string=query)
        self.assertEqual(self.mail.call_count, 2)
        self.assertNotIn('03.2026', self.mail.call_args.kwargs['body'])
        self.assertIn(f'{email}: 10.04.2026', self.mail.call_args.kwargs['body'])

    def test_login_cookie_and_logout_revocation(self):
        email = self.user(authorized=1)
        response = self.oauth(email)
        cookie = next(value for value in response.headers.getlist('Set-Cookie') if value.startswith('token='))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.client.get('/logout')
        self.assertIsNone(database.get_user(self.db(), email=email)['token'])

    def test_home_keeps_csrf_protection(self):
        email = self.user(authorized=1, token='login')
        self.client.set_cookie('token', 'login')
        with patch.dict(index.application.config, WTF_CSRF_ENABLED=True):
            self.assertEqual(self.client.post('/', data={'address': 'Test', 'emails': email}).status_code, 400)
        self.assertEqual(list(database.get_subscriptions(self.db(), email)), ['Test 12'])

    def test_old_database_migration_preserves_links_and_revokes_legacy_logins(self):
        path = str(Path(self.temp.name) / 'legacy.db')
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE users (email TEXT UNIQUE, token TEXT UNIQUE, authorized INTEGER, address TEXT, picture TEXT, last_seen TEXT)')
        conn.executemany('INSERT INTO users (email,token,authorized,address) VALUES (?,?,?,?)',
                         [('pending@example.com', 'old-approval', 0, None), ('admin@example.com', 'old-login', 2, ' Old 1 ')])
        conn.commit()
        database.init_database(conn)
        database.init_database(conn)
        self.assertEqual(conn.execute('SELECT token,approval_token FROM users WHERE authorized=0').fetchone(), (None, 'old-approval'))
        self.assertEqual(conn.execute('SELECT token,approval_token FROM users WHERE authorized=2').fetchone(), (None, None))
        conn.execute("UPDATE users SET token='new-login' WHERE authorized=2")
        conn.commit()
        database.init_database(conn)
        self.assertEqual(conn.execute('SELECT token FROM users WHERE authorized=2').fetchone(), ('new-login',))
        self.assertEqual(conn.execute('SELECT * FROM subscriptions').fetchall(),
                         [('admin@example.com', 'Old 1', 'admin@example.com')])

    def model_response(self, data):
        response = Mock()
        response.json.return_value = {'candidates': [{'content': {'parts': [{'text': json.dumps(data)}]}}]}
        return response

    def test_model_schema_rejects_missing_or_wrong_types(self):
        good = {'address_id': 'a1', 'outage': False, 'details': ''}
        invalid = [[], None, {}, {'date_matches': 'true', 'results': []},
                   {'date_matches': True, 'results': {}},
                   {'date_matches': True, 'results': [None]}]
        invalid += [{'date_matches': True, 'results': [dict(good, **change)]}
                    for change in [{'outage': 'false'}, {'outage': 0}, {'details': 12}, {'address_id': 1}]]
        invalid.append({'date_matches': True, 'results': [{'outage': False, 'details': ''}]})
        for data in invalid:
            with self.subTest(data=data), patch.object(checker.httpx, 'post', return_value=self.model_response(data)):
                with self.assertRaises(ValueError):
                    checker.ask('test', checker.OUTAGE_SCHEMA)
        with patch.object(checker.httpx, 'post', return_value=self.model_response({'date_matches': True, 'results': [good]})):
            self.assertIs(checker.ask('test', checker.OUTAGE_SCHEMA)['results'][0]['outage'], False)

    def test_water_selection_requires_exact_url_or_exact_no_result(self):
        url = 'https://www.vikns.rs/najava-radova-sr/notice/'
        html = f"<a href='{url}'><strong>Radovi</strong></a>"
        for answer in ['https://www.vikns.rs', url[:-1], 'unknown', 'NEMA extra']:
            with self.subTest(answer=answer), patch.object(helper, 'fetch_html', return_value=html) as fetch, patch.object(checker, 'ask', return_value=answer):
                with self.assertRaises(ValueError):
                    checker.find_water_page(self.tomorrow)
                fetch.assert_called_once()
        with patch.object(helper, 'fetch_html', side_effect=[html, 'selected page']), patch.object(checker, 'ask', return_value=url):
            self.assertEqual(checker.find_water_page(self.tomorrow), 'selected page')
        with patch.object(helper, 'fetch_html', return_value=html), patch.object(checker, 'ask', return_value='NEMA'):
            self.assertIsNone(checker.find_water_page(self.tomorrow))
        self.assertEqual(checker.extract_links("<a href='/najava-radova-sr/test/'>OK</a><a href='https://evil.example/najava-radova-sr/test/'>Bad</a>"),
                         {'https://www.vikns.rs/najava-radova-sr/test/': 'OK'})

    def test_power_date_must_match_heading(self):
        html = f'<h1>Планирана искључења за датум: <b>{self.tomorrow:%d.%m.%Y}.</b></h1>'
        checker.validate_power_date(html, self.tomorrow)
        for invalid in ['No announcement date', 'за датум: 01.01.2000. ' + html,
                        f'<script>за датум: {self.tomorrow:%d.%m.%Y}</script>']:
            with self.assertRaises(ValueError):
                checker.validate_power_date(invalid, self.tomorrow)

    def test_address_matching_includes_target_date_and_rejects_wrong_date(self):
        with patch.object(checker, 'ask', return_value={'date_matches': False, 'results': []}) as ask:
            with self.assertRaises(ValueError):
                checker.check_addresses('page', ['Test 12'], 'voda', self.tomorrow)
            self.assertIn(self.tomorrow.strftime('%d.%m.%Y'), ask.call_args.args[0])

    def setup_run(self):
        self.user(authorized=1)
        self.enterContext(patch.object(helper, 'fetch_html', return_value=f'за датум: {self.tomorrow:%d.%m.%Y}'))
        self.enterContext(patch.object(checker, 'find_water_page', return_value='water page'))

    def test_retry_only_sends_services_that_were_not_delivered(self):
        self.setup_run()
        def match(html, addresses, service, date):
            if service == 'voda':
                raise ValueError('water failed')
            return {address: {'outage': True, 'details': '8–10'} for address in addresses}
        with patch.object(checker, 'check_addresses', side_effect=match):
            first = checker.run_checks()
        self.assertTrue(first['errors'])
        self.assertEqual(self.mail.call_count, 1)
        self.assertIn('Struja', self.mail.call_args.kwargs['body'])
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '9–11'}}) as match:
            second = checker.run_checks()
            self.assertEqual(match.call_count, 1)
            self.assertEqual(match.call_args.args[2], 'voda')
        self.assertFalse(second['errors'])
        self.assertEqual(self.mail.call_count, 2)
        self.assertNotIn('Struja', self.mail.call_args.kwargs['body'])
        self.assertTrue(checker.run_checks()['skipped'])
        self.assertEqual(self.mail.call_count, 2)

    def test_retry_does_not_resend_to_successful_user(self):
        self.setup_run()
        self.user(email='second@example.com', authorized=1)
        self.mail.side_effect = [None, RuntimeError('SMTP failed'), None]
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            self.assertTrue(checker.run_checks()['errors'])
            self.assertFalse(checker.run_checks()['errors'])
        self.assertEqual([c.kwargs['recipient'] for c in self.mail.call_args_list],
                         ['second@example.com', 'user@example.com', 'user@example.com'])

    def test_bulk_matches_ids_in_any_order_and_sends_unique_address_list(self):
        data = {'date_matches': True, 'results': [
            {'address_id': 'a2', 'outage': True, 'details': '10–12'},
            {'address_id': 'a1', 'outage': False, 'details': ''},
        ]}
        with patch.object(checker.httpx, 'post', return_value=self.model_response(data)) as post:
            matches = checker.check_addresses('announcement', ['Test 12', 'Test 24', 'Test 12'], 'struja', self.tomorrow)
        self.assertEqual(matches, {'Test 12': {'outage': False, 'details': ''},
                                   'Test 24': {'outage': True, 'details': '10–12'}})
        post.assert_called_once()
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['generationConfig']['responseSchema'], checker.OUTAGE_SCHEMA)
        prompt = payload['contents'][0]['parts'][0]['text']
        listing = json.loads(prompt.split('ADRESE (JSON):\n')[1].split('\n\nOBJAVA (HTML):')[0])
        self.assertEqual(listing, [{'address_id': 'a1', 'address': 'Test 12'},
                                   {'address_id': 'a2', 'address': 'Test 24'}])

    def test_bulk_rejects_missing_duplicate_unknown_ids_and_empty_outage_details(self):
        first = {'address_id': 'a1', 'outage': False, 'details': ''}
        second = {'address_id': 'a2', 'outage': True, 'details': '8–10'}
        for entries in [[], [first], [first, first], [first, dict(second, address_id='unknown')],
                        [first, dict(second, details='  ')], [first, second, dict(first, address_id='a3')]]:
            with self.subTest(entries=entries), patch.object(checker.httpx, 'post', return_value=self.model_response(
                    {'date_matches': True, 'results': entries})):
                with self.assertRaises(ValueError):
                    checker.check_addresses('page', ['Test 12', 'Test 24'], 'struja', self.tomorrow)

    def test_bulk_checks_shared_addresses_once_and_routes_results_to_users(self):
        self.setup_run()
        self.user(email='neighbor@example.com', authorized=1)
        self.user(email='other@example.com', authorized=1)
        database.set_subscription(self.db(), 'other@example.com', 'Test 12', [])
        database.set_subscription(self.db(), 'other@example.com', 'Test 24', ['other@example.com'])
        self.user(email='pending@example.com', authorized=0)
        self.user(email='blank@example.com', authorized=1)
        database.set_subscription(self.db(), 'blank@example.com', 'Test 12', [])
        power = {'date_matches': True, 'results': [
            {'address_id': 'a2', 'outage': False, 'details': ''},
            {'address_id': 'a1', 'outage': True, 'details': '8–10'},
        ]}
        water = {'date_matches': True, 'results': [
            {'address_id': 'a1', 'outage': False, 'details': ''},
            {'address_id': 'a2', 'outage': True, 'details': '12–14'},
        ]}
        with patch.object(checker.httpx, 'post', side_effect=[self.model_response(power), self.model_response(water)]) as post:
            report = checker.run_checks()
        self.assertFalse(report['errors'])
        self.assertEqual(report['checked'], 3)
        self.assertEqual(post.call_count, 2)
        for call in post.call_args_list:
            prompt = call.kwargs['json']['contents'][0]['parts'][0]['text']
            self.assertEqual(prompt.count('"address": "Test 12"'), 1)
            self.assertEqual(prompt.count('"address": "Test 24"'), 1)
            self.assertNotIn('@example.com', prompt)
        mails = {call.kwargs['recipient']: call.kwargs['body'] for call in self.mail.call_args_list}
        self.assertEqual(set(mails), {'user@example.com', 'neighbor@example.com', 'other@example.com'})
        self.assertEqual(mails['user@example.com'], mails['neighbor@example.com'])
        self.assertIn('Struja: 8–10', mails['user@example.com'])
        self.assertNotIn('Voda:', mails['user@example.com'])
        self.assertIn('Voda: 12–14', mails['other@example.com'])
        self.assertNotIn('Struja:', mails['other@example.com'])

    def test_notification_unsubscribe_link_removes_only_that_address(self):
        self.setup_run()
        database.set_subscription(self.db(), 'user@example.com', 'Test 24', ['user@example.com'])
        self.user(email='manager@example.com', authorized=1)
        database.set_subscription(self.db(), 'manager@example.com', 'Test 12', ['manager@example.com', 'user@example.com'])
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN})
        body = self.mail.call_args.kwargs['body']
        link = body.split('Kliknite ovde da se odjavite sa obaveštenja za ovu adresu: http://localhost')[1].strip()
        self.assertEqual(self.client.get(link.replace('token=', 'token=x')).status_code, 403)
        self.assertEqual(self.client.get(link).status_code, 200)
        self.assertIn('Test 12', database.get_subscriptions(self.db(), 'manager@example.com'))
        path, query = link.split('?')
        self.assertIn('Odjavljeni ste', self.client.post(path, data=dict(parse_qsl(query))).get_data(as_text=True))
        self.assertEqual(database.get_subscriptions(self.db(), 'user@example.com'), {'Test 24': ['user@example.com']})
        self.assertEqual(database.get_subscriptions(self.db(), 'manager@example.com'), {'Test 12': ['manager@example.com']})

    def test_account_signature_is_appended_to_notifications(self):
        self.setup_run()
        self.client.set_cookie('token', 'login')
        database.update_user(self.db(), 'user@example.com', token='login')
        self.client.post('/', data={'action': 'signature', 'signature': ' Upravnik zgrade\n060 123 '})
        self.assertIn('Upravnik zgrade', self.client.get('/').get_data(as_text=True))
        self.user(email='manager@example.com', authorized=1)
        database.set_subscription(self.db(), 'manager@example.com', 'Test 12', ['user@example.com'])
        database.update_user(self.db(), 'manager@example.com', signature='Drugi upravnik')
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN})
        body = self.mail.call_args.kwargs['body']
        self.assertEqual(self.mail.call_count, 1)
        self.assertIn('Voda: 8–10\n\n--\nDrugi upravnik\n\n--\nUpravnik zgrade\n060 123\n\nKliknite ovde', body)
        self.assertIn('Kliknite ovde da se odjavite', self.mail.call_args.kwargs['html'])
        self.assertEqual(self.mail.call_args.kwargs['images'], [])

    def test_logo_upload_validation_delete_and_inline_mail(self):
        self.setup_run()
        database.update_user(self.db(), 'user@example.com', token='login')
        self.client.set_cookie('token', 'login')
        png = b'\x89PNG\r\n\x1a\n' + b'0' * 100
        logo = lambda: database.get_user(self.db(), email='user@example.com')['logo']
        for data, name in [(b'GIF89a...', 'fake.png'), (b'\xff\xd8\xff' + b'0' * (500 * 1024), 'big.jpg')]:
            self.client.post('/', data={'logo': (io.BytesIO(data), name)}, content_type='multipart/form-data')
            self.assertIsNone(logo())
        self.client.post('/', data={'logo': (io.BytesIO(png), 'logo.png')}, content_type='multipart/form-data')
        self.assertTrue(logo().startswith('data:image/png;base64,'))
        self.assertIn(logo(), self.client.get('/').get_data(as_text=True))
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN})
        kwargs = self.mail.call_args.kwargs
        self.assertEqual(kwargs['images'], [('logo0', png, 'png')])
        self.assertRegex(kwargs['html'], r'<tr><td[^>]*><img src="cid:logo0"[^>]*></td><td[^>]*></td></tr>')
        self.assertIn('<a href="http://localhost/unsubscribe?', kwargs['html'])
        self.assertIn('Kliknite ovde da se odjavite sa obaveštenja za ovu adresu: http://localhost/unsubscribe?', kwargs['body'])
        self.client.post('/', data={'action': 'logo_delete'})
        self.assertIsNone(logo())

    def test_html_email_inlines_images(self):
        with patch.object(helper, 'send_email', self.real_send_email), \
             patch.object(helper.smtplib, 'SMTP') as smtp, patch.object(settings, 'SMTP_PASS', 'x', create=True):
            helper.send_email('a@example.com', 'Subject', 'plain', html='<img src="cid:logo0">',
                              images=[('logo0', b'\x89PNG\r\n\x1a\n', 'png')])
        raw = smtp.return_value.__enter__.return_value.sendmail.call_args.args[2]
        self.assertIn('multipart/related', raw)
        self.assertIn('multipart/alternative', raw)
        self.assertIn('Content-ID: <logo0>', raw)
        self.assertIn('Content-Type: image/png', raw)

    def test_invalid_bulk_service_response_does_not_prevent_other_service_delivery(self):
        self.setup_run()
        missing = {'date_matches': True, 'results': []}
        good = {'date_matches': True, 'results': [{'address_id': 'a1', 'outage': True, 'details': '8–10'}]}
        with patch.object(checker.httpx, 'post', side_effect=[self.model_response(missing), self.model_response(good)]):
            report = checker.run_checks()
        self.assertEqual(len(report['errors']), 1)
        self.mail.assert_called_once()
        self.assertIn('Voda:', self.mail.call_args.kwargs['body'])
        self.assertNotIn('Struja:', self.mail.call_args.kwargs['body'])
        self.assertFalse(database.check_done(self.db(), str(self.tomorrow - datetime.timedelta(days=1))))

    def test_recipients_are_fetched_after_bulk_results(self):
        self.setup_run()
        def check(html, addresses, service, date):
            if service == 'struja':
                database.delete_user(self.db(), 'user@example.com')
                self.user(email='new-neighbor@example.com', authorized=1)
            return {address: {'outage': True, 'details': '8–10'} for address in addresses}
        with patch.object(checker, 'check_addresses', side_effect=check):
            report = checker.run_checks()
        self.assertFalse(report['errors'])
        self.mail.assert_called_once()
        self.assertEqual(self.mail.call_args.kwargs['recipient'], 'new-neighbor@example.com')

    def test_address_changed_during_check_is_deferred_to_next_day(self):
        self.setup_run()
        def check(html, addresses, service, date):
            database.set_subscription(self.db(), 'user@example.com', 'Test 12', [])
            database.set_subscription(self.db(), 'user@example.com', 'New 24', ['user@example.com'])
            return {address: {'outage': True, 'details': '8–10'} for address in addresses}
        with patch.object(checker, 'check_addresses', side_effect=check):
            report = checker.run_checks()
        self.assertFalse(report['errors'])
        self.mail.assert_not_called()
        self.assertTrue(database.check_done(self.db(), str(self.tomorrow - datetime.timedelta(days=1))))

    def test_no_affected_addresses_sends_no_email_and_completes_run(self):
        self.setup_run()
        data = {'date_matches': True, 'results': [{'address_id': 'a1', 'outage': False, 'details': ''}]}
        with patch.object(checker.httpx, 'post', return_value=self.model_response(data)):
            report = checker.run_checks()
        self.assertFalse(report['errors'])
        self.mail.assert_not_called()
        self.assertTrue(database.check_done(self.db(), str(self.tomorrow - datetime.timedelta(days=1))))

    def test_no_eligible_addresses_skips_google_and_notifications(self):
        with patch.object(checker, 'ask') as ask, patch.object(helper, 'fetch_html') as fetch:
            report = checker.run_checks()
        self.assertEqual(report['checked'], 0)
        ask.assert_not_called()
        fetch.assert_not_called()
        self.mail.assert_not_called()

    def test_four_url_triggers_only_retry_failed_emails_after_successful_check(self):
        self.setup_run()
        self.user(email='other@example.com', authorized=1)
        failures = {'user@example.com': 1}
        def send(recipient, subject, body, **kwargs):
            # Analysis success is already durable when SMTP starts.
            self.assertTrue(database.check_done(self.db(), str(self.tomorrow - datetime.timedelta(days=1))))
            if failures.get(recipient, 0):
                failures[recipient] -= 1
                raise RuntimeError('SMTP temporarily unavailable')
        self.mail.side_effect = send
        query = {'token': settings.TRIGGER_TOKEN}
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}) as match:
            first = self.client.get('/run_check', query_string=query)
            self.assertEqual(match.call_count, 2)
        self.assertEqual(first.status_code, 500)
        self.assertIn('Provera zavrsena: da', first.get_data(as_text=True))
        self.assertIn('Preostalih mejlova za slanje: 1', first.get_data(as_text=True))
        with patch.object(checker, 'check_addresses', side_effect=AssertionError('No repeated analysis')) as match, \
             patch.object(helper, 'fetch_html', side_effect=AssertionError('No repeated fetch')) as fetch, \
             patch.object(checker, 'find_water_page', side_effect=AssertionError('No repeated water lookup')) as water:
            second, third, fourth = [self.client.get('/run_check', query_string=query) for _ in range(3)]
            match.assert_not_called()
            fetch.assert_not_called()
            water.assert_not_called()
        self.assertEqual([r.status_code for r in (second, third, fourth)], [200, 200, 200])
        self.assertIn('sacuvana ranije', second.get_data(as_text=True))
        self.assertIn('Nema mejlova za ponovno slanje', third.get_data(as_text=True))
        recipients = [call.kwargs['recipient'] for call in self.mail.call_args_list]
        self.assertEqual(recipients, ['other@example.com', 'user@example.com', 'user@example.com'])
        retries = [call.kwargs['body'] for call in self.mail.call_args_list if call.kwargs['recipient'] == 'user@example.com']
        self.assertEqual(retries[0], retries[1])
        self.assertEqual(self.db().execute("SELECT COUNT(*) FROM notification_outbox WHERE status='pending'").fetchone()[0], 0)

    def test_failed_email_retry_survives_a_fresh_cgi_process(self):
        self.setup_run()
        self.mail.side_effect = RuntimeError('SMTP failed')
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            first = checker.run_checks()
        self.assertTrue(first['check_complete'])
        self.assertEqual(first['pending'], 1)
        code = """
import json, sys
sys.path.insert(0, 'tests')
import test_regressions as t
from unittest.mock import patch
t.database.db_path = sys.argv[1]
with patch.object(t.helper, 'fetch_html', side_effect=AssertionError('Must not fetch')), \
     patch.object(t.checker, 'ask', side_effect=AssertionError('Must not call Gemini')), \
     patch.object(t.helper, 'send_email') as mail:
    response = t.index.application.test_client().get('/run_check', query_string={'token': t.settings.TRIGGER_TOKEN})
    print(json.dumps({'status': response.status_code, 'body': response.get_data(as_text=True), 'sent': mail.call_count}))
"""
        completed = subprocess.run([sys.executable, '-c', code, database.db_path], cwd=ROOT,
                                   capture_output=True, text=True, timeout=15, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result['status'], 200, result)
        self.assertEqual(result['sent'], 1)
        self.assertIn('sacuvana ranije', result['body'])
        self.assertEqual(len(database.get_pending_notifications(self.db(), str(self.tomorrow - datetime.timedelta(days=1)))), 0)

    def test_crash_before_email_keeps_check_and_outbox_for_next_url_call(self):
        self.setup_run()
        self.mail.side_effect = SystemExit('CGI request terminated')
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            with self.assertRaises(SystemExit):
                checker.run_checks()
        self.assertTrue(database.check_done(self.db(), str(self.tomorrow - datetime.timedelta(days=1))))
        self.mail.side_effect = None
        with patch.object(checker, 'check_addresses') as match, patch.object(helper, 'fetch_html') as fetch:
            retry = checker.run_checks()
        match.assert_not_called()
        fetch.assert_not_called()
        self.assertTrue(retry['check_cached'])
        self.assertEqual(retry['notified'], ['user@example.com'])

    def test_no_announcement_and_no_outages_are_not_rechecked(self):
        self.setup_run()
        with patch.object(checker, 'find_water_page', return_value=None), \
             patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': False, 'details': ''}}):
            first = checker.run_checks()
        self.assertTrue(first['check_complete'])
        with patch.object(checker, 'check_addresses') as match, \
             patch.object(checker, 'find_water_page') as water, patch.object(helper, 'fetch_html') as fetch:
            self.assertTrue(checker.run_checks()['skipped'])
        match.assert_not_called()
        water.assert_not_called()
        fetch.assert_not_called()
        self.mail.assert_not_called()

    def test_successful_service_stays_cached_when_other_service_fails(self):
        self.setup_run()
        with patch.object(checker, 'find_water_page', side_effect=RuntimeError('Water unavailable')), \
             patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            self.assertFalse(checker.run_checks()['check_complete'])
        with patch.object(helper, 'fetch_html') as fetch, \
             patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': False, 'details': ''}}) as match:
            second = checker.run_checks()
        fetch.assert_not_called()
        match.assert_called_once()
        self.assertEqual(match.call_args.args[2], 'voda')
        self.assertTrue(second['check_complete'])
        self.mail.assert_called_once()

    def test_pending_email_is_cancelled_if_user_changes_address(self):
        self.setup_run()
        self.mail.side_effect = RuntimeError('SMTP failed')
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            checker.run_checks()
        database.set_subscription(self.db(), 'user@example.com', 'Test 12', [])
        database.set_subscription(self.db(), 'user@example.com', 'New 24', ['user@example.com'])
        self.mail.reset_mock(side_effect=True)
        with patch.object(checker, 'check_addresses') as match:
            retry = checker.run_checks()
        self.assertTrue(retry['skipped'])
        self.assertEqual(retry['pending'], 0)
        self.mail.assert_not_called()
        match.assert_not_called()

    def test_outbox_creation_and_service_completion_are_atomic(self):
        self.user(authorized=1)
        conn = self.db()
        conn.execute("""CREATE TRIGGER fail_outbox BEFORE INSERT ON notification_outbox
                        BEGIN SELECT RAISE(ABORT, 'test insertion failure'); END""")
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            database.save_service_check(conn, '2026-09-26', '2026-09-27', 'struja',
                                        {'Test 12': {'outage': True, 'details': '8–10'}})
        self.assertEqual(database.get_service_checks(conn, '2026-09-26'), {})
        self.assertEqual(conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 0)

    def test_previous_day_does_not_block_new_day_or_retry_stale_mail(self):
        self.setup_run()
        self.mail.side_effect = RuntimeError('SMTP failed')
        with patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': True, 'details': '8–10'}}):
            checker.run_checks()
        self.mail.reset_mock(side_effect=True)
        next_day = self.tomorrow
        class NextDay(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return cls.combine(next_day, datetime.time(20), tzinfo=tz)
        with patch.object(checker.datetime, 'datetime', NextDay), \
             patch.object(helper, 'fetch_html', return_value=f'за датум: {next_day + datetime.timedelta(days=1):%d.%m.%Y}'), \
             patch.object(checker, 'check_addresses', return_value={'Test 12': {'outage': False, 'details': ''}}) as match:
            result = checker.run_checks()
        self.assertFalse(result['check_cached'])
        self.assertTrue(result['check_complete'])
        self.assertEqual(match.call_count, 2)
        self.mail.assert_not_called()

    def test_legacy_completed_day_does_not_trigger_new_checks(self):
        database.record_check(self.db(), str(self.tomorrow - datetime.timedelta(days=1)))
        with patch.object(helper, 'fetch_html') as fetch, patch.object(checker, 'ask') as ask:
            result = checker.run_checks()
        self.assertTrue(result['skipped'])
        fetch.assert_not_called()
        ask.assert_not_called()

    def test_stale_power_page_is_error_and_not_sent(self):
        self.user(authorized=1)
        with patch.object(helper, 'fetch_html', return_value='за датум: 01.01.2000.'), patch.object(checker, 'find_water_page', return_value=None):
            self.assertTrue(checker.run_checks()['errors'])
        self.mail.assert_not_called()

    def test_concurrent_process_is_rejected_and_lock_is_released(self):
        context = multiprocessing.get_context('fork')
        started, release = context.Event(), context.Event()
        process = context.Process(target=hold_check_lock, args=(started, release))
        process.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaisesRegex(RuntimeError, 'vec u toku'):
                checker.run_checks()
        finally:
            release.set()
            process.join(5)
            if process.is_alive():
                process.terminate()
                process.join()
        self.assertEqual(process.exitcode, 0)
        with patch.object(checker, '_run_checks', return_value={'released': True}):
            self.assertEqual(checker.run_checks(), {'released': True})

    def test_partial_errors_return_http_500_and_success_200(self):
        result = dict(date='27.09.2026', checked=1, notified=[], errors=['failed'], skipped=False)
        with patch.object(checker, 'run_checks', return_value=result):
            self.assertEqual(self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN}).status_code, 500)
            result['errors'] = []
            self.assertEqual(self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN}).status_code, 200)


if __name__ == '__main__':
    unittest.main()
