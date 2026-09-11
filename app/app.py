#!/usr/bin/env python3
"""
Flask Web Application for Helm Chart Version Tracker
"""

from flask import Flask, render_template, jsonify, request, send_from_directory
import json
import os
from datetime import datetime
import threading
import time
from tracker import HelmChartTracker, clusters_from_env, CLUSTERS_ENV_VAR  # Import your existing tracker
from staleness import TIER_ICONS, TIER_LABELS, TIER_ORDER, semver_key
import differ

app = Flask(__name__)

# Global variables to store data
chart_data = {}
# {(chart_name, repo_url): [version, ...]} - see tracker.version_index()
chart_versions_index = {}
last_update = None
update_in_progress = False

def update_chart_data():
    """Background function to update chart data"""
    global chart_data, chart_versions_index, last_update, update_in_progress
    
    update_in_progress = True
    # Chart releases are immutable, so cached renders can't go stale within a run -
    # but clearing on refresh matches what users expect "Refresh" to mean, and
    # covers the rare republished-version case.
    differ.clear_render_cache()
    try:
        # Get configuration from environment variables
        git_repo_url = os.getenv('GIT_REPO_URL', 'git@github.com:NCAR/cisl-cloud-charts.git')
        ssh_key_path = os.getenv('SSH_KEY_PATH')  # Don't provide default here
        clusters = clusters_from_env()  # None means "all known clusters"
        
        print(f"Starting chart analysis with repo: {git_repo_url}")
        print(f"Clusters: {', '.join(clusters) if clusters else 'all (%s not set)' % CLUSTERS_ENV_VAR}")
        print(f"SSH key path: {ssh_key_path}")
        print(f"SSH_KEY_CONTENT_BASE64 set: {bool(os.getenv('SSH_KEY_CONTENT_BASE64'))}")
        print(f"SSH_KEY_CONTENT set: {bool(os.getenv('SSH_KEY_CONTENT'))}")
        
        # Only check file path if using SSH_KEY_PATH (not environment content)
        if ssh_key_path and not os.getenv('SSH_KEY_CONTENT') and not os.getenv('SSH_KEY_CONTENT_BASE64'):
            expanded_path = os.path.expanduser(ssh_key_path)
            if not os.path.exists(expanded_path):
                print(f"Warning: SSH key file not found at {expanded_path}")
            else:
                print(f"SSH key file found at {expanded_path}")
        
        tracker = HelmChartTracker(
            git_repo_url=git_repo_url,
            ssh_key_path=ssh_key_path,  # Pass None if not set
            clusters=clusters
        )
        
        print("Analyzing charts...")
        charts = tracker.analyze_charts()
        
        if charts:
            chart_data = tracker.generate_dashboard_data(charts)
            # Kept out of chart_data (too large to ship on every poll) but needed by
            # /api/versions and the diff allowlist, so hold onto it here.
            chart_versions_index = tracker.version_index(charts)
            print(f"✅ Successfully analyzed {len(charts)} charts")
            
            # Print summary
            by_tier = chart_data['summary']['by_tier']
            breakdown = ', '.join(f"{TIER_ICONS[tier]} {by_tier[tier]} {TIER_LABELS[tier].lower()}"
                                  for tier in TIER_ORDER if by_tier[tier])
            print(f"📊 Summary: {breakdown}")
        else:
            print("⚠️ No charts found - this might indicate a problem with repository access or parsing")
            chart_data = {"error": "No charts found. Check repository access and SSH key configuration."}
            chart_versions_index = {}
        
        last_update = datetime.now()
        print(f"🏁 Chart analysis completed at {last_update}")
        print("=" * 60)
        
    except Exception as e:
        print(f"Error updating chart data: {e}")
        import traceback
        traceback.print_exc()
        chart_data = {"error": str(e)}
        chart_versions_index = {}
    finally:
        update_in_progress = False

def background_updater():
    """Background thread to periodically update data - DISABLED"""
    # Background updating disabled - user will manually refresh as needed
    pass

@app.route('/')
def index():
    """Main dashboard page"""
    return render_template('index.html')

@app.route('/static/<path:filename>')
def static_files(filename):
    """Serve static files"""
    return send_from_directory('static', filename)

@app.route('/api/charts')
def api_charts():
    """API endpoint to get chart data"""
    global chart_data, last_update
    
    response = {
        'data': chart_data,
        'last_update': last_update.isoformat() if last_update else None,
        'update_in_progress': update_in_progress
    }
    return jsonify(response)

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """API endpoint to trigger a manual refresh"""
    if not update_in_progress:
        threading.Thread(target=update_chart_data, daemon=True).start()
        return jsonify({"status": "refresh_started"})
    else:
        return jsonify({"status": "update_in_progress"})

# Which HTTP status each helm failure maps to
DIFF_ERROR_STATUS = {
    'INVALID_INPUT': 400,
    'NOT_IN_CATALOG': 403,
    'NO_DATA': 503,
    'CHART_NOT_FOUND': 404,
    'RENDER_FAILED': 422,
    'RATE_LIMITED': 429,
    'REGISTRY_AUTH': 502,
    'NETWORK': 502,
    'HELM_MISSING': 503,
    'TIMEOUT': 504,
    'OUTPUT_TOO_LARGE': 413,
}


def iter_charts(data):
    """Every chart entry in a dashboard payload, across all clusters"""
    for cluster in (data.get('clusters') or {}).values():
        for chart in cluster.get('charts') or []:
            yield chart


def chart_catalog_entry(chart_name, repo_url):
    """The first dashboard entry matching a chart/repo pair, or None

    This is the allowlist: the diff endpoint executes a binary that makes outbound
    requests, and the app has no authentication, so it will only ever render charts
    the dashboard is already tracking. That closes off both SSRF (an arbitrary
    repo_url fetched by helm from inside the cluster) and rendering an
    attacker-chosen chart.
    """
    for chart in iter_charts(chart_data):
        if chart.get('chart_name') == chart_name and chart.get('repo_url') == repo_url:
            return chart
    return None


def known_versions(chart_name, repo_url):
    """Every version we'll allow a diff against for this chart"""
    versions = set(chart_versions_index.get((chart_name, repo_url)) or [])

    # The pinned and latest versions are always fair game even if the version list
    # failed to load, so a diff still works when only the index lookup broke.
    for chart in iter_charts(chart_data):
        if chart.get('chart_name') == chart_name and chart.get('repo_url') == repo_url:
            versions.update(v for v in (chart.get('current_version'),
                                        chart.get('latest_version')) if v)

    return versions


def sorted_versions(versions):
    """Newest first, semver-aware, with unparseable versions last in their own order"""
    parsed = [(semver_key(v), v) for v in versions]
    ranked = sorted((k, v) for k, v in parsed if k is not None)
    unparsed = sorted(v for k, v in parsed if k is None)
    return [v for _, v in reversed(ranked)] + unparsed


@app.route('/api/versions')
def api_versions():
    """Versions available for one chart, for the diff view's version picker

    Served separately rather than inlined into /api/charts: a few hundred charts
    times OCI tag lists in the hundreds would make every dashboard poll multi-
    megabyte, for data only the diff modal ever reads.
    """
    chart_name = request.args.get('chart', '')
    repo_url = request.args.get('repo', '')

    try:
        chart_name = differ.validate_chart_name(chart_name)
        repo_url = differ.validate_repo_url(repo_url)
    except differ.HelmError as e:
        return jsonify({"error": e.as_dict()}), 400

    if not chart_data or 'clusters' not in chart_data:
        return jsonify({"error": {"code": "NO_DATA",
                                  "message": "Chart data hasn't loaded yet. Refresh first."}}), 503

    entry = chart_catalog_entry(chart_name, repo_url)
    if entry is None:
        return jsonify({"error": {"code": "NOT_IN_CATALOG",
                                  "message": "That chart isn't in the current dashboard data."}}), 403

    return jsonify({
        "chart_name": chart_name,
        "repo_url": repo_url,
        "current_version": entry.get('current_version'),
        "latest_version": entry.get('latest_version'),
        "versions": sorted_versions(known_versions(chart_name, repo_url)),
    })


@app.route('/api/diff', methods=['POST'])
def api_diff():
    """Render two versions of a chart and return a unified diff

    Synchronous: two renders of even a very large chart take a few seconds, and
    Werkzeug is threaded, so this doesn't block the dashboard. Wrapping it in a job
    registry would buy nothing but a second polling loop.
    """
    payload = request.get_json(silent=True) or {}

    try:
        chart_name = differ.validate_chart_name(payload.get('chart_name'))
        repo_url = differ.validate_repo_url(payload.get('repo_url'))
        old_version = differ.validate_version(payload.get('old_version'))
        new_version = differ.validate_version(payload.get('new_version'))
    except differ.HelmError as e:
        return jsonify({"error": e.as_dict()}), 400

    # An empty catalog denies everything, so say "not loaded yet" rather than
    # letting it look like bad input.
    if not chart_data or 'clusters' not in chart_data:
        return jsonify({"error": {"code": "NO_DATA",
                                  "message": "Chart data hasn't loaded yet. Click Refresh first."}}), 503

    entry = chart_catalog_entry(chart_name, repo_url)
    if entry is None:
        return jsonify({"error": {"code": "NOT_IN_CATALOG",
                                  "message": "That chart isn't in the current dashboard data. "
                                             "Refresh and try again."}}), 403

    allowed_versions = known_versions(chart_name, repo_url)
    unknown = [v for v in (old_version, new_version) if v not in allowed_versions]
    if unknown:
        return jsonify({"error": {"code": "NOT_IN_CATALOG",
                                  "message": f"Unknown version(s) for {chart_name}: "
                                             f"{', '.join(unknown)}."}}), 403

    try:
        result = differ.diff_versions(
            chart_name, repo_url, old_version, new_version,
            # Both sides use the deployed release name. The CI workflow renders as
            # `old` and `new`, which leaks .Release.Name into every resource name
            # and makes roughly 40% of its diff output noise.
            release_name=entry.get('release_name') or chart_name)
    except differ.HelmError as e:
        return jsonify({"error": e.as_dict()}), DIFF_ERROR_STATUS.get(e.code, 500)

    return jsonify(result)


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "last_update": last_update.isoformat() if last_update else None,
        # The dashboard hides its View Diff buttons when helm is unavailable
        "helm_available": differ.helm_version() is not None,
        "helm_version": differ.helm_version(),
    })

@app.route('/debug')
def debug():
    """Debug endpoint to check configuration"""
    debug_info = {
        "git_repo_url": os.getenv('GIT_REPO_URL', 'Not set'),
        "ssh_key_path": os.getenv('SSH_KEY_PATH', 'Not set'),
        "clusters": os.getenv(CLUSTERS_ENV_VAR, 'Not set (all clusters)'),
        "ssh_key_content_set": bool(os.getenv('SSH_KEY_CONTENT')),
        "update_in_progress": update_in_progress,
        "chart_data_keys": list(chart_data.keys()) if chart_data else None,
        "last_update": last_update.isoformat() if last_update else None,
        "helm_version": differ.helm_version(),
        "helm_binary": differ.HELM_BINARY,
        "charts_with_version_lists": len(chart_versions_index),
        "working_directory": os.getcwd(),
        "ssh_key_file_exists": os.path.exists(os.path.expanduser(os.getenv('SSH_KEY_PATH', '/app/.ssh/id_rsa'))) if os.getenv('SSH_KEY_PATH') else False
    }
    return jsonify(debug_info)

if __name__ == '__main__':
    # Create static directory if it doesn't exist
    os.makedirs('static', exist_ok=True)
    
    # Start initial data load in background (non-blocking)
    print("Starting initial chart analysis in background...")
    initial_load_thread = threading.Thread(target=update_chart_data, daemon=True)
    initial_load_thread.start()
    
    # Start Flask app immediately
    port = int(os.getenv('PORT', 5000))
    print(f"Flask app starting on port {port} - chart analysis running in background")
    app.run(host='0.0.0.0', port=port, debug=False)