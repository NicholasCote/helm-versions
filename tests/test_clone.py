#!/usr/bin/env python3
"""Tests for cloning the config repo with a GitHub OAuth token.

The point of most of these is negative: the token must not reach argv, the URL, or a
log line. Needs PyYAML and requests (tracker.py imports them); git is never run.

    python -m unittest discover -s tests -t .
"""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from tracker import HelmChartTracker  # noqa: E402

TOKEN = 'gho_pretendthisisreal'
SSH_URL = 'git@github.com:NCAR/cisl-cloud-charts.git'


def tracker(**kwargs):
    return HelmChartTracker(git_repo_url=kwargs.pop('url', SSH_URL), **kwargs)


class CloneDispatchTests(unittest.TestCase):
    def test_a_token_takes_the_https_path_not_ssh(self):
        subject = tracker(github_token=TOKEN)
        with mock.patch.object(subject, 'clone_with_token', return_value=True) as https, \
             mock.patch.object(subject, 'setup_git_ssh') as ssh:
            self.assertTrue(subject.clone_repo('/tmp/x'))
        https.assert_called_once()
        ssh.assert_not_called()

    def test_no_token_still_uses_ssh(self):
        subject = tracker(ssh_key_path='/tmp/key')
        with mock.patch.object(subject, 'clone_with_token') as https, \
             mock.patch.object(subject, 'setup_git_ssh'), \
             mock.patch('subprocess.run'):
            subject.clone_repo('/tmp/x')
        https.assert_not_called()


class TokenHandlingTests(unittest.TestCase):
    def run_clone(self, url=SSH_URL, token=TOKEN):
        """Clone with subprocess stubbed out; returns the call it would have made"""
        subject = tracker(url=url, github_token=token)
        with mock.patch('subprocess.run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='', stderr='')
            ok = subject.clone_with_token('/tmp/target')
        return ok, run

    def test_token_is_never_in_argv(self):
        # argv is world-readable through /proc on a shared node; a sidecar or a debug
        # container in the same pod could read it straight out of `ps`.
        _, run = self.run_clone()
        self.assertNotIn(TOKEN, ' '.join(run.call_args.args[0]))

    def test_token_is_never_in_the_clone_url(self):
        # A URL credential gets written into .git/config and echoed in git's errors.
        _, run = self.run_clone()
        url = run.call_args.args[0][2]
        self.assertNotIn(TOKEN, url)
        self.assertEqual(url, 'https://github.com/NCAR/cisl-cloud-charts.git')

    def test_the_clone_url_carries_no_username_either(self):
        # Not cosmetic: given a username in the URL, git skips GIT_ASKPASS entirely and
        # sends an empty password, so every clone 401s. Verified end-to-end against git
        # 2.43 - with `x-access-token@` in the URL the helper is called 0 times and the
        # wire carries `x-access-token:`; without it, 2 calls and the real token.
        _, run = self.run_clone()
        self.assertNotIn('@', run.call_args.args[0][2])

    def test_token_reaches_git_through_the_environment(self):
        _, run = self.run_clone()
        env = run.call_args.kwargs['env']
        self.assertEqual(env['GIT_OAUTH_TOKEN'], TOKEN)
        self.assertTrue(env['GIT_ASKPASS'].endswith('askpass.sh'))

    def test_interactive_prompting_is_disabled(self):
        # Without this a container with no tty hangs on the credential prompt instead
        # of failing.
        _, run = self.run_clone()
        self.assertEqual(run.call_args.kwargs['env']['GIT_TERMINAL_PROMPT'], '0')

    def test_a_stale_ssh_command_cannot_take_precedence(self):
        with mock.patch.dict('os.environ', {'GIT_SSH_COMMAND': 'ssh -F /tmp/.ssh/config'}):
            _, run = self.run_clone()
        self.assertNotIn('GIT_SSH_COMMAND', run.call_args.kwargs['env'])

    def test_the_askpass_helper_answers_username_and_password_differently(self):
        """The helper must return the literal username, not the token, for Username

        A helper that echoes the token for every prompt puts it in the username half of
        basic auth too, where it lands in any server access log that records userinfo.
        Runs the real script against the real prompt strings git 2.43 uses.
        """
        subject = tracker(github_token=TOKEN)
        body = {}

        def capture(argv, **kwargs):
            body['text'] = open(kwargs['env']['GIT_ASKPASS']).read()
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('subprocess.run', side_effect=capture):
            subject.clone_with_token('/tmp/target')

        with tempfile.TemporaryDirectory() as workdir:
            helper = Path(workdir) / 'askpass.sh'
            helper.write_text(body['text'])
            helper.chmod(0o700)

            def ask(prompt):
                return subprocess.run([str(helper), prompt], capture_output=True, text=True,
                                      env={'GIT_OAUTH_TOKEN': TOKEN, 'PATH': '/usr/bin:/bin'}).stdout

            self.assertEqual(ask("Username for 'https://github.com': "), 'x-access-token')
            self.assertEqual(ask("Password for 'https://github.com': "), TOKEN)

    def test_the_askpass_helper_holds_no_secret_and_is_not_left_behind(self):
        subject = tracker(github_token=TOKEN)
        seen = {}

        def capture(argv, **kwargs):
            path = kwargs['env']['GIT_ASKPASS']
            seen['path'] = path
            seen['body'] = open(path).read()
            seen['mode'] = oct(Path(path).stat().st_mode)[-3:]
            return mock.Mock(returncode=0, stdout='', stderr='')

        with mock.patch('subprocess.run', side_effect=capture):
            subject.clone_with_token('/tmp/target')

        self.assertNotIn(TOKEN, seen['body'])
        self.assertEqual(seen['mode'], '700')
        self.assertFalse(Path(seen['path']).exists())  # temp dir is cleaned up

    def test_clone_is_bounded_by_a_timeout(self):
        _, run = self.run_clone()
        self.assertEqual(run.call_args.kwargs['timeout'],
                         HelmChartTracker.CLONE_TIMEOUT_SECONDS)


class FailureTests(unittest.TestCase):
    def test_a_non_github_url_is_refused_rather_than_clone_attempted(self):
        subject = tracker(url='git@gitlab.com:NCAR/charts.git', github_token=TOKEN)
        with mock.patch('subprocess.run') as run:
            self.assertFalse(subject.clone_with_token('/tmp/target'))
        run.assert_not_called()

    def test_a_failed_clone_returns_false(self):
        subject = tracker(github_token=TOKEN)
        error = subprocess.CalledProcessError(128, 'git', stderr='fatal: repository not found')
        with mock.patch('subprocess.run', side_effect=error):
            self.assertFalse(subject.clone_with_token('/tmp/target'))

    def test_a_timeout_returns_false(self):
        subject = tracker(github_token=TOKEN)
        with mock.patch('subprocess.run',
                        side_effect=subprocess.TimeoutExpired('git', 120)):
            self.assertFalse(subject.clone_with_token('/tmp/target'))

    def test_git_output_is_scrubbed_before_it_is_logged(self):
        # git shouldn't echo the token - it never sees it in argv or the URL - but the
        # log line is what ends up in a shared cluster's log store, so don't rely on it.
        subject = tracker(github_token=TOKEN)
        error = subprocess.CalledProcessError(
            128, 'git', stderr=f'fatal: could not read Password for {TOKEN}')
        with mock.patch('subprocess.run', side_effect=error), \
             mock.patch('builtins.print') as printed:
            subject.clone_with_token('/tmp/target')

        logged = ' '.join(str(call) for call in printed.call_args_list)
        self.assertNotIn(TOKEN, logged)
        self.assertIn('***', logged)

    def test_scrub_is_a_no_op_without_a_token(self):
        self.assertEqual(tracker().scrub('  plain text  '), 'plain text')

    def test_scrub_handles_none(self):
        self.assertEqual(tracker(github_token=TOKEN).scrub(None), '')


if __name__ == '__main__':
    unittest.main()
