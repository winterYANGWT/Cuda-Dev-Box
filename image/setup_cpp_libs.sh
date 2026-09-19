#!/bin/bash
set -e

apt-get update
apt-get install -y --no-install-recommends \
    alsa-base alsa-utils alsa-source libasound2-dev \
    v4l-utils libv4l-dev \
    bpftrace libbpf-dev liburing-dev numactl libnuma-dev libmimalloc-dev \
    libspdlog-dev \
    libboost-all-dev \
    libgtest-dev libgmock-dev
