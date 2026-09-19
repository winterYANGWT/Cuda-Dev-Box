#!/bin/bash
set -euo pipefail

: "${PYTHON_VERSION:?PYTHON_VERSION must be set}"

# Install only the default interpreter. Each project owns its dependencies and .venv via uv sync.
uv python install "$PYTHON_VERSION" --default

python --version
uv --version
