# helm-versions

A Helm chart for deploying the [Helm chart version dashboard](../../README.md) to CIRRUS,
with GitHub OAuth authentication and secrets injected from
[OpenBao](https://openbao.org/) (`bao.k8s.ucar.edu`) via ExternalSecrets.

Templated from
[cirrus-examples/external-secret-helm](https://github.com/NCAR/cirrus-examples/tree/main/helm/external-secret-helm),
with the differences noted under [Deviations](#deviations-from-external-secret-helm).

## Prerequisites

| Parameter | Description |
|-----------|-------------|
| **FQDN** | Full URL for the dashboard, must end in `.k8s.ucar.edu` and be unique |
| **Container image** | A pre-built image in a registry CIRRUS can pull from. `repository` is set once; `tag` is written by CI on each build. |
| **Visibility** | `internal` (UCAR network/VPN) or `external` (public) |
| **OAuth App** | A GitHub OAuth App, registered before deploying — see below |
| **Secret path** | The path in `bao.k8s.ucar.edu` holding the client secret and cookie key |

> **Note:** A `SecretStore` must be configured in your namespace to access OpenBao.
> Contact [cirrus-admin@ucar.edu](mailto:cirrus-admin@ucar.edu) to have this set up.

### GitHub OAuth App setup

On GitHub, **Settings → Developer settings → OAuth Apps → New OAuth App**:

| Field | Value |
|-------|-------|
| Homepage URL | `https://<your fqdn>` |
| Authorization callback URL | `https://<your fqdn>/auth/callback` |

The callback URL must match what the chart sends GitHub. The chart derives it from
`webapp.tls.fqdn`, so as long as the FQDN here and in `values.yaml` agree, they cannot
drift apart. A mismatch fails the login with `redirect_uri_mismatch` rather than doing
anything subtle.

Access is limited to active members of `oauth.allowedTeam` (default
`NCAR/cirrus-admins`). Nothing else grants access, and nobody needs an SSH key or a
token of their own: the dashboard clones the config repo using the OAuth token of
whoever signs in.

> **Check this before rolling out:** if the GitHub org has *OAuth App access
> restrictions* enabled, an org owner has to approve the app once under **Settings →
> Third-party Access**. Until they do, tokens from it cannot read org-owned private repos
> **or** org team data, and every legitimate team member is turned away at sign-in. The
> sign-in page names this case specifically rather than reporting it as a membership
> problem, so the message will tell you if this is what happened.

### OpenBao secret setup

Store two values at `oauth.secret.secretPath`:

| Key | Value |
|-----|-------|
| `client-secret` | The OAuth App's client secret |
| `secret-key` | A random string that signs the session cookie — `openssl rand -hex 32` |

Rotating `secret-key` signs everyone out; it does not otherwise affect the deployment.

The **client id is not stored in OpenBao**. It appears in every authorization URL the
browser follows, so it is not a secret, and keeping it in `values.yaml` means the
deployed configuration is reviewable in git rather than split across two systems.

## Configuration

Set `oauth.clientId` and the FQDN; the rest has working defaults.

```yaml
webapp:
  name: helm-versions                     # Name for k8s objects
  group: helm-versions                    # Group label for related resources
  path: /                                 # URL path suffix
  tls:
    fqdn: helm-versions.k8s.ucar.edu      # Must be unique and end in .k8s.ucar.edu
    secretName: incommon-cert-helm-versions   # Unique TLS secret name for your FQDN
  ingress:
    visibility: internal                  # internal or external
  container:
    image:
      repository: hub.k8s.ucar.edu/cirrus/helm-versions
      tag: ""                             # REQUIRED - CI sets this per build
    port: 5000                            # Port the container listens on
    requests: { memory: 512M, cpu: 500m }
    limits:   { memory: 2G,   cpu: 2 }
  tmp:
    sizeLimit: 2Gi                        # Scratch space: clone + helm chart cache

tracker:
  gitRepoUrl: git@github.com:NCAR/cisl-cloud-charts.git
  clusters: ""                            # e.g. "mlc1,nwc3". Empty = all clusters.
  sessionTtlHours: 8

oauth:
  clientId: ""                            # REQUIRED - rendering fails without it
  allowedTeam: NCAR/cirrus-admins
  redirectUri: ""                         # Derived from tls.fqdn unless set
  secret:
    secretPath: cirrus/helm-versions
    clientSecretKey: client-secret
    sessionKeyKey: secret-key
```

## Deploying

```bash
helm upgrade --install helm-versions ./helm/helm-versions \
  --namespace cirrus \
  --set oauth.clientId=Iv1.xxxxxxxxxxxx
```

Nothing is analyzed at startup — the clone runs as a signed-in user, and at boot there
is nobody signed in. The first person to sign in gets an empty dashboard until they hit
**Refresh**; everyone after that sees the result without refreshing.

## The image tag

`webapp.container.image` is split into `repository` and `tag`, and `tag` is **required** —
rendering fails without it rather than guessing.

`tag` is never `latest`. `imagePullPolicy` is `IfNotPresent`, so a re-pushed `latest`
would never be pulled: the deployment would sit on a stale image and report success. An
immutable per-build tag is what makes a rollout mean anything, and makes a rollback a
matter of naming an older tag.

[`.github/workflows/build-push-image.yaml`](../../.github/workflows/build-push-image.yaml)
writes the short commit SHA into `tag` after a successful push, and commits it back to
`main`. So the chart in git always names an image that exists in the registry — the bump
happens *after* the push, never before. Before the first CI build, `helm template` needs
`--set webapp.container.image.tag=<something>`.

`repository` is read *out of this file* by the workflow, so renaming the registry project
is one edit here rather than four that can drift apart.

## Deviations from external-secret-helm

Five, each forced by how the application behaves:

**`replicas` is pinned to 1 and is not a value.** Sign-in sessions are held in the
application's memory, so a second replica signs people out at random as their requests
land on the pod that doesn't have their session. This is a correctness constraint, not a
capacity setting, so it isn't offered as a knob. `strategy: Recreate` is the same
constraint applied to rollouts — a rolling update briefly runs two pods. Scaling out
means giving the app's `TokenStore` a shared backend first; it is about thirty lines
behind three methods (`create` / `get` / `drop`) in `app/githubauth.py`.

**Two secrets instead of one.** The example injects a single key; the ExternalSecret here
pulls `client-secret` and `secret-key` from one OpenBao path.

**Readiness and liveness probe different things.** `/health` reports `503` when the app
has no working authentication configuration, so readiness fails and a bad rollout stops
rather than completing onto pods that refuse every request. Liveness is a plain TCP check
on purpose: restarting a pod does not fix a missing Secret, and a crash-looping pod is
harder to read the logs of than a running one that says why it is unhappy.

**`image` is split into `repository` and `tag`.** The example uses one `image:` string.
CI rewrites the tag on every build, and a single key is unambiguous to rewrite where
reconstructing a whole path risks clobbering the registry. See [The image tag](#the-image-tag).

**A read-only root filesystem and a writable `/tmp`.** The container drops all
capabilities and runs with `readOnlyRootFilesystem: true` as uid 1000. It needs exactly
one writable path: `/tmp`, an `emptyDir`, which holds the git clone, helm's chart cache
(`HELM_*_HOME` point under `/tmp/helm`) and the short-lived `GIT_ASKPASS` helper the
clone authenticates with. None of it should outlive the pod, so it is deliberately not a
PVC. Verified by running the image with `--read-only --tmpfs /tmp --cap-drop ALL
--user 1000:1000`: the app starts, `/health` answers, and a clone reaches GitHub and gets
as far as credential rejection rather than a filesystem error.

## Templates

| Template | Resource |
|----------|----------|
| `deployment.yaml` | Deployment — single replica, read-only rootfs, probes, `/tmp` emptyDir |
| `service.yaml` | ClusterIP Service on the container port |
| `ingress.yaml` | Ingress with `traefik-{visibility}` and an InCommon TLS certificate |
| `external_secrets.yaml` | ExternalSecret pulling both secrets from OpenBao |

## Troubleshooting

| Symptom | Cause |
|---------|-------|
| Pod never becomes Ready, `/health` returns `503 not_configured` | The OAuth Secret didn't resolve. Check the ExternalSecret synced and the key names match `values.yaml`. |
| Pod crashes at startup with `ConfigError` | One OAuth variable is set without the other, or `OAUTH_REDIRECT_URI` is missing. The message names which. |
| `redirect_uri_mismatch` from GitHub | The OAuth App's callback URL doesn't match `https://<fqdn>/auth/callback`. |
| "GitHub wouldn't answer whether you're in ..." | The org restricts OAuth Apps and hasn't approved this one. An org owner approves it once — see the note above. |
| "Your GitHub token isn't authorized for the ... organization" | The org enforces SAML SSO; authorize the app for it on GitHub and sign in again. |
| "You're not a member of ..." | Genuinely not an active member of `oauth.allowedTeam`. A pending invitation gets its own message. |
| Everyone signed out after a deploy | Expected: sessions are in-memory and the pod was replaced. |
| `webapp.container.image.tag is required` | No CI build has run yet, or `values.yaml` was reverted. Pass `--set webapp.container.image.tag=<sha>`. |

More detail in the [application README](../../README.md#authentication).
