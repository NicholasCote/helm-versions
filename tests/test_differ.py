#!/usr/bin/env python3
"""Tests for the helm diff renderer.

No helm binary and no network: every test here covers the pure functions, which
are the parts that decide what gets handed to a subprocess.

    python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

import differ  # noqa: E402
from chartrefs import oci_chart_location  # noqa: E402

HTTPS_REPO = 'https://kubernetes.github.io/ingress-nginx'
OCI_REPO_WITH_CHART = 'oci://quay.io/cilium/charts/cilium'
OCI_REPO_NAMESPACE = 'ghcr.io/deliveryhero/helm-charts'


class ValidationTest(unittest.TestCase):

    def assertRejected(self, fn, value):
        with self.assertRaises(differ.ValidationError):
            fn(value)

    def test_rejects_flag_injection(self):
        """A list argv stops shell injection but not flag injection"""
        for value in ['--kubeconfig=/app/.ssh/id_rsa', '--set', '-f', '--devel']:
            with self.subTest(value):
                self.assertRejected(differ.validate_chart_name, value)
                self.assertRejected(differ.validate_version, value)
                self.assertRejected(differ.validate_repo_url, value)

    def test_rejects_shell_metacharacters(self):
        for value in ['1;rm -rf /', '$(id)', '`id`', 'a|b', 'a&b', 'a>b']:
            with self.subTest(value):
                self.assertRejected(differ.validate_version, value)

    def test_rejects_whitespace_and_control_characters(self):
        for value in ['a b', 'a\tb', 'a\nb', 'a\x00b', 'a\x7fb']:
            with self.subTest(repr(value)):
                self.assertRejected(differ.validate_chart_name, value)

    def test_rejects_path_traversal(self):
        for value in ['../../etc/passwd', '/etc/passwd', './x']:
            with self.subTest(value):
                self.assertRejected(differ.validate_chart_name, value)

    def test_rejects_empty_and_non_string(self):
        for value in ['', '   ', None, 42, [], {}]:
            with self.subTest(repr(value)):
                self.assertRejected(differ.validate_chart_name, value)

    def test_rejects_over_length(self):
        self.assertRejected(differ.validate_chart_name, 'a' * 200)
        self.assertRejected(differ.validate_version, '1.' + '2' * 100)
        self.assertRejected(differ.validate_repo_url, 'https://x.com/' + 'a' * 600)

    def test_rejects_non_chart_schemes(self):
        """Anything that could point the subprocess somewhere it shouldn't go"""
        for value in ['http://example.com', 'file:///etc/passwd', 'file://x/y',
                      'ftp://example.com/x', 'gopher://example.com/x',
                      'git@github.com:org/repo.git', 'ssh://git@example.com/x']:
            with self.subTest(value):
                self.assertRejected(differ.validate_repo_url, value)

    def test_rejects_credentials_in_url(self):
        self.assertRejected(differ.validate_repo_url, 'https://user:pass@example.com/charts')

    def test_accepts_real_repo_urls(self):
        for value in [HTTPS_REPO, OCI_REPO_WITH_CHART, OCI_REPO_NAMESPACE]:
            with self.subTest(value):
                self.assertEqual(differ.validate_repo_url(value), value)

    def test_accepts_real_versions(self):
        for value in ['1.2.3', 'v1.2.3', '1.2.3-rc.1', '1.2.3+build.5', '4.11.0']:
            with self.subTest(value):
                self.assertEqual(differ.validate_version(value), value)

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(differ.validate_chart_name('  cilium  '), 'cilium')


class ChartReferenceTest(unittest.TestCase):

    def test_https_uses_repo_flag_not_repo_add(self):
        """--repo takes helm's in-memory path, so concurrent renders can't collide"""
        chart_arg, extra = differ.chart_reference('ingress-nginx', HTTPS_REPO)
        self.assertEqual(chart_arg, 'ingress-nginx')
        self.assertEqual(extra, ['--repo', HTTPS_REPO])

    def test_oci_url_already_ending_in_chart_name(self):
        chart_arg, extra = differ.chart_reference('cilium', OCI_REPO_WITH_CHART)
        self.assertEqual(chart_arg, 'oci://quay.io/cilium/charts/cilium')
        self.assertEqual(extra, [])

    def test_oci_url_naming_only_the_namespace(self):
        chart_arg, extra = differ.chart_reference('node-problem-detector', OCI_REPO_NAMESPACE)
        self.assertEqual(chart_arg,
                         'oci://ghcr.io/deliveryhero/helm-charts/node-problem-detector')
        self.assertEqual(extra, [])

    def test_matches_the_trackers_own_oci_resolution(self):
        """The tag lookup and the render must agree on where a chart lives"""
        for chart_name, repo_url in [('cilium', OCI_REPO_WITH_CHART),
                                     ('node-problem-detector', OCI_REPO_NAMESPACE)]:
            with self.subTest(repo_url):
                chart_arg, _ = differ.chart_reference(chart_name, repo_url)
                self.assertEqual(chart_arg,
                                 'oci://' + oci_chart_location(chart_name, repo_url))


class BuildTemplateArgvTest(unittest.TestCase):

    def test_https_argv(self):
        self.assertEqual(
            differ.build_template_argv('rel', 'ingress-nginx', HTTPS_REPO, '4.11.0'),
            [differ.HELM_BINARY, 'template', 'rel', 'ingress-nginx',
             '--repo', HTTPS_REPO, '--version', '4.11.0', '--namespace', 'default'])

    def test_oci_argv_has_no_repo_flag(self):
        argv = differ.build_template_argv('cilium', 'cilium', OCI_REPO_WITH_CHART, '1.16.19')
        self.assertNotIn('--repo', argv)
        self.assertIn('oci://quay.io/cilium/charts/cilium', argv)

    def test_both_sides_of_a_diff_use_the_same_release_name(self):
        """Regression guard for the CI workflow's `old`/`new` release-name bug

        Most charts interpolate .Release.Name into their fullname template, so
        differing release names leak into every resource name and label. Measured at
        481 spurious diff lines when comparing a version against itself.
        """
        old_argv = differ.build_template_argv('rel', 'ingress-nginx', HTTPS_REPO, '4.11.0')
        new_argv = differ.build_template_argv('rel', 'ingress-nginx', HTTPS_REPO, '4.12.0')
        self.assertEqual(old_argv[2], new_argv[2])

    def test_never_passes_values_flags(self):
        """Defaults only, matching what the CI workflow renders"""
        argv = differ.build_template_argv('rel', 'ingress-nginx', HTTPS_REPO, '4.11.0')
        for flag in ['--set', '--values', '-f', '--set-string']:
            self.assertNotIn(flag, argv)


class HelmEnvTest(unittest.TestCase):

    def test_does_not_leak_the_git_deploy_key(self):
        """helm talks to arbitrary registries; the SSH key has no business there"""
        with mock.patch.dict('os.environ', {
                'SSH_KEY_CONTENT': 'secret-key',
                'SSH_KEY_CONTENT_BASE64': 'c2VjcmV0',
                'SSH_KEY_PATH': '/app/.ssh/id_rsa',
                'PATH': '/usr/bin'}, clear=True):
            env = differ.helm_env()

        self.assertNotIn('SSH_KEY_CONTENT', env)
        self.assertNotIn('SSH_KEY_CONTENT_BASE64', env)
        self.assertNotIn('SSH_KEY_PATH', env)
        self.assertNotIn('secret-key', ''.join(env.values()))

    def test_passes_through_helm_and_proxy_settings(self):
        with mock.patch.dict('os.environ', {
                'PATH': '/usr/bin',
                'HELM_CACHE_HOME': '/tmp/helm/cache',
                'HTTPS_PROXY': 'http://proxy.example:3128'}, clear=True):
            env = differ.helm_env()

        self.assertEqual(env['HELM_CACHE_HOME'], '/tmp/helm/cache')
        self.assertEqual(env['HTTPS_PROXY'], 'http://proxy.example:3128')
        self.assertEqual(env['PATH'], '/usr/bin')


class ClassifyHelmFailureTest(unittest.TestCase):

    CASES = [
        ('CHART_NOT_FOUND',
         'Error: chart "ingress-nginx" version "99.9.9" not found in https://x repository'),
        ('REGISTRY_AUTH', 'Error: unauthorized: authentication required'),
        ('REGISTRY_AUTH', 'Error: denied: requested access to the resource is denied'),
        ('NETWORK', 'Error: Get "https://x/index.yaml": dial tcp: lookup x: no such host'),
        ('NETWORK', 'Error: tls: failed to verify certificate'),
        ('RENDER_FAILED',
         'Error: execution error at (chart/templates/x.yaml:12:3): ingress host is required'),
        ('RENDER_FAILED', 'Error: template: mychart/templates/x:5:12: executing "x"'),
        ('RENDER_FAILED', 'Error: values don\'t meet the specifications of the schema'),
    ]

    def test_classification(self):
        for code, stderr in self.CASES:
            with self.subTest(stderr[:45]):
                error = differ.classify_helm_failure(stderr, 'chart', 'repo', '1.0.0')
                self.assertEqual(error.code, code)

    def test_unrecognised_failure_defaults_to_render_failed(self):
        error = differ.classify_helm_failure('something strange', 'chart', 'repo', '1.0.0')
        self.assertEqual(error.code, 'RENDER_FAILED')

    def test_message_names_the_chart_and_keeps_helm_output(self):
        error = differ.classify_helm_failure(
            'Error: chart "cilium" version "9.9.9" not found', 'cilium', 'oci://x', '9.9.9')
        self.assertIn('cilium', error.message)
        self.assertIn('9.9.9', error.message)
        self.assertIn('not found', error.detail)

    def test_detail_is_truncated(self):
        error = differ.classify_helm_failure('x' * 10000, 'chart', 'repo', '1.0.0')
        self.assertLessEqual(len(error.detail), 4000)

    def test_error_serialises_for_json(self):
        payload = differ.HelmError('TIMEOUT', 'too slow', 'detail').as_dict()
        self.assertEqual(payload, {'code': 'TIMEOUT', 'message': 'too slow',
                                   'detail': 'detail'})


class DiffVersionsTest(unittest.TestCase):
    """diff_versions with rendering stubbed out - no helm, no network"""

    def setUp(self):
        differ.clear_render_cache()
        self.addCleanup(differ.clear_render_cache)

    def diff(self, old_text, new_text, **kwargs):
        renders = {'1.0.0': old_text, '2.0.0': new_text}
        with mock.patch.object(differ, '_run_helm',
                               side_effect=lambda argv, c, r, v: renders[v]):
            return differ.diff_versions('mychart', HTTPS_REPO, '1.0.0', '2.0.0', **kwargs)

    def test_identical_renders(self):
        result = self.diff('a\nb\nc\n', 'a\nb\nc\n')
        self.assertTrue(result['identical'])
        self.assertEqual(result['line_count'], 0)
        self.assertEqual(result['diff'], '')

    def test_counts_additions_and_removals_excluding_headers(self):
        result = self.diff('a\nb\nc\n', 'a\nB\nc\n')
        self.assertFalse(result['identical'])
        self.assertEqual(result['added'], 1)
        self.assertEqual(result['removed'], 1)

    def test_truncation(self):
        old = '\n'.join(f'line {i}' for i in range(50000))
        new = '\n'.join(f'changed {i}' for i in range(50000))
        result = self.diff(old, new)
        self.assertTrue(result['truncated'])
        self.assertLessEqual(result['line_count'], differ.MAX_DIFF_LINES + 1)
        self.assertIn('truncated', result['diff'].splitlines()[-1])

    def test_release_name_defaults_to_chart_name(self):
        self.assertEqual(self.diff('a\n', 'b\n')['release_name'], 'mychart')

    def test_release_name_is_honoured(self):
        result = self.diff('a\n', 'b\n', release_name='my-release')
        self.assertEqual(result['release_name'], 'my-release')

    def test_invalid_input_is_rejected_before_rendering(self):
        with mock.patch.object(differ, '_run_helm') as run:
            with self.assertRaises(differ.ValidationError):
                differ.diff_versions('mychart', HTTPS_REPO, '1.0.0', '--devel')
        run.assert_not_called()


class RenderCacheTest(unittest.TestCase):

    def setUp(self):
        differ.clear_render_cache()
        self.addCleanup(differ.clear_render_cache)

    def test_second_render_is_served_from_cache(self):
        with mock.patch.object(differ, '_run_helm', return_value='manifest\n') as run:
            first = differ.render_chart('mychart', HTTPS_REPO, '1.0.0')
            second = differ.render_chart('mychart', HTTPS_REPO, '1.0.0')

        self.assertEqual(first, second)
        run.assert_called_once()

    def test_cache_is_keyed_by_version_and_release_name(self):
        with mock.patch.object(differ, '_run_helm', return_value='manifest\n') as run:
            differ.render_chart('mychart', HTTPS_REPO, '1.0.0')
            differ.render_chart('mychart', HTTPS_REPO, '2.0.0')
            differ.render_chart('mychart', HTTPS_REPO, '1.0.0', release_name='other')

        self.assertEqual(run.call_count, 3)

    def test_clear_render_cache_forces_a_rerender(self):
        with mock.patch.object(differ, '_run_helm', return_value='manifest\n') as run:
            differ.render_chart('mychart', HTTPS_REPO, '1.0.0')
            differ.clear_render_cache()
            differ.render_chart('mychart', HTTPS_REPO, '1.0.0')

        self.assertEqual(run.call_count, 2)

    def test_cache_evicts_beyond_the_entry_limit(self):
        with mock.patch.object(differ, 'MAX_CACHE_ENTRIES', 2):
            with mock.patch.object(differ, '_run_helm', return_value='manifest\n') as run:
                for version in ['1.0.0', '2.0.0', '3.0.0']:
                    differ.render_chart('mychart', HTTPS_REPO, version)
                # 1.0.0 was evicted, so this re-renders
                differ.render_chart('mychart', HTTPS_REPO, '1.0.0')

        self.assertEqual(run.call_count, 4)


if __name__ == '__main__':
    unittest.main()
