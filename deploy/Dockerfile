# 必须在任意第一个 FROM 之前声明，第二个阶段才能使用 ${PYTHON_RUNTIME_IMAGE}。
# 国内拉 Python 基础镜像：docker build --build-arg PYTHON_RUNTIME_IMAGE=... 或本机 docker tag
# 固定 CPython 3.11 + glibc（bookworm），避免 onnxruntime 在部分 node:slim 环境无 wheel。
ARG PYTHON_RUNTIME_IMAGE=python:3.11-slim-bookworm

# -----------------------------------------------------------------------------
# Stage 1: build console frontend (dist not committed in repo).
# -----------------------------------------------------------------------------
FROM agentscope-registry.ap-southeast-1.cr.aliyuncs.com/agentscope/node:slim AS console-builder
WORKDIR /app
COPY console /app/console
RUN cd /app/console && npm ci --include=dev && npm run build

# -----------------------------------------------------------------------------
# Stage 2: runtime image with Python, Chromium, and app.
# 国内临时拉 Python 基础镜像（不改 Docker daemon）二选一：
# 1) 构建时走镜像代理（推荐，一条命令即可）：
#    docker build --build-arg PYTHON_RUNTIME_IMAGE=docker.m.daocloud.io/library/python:3.11-slim-bookworm ...
# 2) 宿主机先拉再打 tag，再使用默认 FROM 构建：
#    docker pull docker.m.daocloud.io/library/python:3.11-slim-bookworm
#    docker tag docker.m.daocloud.io/library/python:3.11-slim-bookworm python:3.11-slim-bookworm
# 固定 CPython 3.11 + glibc（bookworm），避免 onnxruntime 在部分 node:slim 环境无 wheel。
FROM ${PYTHON_RUNTIME_IMAGE}

# ENV variables
ENV NODE_ENV=production
ENV WORKSPACE_DIR=/app
ENV COPAW_WORKING_DIR=/app/working
ENV COPAW_SECRET_DIR=/app/working.secret

# Channel filtering: use COPAW_DISABLED_CHANNELS (exclusion, recommended)
# or COPAW_ENABLED_CHANNELS (whitelist). Override at runtime with -e.
ARG COPAW_DISABLED_CHANNELS="imessage"
ENV COPAW_DISABLED_CHANNELS=${COPAW_DISABLED_CHANNELS}
ARG COPAW_ENABLED_CHANNELS=""
ENV COPAW_ENABLED_CHANNELS=${COPAW_ENABLED_CHANNELS}

ARG DEBIAN_FRONTEND=noninteractive

# If apt cannot reach deb.debian.org (e.g. blocked CDN), use a Debian mirror at build time:
#   docker build --build-arg APT_USE_MIRROR=1 -f deploy/Dockerfile ...
# Optional: --build-arg APT_MIRROR_HOST=mirrors.tuna.tsinghua.edu.cn
ARG APT_USE_MIRROR=0
ARG APT_MIRROR_HOST=mirrors.aliyun.com
RUN set -eux; \
  if [ "$APT_USE_MIRROR" = "1" ]; then \
    find /etc/apt -type f \( -name "debian.sources" -o -name "*.sources" -o -name "*.list" -o -name "sources.list" \) | while read -r f; do \
      sed -i "s/deb.debian.org/${APT_MIRROR_HOST}/g" "$f"; \
      sed -i "s/security.debian.org/${APT_MIRROR_HOST}/g" "$f"; \
    done; \
  fi

RUN apt-get update && apt-get install -y --fix-missing \
    curl  \
    build-essential  \
    libssl-dev  \
    git  \
    supervisor  \
    vim  \
    gettext-base \
    xfce4 \
    xfce4-terminal \
    xvfb \
    dbus-x11 \
    fonts-wqy-zenhei \
    fonts-wqy-microhei \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get clean


RUN apt-get update && apt-get install -y --fix-missing \
    chromium \
    chromium-sandbox \
    libx11-xcb1 \
    libxcomposite1 \
    libxdamage1 \
    libxext6 \
    libxfixes3 \
    libxi6 \
    libxtst6 \
    libnss3 \
    libglib2.0-0 \
    libdrm2 \
    libgbm1 \
    libasound2 \
    fonts-liberation \
    libu2f-udev \
  && rm -rf /var/lib/apt/lists/* \
  && apt-get clean


RUN sed -i 's/^CHROMIUM_FLAGS=""/CHROMIUM_FLAGS="--no-sandbox"/' /usr/bin/chromium

# Playwright: use system Chromium (already installed above).
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium
# Avoid Playwright downloading its own browser when executable_path is used.
ENV PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
# Indicate running in container (used e.g. for Chromium --no-sandbox).
ENV COPAW_RUNNING_IN_CONTAINER=1

WORKDIR ${WORKSPACE_DIR}

RUN python3 -m venv venv
ENV PATH="/app/venv/bin:$PATH"

COPY pyproject.toml setup.py README.md ./
COPY src ./src
# Inject console dist from build stage (repo does not commit dist).
COPY --from=console-builder /app/console/dist/ ./src/copaw/console/
RUN pip install --no-cache-dir .[ollama]

# Init working dir with default config.json and HEARTBEAT.md (build time).
RUN copaw init --defaults --accept-security

# lcClaw app port (default 8088). Override at runtime with -e COPAW_PORT=3000.
ENV COPAW_PORT=8088

COPY deploy/config/supervisord.conf.template /etc/supervisor/conf.d/supervisord.conf.template
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh

EXPOSE 8088

CMD ["/entrypoint.sh"]
