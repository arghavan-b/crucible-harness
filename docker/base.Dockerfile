# Crucible base image (CPU, Python 3.12). Build:
#   docker build -f docker/base.Dockerfile -t crucible/base:py3.12 .
#
# For CUDA, base off an nvidia/cuda image instead and tag e.g.
#   crucible/base:cuda12.4-py3.12  (see DockerEnvironmentManager.BASE_IMAGE_MATRIX)
FROM python:3.12-slim

# uv for fast, deterministic dependency operations (design §15).
RUN pip install --no-cache-dir uv

# Common build tooling many research repos need.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
