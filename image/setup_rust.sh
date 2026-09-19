#!/bin/bash
set -euo pipefail

# rustup, cargo and rustc are copied from the official rust image in the
# Dockerfile. That image installs rustup's "minimal" profile, so add the
# formatter and linter the old rustup-init "default" profile shipped with.
rustup component add rustfmt clippy

rustc --version
cargo --version
cargo fmt --version
cargo clippy --version
