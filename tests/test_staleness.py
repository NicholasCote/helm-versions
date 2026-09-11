#!/usr/bin/env python3
"""Tests for the staleness tier classifier.

stdlib unittest, no dependencies: staleness.py imports only `re`, `os` and
`dataclasses`, so this runs without Flask/PyYAML/requests installed.

    python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))

from staleness import (  # noqa: E402
    NEEDS_UPDATE_TIERS, TIER_AHEAD, TIER_CURRENT, TIER_MAJOR, TIER_ORDER,
    TIER_PATCH, TIER_STALE, TIER_UNKNOWN, classify_staleness,
    count_releases_between, resolve_latest, select_latest_version, semver_key,
)


class ClassifyStalenessTest(unittest.TestCase):
    """Table-driven: (current, latest, available, expected_tier, label_substring)"""

    CASES = [
        # --- core tiers ---
        ('exact match', '1.2.3', '1.2.3', [], TIER_CURRENT, 'Up to date'),
        ('one patch behind', '1.2.3', '1.2.4', ['1.2.3', '1.2.4'],
         TIER_PATCH, '1 patch release behind'),
        # The case that justifies counting releases rather than subtracting numbers:
        # a numeric delta of 10, but only two releases actually shipped.
        ('sparse patch numbering', '1.2.0', '1.2.10', ['1.2.0', '1.2.5', '1.2.10'],
         TIER_PATCH, '2 patch releases behind'),
        # Same pair with no list to count: falls back to arithmetic and over-reports.
        # This asymmetry is exactly why plumbing the version list through matters.
        ('sparse patch numbering, no list', '1.2.0', '1.2.10', [],
         TIER_STALE, '10 patch versions behind'),
        ('three patch releases behind', '1.2.0', '1.2.3',
         ['1.2.0', '1.2.1', '1.2.2', '1.2.3'], TIER_STALE, '3 patch releases behind'),
        ('one minor behind', '1.2.3', '1.3.0', ['1.2.3', '1.3.0'],
         TIER_STALE, '1 minor version behind'),
        ('two minors behind', '1.2.3', '1.4.0', [], TIER_MAJOR, '2 minor versions behind'),
        ('one major behind', '1.2.3', '2.0.0', [], TIER_MAJOR, '1 major version behind'),
        ('two majors behind', '1.2.3', '3.1.4', [], TIER_MAJOR, '2 major versions behind'),

        # --- edge cases, all of which occur in the wild ---
        # Regression guard: tracker.py used to compare raw strings, so a v-prefixed
        # pin read as permanently out of date.
        ('v prefix equality', 'v1.2.3', '1.2.3', [], TIER_CURRENT, 'Up to date'),
        ('v prefix on both sides', 'v1.2.3', 'v1.2.3', [], TIER_CURRENT, 'Up to date'),
        ('build metadata ignored', '1.2.3+build.5', '1.2.3', [], TIER_CURRENT, 'Up to date'),
        ('ahead of latest', '1.3.0', '1.2.9', [], TIER_AHEAD, 'Ahead of published latest'),
        ('pinned prerelease ahead', '2.0.0-rc.1', '1.9.9', [], TIER_AHEAD, 'Ahead'),
        ('prerelease of released version', '1.2.3-rc.1', '1.2.3', [],
         TIER_PATCH, 'Prerelease; 1.2.3 is released'),
        # A prerelease is not 'released', so this must not claim otherwise.
        ('prerelease to prerelease', '1.2.3-rc.1', '1.2.3-rc.2', [],
         TIER_PATCH, 'patch version behind'),
        ('non-semver current', 'stable', '1.2.3', [], TIER_UNKNOWN, 'Cannot compare'),
        ('non-semver latest', '1.2.3', 'latest', [], TIER_UNKNOWN, 'Cannot compare'),
        ('non-semver but equal', 'stable', 'stable', [], TIER_CURRENT, 'Up to date'),
        ('non-semver equal modulo v', 'vstable', 'stable', [], TIER_CURRENT, 'Up to date'),
        ('no latest at all', '1.2.3', None, [], TIER_UNKNOWN, 'No published version'),
        ('no latest, empty string', '1.2.3', '', [], TIER_UNKNOWN, 'No published version'),
    ]

    def test_tiers_and_labels(self):
        for name, current, latest, available, tier, label_part in self.CASES:
            with self.subTest(name):
                result = classify_staleness(current, latest, available)
                self.assertEqual(result.tier, tier)
                self.assertIn(label_part, result.label)

    def test_never_reports_zero_behind(self):
        """A chart that is behind must never render as '0 ... behind'"""
        for name, current, latest, available, tier, _ in self.CASES:
            if tier not in NEEDS_UPDATE_TIERS:
                continue
            with self.subTest(name):
                label = classify_staleness(current, latest, available).label
                self.assertFalse(label.startswith('0 '), label)

    def test_needs_update_matches_tier_table(self):
        for name, current, latest, available, tier, _ in self.CASES:
            with self.subTest(name):
                result = classify_staleness(current, latest, available)
                self.assertEqual(result.needs_update, tier in NEEDS_UPDATE_TIERS)

    def test_comparable_flag(self):
        self.assertTrue(classify_staleness('1.2.3', '1.2.4').comparable)
        self.assertFalse(classify_staleness('stable', '1.2.3').comparable)
        self.assertFalse(classify_staleness('stable', 'stable').comparable)
        self.assertFalse(classify_staleness('1.2.3', None).comparable)

    def test_deltas_are_never_negative(self):
        """Across a major bump the lower components go negative and must be clamped"""
        result = classify_staleness('1.9.9', '2.0.0')
        self.assertEqual(result.major_behind, 1)
        self.assertGreaterEqual(result.minor_behind, 0)
        self.assertGreaterEqual(result.patch_behind, 0)

    def test_empty_and_none_inputs_do_not_raise(self):
        for current, latest in [(None, None), ('', ''), (None, '1.2.3'), ('1.2.3', None)]:
            with self.subTest(f'{current!r}/{latest!r}'):
                self.assertEqual(classify_staleness(current, latest).tier, TIER_UNKNOWN)

    def test_every_tier_is_in_tier_order(self):
        for name, current, latest, available, _, _ in self.CASES:
            with self.subTest(name):
                self.assertIn(classify_staleness(current, latest, available).tier, TIER_ORDER)


class ThresholdTest(unittest.TestCase):
    """The boundaries must actually be tunable, not just configurable-looking"""

    RELEASES = ['1.2.0', '1.2.1', '1.2.2', '1.2.3']

    def test_patch_boundary_default(self):
        two = classify_staleness('1.2.1', '1.2.3', self.RELEASES)
        self.assertEqual(two.releases_behind, 2)
        self.assertEqual(two.tier, TIER_PATCH)

        three = classify_staleness('1.2.0', '1.2.3', self.RELEASES)
        self.assertEqual(three.releases_behind, 3)
        self.assertEqual(three.tier, TIER_STALE)

    def test_patch_boundary_raised(self):
        """Raising the threshold flips the 3-release case back to yellow"""
        result = classify_staleness('1.2.0', '1.2.3', self.RELEASES,
                                    thresholds={'max_patch_releases_for_patch_tier': 5})
        self.assertEqual(result.tier, TIER_PATCH)

    def test_minor_boundary_default(self):
        self.assertEqual(classify_staleness('1.2.0', '1.3.0').tier, TIER_STALE)
        self.assertEqual(classify_staleness('1.2.0', '1.4.0').tier, TIER_MAJOR)

    def test_minor_boundary_raised(self):
        result = classify_staleness('1.2.0', '1.4.0',
                                    thresholds={'max_minor_behind_for_stale_tier': 2})
        self.assertEqual(result.tier, TIER_STALE)

    def test_partial_override_keeps_other_default(self):
        result = classify_staleness('1.2.0', '1.4.0',
                                    thresholds={'max_patch_releases_for_patch_tier': 99})
        self.assertEqual(result.tier, TIER_MAJOR)


class CountReleasesBetweenTest(unittest.TestCase):

    def key(self, version):
        return semver_key(version)

    def test_counts_only_versions_in_range(self):
        versions = ['1.0.0', '1.2.0', '1.2.1', '1.2.2', '2.0.0']
        self.assertEqual(
            count_releases_between(self.key('1.2.0'), self.key('1.2.2'), versions), 2)

    def test_ignores_prereleases(self):
        versions = ['1.2.0', '1.2.1-rc.1', '1.2.1-rc.2', '1.2.1']
        self.assertEqual(
            count_releases_between(self.key('1.2.0'), self.key('1.2.1'), versions), 1)

    def test_dedupes_equivalent_spellings(self):
        versions = ['1.2.0', '1.2.1', 'v1.2.1', '1.2.1+build']
        self.assertEqual(
            count_releases_between(self.key('1.2.0'), self.key('1.2.1'), versions), 1)

    def test_ignores_unparseable_versions(self):
        versions = ['1.2.0', 'latest', 'nightly', '1.2.1']
        self.assertEqual(
            count_releases_between(self.key('1.2.0'), self.key('1.2.1'), versions), 1)

    def test_returns_none_rather_than_zero(self):
        """0 means the list didn't contain latest, so the caller must fall back"""
        self.assertIsNone(count_releases_between(self.key('1.2.0'), self.key('1.2.1'), []))
        self.assertIsNone(count_releases_between(self.key('1.2.0'), self.key('1.2.1'), None))
        self.assertIsNone(
            count_releases_between(self.key('1.2.0'), self.key('1.2.1'), ['9.9.9']))


class ResolveLatestTest(unittest.TestCase):

    def test_empty(self):
        self.assertIsNone(resolve_latest([]))
        self.assertIsNone(resolve_latest(None))

    def test_prefers_stable_over_prerelease(self):
        self.assertEqual(resolve_latest(['1.20.0', '1.21.0-pre.2']), '1.20.0')

    def test_falls_back_to_prereleases_when_only_option(self):
        self.assertEqual(resolve_latest(['1.21.0-pre.1', '1.21.0-pre.2']), '1.21.0-pre.2')

    def test_falls_back_to_index_order_for_non_semver(self):
        """index.yaml is conventionally newest-first, so entry 0 is the best guess"""
        self.assertEqual(resolve_latest(['abc', 'def']), 'abc')

    def test_ignores_unparseable_entries_when_semver_present(self):
        self.assertEqual(resolve_latest(['nightly', '1.2.3', '1.2.4']), '1.2.4')

    def test_matches_select_latest_version_when_semver_present(self):
        versions = ['1.2.3', '1.10.0', '1.9.0']
        self.assertEqual(resolve_latest(versions), select_latest_version(versions))


class SemverKeyTest(unittest.TestCase):

    def test_numeric_ordering_is_not_lexical(self):
        self.assertGreater(semver_key('1.10.0'), semver_key('1.9.0'))

    def test_release_outranks_its_prereleases(self):
        self.assertGreater(semver_key('1.20.0'), semver_key('1.20.0-rc.1'))

    def test_v_prefix_and_build_metadata_are_equivalent(self):
        self.assertEqual(semver_key('v1.2.3'), semver_key('1.2.3'))
        self.assertEqual(semver_key('1.2.3+build.9'), semver_key('1.2.3'))

    def test_non_semver_returns_none(self):
        for version in ['latest', 'stable', '1.2', '1.2.3.4', '', 'v']:
            with self.subTest(version):
                self.assertIsNone(semver_key(version))


if __name__ == '__main__':
    unittest.main()
