#!/usr/bin/env python3
"""
Helm Chart Version Tracker for Multi-Cluster Setup
Analyzes private git repository to find current chart versions across clusters
and compares with latest available versions
"""

import os
import yaml
import requests
import subprocess
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass
from pathlib import Path
import tempfile
import shutil
import re
import concurrent.futures
import threading

@dataclass
class ChartInfo:
    name: str
    cluster: str
    current_version: str
    latest_version: str = None
    repo_url: str = None
    chart_name: str = None
    needs_update: bool = False
    
    def __post_init__(self):
        if self.latest_version and self.current_version != self.latest_version:
            self.needs_update = True

class HelmChartTracker:
    def __init__(self, git_repo_url: str, ssh_key_path: str = None):
        self.git_repo_url = git_repo_url
        self.ssh_key_path = ssh_key_path
        self.clusters = ['mgmt', 'nwc1', 'mlc1']
        self.chart_mappings = {}  # Maps app names to chart info
        self.version_cache = {}  # Cache for chart versions: {(chart_name, repo_url): version}
        self.cache_lock = threading.Lock()  # Thread safety for cache
        
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
    
    def parse_cluster_infraapps(self, repo_path: str, cluster: str) -> Dict[str, Dict]:
        """Parse the infraapps.yaml file for a specific cluster"""
        infraapps_path = os.path.join(repo_path, 'clusters', cluster, 'infraapps.yaml')
        
        if not os.path.exists(infraapps_path):
            print(f"Warning: infraapps.yaml not found for cluster {cluster} at {infraapps_path}")
            return {}
        
        with open(infraapps_path, 'r') as f:
            infraapps_data = yaml.safe_load(f)
        
        apps = infraapps_data.get('apps', {})
        chart_info = {}
        
        for app_name, app_config in apps.items():
            if app_config.get('enable', False) and 'chartVersion' in app_config:
                chart_info[app_name] = {
                    'chartVersion': app_config['chartVersion'],
                    'policiesChartVersion': app_config.get('policiesChartVersion')
                }
        
        return chart_info
    
    def parse_argo_application_template(self, template_path: str) -> List[Dict[str, str]]:
        """Parse a single Argo Application template to extract all chart and repo info"""
        if not os.path.exists(template_path):
            return []
        
        try:
            with open(template_path, 'r') as f:
                content = f.read()
            
            charts = []
            
            # Split content by sources to handle multiple chart definitions
            # Look for patterns that indicate separate chart sources
            sources_pattern = r'- repoURL: (https://[^\s\n]+).*?chart: ([^\s\n]+).*?(?:releaseName: ([^\s\n]+))?'
            
            # Find all chart definitions in the template
            matches = re.finditer(sources_pattern, content, re.DOTALL | re.MULTILINE)
            
            for match in matches:
                repo_url = match.group(1).strip().strip('"').strip("'")
                chart_name = match.group(2).strip().strip('"').strip("'")
                release_name = match.group(3).strip().strip('"').strip("'") if match.group(3) else chart_name
                
                # Skip git repositories (your cloud-charts repo)
                if 'github.com' in repo_url and '.git' in repo_url:
                    print(f"    Skipping git repo: {repo_url}")
                    continue
                
                # This is a Helm repository
                charts.append({
                    'chart_name': chart_name,
                    'repo_url': repo_url,
                    'release_name': release_name
                })
                print(f"    Found chart: {chart_name} (release: {release_name}) @ {repo_url}")
            
            return charts
                
        except Exception as e:
            print(f"Error parsing {template_path}: {e}")
        
        return []
    
    def extract_app_name_from_template(self, template_path: str) -> Optional[str]:
        """Extract the app name from the first line of the Argo Application template"""
        try:
            with open(template_path, 'r') as f:
                first_line = f.readline().strip()
            
            # Look for pattern: {{- if .Values.apps.APPNAME.enable }}
            pattern = r'\{\{-\s*if\s+\.Values\.apps\.([^.\s]+)\.enable\s*\}\}'
            match = re.search(pattern, first_line)
            
            if match:
                app_name = match.group(1)
                print(f"    Extracted app name from template: {app_name}")
                return app_name
            else:
                print(f"    Could not extract app name from first line: {first_line}")
                return None
                
        except Exception as e:
            print(f"    Error reading first line of {template_path}: {e}")
            return None

    def build_chart_mappings(self, repo_path: str) -> Dict[str, List[Dict]]:
        """Build mappings between app names and their chart information"""
        templates_dir = os.path.join(repo_path, 'infra-chart', 'templates')
        mappings = {}
        
        if not os.path.exists(templates_dir):
            print(f"Error: Argo Application templates directory not found at {templates_dir}")
            return mappings
        
        print(f"Parsing Argo Application templates from: {templates_dir}")
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
            
            if chart_info_list and app_name:
                print(f"  Found {len(chart_info_list)} charts for app: {app_name}")
                mappings[app_name] = chart_info_list
                print(f"  ✓ Added mapping: {app_name}")
            elif chart_info_list and not app_name:
                # Fallback to filename-based approach if we can't extract from first line
                base_name = template_file.stem
                app_name_fallback = base_name.replace('-', '').replace('_', '')
                print(f"  Chart info found but no app name extracted, using fallback: {app_name_fallback}")
                mappings[app_name_fallback] = chart_info_list
                print(f"  ✓ Added fallback mapping: {app_name_fallback}")
            else:
                print(f"  ✗ Could not extract chart info or app name from {template_file.name}")
        
        print(f"\nFinal mappings built: {len(mappings)} entries")
        for app, chart_list in mappings.items():
            print(f"  {app}: {len(chart_list)} charts")
            for chart in chart_list:
                print(f"    - {chart.get('chart_name', 'Unknown')} @ {chart.get('repo_url', 'Unknown')}")
        
        return mappings
    
    def get_latest_chart_version(self, chart_name: str, repo_url: str) -> Optional[str]:
        """Get the latest version of a chart from its repository (with caching)"""
        
        # Check cache first (thread-safe)
        cache_key = (chart_name, repo_url)
        with self.cache_lock:
            if cache_key in self.version_cache:
                cached_version = self.version_cache[cache_key]
                print(f"    ✓ Using cached version: {cached_version}")
                return cached_version
        
        try:
            # For Helm repositories, fetch the index.yaml
            if repo_url.endswith('/'):
                index_url = f"{repo_url}index.yaml"
            else:
                index_url = f"{repo_url}/index.yaml"
            
            print(f"    Fetching index from: {index_url}")
            response = requests.get(index_url, timeout=30)  # Increased timeout
            response.raise_for_status()
            
            index_data = yaml.safe_load(response.text)
            entries = index_data.get('entries', {})
            
            if chart_name in entries:
                # Get the latest version (first in the list, sorted by version desc)
                latest_entry = entries[chart_name][0]
                version = latest_entry.get('version')
                print(f"    ✓ Latest version: {version}")
                
                # Cache the result (thread-safe)
                with self.cache_lock:
                    self.version_cache[cache_key] = version
                return version
            else:
                available_charts = list(entries.keys())
                print(f"    ✗ Chart '{chart_name}' not found in repository.")
                print(f"    Available charts: {available_charts[:10]}..." if len(available_charts) > 10 else f"    Available charts: {available_charts}")
                
                # Cache the negative result (thread-safe)
                with self.cache_lock:
                    self.version_cache[cache_key] = None
                return None
            
        except requests.exceptions.Timeout:
            print(f"    ✗ Timeout fetching from {repo_url}")
        except requests.exceptions.RequestException as e:
            print(f"    ✗ Request failed for {repo_url}: {e}")
        except yaml.YAMLError as e:
            print(f"    ✗ Failed to parse YAML from {repo_url}: {e}")
        except Exception as e:
            print(f"    ✗ Unexpected error for {chart_name} from {repo_url}: {e}")
        
        # Cache the error result (thread-safe)
        with self.cache_lock:
            self.version_cache[cache_key] = None
        return None

    def fetch_versions_parallel(self, charts_to_check: List[Tuple]) -> Dict:
        """Fetch multiple chart versions in parallel"""
        print(f"  Starting parallel version checks for {len(charts_to_check)} charts...")
        
        version_results = {}
        
        def fetch_single_version(chart_tuple):
            chart_name, repo_url, display_name = chart_tuple
            try:
                version = self.get_latest_chart_version(chart_name, repo_url)
                return (display_name, version)
            except Exception as e:
                print(f"    ✗ Error checking {display_name}: {e}")
                return (display_name, None)
        
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
                    display_name, version = future.result()
                    version_results[display_name] = version
                except Exception as e:
                    chart_tuple = future_to_chart[future]
                    print(f"    ✗ Failed to get version for {chart_tuple[2]}: {e}")
                    version_results[chart_tuple[2]] = None
        
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
                
                # Parse current versions from cluster's infraapps file
                cluster_apps = self.parse_cluster_infraapps(temp_dir, cluster)
                
                # Collect all charts that need version checking
                charts_to_check = []
                cluster_chart_info = []
                
                for app_name, version_info in cluster_apps.items():
                    current_version = version_info['chartVersion']
                    
                    # Get chart information from mappings
                    chart_mapping_list = chart_mappings.get(app_name, [])
                    
                    if not chart_mapping_list:
                        # No mapping found - create entry without repo info
                        chart_info = ChartInfo(
                            name=app_name,
                            cluster=cluster,
                            current_version=current_version,
                            repo_url=None,
                            chart_name=None
                        )
                        cluster_chart_info.append(chart_info)
                        continue
                    
                    # Process each chart defined in this application
                    for chart_mapping in chart_mapping_list:
                        repo_url = chart_mapping.get('repo_url')
                        chart_name = chart_mapping.get('chart_name')
                        release_name = chart_mapping.get('release_name', chart_name)
                        
                        # Use release name as the display name
                        display_name = release_name
                        
                        # Determine which version to use based on the chart
                        if 'policies' in chart_name.lower() and 'policiesChartVersion' in version_info:
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
                            chart_name=chart_name
                        )
                        
                        # Add to list for parallel checking if we have repo info
                        if repo_url and chart_name:
                            charts_to_check.append((chart_name, repo_url, display_name))
                        
                        cluster_chart_info.append(chart_info)
                
                # Fetch all versions in parallel
                if charts_to_check:
                    version_results = self.fetch_versions_parallel(charts_to_check)
                    
                    # Update chart info with results
                    for chart_info in cluster_chart_info:
                        if chart_info.name in version_results:
                            latest_version = version_results[chart_info.name]
                            chart_info.latest_version = latest_version
                            chart_info.needs_update = (latest_version and 
                                                     chart_info.current_version != latest_version)
                
                # Add all charts from this cluster
                charts.extend(cluster_chart_info)
                print(f"✓ Completed cluster {cluster}: {len(cluster_chart_info)} charts processed")
            
            print(f"\n🎉 Analysis complete! Processed {len(charts)} charts across {len(self.clusters)} clusters")
            
            return charts
    
    def generate_report(self, charts: List[ChartInfo]) -> str:
        """Generate a human-readable report organized by cluster"""
        report = []
        report.append("Multi-Cluster Helm Chart Version Report")
        report.append("=" * 60)
        report.append("")
        
        # Overall summary
        total_charts = len(charts)
        updates_needed = len([c for c in charts if c.needs_update])
        up_to_date = len([c for c in charts if not c.needs_update and c.latest_version])
        no_info = len([c for c in charts if not c.latest_version])
        
        report.append(f"📊 Overall Summary:")
        report.append(f"  Total charts: {total_charts}")
        report.append(f"  Need updates: {updates_needed}")
        report.append(f"  Up-to-date: {up_to_date}")
        report.append(f"  No version info: {no_info}")
        report.append("")
        
        # Group by cluster
        for cluster in self.clusters:
            cluster_charts = [c for c in charts if c.cluster == cluster]
            if not cluster_charts:
                continue
                
            report.append(f"🏢 Cluster: {cluster.upper()}")
            report.append("-" * 30)
            
            # Charts needing updates in this cluster
            cluster_updates = [c for c in cluster_charts if c.needs_update]
            if cluster_updates:
                report.append(f"  📈 Updates needed ({len(cluster_updates)}):")
                for chart in cluster_updates:
                    report.append(f"    {chart.name}: {chart.current_version} → {chart.latest_version}")
                report.append("")
            
            # Up to date charts in this cluster
            cluster_ok = [c for c in cluster_charts if not c.needs_update and c.latest_version]
            if cluster_ok:
                report.append(f"  ✅ Up-to-date ({len(cluster_ok)}):")
                for chart in cluster_ok:
                    report.append(f"    {chart.name}: {chart.current_version}")
                report.append("")
            
            # Charts without version info
            cluster_no_info = [c for c in cluster_charts if not c.latest_version]
            if cluster_no_info:
                report.append(f"  ❓ No version info ({len(cluster_no_info)}):")
                for chart in cluster_no_info:
                    reason = "No mapping found" if not chart.repo_url else "Failed to fetch"
                    report.append(f"    {chart.name}: {chart.current_version} ({reason})")
                report.append("")
        
        return "\n".join(report)
    
    def generate_dashboard_data(self, charts: List[ChartInfo]) -> Dict:
        """Generate structured data for dashboard consumption"""
        clusters_data = {}
        
        for cluster in self.clusters:
            cluster_charts = [c for c in charts if c.cluster == cluster]
            clusters_data[cluster] = {
                'summary': {
                    'total_charts': len(cluster_charts),
                    'needs_update': len([c for c in cluster_charts if c.needs_update]),
                    'up_to_date': len([c for c in cluster_charts if not c.needs_update and c.latest_version]),
                    'no_version_info': len([c for c in cluster_charts if not c.latest_version])
                },
                'charts': [
                    {
                        'name': chart.name,
                        'current_version': chart.current_version,
                        'latest_version': chart.latest_version,
                        'needs_update': chart.needs_update,
                        'repo_url': chart.repo_url,
                        'chart_name': chart.chart_name
                    }
                    for chart in cluster_charts
                ]
            }
        
        # Overall summary
        return {
            'summary': {
                'total_charts': len(charts),
                'needs_update': len([c for c in charts if c.needs_update]),
                'up_to_date': len([c for c in charts if not c.needs_update and c.latest_version]),
                'no_version_info': len([c for c in charts if not c.latest_version])
            },
            'clusters': clusters_data
        }

def main():
    """Example usage"""
    # Update these values for your setup
    tracker = HelmChartTracker(
        git_repo_url="git@github.com:NCAR/cisl-cloud-charts.git",
        ssh_key_path="~/.ssh/id_rsa"  # Path to your SSH private key
    )
    
    print("Analyzing Helm charts across clusters...")
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