#!/usr/bin/env python3
"""Where a Helm chart lives: classifying repoURLs and resolving OCI references.

Pure string handling, stdlib only, so both the version tracker and the diff
renderer can share it - and so the two can't disagree about where a chart lives.
"""

from typing import Optional


def is_helm_repo_url(repo_url: str) -> bool:
    """True for HTTP(S) chart repos and OCI registries, False for git sources"""
    if repo_url.startswith(('http://', 'https://')):
        # A git repo served over https still isn't a chart repo
        return not repo_url.endswith('.git')

    if repo_url.startswith('oci://'):
        return True

    if repo_url.startswith(('git@', 'ssh://', 'git://')):
        return False

    # Argo also accepts a scheme-less OCI reference (ghcr.io/deliveryhero/helm-charts).
    # Treat the first segment as a registry if it looks like a host, the same way
    # container tooling distinguishes a registry from a bare repository name.
    host = repo_url.split('/', 1)[0]
    return '.' in host or ':' in host or host == 'localhost'


def is_oci_repo_url(repo_url: str) -> bool:
    """True for OCI registry references, with or without the oci:// scheme"""
    if repo_url.startswith('oci://'):
        return True
    if repo_url.startswith(('http://', 'https://')):
        return False
    return is_helm_repo_url(repo_url)


def oci_chart_location(chart_name: str, repo_url: str) -> Optional[str]:
    """Resolve an Argo OCI source to a `host/repository` path, or None if unparseable

    Templates write OCI sources both ways: repoURL may already end with the chart
    (oci://quay.io/cilium/charts/cilium + chart: cilium), or name only the namespace
    (ghcr.io/deliveryhero/helm-charts + chart: node-problem-detector). Getting this
    wrong turns the first form into `.../cilium/cilium`, which 404s.
    """
    location = repo_url.split('://', 1)[-1].strip('/')
    host, _, repository = location.partition('/')

    if not host or not repository:
        return None

    if repository.rsplit('/', 1)[-1] != chart_name:
        repository = f"{repository}/{chart_name}"

    return f"{host}/{repository}"
