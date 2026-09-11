#!/usr/bin/env python3
"""
Helm Chart Version Tracker for Multi-Cluster Setup
Analyzes private git repository to find current chart versions across clusters
and compares with latest available versions
"""

import argparse
import os
import yaml
import requests
import subprocess
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, field
from pathlib import Path
import tempfile
import re
import concurrent.futures
import threading
from urllib.parse import urljoin

from chartrefs import is_helm_repo_url, is_oci_repo_url, oci_chart_location
from staleness import (
    NEEDS_UPDATE_TIERS, SEMVER_PATTERN, TIER_ICONS, TIER_LABELS, TIER_ORDER,
    Staleness, classify_staleness, resolve_latest, select_latest_version,
    semver_key, staleness_thresholds_from_env,
)

@dataclass
class ChartInfo:
    name: str
    cluster: str
    current_version: str
    latest_version: str = None
    repo_url: str = None
    chart_name: str = None
    needs_update: bool = False
    source: str = None  # Which cluster app file this came from
    class_name: str = None  # Ingress class (external/internal) for apps with a class list
    release_name: str = None  # Helm release name, so both sides of a diff render alike
    # Every version the chart's repo publishes, used to count how many releases behind
    # this pin is. Deliberately not included in the dashboard JSON - see api/versions.
    available_versions: List[str] = field(default_factory=list)
    staleness: Staleness = None
    staleness_thresholds: dict = None

    def __post_init__(self):
        # Charts with no Argo mapping never get apply_versions() called, so classify
        # here too - they land in the 'unknown' tier.
        if self.staleness is None:
            self.recompute_staleness()

    def recompute_staleness(self):
        self.staleness = classify_staleness(
            self.current_version, self.latest_version, self.available_versions,
            self.staleness_thresholds)
        self.needs_update = self.staleness.tier in NEEDS_UPDATE_TIERS

    def apply_versions(self, versions: List[str]):
        """Attach a freshly fetched version list, then re-derive latest + staleness

        The single place latest_version/needs_update/staleness are computed from a
        fetch result, so the three can't drift apart.
        """
        self.available_versions = list(versions or [])
        self.latest_version = resolve_latest(self.available_versions)
        self.recompute_staleness()

class HelmChartTracker:
    DEFAULT_CLUSTERS = ['mgmt', 'nwc1', 'mlc1', 'nwc3', 'mlc3']

    # Per-cluster app files to read, as (source label, filename). Both use the same
    # `apps: {name: {enable, chartVersion}}` schema.
    APP_SOURCES = [
        ('infraapps', 'infraapps.yaml'),
        ('bootstrapapps', 'bootstrapapps.yaml'),
    ]

    # Whole-line Go template control directives, dropped before YAML parsing
    TEMPLATE_DIRECTIVE_LINE = re.compile(
        r'^\s*\{\{-?\s*(if|else|end|range|with|define|template|include|block)\b.*\}\}\s*$')
    # Any `.Values.apps.<name>.enable` reference, wherever it appears in a template
    APP_NAME_PATTERN = re.compile(r'\.Values\.apps\.([A-Za-z0-9_-]+)\.enable')
    # Any remaining {{ ... }} expression, replaced with a placeholder scalar
    TEMPLATE_EXPRESSION = re.compile(r'\{\{-?.*?-?\}\}', re.DOTALL)

    # Directories holding Argo Application templates. Missing ones are skipped.
    TEMPLATE_DIRS = [
        os.path.join('infra-chart', 'templates'),
        os.path.join('bootstrap-chart', 'templates'),
    ]

    def __init__(self, git_repo_url: str, ssh_key_path: str = None, clusters: List[str] = None):
        self.git_repo_url = git_repo_url
        self.ssh_key_path = ssh_key_path
        self.clusters = list(clusters) if clusters else list(self.DEFAULT_CLUSTERS)
        self.chart_mappings = {}  # Maps app names to chart info
        # Every version a chart publishes: {(chart_name, repo_url): [version, ...]}.
        # An empty list is a cached failure, so a miss isn't retried within a run.
        self.version_cache = {}
        self.cache_lock = threading.Lock()  # Thread safety for cache
        self.staleness_thresholds = staleness_thresholds_from_env()
        
    def setup_git_ssh(self):
        """Setup SSH configuration for private repository access"""
        ssh_key_content = os.getenv('SSH_KEY_CONTENT')
        ssh_key_content_base64 = os.getenv('SSH_KEY_CONTENT_BASE64')
        
        if ssh_key_content_base64:
            # Decode base64 encoded key
            import base64
            try:
                ssh_key_content = base64.b64decode(ssh_key_content_base64).decode('utf-8')
                print("✓ Decoded base64 SSH key content")
            except Exception as e:
                print(f"✗ Failed to decode base64 SSH key: {e}")
                return
        
        if ssh_key_content:
            # Create SSH directory
            ssh_dir = '/tmp/.ssh'
            os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
            
            # Create SSH key file
            ssh_key_file = os.path.join(ssh_dir, 'id_key')
            
            # Debug: Show first and last few characters of key
            print(f"SSH key starts with: {ssh_key_content[:50]}...")
            print(f"SSH key ends with: ...{ssh_key_content[-50:]}")
            
            # Ensure the key content has proper formatting
            if not ssh_key_content.strip().startswith('-----BEGIN'):
                print("✗ SSH key content doesn't start with proper header")
                return
            
            if not ssh_key_content.strip().endswith('-----END OPENSSH PRIVATE KEY-----') and \
               not ssh_key_content.strip().endswith('-----END RSA PRIVATE KEY-----') and \
               not ssh_key_content.strip().endswith('-----END PRIVATE KEY-----'):
                print("✗ SSH key content doesn't end with proper footer")
                return
            
            # Write the key with proper formatting
            try:
                with open(ssh_key_file, 'w') as f:
                    # Write exactly as provided, but ensure it ends with newline
                    key_content = ssh_key_content.strip() + '\n'
                    f.write(key_content)
                
                # Set proper permissions
                os.chmod(ssh_key_file, 0o600)
                
                # Verify file was written correctly
                with open(ssh_key_file, 'r') as f:
                    written_content = f.read()
                    print(f"✓ SSH key file created with {len(written_content)} characters")
                
                self.ssh_key_path = ssh_key_file
                print(f"✓ Created SSH key file at {ssh_key_file}")
                
            except Exception as e:
                print(f"✗ Failed to write SSH key file: {e}")
                return
        
        if self.ssh_key_path:
            # Expand user path if needed
            ssh_key_path = os.path.expanduser(self.ssh_key_path)
            
            # Verify key file exists and has content
            if not os.path.exists(ssh_key_path):
                print(f"✗ SSH key file does not exist at {ssh_key_path}")
                return
            
            # Check file permissions and size
            file_stat = os.stat(ssh_key_path)
            file_perms = oct(file_stat.st_mode)[-3:]
            file_size = file_stat.st_size
            print(f"✓ SSH key file: {file_size} bytes, permissions: {file_perms}")
            
            # Create SSH config to avoid known_hosts issues
            ssh_config_file = '/tmp/.ssh/config'
            with open(ssh_config_file, 'w') as f:
                f.write("""Host github.com
    HostName github.com
    User git
    IdentityFile {}
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR
""".format(ssh_key_path))
            os.chmod(ssh_config_file, 0o600)
            
            # Set SSH environment variables
            os.environ['SSH_CONFIG_FILE'] = ssh_config_file
            os.environ['GIT_SSH_COMMAND'] = f'ssh -F {ssh_config_file}'
            
            print(f"✓ SSH configuration created")
            print(f"✓ GIT_SSH_COMMAND set to: ssh -F {ssh_config_file}")
    
    def clone_repo(self, target_dir: str) -> bool:
        """Clone the private git repository to analyze"""
        try:
            self.setup_git_ssh()
            
            # Test SSH connection first
            if self.ssh_key_path:
                print("Testing SSH connection to GitHub...")
                test_cmd = ['ssh', '-i', os.path.expanduser(self.ssh_key_path), 
                           '-o', 'StrictHostKeyChecking=no', 
                           '-o', 'UserKnownHostsFile=/dev/null',
                           '-o', 'PasswordAuthentication=no',
                           '-T', 'git@github.com']
                
                try:
                    result = subprocess.run(test_cmd, capture_output=True, text=True, timeout=10)
                    print(f"SSH test output: {result.stderr}")
                    if "successfully authenticated" in result.stderr or "You've successfully authenticated" in result.stderr:
                        print("✓ SSH authentication successful")
                    else:
                        print("⚠ SSH authentication may have issues")
                except subprocess.TimeoutExpired:
                    print("SSH test timed out")
                except Exception as e:
                    print(f"SSH test failed: {e}")
            
            print(f"Cloning repository: {self.git_repo_url}")
            subprocess.run(['git', 'clone', self.git_repo_url, target_dir], 
                          check=True, capture_output=True, text=True)
            print("✓ Repository cloned successfully")
            return True
            
        except subprocess.CalledProcessError as e:
            print(f"Failed to clone repository: {e}")
            print(f"Error output: {e.stderr if e.stderr else 'No error output'}")
            print(f"Standard output: {e.stdout if e.stdout else 'No standard output'}")
            return False
    
    def parse_cluster_app_file(self, repo_path: str, cluster: str, filename: str) -> Dict[str, Dict]:
        """Parse one of a cluster's app files (infraapps.yaml, bootstrapapps.yaml, ...)
        
        An app is kept if it's enabled and pins a version either at the top level or on at
        least one entry of a `class:` list (traefik2 and friends pin per class only).
        """
        app_file_path = os.path.join(repo_path, 'clusters', cluster, filename)
        
        if not os.path.exists(app_file_path):
            print(f"Warning: {filename} not found for cluster {cluster} at {app_file_path}")
            return {}
        
        with open(app_file_path, 'r') as f:
            app_file_data = yaml.safe_load(f) or {}
        
        apps = app_file_data.get('apps', {})
        chart_info = {}
        
        for app_name, app_config in apps.items():
            if not app_config.get('enable', False):
                continue
            
            base_version = app_config.get('chartVersion')
            classes = self.parse_app_classes(app_name, app_config, base_version)
            
            if not classes and not base_version:
                continue
            
            chart_info[app_name] = {
                'chartVersion': base_version,
                'policiesChartVersion': app_config.get('policiesChartVersion'),
                'classes': classes
            }
        
        return chart_info

    @staticmethod
    def parse_app_classes(app_name: str, app_config: Dict, base_version: str) -> List[Dict]:
        """Normalize an app's `class:` list into [{'name', 'chartVersion'}, ...]
        
        A class without its own chartVersion inherits the app's top-level one.
        """
        raw_classes = app_config.get('class') or []
        
        if not isinstance(raw_classes, list):
            print(f"    ⚠ {app_name}: 'class' is not a list, ignoring it")
            return []
        
        classes = []
        for entry in raw_classes:
            if not isinstance(entry, dict):
                print(f"    ⚠ {app_name}: skipping malformed class entry {entry!r}")
                continue
            
            class_name = entry.get('name')
            class_version = entry.get('chartVersion', base_version)
            
            if not class_name:
                print(f"    ⚠ {app_name}: skipping class entry with no name")
                continue
            if not class_version:
                print(f"    ⚠ {app_name}: class '{class_name}' has no chartVersion, skipping")
                continue
            
            classes.append({'name': class_name, 'chartVersion': class_version})
        
        return classes

    def parse_cluster_infraapps(self, repo_path: str, cluster: str) -> Dict[str, Dict]:
        """Parse the infraapps.yaml file for a specific cluster"""
        return self.parse_cluster_app_file(repo_path, cluster, 'infraapps.yaml')

    def parse_cluster_bootstrapapps(self, repo_path: str, cluster: str) -> Dict[str, Dict]:
        """Parse the bootstrapapps.yaml file for a specific cluster"""
        return self.parse_cluster_app_file(repo_path, cluster, 'bootstrapapps.yaml')

    def parse_cluster_apps(self, repo_path: str, cluster: str) -> List[Tuple[str, Dict, str]]:
        """Collect every enabled app for a cluster across all app files
        
        Returns (app_name, version_info, source) tuples, where version_info carries a
        resolved 'chartVersion' plus an optional 'className'. An app with a `class:` list
        yields one tuple per class, since each class is deployed as its own release and can
        sit on a different version.
        
        A list rather than a dict so neither multiple classes nor an app appearing in more
        than one file end up overwriting each other.
        """
        cluster_apps = []
        
        for source, filename in self.APP_SOURCES:
            apps = self.parse_cluster_app_file(repo_path, cluster, filename)
            entries = []
            
            for app_name, version_info in apps.items():
                classes = version_info.get('classes') or []
                policies_version = version_info.get('policiesChartVersion')
                
                if classes:
                    base_version = version_info.get('chartVersion')
                    class_versions = {c['chartVersion'] for c in classes}
                    
                    # A top-level pin that no class agrees with is worth surfacing: the
                    # classes are what actually deploy, so the top level may be stale.
                    if base_version and base_version not in class_versions:
                        print(f"    ⚠ {app_name}: top-level chartVersion {base_version} "
                              f"differs from its class versions "
                              f"({', '.join(sorted(class_versions))}); reporting classes only")
                    
                    for class_info in classes:
                        entries.append((app_name, {
                            'chartVersion': class_info['chartVersion'],
                            'policiesChartVersion': policies_version,
                            'className': class_info['name']
                        }, source))
                else:
                    entries.append((app_name, {
                        'chartVersion': version_info['chartVersion'],
                        'policiesChartVersion': policies_version,
                        'className': None
                    }, source))
            
            print(f"  {source}: {len(apps)} enabled apps -> {len(entries)} versioned releases")
            cluster_apps.extend(entries)
        
        return cluster_apps
    
    def strip_go_template(self, content: str) -> str:
        """Turn a Go-templated Argo Application into something YAML can parse
        
        Whole-line control directives ({{- if }}, {{- end }}, ...) are dropped, and any
        remaining {{ ... }} expression becomes a plain scalar placeholder.
        """
        lines = [line for line in content.splitlines()
                 if not self.TEMPLATE_DIRECTIVE_LINE.match(line)]
        return self.TEMPLATE_EXPRESSION.sub('TEMPLATED', '\n'.join(lines))

    def parse_argo_application_template(self, template_path: str) -> List[Dict[str, str]]:
        """Parse a single Argo Application template to extract all chart and repo info
        
        Handles both `spec.source` (single) and `spec.sources` (list), and both classic
        Helm repos (https://) and OCI registries (oci://).
        """
        if not os.path.exists(template_path):
            return []
        
        try:
            with open(template_path, 'r') as f:
                content = f.read()
        except Exception as e:
            print(f"Error reading {template_path}: {e}")
            return []
        
        try:
            application = yaml.safe_load(self.strip_go_template(content))
        except yaml.YAMLError as e:
            print(f"    ⚠ Could not parse {os.path.basename(template_path)} as YAML ({e});"
                  f" falling back to regex scan")
            return self.parse_argo_application_regex(content)
        
        if not isinstance(application, dict):
            print(f"    ⚠ {os.path.basename(template_path)} did not parse to a mapping;"
                  f" falling back to regex scan")
            return self.parse_argo_application_regex(content)
        
        spec = application.get('spec') or {}
        
        # Argo allows either `source` (single) or `sources` (list)
        raw_sources = spec.get('sources') or []
        if not isinstance(raw_sources, list):
            raw_sources = []
        if isinstance(spec.get('source'), dict):
            raw_sources = [spec['source']] + raw_sources
        
        charts = []
        for source in raw_sources:
            if not isinstance(source, dict):
                continue
            
            chart = self.chart_from_source(source)
            if chart:
                charts.append(chart)
                print(f"    Found chart: {chart['chart_name']} "
                      f"(release: {chart['release_name']}) @ {chart['repo_url']}")
        
        return charts

    def chart_from_source(self, source: Dict) -> Optional[Dict[str, str]]:
        """Turn one Argo source mapping into chart info, or None if it isn't a Helm chart"""
        repo_url = source.get('repoURL')
        chart_name = source.get('chart')
        
        # Sources without a `chart` are git/ref sources (path-based or $values)
        if not repo_url or not chart_name:
            return None
        
        repo_url = str(repo_url).strip()
        chart_name = str(chart_name).strip()
        
        if not self.is_helm_repo_url(repo_url):
            print(f"    Skipping non-Helm repo: {repo_url}")
            return None
        
        helm_config = source.get('helm')
        release_name = chart_name
        if isinstance(helm_config, dict) and helm_config.get('releaseName'):
            release_name = str(helm_config['releaseName']).strip()
        
        return {
            'chart_name': chart_name,
            'repo_url': repo_url,
            'release_name': release_name
        }

    # Chart-location rules live in chartrefs.py so differ.py can share them without
    # pulling in yaml/requests. Re-exposed here because they were part of this
    # class's surface.
    is_helm_repo_url = staticmethod(is_helm_repo_url)
    is_oci_repo_url = staticmethod(is_oci_repo_url)
    oci_chart_location = staticmethod(oci_chart_location)

    def parse_argo_application_regex(self, content: str) -> List[Dict[str, str]]:
        """Best-effort fallback for templates that won't parse as YAML"""
        charts = []
        
        sources_pattern = r'repoURL: ((?:https?|oci)://[^\s\n]+).*?chart: ([^\s\n]+)'
        matches = re.finditer(sources_pattern, content, re.DOTALL | re.MULTILINE)
        
        for match in matches:
            repo_url = match.group(1).strip().strip('"').strip("'")
            chart_name = match.group(2).strip().strip('"').strip("'")
            
            if not self.is_helm_repo_url(repo_url):
                print(f"    Skipping non-Helm repo: {repo_url}")
                continue
            
            charts.append({
                'chart_name': chart_name,
                'repo_url': repo_url,
                'release_name': chart_name
            })
            print(f"    Found chart (regex): {chart_name} @ {repo_url}")
        
        return charts
    
    def extract_app_name_from_template(self, template_path: str) -> Optional[str]:
        """Extract the app name from an Argo Application template
        
        Scans the whole file for any `.Values.apps.<name>.enable` reference rather than
        matching an exact `{{- if .Values.apps.X.enable }}` on line 1, so templates whose
        guard is compound (`{{- if and .Values.apps.npd.enable ... }}`), differently
        whitespaced, or not on the first line still resolve.
        """
        try:
            with open(template_path, 'r') as f:
                content = f.read()
        except Exception as e:
            print(f"    Error reading {template_path}: {e}")
            return None
        
        names = self.APP_NAME_PATTERN.findall(content)
        
        if not names:
            print(f"    Could not find a .Values.apps.<name>.enable reference in "
                  f"{os.path.basename(template_path)}")
            return None
        
        # First occurrence is the guard at the top of the template
        app_name = names[0]
        
        distinct = list(dict.fromkeys(names))
        if len(distinct) > 1:
            print(f"    Extracted app name from template: {app_name} "
                  f"(template also references: {', '.join(distinct[1:])})")
        else:
            print(f"    Extracted app name from template: {app_name}")
        
        return app_name

    def parse_templates_dir(self, templates_dir: str, mappings: Dict[str, List[Dict]]) -> None:
        """Parse every Argo Application template in one directory into `mappings`"""
        existing_files = list(Path(templates_dir).glob("*.yaml"))
        print(f"Found {len(existing_files)} YAML files:")
        for f in existing_files:
            print(f"  - {f.name}")
        
        for template_file in existing_files:
            print(f"\nParsing {template_file.name}...")
            
            # First, try to extract the app name from the template's first line
            app_name = self.extract_app_name_from_template(str(template_file))
            
            # Then extract all chart info from this template
            chart_info_list = self.parse_argo_application_template(str(template_file))
            
            if not chart_info_list:
                print(f"  ✗ Could not extract chart info from {template_file.name}")
                continue
            
            if not app_name:
                # Fallback to filename-based approach if we can't extract from first line
                base_name = template_file.stem
                app_name = base_name.replace('-', '').replace('_', '')
                print(f"  Chart info found but no app name extracted, using fallback: {app_name}")
            
            print(f"  Found {len(chart_info_list)} charts for app: {app_name}")
            
            if app_name in mappings:
                # Same app defined in two template dirs - keep both dirs' charts, but
                # don't list the same chart twice.
                print(f"  ⚠ {app_name} already has a mapping, merging charts")
                seen = {(c.get('chart_name'), c.get('repo_url')) for c in mappings[app_name]}
                added = [c for c in chart_info_list
                         if (c.get('chart_name'), c.get('repo_url')) not in seen]
                mappings[app_name].extend(added)
                print(f"  ✓ Merged mapping: {app_name} (+{len(added)} new charts)")
            else:
                mappings[app_name] = chart_info_list
                print(f"  ✓ Added mapping: {app_name}")

    def build_chart_mappings(self, repo_path: str) -> Dict[str, List[Dict]]:
        """Build mappings between app names and their chart information"""
        mappings = {}
        found_any_dir = False
        
        for rel_dir in self.TEMPLATE_DIRS:
            templates_dir = os.path.join(repo_path, rel_dir)
            
            if not os.path.exists(templates_dir):
                print(f"Skipping missing templates directory: {rel_dir}")
                continue
            
            found_any_dir = True
            print(f"\nParsing Argo Application templates from: {rel_dir}")
            self.parse_templates_dir(templates_dir, mappings)
        
        if not found_any_dir:
            print(f"Error: no Argo Application template directories found "
                  f"(looked for: {', '.join(self.TEMPLATE_DIRS)})")
            return mappings
        
        print(f"\nFinal mappings built: {len(mappings)} entries")
        for app, chart_list in mappings.items():
            print(f"  {app}: {len(chart_list)} charts")
            for chart in chart_list:
                print(f"    - {chart.get('chart_name', 'Unknown')} @ {chart.get('repo_url', 'Unknown')}")
        
        return mappings
    
    # Semver parsing lives in staleness.py so it can be tested without Flask/YAML.
    # Re-exposed here because these were part of this class's surface.
    SEMVER_PATTERN = SEMVER_PATTERN
    semver_key = staticmethod(semver_key)
    select_latest_version = staticmethod(select_latest_version)

    def fetch_oci_token(self, session, challenge: str, repository: str) -> Optional[str]:
        """Get an anonymous pull token from a registry's WWW-Authenticate challenge"""
        if not challenge.lower().startswith('bearer '):
            return None
        
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        realm = params.get('realm')
        if not realm:
            return None
        
        query = {'scope': params.get('scope') or f'repository:{repository}:pull'}
        if params.get('service'):
            query['service'] = params['service']
        
        response = session.get(realm, params=query, timeout=30)
        response.raise_for_status()
        payload = response.json()
        return payload.get('token') or payload.get('access_token')

    def fetch_oci_tags(self, host: str, repository: str) -> List[str]:
        """List every tag for an OCI repository, following pagination"""
        session = requests.Session()
        headers = {}
        url = f"https://{host}/v2/{repository}/tags/list"
        tags = []
        
        # Bounded so a registry with a broken Link header can't loop forever
        for _ in range(20):
            response = session.get(url, headers=headers, timeout=30)
            
            if response.status_code == 401 and 'Authorization' not in headers:
                token = self.fetch_oci_token(
                    session, response.headers.get('WWW-Authenticate', ''), repository)
                if not token:
                    response.raise_for_status()
                headers['Authorization'] = f'Bearer {token}'
                continue
            
            response.raise_for_status()
            tags.extend(response.json().get('tags') or [])
            
            # Registries paginate with a Link: <...>; rel="next" header
            link = response.headers.get('Link', '')
            next_match = re.search(r'<([^>]+)>\s*;\s*rel="?next"?', link)
            if not next_match:
                break
            url = urljoin(f"https://{host}", next_match.group(1))
        
        return tags

    def fetch_oci_chart_versions(self, chart_name: str, repo_url: str) -> List[str]:
        """Every version an OCI registry publishes for a chart

        OCI registries have no index.yaml; the chart's versions are its image tags.
        """
        location = self.oci_chart_location(chart_name, repo_url)

        if not location:
            print(f"    ✗ Could not parse OCI reference: {repo_url}")
            return []

        host, _, repository = location.partition('/')

        print(f"    Listing OCI tags from: https://{host}/v2/{repository}/tags/list")
        tags = self.fetch_oci_tags(host, repository)

        if not tags:
            print(f"    ✗ No tags returned for {repository}")
            return []

        version = resolve_latest(tags)
        if select_latest_version(tags) is None:
            print(f"    ✗ No semver tags among {len(tags)} tags for {repository}")
        else:
            print(f"    ✓ Latest version: {version} (from {len(tags)} tags)")

        return tags

    def get_chart_versions(self, chart_name: str, repo_url: str) -> List[str]:
        """Every version a chart's repository publishes, newest-first-ish (with caching)

        The full list rather than just the latest, because staleness is measured in
        *released versions* between the pin and the latest - a chart can jump 1.2.0 to
        1.2.10 in two releases - and because the diff view offers it as a version picker.
        """
        cache_key = (chart_name, repo_url)
        with self.cache_lock:
            if cache_key in self.version_cache:
                cached = self.version_cache[cache_key]
                print(f"    ✓ Using {len(cached)} cached versions")
                return cached

        if self.is_oci_repo_url(repo_url):
            versions = self.fetch_oci_chart_versions(chart_name, repo_url)
        else:
            versions = self.fetch_http_chart_versions(chart_name, repo_url)

        # An empty list is a cached failure: a miss isn't retried within a run.
        with self.cache_lock:
            self.version_cache[cache_key] = versions
        return versions

    def get_latest_chart_version(self, chart_name: str, repo_url: str) -> Optional[str]:
        """The latest version of a chart, for callers that don't need the whole list"""
        return resolve_latest(self.get_chart_versions(chart_name, repo_url))

    def fetch_http_chart_versions(self, chart_name: str, repo_url: str) -> List[str]:
        """Every version an HTTP(S) Helm repository lists for a chart, [] on any failure"""
        try:
            # For Helm repositories, fetch the index.yaml
            if repo_url.endswith('/'):
                index_url = f"{repo_url}index.yaml"
            else:
                index_url = f"{repo_url}/index.yaml"
            
            print(f"    Fetching index from: {index_url}")
            response = requests.get(index_url, timeout=30)  # Increased timeout
            response.raise_for_status()
            
            # Parse the raw bytes, not response.text: Helm repos commonly serve
            # index.yaml as `text/yaml` with no charset, and requests then falls back to
            # ISO-8859-1 per the HTTP spec. That turns UTF-8 release notes into Latin-1
            # C1 control characters, which PyYAML rejects ("unacceptable character
            # #x0080"). PyYAML handles the encoding itself when given bytes.
            index_data = yaml.safe_load(response.content) or {}
            entries = index_data.get('entries', {})
            
            if chart_name in entries:
                # index.yaml is conventionally sorted newest-first, but merged or
                # hand-edited indexes aren't reliably ordered, and the newest entry may be
                # a prerelease. Pick explicitly instead of trusting position.
                chart_versions = [entry.get('version') for entry in entries[chart_name]
                                  if entry.get('version')]

                if select_latest_version(chart_versions) is None:
                    print(f"    ⚠ No semver versions for {chart_name}, using first entry")
                print(f"    ✓ Latest version: {resolve_latest(chart_versions)} "
                      f"(from {len(chart_versions)} versions)")

                return chart_versions
            else:
                available_charts = list(entries.keys())
                print(f"    ✗ Chart '{chart_name}' not found in repository.")
                print(f"    Available charts: {available_charts[:10]}..." if len(available_charts) > 10 else f"    Available charts: {available_charts}")
                return []
            
        except requests.exceptions.Timeout:
            print(f"    ✗ Timeout fetching from {repo_url}")
        except requests.exceptions.RequestException as e:
            print(f"    ✗ Request failed for {repo_url}: {e}")
        except yaml.YAMLError as e:
            print(f"    ✗ Failed to parse YAML from {repo_url}: {e}")
        except UnicodeDecodeError as e:
            print(f"    ✗ index.yaml from {repo_url} is not valid UTF-8: {e}")
        except Exception as e:
            print(f"    ✗ Unexpected error for {chart_name} from {repo_url}: {e}")

        return []

    def fetch_chart_versions_parallel(self, charts_to_check: List[Tuple]) -> Dict:
        """Fetch multiple charts' version lists in parallel
        
        Results are keyed by (chart_name, repo_url) so two apps sharing a release name
        can't overwrite each other's versions.
        """
        print(f"  Starting parallel version checks for {len(charts_to_check)} charts...")
        
        version_results = {}
        
        def fetch_single_version(chart_tuple):
            chart_name, repo_url, display_name = chart_tuple
            try:
                versions = self.get_chart_versions(chart_name, repo_url)
                return ((chart_name, repo_url), versions)
            except Exception as e:
                print(f"    ✗ Error checking {display_name}: {e}")
                return ((chart_name, repo_url), [])
        
        # Use ThreadPoolExecutor for parallel requests
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            # Submit all requests
            future_to_chart = {
                executor.submit(fetch_single_version, chart_tuple): chart_tuple 
                for chart_tuple in charts_to_check
            }
            
            # Collect results as they complete
            for future in concurrent.futures.as_completed(future_to_chart):
                try:
                    chart_key, version = future.result()
                    version_results[chart_key] = version
                except Exception as e:
                    chart_tuple = future_to_chart[future]
                    print(f"    ✗ Failed to get version for {chart_tuple[2]}: {e}")
                    version_results[(chart_tuple[0], chart_tuple[1])] = []
        
        print(f"  ✓ Completed parallel version checks")
        return version_results
    
    def analyze_charts(self) -> List[ChartInfo]:
        """Analyze all charts across all clusters and return comparison data"""
        with tempfile.TemporaryDirectory() as temp_dir:
            if not self.clone_repo(temp_dir):
                return []
            
            # Build chart mappings from Argo Application templates
            chart_mappings = self.build_chart_mappings(temp_dir)
            
            charts = []
            
            for cluster in self.clusters:
                print(f"Analyzing cluster: {cluster}")
                
                # Parse current versions from every one of the cluster's app files
                cluster_apps = self.parse_cluster_apps(temp_dir, cluster)
                
                # Collect all charts that need version checking, deduped by
                # (chart_name, repo_url) so shared charts are fetched once
                charts_to_check = {}
                cluster_chart_info = []
                
                for app_name, version_info, source in cluster_apps:
                    current_version = version_info['chartVersion']
                    class_name = version_info.get('className')
                    
                    # Get chart information from mappings
                    chart_mapping_list = chart_mappings.get(app_name, [])
                    
                    if not chart_mapping_list:
                        print(f"  ⚠ No Argo template maps app '{app_name}' to a chart. "
                              f"Check that a template under {', '.join(self.TEMPLATE_DIRS)} "
                              f"references .Values.apps.{app_name}.enable")
                        # No mapping found - create entry without repo info
                        chart_info = ChartInfo(
                            name=self.release_display_name(app_name, class_name),
                            cluster=cluster,
                            current_version=current_version,
                            repo_url=None,
                            chart_name=None,
                            source=source,
                            class_name=class_name,
                            staleness_thresholds=self.staleness_thresholds
                        )
                        cluster_chart_info.append(chart_info)
                        continue
                    
                    # Process each chart defined in this application
                    for chart_mapping in chart_mapping_list:
                        repo_url = chart_mapping.get('repo_url')
                        chart_name = chart_mapping.get('chart_name')
                        release_name = chart_mapping.get('release_name', chart_name)
                        
                        # Use release name as the display name, qualified by class so
                        # external/internal releases stay distinct
                        display_name = self.release_display_name(release_name, class_name)
                        
                        # Determine which version to use based on the chart
                        if 'policies' in chart_name.lower() and version_info.get('policiesChartVersion'):
                            # Use policies version for policy charts
                            chart_version = version_info['policiesChartVersion']
                            print(f"  Using policies version {chart_version} for {display_name}")
                        else:
                            # Use main chart version
                            chart_version = current_version
                        
                        chart_info = ChartInfo(
                            name=display_name,
                            cluster=cluster,
                            current_version=chart_version,
                            repo_url=repo_url,
                            chart_name=chart_name,
                            source=source,
                            class_name=class_name,
                            release_name=release_name,
                            staleness_thresholds=self.staleness_thresholds
                        )
                        
                        # Queue for parallel checking if we have repo info
                        if repo_url and chart_name:
                            charts_to_check[(chart_name, repo_url)] = display_name
                        
                        cluster_chart_info.append(chart_info)
                
                # Fetch all versions in parallel
                if charts_to_check:
                    version_results = self.fetch_chart_versions_parallel(
                        [(chart_name, repo_url, display_name)
                         for (chart_name, repo_url), display_name in charts_to_check.items()]
                    )
                    
                    # Update chart info with results
                    for chart_info in cluster_chart_info:
                        chart_key = (chart_info.chart_name, chart_info.repo_url)
                        if chart_key in version_results:
                            chart_info.apply_versions(version_results[chart_key])
                
                # Add all charts from this cluster
                charts.extend(cluster_chart_info)
                print(f"✓ Completed cluster {cluster}: {len(cluster_chart_info)} charts processed")
            
            print(f"\n🎉 Analysis complete! Processed {len(charts)} charts across {len(self.clusters)} clusters")
            
            return charts
    
    @staticmethod
    def release_display_name(name: str, class_name: str = None) -> str:
        """Qualify a release name with its class, e.g. 'traefik (internal)'"""
        return f"{name} ({class_name})" if class_name else name

    @classmethod
    def describe_versions(cls, chart: ChartInfo) -> str:
        """One line describing where a chart sits, e.g. `1.2.3 → 1.2.6 (3 releases behind)`"""
        if not chart.latest_version:
            return f"{chart.current_version} ({cls.no_version_reason(chart)})"
        
        if not chart.needs_update:
            return f"{chart.current_version} ({chart.staleness.label})"
        
        return (f"{chart.current_version} → {chart.latest_version} "
                f"({chart.staleness.label})")

    @staticmethod
    def no_version_reason(chart: ChartInfo) -> Optional[str]:
        """Why a chart has no latest version, or None if it resolved fine"""
        if chart.latest_version:
            return None
        if not chart.repo_url:
            return "No chart mapping found in Argo templates"
        return "Failed to fetch latest version"

    @staticmethod
    def source_tag(chart: ChartInfo) -> str:
        """Short suffix identifying which app file a chart came from"""
        return f" [{chart.source}]" if chart.source else ""

    def summarize(self, charts: List[ChartInfo]) -> Dict:
        """Counts for a set of charts, per tier and per source

        by_tier['unknown'] can exceed no_version_info: it also counts charts whose
        latest version resolved but isn't semver-comparable.
        """
        return {
            'total_charts': len(charts),
            'needs_update': len([c for c in charts if c.needs_update]),
            'up_to_date': len([c for c in charts if not c.needs_update and c.latest_version]),
            'no_version_info': len([c for c in charts if not c.latest_version]),
            'by_tier': {tier: len([c for c in charts if c.staleness.tier == tier])
                        for tier in TIER_ORDER},
            'by_source': {source: len([c for c in charts if c.source == source])
                          for source, _ in self.APP_SOURCES}
        }

    def generate_report(self, charts: List[ChartInfo]) -> str:
        """Generate a human-readable report organized by cluster"""
        report = []
        report.append("Multi-Cluster Helm Chart Version Report")
        report.append("=" * 60)
        report.append("")
        
        # Overall summary
        summary = self.summarize(charts)
        
        report.append(f"📊 Overall Summary:")
        report.append(f"  Total charts: {summary['total_charts']}")
        report.append(f"  Need updates: {summary['needs_update']}")
        for tier in TIER_ORDER:
            count = summary['by_tier'][tier]
            if count:
                report.append(f"    {TIER_ICONS[tier]} {TIER_LABELS[tier]}: {count}")
        for source, _ in self.APP_SOURCES:
            report.append(f"  From {source}: {summary['by_source'][source]}")
        report.append("")
        
        # Group by cluster
        for cluster in self.clusters:
            cluster_charts = [c for c in charts if c.cluster == cluster]
            if not cluster_charts:
                continue
                
            report.append(f"🏢 Cluster: {cluster.upper()}")
            report.append("-" * 30)
            
            # Grouped by staleness tier, most severe first, so the urgent work is on top
            for tier in TIER_ORDER:
                tier_charts = [c for c in cluster_charts if c.staleness.tier == tier]
                if not tier_charts:
                    continue
                
                report.append(f"  {TIER_ICONS[tier]} {TIER_LABELS[tier]} ({len(tier_charts)}):")
                for chart in tier_charts:
                    report.append(f"    {chart.name}{self.source_tag(chart)}: "
                                  f"{self.describe_versions(chart)}")
                report.append("")
        
        return "\n".join(report)
    
    def generate_dashboard_data(self, charts: List[ChartInfo]) -> Dict:
        """Generate structured data for dashboard consumption"""
        clusters_data = {}
        
        for cluster in self.clusters:
            cluster_charts = [c for c in charts if c.cluster == cluster]
            clusters_data[cluster] = {
                'summary': self.summarize(cluster_charts),
                'charts': [self.chart_payload(chart) for chart in cluster_charts]
            }
        
        # Overall summary
        return {
            'summary': self.summarize(charts),
            'clusters': clusters_data
        }

    def chart_payload(self, chart: ChartInfo) -> Dict:
        """One chart as the dashboard consumes it

        Note `available_versions` is deliberately absent: a few hundred charts times
        OCI tag lists in the hundreds would make /api/charts multi-megabyte on every
        poll. The diff view fetches it lazily from /api/versions instead.
        """
        return {
            'name': chart.name,
            'current_version': chart.current_version,
            'latest_version': chart.latest_version,
            'needs_update': chart.needs_update,
            'repo_url': chart.repo_url,
            'chart_name': chart.chart_name,
            'release_name': chart.release_name,
            'source': chart.source,
            'class_name': chart.class_name,
            'staleness_tier': chart.staleness.tier,
            'staleness_label': chart.staleness.label,
            'versions_behind': chart.staleness.releases_behind,
            'no_version_reason': self.no_version_reason(chart)
        }

    @staticmethod
    def version_index(charts: List[ChartInfo]) -> Dict:
        """{(chart_name, repo_url): [versions]} for the diff view's version picker

        Kept out of the dashboard payload but retained in memory, since analyze_charts
        discards the tracker (and its cache) once a refresh finishes.
        """
        return {(c.chart_name, c.repo_url): c.available_versions
                for c in charts if c.chart_name and c.repo_url and c.available_versions}

CLUSTERS_ENV_VAR = 'CLUSTERS'


def split_cluster_names(values: List[str]) -> List[str]:
    """Flatten repeated and/or comma-separated cluster values into a clean list"""
    return [name.strip() for entry in values
            for name in entry.split(',') if name.strip()]


def validate_clusters(names: List[str]) -> List[str]:
    """Reject cluster names that aren't part of the known set"""
    unknown = [c for c in names if c not in HelmChartTracker.DEFAULT_CLUSTERS]
    if unknown:
        raise ValueError("unknown cluster(s): %s (known: %s)"
                         % (', '.join(unknown), ', '.join(HelmChartTracker.DEFAULT_CLUSTERS)))
    return names


def clusters_from_env() -> Optional[List[str]]:
    """Read the cluster selection from the CLUSTERS environment variable"""
    raw = os.getenv(CLUSTERS_ENV_VAR)
    if not raw:
        return None

    names = split_cluster_names([raw])
    if not names:
        return None

    return validate_clusters(names)


def parse_args(argv: List[str] = None):
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(
        description="Compare Helm chart versions across clusters with the latest available versions"
    )
    parser.add_argument(
        '-c', '--cluster',
        action='append',
        dest='clusters',
        metavar='NAME',
        help=("Only analyze this cluster. Repeat the flag or use a comma-separated "
              "list to select several. Overrides the %s environment variable. "
              "Default: %s" % (CLUSTERS_ENV_VAR, ', '.join(HelmChartTracker.DEFAULT_CLUSTERS)))
    )
    parser.add_argument(
        '--repo-url',
        default=os.getenv('GIT_REPO_URL', "git@github.com:NCAR/cisl-cloud-charts.git"),
        help="Git repository to analyze (default: %(default)s)"
    )
    parser.add_argument(
        '--ssh-key-path',
        default=os.getenv('SSH_KEY_PATH', "~/.ssh/id_rsa"),
        help="Path to the SSH private key used for the clone (default: %(default)s)"
    )
    args = parser.parse_args(argv)

    try:
        if args.clusters:
            # Flatten any comma-separated values, e.g. --cluster mlc1,nwc3
            args.clusters = validate_clusters(split_cluster_names(args.clusters))
        else:
            # Fall back to the environment, so `docker run -e CLUSTERS=mlc1` works
            args.clusters = clusters_from_env()
    except ValueError as e:
        parser.error(str(e))

    return args


def main():
    """Example usage"""
    args = parse_args()

    tracker = HelmChartTracker(
        git_repo_url=args.repo_url,
        ssh_key_path=args.ssh_key_path,  # Path to your SSH private key
        clusters=args.clusters
    )
    
    print("Analyzing Helm charts across clusters: %s" % ', '.join(tracker.clusters))
    charts = tracker.analyze_charts()
    
    if charts:
        print("\n" + tracker.generate_report(charts))
        
        # Generate JSON for dashboard
        dashboard_data = tracker.generate_dashboard_data(charts)
        
        # Save to file for dashboard consumption
        import json
        with open('multi_cluster_chart_status.json', 'w') as f:
            json.dump(dashboard_data, f, indent=2)
        
        print(f"\nDashboard data saved to multi_cluster_chart_status.json")
        
        # Print quick summary of charts needing updates
        updates_needed = [c for c in charts if c.needs_update]
        if updates_needed:
            print(f"\n🚨 {len(updates_needed)} charts need updates across all clusters")
    else:
        print("No charts found or failed to analyze repository")

if __name__ == "__main__":
    main()