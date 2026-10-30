# syntax=docker/dockerfile:1

# Build with: host/devbox build (tags cuda-devbox:<VERSION file> and latest).
# Version defaults live here; override any one with --build-arg NAME=value.
#
# ARGs declared here (global scope) are usable in any FROM line below, but
# NOT automatically inside a stage's RUN/COPY — the final stage re-declares
# each one it needs with a bare `ARG NAME` (no value) right before the step
# that uses it. Every RUN after a declaration carries that value in its cache
# key, so declaring it earlier would rebuild unrelated layers on each change.
#
# Base images, repository and tag as separate ARGs.
ARG UV_REPO=astral/uv
ARG UV_TAG=0.12.0
ARG GO_REPO=golang
ARG GO_TAG=1-trixie
ARG JAVA_REPO=eclipse-temurin
ARG JAVA_TAG=25-jdk-resolute
ARG MAVEN_REPO=maven
ARG MAVEN_TAG=3-eclipse-temurin-25-noble
ARG GRADLE_REPO=gradle
ARG GRADLE_TAG=9-jdk25-resolute
ARG NODE_REPO=node
ARG NODE_TAG=lts-trixie
ARG DOCKER_REPO=docker
ARG DOCKER_TAG=29
ARG RUST_REPO=rust
ARG RUST_TAG=1-slim-trixie
ARG BASE_REPO=nvidia/cuda
ARG BASE_TAG=13.3.1-cudnn-devel-ubuntu26.04
#
# KOTLIN_VERSION/PYTHON_VERSION: setup_jvm.sh and setup_python.sh each guard
# with `: "${VAR:?...}"` and fail with a clear message if empty.
ARG KOTLIN_VERSION=2.3.21
ARG PYTHON_VERSION=3.14
#
# Pin these to specific releases when a repeatable CLI version is needed.
ARG CODEX_VERSION=latest
ARG CLAUDE_CODE_VERSION=latest
ARG DEEPSEEK_HARNESS_VERSION=latest

FROM ${UV_REPO}:${UV_TAG} AS uv-sdk
FROM ${GO_REPO}:${GO_TAG} AS go-sdk
FROM ${JAVA_REPO}:${JAVA_TAG} AS java-sdk
FROM ${MAVEN_REPO}:${MAVEN_TAG} AS maven-sdk
FROM ${GRADLE_REPO}:${GRADLE_TAG} AS gradle-sdk
FROM ${NODE_REPO}:${NODE_TAG} AS node-sdk
FROM ${DOCKER_REPO}:${DOCKER_TAG} AS docker-sdk
FROM ${RUST_REPO}:${RUST_TAG} AS rust-sdk

FROM ${BASE_REPO}:${BASE_TAG}

LABEL maintainer="winteryan"
LABEL version="26.04"
LABEL description="This is a Docker image for Ubuntu 26.04"

ENV CUDA_HOME="/usr/local/cuda"
ENV TZ="Asia/Tokyo" \
    LANG="C.UTF-8" \
    LANGUAGE="en_US:en" \
    LC_ALL="C.UTF-8"

# Install ca-certificates first using default sources
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ca-certificates gnupg tzdata && \
    update-ca-certificates

# Configure timezone before installing the remaining development tools.
RUN ln -snf "/usr/share/zoneinfo/$TZ" /etc/localtime \
    && echo "$TZ" > /etc/timezone

# Install system development packages. openssh-server's install generates SSH
# host keys; delete them in this same layer so no layer of the image keeps them.
# devbox create mounts per-container keys at /etc/ssh/host-keys instead.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    wget \
    fontconfig p11-kit \
    git curl vim unzip htop bubblewrap \
    build-essential autoconf automake \
    gfortran-15 \
    clang clangd clang-format clang-tidy cmake gdb pkg-config nasm \
    libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libncursesw5-dev xz-utils tk-dev \
    libxml2-dev libxmlsec1-dev libffi-dev liblzma-dev \
    openssh-server && \
    rm -f /etc/ssh/ssh_host_*

# The image's locale is C.UTF-8, built into glibc, everywhere including SSH
# sessions (setup_ssh.sh no longer accepts the client's LANG/LC_*). Being UTF-8,
# it handles Chinese, Japanese and any other text. Only en_US.UTF-8 is generated
# besides, for programs that ask for it by name; all ~154 UTF-8 locales took 103 MB.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    apt-get update && \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends locales && \
    sed -i 's|^# *\(en_US\.UTF-8 UTF-8\)|\1|' /etc/locale.gen && \
    locale-gen

# Set clang as default compiler
RUN update-alternatives --install /usr/bin/cc cc /usr/bin/clang 100 \
    && update-alternatives --install /usr/bin/c++ c++ /usr/bin/clang++ 100
ENV CC=clang CXX=clang++

# Install C/C++ libraries (independent layer, easy to extend)
COPY image/setup_cpp_libs.sh /tmp/setup_cpp_libs.sh
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    chmod +x /tmp/setup_cpp_libs.sh && /tmp/setup_cpp_libs.sh && rm /tmp/setup_cpp_libs.sh

# Keep the Fortran and C++ system toolchains on GCC 15. This also catches
# incomplete GCC installations that make Clang lose the standard C++ headers.
RUN gfortran-15 -dumpversion | grep -qx '15' \
    && printf '%s\n' '#include <iostream>' 'int main() { return 0; }' \
    | clang++ -x c++ -fsyntax-only -

# Set up SSH
COPY image/setup_ssh.sh /tmp/setup_ssh.sh
RUN chmod +x /tmp/setup_ssh.sh && /tmp/setup_ssh.sh && rm /tmp/setup_ssh.sh
EXPOSE 22 8000-8099/tcp

# Set Directory
RUN mkdir -p /root/data

# Toolchains copied from the official images above. Each COPY --from sits
# right before the steps that need it, so a new SDK image (a changed *_TAG, or
# a floating tag moving under --pull) rebuilds only from its own section down
# and the system layers above stay cached. Go and Docker come last because
# nothing but their version checks depends on them.

# Rust (rustup, cargo, rustc) is copied from the official rust image above,
# at that image's own CARGO_HOME/RUSTUP_HOME paths.
ENV CARGO_HOME="/usr/local/cargo" RUSTUP_HOME="/usr/local/rustup"
ENV PATH="$CARGO_HOME/bin:${PATH}"
COPY --from=rust-sdk /usr/local/cargo /usr/local/cargo
COPY --from=rust-sdk /usr/local/rustup /usr/local/rustup
COPY image/setup_rust.sh /tmp/setup_rust.sh
RUN chmod +x /tmp/setup_rust.sh && /tmp/setup_rust.sh && rm /tmp/setup_rust.sh

# Go SDK is copied from the latest stable official Go image further down; its
# environment is set here so PATH keeps the same order.
ENV GOROOT="/usr/local/go"
ENV PATH="$GOROOT/bin:$PATH"

# Configure the copied JVM development tools.
COPY --from=java-sdk /opt/java/openjdk /opt/java/openjdk
COPY --from=maven-sdk /usr/share/maven /opt/maven
COPY --from=gradle-sdk /opt/gradle /opt/gradle
COPY image/setup_jvm.sh /tmp/setup_jvm.sh
ARG KOTLIN_VERSION
RUN chmod +x /tmp/setup_jvm.sh && \
    KOTLIN_VERSION="${KOTLIN_VERSION}" /tmp/setup_jvm.sh && \
    rm /tmp/setup_jvm.sh
ENV JAVA_HOME="/opt/java/openjdk"
ENV MAVEN_HOME="/opt/maven"
ENV GRADLE_HOME="/opt/gradle"
ENV KOTLIN_HOME="/opt/kotlin"
ENV PATH="$JAVA_HOME/bin:$MAVEN_HOME/bin:$GRADLE_HOME/bin:$KOTLIN_HOME/bin:$PATH"

RUN java --version && javac --version && mvn --version && gradle --version && kotlinc -version

# Install the default Python runtime with uv. Project environments are created later with uv sync.
# ~/.local/bin holds commands installed later (uv tool, pip --user) in the persisted home.
ENV PATH="/root/.local/bin:$PATH"
COPY --from=uv-sdk /uv /uvx /usr/local/bin/
COPY image/setup_python.sh /tmp/setup_python.sh
ARG PYTHON_VERSION
RUN --mount=type=cache,target=/root/.cache/uv \
    chmod +x /tmp/setup_python.sh && \
    PYTHON_VERSION="${PYTHON_VERSION}" /tmp/setup_python.sh && \
    rm /tmp/setup_python.sh

# Node.js/npm are copied from the official Node image; install TypeScript,
# VS Code extension dev tools, and the Codex/Claude Code/DeepSeek CLIs with npm.
COPY --from=node-sdk /usr/local/bin/node /usr/local/bin/
COPY --from=node-sdk /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY image/setup_typescript.sh /tmp/setup_typescript.sh
ARG CODEX_VERSION
ARG CLAUDE_CODE_VERSION
ARG DEEPSEEK_HARNESS_VERSION
RUN --mount=type=cache,target=/root/.npm \
    chmod +x /tmp/setup_typescript.sh && \
    CODEX_VERSION="${CODEX_VERSION}" \
    CLAUDE_CODE_VERSION="${CLAUDE_CODE_VERSION}" \
    DEEPSEEK_HARNESS_VERSION="${DEEPSEEK_HARNESS_VERSION}" \
    /tmp/setup_typescript.sh && \
    rm /tmp/setup_typescript.sh
RUN node --version && npm --version && tsc --version && codex --version && claude --version && dsh --version

COPY --from=go-sdk /usr/local/go /usr/local/go
RUN go version

# Docker CLI/daemon/buildx/compose are copied from the official docker image
# above (static binaries, no apt install needed) and never started as a
# daemon during the build; `host/devbox create` normally connects the client to
# the host's mounted socket. Newer Docker embeds containerd in dockerd; the
# wildcard also picks up the standalone containerd binary when an older Docker
# tag still ships it.
COPY --from=docker-sdk /usr/local/bin/docker /usr/local/bin/dockerd /usr/local/bin/containerd* /usr/local/bin/ctr /usr/local/bin/runc /usr/local/bin/docker-init /usr/local/bin/docker-proxy /usr/local/bin/
COPY --from=docker-sdk /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
RUN docker --version && dockerd --version && docker buildx version && docker compose version

# Runtime installs and AI state live in /root, which devbox create persists.
# Set the npm prefix and cargo install root only AFTER image tools are installed
# in /usr/local, so an empty home cannot hide them. User-installed commands take
# precedence. The cargo registry cache stays in CARGO_HOME, outside the home.
ENV CLAUDE_CONFIG_DIR="/root/.claude" \
    NPM_CONFIG_PREFIX="/root/.npm-global" \
    NPM_CONFIG_USERCONFIG="/root/.npm/npmrc" \
    CARGO_INSTALL_ROOT="/root/.cargo"
ENV PATH="$CARGO_INSTALL_ROOT/bin:$NPM_CONFIG_PREFIX/bin:$PATH"

# Pass Dockerfile ENV values to SSH sessions (sshd SetEnv), with a profile
# fallback that fills in values a shell is missing.
COPY image/setup_env.sh /tmp/setup_env.sh
RUN chmod +x /tmp/setup_env.sh && /tmp/setup_env.sh && rm /tmp/setup_env.sh

# Keep build metadata last so changing it preserves the toolchain layer cache.
# devbox build passes IMAGE_VERSION from the VERSION file; a plain docker build
# leaves the label empty.
ARG IMAGE_VERSION
LABEL org.opencontainers.image.version="${IMAGE_VERSION}"
# Nothing the image needs lives in /root, so devbox create may mount the whole
# home from the host. Older images kept the default Python there; create asks
# before using an image without this label.
LABEL cuda-devbox.persistent-home="true"
