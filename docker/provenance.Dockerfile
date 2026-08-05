FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG UV_VERSION=0.12.1

# Ubuntu 24.04 supplies Python 3.12 and strace 6.8.  The collector requires
# strace >= 6.6 for --kill-on-exit, which ensures an interrupted collector does
# not leave an untraced workload running.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        git \
        python3 \
        python3-venv \
        strace \
    && rm -rf /var/lib/apt/lists/*

# Keep uv and the application environment outside /workspace.  The launcher
# bind-mounts the checkout at /workspace so certificates remain host-visible.
RUN python3 -m venv /opt/uv \
    && /opt/uv/bin/pip install --no-cache-dir "uv==${UV_VERSION}"

ENV PATH="/opt/uv/bin:${PATH}"
ENV UV_PROJECT_ENVIRONMENT=/opt/crucible-venv

WORKDIR /opt/crucible
COPY pyproject.toml uv.lock README.md ./
COPY crucible ./crucible
RUN uv sync --frozen --no-dev

# Import the mounted checkout's source while using the baked, locked
# dependencies and console entry point.  HOME must be writable when the
# launcher maps the container process to the invoking host UID/GID.
ENV PATH="/opt/crucible-venv/bin:/opt/uv/bin:${PATH}"
ENV PYTHONPATH=/workspace
ENV HOME=/tmp

WORKDIR /workspace
ENTRYPOINT ["crucible"]
CMD ["--help"]
