# Helm binary, fetched in a builder stage so curl isn't shipped in the runtime image.
# Pinned to 3.14.4 to match the Helm Diff GitHub Actions workflow: rendered output
# depends on helm's built-in default .Capabilities.KubeVersion, which shifts between
# helm minors, so bumping this silently changes every diff.
FROM python:3.11-slim AS helmbin
ARG HELM_VERSION=3.14.4
# Literal digest from https://get.helm.sh/helm-v3.14.4-linux-amd64.tar.gz.sha256sum.
# Hardcoded rather than fetched alongside the tarball: downloading the checksum from
# the same host proves only transit integrity, since whoever controls the tarball
# controls the checksum file. A literal makes the build fail if upstream changes.
ARG HELM_SHA256=a5844ef2c38ef6ddf3b5a8f7d91e7e0e8ebc39a38bb3fc8013d629c1ef29c259
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL -o /tmp/helm.tgz "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
 && printf '%s  /tmp/helm.tgz\n' "${HELM_SHA256}" | sha256sum -c - \
 && tar -xzf /tmp/helm.tgz -C /tmp \
 && install -m 0755 /tmp/linux-amd64/helm /usr/local/bin/helm

# Use Python 3.11 slim image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=helmbin /usr/local/bin/helm /usr/local/bin/helm
# Fail the build, not the first diff at 3am, if the install is broken
RUN helm version --short

# Create SSH directory
RUN mkdir -p /app/.ssh && chmod 700 /app/.ssh

# Copy application files
COPY app/ .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

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
