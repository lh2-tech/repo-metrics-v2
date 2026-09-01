# syntax=docker/dockerfile:1
# run_metrics_and_sample.py, containerized.
#
# Build context MUST be the DataLabs root (not this docker/ directory) so it
# can COPY datalabs_paths.py and skills/repo-metrics/scripts/ directly:
#
#   docker build -f skills/repo-metrics/docker/Dockerfile -t repo-metrics-sample .
#
# See skills/repo-metrics/docker/README.md for full run instructions,
# volume/token setup, and — importantly — the fallback behavior for every
# tool this image installs.

FROM python:3.12-slim

# git            required, no fallback.
# curl           only used at build time, to fetch the scc binary below.
# nodejs, npm    for jscpd (duplication detection). If this install step is
#                ever unavailable at runtime, the scripts detect that and
#                fall back to a weaker built-in detector automatically —
#                see README.md. Not a hard requirement, but installed here
#                so the strong path is the default.
# docker.io      the `docker` CLI only (not a nested daemon) — lets
#                --build-check docker work IF the host's docker socket is
#                bind-mounted in at run time (see README.md). Without that
#                mount, repo_metrics.py detects the daemon is unreachable
#                and downgrades to --build-check detect automatically; this
#                package's CLI presence alone does not change that.
# apt.debian.org round-robins across many mirrors; occasionally one serves a
# stale/corrupted package (hash-sum mismatch) or 403s outright, and some
# corporate proxies/VPNs silently mangle plain http:// traffic in transit —
# both were observed here across repeated build retries. Force https://
# (ca-certificates is already present in this base image, so TLS works
# before this step even runs) and retry a few times before giving up.
RUN <<'EOF'
set -eux
for f in /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list; do
    if [ -f "$f" ]; then
        sed -i \
            -e 's|http://deb.debian.org|https://deb.debian.org|g' \
            -e 's|http://security.debian.org|https://security.debian.org|g' \
            "$f"
    fi
done
n=0
until [ "$n" -ge 3 ]; do
    if apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates nodejs npm docker.io; then
        break
    fi
    n=$((n + 1))
    echo "apt-get attempt $n/3 failed; retrying in 5s..." >&2
    sleep 5
done
if [ "$n" -ge 3 ]; then
    echo "ERROR: apt-get failed after 3 attempts" >&2
    exit 1
fi
rm -rf /var/lib/apt/lists/*
EOF

# Bind-mounted checkouts (repos.txt local paths, /data/checkouts) keep the
# host's UID, which differs from this image's root user. Git >= 2.35.2
# refuses to run on a repo it doesn't own ("dubious ownership") unless the
# path is explicitly trusted. Every repo under these mounts is the caller's
# own, read-only, so trust all of them rather than one directory at a time.
RUN git config --system --add safe.directory '*'

RUN npm install -g jscpd@5

# scc — the one hard, non-fallback-able requirement. TARGETARCH is set
# automatically by BuildKit; mapped to scc's own release-asset naming.
ARG TARGETARCH
ARG SCC_VERSION=4.0.0
RUN set -eux; \
    case "${TARGETARCH}" in \
        amd64) SCC_ARCH=x86_64 ;; \
        arm64) SCC_ARCH=arm64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors "https://github.com/boyter/scc/releases/download/v${SCC_VERSION}/scc_Linux_${SCC_ARCH}.tar.gz" \
        -o /tmp/scc.tar.gz; \
    tar -xzf /tmp/scc.tar.gz -C /usr/local/bin scc; \
    chmod +x /usr/local/bin/scc; \
    rm /tmp/scc.tar.gz; \
    scc --version

WORKDIR /app
COPY datalabs_paths.py /app/datalabs_paths.py
COPY skills/repo-metrics/scripts/ /app/skills/repo-metrics/scripts/

# Everything the scripts write lands under here — bind-mount it (see
# README.md) or it disappears when the container exits.
RUN mkdir -p /app/outputs /app/clones

WORKDIR /data/checkouts
ENTRYPOINT ["python3", "/app/skills/repo-metrics/scripts/run_metrics_and_sample.py"]
CMD ["--help"]
