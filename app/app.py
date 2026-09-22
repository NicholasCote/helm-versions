#!/usr/bin/env python3
"""
Flask Web Application for Helm Chart Version Tracker
"""

from flask import (Flask, make_response, render_template, jsonify, request, redirect,
                   session, url_for, send_from_directory)
import json
import os
import secrets
import requests
from datetime import datetime
from functools import wraps
import threading
import time
from tracker import HelmChartTracker, clusters_from_env, CLUSTERS_ENV_VAR  # Import your existing tracker
from staleness import TIER_ICONS, TIER_LABELS, TIER_ORDER, semver_key
import githubauth
import differ

app = Flask(__name__)

GIT_REPO_URL = os.getenv('GIT_REPO_URL', 'git@github.com:NCAR/cisl-cloud-charts.git')

# None when GITHUB_CLIENT_ID/SECRET aren't set. That is the local-development case: the
# dashboard then behaves exactly as it did before, unauthenticated, driven by an ssh key.
# It is emphatically not a deployment mode - see require_auth.
OAUTH = githubauth.config_from_env(GIT_REPO_URL)

# Serving with no authentication has to be asked for by name. Without OAuth configured
# the dashboard is fully open - /debug dumps configuration and /api/diff shells out to
# helm - and the failure mode that matters is nobody deciding to do that: a Secret gets
# renamed, the pod starts healthy because /health is public, the readiness probe passes,
# and the rollout succeeds onto an open Ingress. One line of stdout is not a control, so
# the app refuses to serve instead.
ALLOW_UNAUTHENTICATED = (os.getenv('ALLOW_UNAUTHENTICATED') or '').lower() in ('1', 'true', 'yes')

# The callback URL registered on the OAuth App. Required, not derived: building it from
# the request means trusting the Host / X-Forwarded-* headers, and GitHub accepts any
# redirect_uri whose host matches the registered callback's *excluding sub-domains*. A
# dangling sub-domain pointed at this same Ingress would therefore be handed real
# authorization codes - which land in a session bound to a `repo`-scoped token.
OAUTH_REDIRECT_URI = (os.getenv('OAUTH_REDIRECT_URI') or '').strip()

# Which secrets are present, snapshotted at startup for /debug. Booleans only - the
# endpoint reports whether a thing is configured, never what it is.
CONFIG_PRESENT = {
    'github_client_id': bool(os.getenv('GITHUB_CLIENT_ID')),
    'github_client_secret': bool(os.getenv('GITHUB_CLIENT_SECRET')),
    'secret_key': bool(os.getenv('SECRET_KEY')),
    'ssh_key_path': os.getenv('SSH_KEY_PATH') or 'Not set',
    'ssh_key_content': bool(os.getenv('SSH_KEY_CONTENT')),
    'ssh_key_content_base64': bool(os.getenv('SSH_KEY_CONTENT_BASE64')),
}

# Tokens live here, keyed by an opaque id; the cookie carries only that id.
token_store = githubauth.TokenStore(
    ttl_seconds=int(os.getenv('SESSION_TTL_HOURS', '8')) * 3600)

# Signs the session cookie. Generated when unset so a misconfigured deploy still gets a
# *random* key rather than a predictable one - but every restart then invalidates every
# session, so set it from a Secret in Kubernetes.
app.secret_key = os.getenv('SECRET_KEY') or secrets.token_hex(32)
if not os.getenv('SECRET_KEY') and OAUTH:
    print("⚠ SECRET_KEY not set - generated a random one. "
          "Sessions will not survive a restart; set it from a Secret.")

if OAUTH and not OAUTH_REDIRECT_URI:
    raise githubauth.ConfigError(
        "OAUTH_REDIRECT_URI must be set when GitHub OAuth is configured. It has to "
        "match the OAuth App's registered callback URL exactly, e.g. "
        "https://helm-versions.example.org/auth/callback")

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    # Not Werkzeug's default `session`. Every Flask app on a shared parent domain writes
    # a cookie by that name, and a browser holding one scoped to a parent of this host -
    # or left over from an earlier deploy signed with a different SECRET_KEY - sends it
    # in the same header as ours. Werkzeug reads the first `session` it finds, so the
    # wrong one can win and the OAuth state disappears between /login and the callback.
    # A name only this app uses can't be shadowed, and it orphans any stale `session`
    # cookie already in the browser rather than fighting it for the slot.
    SESSION_COOKIE_NAME='helm_versions_session',
    # OAuth redirects back over https in any real deployment. Left overridable because
    # `docker run -p 5000:5000` is plain http and a Secure cookie would never be sent.
    SESSION_COOKIE_SECURE=os.getenv('SESSION_COOKIE_SECURE', 'true').lower() != 'false',
)

# Global variables to store data
chart_data = {}
# {(chart_name, repo_url): [version, ...]} - see tracker.version_index()
chart_versions_index = {}
last_update = None
# The GitHub login whose token produced the data currently on screen, so the dashboard
# can say whose view it is rather than implying it's everyone's.
last_update_by = None
update_in_progress = False

def update_chart_data(github_token=None, triggered_by=None):
    """Background function to update chart data

    `github_token` is the OAuth token of whoever clicked Refresh; the clone runs as
    them. One dashboard is shared by everyone signed in, so it shows whatever that
    person could see - which is the same set of clusters for anyone in the allowed
    team, but it is why the payload records who fetched it.
    """
    global chart_data, chart_versions_index, last_update, update_in_progress, last_update_by
    
    update_in_progress = True
    # Chart releases are immutable, so cached renders can't go stale within a run -
    # but clearing on refresh matches what users expect "Refresh" to mean, and
    # covers the rare republished-version case.
    differ.clear_render_cache()
    try:
        # Get configuration from environment variables
        git_repo_url = GIT_REPO_URL
        ssh_key_path = None if github_token else os.getenv('SSH_KEY_PATH')
        clusters = clusters_from_env()  # None means "all known clusters"
        
        print(f"Starting chart analysis with repo: {git_repo_url}")
        print(f"Clusters: {', '.join(clusters) if clusters else 'all (%s not set)' % CLUSTERS_ENV_VAR}")
        if github_token:
            print(f"Auth: GitHub OAuth token for {triggered_by}")
        else:
            print(f"Auth: SSH key (no OAuth session)")
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
            clusters=clusters,
            github_token=github_token,
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
            reason = ("Check that your GitHub account can read the repository."
                      if github_token else
                      "Check repository access and SSH key configuration.")
            chart_data = {"error": f"No charts found. {reason}"}
            chart_versions_index = {}
        
        last_update = datetime.now()
        last_update_by = triggered_by
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

# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

# Reachable without a session. /health is here so a Kubernetes probe doesn't need a
# credential; it deliberately reports no chart data. The login routes are obviously
# exempt, and static assets are the dashboard's own CSS/JS - the login page needs them.
PUBLIC_ENDPOINTS = {'login', 'callback', 'logout', 'health', 'static', 'static_files'}


def current_session():
    """The signed-in user's session record, or None"""
    return token_store.get(session.get('sid'))


def wants_json():
    """True when a 401 should be JSON rather than a redirect to GitHub

    fetch() from dashboard.js follows redirects transparently, so answering an expired
    API call with a 302 to github.com hands the JS an opaque HTML body instead of an
    error it can act on. The dashboard reads the 401 and shows its own sign-in prompt.
    """
    return (request.path.startswith('/api/')
            or request.headers.get('X-Requested-With') == 'XMLHttpRequest'
            or 'application/json' in (request.headers.get('Accept') or ''))


@app.before_request
def require_auth():
    """Gate every route that isn't explicitly public.

    Default-deny by endpoint name rather than by path prefix: a route added later is
    protected unless someone adds it to PUBLIC_ENDPOINTS on purpose. The old code's
    comment that "the app has no authentication" is what this replaces - the diff
    endpoint in particular shells out to helm, and was only ever safe because nothing
    could reach it.
    """
    # Public first, so that a server which is refusing to serve still answers its
    # probe. A pod that looks healthy and empty is the regression being guarded
    # against here; one that fails its readiness check is the intended outcome.
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    if not OAUTH:
        if ALLOW_UNAUTHENTICATED:
            return None  # deliberately open: local development against an ssh key
        # Configured for neither authentication nor an explicitly open dashboard.
        # Serve nothing rather than guess which was meant.
        return jsonify({"error": {
            "code": "NOT_CONFIGURED",
            "message": "This server has no authentication configured. Set "
                       "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET, or set "
                       "ALLOW_UNAUTHENTICATED=1 to serve the dashboard openly."}}), 503

    if current_session():
        return None

    if wants_json():
        return jsonify({"error": {"code": "UNAUTHENTICATED",
                                  "message": "Sign in with GitHub to continue."}}), 401

    return redirect(url_for('login', next=request.path))


# How many sign-ins one browser may have in flight at once. A person clicking the button
# starts one - but a browser that prefetches or prerenders the sign-in link starts one
# before they have clicked anything, and a second tab or a back-button retry starts
# another. Chrome does that prediction from history, which is exactly why this shows up
# in a normal profile and never in a fresh incognito window. Holding only the newest
# state threw away the one the person actually followed, and GitHub then handed the
# callback a state this session no longer recognized. Each state is still single-use and
# still dies with the session - this widens the window, not the lifetime.
MAX_PENDING_STATES = 4


def state_matches(received: str, pending) -> bool:
    """True when the callback's state is one this session actually issued.

    compare_digest rather than `in` or `!=`, so a mismatch can't be narrowed by timing,
    and over UTF-8 bytes because its str form refuses non-ASCII outright: a callback
    carrying a non-ASCII state is a crafted link, and it should be turned away like any
    other bad state rather than raising TypeError into a 500.
    """
    if not received or not pending:
        return False
    received_bytes = received.encode('utf-8', 'surrogatepass')
    return any(secrets.compare_digest(received_bytes, issued.encode('utf-8', 'surrogatepass'))
               for issued in pending if isinstance(issued, str))


@app.route('/login')
def login():
    """Start the GitHub OAuth flow"""
    if not OAUTH:
        return jsonify({"error": "GitHub OAuth is not configured on this server."}), 501

    if current_session():
        return redirect('/')

    state = githubauth.new_state()
    pending = [s for s in session.get('oauth_states') or [] if isinstance(s, str)]
    session['oauth_states'] = (pending + [state])[-MAX_PENDING_STATES:]
    # Only ever a path on this app, never an absolute URL: an open redirect here would
    # let a crafted link bounce a signed-in user off-site after login.
    target = request.args.get('next') or '/'
    session['next'] = target if target.startswith('/') and not target.startswith('//') else '/'

    # Nothing may cache this redirect. Replaying a stored copy sends someone to GitHub
    # carrying a state from a session that has already spent it, which fails the callback
    # check for a reason nothing they can see in their browser explains.
    response = redirect(githubauth.authorize_url(OAUTH, oauth_redirect_uri(), state))
    response.headers['Cache-Control'] = 'no-store'
    return response


def oauth_redirect_uri():
    """The callback URL to send GitHub

    Always the configured value, never one derived from the request - see the comment
    on OAUTH_REDIRECT_URI. Startup fails when OAuth is on and this is unset, so by the
    time a request reaches here it is non-empty.
    """
    return OAUTH_REDIRECT_URI


@app.route('/auth/callback')
def callback():
    """Finish the OAuth flow: verify state, exchange the code, check the team"""
    if not OAUTH:
        return jsonify({"error": "GitHub OAuth is not configured on this server."}), 501

    pending_states = session.pop('oauth_states', None) or []
    target = session.pop('next', '/')
    had_cookie = app.config['SESSION_COOKIE_NAME'] in request.cookies

    if request.args.get('error'):
        # Logged rather than rendered. This runs before state is validated, so anyone
        # can reach it with a crafted link, and error_description is whatever the query
        # string says - attacker-chosen prose on the genuine sign-in page. Autoescaping
        # stops it being markup; it doesn't stop it reading as our own instructions.
        print(f"✗ OAuth callback returned error={request.args.get('error')!r} "
              f"description={request.args.get('error_description')!r}")
        return render_login_error(
            "GitHub didn't complete the sign-in. Try again.", status=401)

    # A callback whose state this session never issued is a CSRF attempt or a stale
    # bookmark, not a login, and is refused either way.
    state = request.args.get('state', '')
    if not state_matches(state, pending_states):
        # The one cause here that isn't an attack is a session cookie that didn't
        # survive the trip to GitHub, and from the page the user sees the two are
        # indistinguishable - so record which it looked like. No cookie at all means the
        # browser never stored ours or dropped it; a cookie with nothing pending means it
        # carried a *different* session than the one /login wrote to, which is the shape
        # a stale or shadowed cookie leaves behind.
        print(f"✗ OAuth callback state rejected: cookie="
              f"{'present' if had_cookie else 'absent'}, states_pending={len(pending_states)}, "
              f"state_param={'present' if state else 'absent'}")
        return render_login_error(
            "That sign-in link has expired or didn't start here. Try again.", status=400)

    code = request.args.get('code')
    if not code:
        return render_login_error("GitHub didn't send an authorization code.", status=400)

    try:
        token = githubauth.exchange_code(OAUTH, code, oauth_redirect_uri())
        user = githubauth.fetch_user(token)
        githubauth.assert_team_member(token, OAUTH, user['login'])
    except githubauth.AuthError as e:
        return render_login_error(e.message, status=e.status)
    except requests.RequestException as e:
        return render_login_error(f"Couldn't reach GitHub: {e}", status=502)

    # New session id on every login, so a session id captured before sign-in can't be
    # reused after it.
    session.clear()
    session['sid'] = token_store.create(token, user)
    session.permanent = False

    print(f"✓ {user['login']} signed in ({OAUTH.team_slug})")
    return redirect(target)


@app.route('/logout', methods=['GET', 'POST'])
def logout():
    """Drop the server-side token and clear the cookie"""
    record = current_session()
    token_store.drop(session.get('sid'))
    session.clear()
    if record:
        print(f"✓ {record['login']} signed out")
    return redirect(url_for('login'))


def render_login_error(message, status=403):
    """The sign-in page, with a reason the attempt didn't work

    Uncacheable: the reason belongs to one attempt, and a stored copy would show it
    again on the sign-in URL to whoever tried next.
    """
    response = make_response(
        render_template('login.html', error=message,
                        team=OAUTH.team_slug if OAUTH else None),
        status)
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.route('/api/me')
def api_me():
    """Who's signed in, for the dashboard header"""
    record = current_session()
    if not record:
        return jsonify({"authenticated": False, "oauth_enabled": bool(OAUTH)})
    return jsonify({
        "authenticated": True,
        "oauth_enabled": True,
        "login": record['login'],
        "name": record['name'],
        "avatar_url": record['avatar_url'],
        "team": OAUTH.team_slug,
    })


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
        'last_update_by': last_update_by,
        'update_in_progress': update_in_progress
    }
    return jsonify(response)

@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """API endpoint to trigger a manual refresh

    The clone runs with the caller's own GitHub token. The token is read out of the
    store here, on the request thread, and handed to the worker as an argument rather
    than left for it to look up: the worker outlives the request, and by the time it
    clones, the user may have signed out and dropped the session.
    """
    record = current_session()

    if update_in_progress:
        return jsonify({"status": "update_in_progress"})

    if OAUTH and not record:
        return jsonify({"error": {"code": "UNAUTHENTICATED",
                                  "message": "Sign in with GitHub to refresh."}}), 401

    threading.Thread(
        target=update_chart_data,
        kwargs={'github_token': record['token'] if record else None,
                'triggered_by': record['login'] if record else None},
        daemon=True).start()
    return jsonify({"status": "refresh_started"})

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
    requests, so it will only ever render charts the dashboard is already tracking.
    That closes off both SSRF (an arbitrary repo_url fetched by helm from inside the
    cluster) and rendering an attacker-chosen chart.

    Still enforced now that the app authenticates. Sign-in narrows who can reach the
    endpoint to the allowed team; it does not make an arbitrary repo_url safe to hand
    to a subprocess, and defence that only holds while every caller is trusted is not
    defence.
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
    """Health check endpoint

    Reachable without a session, so a probe needs no credential, and it carries no
    chart data.

    Returns 503 when the app is configured for neither authentication nor an
    explicitly open dashboard. In that state every other route refuses too, and a
    server refusing every route is not ready - reporting healthy would let a rollout
    complete onto pods that serve nothing, which is the failure this is here to make
    visible. Use it for readiness; a restart won't fix a missing Secret, so liveness
    should be a plain TCP check.
    """
    configured = bool(OAUTH) or ALLOW_UNAUTHENTICATED
    payload = {
        "status": "healthy" if configured else "not_configured",
        "authenticated": bool(OAUTH),
        "last_update": last_update.isoformat() if last_update else None,
        # The dashboard hides its View Diff buttons when helm is unavailable
        "helm_available": differ.helm_version() is not None,
        "helm_version": differ.helm_version(),
    }
    if not configured:
        payload["message"] = ("Set GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET, or set "
                              "ALLOW_UNAUTHENTICATED=1 to serve the dashboard openly.")
    return jsonify(payload), (200 if configured else 503)

@app.route('/debug')
def debug():
    """Debug endpoint to check configuration

    Behind require_auth, so only the allowed team can read it - but it is still a
    configuration dump, so it reports whether each secret is *set*, never its value.
    """
    debug_info = {
        "git_repo_url": GIT_REPO_URL,
        "clusters": os.getenv(CLUSTERS_ENV_VAR, 'Not set (all clusters)'),
        "oauth_enabled": bool(OAUTH),
        "oauth_team": OAUTH.team_slug if OAUTH else 'Not set',
        "oauth_client_id_set": CONFIG_PRESENT['github_client_id'],
        "oauth_client_secret_set": CONFIG_PRESENT['github_client_secret'],
        "oauth_redirect_uri": OAUTH_REDIRECT_URI or 'derived from request',
        "secret_key_set": CONFIG_PRESENT['secret_key'],
        "active_sessions": len(token_store),
        "signed_in_as": (current_session() or {}).get('login'),
        "ssh_key_path": CONFIG_PRESENT['ssh_key_path'],
        "ssh_key_content_set": CONFIG_PRESENT['ssh_key_content'],
        "ssh_key_content_base64_set": CONFIG_PRESENT['ssh_key_content_base64'],
        "update_in_progress": update_in_progress,
        "chart_data_keys": list(chart_data.keys()) if chart_data else None,
        "last_update": last_update.isoformat() if last_update else None,
        "last_update_by": last_update_by,
        "helm_version": differ.helm_version(),
        "helm_binary": differ.HELM_BINARY,
        "charts_with_version_lists": len(chart_versions_index),
        "working_directory": os.getcwd(),
    }
    return jsonify(debug_info)

if __name__ == '__main__':
    # Create static directory if it doesn't exist
    os.makedirs('static', exist_ok=True)
    
    # With OAuth on there is no credential to clone with until somebody signs in and
    # clicks Refresh, so the startup load is skipped rather than failing noisily on
    # every boot. Without OAuth the ssh key is present at startup and this behaves as
    # it always did.
    if OAUTH:
        print(f"GitHub OAuth enabled - access limited to the {OAUTH.team_slug} team.")
        print("Waiting for a signed-in user to trigger the first analysis.")
    elif ALLOW_UNAUTHENTICATED:
        print("⚠ ALLOW_UNAUTHENTICATED is set - the dashboard is serving with NO "
              "authentication. /api/diff runs helm on request; don't expose this.")
        print("Starting initial chart analysis in background...")
        threading.Thread(target=update_chart_data, daemon=True).start()
    else:
        print("✗ No authentication configured. Set GITHUB_CLIENT_ID and "
              "GITHUB_CLIENT_SECRET, or set ALLOW_UNAUTHENTICATED=1 to serve openly.")
        print("  Serving 503 on every route except /health until one of those is set.")
    
    # Start Flask app immediately
    port = int(os.getenv('PORT', 5000))
    print(f"Flask app starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False)