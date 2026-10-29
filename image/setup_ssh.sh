#!/bin/bash
set -e

set_sshd_option() {
    local key="$1"
    local value="$2"

    if grep -qE "^#?${key}[[:space:]]" /etc/ssh/sshd_config; then
        sed -i -E "s|^#?${key}[[:space:]].*|${key} ${value}|" /etc/ssh/sshd_config
    else
        echo "${key} ${value}" >> /etc/ssh/sshd_config
    fi
}

mkdir -p /run/sshd
set_sshd_option PermitRootLogin yes
set_sshd_option PasswordAuthentication yes
set_sshd_option PubkeyAuthentication yes
set_sshd_option AuthorizedKeysFile .ssh/authorized_keys
set_sshd_option StrictModes no

# Every SSH session, `ssh box command` included, uses the image's locale
# (C.UTF-8 from the Dockerfile ENV; being UTF-8 it handles any language's text).
# The client's LANG/LC_* are not accepted: login shells reset them anyway, so
# accepting them only made non-interactive sessions behave differently.
: "${LANG:?LANG must be set}" "${LC_ALL:?LC_ALL must be set}"
set_sshd_option AcceptEnv "COLORTERM NO_COLOR"
set_sshd_option SetEnv "LANG=${LANG} LC_ALL=${LC_ALL}"
