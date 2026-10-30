#!/bin/bash
set -euo pipefail

# Dockerfile ENV values that SSH sessions need. docker exec inherits the
# container env, but sshd builds each session's environment from scratch.
# A fixed list rather than the whole build environment, which also holds
# build ARGs such as CODEX_VERSION.
names=(
    PATH CUDA_HOME LIBRARY_PATH LD_LIBRARY_PATH
    GOROOT CARGO_HOME RUSTUP_HOME
    JAVA_HOME MAVEN_HOME GRADLE_HOME KOTLIN_HOME
    CLAUDE_CONFIG_DIR NPM_CONFIG_PREFIX NPM_CONFIG_USERCONFIG CARGO_INSTALL_ROOT
    TZ LANG LANGUAGE LC_ALL CC CXX
)
for name in "${names[@]}"; do
    if [ -z "${!name:-}" ]; then
        echo "ERROR: required environment variable $name is not set" >&2
        exit 1
    fi
    # sshd splits SetEnv on whitespace and treats quotes and backslashes specially.
    case "${!name}" in
        *[[:space:]\"\'\\]*)
            echo "ERROR: $name contains whitespace, a quote or a backslash: ${!name}" >&2
            exit 1
            ;;
    esac
done

# sshd sets these for every SSH session, `ssh box command` and VS Code's remote
# server included, without relying on shell startup files. sshd honours only
# the first SetEnv line, so the locale is set here too, not in setup_ssh.sh.
{
    echo '# Generated from Dockerfile ENV values by setup_env.sh.'
    printf 'SetEnv'
    for name in "${names[@]}"; do
        printf ' %s=%s' "$name" "${!name}"
    done
    echo
} > /etc/ssh/sshd_config.d/devbox-env.conf

# Fallback for shells started without that environment (env -i, su -). It only
# fills in what is missing: overwriting PATH here used to drop directories that
# whatever started the shell had added, such as a venv or VS Code's `code`.
profile=/etc/profile.d/dev-tools.sh
{
    echo '# Generated from Dockerfile ENV values by setup_env.sh.'
    echo '# Only fills in missing values; never overrides what the parent process set.'
    for name in "${names[@]}"; do
        [ "$name" = PATH ] && continue
        printf '[ -n "${%s+set}" ] || export %s=%q\n' "$name" "$name" "${!name}"
    done
    # Prepend missing PATH directories, last first, so they keep the image's order.
    IFS=: read -ra dirs <<< "$PATH"
    for ((i = ${#dirs[@]} - 1; i >= 0; i--)); do
        printf 'case ":${PATH:-}:" in *:%q:*) ;; *) PATH=%q${PATH:+:$PATH} ;; esac\n' "${dirs[i]}" "${dirs[i]}"
    done
    echo 'export PATH'
    cat <<'PROFILE_TAIL'

# If the container was started with the persistent SSH agent mounted at
# /ssh-agent/docker-agent.sock (`host/devbox create`), point SSH sessions at it.
# docker exec inherits SSH_AUTH_SOCK from the container env (create's -e), but
# sshd does not pass it on to sessions. Without this, git clone over SSH from
# an SSH login fails with "Permission denied (publickey)". Only set it when
# nothing was forwarded by the SSH client.
if [ -z "${SSH_AUTH_SOCK:-}" ] && [ -S /ssh-agent/docker-agent.sock ]; then
    export SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock
fi
PROFILE_TAIL
} > "$profile"
chmod 0644 "$profile"

# Non-login interactive shells (VS Code terminals) skip /etc/profile.d; they
# need the SSH_AUTH_SOCK fallback above. The system bashrc, not /root/.bashrc,
# because devbox create mounts the home from the host.
bashrc_line='[ -r /etc/profile.d/dev-tools.sh ] && . /etc/profile.d/dev-tools.sh'
grep -qxF "$bashrc_line" /etc/bash.bashrc || echo "$bashrc_line" >> /etc/bash.bashrc
