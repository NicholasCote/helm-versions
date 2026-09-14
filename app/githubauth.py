#!/usr/bin/env python3
"""GitHub OAuth web flow and the team-membership gate.

Deliberately free of Flask: everything here is pure functions over `requests`, so the
token exchange, the team check and the URL parsing can be tested without standing up an
app or a session. `app.py` owns the cookie, the session store and the route wiring.

The app is an OAuth App (not a GitHub App), because the access token it gets back is
used for two different things:

  1. authorizing the person - are they in the allowed team
  2. cloning GIT_REPO_URL as that person, replacing the deploy key

A GitHub App's user-to-server token would be tighter (fine-grained, repo-scoped,
expiring) but only reaches repositories the *installation* covers, which puts an install
step and an org-admin approval between the team and a working dashboard. OAuth App scopes
are coarse: `repo` is read/write on every repo the user can reach. We never write, but
the token could - so it is never written to disk, never placed in argv, and never sent
to the browser. See the session store in app.py and the askpass helper in tracker.py.
"""

import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
from urllib.parse import urlencode

import requests

AUTHORIZE_URL = 'https://github.com/login/oauth/authorize'
ACCESS_TOKEN_URL = 'https://github.com/login/oauth/access_token'
API_ROOT = 'https://api.github.com'

# `repo` is what makes a private clone work; `read:org` is what makes the team lookup
# work. Neither is optional, and GitHub has no narrower read-only equivalent of `repo`
# for OAuth Apps - `public_repo` covers public repos only.
SCOPES = ['repo', 'read:org']

# Every outbound auth call is bounded. A hung GitHub API call during login should fail
# the login, not hold a worker thread open indefinitely.
HTTP_TIMEOUT = 15

DEFAULT_TEAM = 'NCAR/cirrus-admins'

# github.com/owner/repo, git@github.com:owner/repo.git, ssh://git@github.com/owner/repo
REPO_SLUG_PATTERN = re.compile(
    r'(?:github\.com[:/])(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$')


class AuthError(Exception):
    """A login that cannot proceed, with a message safe to show the user"""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class OAuthConfig:
    client_id: str
    client_secret: str
    org: str
    team: str
    scopes: List[str] = field(default_factory=lambda: list(SCOPES))

    @property
    def team_slug(self) -> str:
        return f"{self.org}/{self.team}"


def repo_slug(git_repo_url: str) -> Optional[Tuple[str, str]]:
    """(owner, repo) for a GitHub URL in any of the forms Argo/git accept, else None"""
    if not git_repo_url:
        return None
    match = REPO_SLUG_PATTERN.search(git_repo_url.strip())
    if not match:
        return None
    return match.group('owner'), match.group('repo')


def https_clone_url(git_repo_url: str) -> Optional[str]:
    """The https:// form of a GitHub repo URL, for token auth instead of SSH"""
    slug = repo_slug(git_repo_url)
    if not slug:
        return None
    return f"https://github.com/{slug[0]}/{slug[1]}.git"


def config_from_env(git_repo_url: str = None) -> Optional[OAuthConfig]:
    """OAuth settings from the environment, or None if OAuth isn't configured.

    Returning None rather than raising is what lets the CLI and local SSH runs keep
    working untouched: no client id means no web auth, and app.py refuses to serve
    rather than falling back to an open dashboard.
    """
    client_id = (os.getenv('GITHUB_CLIENT_ID') or '').strip()
    client_secret = (os.getenv('GITHUB_CLIENT_SECRET') or '').strip()

    if not client_id or not client_secret:
        return None

    # Default the org to whoever owns the repo being analyzed - a dashboard for
    # NCAR/cisl-cloud-charts is gated on an NCAR team - so a normal deploy sets two
    # variables, not three. GITHUB_ALLOWED_TEAM overrides as `org/team` or bare `team`.
    configured = (os.getenv('GITHUB_ALLOWED_TEAM') or '').strip() or DEFAULT_TEAM
    if '/' in configured:
        org, _, team = configured.partition('/')
    else:
        slug = repo_slug(git_repo_url or '')
        org, team = (slug[0] if slug else DEFAULT_TEAM.split('/')[0]), configured

    return OAuthConfig(client_id=client_id, client_secret=client_secret,
                       org=org.strip(), team=team.strip())


def new_state() -> str:
    """Unguessable CSRF state for the authorize redirect"""
    return secrets.token_urlsafe(32)


def authorize_url(config: OAuthConfig, redirect_uri: str, state: str) -> str:
    """Where to send the browser to start the flow"""
    query = urlencode({
        'client_id': config.client_id,
        'redirect_uri': redirect_uri,
        'scope': ' '.join(config.scopes),
        'state': state,
        # Don't silently reuse an existing GitHub session's grant when the user
        # explicitly logged out of the dashboard.
        'allow_signup': 'false',
    })
    return f"{AUTHORIZE_URL}?{query}"


def exchange_code(config: OAuthConfig, code: str, redirect_uri: str) -> str:
    """Trade the callback's ?code= for an access token"""
    response = requests.post(
        ACCESS_TOKEN_URL,
        data={
            'client_id': config.client_id,
            'client_secret': config.client_secret,
            'code': code,
            'redirect_uri': redirect_uri,
        },
        headers={'Accept': 'application/json'},
        timeout=HTTP_TIMEOUT,
    )
    response.raise_for_status()
    payload = response.json()

    # GitHub signals failure with a 200 and an `error` key, not a status code
    if payload.get('error'):
        raise AuthError(
            f"GitHub rejected the login: {payload.get('error_description') or payload['error']}",
            status=401)

    token = payload.get('access_token')
    if not token:
        raise AuthError("GitHub returned no access token.", status=401)
    return token


def api_get(token: str, path: str, **kwargs):
    """Authenticated GET against the GitHub API"""
    return requests.get(
        f"{API_ROOT}{path}",
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28',
        },
        timeout=HTTP_TIMEOUT,
        **kwargs,
    )


def fetch_user(token: str) -> dict:
    """The authenticated user's profile"""
    response = api_get(token, '/user')
    if response.status_code != 200:
        raise AuthError("Couldn't read your GitHub profile.", status=502)
    payload = response.json()
    if not payload.get('login'):
        raise AuthError("GitHub returned a profile with no username.", status=502)
    return payload


def assert_team_member(token: str, config: OAuthConfig, login: str) -> None:
    """Raise AuthError unless `login` is an active member of the allowed team.

    A non-member gets a 404 rather than a 403, and so does a team that doesn't exist or
    that the user can't see (secret teams are invisible to outsiders). Those are
    indistinguishable over the API and all mean the same thing here - not a member - so
    they collapse into one denial rather than leaking which case it was.

    `pending` is an invitation that hasn't been accepted. It is not membership yet, and
    is called out separately because "accept the invite" is a fix the user can act on.
    """
    response = api_get(
        token, f"/orgs/{config.org}/teams/{config.team}/memberships/{login}")

    if response.status_code == 200:
        state = (response.json() or {}).get('state')
        if state == 'active':
            return
        if state == 'pending':
            raise AuthError(
                f"Your invitation to {config.team_slug} is still pending - "
                f"accept it on GitHub, then sign in again.")
        raise AuthError(f"Your membership in {config.team_slug} is '{state}', not active.")

    if response.status_code in (403, 404):
        raise AuthError(f"You're not a member of the {config.team_slug} team.")

    if response.status_code == 401:
        raise AuthError("GitHub rejected the access token.", status=401)

    raise AuthError(
        f"Couldn't check {config.team_slug} membership (GitHub returned "
        f"{response.status_code}).", status=502)


class TokenStore:
    """Server-side store for signed-in users' GitHub tokens.

    The browser cookie holds only an opaque session id. The token itself stays here, in
    process memory, because Flask's default session cookie is *signed, not encrypted* -
    its contents are base64 and readable by anyone holding the cookie. A `repo`-scoped
    GitHub token in a readable cookie would be a write-capable credential for every
    repository that user can reach, sitting in their browser and in any proxy log that
    captures cookies.

    In-process means sessions don't survive a restart and don't work across replicas;
    run this Deployment with replicas: 1, or put a shared store behind the same three
    methods. Signing out mid-session is a feature of holding them here: dropping the
    record revokes access immediately, which a stateless cookie can't do.
    """

    def __init__(self, ttl_seconds: int = 8 * 3600):
        self.ttl_seconds = ttl_seconds
        self._sessions = {}
        self._lock = threading.Lock()

    def create(self, token: str, user: dict) -> str:
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._sweep_locked(time.time())
            self._sessions[sid] = {
                'token': token,
                'login': user.get('login'),
                'name': user.get('name') or user.get('login'),
                'avatar_url': user.get('avatar_url'),
                'expires_at': time.time() + self.ttl_seconds,
            }
        return sid

    def get(self, sid: Optional[str]) -> Optional[dict]:
        if not sid:
            return None
        with self._lock:
            record = self._sessions.get(sid)
            if not record:
                return None
            if record['expires_at'] <= time.time():
                del self._sessions[sid]
                return None
            return dict(record)

    def drop(self, sid: Optional[str]) -> None:
        if not sid:
            return
        with self._lock:
            self._sessions.pop(sid, None)

    def _sweep_locked(self, now: float) -> None:
        expired = [k for k, v in self._sessions.items() if v['expires_at'] <= now]
        for k in expired:
            del self._sessions[k]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
