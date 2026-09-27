"""Offline regression tests; isolated settings, temporary SQLite, mocked OAuth/SMTP/HTTP."""
import datetime
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
settings = types.ModuleType('appsettings')
exec((ROOT / 'appsettings.py.example').read_text(), settings.__dict__)
initial_db = tempfile.TemporaryDirectory()
settings.DB_NAME = str(Path(initial_db.name) / 'initial.db')
sys.modules['appsettings'] = settings
# Skip loading real OAuth credentials during module initialization only.
with patch.dict(os.environ, {'FLASK_DEBUG': '1'}), patch('helper.generate_contact_image'):
    import index
import checker
import database
import helper
index.application.config.update(TESTING=True, DEBUG=False, WTF_CSRF_ENABLED=False)


def hold_check_lock(started, release):
    def held_run(today):
        started.set()
        release.wait(10)
        return {}
    with patch.object(checker, '_run_checks', side_effect=held_run):
        checker.run_checks()


class RegressionTests(unittest.TestCase):
    real_contact_generator = staticmethod(helper.generate_contact_image)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db_patch = patch.object(database, 'db_path', str(Path(self.temp.name) / 'test.db'))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.contact_image = self.enterContext(patch.object(helper, 'generate_contact_image'))
        database.setup_initial_db()
        self.enterContext(patch.object(settings, 'CHECK_START_HOUR', 0))
        self.client = index.application.test_client()
        self.mail = self.enterContext(patch.object(helper, 'send_email'))
        # Unexpected real network calls fail rather than contact external services.
        self.enterContext(patch.object(checker.httpx, 'get', side_effect=AssertionError('Unexpected HTTP GET')))
        self.enterContext(patch.object(checker.httpx, 'post', side_effect=AssertionError('Unexpected HTTP POST')))
        self.tomorrow = datetime.datetime.now(ZoneInfo('Europe/Belgrade')).date() + datetime.timedelta(days=1)

    def test_contact_image_generated_only_for_new_database(self):
        self.contact_image.assert_called_once_with(
            settings.ADMIN_EMAIL, os.path.join(database.script_dir, 'static', 'contact.png'))
        database.setup_initial_db()
        self.contact_image.assert_called_once()

    def test_contact_image_generation(self):
        destination = Path(self.temp.name) / 'contact.png'
        # Call the original function while the database setup mock stays active.
        self.real_contact_generator('admin@example.com', destination)
        data = destination.read_bytes()
        self.assertTrue(data.startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertNotIn(b'admin@example.com', data)

    def db(self):
        conn = database.open_db()
        self.addCleanup(conn.close)
        return conn

    def user(self, email='user@example.com', authorized=0, token=None, approval='approve'):
        conn = self.db()
        database.add_user(conn, email, token, 'Test 12', approval_token=approval)
        database.update_user(conn, email, authorized=authorized)
        return email

    def identity(self, email='new@example.com', age=0):
        with self.client.session_transaction() as session:
            session['registration'] = {'email': email, 'picture': None, 'issued_at': time.time() - age}

    def oauth(self, email, verified=True):
        google = Mock()
        google.get.return_value.json.return_value = {'email': email, 'email_verified': verified}
        with patch.object(index, 'google', google):
            return self.client.get('/oauth2callback')

    def test_keyless_address_picker_is_available_on_all_address_forms(self):
        self.identity()
        registration = self.client.get('/complete_registration')
        self.user(authorized=2, token='admin-test')
        self.client.set_cookie('token', 'admin-test')
        home = self.client.get('/')
        admin = self.client.get('/manage_users')
        for response in (registration, home, admin):
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

    def test_address_picker_preserves_manual_entry(self):
        self.user(authorized=1, token='login')
        self.client.set_cookie('token', 'login')
        html = self.client.get('/').get_data(as_text=True)
        response = self.client.post('/', data={'address': 'Manual address 12'})
        self.assertNotIn('data-map-link', html)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(database.get_user(self.db(), email='user@example.com')['address'], 'Manual address 12')

    def test_registration_requires_verified_session_for_get_and_post(self):
        for method in (self.client.get, self.client.post):
            response = method('/complete_registration?email=attacker@example.com',
                              data={'email': settings.ADMIN_EMAIL, 'address': 'Bad 1'})
            self.assertEqual(response.status_code, 302)
            self.assertTrue(response.location.endswith('/login'))
        self.assertEqual(database.get_user(self.db(), email=settings.ADMIN_EMAIL)['authorized'], 2)
        self.mail.assert_not_called()

    def test_registration_ignores_forged_email_and_uses_google_identity(self):
        self.assertTrue(self.oauth('verified@example.com').location.endswith('/complete_registration'))
        response = self.client.post('/complete_registration', data={'email': settings.ADMIN_EMAIL, 'address': ' Good 12 '})
        self.assertEqual(response.status_code, 302)
        user = database.get_user(self.db(), email='verified@example.com')
        self.assertEqual(user['address'], 'Good 12')
        self.assertIsNone(user['token'])
        self.assertTrue(user['approval_token'])
        self.assertEqual(database.get_user(self.db(), email=settings.ADMIN_EMAIL)['authorized'], 2)
        self.mail.assert_called_once()
        with self.client.session_transaction() as session:
            self.assertNotIn('registration', session)

    def test_registration_expiration_and_unverified_google(self):
        self.identity(age=901)
        self.assertTrue(self.client.get('/complete_registration').location.endswith('/login'))
        self.assertTrue(self.oauth('bad@example.com', verified=False).location.endswith('/login'))
        with self.client.session_transaction() as session:
            self.assertNotIn('registration', session)

    def test_duplicate_registration_preserves_account_and_token(self):
        email = self.user(authorized=2, token='existing')
        self.identity(email)
        self.client.post('/complete_registration', data={'address': 'Replacement'})
        user = database.get_user(self.db(), email=email)
        self.assertEqual((user['authorized'], user['address'], user['token']), (2, 'Test 12', 'existing'))
        self.assertFalse(database.add_user(self.db(), email, 'other', 'Other'))
        self.mail.assert_not_called()

    def test_pending_user_cannot_access_or_update_home_even_with_login_token(self):
        email = self.user(token='pending-login')
        self.client.set_cookie('token', 'pending-login')
        for response in (self.client.get('/'), self.client.post('/', data={'address': 'Replacement'})):
            self.assertTrue(response.location.endswith('/login'))
        self.assertEqual(database.get_user(self.db(), email=email)['address'], 'Test 12')

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

    def test_login_cookie_and_logout_revocation(self):
        email = self.user(authorized=1)
        response = self.oauth(email)
        cookie = next(value for value in response.headers.getlist('Set-Cookie') if value.startswith('token='))
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)
        self.assertEqual(self.client.get('/').status_code, 200)
        self.client.get('/logout')
        self.assertIsNone(database.get_user(self.db(), email=email)['token'])

    def test_registration_keeps_csrf_protection(self):
        self.identity()
        with patch.dict(index.application.config, WTF_CSRF_ENABLED=True):
            self.assertEqual(self.client.post('/complete_registration', data={'address': 'Test'}).status_code, 400)
        self.mail.assert_not_called()

    def test_old_database_migration_preserves_links_and_revokes_legacy_logins(self):
        path = str(Path(self.temp.name) / 'legacy.db')
        conn = sqlite3.connect(path)
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE users (email TEXT UNIQUE, token TEXT UNIQUE, authorized INTEGER, address TEXT, picture TEXT, last_seen TEXT)')
        conn.executemany('INSERT INTO users (email,token,authorized) VALUES (?,?,?)',
                         [('pending@example.com', 'old-approval', 0), ('admin@example.com', 'old-login', 2)])
        conn.commit()
        database.init_database(conn)
        database.init_database(conn)
        self.assertEqual(conn.execute('SELECT token,approval_token FROM users WHERE authorized=0').fetchone(), (None, 'old-approval'))
        self.assertEqual(conn.execute('SELECT token,approval_token FROM users WHERE authorized=2').fetchone(), (None, None))
        conn.execute("UPDATE users SET token='new-login' WHERE authorized=2")
        conn.commit()
        database.init_database(conn)
        self.assertEqual(conn.execute('SELECT token FROM users WHERE authorized=2').fetchone(), ('new-login',))

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
        database.update_user(self.db(), 'other@example.com', address='Test 24')
        self.user(email='pending@example.com', authorized=0)
        self.user(email='blank@example.com', authorized=1)
        database.update_user(self.db(), 'blank@example.com', address='   ')
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
            database.update_user(self.db(), 'user@example.com', address='New 24')
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
        def send(recipient, subject, body):
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
t.settings.CHECK_START_HOUR = 0
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
        database.update_user(self.db(), 'user@example.com', address='New 24')
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

    def test_hourly_trigger_skips_before_configured_belgrade_hour(self):
        self.user(authorized=1)
        # September is UTC+2, January is UTC+1. Neither uses server-local time.
        for utc in [datetime.datetime(2026, 9, 26, 16, 59, 59, tzinfo=datetime.timezone.utc),
                    datetime.datetime(2026, 1, 26, 17, 59, 59, tzinfo=datetime.timezone.utc),
                    datetime.datetime(2026, 9, 26, 22, 0, tzinfo=datetime.timezone.utc)]:
            with self.subTest(utc=utc), patch.object(settings, 'CHECK_START_HOUR', 19), \
                 patch.object(checker.datetime, 'datetime') as clock, patch.object(checker, '_run_checks') as run:
                clock.now.side_effect = lambda tz: utc.astimezone(tz)
                response = self.client.get('/run_check', query_string={'token': settings.TRIGGER_TOKEN})
            self.assertEqual(response.status_code, 200)
            self.assertIn('19:00 (Europe/Belgrade)', response.get_data(as_text=True))
            run.assert_not_called()
        self.mail.assert_not_called()
        self.assertFalse(Path(database.db_path + '.check.lock').exists())
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM check_batches').fetchone()[0], 0)
        self.assertEqual(self.db().execute('SELECT COUNT(*) FROM checks').fetchone()[0], 0)

    def test_hourly_trigger_starts_exactly_at_configured_hour(self):
        for start_hour, utc in [
            (19, datetime.datetime(2026, 9, 26, 17, 0, tzinfo=datetime.timezone.utc)),
            (19, datetime.datetime(2026, 1, 26, 18, 0, tzinfo=datetime.timezone.utc)),
            (20, datetime.datetime(2026, 9, 26, 18, 0, tzinfo=datetime.timezone.utc)),
            (0, datetime.datetime(2026, 9, 25, 22, 0, tzinfo=datetime.timezone.utc)),
            (23, datetime.datetime(2026, 9, 26, 21, 0, tzinfo=datetime.timezone.utc)),
        ]:
            with self.subTest(start_hour=start_hour, utc=utc), patch.object(settings, 'CHECK_START_HOUR', start_hour), \
                 patch.object(checker.datetime, 'datetime') as clock, patch.object(checker, '_run_checks', return_value={}) as run:
                clock.now.side_effect = lambda tz: utc.astimezone(tz)
                checker.run_checks()
            run.assert_called_once_with(utc.astimezone(ZoneInfo('Europe/Belgrade')).date())

    def test_start_hour_defaults_to_19_for_existing_settings(self):
        utc = datetime.datetime(2026, 9, 26, 16, 0, tzinfo=datetime.timezone.utc)
        with patch.dict(settings.__dict__):
            del settings.CHECK_START_HOUR
            with patch.object(checker.datetime, 'datetime') as clock:
                clock.now.side_effect = lambda tz: utc.astimezone(tz)
                result = checker.run_checks()
        self.assertEqual(result['skip_reason'], 'before_start_hour')
        self.assertEqual(result['start_hour'], 19)

    def test_invalid_start_hour_does_not_run_a_check(self):
        for value in [-1, 24, '19', 19.5, True, None]:
            with self.subTest(value=value), patch.object(settings, 'CHECK_START_HOUR', value), \
                 patch.object(checker, '_run_checks') as run:
                with self.assertRaisesRegex(ValueError, 'CHECK_START_HOUR'):
                    checker.run_checks()
                run.assert_not_called()

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
