#!/usr/bin/env python3
"""Tests for the Flask auth wiring: the gate, the callback, and what leaks.

Needs Flask, PyYAML and requests (app.py imports the tracker). Everything outbound is
mocked.

    python -m unittest discover -s tests -t .
"""

import importlib
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

OAUTH_ENV = {
    'GITHUB_CLIENT_ID': 'cid',
    'GITHUB_CLIENT_SECRET': 'shh',
    'GITHUB_ALLOWED_TEAM': 'NCAR/cirrus-admins',
    'SECRET_KEY': 'test-key',
    'OAUTH_REDIRECT_URI': 'https://dash.example/auth/callback',
    'SESSION_COOKIE_SECURE': 'false',  # the test client speaks http
    'GIT_REPO_URL': 'git@github.com:NCAR/cisl-cloud-charts.git',
}


def load_app(env):
    """Import app.py fresh under a given environment.

    Reimported per test because OAUTH, the secret key and the cookie settings are all
    read at import time - which is the right place for them, but it means the module
    has to be reloaded to test a different configuration.
    """
    with mock.patch.dict('os.environ', env, clear=True):
        if 'app' in sys.modules:
            del sys.modules['app']
        return importlib.import_module('app')


class GateTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app(OAUTH_ENV)
        self.client = self.app.app.test_client()

    def test_dashboard_redirects_anonymous_users_to_login(self):
        response = self.client.get('/')
        self.assertEqual(response.status_code, 302)
        self.assertIn('/login', response.headers['Location'])

    def test_api_answers_401_json_not_a_redirect(self):
        # dashboard.js follows redirects transparently; a 302 to github.com would reach
        # it as an opaque HTML body instead of something it can act on.
        response = self.client.get('/api/charts')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json()['error']['code'], 'UNAUTHENTICATED')

    def test_diff_endpoint_is_gated(self):
        # The endpoint that shells out to helm is the one that most needs this.
        response = self.client.post('/api/diff', json={'chart_name': 'x'})
        self.assertEqual(response.status_code, 401)

    def test_refresh_is_gated(self):
        self.assertEqual(self.client.post('/api/refresh').status_code, 401)

    def test_debug_is_gated(self):
        self.assertEqual(self.client.get('/debug').status_code, 302)

    def test_health_stays_public_for_kubernetes_probes(self):
        response = self.client.get('/health')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['status'], 'healthy')

    def test_login_page_is_public(self):
        self.assertEqual(self.client.get('/login').status_code, 302)

    def test_every_route_is_gated_unless_named_public(self):
        # Default-deny: a route added later shouldn't quietly become reachable.
        exempt = self.app.PUBLIC_ENDPOINTS
        gated = [r for r in self.app.app.url_map.iter_rules()
                 if r.endpoint not in exempt]
        self.assertTrue(gated)
        for rule in gated:
            if '<' in rule.rule:
                continue
            method = 'POST' if 'POST' in rule.methods and 'GET' not in rule.methods else 'GET'
            with self.subTest(rule=rule.rule):
                response = self.client.open(rule.rule, method=method)
                self.assertIn(response.status_code, (302, 401))


class LoginFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app(OAUTH_ENV)
        self.client = self.app.app.test_client()

    def test_login_redirects_to_github_with_state(self):
        response = self.client.get('/login')
        self.assertEqual(response.status_code, 302)
        location = response.headers['Location']
        self.assertTrue(location.startswith('https://github.com/login/oauth/authorize'))
        self.assertIn('state=', location)
        self.assertIn('redirect_uri=https%3A%2F%2Fdash.example%2Fauth%2Fcallback', location)

    def test_callback_without_state_is_rejected(self):
        response = self.client.get('/auth/callback?code=abc&state=forged')
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"didn&#39;t start here", response.data)

    def test_callback_with_a_mismatched_state_is_rejected(self):
        with self.client.session_transaction() as session:
            session['oauth_state'] = 'real-state'
        response = self.client.get('/auth/callback?code=abc&state=forged')
        self.assertEqual(response.status_code, 400)

    def start_login(self):
        """Run /login and return the state it stashed in the session"""
        self.client.get('/login')
        with self.client.session_transaction() as session:
            return session['oauth_state']

    def test_successful_login_stores_the_token_server_side(self):
        state = self.start_login()
        with mock.patch.object(self.app.githubauth, 'exchange_code', return_value='gho_x'), \
             mock.patch.object(self.app.githubauth, 'fetch_user',
                               return_value={'login': 'octocat', 'name': 'Octo'}), \
             mock.patch.object(self.app.githubauth, 'assert_team_member'):
            response = self.client.get(f'/auth/callback?code=abc&state={state}')

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(self.app.token_store), 1)

    def test_the_token_never_reaches_the_browser(self):
        # The whole reason for the server-side store: Flask's session cookie is signed,
        # not encrypted, so anything put in it is readable by the client.
        state = self.start_login()
        with mock.patch.object(self.app.githubauth, 'exchange_code', return_value='gho_SECRET'), \
             mock.patch.object(self.app.githubauth, 'fetch_user',
                               return_value={'login': 'octocat'}), \
             mock.patch.object(self.app.githubauth, 'assert_team_member'):
            response = self.client.get(f'/auth/callback?code=abc&state={state}')

        cookies = ''.join(response.headers.getlist('Set-Cookie'))
        self.assertNotIn('gho_SECRET', cookies)

        import base64
        with self.client.session_transaction() as session:
            self.assertNotIn('gho_SECRET', str(dict(session)))
        # and not merely encoded out of sight
        self.assertNotIn(b'gho_SECRET', base64.b64decode(
            base64.b64encode(cookies.encode())))

    def test_non_team_member_is_refused_and_gets_no_session(self):
        state = self.start_login()
        denial = self.app.githubauth.AuthError(
            "You're not a member of the NCAR/cirrus-admins team.")
        with mock.patch.object(self.app.githubauth, 'exchange_code', return_value='gho_x'), \
             mock.patch.object(self.app.githubauth, 'fetch_user',
                               return_value={'login': 'stranger'}), \
             mock.patch.object(self.app.githubauth, 'assert_team_member', side_effect=denial):
            response = self.client.get(f'/auth/callback?code=abc&state={state}')

        self.assertEqual(response.status_code, 403)
        self.assertIn(b'cirrus-admins', response.data)
        self.assertEqual(len(self.app.token_store), 0)
        self.assertEqual(self.client.get('/api/charts').status_code, 401)

    def test_login_does_not_redirect_off_site(self):
        self.client.get('/login?next=https://evil.example/steal')
        with self.client.session_transaction() as session:
            self.assertEqual(session['next'], '/')

    def test_login_does_not_redirect_to_a_protocol_relative_url(self):
        self.client.get('/login?next=//evil.example/steal')
        with self.client.session_transaction() as session:
            self.assertEqual(session['next'], '/')

    def test_login_keeps_a_local_next_path(self):
        self.client.get('/login?next=/debug')
        with self.client.session_transaction() as session:
            self.assertEqual(session['next'], '/debug')


class SignedInTests(unittest.TestCase):
    def setUp(self):
        self.app = load_app(OAUTH_ENV)
        self.client = self.app.app.test_client()
        sid = self.app.token_store.create('gho_SECRET', {'login': 'octocat', 'name': 'Octo'})
        with self.client.session_transaction() as session:
            session['sid'] = sid

    def test_dashboard_is_served(self):
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_api_me_reports_the_user_and_team(self):
        payload = self.client.get('/api/me').get_json()
        self.assertTrue(payload['authenticated'])
        self.assertEqual(payload['login'], 'octocat')
        self.assertEqual(payload['team'], 'NCAR/cirrus-admins')

    def test_api_me_never_returns_the_token(self):
        self.assertNotIn('gho_SECRET', self.client.get('/api/me').get_data(as_text=True))

    def test_debug_reports_secrets_as_set_never_by_value(self):
        body = self.client.get('/debug').get_data(as_text=True)
        payload = self.client.get('/debug').get_json()
        self.assertTrue(payload['oauth_client_secret_set'])
        self.assertEqual(payload['signed_in_as'], 'octocat')
        for secret in ('shh', 'gho_SECRET', 'test-key'):
            self.assertNotIn(secret, body)

    def test_refresh_clones_as_the_signed_in_user(self):
        with mock.patch.object(self.app.threading, 'Thread') as thread:
            self.client.post('/api/refresh')
        kwargs = thread.call_args.kwargs['kwargs']
        self.assertEqual(kwargs['github_token'], 'gho_SECRET')
        self.assertEqual(kwargs['triggered_by'], 'octocat')

    def test_logout_revokes_the_session_immediately(self):
        self.client.get('/logout')
        self.assertEqual(len(self.app.token_store), 0)
        self.assertEqual(self.client.get('/api/charts').status_code, 401)

    def test_an_expired_server_side_session_locks_the_cookie_out(self):
        # The cookie is still valid and correctly signed; the token behind it is gone.
        self.app.token_store._sessions.clear()
        self.assertEqual(self.client.get('/api/charts').status_code, 401)


class OAuthDisabledTests(unittest.TestCase):
    """Without client credentials the app behaves exactly as it did before."""

    def setUp(self):
        self.app = load_app({'GIT_REPO_URL': 'git@github.com:NCAR/cisl-cloud-charts.git'})
        self.client = self.app.app.test_client()

    def test_dashboard_is_open(self):
        self.assertEqual(self.client.get('/').status_code, 200)

    def test_api_is_open(self):
        self.assertEqual(self.client.get('/api/charts').status_code, 200)

    def test_api_me_reports_oauth_off(self):
        payload = self.client.get('/api/me').get_json()
        self.assertFalse(payload['authenticated'])
        self.assertFalse(payload['oauth_enabled'])

    def test_login_is_not_offered(self):
        self.assertEqual(self.client.get('/login').status_code, 501)

    def test_refresh_falls_back_to_the_ssh_path(self):
        with mock.patch.object(self.app.threading, 'Thread') as thread:
            self.client.post('/api/refresh')
        self.assertIsNone(thread.call_args.kwargs['kwargs']['github_token'])


if __name__ == '__main__':
    unittest.main()
