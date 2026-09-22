#!/bin/bash
set -euo pipefail

for name in \
    CUDA_HOME GOROOT CARGO_HOME RUSTUP_HOME \
    JAVA_HOME MAVEN_HOME GRADLE_HOME KOTLIN_HOME \
    CLAUDE_CONFIG_DIR NPM_CONFIG_PREFIX NPM_CONFIG_USERCONFIG \
    TZ LANG LANGUAGE LC_ALL CC CXX; do
    if [ -z "${!name:-}" ]; then
        echo "ERROR: required environment variable $name is not set" >&2
        exit 1
    fi
done

profile=/etc/profile.d/dev-tools.sh
{
    echo '# Generated from Dockerfile ENV values by setup_env.sh.'
    printf 'export CUDA_HOME=%q\n' "$CUDA_HOME"
    printf 'export GOROOT=%q\n' "$GOROOT"
    printf 'export CARGO_HOME=%q\n' "$CARGO_HOME"
    printf 'export RUSTUP_HOME=%q\n' "$RUSTUP_HOME"
    printf 'export JAVA_HOME=%q\n' "$JAVA_HOME"
    printf 'export MAVEN_HOME=%q\n' "$MAVEN_HOME"
    printf 'export GRADLE_HOME=%q\n' "$GRADLE_HOME"
    printf 'export KOTLIN_HOME=%q\n' "$KOTLIN_HOME"
    printf 'export CLAUDE_CONFIG_DIR=%q\n' "$CLAUDE_CONFIG_DIR"
    printf 'export NPM_CONFIG_PREFIX=%q\n' "$NPM_CONFIG_PREFIX"
    printf 'export NPM_CONFIG_USERCONFIG=%q\n' "$NPM_CONFIG_USERCONFIG"
    printf 'export TZ=%q\n' "$TZ"
    printf 'export LANG=%q\n' "$LANG"
    printf 'export LANGUAGE=%q\n' "$LANGUAGE"
    printf 'export LC_ALL=%q\n' "$LC_ALL"
    printf 'export CC=%q\n' "$CC"
    printf 'export CXX=%q\n' "$CXX"
    printf 'export PATH=%q\n' "$PATH"
    cat <<'PROFILE_TAIL'

# If the container was started with the persistent SSH agent mounted at
# /ssh-agent/docker-agent.sock (`host/devbox create`), point SSH sessions at it.
# docker exec inherits SSH_AUTH_SOCK from the container env, but an SSH login
# does not: sshd (started by systemd) never got that -e variable. Without this,
# git clone over SSH from an SSH login fails with "Permission denied
# (publickey)". Only set it when nothing was forwarded by the SSH client.
if [ -z "${SSH_AUTH_SOCK:-}" ] && [ -S /ssh-agent/docker-agent.sock ]; then
    export SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock
fi
PROFILE_TAIL
} > "$profile"
chmod 0644 "$profile"

bashrc_line='[ -r /etc/profile.d/dev-tools.sh ] && . /etc/profile.d/dev-tools.sh'
grep -qxF "$bashrc_line" /root/.bashrc || echo "$bashrc_line" >> /root/.bashrc
