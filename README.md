# Cuda-Dev-Box

A Docker development container based on NVIDIA CUDA 13.3.1 and Ubuntu 26.04, with a full toolchain for C/C++, CUDA, Python, Rust, Go, Java/Kotlin, TypeScript, and AI coding CLIs pre-installed. Good fit for AI/ML development, systems programming, JVM development, and VS Code extension development.

## Contents

- [Quick start](#quick-start)
- [Image features](#image-features)
- [Building the image](#building-the-image)
- [Build arguments](#build-arguments)
- [Creating a container](#creating-a-container)
- [Connecting to the container](#connecting-to-the-container)
- [Repository layout](#repository-layout)
- [What's installed](#whats-installed)
- [Customization](#customization)

---

## Quick start

```bash
# 1. Build the image (tagged with the version in VERSION and with latest)
host/devbox build

# 2. Create a container interactively (recommended)
host/devbox create

# 3. SSH in
ssh root@localhost -p <the host port you chose>
```

Every host-side task goes through one command, `host/devbox` (`host/devbox --help` lists them):

| Command | What it does |
| --- | --- |
| `host/devbox build [--pull] [--no-cache] [--build-arg NAME=VALUE]` | Build the image, tagged with the version in `VERSION` and with `latest` |
| `host/devbox create` | Interactively create a container (every setting is a prompt) |
| `host/devbox memory [--profile 16] [--apply]` | Preview, or with `sudo` and `--apply` install, host HugeTLB/CMA/NUMA settings for the next boot |
| `host/devbox agent install` | One-time: run a persistent host SSH agent for containers |
| `host/devbox agent refresh <container-name>` | Legacy containers only: restart the per-container SSH agent proxy |

---

## Image features

| Feature | Details |
| --- | --- |
| Base image | NVIDIA CUDA 13.3.1 cuDNN devel / Ubuntu 26.04 (Resolute Raccoon) |
| Package sources | Official upstreams only (apt, rustup, npm, Maven/Gradle); no mirrors configured |
| Compiler | Clang by default; GCC also available |
| SSH | Configured; root login by password and/or key. Every SSH session, `ssh <container> <command>` and VS Code's remote server included, gets the same tool environment (`PATH`, `CUDA_HOME`, `JAVA_HOME`, ...) as `docker exec`, from sshd's `SetEnv` |
| GPU | CUDA 13.3.1 + cuDNN; select all GPUs (default), individual indices, or ranges when Docker GPU support is available |
| Timezone | Asia/Tokyo |
| Locale | `C.UTF-8` everywhere: `docker exec`, SSH logins, and `ssh <container> <command>` alike. It is UTF-8, so Chinese, Japanese, and other text work; dates, sorting, and messages follow English/POSIX conventions. The SSH client's `LANG`/`LC_*` are not accepted. `en_US.UTF-8` is also generated for programs that ask for it by name |
| Languages | Python / Rust / Go / Java / Kotlin / TypeScript / C/C++ |
| AI CLIs | Codex CLI / Claude Code CLI / DeepSeek Harness (`dsh`), from their official npm packages |
| Docker | Docker Engine, CLI, Buildx, Compose v2 — connects to the host socket by default |
| Memory profiles | 8 / 16 / 32 / 64 GiB, auto-selects container memory/swap limits |

---

## Building the image

```bash
host/devbox build
```

It builds the repository's `Dockerfile` and tags the image twice: `cuda-devbox:<version>`, with the version read from the `VERSION` file at the repository root, and `cuda-devbox:latest`. The same version goes into the image's `org.opencontainers.image.version` label. A version is `YYYYMM`, or `YYYYMM-N` for another build in the same month (`202610`, `202610-2`, `202610-3`, ...). Set `VERSION` before each build: if `cuda-devbox:<version>` already exists, the command stops and suggests the next version, rather than moving the tag off the older image.

```bash
echo 202611 > VERSION
host/devbox build

docker images cuda-devbox                  # 202610, 202610-2, 202611, latest
docker image inspect cuda-devbox:latest --format '{{index .Config.Labels "org.opencontainers.image.version"}}'
```

Changing the version never invalidates the layer cache. Older images stay around until you remove them (`docker rmi cuda-devbox:202608`).

Every other build argument's default value (base image tags, Python/Kotlin/Codex/Claude Code/DeepSeek Harness versions) lives in the `Dockerfile`. Override one for a single build with `--build-arg NAME=VALUE` (repeatable); `--pull` and `--no-cache` are passed on to `docker build`. A plain `docker build -t cuda-devbox:latest .` still works, but adds no version tag and leaves the version label empty.

### Build arguments

The image declares SSH port `22/tcp` and dev-service ports `8000–8099/tcp` (100 ports). Building the image doesn't start a container or open any host port; `devbox create` asks for a host start port (default `8000`) and publishes the 100 host ports from there onto container `8000-8099`: start `62000` gives `-p 62000-62099:8000-8099/tcp`. SSH uses a separate host port, `2222` by default.

The build is split into layers. uv, Go, JDK, Maven, Gradle, Node.js, Docker, Rust, and the outer CUDA/Ubuntu base image all receive their tag through `ARG`s with defaults at the top of the Dockerfile (see [Overriding base image versions](#overriding-base-image-versions)). The system packages come first; each toolchain is copied in right before the steps that use it, and each version argument is declared right before the step that uses it. Changing one tag or version, or a floating tag moving under `--pull`, therefore rebuilds only from that step down: pinning `CODEX_VERSION` reruns the npm layer and the version checks after it, not apt, locales, or the other toolchains. Everything else is configured by its own setup script in `image/`:

| File | Purpose |
| --- | --- |
| `Dockerfile` | Main build file; copies uv/Go/JDK/Maven/Gradle/Node.js/Docker/Rust from their official images |
| `image/setup_cpp_libs.sh` | C/C++ system libraries (Boost, spdlog, GoogleTest, etc.) |
| `image/setup_env.sh` | Passes Docker `ENV` values to SSH sessions through sshd's `SetEnv`, plus a shell fallback (`/etc/profile.d/dev-tools.sh`) that only fills in values a shell is missing, so a venv's or VS Code's `PATH` entries are kept |
| `image/setup_ssh.sh` | SSH daemon configuration |
| `image/setup_rust.sh` | Adds rustfmt/clippy to the Rust toolchain copied from the official image |
| `image/setup_jvm.sh` | Kotlin install and JVM tool entrypoints |
| `image/setup_python.sh` | Installs Python 3.14 via uv; no project dependencies pre-installed |
| `image/setup_typescript.sh` | npm globals: TypeScript, VS Code extension dev tools, and the Codex/Claude Code/DeepSeek Harness CLIs; Node.js itself is copied from the official image in the Dockerfile |

Codex, Claude Code, and DeepSeek Harness default to whatever is latest at build time; pin versions with `--build-arg CODEX_VERSION=<version>`, `--build-arg CLAUDE_CODE_VERSION=<version-or-stable>`, or `--build-arg DEEPSEEK_HARNESS_VERSION=<version>` (for example, `0.2.1-rc.2`). Docker reuses layer cache; to force a refresh of floating versions, run `host/devbox build --pull --no-cache`.

### Image size and build cache

The image keeps the full CUDA Toolkit, cuDNN, and every language toolchain, but doesn't pre-install Python project dependencies like PyTorch. The setup scripts use `--no-install-recommends`; APT, uv, and npm download caches are reused across builds via BuildKit cache mounts, but never land in the final image layers.

Docker's build cache isn't part of the image and can be pruned on the host as needed:

```bash
# Drop cache entries unused for a week
docker builder prune --filter until=168h

# Drop everything once you're sure you don't need old cache
docker builder prune
```

### Overriding base image versions

uv/Go/JDK/Maven/Gradle/Node.js/Docker/Rust and the outer CUDA base image all receive their tag through `ARG`s at the top of the Dockerfile — **repository and tag are two separate variables**. Edit the defaults there directly if you're changing them for good, or override individual values with `--build-arg` for one build:

| Image | REPO variable / default | TAG variable / default |
| --- | --- | --- |
| uv | `UV_REPO` = `astral/uv` | `UV_TAG` = `0.12.0` |
| Go | `GO_REPO` = `golang` | `GO_TAG` = `1-trixie` |
| JDK | `JAVA_REPO` = `eclipse-temurin` | `JAVA_TAG` = `25-jdk-resolute` |
| Maven | `MAVEN_REPO` = `maven` | `MAVEN_TAG` = `3-eclipse-temurin-25-noble` |
| Gradle | `GRADLE_REPO` = `gradle` | `GRADLE_TAG` = `9-jdk25-resolute` |
| Node.js | `NODE_REPO` = `node` | `NODE_TAG` = `lts-trixie` |
| Docker | `DOCKER_REPO` = `docker` | `DOCKER_TAG` = `29` (CLI/daemon/buildx/compose, all static binaries, copied directly) |
| Rust | `RUST_REPO` = `rust` | `RUST_TAG` = `1-slim-trixie` (only `/usr/local/cargo` and `/usr/local/rustup` are copied) |
| CUDA/Ubuntu | `BASE_REPO` = `nvidia/cuda` | `BASE_TAG` = `13.3.1-cudnn-devel-ubuntu26.04` |

Overriding a `*_REPO` alone points at a different registry/mirror (e.g. a private proxy) without touching the tag; overriding a `*_TAG` alone changes the version without touching the repo. Nothing is cross-referenced between images or between the two halves of a pair — bumping the JDK version means editing `JAVA_TAG`, `MAVEN_TAG` (which embeds `eclipse-temurin-25`), and `GRADLE_TAG` (which embeds `jdk25`) separately; changing one won't move the others.

`java-sdk`/`gradle-sdk` use `resolute` (Ubuntu 26.04), matching the outer image's own OS version; `maven-sdk` stays on `noble` (24.04) — the official `maven` image has not published any `resolute` tag (unlike `eclipse-temurin` and `gradle`, checked against Docker Hub). None of these three official images offer a Debian-family (`trixie`/`bookworm`) variant at all — only Ubuntu (`noble`/`jammy`/`resolute`), Alpine, and UBI.

For example, to pin a Go minor version, edit `GO_TAG`'s default in the Dockerfile, or override it for one build:

```bash
host/devbox build --build-arg GO_TAG=1.26-trixie
```

Setting `BASE_TAG` to `13.3.1-cudnn-runtime-ubuntu26.04` gets a smaller image without `nvcc`/CUDA headers — but that's not a drop-in swap for the C/C++ toolchain this Dockerfile installs. The image is built around a `devel` CUDA base; confirm you don't need to compile CUDA code inside the container before switching to `runtime`.

Floating tags (like `1-trixie`, `lts-trixie`) don't automatically track the newest image on their own; `host/devbox build --pull` forces a re-pull of whatever the tag currently points at.

---

## Creating a container

Use `host/devbox create` to create a container interactively:

```bash
host/devbox create
```

It takes no options; every setting is asked for. It needs a native Linux host (WSL isn't supported) with at least 6.625 GiB of usable RAM for the smallest memory profile; otherwise it exits before the first question. It also runs the host's `ssh-keygen` (from the OpenSSH client) to make the container's [SSH host keys](#ssh-host-keys). For example, for SSH port `61000`, dev ports `62000–62099`, and key-only login, answer the dev-port start with `62000`, the SSH port with `61000`, and the password-login question with `y`.

Press Enter at the image-tag prompt to take `cuda-devbox:latest` (or the newest local versioned build, such as `cuda-devbox:202610-2`, if `latest` is absent). You can also type another tag such as `202608`, or a full image name such as `other/image:1.0`. For a plain container, answer `n` to the SSH agent, sandbox compatibility, privileged mode, and kernel passthrough prompts. The dev ports are always 100 consecutive host ports starting at the start port, mapped onto container `8000-8099`: start `62000` gives host `62000-62099` (host `62080` reaches a service on container port `8080`). The SSH port can't fall inside that host range. Key-only login disables password login and requires a public key. The GPU question is only asked when Docker can actually use the GPU; otherwise the script warns and leaves the GPU out. The container's `authorized_keys` gets the host's own `~/.ssh/*.pub` plus every key in the host's `~/.ssh/authorized_keys` (so a laptop that can SSH into the host can SSH into the container too; lines with options such as `from=` are skipped). The host is the only source: every create rewrites the file, so recreating a container drops keys that are no longer on the host and keys added inside the container. Private keys in the container's `.ssh` are left alone. The `~/.gitconfig` mount is skipped if the host doesn't have one.

The GPU selection defaults to `all`; enter `0,2` for individual host GPU indices, `0-3` for an inclusive range, or `0,2-4` to combine them. Repeated indices are removed, and malformed inputs are re-asked. For example, `0,2-3` becomes Docker's `--gpus '"device=0,2,3"'`. Use `nvidia-smi -L` on the host to see its GPU indices.

The script asks, in order:

```
Image tag for cuda-devbox (latest, 202609, 202608; default latest; or a full image name):   # Enter = latest
Enter the container name:           # e.g. my-dev; re-asked if invalid or already in use
Host start port for the 100 dev ports mapped to container 8000-8099 (default 8000):   # 62000 -> 62000-62099
Enter the host port to map to container's port 22 (default 2222):    # must not overlap the dev-port range
Disable password login and allow SSH public keys only? (y/N):
Host RAM profile (auto/8/16/32/64, default auto):     # Enter = the largest profile that fits usable memory
NVIDIA GPUs to use (all, 0,2, or 0-3; default all):   # Enter = all; only asked when Docker can use the GPU
Forward host SSH agent into the container? (y/N):
Enable Cursor/Claude terminal sandbox compatibility? (adds SYS_ADMIN/NET_ADMIN and unconfined seccomp/AppArmor) (y/N):
Enable privileged mode and host Docker socket access? (Y/n):  # for all GPUs; subsets ask only for Docker socket access
Enable host kernel development passthrough for bpftrace/HugeTLB/CMA/NUMA? (Y/n):  # on by default
```

The container name follows Docker's naming rule (letters, digits, `_`, `.`, `-`, starting with a letter or digit) and is limited to 63 characters because it is also the hostname. It names the state directory `~/docker_mount/<container-name>`, so an invalid name (empty, `..`, containing `/`) or the name of an existing container is asked again before anything on the host changes.

A full image name typed at the image-tag prompt can only have one tag-separating colon. `dev:ubuntu:26.04` is invalid; to express namespace/repository/tag, use `dev/ubuntu:26.04`, or a single repo name like `dev-ubuntu:26.04`.

### Container launch parameters

The script uses `docker create`, `docker start`, then sets the SSH password (key-only login skips that step) and checks the sshd config; if creation or initialization fails, or you press Ctrl-C during initialization, only the newly created container is cleaned up. The successful default-port-range path is roughly equivalent to:

```bash
docker run -d \
    --name <container-name> \
    --hostname <container-name> \
    -p <host-port>:22 \
    -p 8000-8099:8000-8099/tcp \
    --memory 10g \
    --memory-swap 12g \
    -v ~/docker_mount/<container-name>/data:/root/data \
    -v ~/docker_mount/<container-name>/.vscode-server:/root/.vscode-server \
    -v ~/.gitconfig:/root/.gitconfig:ro \
    -v ~/docker_mount/<container-name>/.ssh:/root/.ssh \
    -v ~/docker_mount/<container-name>/ssh-host-keys:/etc/ssh/host-keys:ro \
    -v ~/docker_mount/<container-name>/.codex:/root/.codex \
    -v ~/docker_mount/<container-name>/.claude:/root/.claude \
    -v ~/docker_mount/<container-name>/.agents:/root/.agents \
    -v ~/docker_mount/<container-name>/.npm:/root/.npm \
    -v ~/docker_mount/<container-name>/.npm-global:/root/.npm-global \
    [--gpus all] \
    --entrypoint /usr/sbin/sshd \
    --restart unless-stopped \
    <image-name> -D -e \
    -o HostKey=/etc/ssh/host-keys/ssh_host_ed25519_key \
    -o HostKey=/etc/ssh/host-keys/ssh_host_ecdsa_key \
    -o HostKey=/etc/ssh/host-keys/ssh_host_rsa_key
```

The memory values above are the 16 GiB profile; other profiles are in the table below. With `all` GPUs, the script offers `--privileged` plus the host Docker socket by default. With specific GPU indices, it uses nonprivileged mode and asks only `Enable host Docker socket access? (Y/n):`, because privileged mode exposes all host devices and defeats GPU selection. Host kernel development passthrough is also enabled by default. Kernel dev passthrough appends:

```bash
    --ipc=host \
    --pid=host \
    --ulimit memlock=-1:-1 \
    -v /dev/hugepages:/dev/hugepages:rw \
    -v /sys/kernel/tracing:/sys/kernel/tracing:rw \
    -v /sys/kernel/debug:/sys/kernel/debug:rw \
    -v /sys/fs/bpf:/sys/fs/bpf:rw \
    --device /dev/dma_heap/<heap present on the host> \
    --device /dev/udmabuf
```

If the host is missing `/dev/hugepages`, `/dev/dma_heap`, `/dev/udmabuf`, or the tracing/debug/bpf filesystems, the script prints a warning and skips the corresponding mount or device rather than aborting. Docker can only pass through kernel capabilities the host already has enabled; it can't create HugeTLB, CMA, or new NUMA topology inside the container.

`/dev/shm` depends on this choice. With kernel dev passthrough, `--ipc=host` gives the container the host's `/dev/shm` (by default half the host RAM), shared with the host and every other such container; files left there outlive the container until deleted or the host reboots. Without it, Docker's private `/dev/shm` defaults to 64 MiB, too small for PyTorch `DataLoader` workers and NCCL, so the script appends `--shm-size` at half the profile's container RAM limit: 2048m, 5120m, 11264m, or 24576m. Either way `/dev/shm` is tmpfs: its size is a cap, not a reservation, and what a container writes there counts against its `--memory` limit. A private `/dev/shm` is emptied when the container stops.

In nonprivileged mode (specific GPU indices, or answering `n` at the privileged-mode prompt), the script appends capabilities based on what else you enabled. Cursor/Claude sandbox compatibility adds `SYS_ADMIN`, `NET_ADMIN`, `seccomp=unconfined`, `apparmor=unconfined`; kernel dev passthrough adds `IPC_LOCK`, `SYS_RESOURCE`, `SYS_ADMIN`, `SYS_PTRACE`, `BPF`, `PERFMON`, and `seccomp=unconfined`. With `--privileged`, none of these `--cap-add`/unconfined security opts get appended again since they'd be redundant.

If SSH agent forwarding is enabled, the script installs the persistent host agent if needed, starts it if stopped, and waits for its socket to become ready. If the agent has no keys, it runs `ssh-add` on the host with every private key in `~/.ssh` that has a matching `.pub`, custom file names included: Ed25519 first, then ECDSA, then RSA, so the container offers the Ed25519 key first to services such as GitHub. Other key types, such as DSA, are skipped. Encrypted keys prompt for their passphrase in the terminal. With no such key, the script warns and continues with an empty agent. An agent that already has keys is reused. Installation or key-loading failure stops creation before Docker creates a container. The script then appends:

```bash
    -v ~/.ssh-agent:/ssh-agent \
    -e SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock
```

No separate setup command is required before `devbox create`. To set up the agent in advance, or to load a key that has no `.pub` next to it:

```bash
host/devbox agent install
SSH_AUTH_SOCK=~/.ssh-agent/docker-agent.sock ssh-add ~/.ssh/id_ed25519
```

This maintains `~/.ssh-agent/docker-agent.sock` via a user systemd service. Docker only mounts the fixed directory `~/.ssh-agent`, never a login-session temp path like the host's `/tmp/ssh-*/agent.*`, so containers can restart cleanly after a host reboot. If you want the user systemd service to also start on boot with nobody logged in:

```bash
sudo loginctl enable-linger "$USER"
```

Run creation as your normal host user with access to Docker and a working user systemd session. An empty agent needs a local private key for `ssh-add`; a forwarded login-session agent's keys cannot be copied into the persistent agent. Keys live in agent memory and need to be added again after a host reboot. If the private key lives only on another machine such as your laptop, connect to the container with `ssh -A` instead: SSH sessions then use the forwarded agent and don't need the persistent one.

This only lets processes inside the container request signatures from the agent; it never copies the host's private key into the container. Containers restart cleanly after a host reboot; if a container can't find an agent key when reaching a private repo, first try re-adding the key to the persistent agent:

```bash
SSH_AUTH_SOCK=~/.ssh-agent/docker-agent.sock ssh-add ~/.ssh/id_ed25519
```

Older containers may use a per-container proxy at `~/docker_mount/<container-name>/ssh-agent/agent.sock`. To reconnect one to the current shell's `SSH_AUTH_SOCK`, run:

```bash
host/devbox agent refresh <container-name>
```

Containers from the current `devbox create` have no such proxy; for them the command exits with an error that points to the persistent agent.

With host Docker access enabled (the default), the script also appends:

```bash
    --privileged \
    -v /var/run/docker.sock:/var/run/docker.sock
```

### Host capability prerequisites

GPU passthrough needs the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) on the host in addition to the driver; a working `nvidia-smi` alone isn't enough. Without it `docker start` fails with `could not select device driver "" with capabilities: [[gpu]]`, so `devbox create` checks first and creates the container without a GPU (with a warning) instead. Install it once:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
    | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all ubuntu nvidia-smi    # should list the GPU
```

Then recreate any container that was created without the GPU (`docker rm -f <name>`, `host/devbox create`).

The `docker`/`dockerd`/`buildx`/`compose` binaries inside the image are copied from the official `docker` image (static binaries — see `DOCKER_REPO`/`DOCKER_TAG` under [Overriding base image versions](#overriding-base-image-versions)), not apt-installed. By default the `docker` command operates the host daemon; any other containers you create from inside are host containers too, and bind-mount paths resolve on the host. This isn't a standalone DinD daemon — the container's own entrypoint only starts SSH.

`bpftrace`, `libbpf-dev`, `liburing-dev`, `numactl`, `libnuma-dev` inside the container only give you the development/calling interfaces; HugeTLB, CMA, tracefs, bpffs, and NUMA topology must be provided by the host kernel.

Check what's actually in effect on the host:

```bash
cat /proc/cmdline
sysctl vm.nr_hugepages kernel.io_uring_disabled
grep -E 'HugePages|Hugetlb|CmaTotal|CmaFree' /proc/meminfo
findmnt /dev/hugepages
ls -ld /dev/hugepages /sys/kernel/tracing /sys/kernel/debug /sys/fs/bpf /dev/dma_heap 2>/dev/null
ls -l /dev/dma_heap 2>/dev/null
```

### 8 / 16 / 32 / 64 GiB memory profiles

`PROFILES` in `host/devbox_lib/memory.py` is the single source for both container limits and host reservation parameters:

| Physical RAM profile | Container RAM limit | Container swap limit | Docker `--memory-swap` total | HugeTLB reservation (2 MiB pages) | CMA |
| --- | --- | --- | --- | --- | --- |
| 8 GiB | 4 GiB | 2 GiB | 6 GiB | 256 pages / 512 MiB | 128 MiB |
| 16 GiB | 10 GiB | 2 GiB | 12 GiB | 512 pages / 1 GiB | 256 MiB |
| 32 GiB | 22 GiB | 4 GiB | 26 GiB | 1024 pages / 2 GiB | 512 MiB |
| 64 GiB | 48 GiB | 4 GiB | 52 GiB | 2048 pages / 4 GiB | 1024 MiB |

These defaults leave headroom for the host and the hugepage reservation; they're not tuned to be optimal for every workload. A profile fits when the host's usable RAM (`MemTotal`) covers its container RAM limit, both reservations, and 2 GiB for the host: 6.625 GiB for 8, 13.25 GiB for 16, 26.5 GiB for 32, and 55 GiB for 64. `auto` takes the largest profile that fits, so firmware or an integrated-GPU carve-out can move a host down a profile, and a profile that doesn't fit is refused. After a RAM upgrade, reapply the host profile and recreate containers — existing containers don't change their limits automatically.

`--memory-swap` is the RAM+swap total. There's no `--memory-swappiness` here (unsupported on cgroup v2); instead the host's `vm.swappiness` is set to `10`. The host's existing ~4 GiB `/swap.img` is left alone, never rebuilt or cleared; container parameters don't add host swap out of nothing either. Multiple containers share whatever memory, swap, and hugepage pool is actually available — the table isn't a per-container guarantee. See [Docker memory limits](https://docs.docker.com/engine/containers/resource_constraints/).

Run on the host (preview-only by default):

```bash
host/devbox memory --profile 16

# Write the config and update GRUB, effective after the next reboot; the script never reboots or starts containers itself
sudo host/devbox memory --profile 16 --apply

# After a RAM upgrade, use --profile 32, --profile 64, or --profile auto
```

The script manages two dedicated files, preserving every other GRUB parameter (root, encryption, crashkernel, etc.):

- `/etc/default/grub.d/99-ubuntubase-memory.cfg`: CMA, the 2 MiB HugeTLB reservation, and `transparent_hugepage=madvise`.
- `/etc/sysctl.d/90-ubuntubase-memory.conf`: `vm.swappiness=10`, hugepage count, NUMA balancing.

A single-NUMA-node machine gets `kernel.numa_balancing=0`; detecting multiple real nodes sets it to `1`, without pinning cores or restricting nodes. Adding RAM doesn't guarantee more NUMA nodes. The script won't add a `numa=fake` parameter on a kernel without `CONFIG_NUMA_EMU`. See the [kernel NUMA balancing docs](https://docs.kernel.org/admin-guide/sysctl/kernel.html#numa-balancing).

The 1/2/4 GiB of HugeTLB reservation comes out of ordinary memory and must be explicitly used by a program to matter; `memlock=-1:-1` only lifts the lock-page limit, it doesn't lock all memory by itself. CMA is for contiguous physical allocation; a successful reservation doesn't guarantee the kernel exposes a `/dev/dma_heap` device — the launch script still only passes through devices that actually exist. THP stays `madvise`, requested explicitly by the application. See the [kernel boot parameters](https://docs.kernel.org/admin-guide/kernel-parameters.html) and [HugeTLB docs](https://docs.kernel.org/admin-guide/mm/hugetlbpage.html).

After a reboot, confirm the 16 GiB profile actually took effect:

```bash
cat /proc/cmdline                     # cma=256M hugepagesz=2M hugepages=512
grep -E 'HugePages|Hugepagesize|CmaTotal|CmaFree' /proc/meminfo
sysctl vm.nr_hugepages vm.swappiness kernel.numa_balancing
cat /sys/kernel/mm/transparent_hugepage/enabled
cat /sys/devices/system/node/online
swapon --show
findmnt /dev/hugepages
```

Expect `HugePages_Total=512`, `Hugepagesize=2048 kB`, `CmaTotal=262144 kB`, swappiness `10`, single-node NUMA balancing `0`. Always check the actual readings — generating the config isn't the same as the reservation actually succeeding. Ubuntu's `dev-hugepages.mount` usually handles mounting `/dev/hugepages`; if it isn't mounted, run `sudo systemctl start dev-hugepages.mount`.

Re-applying replaces both managed files; the previous version is backed up as `.previous` alongside the same path, and the original `/boot/grub/grub.cfg` is backed up to `/boot/grub/grub.cfg.ubuntubase-previous`. A failed update restores the config, generated GRUB file, and file permissions; if that restoration also fails, the script errors explicitly. To undo the very first apply, delete the two managed files, run `sudo update-grub`, and reboot; if the files already existed before you applied, restore their `.previous` backups instead, then update GRUB and reboot. Other custom sysctl files may also affect the final numbers.

Existing containers can't gain new port mappings, mounts, devices, or security options via `docker start`; you need to remove and recreate them. Data under `~/docker_mount/<container-name>` is preserved. When upgrading from an older version that did not mount AI/npm state, follow [Migrating an existing container](#migrating-an-existing-container) **before removing it**:

```bash
docker rm -f <container-name>
host/devbox create
```

### Mount directories

| Host path | Container path | Purpose |
| --- | --- | --- |
| `~/docker_mount/<container-name>/data` | `/root/data` | Persistent data storage |
| `~/docker_mount/<container-name>/.vscode-server` | `/root/.vscode-server` | VS Code Server cache (speeds up remote reconnects) |
| `~/.gitconfig` | `/root/.gitconfig` (ro) | Shared Git config |
| `~/docker_mount/<container-name>/.ssh` | `/root/.ssh` | `authorized_keys` (generated from host public keys) |
| `~/docker_mount/<container-name>/ssh-host-keys` | `/etc/ssh/host-keys` (ro) | The container's own [SSH host keys](#ssh-host-keys) |
| `~/docker_mount/<container-name>/.codex` | `/root/.codex` | Codex configuration, file-based login, sessions, and plugins |
| `~/docker_mount/<container-name>/.claude` | `/root/.claude` | Claude configuration, login, sessions, memory, and plugins |
| `~/docker_mount/<container-name>/.dsh` | `/root/.dsh` | DeepSeek Harness credentials, configuration, profiles, and sessions |
| `~/docker_mount/<container-name>/.agents` | `/root/.agents` | Shared user skills, including `~/.agents/skills` |
| `~/docker_mount/<container-name>/.npm` | `/root/.npm` | npm download cache and user config (`npmrc`) |
| `~/docker_mount/<container-name>/.npm-global` | `/root/.npm-global` | Runtime npm global packages and their command links |

These are **bind mounts** into host directories, not Docker-managed named volumes. `docker rm` removes the container's writable layer but leaves these directories intact. Recreate using the **same container name** to reuse them; another name gets separate state. New tool-state directories are created with mode `0700`. Keep the host directories when rebuilding or cleaning up containers.

### AI state and npm packages

The image sets `CLAUDE_CONFIG_DIR=/root/.claude`, so Claude's global `.claude.json` is also stored inside the mounted directory, alongside settings and credentials. This avoids a separate single-file mount that can interfere with atomic file replacement. Codex uses its default `/root/.codex`; file-backed credentials persist there (an OS keyring or ephemeral login is not saved by this mount). See [OpenAI's credential storage documentation](https://developers.openai.com/codex/auth/#credential-storage) and [Claude's configuration directory](https://code.claude.com/docs/en/env-vars).

For npm, there are three separate kinds of data:

- **Image tools**: Node.js, npm, TypeScript, Codex, Claude, and DeepSeek Harness stay in `/usr/local`. They remain available even when the new mounts are empty.
- **Runtime global installs**: `npm install -g <package>` installs into `/root/.npm-global`, including `lib/node_modules` and `bin`. Its `bin` comes first in `PATH`, so a persisted CLI upgrade overrides the image's bundled version. This works in `docker exec` and SSH login/interactive shells.
- **Project installs**: `npm install` creates `node_modules` within the project. Keep projects under `/root/data` (or another mounted directory) to preserve both source and dependencies. The npm cache alone does not preserve installed packages.

`NPM_CONFIG_USERCONFIG=/root/.npm/npmrc` also persists settings written by `npm config set` and `npm login`. The image exports these variables for SSH sessions as well as Docker commands. A rebuilt image is required to get this environment setup; mounts alone on an older image will not redirect npm global installs. If a new image changes Node.js major version or CPU architecture, reinstall packages containing native addons in the new environment.

### Migrating an existing container

For a container created before these mounts were added, save its state **while it still exists**. Exit Codex/Claude/DeepSeek Harness and stop package installs first so files are not changing during the copy. The commands below assume the old container is running, use the default old paths, and keep a separate backup. Set `box` to its actual name:

```bash
set -e
box=my-dev
state_dir="$HOME/docker_mount/$box"
backup_dir=$(mktemp -d "$HOME/devbox-backup.XXXXXX")

for directory in .codex .claude .dsh .agents .npm .npm-global; do
    if docker exec "$box" test -d "/root/$directory"; then
        docker cp "$box:/root/$directory" "$backup_dir/$directory"
    fi
done

# Move the two legacy standalone config files into the new mounted directories.
if [ ! -e "$backup_dir/.claude/.claude.json" ] && docker exec "$box" test -f /root/.claude.json; then
    mkdir -p "$backup_dir/.claude"
    docker cp "$box:/root/.claude.json" "$backup_dir/.claude/.claude.json"
fi
if [ ! -e "$backup_dir/.npm/npmrc" ] && docker exec "$box" test -f /root/.npmrc; then
    mkdir -p "$backup_dir/.npm"
    docker cp "$box:/root/.npmrc" "$backup_dir/.npm/npmrc"
fi

# Record old global installs so extra packages can be reinstalled after recreation.
docker exec "$box" npm ls -g --depth=0 > "$backup_dir/npm-global-packages.txt"
mkdir -p "$state_dir"
cp -an "$backup_dir/." "$state_dir/"
printf 'Backup saved to %s\n' "$backup_dir"
```

Existing destination files are kept by `cp -an`; if both locations already contain state, review the backup before replacing any destination files. Rebuild the image with `host/devbox build`, then remove and recreate the container with the same name. Old packages under `/usr/local` were not persisted: use the saved package list to reinstall any extras with `npm install -g <package>@<version>` in the new container. Subsequent global installs and upgrades persist automatically. Files already lost by deleting an unmounted container cannot be recovered by adding a mount later.

---

## Connecting to the container

### SSH

```bash
# Password login (set by the script after startup; the random password is shown once)
ssh root@localhost -p <host-port>

# Key login (the script copies the host's ~/.ssh/*.pub and ~/.ssh/authorized_keys public keys, never a private key)
ssh root@localhost -p <host-port>
```

### SSH host keys

The image ships without SSH host keys. The first `devbox create` for a container name runs the host's `ssh-keygen` to make Ed25519, ECDSA, and RSA host keys in `~/docker_mount/<container-name>/ssh-host-keys`, mounts them read-only, and points sshd at them alone. Each container gets its own keys, and recreating it with the same name, even from a newly built image, keeps them, so SSH clients don't see `REMOTE HOST IDENTIFICATION HAS CHANGED`. Older images still contain built-in keys, but sshd ignores them under the current script.

A container created before this change used the image's built-in keys, so its first recreation changes the host key once. Remove the old entry, then connect again and accept the new key:

```bash
ssh-keygen -R '[localhost]:<host-port>'
```

To replace a container's host keys, delete its `ssh-host-keys` directory before recreating it; clients then need the same `ssh-keygen -R`.

### Port 8080

A service inside the container needs to listen on `0.0.0.0:8080` for the host to reach it via `http://localhost:8080` (or `http://localhost:62080` when the container was created with dev start port `62000`). For example:

```bash
python3 -m http.server 8080 --bind 0.0.0.0
```

### VS Code Remote-SSH

Add to VS Code's `~/.ssh/config`:

```
Host my-docker-dev
    HostName localhost
    Port 2222
    User root
```

Then connect with the Remote-SSH extension. The `.vscode-server` directory is persisted, so reconnecting never needs to reinstall extensions.

---

## Repository layout

Files are grouped by where they run: `image/` runs inside the image at build time, `host/` runs on the host machine.

```
Cuda-Dev-Box/
├── README.md                 # this file
├── LICENSE
├── Dockerfile                # image build file
├── VERSION                   # image version for `devbox build`: YYYYMM or YYYYMM-N
├── image/                    # setup scripts the Dockerfile copies in and runs (never run by hand)
│   ├── setup_cpp_libs.sh     # C/C++ library install (incl. GoogleTest)
│   ├── setup_env.sh          # Docker ENV for SSH sessions (sshd SetEnv) and shells
│   ├── setup_jvm.sh          # Kotlin install and JVM tool entrypoints
│   ├── setup_python.sh       # uv-managed Python 3.14 install
│   ├── setup_rust.sh         # rustfmt/clippy on top of the copied Rust toolchain
│   ├── setup_ssh.sh          # SSH configuration
│   └── setup_typescript.sh   # TypeScript, VS Code extension tools, Codex / Claude Code / DeepSeek Harness (npm)
├── host/                     # tools you run on the host
│   ├── devbox                # the one entry point: devbox build | create | memory | agent install | agent refresh
│   └── devbox_lib/           # modules behind it (not run directly)
│       ├── container.py      # `devbox create`: interactive container creation
│       ├── image.py          # `devbox build`: build and tag the image from VERSION
│       ├── memory.py         # `devbox memory` + the 8/16/32/64 GiB profiles `devbox create` also uses
│       └── ssh_agent.py      # `devbox agent`: persistent agent install, legacy per-container proxy
├── docs/
│   └── ISSUE_MATRIX.md       # review findings and their fixes (in Chinese)
└── tests/                    # python3 -m unittest discover -s tests
    ├── test_setup_typescript.py
    ├── test_create_docker.py
    ├── test_devbox_build.py
    ├── test_image_scripts.py
    ├── test_memory_profiles.py
    ├── test_persistence_docker.py   # needs a built image: DEVBOX_TEST_IMAGE=cuda-devbox:latest
    ├── test_ssh_agent.py
    └── test_ssh_env_docker.py       # needs a built image, likewise
```

Run `python3 -m unittest discover -s tests` for the host tests. To also verify persistence against a rebuilt image, run:

```bash
DEVBOX_TEST_IMAGE=cuda-devbox:latest python3 -m unittest discover -s tests -v
```

The optional Docker test uses temporary directories and offline containers, installs a local npm fixture, then deletes and recreates a container to check state, commands, and the SSH login environment.

---

## What's installed

### System tools

`wget` `git` `curl` `vim` `unzip` `htop` `tzdata` `bubblewrap`

### C/C++

| Tool | Version |
| --- | --- |
| Clang | system default |
| clang-format | system default |
| clang-tidy | system default |
| CMake | system default |
| GDB | system default |
| NASM | system default |
| Boost | system default |
| spdlog | system default |
| GoogleTest / GoogleMock | system default |
| ALSA / V4L2 | system default |
| bpftrace | system default |
| eBPF (libbpf) | system default |
| io_uring (liburing) | system default |
| mimalloc | system default |
| NUMA (numactl/libnuma) | system default |

### GPU / CUDA

| Component | Version |
| --- | --- |
| CUDA Toolkit | 13.3.1 |
| cuDNN | latest |

CUDA Toolkit and the surrounding environment come from NVIDIA's official `devel` image.

### Language runtimes

| Language | Version | Install method |
| --- | --- | --- |
| Python | 3.14 | uv |
| Rust | latest stable at build time | official Rust image (rustup, cargo, rustc) + rustfmt/clippy |
| Go | latest stable | official Go image |
| Java | OpenJDK 25 LTS | official Eclipse Temurin image |
| Maven | latest 3.x | official Maven image |
| Gradle | latest 9.x | official Gradle image |
| Kotlin | 2.3.21 | official zip, installed directly |
| Node.js | LTS | official Node image |
| TypeScript | latest | npm global |

The JVM environment (`JAVA_HOME`, `MAVEN_HOME`, `GRADLE_HOME`, `KOTLIN_HOME`, and their `bin` directories on `PATH`) reaches SSH sessions through sshd's `SetEnv` (`/etc/ssh/sshd_config.d/devbox-env.conf`), which `setup_env.sh` writes from the Dockerfile `ENV` values; `mvn`, `gradle`, `kotlin`, `kotlinc` are also symlinked into `/usr/local/bin`.

Rust lives in `CARGO_HOME=/usr/local/cargo` and `RUSTUP_HOME=/usr/local/rustup`, the same paths as the official image. The official image pins its default toolchain to an exact version (for example `1.98.1`) rather than the `stable` channel, so `rustup update` won't move it. Rebuild with a newer `RUST_TAG`, or run `rustup default stable` in the container to track stable (that downloads a full toolchain). rustfmt and clippy are installed; the offline docs (`rust-docs`) aren't.

### Python project dependencies

The base image doesn't pre-install Python project dependencies or create a virtualenv. Inside a project, run `uv sync`; it creates `.venv` and installs dependencies from that project's `pyproject.toml`/`uv.lock`.

### Node.js global packages

`typescript` `ts-node` `@vscode/vsce` `yo` `generator-code` `eslint`, plus `@openai/codex`, `@anthropic-ai/claude-code`, and `@deepseek-ai/dsh` (see below)

### AI CLI login

The image pre-installs both CLIs from their official npm packages, [`@openai/codex`](https://www.npmjs.com/package/@openai/codex) and [`@anthropic-ai/claude-code`](https://code.claude.com/docs/en/setup#install-with-npm). No token or API key is baked in. Each package carries the same native binary as the standalone installer, pulled in as a per-platform optional dependency. They install into `/usr/local/lib/node_modules`, with `codex` and `claude` in `/usr/local/bin`, reachable from SSH and non-login shells alike. The build runs `codex --version` and `claude --version` to verify them.

After building, run `codex` and `claude` at container runtime to log in / configure each one. `devbox create` persists their user state and uses a separate npm prefix for later installs (see [AI state and npm packages](#ai-state-and-npm-packages)). To update inside the container, run `npm install -g @openai/codex@latest` or `npm install -g --allow-scripts=@anthropic-ai/claude-code @anthropic-ai/claude-code@latest`; those copies survive recreation and take precedence over the image's bundled versions. The mounts leave `/usr/local` and the Python runtime under `/root/.local` visible. Cursor isn't installed via npm here, since the npm package of the same name isn't the official Cursor CLI.

### DeepSeek Harness

The image also installs [`@deepseek-ai/dsh`](https://github.com/deepseek-ai/deepseek-harness#run) directly from the official npm registry. `dsh` lives in `/usr/local/bin`, and the build checks `dsh --version`. `DEEPSEEK_HARNESS_VERSION` defaults to `latest`; the official latest release may be a preview or release candidate. No API key is needed at build time.

Start the Web UI inside the container:

```bash
dsh web --no-open
```

Forward its loopback port over SSH from your computer (replace `2222` with the container's published SSH port):

```bash
ssh -N -L 3080:127.0.0.1:3080 -p 2222 root@localhost
```

Open the full `http://127.0.0.1:3080/?token=...` URL printed by `dsh`. The launch token signs the browser in. In **Settings → Models**, enter your DeepSeek API key, then choose a workspace. See the [official Web UI guide](https://deepseek-harness.github.io/deepseek-harness/en/guide/quickstart). VS Code port forwarding can also forward container port `3080`.

`devbox create` persists Harness's default `/root/.dsh` directory, including credentials and profiles, using the `.dsh` bind mount above. Keep the default `DSH_HOME` to use this mount. To update at runtime, run `npm install -g --registry=https://registry.npmjs.org --allow-scripts=@deepseek-ai/dsh-subprocess-local,node-pty,koffi,protobufjs,@google/genai @deepseek-ai/dsh@latest`; the updated CLI uses the persisted npm prefix.

### Slow or failing AI CLI downloads

The two packages are large (roughly 350 MB for Codex and 230 MB for Claude Code, unpacked); `setup_typescript.sh` installs them after the TypeScript tools, in the same layer. If npm times out fetching them, pass your proxy to the build with `--build-arg HTTP_PROXY=...` / `--build-arg HTTPS_PROXY=...`. The daemon successfully pulling images doesn't mean npm inside the build got the same proxy config. See [Docker build proxy configuration](https://docs.docker.com/engine/cli/proxy/).

---

## Customization

### Changing the Python version

The default lives in the Dockerfile's `PYTHON_VERSION` build argument (see [Build arguments](#build-arguments)). Edit it there, or override it for one build:

```bash
host/devbox build --build-arg PYTHON_VERSION=3.13
```

### Adding a Python package

Inside a specific project, run `uv add <package>`, then `uv sync` to reproduce the project's environment — no need to rebuild the base image.

### Setting the SSH root password

`devbox create` sets the root password after the container starts. If you set a host environment variable, that value is used and never echoed:

```bash
export DOCKER_SSH_ROOT_PASSWORD='your-password'
host/devbox create
```

Without `DOCKER_SSH_ROOT_PASSWORD` set, the script generates a random password and shows it in the terminal exactly once. To reset it later:

```bash
docker exec -it <container-name> passwd root
```

> **Security note**: never hardcode a password in `setup_ssh.sh` or the Dockerfile — a build-time password would end up baked into the image state or its build history.

### Cursor / Claude Code sandbox errors

If Cursor reports `Terminal sandbox could not start`, or Claude Code hits `EPERM` / hangs running something like `cmake`, recreate the container and answer `y` to `Enable Cursor/Claude terminal sandbox compatibility?`.

If Cursor is installed directly on the host rather than reached through Remote-SSH into the container, this Docker launch parameter has no effect on the host's Cursor — you'd need to configure Cursor's AppArmor profile or user-namespace policy on the host side instead.

---

## Common Docker commands

```bash
# List running containers
docker ps

# Open a shell in the container
docker exec -it <container-name> bash

# Stop the container
docker stop <container-name>

# Start an existing container
docker start <container-name>

# Remove the container (data in the mounted directories is preserved)
docker rm -f <container-name>

# View container logs
docker logs <container-name>
```
