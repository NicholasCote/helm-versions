#!/usr/bin/env python3
"""Tests for the GitHub OAuth helpers and the team gate.

stdlib unittest plus `requests` (mocked - nothing here touches the network).

    python -m unittest discover -s tests -t .
"""

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import requests  # noqa: E402

import githubauth  # noqa: E402
from githubauth import (  # noqa: E402
    AuthError, OAuthConfig, TokenStore, assert_team_member, authorize_url,
    config_from_env, exchange_code, fetch_user, https_clone_url, repo_slug,
)

CONFIG = OAuthConfig(client_id='cid', client_secret='shh', org='NCAR', team='cirrus-admins')


def response(status=200, payload=None):
    """A stand-in for a requests.Response carrying JSON"""
    stub = mock.Mock()
    stub.status_code = status
    stub.json.return_value = payload if payload is not None else {}
    stub.raise_for_status.side_effect = (
        None if status < 400 else requests.HTTPError(f"{status}"))
    return stub


class RepoSlugTests(unittest.TestCase):
    def test_parses_every_url_form_git_accepts(self):
        for url in ['git@github.com:NCAR/cisl-cloud-charts.git',
                    'https://github.com/NCAR/cisl-cloud-charts.git',
                    'https://github.com/NCAR/cisl-cloud-charts',
                    'ssh://git@github.com/NCAR/cisl-cloud-charts.git',
                    'https://github.com/NCAR/cisl-cloud-charts/']:
            with self.subTest(url=url):
                self.assertEqual(repo_slug(url), ('NCAR', 'cisl-cloud-charts'))

    def test_rejects_non_github_and_empty(self):
        for url in ['', None, 'git@gitlab.com:NCAR/charts.git',
                    'https://example.com/NCAR/charts.git']:
            with self.subTest(url=url):
                self.assertIsNone(repo_slug(url))

    def test_https_clone_url_normalizes_ssh(self):
        self.assertEqual(https_clone_url('git@github.com:NCAR/cisl-cloud-charts.git'),
                         'https://github.com/NCAR/cisl-cloud-charts.git')

    def test_https_clone_url_none_for_non_github(self):
        self.assertIsNone(https_clone_url('git@gitlab.com:NCAR/charts.git'))


class ConfigTests(unittest.TestCase):
    def test_none_without_client_credentials(self):
        with mock.patch.dict('os.environ', {}, clear=True):
            self.assertIsNone(config_from_env('git@github.com:NCAR/charts.git'))

    def test_none_when_only_client_id_is_set(self):
        with mock.patch.dict('os.environ', {'GITHUB_CLIENT_ID': 'cid'}, clear=True):
            self.assertIsNone(config_from_env())

    def test_defaults_to_cirrus_admins(self):
        env = {'GITHUB_CLIENT_ID': 'cid', 'GITHUB_CLIENT_SECRET': 'shh'}
        with mock.patch.dict('os.environ', env, clear=True):
            config = config_from_env('git@github.com:NCAR/cisl-cloud-charts.git')
        self.assertEqual(config.team_slug, 'NCAR/cirrus-admins')

    def test_bare_team_name_takes_org_from_the_repo(self):
        env = {'GITHUB_CLIENT_ID': 'cid', 'GITHUB_CLIENT_SECRET': 'shh',
               'GITHUB_ALLOWED_TEAM': 'platform'}
        with mock.patch.dict('os.environ', env, clear=True):
            config = config_from_env('git@github.com:ExampleOrg/charts.git')
        self.assertEqual(config.team_slug, 'ExampleOrg/platform')

    def test_qualified_team_overrides_the_repo_org(self):
        env = {'GITHUB_CLIENT_ID': 'cid', 'GITHUB_CLIENT_SECRET': 'shh',
               'GITHUB_ALLOWED_TEAM': 'OtherOrg/sre'}
        with mock.patch.dict('os.environ', env, clear=True):
            config = config_from_env('git@github.com:NCAR/charts.git')
        self.assertEqual((config.org, config.team), ('OtherOrg', 'sre'))


class AuthorizeUrlTests(unittest.TestCase):
    def test_carries_state_and_both_required_scopes(self):
        url = authorize_url(CONFIG, 'https://dash.example/auth/callback', 'st8')
        self.assertIn('state=st8', url)
        self.assertIn('client_id=cid', url)
        # repo for the private clone, read:org for the team lookup
        self.assertIn('repo', url)
        self.assertIn('read%3Aorg', url)

    def test_never_carries_the_client_secret(self):
        self.assertNotIn('shh', authorize_url(CONFIG, 'https://d/cb', 'st8'))


class ExchangeCodeTests(unittest.TestCase):
    def test_returns_the_token(self):
        with mock.patch('requests.post', return_value=response(200, {'access_token': 'gho_x'})):
            self.assertEqual(exchange_code(CONFIG, 'code', 'https://d/cb'), 'gho_x')

    def test_github_reports_failure_as_200_with_an_error_key(self):
        payload = {'error': 'bad_verification_code',
                   'error_description': 'The code is incorrect.'}
        with mock.patch('requests.post', return_value=response(200, payload)):
            with self.assertRaises(AuthError) as caught:
                exchange_code(CONFIG, 'code', 'https://d/cb')
        self.assertEqual(caught.exception.status, 401)
        self.assertIn('The code is incorrect.', caught.exception.message)

    def test_missing_token_is_an_error_not_a_none_return(self):
        with mock.patch('requests.post', return_value=response(200, {})):
            with self.assertRaises(AuthError):
                exchange_code(CONFIG, 'code', 'https://d/cb')


class FetchUserTests(unittest.TestCase):
    def test_returns_the_profile(self):
        with mock.patch('requests.get', return_value=response(200, {'login': 'octocat'})):
            self.assertEqual(fetch_user('tok')['login'], 'octocat')

    def test_raises_on_a_profile_with_no_login(self):
        with mock.patch('requests.get', return_value=response(200, {'name': 'No Login'})):
            with self.assertRaises(AuthError):
                fetch_user('tok')

    def test_sends_a_bearer_token(self):
        with mock.patch('requests.get', return_value=response(200, {'login': 'o'})) as get:
            fetch_user('tok')
        self.assertEqual(get.call_args.kwargs['headers']['Authorization'], 'Bearer tok')


class TeamMembershipTests(unittest.TestCase):
    def test_active_member_passes(self):
        with mock.patch('requests.get', return_value=response(200, {'state': 'active'})):
            assert_team_member('tok', CONFIG, 'octocat')  # no raise

    def test_pending_invitation_is_refused_with_actionable_wording(self):
        with mock.patch('requests.get', return_value=response(200, {'state': 'pending'})):
            with self.assertRaises(AuthError) as caught:
                assert_team_member('tok', CONFIG, 'octocat')
        self.assertIn('pending', caught.exception.message)

    def test_404_and_403_both_read_as_not_a_member(self):
        # A secret team is invisible to non-members (404) and a visible one refuses the
        # lookup (403). Same denial either way - the distinction isn't the user's.
        for status in (403, 404):
            with self.subTest(status=status):
                with mock.patch('requests.get', return_value=response(status)):
                    with self.assertRaises(AuthError) as caught:
                        assert_team_member('tok', CONFIG, 'octocat')
                self.assertIn('not a member', caught.exception.message)
                self.assertEqual(caught.exception.status, 403)

    def test_server_error_is_reported_as_upstream_not_as_denial(self):
        with mock.patch('requests.get', return_value=response(500)):
            with self.assertRaises(AuthError) as caught:
                assert_team_member('tok', CONFIG, 'octocat')
        self.assertEqual(caught.exception.status, 502)

    def test_queries_the_configured_team(self):
        with mock.patch('requests.get', return_value=response(200, {'state': 'active'})) as get:
            assert_team_member('tok', CONFIG, 'octocat')
        self.assertIn('/orgs/NCAR/teams/cirrus-admins/memberships/octocat',
                      get.call_args.args[0])


class TokenStoreTests(unittest.TestCase):
    def test_round_trips_a_session(self):
        store = TokenStore()
        sid = store.create('gho_x', {'login': 'octocat', 'name': 'Octo',
                                     'avatar_url': 'https://a/x.png'})
        record = store.get(sid)
        self.assertEqual(record['token'], 'gho_x')
        self.assertEqual(record['login'], 'octocat')

    def test_session_ids_are_unguessable_and_unique(self):
        store = TokenStore()
        ids = {store.create('t', {'login': 'u'}) for _ in range(50)}
        self.assertEqual(len(ids), 50)
        self.assertTrue(all(len(i) >= 32 for i in ids))

    def test_unknown_and_missing_ids_return_none(self):
        store = TokenStore()
        self.assertIsNone(store.get('nope'))
        self.assertIsNone(store.get(None))
        self.assertIsNone(store.get(''))

    def test_drop_revokes_immediately(self):
        store = TokenStore()
        sid = store.create('gho_x', {'login': 'octocat'})
        store.drop(sid)
        self.assertIsNone(store.get(sid))

    def test_expired_sessions_are_not_returned(self):
        store = TokenStore(ttl_seconds=-1)
        sid = store.create('gho_x', {'login': 'octocat'})
        self.assertIsNone(store.get(sid))

    def test_name_falls_back_to_login(self):
        store = TokenStore()
        sid = store.create('gho_x', {'login': 'octocat', 'name': None})
        self.assertEqual(store.get(sid)['name'], 'octocat')

    def test_get_returns_a_copy_so_callers_cant_mutate_the_store(self):
        store = TokenStore()
        sid = store.create('gho_x', {'login': 'octocat'})
        store.get(sid)['token'] = 'tampered'
        self.assertEqual(store.get(sid)['token'], 'gho_x')

    def test_creating_a_session_sweeps_expired_ones(self):
        store = TokenStore(ttl_seconds=3600)
        stale = store.create('old', {'login': 'a'})
        with mock.patch.object(githubauth.time, 'time', return_value=time.time() + 7200):
            store.create('new', {'login': 'b'})
        self.assertEqual(len(store), 1)
        self.assertIsNone(store.get(stale))


if __name__ == '__main__':
    unittest.main()
