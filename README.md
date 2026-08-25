# helm-versions

A small Flask dashboard that tracks Helm chart versions across clusters. It clones a
private git repo of cluster config, reads the `chartVersion` pinned for each enabled app,
looks up the latest version in each chart's upstream Helm repo, and shows what's behind.

## How it works

1. Clones `GIT_REPO_URL` into a temp dir over SSH.
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
4. Looks up the latest version for each chart (10 in parallel, results cached per run):
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
5. Serves the comparison at `/`.

The known cluster list is `HelmChartTracker.DEFAULT_CLUSTERS` in `app/tracker.py`
(`mgmt`, `nwc1`, `mlc1`, `nwc3`, `mlc3`). All of them are analyzed unless you narrow the
selection with the `CLUSTERS` env var or the CLI's `--cluster` flag. Adding a genuinely new
cluster still means editing `DEFAULT_CLUSTERS`.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `GIT_REPO_URL` | no | `git@github.com:NCAR/cisl-cloud-charts.git` | Repo to clone and analyze. |
| `SSH_KEY_CONTENT_BASE64` | one of the three | — | Base64-encoded SSH private key. Preferred for Kubernetes Secrets. |
| `SSH_KEY_CONTENT` | one of the three | — | Raw PEM SSH private key, including header/footer lines. |
| `SSH_KEY_PATH` | one of the three | — | Path to an SSH private key file already present in the container. |
| `CLUSTERS` | no | all clusters | Comma-separated subset of clusters to analyze, e.g. `mlc1` or `mlc1,nwc3`. Unknown names abort the run. |
| `PORT` | no | `5000` | Port Flask listens on. Already set in the Dockerfile. |

`FLASK_APP` and `PYTHONPATH` are set in the Dockerfile and don't need to be supplied.

### SSH key precedence

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
`--ssh-key-path`), which default to their env var equivalents.

### Kubernetes

```yaml
env:
  - name: GIT_REPO_URL
    value: git@github.com:NCAR/cisl-cloud-charts.git
  - name: CLUSTERS
    value: mlc1,nwc3
  - name: SSH_KEY_CONTENT_BASE64
    valueFrom:
      secretKeyRef:
        name: helm-versions-ssh
        key: ssh-privatekey-base64
```

Note that Kubernetes already base64-encodes Secret values in `data:`. Store the *already
base64-encoded key* as the secret value so the app receives base64 after Kubernetes decodes
its own layer — i.e. double-encode when creating with `data:`, or use `stringData:` with the
single-encoded key.

## Dashboard

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
| `/health` | GET | Liveness/readiness check. |
| `/debug` | GET | Reports which config vars are set and whether the key file exists. |

## Refresh behavior

An initial analysis starts in a background thread at boot, so the app serves immediately
while data is still loading. Periodic auto-refresh is **disabled** (`background_updater()` is
a no-op) — trigger updates manually via `POST /api/refresh` or the dashboard button. Data is
held in memory only and is lost on restart.

## Troubleshooting

- **"No charts found"** — repo cloned but parsing found nothing. Check that `infra-chart/templates/`
  and `clusters/<name>/infraapps.yaml` exist at the expected paths.
- **Clone fails** — hit `/debug` to confirm which key variable the app sees. Container logs
  include an `ssh -T git@github.com` auth test before the clone.
- **A chart shows no latest version** — either no `repoURL`/`chart` was matched in the Argo
  template, or the chart name isn't in that repo's `index.yaml`. Logs print the available
  chart names on a miss.
