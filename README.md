# helm-versions

A small Flask dashboard that tracks Helm chart versions across clusters. It clones a
private git repo of cluster config, reads the `chartVersion` pinned for each enabled app,
looks up the latest version in each chart's upstream Helm repo, and shows what's behind —
graded by how far behind, with a one-click diff of what an upgrade actually changes.

## How it works

1. Clones `GIT_REPO_URL` into a temp dir — over https as the signed-in GitHub user when
   [OAuth](#authentication) is configured, or over SSH with a key when it isn't.
2. Parses `infra-chart/templates/*.yaml` and `bootstrap-chart/templates/*.yaml` (Argo
   `Application` templates) to map each app name to its `chart` + `repoURL`. App names come
   from the `{{- if .Values.apps.NAME.enable }}` guard on the template's first line. Missing
   template directories are skipped with a warning; see `HelmChartTracker.TEMPLATE_DIRS`.

   Templates are parsed as YAML after Go template directives are stripped, so both
   `spec.source` (single) and `spec.sources` (list) work, and `helm.releaseName` is honored.
   Sources without a `chart` key (git/`path` sources, `ref: values`) are ignored. If a
   template won't parse as YAML, it falls back to a regex scan.
3. Parses `clusters/<cluster>/infraapps.yaml` and `clusters/<cluster>/bootstrapapps.yaml`
   for each cluster to get the currently pinned `chartVersion` (and `policiesChartVersion`
   for charts with "policies" in the name). Each chart is tagged with the file it came from
   (`infraapps` / `bootstrapapps`), shown as a badge on the dashboard and in the text report.
   See `HelmChartTracker.APP_SOURCES`.

   An app with a `class:` list (`traefik2`, `ingressnginx2`) is reported once **per class**,
   since each class deploys as its own release and can sit on its own version — they show up
   as `traefik (internal)` / `traefik (external)`. A class without its own `chartVersion`
   inherits the app's top-level one. An app that pins versions *only* under `class:`, with no
   top-level `chartVersion`, is still picked up. When a top-level `chartVersion` disagrees
   with every class version, the classes win and the mismatch is logged as a warning.
4. Looks up **every** published version for each chart (10 in parallel, results cached per
   run). The full list rather than just the latest, because staleness is measured in
   released versions and the diff view offers it as a version picker:
   - `https://` repos: fetches `index.yaml`.
   - OCI registries: lists the repository's tags via the OCI Distribution API
     (`/v2/<repo>/tags/list`), handling anonymous bearer auth (ghcr.io) and pagination.
     Both `oci://quay.io/...` and scheme-less `ghcr.io/org/charts` repoURLs are recognized;
     `git@`/`ssh://`/`.git` sources are not. The chart name is appended to the repoURL
     unless the repoURL already ends with it, covering both ways Argo templates write OCI
     sources.

   In both cases the highest **stable** semver wins. Prereleases (`1.21.0-pre.0`) are
   ignored unless a chart has nothing but prereleases, matching `helm search repo` without
   `--devel`.
5. Classifies each chart into a staleness tier (see below) and serves the result at `/`.

## Staleness tiers

Charts are graded by semver distance rather than shown as a flat "up to date / not up to
date", so it's obvious at a glance which upgrades are routine and which need thought.

| Tier | Colour | Meaning |
| --- | --- | --- |
| **Major** | red | A major version behind, or more than one minor behind |
| **Stale** | orange | Exactly one minor behind, or several patch releases behind |
| **Patch** | yellow | A patch release or two behind |
| **Up to date** | green | On the latest published stable version |
| **Ahead** | teal | Pinned *past* the latest published stable (a prerelease, or a yanked release) |
| **Unknown** | grey | No latest version resolved, or versions that aren't semver-comparable |

Patch distance is counted in **actually published releases**, not by subtracting version
numbers: charts skip patch numbers constantly, so `1.2.0 → 1.2.10` can be just two
releases. When the version list can't be fetched it falls back to the numeric delta, and
says "patch versions" rather than "patch releases" in the label to mark the difference.

Comparison is semver-aware, so a chart pinned at `v1.2.3` against an index publishing
`1.2.3` reads as up to date. (It did not before — the old code compared raw strings.)

Thresholds live in `STALENESS_THRESHOLDS` in `app/staleness.py` and can be overridden with
the `STALENESS_MAX_PATCH_RELEASES` and `STALENESS_MAX_MINOR_BEHIND` environment variables.

## Diff view

Charts that are behind get a **View Diff** button. It renders both versions with
`helm template` inside the container and shows a unified diff, so you can see exactly what
an upgrade does to the rendered manifests before taking it.

This reproduces the team's `Helm Diff` GitHub Actions workflow — that workflow installs the
helm-diff plugin but never uses it, so its real output is two default-values `helm template`
renders compared with `diff -u`. None of that needs cluster access, so it runs here in
seconds instead of minutes in CI. Helm is pinned to **3.22.0** so a rebuild can't quietly
change every diff — but the pin is not as load-bearing as it once read. The default
`.Capabilities.KubeVersion` has been frozen at `v1.20.0` in every helm 3.x from 3.14 to
3.22, and 3.14.4 and 3.22.0 render `ingress-nginx`, `cert-manager`, `argo-cd` and
`external-dns` byte-for-byte identically. What *does* move between minors is
`.Capabilities.APIVersions`, which grows with helm's vendored Kubernetes libraries, so
re-run that comparison before the next bump if any chart gates on
`.Capabilities.APIVersions.Has`.

Two things to know when reading a diff:

- It renders from **chart defaults with no cluster values**, the same basis as CI. A chart
  that needs configuration it doesn't default will fail to render, with the reason shown.
- Charts that generate secrets at render time (`randAlphaNum`, `genCA`, `uuidv4`) produce
  spurious Secret and certificate changes.

Both renders use the **same release name**. The CI workflow renders as `old` and `new`;
since most charts interpolate `.Release.Name` into their fullname template, that leaks into
every resource name and label — measured at ~40% of the workflow's diff output, and 481
spurious lines when diffing `ingress-nginx` 4.11.0 against itself. Worth fixing there too.

The endpoint only renders charts that appear in the current dashboard data, and validates
every field before it reaches `subprocess`. Both checks still apply now that the app
authenticates: signing in narrows *who* can reach the endpoint, it doesn't make an
arbitrary `repo_url` safe to hand to a subprocess.

The known cluster list is `HelmChartTracker.DEFAULT_CLUSTERS` in `app/tracker.py`
(`mgmt`, `nwc1`, `mlc1`, `nwc3`, `mlc3`). All of them are analyzed unless you narrow the
selection with the `CLUSTERS` env var or the CLI's `--cluster` flag. Adding a genuinely new
cluster still means editing `DEFAULT_CLUSTERS`.

## Authentication

The dashboard signs users in with GitHub and limits access to one GitHub team. The same
sign-in does double duty: the OAuth token it produces is also what clones `GIT_REPO_URL`,
so there is no deploy key or SSH secret in the cluster at all.

Set `GITHUB_CLIENT_ID` and `GITHUB_CLIENT_SECRET` to turn it on. **With them unset the
app serves the dashboard with no authentication whatsoever** — that is the local
development mode, and it prints a warning at startup. Don't expose that beyond localhost;
`/api/diff` runs `helm` on request.

### How access is decided

1. The user is sent to GitHub and grants the `repo` and `read:org` scopes.
2. The callback exchanges the code for an access token and reads the user's profile.
3. `GET /orgs/<org>/teams/<team>/memberships/<user>` must come back `active`. A `pending`
   invitation is refused with a message saying to accept it; a non-member, a team that
   doesn't exist, and a secret team the user can't see are indistinguishable over the API
   and all collapse into one "you're not a member" denial.
4. The token is stored **server-side** and the browser gets an opaque session id.

Step 4 matters: Flask's session cookie is *signed, not encrypted*, so anything put in it
is readable by whoever holds the cookie. A `repo`-scoped token is write-capable against
every repository that user can reach, so it never goes near the browser. It lives in
process memory, which means **sessions don't survive a restart and don't work across
replicas — run this with `replicas: 1`**, or swap `TokenStore` for a shared backend.

### Why an OAuth App and not a GitHub App

A GitHub App's user-to-server tokens would be tighter — fine-grained, repo-scoped and
expiring — but they only reach repositories the *installation* covers, which puts an
install step and an org-admin approval between the team and a working dashboard. The
tradeoff is that OAuth App scopes are coarse: `repo` is read **and write** on every
repository the user can reach. The app never writes, and the token is never written to
disk, never placed in `argv` and never sent to the browser, but it's worth knowing what
you're granting.

### Setting up the OAuth App

On GitHub, **Settings → Developer settings → OAuth Apps → New OAuth App**:

| Field | Value |
| --- | --- |
| Homepage URL | `https://helm-versions.example.org` |
| Authorization callback URL | `https://helm-versions.example.org/auth/callback` |

The callback must match `OAUTH_REDIRECT_URI` exactly. Set that variable explicitly in
Kubernetes rather than letting the app derive it from the request — deriving it means
trusting the `Host` / `X-Forwarded-*` headers, which an attacker can set to point the
authorization code at a host they control.

### How the token reaches git

Not through the URL and not through `argv`. `argv` is readable through `/proc` by
anything else in the pod, and a credential in the clone URL gets written into
`.git/config` and echoed back in git's own error messages. Instead the token goes in an
environment variable and git reads it through a `GIT_ASKPASS` helper, which is deleted
with its temp dir when the clone finishes.

One non-obvious detail, since it looks like a simplification waiting to happen: the clone
URL carries **no username**. Given `https://x-access-token@github.com/...`, git decides it
has a complete credential, never calls `GIT_ASKPASS`, and sends an *empty* password — so
every clone 401s. With the username omitted, git asks the helper for both halves and
authenticates correctly. Measured against git 2.43.

### Whose data is on the dashboard

The clone runs with the token of whoever clicked **Refresh**, and there is one shared
dashboard — so the data everyone sees is whatever that person could see. For a team that
all has access to the same config repo this is a distinction without a difference, but
it's why `/api/charts` reports `last_update_by` and the header shows who you're signed in
as. Nothing is analyzed until someone signs in and refreshes; with OAuth on there is no
credential at startup to clone with.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GIT_REPO_URL` | no | `git@github.com:NCAR/cisl-cloud-charts.git` | Repo to clone and analyze. Accepts `git@`, `https://` or `ssh://` form; it's normalized to https when cloning with a token. |
| `GITHUB_CLIENT_ID` | for OAuth | — | OAuth App client id. Setting this **and** the secret enables authentication. |
| `GITHUB_CLIENT_SECRET` | for OAuth | — | OAuth App client secret. |
| `GITHUB_ALLOWED_TEAM` | no | `NCAR/cirrus-admins` | Team whose members may sign in, as `org/team`. A bare `team` takes the org from `GIT_REPO_URL`. |
| `OAUTH_REDIRECT_URI` | recommended | derived from request | Must equal the OAuth App's callback URL. Set it explicitly in Kubernetes. |
| `SECRET_KEY` | recommended | random per start | Signs the session cookie. Unset means every restart signs everyone out. |
| `SESSION_TTL_HOURS` | no | `8` | How long a sign-in lasts before it has to be repeated. |
| `SESSION_COOKIE_SECURE` | no | `true` | Set to `false` only to run over plain http locally; the cookie is otherwise never sent. |
| `GITHUB_TOKEN` | no | — | **CLI only.** Personal access token to clone with, instead of an SSH key. The web app uses the signed-in user's token and ignores this. |
| `SSH_KEY_CONTENT_BASE64` | one of the three | — | Base64-encoded SSH private key. Local/CLI use; unnecessary when OAuth is configured. |
| `SSH_KEY_CONTENT` | one of the three | — | Raw PEM SSH private key, including header/footer lines. |
| `SSH_KEY_PATH` | one of the three | — | Path to an SSH private key file already present in the container. |
| `CLUSTERS` | no | all clusters | Comma-separated subset of clusters to analyze, e.g. `mlc1` or `mlc1,nwc3`. Unknown names abort the run. |
| `PORT` | no | `5000` | Port Flask listens on. Already set in the Dockerfile. |
| `STALENESS_MAX_PATCH_RELEASES` | no | `2` | At most this many patch releases behind stays yellow; beyond it, orange. |
| `STALENESS_MAX_MINOR_BEHIND` | no | `1` | More than this many minor versions behind escalates orange to red. |
| `HELM_BINARY` | no | `helm` | Helm executable used for diffs. |
| `HELM_RENDER_TIMEOUT` | no | `90` | Seconds before a single `helm template` is abandoned. |
| `HELM_MAX_CONCURRENT_RENDERS` | no | `2` | Simultaneous helm processes; extra requests get a 429. |
| `HELM_MAX_DIFF_LINES` | no | `20000` | Diffs longer than this are truncated. |

`HELM_CACHE_HOME`, `HELM_CONFIG_HOME` and `HELM_DATA_HOME` are set to paths under `/tmp` in
the Dockerfile. They are deliberately explicit rather than left to default under `$HOME`:
kubelet does not set `HOME` from `/etc/passwd`, so helm would otherwise resolve its cache to
`/.cache/helm` and fail on the first chart pull. With `readOnlyRootFilesystem: true`, mount
an `emptyDir` at `/tmp/helm`.

`FLASK_APP` and `PYTHONPATH` are set in the Dockerfile and don't need to be supplied.

### SSH key precedence

SSH is the fallback for local runs and the CLI, which has no browser to run an OAuth flow
through. When a GitHub token is present — the signed-in user's in the web app, or
`GITHUB_TOKEN`/`--github-token` on the CLI — the clone goes over https and every variable
below is ignored.

Only one of the three key variables is used, in this order:

1. `SSH_KEY_CONTENT_BASE64` — decoded, written to `/tmp/.ssh/id_key` (mode 0600).
2. `SSH_KEY_CONTENT` — written to `/tmp/.ssh/id_key` (mode 0600).
3. `SSH_KEY_PATH` — used as-is, `~` expanded.

If either content variable is set, it overrides `SSH_KEY_PATH`. The key content is validated:
it must start with `-----BEGIN` and end with one of `-----END OPENSSH PRIVATE KEY-----`,
`-----END RSA PRIVATE KEY-----`, or `-----END PRIVATE KEY-----`. A key that fails validation
is silently skipped and the clone will fail — check container logs for the `✗` lines.

An SSH config is generated at `/tmp/.ssh/config` with `StrictHostKeyChecking no` and
`UserKnownHostsFile /dev/null`, so no `known_hosts` setup is needed.

## Running

### Build

```bash
docker build -t helm-versions .
```

### With a base64 key (recommended)

```bash
docker run -p 5000:5000 \
  -e GIT_REPO_URL="git@github.com:NCAR/cisl-cloud-charts.git" \
  -e SSH_KEY_CONTENT_BASE64="$(base64 -w0 ~/.ssh/id_ed25519)" \
  helm-versions
```

### With a mounted key file

```bash
docker run -p 5000:5000 \
  -e SSH_KEY_PATH=/app/.ssh/id_rsa \
  -v ~/.ssh/id_ed25519:/app/.ssh/id_rsa:ro \
  helm-versions
```

The container runs as UID 1000 (`appuser`), so a mounted key must be readable by that UID.

### Limiting to specific clusters

Pass `CLUSTERS` to scope the dashboard to a subset:

```bash
docker run -p 5000:5000 \
  -e SSH_KEY_CONTENT_BASE64="$(base64 -w0 ~/.ssh/id_ed25519)" \
  -e CLUSTERS=mlc1 \
  helm-versions
```

```bash
docker run -p 5000:5000 \
  -e SSH_KEY_CONTENT_BASE64="$(base64 -w0 ~/.ssh/id_ed25519)" \
  -e CLUSTERS=mlc1,nwc3 \
  helm-versions
```

Leave `CLUSTERS` unset to get all five. An unknown name fails the analysis and shows up as
an error on `/api/charts`; `/debug` echoes the value the container actually received.

### One-off CLI report (no web server)

The same image can run the tracker directly, printing a text report and writing
`multi_cluster_chart_status.json` to the working directory. Override the `CMD`:

```bash
docker run --rm \
  -e SSH_KEY_CONTENT_BASE64="$(base64 -w0 ~/.ssh/id_ed25519)" \
  helm-versions python tracker.py --cluster mlc1
```

`--cluster` takes precedence over `CLUSTERS`, is repeatable (`-c mlc1 -c nwc3`), and also
accepts a comma-separated list. `python tracker.py --help` lists the rest (`--repo-url`,
`--ssh-key-path`, `--github-token`), which default to their env var equivalents.

The CLI has no browser to run an OAuth flow through, so it authenticates with an SSH key
or a personal access token. Prefer the environment over the flag — an argument is visible
in `ps` and in your shell history:

```bash
docker run --rm -e GITHUB_TOKEN helm-versions python tracker.py --cluster mlc1
```

### Kubernetes

With OAuth there is no git credential in the cluster — only the OAuth App's own client
secret and a cookie signing key:

```bash
kubectl create secret generic helm-versions-oauth \
  --from-literal=client-id=Iv1.xxxxxxxxxxxx \
  --from-literal=client-secret=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx \
  --from-literal=secret-key="$(openssl rand -hex 32)"
```

```yaml
spec:
  # Sessions are held in process memory, so a second replica would sign users out at
  # random as requests landed on the pod that didn't have their session.
  replicas: 1
  template:
    spec:
      containers:
        - name: helm-versions
          env:
            - name: GIT_REPO_URL
              value: git@github.com:NCAR/cisl-cloud-charts.git
            - name: CLUSTERS
              value: mlc1,nwc3
            - name: GITHUB_ALLOWED_TEAM
              value: NCAR/cirrus-admins
            - name: OAUTH_REDIRECT_URI
              value: https://helm-versions.example.org/auth/callback
            - name: GITHUB_CLIENT_ID
              valueFrom:
                secretKeyRef: { name: helm-versions-oauth, key: client-id }
            - name: GITHUB_CLIENT_SECRET
              valueFrom:
                secretKeyRef: { name: helm-versions-oauth, key: client-secret }
            - name: SECRET_KEY
              valueFrom:
                secretKeyRef: { name: helm-versions-oauth, key: secret-key }
          readinessProbe:
            httpGet: { path: /health, port: 5000 }
          volumeMounts:
            - { name: helm-tmp, mountPath: /tmp/helm }
      volumes:
        - name: helm-tmp
          emptyDir: {}
```

`/health` is deliberately reachable without a session so probes don't need a credential;
it reports no chart data. Everything else is default-deny.

#### With an SSH key instead (no OAuth)

Kubernetes already base64-encodes Secret values in `data:`. Store the *already
base64-encoded key* as the secret value so the app receives base64 after Kubernetes decodes
its own layer — i.e. double-encode when creating with `data:`, or use `stringData:` with the
single-encoded key.

```yaml
env:
  - name: SSH_KEY_CONTENT_BASE64
    valueFrom:
      secretKeyRef:
        name: helm-versions-ssh
        key: ssh-privatekey-base64
```

Leaving `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` unset serves the dashboard with **no
authentication**. Put something in front of it if you do this.

## Dashboard

A **Status** dropdown filters by staleness tier, and the summary cards are clickable and do
the same thing — one click to see only what's badly out of date. Like the cluster filter it
works client-side against data already loaded, and is remembered in `localStorage`. The
summary cards keep showing unfiltered totals: they are the legend and the navigation, not a
reflection of what the grid is currently showing.

A **Cluster** dropdown in the controls bar filters the view. It defaults to **All clusters**,
lists whatever clusters the API returned, and filtering happens client-side against data
already loaded, so switching is instant and doesn't re-run the analysis. The summary cards
recount to match the selection. The choice is remembered in `localStorage`; if a remembered
cluster is no longer in the data (e.g. `CLUSTERS` was narrowed), it falls back to All.

Note this filters the *view* only. `CLUSTERS` / `--cluster` control which clusters are
actually analyzed — use those to make the run faster, and the dropdown to focus the display.

## Endpoints

| Path | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Dashboard UI. |
| `/api/charts` | GET | Current chart data, `last_update`, `update_in_progress`. |
| `/api/refresh` | POST | Kicks off a re-analysis in a background thread. |
| `/api/versions` | GET | Versions available for one chart (`?chart=&repo=`), for the diff version picker. Served separately so `/api/charts` doesn't carry hundreds of tags per chart on every poll. |
| `/api/diff` | POST | Unified diff between two versions of a chart. Body: `chart_name`, `repo_url`, `old_version`, `new_version`. Synchronous — two renders of even a very large chart take a few seconds. |
| `/api/me` | GET | Who's signed in, for the header badge. |
| `/login` | GET | Starts the GitHub OAuth flow. Public. |
| `/auth/callback` | GET | OAuth callback: verifies state, exchanges the code, checks team membership. Public. |
| `/logout` | GET, POST | Drops the server-side token and clears the cookie. Public. |
| `/health` | GET | Liveness/readiness check. **Public** so probes need no credential; reports no chart data. |
| `/debug` | GET | Reports which config vars are set — booleans only, never values. |

Every path not marked Public requires a session when OAuth is configured. The gate is
default-deny by endpoint name, so a route added later is protected unless someone adds it
to `PUBLIC_ENDPOINTS` on purpose. Requests under `/api/` get a `401` with a JSON body
rather than a redirect, because `fetch()` follows redirects transparently and the
dashboard needs an error it can act on.

## Refresh behavior

Periodic auto-refresh is **disabled** (`background_updater()` is a no-op) — trigger updates
manually via `POST /api/refresh` or the dashboard button. Data is held in memory only and
is lost on restart.

Without OAuth, an initial analysis starts in a background thread at boot, so the app serves
immediately while data is still loading. **With OAuth there is no startup analysis**: the
clone runs as a signed-in user, and at boot there is nobody signed in. The dashboard is
empty until the first person signs in and hits Refresh.

## Running the tests

Standard library `unittest`, no dependencies to install and no network or helm binary
needed:

```bash
python -m unittest discover -s tests -t .
```

`tests/test_staleness.py` covers the tier classifier (including the sparse-patch-numbering
case that motivated counting releases, and the `v`-prefix regression). `tests/test_differ.py`
covers input validation, argv construction for both HTTPS and OCI charts, environment
isolation, and helm error classification.

`app/staleness.py` and `app/chartrefs.py` are stdlib-only by design, and `app/differ.py`
imports no Flask, which is what keeps those tests dependency-free.

The auth tests need what the app already depends on — Flask, PyYAML and `requests` — and
mock every outbound call; nothing touches the network or runs git.
`tests/test_githubauth.py` covers URL parsing, the team gate's denial cases and the token
store's expiry. `tests/test_clone.py` is mostly negative: the token must not appear in
`argv`, in the clone URL, or in a log line, and the askpass helper must answer the
username and password prompts *differently*. `tests/test_auth_routes.py` walks the login
flow end to end against Flask's test client, including the assertion that every route not
named in `PUBLIC_ENDPOINTS` refuses an anonymous request.

## Keeping the image patched

Everything that ships is pinned — `python:3.11-slim` is the one floating reference, and
it should stay floating so rebuilds pick up base fixes.

`app/requirements.txt` pins the **full** tree, transitive packages included, not just
Flask/PyYAML/requests. The looser version of this file left Werkzeug, Jinja2, urllib3 and
certifi unpinned, which is the worst of both worlds: builds weren't reproducible, and the
packages most likely to carry a CVE were the ones nobody was tracking. Regenerate with:

```bash
pip install --dry-run --report - Flask PyYAML requests
```

Deliberately *not* switched to `>=` ranges. Unpinned installs do pick up security fixes at
build time, but they also mean the image that passes CI isn't the image that ships, and a
major release lands with no review. Pin, and bump on a schedule (Dependabot or Renovate on
`app/requirements.txt` and the `HELM_VERSION`/`HELM_SHA256` args does this well).

Three things in the Dockerfile exist only to keep the scan clean, and are easy to
misread as cruft:

- `apt-get upgrade -y` in the runtime stage. Without it the image inherits whatever
  Debian shipped on the base image's build date, including fixes that are already
  published. This is also what makes a no-op rebuild worth running.
- `pip install --upgrade setuptools` before the requirements install — the base image's
  copy vendors flagged versions of `jaraco.context` and `wheel`.
- `pip uninstall setuptools pip` after it. Pip's own vendored tree (`msgpack`, a
  `pkg_resources` copied from setuptools 70.3.0) is the last thing in the image with
  fixable CVEs against it, and no pip release fixes them because they *are* pip's
  vendored copies. Nothing here imports pip or setuptools at runtime. Drop that line if
  you want an interactive pip back for debugging.

What's left after all of that is `git` and `openssh-client`, which can't go — cloning
`GIT_REPO_URL` over SSH is the whole first step — plus the `perl` that `git` depends on.
Their remaining findings have no published Debian fix; they clear when Debian publishes
one and the image is rebuilt.

## Troubleshooting

- **"No charts found"** — repo cloned but parsing found nothing. Check that `infra-chart/templates/`
  and `clusters/<name>/infraapps.yaml` exist at the expected paths.
- **Clone fails** — hit `/debug` to confirm which credential the app sees. On the SSH path
  the container logs include an `ssh -T git@github.com` auth test before the clone; on the
  OAuth path the log names the user whose token was used.
- **"redirect_uri_mismatch" from GitHub** — `OAUTH_REDIRECT_URI` doesn't exactly match the
  OAuth App's registered callback URL. It has to match including scheme and trailing path.
- **"That sign-in link has expired or didn't start here"** — the CSRF state didn't match.
  Usually a stale bookmark of `/auth/callback`, a session cookie that was dropped between
  `/login` and the callback, or a pod restart mid-login with `SECRET_KEY` unset. Start
  again from `/`.
- **Signed in, but "you're not a member"** — the check requires *active* membership of
  `GITHUB_ALLOWED_TEAM`; a pending invitation is refused with its own message. If you are
  certain the membership is active, confirm the org and team slug (the URL form, not the
  display name), and that the `read:org` scope was granted.
- **Everyone gets signed out at random** — more than one replica. Sessions live in process
  memory; run `replicas: 1`.
- **Signed out after every deploy** — `SECRET_KEY` is unset, so a new one is generated each
  start. Set it from a Secret.
- **A chart shows no latest version** — either no `repoURL`/`chart` was matched in the Argo
  template, or the chart name isn't in that repo's `index.yaml`. Logs print the available
  chart names on a miss.
- **No View Diff buttons anywhere** — the dashboard hides them when helm is unavailable.
  Check `helm_available` on `/health` and `helm_version` on `/debug`.
- **"can't be rendered with default values"** — expected for charts that require
  configuration (a hostname, a password, a storage class). The CI workflow has the same
  limitation; helm's own output is in the Details section.
- **"registry requires credentials"** — the dashboard pulls anonymously. Use the CI
  workflow for charts in private registries.
