# Playwright's published image already contains Chromium plus every system
# library it needs (libnss3, libatk, libgbm, ...), which is the single hardest
# part of containerising a browser agent.
#
# The version MUST match the playwright pin in requirements.txt. Playwright
# refuses to run against a browser build from a different version, and the
# failure is a baffling "executable doesn't exist" at launch rather than a clear
# version error. Bump both together.
#
# TARGETARCH comes from BuildKit and matches Playwright's own tag suffix, so one
# Dockerfile serves both architectures. Oracle's free tier is Ampere A1 (arm64)
# while a laptop or a Fly machine is amd64, and the difference is one tag. If
# TARGETARCH is ever empty (a builder without BuildKit) the unsuffixed -noble tag
# is a multi-arch manifest and still resolves to the right image.
ARG PLAYWRIGHT_VERSION=1.49.1
ARG TARGETARCH
FROM mcr.microsoft.com/playwright/python:v${PLAYWRIGHT_VERSION}-noble${TARGETARCH:+-$TARGETARCH}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Every mutable path on one mount, so a single volume captures the databases,
# the run traces and the tool sandbox. See fly.toml [mounts].
ENV DATA_DIR=/data \
    RUNS_DIR=/data/runs \
    WORKSPACE_DIR=/data/workspace

WORKDIR /app

# Dependencies first: this layer is cached until requirements.txt itself changes,
# which matters because the Playwright base image is ~2GB.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Created here so the app also runs without a volume mounted (docker run, CI).
RUN mkdir -p /data/runs /data/workspace

# Chromium is told --no-sandbox in agent/tools/browser.py, so running as root
# here is intentional. Dropping that flag would require a non-root user and a
# chown of the volume on every deploy.
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
p=int(os.environ.get('PORT','8080')); \
sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{p}/health',timeout=4).status==200 else 1)"

# Deliberately no --reset. The apps call bootstrap() on startup, which is
# idempotent: a fresh volume seeds itself, and every later boot detects the
# existing rows and leaves them alone. Passing --reset here would wipe the
# volume on every single deploy.
CMD ["python", "-u", "scripts/serve.py"]