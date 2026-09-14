# Helm binary, fetched in a builder stage so curl isn't shipped in the runtime image.
# Still pinned, so a rebuild can't silently change every rendered diff - but pinned to a
# supported release rather than 3.14.4. That build carried every CRITICAL in the image
# (Go 1.21.9 stdlib, github.com/docker/docker v24.0.9, google.golang.org/grpc v1.58.3)
# and the 3.14 line is EOL, so none of them were ever going to be fixed in place.
#
# The old comment warned that rendered output shifts with helm's built-in default
# .Capabilities.KubeVersion. That turns out not to hold across the 3.x line: the default
# has been frozen at v1.20.0 in every release from 3.14 through 3.22. Checked by
# rendering both binaries against ingress-nginx 4.11.0, cert-manager v1.15.1,
# argo-cd 7.3.4 and external-dns 1.14.5 - ~26k lines of manifests, byte-identical.
# (grafana 8.3.2 differs by 4 lines, but it differs from itself the same way: that's the
# randAlphaNum admin password the README already calls out.)
#
# Re-run that comparison before the next bump. The thing that does move between minors
# is .Capabilities.APIVersions, which grows with helm's vendored k8s libraries, so a
# chart gated on `.Capabilities.APIVersions.Has` could still flip.
FROM python:3.11-slim AS helmbin
ARG HELM_VERSION=3.22.0
# Literal digest from https://get.helm.sh/helm-v3.22.0-linux-amd64.tar.gz.sha256sum.
# Hardcoded rather than fetched alongside the tarball: downloading the checksum from
# the same host proves only transit integrity, since whoever controls the tarball
# controls the checksum file. A literal makes the build fail if upstream changes.
ARG HELM_SHA256=1e4ab49e429626cf6c6958d914248b78c9730803c2751b87627e171dc800e7bb
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
 && printf '%s  /tmp/helm.tgz\n' "${HELM_SHA256}" | sha256sum -c - \
 && tar -xzf /tmp/helm.tgz -C /tmp \
 && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm

# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# git and openssh-client are load-bearing - the tracker clones GIT_REPO_URL over SSH -
# so neither can be dropped to shed their CVEs.
#
# The upgrade is the point of this layer. Base image packages go stale between
# python:3.11-slim rebuilds, and without it the image inherits whatever Debian shipped
# on that build date; at last check that was fixed-but-unapplied CVEs in perl (3 of them
# CRITICAL, pulled in as a git dependency), gzip, pcre2, sqlite3 and libssh2. It also
# means rebuilding on an unchanged base is worth something, rather than reproducing the
# same findings.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
    git \
    openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=helmbin /usr/local/bin/helm /usr/local/bin/helm
# Fail the build, not the first diff at 3am, if the install is broken
RUN helm version --short

# Create SSH directory
RUN mkdir -p /app/.ssh && chmod 700 /app/.ssh

# Requirements before the app so editing app code doesn't reinstall the dependency tree.
COPY app/requirements.txt ./requirements.txt

# setuptools is upgraded before the install because the base image's copy vendors
# flagged versions of jaraco.context and wheel, and pip resolves against it.
#
# pip and setuptools are then removed. Nothing in the app imports either - it's Flask,
# PyYAML, requests and stdlib - and pip's own bundled vendor tree (msgpack, a
# pkg_resources copied from setuptools 70.3.0) is the last thing in the image with
# fixable CVEs against it. There is no pip version that fixes them, because they're
# pip's vendored copies; dropping pip is the only thing that does. The container runs
# as a non-root user, so pip could not have installed into site-packages anyway.
# To get an interactive pip back for debugging, drop the uninstall line and rebuild.
RUN pip install --no-cache-dir --upgrade pip setuptools \
 && pip install --no-cache-dir -r requirements.txt \
 && pip uninstall -y --root-user-action=ignore setuptools pip \
 && rm -rf /usr/local/lib/python3.11/ensurepip/_bundled

# Copy application files
COPY app/ .

# Create a non-root user
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Expose the port
EXPOSE 5000

# Set environment variables
ENV FLASK_APP=app.py
ENV PYTHONPATH=/app
ENV PORT=5000
ENV PYTHONUNBUFFERED=1

# Helm's paths are set explicitly rather than left to default under $HOME: USER does
# not reliably export HOME, and kubelet never sets it from /etc/passwd, so in
# Kubernetes helm would resolve its cache to /.cache/helm and fail on the first
# chart pull. /tmp is writable by uid 1000 and these are created lazily.
# With readOnlyRootFilesystem, mount an emptyDir at /tmp/helm.
ENV HELM_CACHE_HOME=/tmp/helm/cache
ENV HELM_CONFIG_HOME=/tmp/helm/config
ENV HELM_DATA_HOME=/tmp/helm/data

# Run the application
CMD ["python", "app.py"]
