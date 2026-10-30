#!/bin/bash
set -euo pipefail

: "${PYTHON_VERSION:?PYTHON_VERSION must be set}"

# Install only the default interpreter. Each project owns its dependencies and .venv via uv sync.
# It lives outside /root, which devbox create mounts from the host: a mounted home
# would hide it, or keep an old one after an image update. Interpreters installed
# later with `uv python install` go to the persisted home as usual.
UV_PYTHON_BIN_DIR=/usr/local/bin uv python install "$PYTHON_VERSION" --default --install-dir /opt/uv/python

python --version
uv --version
