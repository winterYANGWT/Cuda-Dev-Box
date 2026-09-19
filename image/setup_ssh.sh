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
