#!/usr/bin/env python3
"""Render two versions of a Helm chart and diff them.

This reproduces the team's `Helm Diff` GitHub Actions workflow locally: that
workflow installs the helm-diff plugin but never uses it, so its real output is
two default-values `helm template` renders compared with `diff -u`. None of that
needs cluster access, so it runs here in seconds instead of minutes in CI.

One deliberate difference from the workflow: it renders with release names `old`
and `new`. Most charts interpolate .Release.Name into their fullname template, so
that leaks into every resource name and instance label - measured at ~40% of the
workflow's diff output, and 481 spurious lines when diffing a version against
itself. Both sides here use the same release name.

stdlib only (no Flask import either), so it stays unit-testable without a helm
binary and the endpoint can move to an async job model without touching it.
"""

import difflib
import os
import re
import subprocess
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

from chartrefs import is_oci_repo_url, oci_chart_location

HELM_BINARY = os.getenv('HELM_BINARY', 'helm')
RENDER_TIMEOUT = int(os.getenv('HELM_RENDER_TIMEOUT', '90'))
MAX_RENDER_BYTES = int(os.getenv('HELM_MAX_RENDER_BYTES', str(16 * 1024 * 1024)))
MAX_DIFF_LINES = int(os.getenv('HELM_MAX_DIFF_LINES', '20000'))
MAX_CONCURRENT_RENDERS = int(os.getenv('HELM_MAX_CONCURRENT_RENDERS', '2'))
# How long a request waits for a render slot before giving up with 429
SEMAPHORE_TIMEOUT = int(os.getenv('HELM_SEMAPHORE_TIMEOUT', '5'))
# Rendered manifests are large; bound the cache by count and by total bytes
MAX_CACHE_ENTRIES = int(os.getenv('HELM_MAX_CACHE_ENTRIES', '64'))
MAX_CACHE_BYTES = int(os.getenv('HELM_MAX_CACHE_BYTES', str(128 * 1024 * 1024)))

DEFAULT_NAMESPACE = 'default'

# Leading '-' is rejected separately: a list argv stops shell injection but not
# flag injection, and helm would happily read `--kubeconfig=...` as a flag.
CHART_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,126}$')
VERSION_RE = re.compile(r'^v?[0-9][0-9A-Za-z.+_-]{0,63}$')
RELEASE_NAME_RE = re.compile(r'^[a-z0-9][a-z0-9.-]{0,52}$')
MAX_REPO_URL_LENGTH = 512


class HelmError(Exception):
    """A helm failure with a stable code and a message fit to show a user"""

    def __init__(self, code: str, message: str, detail: str = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    def as_dict(self) -> Dict:
        return {'code': self.code, 'message': self.message, 'detail': self.detail}


class ValidationError(HelmError):
    def __init__(self, message: str):
        super().__init__('INVALID_INPUT', message)


# Checked in order: the specific markers must win over the generic ones. Helm says
# "not found" for both a missing chart and some template failures, so the template
# markers are tested first.
_ERROR_RULES = [
    ('REGISTRY_AUTH', ('unauthorized', 'authentication required', 'denied:',
                       'forbidden', '401 ', '403 ')),
    ('NETWORK', ('dial tcp', 'no such host', 'i/o timeout', 'connection refused',
                 'tls:', 'x509:', 'certificate')),
    ('RENDER_FAILED', ('execution error at', 'required value', 'nil pointer',
                       "don't meet the specifications", 'error validating',
                       'template:', 'at <')),
    ('CHART_NOT_FOUND', ('not found', 'no chart name found', 'could not find',
                         '404')),
]

_MESSAGES = {
    'REGISTRY_AUTH': ("{chart} {version} is in a registry that requires credentials. "
                      "The dashboard pulls anonymously, so it can't render this chart - "
                      "use the Helm Diff workflow in CI, or run helm template locally."),
    'NETWORK': "Couldn't reach {repo}. The registry may be down or unreachable from this pod.",
    'RENDER_FAILED': ("{chart} {version} can't be rendered with default values - it needs "
                      "configuration this chart doesn't default (a hostname, a password, a "
                      "storage class). The CI workflow has the same limitation."),
    'CHART_NOT_FOUND': ("{chart} {version} isn't available from {repo}. It may have been "
                        "removed, or the repo URL in the Argo template may be stale."),
}


def _require_text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")

    text = value.strip()

    # Rejected before any pattern check so the reason is specific
    if text.startswith('-'):
        raise ValidationError(f"{field} may not start with '-'")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in text) or any(ch.isspace() for ch in text):
        raise ValidationError(f"{field} contains whitespace or control characters")

    return text


def validate_chart_name(chart_name) -> str:
    text = _require_text(chart_name, 'chart_name')
    if not CHART_NAME_RE.match(text):
        raise ValidationError(f"chart_name {text!r} is not a valid chart name")
    return text


def validate_version(version) -> str:
    text = _require_text(version, 'version')
    if not VERSION_RE.match(text):
        raise ValidationError(f"version {text!r} is not a valid chart version")
    return text


def validate_release_name(release_name) -> str:
    text = _require_text(release_name, 'release_name').lower()
    if not RELEASE_NAME_RE.match(text):
        raise ValidationError(f"release_name {text!r} is not a valid Helm release name")
    return text


def validate_repo_url(repo_url) -> str:
    text = _require_text(repo_url, 'repo_url')

    if len(text) > MAX_REPO_URL_LENGTH:
        raise ValidationError('repo_url is too long')
    # Credentials in the URL, and scp-style git remotes, are both out of scope
    if '@' in text:
        raise ValidationError('repo_url may not contain credentials')

    # Any other scheme is rejected outright. Without this explicit check a
    # scheme-less OCI reference like `file:///etc` slips through: is_oci_repo_url
    # treats the first path segment as a registry host, and `file:` contains a
    # colon, so it looks host-shaped.
    if '://' in text and not text.startswith(('https://', 'oci://')):
        raise ValidationError(f"repo_url {text!r} must use https:// or oci://")

    # Only chart sources helm can pull anonymously.
    if text.startswith('https://') or is_oci_repo_url(text):
        return text

    raise ValidationError(f"repo_url {text!r} must be an https:// Helm repo or an OCI registry")


def chart_reference(chart_name: str, repo_url: str) -> Tuple[str, List[str]]:
    """The chart argument and extra flags for `helm template`

    HTTPS repos use --repo rather than `helm repo add`: it takes helm's in-memory
    path, which never touches the shared repositories.yaml, so concurrent renders
    can't collide and there's no `helm repo update` staleness window.
    """
    if is_oci_repo_url(repo_url):
        location = oci_chart_location(chart_name, repo_url)
        if not location:
            raise ValidationError(f"Could not parse OCI reference: {repo_url}")
        return f"oci://{location}", []

    return chart_name, ['--repo', repo_url]


def build_template_argv(release_name: str, chart_name: str, repo_url: str,
                        version: str, namespace: str = DEFAULT_NAMESPACE) -> List[str]:
    """The full argv for one render. No --set/--values: defaults only, matching CI."""
    chart_arg, extra = chart_reference(chart_name, repo_url)
    return ([HELM_BINARY, 'template', release_name, chart_arg]
            + extra
            + ['--version', version, '--namespace', namespace])


def helm_env() -> Dict[str, str]:
    """A minimal environment for the helm subprocess

    Deliberately not os.environ: that carries SSH_KEY_CONTENT / SSH_KEY_CONTENT_BASE64,
    the git deploy key, which has no business in a process that talks to arbitrary
    chart registries.
    """
    env = {'PATH': os.environ.get('PATH', '/usr/local/bin:/usr/bin:/bin')}

    passthrough = (
        'HOME', 'HELM_CACHE_HOME', 'HELM_CONFIG_HOME', 'HELM_DATA_HOME',
        'HELM_REGISTRY_CONFIG', 'HELM_REPOSITORY_CACHE', 'HELM_REPOSITORY_CONFIG',
        'SSL_CERT_FILE', 'SSL_CERT_DIR',
        'HTTP_PROXY', 'HTTPS_PROXY', 'NO_PROXY',
        'http_proxy', 'https_proxy', 'no_proxy',
    )
    for key in passthrough:
        if key in os.environ:
            env[key] = os.environ[key]

    return env


def classify_helm_failure(stderr: str, chart_name: str, repo_url: str,
                          version: str) -> HelmError:
    """Turn helm's stderr into a coded, user-facing error"""
    haystack = (stderr or '').lower()

    code = 'RENDER_FAILED'
    for candidate, markers in _ERROR_RULES:
        if any(marker in haystack for marker in markers):
            code = candidate
            break

    message = _MESSAGES[code].format(chart=chart_name, repo=repo_url, version=version)
    return HelmError(code, message, detail=(stderr or '').strip()[:4000])


_render_cache = OrderedDict()
_render_cache_bytes = 0
_cache_lock = threading.Lock()
_inflight = {}
_render_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)


def clear_render_cache() -> None:
    """Drop every cached render. Called when a refresh starts."""
    global _render_cache_bytes
    with _cache_lock:
        _render_cache.clear()
        _render_cache_bytes = 0


def _cache_get(key) -> Optional[str]:
    with _cache_lock:
        if key not in _render_cache:
            return None
        _render_cache.move_to_end(key)
        return _render_cache[key]


def _cache_put(key, text: str) -> None:
    global _render_cache_bytes
    size = len(text)
    if size > MAX_CACHE_BYTES:
        return

    with _cache_lock:
        if key in _render_cache:
            _render_cache_bytes -= len(_render_cache.pop(key))
        _render_cache[key] = text
        _render_cache_bytes += size

        while (len(_render_cache) > MAX_CACHE_ENTRIES
               or _render_cache_bytes > MAX_CACHE_BYTES):
            _, evicted = _render_cache.popitem(last=False)
            _render_cache_bytes -= len(evicted)


def _run_helm(argv: List[str], chart_name: str, repo_url: str, version: str) -> str:
    """Run one helm command under the concurrency cap, returning stdout"""
    if not _render_semaphore.acquire(timeout=SEMAPHORE_TIMEOUT):
        raise HelmError('RATE_LIMITED',
                        'Another diff is still rendering. Try again in a moment.')

    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=RENDER_TIMEOUT,
            env=helm_env(),
            # Own process group, so a wedged child tree can be signalled as a unit
            start_new_session=True,
        )
    except FileNotFoundError:
        raise HelmError('HELM_MISSING',
                        'The helm binary is not available in this container. '
                        'Check /debug.')
    except subprocess.TimeoutExpired:
        raise HelmError('TIMEOUT',
                        f'Rendering {chart_name} {version} timed out after '
                        f'{RENDER_TIMEOUT}s. Large charts sometimes exceed the limit.')
    finally:
        _render_semaphore.release()

    if result.returncode != 0:
        raise classify_helm_failure(result.stderr, chart_name, repo_url, version)

    if len(result.stdout) > MAX_RENDER_BYTES:
        raise HelmError('OUTPUT_TOO_LARGE',
                        f'{chart_name} {version} rendered more than '
                        f'{MAX_RENDER_BYTES // (1024 * 1024)}MB of manifests.')

    return result.stdout


def render_chart(chart_name: str, repo_url: str, version: str, *,
                 release_name: str = None,
                 namespace: str = DEFAULT_NAMESPACE) -> str:
    """Render one chart version to a manifest string, with caching

    Cache keys are immutable released versions, so entries never go stale within a
    run; clear_render_cache() on refresh covers the republished-version case.
    """
    chart_name = validate_chart_name(chart_name)
    repo_url = validate_repo_url(repo_url)
    version = validate_version(version)
    release_name = validate_release_name(release_name or chart_name)

    key = (chart_name, repo_url, version, release_name, namespace)

    cached = _cache_get(key)
    if cached is not None:
        return cached

    # Collapse duplicate concurrent renders: two people opening the same card
    # shouldn't each pay for the same helm invocation.
    with _cache_lock:
        event = _inflight.get(key)
        owner = event is None
        if owner:
            event = _inflight[key] = threading.Event()

    if not owner:
        event.wait(RENDER_TIMEOUT + SEMAPHORE_TIMEOUT)
        cached = _cache_get(key)
        if cached is not None:
            return cached
        # The owner failed or timed out; fall through and try for ourselves.

    try:
        text = _run_helm(
            build_template_argv(release_name, chart_name, repo_url, version, namespace),
            chart_name, repo_url, version)
        _cache_put(key, text)
        return text
    finally:
        if owner:
            with _cache_lock:
                _inflight.pop(key, None)
            event.set()


def diff_versions(chart_name: str, repo_url: str, old_version: str, new_version: str, *,
                  release_name: str = None, namespace: str = DEFAULT_NAMESPACE,
                  context_lines: int = 3) -> Dict:
    """Unified diff between two rendered versions of the same chart"""
    started = time.monotonic()

    chart_name = validate_chart_name(chart_name)
    release_name = validate_release_name(release_name or chart_name)
    old_version = validate_version(old_version)
    new_version = validate_version(new_version)

    was_cached = all(
        _cache_get((chart_name, repo_url.strip(), version, release_name, namespace)) is not None
        for version in (old_version, new_version))

    render = dict(release_name=release_name, namespace=namespace)
    old_text = render_chart(chart_name, repo_url, old_version, **render)
    new_text = render_chart(chart_name, repo_url, new_version, **render)

    lines = list(difflib.unified_diff(
        old_text.splitlines(), new_text.splitlines(),
        fromfile=f'{chart_name}-{old_version}.yaml',
        tofile=f'{chart_name}-{new_version}.yaml',
        n=context_lines, lineterm=''))

    truncated = len(lines) > MAX_DIFF_LINES
    if truncated:
        lines = lines[:MAX_DIFF_LINES]
        lines.append(f'... truncated at {MAX_DIFF_LINES} lines; download for the full diff ...')

    # +++/--- are file headers, not content changes
    added = sum(1 for line in lines if line.startswith('+') and not line.startswith('+++'))
    removed = sum(1 for line in lines if line.startswith('-') and not line.startswith('---'))

    return {
        'chart_name': chart_name,
        'repo_url': repo_url,
        'release_name': release_name,
        'old_version': old_version,
        'new_version': new_version,
        'identical': not lines,
        'truncated': truncated,
        'line_count': len(lines),
        'added': added,
        'removed': removed,
        'cached': was_cached,
        'elapsed_ms': int((time.monotonic() - started) * 1000),
        'diff': '\n'.join(lines),
    }


_helm_version = None
_helm_version_checked = False


def helm_version() -> Optional[str]:
    """The installed helm version, or None if helm isn't usable. Never raises."""
    global _helm_version, _helm_version_checked

    if _helm_version_checked:
        return _helm_version

    try:
        result = subprocess.run([HELM_BINARY, 'version', '--short'],
                                capture_output=True, text=True, timeout=10,
                                env=helm_env())
        if result.returncode == 0:
            _helm_version = result.stdout.strip()
    except Exception:
        _helm_version = None

    _helm_version_checked = True
    return _helm_version
