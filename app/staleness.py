#!/usr/bin/env python3
"""Semver parsing and staleness classification for Helm chart versions.

Deliberately dependency-free (stdlib only), so it can be unit tested without
Flask/PyYAML/requests and imported from tracker.py without a cycle.

`semver_key` accepts an optional leading `v` and ignores build metadata when
ordering (per the semver spec). That is more permissive than a strict parser,
which is what chart versions in the wild need: `semver.Version.parse("v1.2.3")`
raises, and v-prefixed chart versions are common upstream.
"""

import os
import re
from dataclasses import dataclass
from typing import List, Optional

# Tier identifiers, ordered most -> least severe. The UI maps these to colours.
TIER_MAJOR = 'major'      # red
TIER_STALE = 'stale'      # orange
TIER_PATCH = 'patch'      # yellow
TIER_CURRENT = 'current'  # green
TIER_AHEAD = 'ahead'      # teal
TIER_UNKNOWN = 'unknown'  # grey

TIER_ORDER = (TIER_MAJOR, TIER_STALE, TIER_PATCH, TIER_CURRENT, TIER_AHEAD, TIER_UNKNOWN)

TIER_LABELS = {
    TIER_MAJOR: 'Major',
    TIER_STALE: 'Stale',
    TIER_PATCH: 'Patch',
    TIER_CURRENT: 'Up to date',
    TIER_AHEAD: 'Ahead of latest',
    TIER_UNKNOWN: 'No version info',
}

TIER_ICONS = {
    TIER_MAJOR: '🔴',
    TIER_STALE: '🟠',
    TIER_PATCH: '🟡',
    TIER_CURRENT: '🟢',
    TIER_AHEAD: '🔵',
    TIER_UNKNOWN: '❓',
}

# Tiers that mean "there is an upgrade to take". `needs_update` is derived from this.
NEEDS_UPDATE_TIERS = frozenset({TIER_PATCH, TIER_STALE, TIER_MAJOR})

# Tunable in one place. Override per-call with the `thresholds` argument, or via
# the environment with staleness_thresholds_from_env().
STALENESS_THRESHOLDS = {
    # At most this many patch releases behind is still yellow; beyond it, orange.
    'max_patch_releases_for_patch_tier': 2,
    # More than this many minor versions behind escalates from orange to red.
    'max_minor_behind_for_stale_tier': 1,
}

SEMVER_PATTERN = re.compile(
    r'^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$')


def semver_key(version: str):
    """Sort key for a semver chart version, or None if it isn't semver

    Ranks a release above its own prereleases (1.20.0 > 1.20.0-rc.1).
    """
    match = SEMVER_PATTERN.match(str(version).strip())
    if not match:
        return None

    major, minor, patch, prerelease = match.groups()

    if prerelease is None:
        # Any release outranks every prerelease of the same version
        prerelease_key = (1,)
    else:
        parts = []
        for part in prerelease.split('.'):
            # Numeric identifiers sort below alphanumeric ones, per semver
            parts.append((0, int(part), '') if part.isdigit() else (1, 0, part))
        prerelease_key = (0, tuple(parts))

    return (int(major), int(minor), int(patch), prerelease_key)


def is_prerelease_key(key) -> bool:
    """True if a semver_key belongs to a prerelease"""
    return key[3][0] == 0


def select_latest_version(versions: List[str]) -> Optional[str]:
    """Pick the highest stable version, ignoring prereleases unless that's all there is

    Mirrors `helm search repo`, which hides prereleases without --devel. Without this an
    OCI tag like 1.21.0-pre.0 would be reported as the latest cilium release.
    """
    parsed = [(semver_key(v), v) for v in versions]
    parsed = [(key, v) for key, v in parsed if key is not None]

    if not parsed:
        return None

    stable = [(key, v) for key, v in parsed if not is_prerelease_key(key)]
    return max(stable or parsed)[1]


def resolve_latest(versions: List[str]) -> Optional[str]:
    """The latest version from a repo's list, falling back to the repo's own ordering

    index.yaml is conventionally sorted newest-first, so its first entry is the best
    guess when nothing in the list parses as semver.
    """
    if not versions:
        return None
    return select_latest_version(versions) or versions[0]


def normalize_version(version: str) -> str:
    """Strip whitespace and a leading `v`, for comparing versions we can't parse"""
    return str(version).strip().lstrip('vV') if version else ''


def count_releases_between(current_key, latest_key, versions) -> Optional[int]:
    """How many stable releases you'd step through going current -> latest

    Returns None rather than 0 when nothing matches: 0 means the list didn't contain
    `latest` at all, so the caller should fall back to the numeric delta instead of
    reporting a chart as up to date.
    """
    if not versions:
        return None

    keys = {key for key in (semver_key(v) for v in versions) if key is not None}
    count = sum(1 for key in keys
                if not is_prerelease_key(key) and current_key < key <= latest_key)
    return count or None


@dataclass(frozen=True)
class Staleness:
    """How far behind a pinned chart version is, and how to describe it"""
    tier: str
    label: str
    major_behind: int = 0
    minor_behind: int = 0
    patch_behind: int = 0
    # Counted from the repo's real version list; None when we had no list to count.
    releases_behind: Optional[int] = None
    # False when either side didn't parse as semver, so the deltas are meaningless.
    comparable: bool = True

    @property
    def needs_update(self) -> bool:
        return self.tier in NEEDS_UPDATE_TIERS


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def classify_staleness(current, latest, available_versions=None,
                       thresholds=None) -> Staleness:
    """Classify how far `current` is behind `latest` into a colour tier

    `available_versions` is the repo's full version list. When present, patch
    distance is counted in *released versions* rather than by subtracting version
    numbers, which matters because charts skip patch numbers constantly: 1.2.0 ->
    1.2.10 can be only two actual releases.
    """
    limits = dict(STALENESS_THRESHOLDS, **(thresholds or {}))

    if not latest:
        return Staleness(TIER_UNKNOWN, 'No published version found', comparable=False)

    current_key = semver_key(current) if current else None
    latest_key = semver_key(latest)

    # Either side unparseable: we can still recognise an exact match (a date tag or
    # digest pinned to the only thing the repo publishes).
    if current_key is None or latest_key is None:
        if current and normalize_version(current) == normalize_version(latest):
            return Staleness(TIER_CURRENT, 'Up to date', comparable=False)
        return Staleness(TIER_UNKNOWN,
                         f"Cannot compare {current or '(none)'} with {latest}",
                         comparable=False)

    # Comparing keys, not strings, so `v1.2.3` == `1.2.3` and `1.2.3+build` == `1.2.3`
    if current_key == latest_key:
        return Staleness(TIER_CURRENT, 'Up to date')

    # Happens with a pinned prerelease, a yanked release, or a lagging mirror
    if current_key > latest_key:
        return Staleness(TIER_AHEAD, f'Ahead of published latest ({latest})')

    major_behind = latest_key[0] - current_key[0]
    # Clamped: across a major bump the lower components can go negative, and a
    # negative "minor versions behind" would be nonsense to report.
    minor_behind = max(0, latest_key[1] - current_key[1])
    patch_behind = max(0, latest_key[2] - current_key[2])
    releases_behind = count_releases_between(current_key, latest_key, available_versions)

    deltas = dict(major_behind=major_behind, minor_behind=minor_behind,
                  patch_behind=patch_behind, releases_behind=releases_behind)

    if major_behind > 0:
        return Staleness(TIER_MAJOR, _plural(major_behind, 'major version') + ' behind',
                         **deltas)

    if minor_behind > limits['max_minor_behind_for_stale_tier']:
        return Staleness(TIER_MAJOR, _plural(minor_behind, 'minor version') + ' behind',
                         **deltas)

    if minor_behind > 0:
        return Staleness(TIER_STALE, _plural(minor_behind, 'minor version') + ' behind',
                         **deltas)

    # Same major.minor, so patch distance decides yellow vs orange.
    if releases_behind is not None:
        steps, noun = releases_behind, 'patch release'
    else:
        # No list to count against. max(..., 1) is load-bearing: a prerelease of the
        # latest version has a patch delta of 0 but is genuinely behind, and must
        # never render as "0 patch versions behind".
        steps, noun = max(patch_behind, 1), 'patch version'

    tier = TIER_PATCH if steps <= limits['max_patch_releases_for_patch_tier'] else TIER_STALE

    if (current_key[:3] == latest_key[:3] and is_prerelease_key(current_key)
            and not is_prerelease_key(latest_key)):
        label = f'Prerelease; {latest} is released'
    else:
        label = _plural(steps, noun) + ' behind'

    return Staleness(tier, label, **deltas)


def staleness_thresholds_from_env() -> dict:
    """Read threshold overrides from the environment, ignoring unparseable values"""
    env_keys = {
        'max_patch_releases_for_patch_tier': 'STALENESS_MAX_PATCH_RELEASES',
        'max_minor_behind_for_stale_tier': 'STALENESS_MAX_MINOR_BEHIND',
    }

    overrides = {}
    for key, env_var in env_keys.items():
        raw = os.getenv(env_var)
        if raw is None:
            continue
        try:
            overrides[key] = int(raw)
        except ValueError:
            print(f"⚠ Ignoring {env_var}={raw!r}: not an integer")

    return overrides
