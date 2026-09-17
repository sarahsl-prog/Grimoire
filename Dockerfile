# syntax=docker/dockerfile:1.7

# ---------------------------------------------------------------------------
# Builder — resolve dependencies into /opt/venv from uv.lock
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS builder

# Pinned to the host's uv version. uv.lock is revision 3; an older uv cannot
# read it, and an unpinned :latest makes builds non-reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.9.18 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# build-essential covers any dependency without a manylinux wheel.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Dependencies first, in their own layer, so source edits do not re-resolve.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# uv.lock pins torch from PyPI, which on Linux is the CUDA build plus ~2.7GB
# of nvidia-* packages.  The default cpu variant swaps in the same torch
# version from the CPU index and drops the CUDA libraries; the gpu variant
# keeps what the lock resolved.  Both end up on the same torch version.
#
# --reinstall-package torch is essential: the CUDA build is already installed
# at this exact version, so without it uv treats the install as satisfied and
# leaves a torch binary linked against CUDA libraries that were just removed.
#
# torchvision has to be swapped in the same breath: it is compiled against
# torch's C++ ABI, and the CUDA-linked build the lockfile resolved does not
# load against a torch reinstalled from the CPU index (docling's transformers
# import chain fails with "operator torchvision::nms does not exist" and the
# parser silently falls back to non-functional). Its version is read off the
# already-resolved environment rather than hardcoded, so it always matches
# whatever uv.lock pins.
ARG TORCH_VARIANT=cpu
ARG TORCH_VERSION=2.11.0
RUN if [ "$TORCH_VARIANT" = "cpu" ]; then \
        nvidia_pkgs="$(uv pip list --python /opt/venv --format=freeze \
            | grep '^nvidia-' | cut -d= -f1 | tr '\n' ' ')"; \
        torchvision_version="$(uv pip list --python /opt/venv --format=freeze \
            | grep '^torchvision==' | sed 's/^torchvision==//' | cut -d+ -f1)"; \
        if [ -n "$nvidia_pkgs" ]; then \
            uv pip uninstall --python /opt/venv $nvidia_pkgs; \
        fi; \
        uv pip install --python /opt/venv \
            --index-url https://download.pytorch.org/whl/cpu \
            --reinstall-package torch \
            --reinstall-package torchvision \
            "torch==${TORCH_VERSION}" "torchvision==${torchvision_version}"; \
    fi

# Install the project itself, non-editable, so the runtime stage needs no
# source tree on disk.
COPY README.md ./
COPY grimoire ./grimoire
RUN uv pip install --python /opt/venv --no-deps .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

# libmagic1 backs python-magic; libgl1 and libglib2.0-0 back Docling's PDF
# and image parsing, which grimoire.yaml enables by default; curl serves the
# container healthchecks.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libmagic1 \
        libgl1 \
        libglib2.0-0 \
        curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 grimoire

COPY --from=builder --chown=grimoire:grimoire /opt/venv /opt/venv

WORKDIR /app

# Alembic is not part of the installed package but db-migrate needs it.
COPY --chown=grimoire:grimoire alembic ./alembic
COPY --chown=grimoire:grimoire alembic.ini ./alembic.ini

# HF_HOME and XDG_CACHE_HOME sit under the home directory so a single volume
# persists every model download across restarts.
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HOME=/home/grimoire/.cache/huggingface \
    XDG_CACHE_HOME=/home/grimoire/.cache \
    GRIMOIRE_CONFIG=/app/grimoire.yaml

RUN mkdir -p /app/logs /app/cache && chown -R grimoire:grimoire /app

USER grimoire

EXPOSE 8001 8100

CMD ["uvicorn", "grimoire.api.main:app", "--host", "0.0.0.0", "--port", "8001"]
