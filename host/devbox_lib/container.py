"""Interactively create an SSH-enabled dev container from the Cuda-Dev-Box image."""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile

from . import memory, ssh_agent

PUBLIC_KEY_PREFIXES = (
    "ssh-ed25519",
    "ssh-rsa",
    "ssh-dss",
    "rsa-sha2-",
    "ecdsa-sha2-",
    "sk-ssh-ed25519@",
    "sk-ecdsa-sha2-",
)
# Default image repository; `devbox build` tags each build with its version and latest.
IMAGE_REPOSITORY = "cuda-devbox"
# A version is YYYYMM, then YYYYMM-2, YYYYMM-3, ... for more builds in the same month.
IMAGE_VERSION_TAG = re.compile(r"\d{6}(?:-\d+)?")
# Docker's name rule, capped at 63 for the hostname; it also keeps the name one safe path component.
CONTAINER_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{1,62}")
# The image EXPOSEs 8000-8099; the host side is START..START+99.
CONTAINER_DEV_PORT_START = 8000
CONTAINER_DEV_PORT_COUNT = 100
# Where `nvidia-ctk cdi generate` writes GPU specs Docker can use for --gpus.
CDI_SPEC_DIRS = ("/etc/cdi", "/var/run/cdi")
# WSL kernels report a release such as 6.6.87.2-microsoft-standard-WSL2.
OSRELEASE_PATH = "/proc/sys/kernel/osrelease"
KERNEL_DEV_BIND_MOUNTS = (
    ("/dev/hugepages", "/dev/hugepages", "rw"),
    ("/sys/kernel/tracing", "/sys/kernel/tracing", "rw"),
    ("/sys/kernel/debug", "/sys/kernel/debug", "rw"),
    ("/sys/fs/bpf", "/sys/fs/bpf", "rw"),
)
# ~/docker_mount/<name> is the box's /root, so everything kept in the home (tool
# logins, shell history, caches, runtime installs) survives recreation and image
# updates. Files devbox keeps for a box live in BOX_STATE_DIR/<name>, which the box
# never sees.
BOX_STATE_DIR = "~/docker_mount/.devbox"
# Set by images that keep nothing they need in /root (see the Dockerfile).
PERSISTENT_HOME_LABEL = "cuda-devbox.persistent-home"
# Ubuntu's startup files for root, which the image's own /root holds and the mount hides.
SEED_HOME_SCRIPT = (
    'for name in .bashrc .profile; do '
    '[ -e "/root/$name" ] || cp "/usr/share/base-files/dot$name" "/root/$name"; done'
)
# SSH host keys live with the container's state, not in the image, so a box keeps
# its host identity across recreation and image rebuilds. sshd is pointed at them
# explicitly, which also overrides the keys baked into older images.
SSH_HOST_KEY_TYPES = ("ed25519", "ecdsa", "rsa")
SSH_HOST_KEY_DIR = "/etc/ssh/host-keys"
# Ports are published on the host's loopback only: a box is reached through its
# host (from another machine, `ssh -J <host>`), never straight from the network.
PUBLISH_ADDRESS = "127.0.0.1"
# Public keys pasted with `devbox key add`, kept with the box's state so recreation keeps them.
ADDED_KEYS_FILE = "ssh-authorized-keys"
# Password login is never offered; create sets no root password.
KEY_ONLY_SSHD_OPTIONS = (
    "-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
    "-o", "PermitRootLogin=prohibit-password",
)


def is_open_ssh_public_key(line):
    parts = line.strip().split()
    if len(parts) < 2:
        return False
    key_type = parts[0]
    return key_type in PUBLIC_KEY_PREFIXES or key_type.startswith(PUBLIC_KEY_PREFIXES)


def public_keys_in(path):
    """Plain public keys in one file; lines with options (from=..., command=...) are
    skipped rather than copied unrestricted."""
    with open(path, "r", encoding="utf-8") as key_file:
        return [line.strip() for line in key_file if is_open_ssh_public_key(line)]


def key_identity(key):
    """Type and key data: the same key under another comment is the same key."""
    return tuple(key.split()[:2])


def home_dir(container_name):
    return os.path.expanduser(f"~/docker_mount/{container_name}")


def box_state_path(container_name, name):
    """A file or directory devbox keeps for the box, outside the box's home.

    Earlier versions kept these in ~/docker_mount/<name>, which is now the home,
    where the box could change them; they move out on first use.
    """
    state_dir = os.path.expanduser(f"{BOX_STATE_DIR}/{container_name}")
    path = os.path.join(state_dir, name)
    legacy_path = os.path.join(home_dir(container_name), name)
    if not os.path.lexists(path) and os.path.lexists(legacy_path):
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        os.rename(legacy_path, path)
    return path


def added_keys_path(container_name):
    return box_state_path(container_name, ADDED_KEYS_FILE)


def box_key_sources(container_name):
    """(key, file) for each key the box authorizes, first file winning for duplicates.

    The host user's own keys (~/.ssh/*.pub), the keys allowed to log in to the host
    (~/.ssh/authorized_keys, e.g. a laptop's), then the keys added with `devbox key add`.
    The box's authorized_keys is rewritten from these alone, so a key removed here
    loses access.
    """
    host_ssh_dir = os.path.expanduser("~/.ssh")
    paths = []
    if os.path.isdir(host_ssh_dir):
        paths = [os.path.join(host_ssh_dir, name) for name in sorted(os.listdir(host_ssh_dir))
                 if name.endswith(".pub") or name == "authorized_keys"]
    paths.append(added_keys_path(container_name))

    sources = []
    seen = set()
    for path in paths:
        if not os.path.isfile(path):
            continue
        for key in public_keys_in(path):
            if key_identity(key) not in seen:
                sources.append((key, path))
                seen.add(key_identity(key))
    return sources


def install_authorized_keys(container, public_keys):
    """Write the box's authorized_keys through Docker.

    Work inside the box can leave its .ssh directory owned by root on the host,
    where the host user can no longer write. docker cp reaches it anyway, whether
    the box is created, running or stopped.
    """
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", prefix="devbox-authorized-keys-") as keys_file:
        keys_file.write("".join(f"{key}\n" for key in public_keys))
        keys_file.flush()
        subprocess.run(["docker", "cp", "-q", keys_file.name, f"{container}:/root/.ssh/authorized_keys"], check=True)


def ensure_ssh_host_keys(host_key_dir, container_name):
    """Generate the host keys this container lacks; existing ones are kept as they are."""
    os.makedirs(host_key_dir, mode=0o700, exist_ok=True)
    for key_type in SSH_HOST_KEY_TYPES:
        key_path = os.path.join(host_key_dir, f"ssh_host_{key_type}_key")
        if os.path.isfile(key_path):
            continue
        try:
            subprocess.run(
                ["ssh-keygen", "-q", "-t", key_type, "-N", "", "-C", f"root@{container_name}", "-f", key_path],
                check=True, stdin=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            raise SystemExit("ssh-keygen is not installed; install the host OpenSSH client first.")


def sshd_host_key_options():
    return [option for key_type in SSH_HOST_KEY_TYPES
            for option in ("-o", f"HostKey={SSH_HOST_KEY_DIR}/ssh_host_{key_type}_key")]


def docker_image_name_error(image_name):
    image_name = image_name.strip()
    if not image_name:
        return "Docker image name must not be empty."
    if any(char.isspace() for char in image_name):
        return "Docker image name must not contain whitespace."
    if "://" in image_name:
        return "Docker image name must not include a URL scheme."

    last_component = image_name.rsplit("/", 1)[-1]
    if last_component.count(":") > 1:
        parts = last_component.split(":")
        if len(parts) == 3:
            slash_suggestion = image_name[: -len(last_component)] + f"{parts[0]}/{parts[1]}:{parts[2]}"
            dash_suggestion = image_name[: -len(last_component)] + f"{parts[0]}-{parts[1]}:{parts[2]}"
            return (
                "Docker image names can have only one tag separator after the final '/'. "
                f"Use '{slash_suggestion}' or '{dash_suggestion}' instead of '{image_name}'."
            )
        return "Docker image names can have only one tag separator after the final '/'."

    if ":" in last_component:
        repository, tag = last_component.split(":", 1)
        if not repository:
            return "Docker image repository must not be empty."
        if not tag:
            return "Docker image tag must not be empty."

    return None


def local_image_tags(repository):
    result = subprocess.run(
        ["docker", "image", "ls", repository, "--format", "{{.Tag}}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return [tag for tag in dict.fromkeys(result.stdout.split()) if tag != "<none>"]


def image_version_key(version):
    """Sort key for YYYYMM[-N]: numeric, so 202610-10 comes after 202610-9."""
    month, _, build = version.partition("-")
    return int(month), int(build or 1)


def prompt_image_tag(repository):
    """Pick a local build; prefer latest, then the newest versioned build."""
    tags = local_image_tags(repository)
    # devbox build moves latest; older versioned tags may still be present.
    versions = sorted((tag for tag in tags if IMAGE_VERSION_TAG.fullmatch(tag)), key=image_version_key, reverse=True)
    default = "latest" if "latest" in tags else versions[0] if versions else None
    if default is None:
        raise SystemExit(f"No local {repository} image found; build one first with `host/devbox build`.")

    choices = ", ".join(dict.fromkeys([default, *versions, *tags]))
    while True:
        tag = input(f"Image tag for {repository} ({choices}; default {default}; or a full image name): ").strip() or default
        if tag in tags:
            print(f"Using image {repository}:{tag}")
            return f"{repository}:{tag}"
        if ":" in tag or "/" in tag:
            error = docker_image_name_error(tag)
            if not error:
                print(f"Using image {tag}")
                return tag
            print(f"Invalid Docker image name: {error}")
            continue
        print(f"No local image {repository}:{tag}. Choose one of: {', '.join(tags)}")


def confirm_persistent_home(image_name):
    """Older images keep the default Python in /root, where the mounted home hides it."""
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", f'{{{{index .Config.Labels "{PERSISTENT_HOME_LABEL}"}}}}', image_name],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    if result.returncode == 0 and result.stdout.strip() == "true":
        return
    print(f"Warning: {image_name} is not a local image built for a persisted home ({PERSISTENT_HOME_LABEL} label). "
          "create mounts ~/docker_mount/<container> as /root, which hides anything such an image keeps there, "
          "such as its default Python. Rebuild the image with `host/devbox build` first.")
    if input("Continue anyway? (y/N): ").strip().lower() != "y":
        raise SystemExit("Nothing was created.")


def existing_container_names():
    result = subprocess.run(
        ["docker", "ps", "--all", "--format", "{{.Names}}"],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    # A container with legacy links lists extra names, comma-separated.
    return {name for line in result.stdout.split() for name in line.split(",")}


def container_name_error(name, existing_names):
    if not CONTAINER_NAME.fullmatch(name):
        return "Use 2-63 letters, digits, '_', '.' or '-', starting with a letter or digit."
    if name in existing_names:
        return f"Container {name} already exists; remove it first (docker rm -f {name}) or choose another name."
    return None


def prompt_container_name():
    existing_names = existing_container_names()
    while True:
        name = input("Enter the container name: ").strip()
        error = container_name_error(name, existing_names)
        if not error:
            return name
        print(error)


def running_under_wsl():
    try:
        with open(OSRELEASE_PATH, "r", encoding="utf-8") as osrelease:
            return "microsoft" in osrelease.read().lower()
    except OSError:
        return False


def prompt_memory_profile(memtotal_kib):
    while True:
        name = input(f"Host RAM profile ({'/'.join(memory.PROFILE_CHOICES)}, default auto): ").strip() or "auto"
        try:
            profile = memory.select_profile(name, memtotal_kib)
        except ValueError as error:
            print(error)
            continue
        print(f"Using {profile.name} GiB profile: RAM limit {profile.memory_gib} GiB, swap limit {profile.swap_gib} GiB.")
        return profile


def dev_port_start_error(value):
    if not (value.isascii() and value.isdecimal()):
        return "Use a single port number, for example 62000."
    start = int(value)
    end = start + CONTAINER_DEV_PORT_COUNT - 1
    if not 1 <= start or end > 65535:
        return f"The start port must be 1-{65536 - CONTAINER_DEV_PORT_COUNT} so all {CONTAINER_DEV_PORT_COUNT} ports fit."
    if start <= 22 <= end:
        return f"Host ports {start}-{end} would include host SSH port 22."
    return None


def prompt_dev_port_start():
    default = str(CONTAINER_DEV_PORT_START)
    while True:
        value = input(
            f"Host start port for the {CONTAINER_DEV_PORT_COUNT} dev ports mapped to container "
            f"8000-8099 (default {default}): "
        ).strip() or default
        error = dev_port_start_error(value)
        if not error:
            return int(value)
        print(error)


def dev_port_range(start):
    return start, start + CONTAINER_DEV_PORT_COUNT - 1


def dev_port_binding(start):
    """Map host START..START+99 onto container 8000-8099."""
    host_start, host_end = dev_port_range(start)
    container_end = CONTAINER_DEV_PORT_START + CONTAINER_DEV_PORT_COUNT - 1
    return f"{PUBLISH_ADDRESS}:{host_start}-{host_end}:{CONTAINER_DEV_PORT_START}-{container_end}/tcp"


def prompt_ssh_port(dev_port_start):
    start, end = dev_port_range(dev_port_start)
    while True:
        value = input("Enter the host port to map to container's port 22 (default 2222): ").strip() or "2222"
        if value.isascii() and value.isdecimal() and 1 <= int(value) <= 65535 and not start <= int(value) <= end:
            return str(int(value))
        print(f"Choose a port from 1 to 65535 outside the development range {start}-{end}.")


def add_unique(values, value):
    if value not in values:
        values.append(value)


def add_optional_bind_mount(command, host_path, container_path, mode):
    if os.path.exists(host_path):
        command += ["-v", f"{host_path}:{container_path}:{mode}"]
    else:
        print(f"Warning: {host_path} does not exist; skipping mount for {container_path}.")


def add_optional_device(command, device_path):
    if os.path.exists(device_path):
        command += ["--device", device_path]
    else:
        print(f"Warning: {device_path} does not exist; skipping device passthrough.")


def nvidia_gpu_unavailable_reason():
    """Why the container cannot get the NVIDIA GPU, or None when `--gpus all` will work."""
    if not os.path.exists("/dev/nvidiactl"):
        return "no NVIDIA GPU device is available"

    try:
        result = subprocess.run(
            ["nvidia-smi", "-L"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return "nvidia-smi is not installed"
    if result.returncode != 0:
        return "the NVIDIA driver is unavailable"

    if not docker_can_use_nvidia_gpus():
        return ("the host has an NVIDIA GPU, but Docker cannot pass it through (NVIDIA Container Toolkit "
                "is not installed or not configured for Docker; see README 'Host capability prerequisites', "
                "then recreate the container)")
    return None


def parse_gpu_selection(value):
    """Normalize GPU indices/ranges to the single CSV field Docker --gpus expects."""
    value = value.strip().lower() or "all"
    if value == "all":
        return value

    devices = {}
    for item in value.split(","):
        match = re.fullmatch(r"([0-9]+)(?:\s*-\s*([0-9]+))?", item.strip())
        if not match:
            raise ValueError("Use all, comma-separated GPU indices (0,2), or ranges (0-3,5).")
        start = int(match[1])
        end = int(match[2]) if match[2] is not None else start
        if start > end:
            raise ValueError("GPU ranges must run from the lower index to the higher index (0-3).")
        for device in range(start, end + 1):
            devices[device] = None

    # Docker parses this argument as CSV, even when invoked without a shell.
    return '"device=' + ",".join(str(device) for device in devices) + '"'


def prompt_gpu():
    reason = nvidia_gpu_unavailable_reason()
    if reason:
        print(f"Warning: {reason}; creating the container without GPU passthrough.")
        return None
    while True:
        value = input("NVIDIA GPUs to use (all, 0,2, or 0-3; default all): ")
        try:
            return parse_gpu_selection(value)
        except ValueError as error:
            print(error)


def docker_can_use_nvidia_gpus():
    """Whether `docker --gpus` has an NVIDIA driver to use; without one, `docker start` fails."""
    # The NVIDIA Container Toolkit hook is what Docker's built-in --gpus driver calls.
    if shutil.which("nvidia-container-runtime-hook"):
        return True
    for cdi_dir in CDI_SPEC_DIRS:
        if os.path.isdir(cdi_dir) and any(name.startswith("nvidia") for name in os.listdir(cdi_dir)):
            return True
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        return False
    return '"nvidia"' in (result.stdout or "")


def add_optional_dma_heap_devices(command):
    dma_heap_dir = "/dev/dma_heap"
    if not os.path.isdir(dma_heap_dir):
        print("Warning: /dev/dma_heap does not exist; skipping DMA heap device passthrough.")
        return

    device_paths = sorted(
        os.path.join(dma_heap_dir, filename)
        for filename in os.listdir(dma_heap_dir)
        if os.path.exists(os.path.join(dma_heap_dir, filename))
    )
    if not device_paths:
        print("Warning: /dev/dma_heap is empty; skipping DMA heap device passthrough.")
        return

    for device_path in device_paths:
        command += ["--device", device_path]


def read_meminfo_value(name):
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as meminfo:
            for line in meminfo:
                if line.startswith(f"{name}:"):
                    parts = line.split()
                    return int(parts[1])
    except OSError:
        return None
    return None


def warn_kernel_dev_host_state():
    hugepages_total = read_meminfo_value("HugePages_Total")
    if hugepages_total == 0:
        print("Warning: host HugePages_Total is 0; HugeTLB allocations will not be available.")

    cma_total_kib = read_meminfo_value("CmaTotal")
    if not cma_total_kib:
        print("Warning: host CmaTotal is 0 or unavailable; CMA-backed contiguous DMA allocation may be unavailable.")

    try:
        with open("/proc/sys/kernel/io_uring_disabled", "r", encoding="utf-8") as io_uring_disabled:
            value = io_uring_disabled.read().strip()
        if value != "0":
            print(f"Warning: host kernel.io_uring_disabled={value}; io_uring may be disabled.")
    except OSError:
        print("Warning: cannot read /proc/sys/kernel/io_uring_disabled.")


def add_persistent_ssh_agent_mount(command):
    ssh_agent.ensure_persistent_agent()
    agent_dir = ssh_agent.persistent_agent_dir()
    command += [
        "-v", f"{agent_dir}:/ssh-agent",
        "-e", f"SSH_AUTH_SOCK=/ssh-agent/{ssh_agent.PERSISTENT_SSH_AGENT_SOCKET}",
    ]
    print(f"Using persistent SSH agent directory: {agent_dir}")


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "create", help="Interactively create an SSH dev container",
        description=f"Interactively create an SSH dev container. Asks for the {IMAGE_REPOSITORY} tag "
                    "(Enter = latest, or newest YYYYMM build), container name, dev-port start, SSH port, "
                    "memory profile, GPU, and optional features.",
    )
    parser.set_defaults(func=run)


def run(args):
    # Refuse hosts that cannot run a box before asking anything.
    if running_under_wsl():
        raise SystemExit("WSL is not supported; run devbox create on a native Linux host.")
    try:
        memtotal_kib = memory.read_memtotal_kib()
        memory.select_profile("auto", memtotal_kib)
    except ValueError as error:
        raise SystemExit(str(error))

    image_name = prompt_image_tag(IMAGE_REPOSITORY)
    confirm_persistent_home(image_name)
    container_name = prompt_container_name()
    # Checked before the other questions: without a key nobody could log in.
    public_keys = [key for key, _ in box_key_sources(container_name)]
    if not public_keys:
        raise SystemExit(
            "No SSH public key to authorize, and password login is not offered. Add the key of the machine "
            f"you connect from with `host/devbox key add {container_name}`, then run create again."
        )

    # Reserve the complete development range and choose a separate SSH port.
    dev_port_start = prompt_dev_port_start()
    host_port = prompt_ssh_port(dev_port_start)
    memory_profile = prompt_memory_profile(memtotal_kib)
    gpu_selection = prompt_gpu()

    # Prompt for SSH agent forwarding
    forward_ssh_agent = input("Forward host SSH agent into the container? (y/N): ").strip().lower() == "y"

    # Prompt for Cursor/Claude Code terminal sandbox compatibility
    enable_agent_sandbox = input(
        "Enable Cursor/Claude terminal sandbox compatibility? (adds SYS_ADMIN/NET_ADMIN and unconfined seccomp/AppArmor) (y/N): "
    ).strip().lower() == "y"

    # Reuse the host daemon through its socket; do not start a nested daemon.
    if gpu_selection not in (None, "all"):
        print("Specific GPU selection uses non-privileged mode; privileged mode exposes all host devices.")
        enable_host_docker = input("Enable host Docker socket access? (Y/n): ").strip().lower() != "n"
        enable_privileged = False
    else:
        enable_host_docker = input("Enable privileged mode and host Docker socket access? (Y/n): ").strip().lower() != "n"
        enable_privileged = enable_host_docker

    # Prompt for host kernel development passthrough. Defaults to enabled for this dev container.
    enable_kernel_dev = input(
        "Enable host kernel development passthrough for bpftrace/HugeTLB/CMA/NUMA? (Y/n): "
    ).strip().lower() != "n"

    # The box's home. A recreated box gets the same one back; an existing one from
    # before the whole home was mounted already holds data, .ssh and the tool directories.
    home = home_dir(container_name)
    gitconfig_path = os.path.expanduser("~/.gitconfig")
    ssh_dir = os.path.join(home, ".ssh")
    os.makedirs(home, mode=0o700, exist_ok=True)
    # docker cp needs /root/.ssh to exist.
    for directory in (os.path.join(home, "data"), ssh_dir):
        os.makedirs(directory, exist_ok=True)

    # The home holds logins, tokens and shell history.
    for directory, mode in ((home, 0o700), (ssh_dir, 0o700)):
        try:
            os.chmod(directory, mode)
        except PermissionError:
            # Work inside the box left it owned by root; authorized_keys goes in through Docker.
            pass
    host_key_dir = box_state_path(container_name, "ssh-host-keys")
    ensure_ssh_host_keys(host_key_dir, container_name)

    # Create first so initialization failures can remove only this new container.
    command = [
        "docker", "create",
        "--name", container_name,
        "--hostname", container_name,
        "-p", f"{PUBLISH_ADDRESS}:{host_port}:22",
        "-p", dev_port_binding(dev_port_start),
    ]
    command += memory_profile.docker_args()

    command += [
        "-v", f"{home}:/root",
        "-v", f"{host_key_dir}:{SSH_HOST_KEY_DIR}:ro",
    ]
    if os.path.isfile(gitconfig_path):
        command += ["-v", f"{gitconfig_path}:/root/.gitconfig:ro"]
    if gpu_selection:
        command += ["--gpus", gpu_selection]

    if enable_kernel_dev:
        warn_kernel_dev_host_state()
        command += [
            "--ipc=host",
            "--pid=host",
            "--ulimit", "memlock=-1:-1",
        ]
        for host_path, container_path, mode in KERNEL_DEV_BIND_MOUNTS:
            add_optional_bind_mount(command, host_path, container_path, mode)
        add_optional_dma_heap_devices(command)
        add_optional_device(command, "/dev/udmabuf")
    else:
        # Docker's private /dev/shm defaults to 64 MiB: too small for PyTorch
        # DataLoader workers and NCCL shared-memory transport.
        command += ["--shm-size", memory_profile.shm_size()]

    if forward_ssh_agent:
        add_persistent_ssh_agent_mount(command)

    if enable_host_docker:
        command += ["-v", "/var/run/docker.sock:/var/run/docker.sock"]
    if enable_privileged:
        command += ["--privileged"]
    else:
        # Cursor/Claude Code and kernel tracing tools use primitives that Docker's
        # default seccomp/AppArmor profiles and dropped capabilities can block.
        # Privileged containers already relax these restrictions.
        cap_adds = []
        security_opts = []
        if enable_agent_sandbox:
            for capability in ("SYS_ADMIN", "NET_ADMIN"):
                add_unique(cap_adds, capability)
            for security_opt in ("seccomp=unconfined", "apparmor=unconfined"):
                add_unique(security_opts, security_opt)
        if enable_kernel_dev:
            for capability in ("IPC_LOCK", "SYS_RESOURCE", "SYS_ADMIN", "SYS_PTRACE", "BPF", "PERFMON"):
                add_unique(cap_adds, capability)
            add_unique(security_opts, "seccomp=unconfined")

        for capability in cap_adds:
            command += ["--cap-add", capability]
        for security_opt in security_opts:
            command += ["--security-opt", security_opt]

    sshd_options = [*sshd_host_key_options(), *KEY_ONLY_SSHD_OPTIONS]
    command += [
        "--entrypoint", "/usr/sbin/sshd",
        "--restart", "unless-stopped",
        image_name,
        "-D",
        "-e",
        *sshd_options,
    ]

    create_result = subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)
    container_id = create_result.stdout.strip()
    if not container_id:
        raise RuntimeError("Docker create did not return a container ID")
    try:
        # Before start, so keys left from an earlier box never get a moment of access.
        install_authorized_keys(container_id, public_keys)
        subprocess.run(["docker", "start", container_id], check=True, stdout=subprocess.PIPE, text=True)
        # A new home starts empty; files already there are left as they are.
        subprocess.run(["docker", "exec", container_id, "/bin/sh", "-c", SEED_HOME_SCRIPT], check=True)
        # Check with the daemon's own options: images without built-in host keys need them.
        subprocess.run(["docker", "exec", container_id, "/usr/sbin/sshd", "-t", *sshd_options], check=True)
    except BaseException:
        # Includes Ctrl-C, which would otherwise leave a half-initialized box.
        cleanup = subprocess.run(["docker", "rm", "-f", container_id], check=False)
        if cleanup.returncode:
            print(f"Warning: initialization failed and Docker could not remove the new container {container_id}.", file=sys.stderr)
        raise
    print(f"Started container: {container_id}")
    print(f"Its /root is {home} on the host and survives recreation.")
    print(f"Authorized {len(public_keys)} SSH public key(s); password login is disabled. "
          f"Add more with `host/devbox key add {container_name}`.")
    print(f"SSH is reachable only from this host: ssh -p {host_port} root@{PUBLISH_ADDRESS}. "
          f"From another machine, jump through this host: ssh -J <this-host> -p {host_port} root@{PUBLISH_ADDRESS}")
    return 0
