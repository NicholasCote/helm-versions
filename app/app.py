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

app = Flask(__name__)

# Global variables to store data
chart_data = {}
last_update = None
update_in_progress = False

def update_chart_data():
    """Background function to update chart data"""
    global chart_data, last_update, update_in_progress
    
    update_in_progress = True
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
            print(f"✅ Successfully analyzed {len(charts)} charts")
            
            # Print summary
            needs_update = len([c for c in charts if c.needs_update])
            up_to_date = len([c for c in charts if not c.needs_update and c.latest_version])
            no_info = len([c for c in charts if not c.latest_version])
            
            print(f"📊 Summary: {needs_update} need updates, {up_to_date} up-to-date, {no_info} no version info")
        else:
            print("⚠️ No charts found - this might indicate a problem with repository access or parsing")
            chart_data = {"error": "No charts found. Check repository access and SSH key configuration."}
        
        last_update = datetime.now()
        print(f"🏁 Chart analysis completed at {last_update}")
        print("=" * 60)
        
    except Exception as e:
        print(f"Error updating chart data: {e}")
        import traceback
        traceback.print_exc()
        chart_data = {"error": str(e)}
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

@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "last_update": last_update.isoformat() if last_update else None})

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