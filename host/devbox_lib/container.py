"""Interactively create an SSH-enabled dev container from the Cuda-Dev-Box image."""

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys

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
# Repository docker-bake.hcl builds (IMAGE_NAME), tagged YYYYMM and latest.
IMAGE_REPOSITORY = "cuda-devbox"
IMAGE_VERSION_TAG = re.compile(r"\d{6}")
LEGACY_PRIVATE_KEY_NAMES = ("id_ed25519", "id_rsa", "id_ecdsa")
# The image EXPOSEs 8000-8099; the host side is START..START+99.
CONTAINER_DEV_PORT_START = 8000
CONTAINER_DEV_PORT_COUNT = 100
# Where `nvidia-ctk cdi generate` writes GPU specs Docker can use for --gpus.
CDI_SPEC_DIRS = ("/etc/cdi", "/var/run/cdi")
KERNEL_DEV_BIND_MOUNTS = (
    ("/dev/hugepages", "/dev/hugepages", "rw"),
    ("/sys/kernel/tracing", "/sys/kernel/tracing", "rw"),
    ("/sys/kernel/debug", "/sys/kernel/debug", "rw"),
    ("/sys/fs/bpf", "/sys/fs/bpf", "rw"),
)


def is_open_ssh_public_key(line):
    parts = line.strip().split()
    if len(parts) < 2:
        return False
    key_type = parts[0]
    return key_type in PUBLIC_KEY_PREFIXES or key_type.startswith(PUBLIC_KEY_PREFIXES)


def collect_public_keys(host_ssh_dir):
    """Host users' own keys (*.pub) plus the keys allowed to log in to the host (authorized_keys).

    authorized_keys holds the keys of other machines used to reach this host,
    e.g. a laptop; they get the same access to the container. Lines with
    options (from=..., command=...) are skipped rather than copied unrestricted.
    """
    if not os.path.isdir(host_ssh_dir):
        return []

    public_keys = []
    seen = set()
    for filename in sorted(os.listdir(host_ssh_dir)):
        if not (filename.endswith(".pub") or filename == "authorized_keys"):
            continue
        path = os.path.join(host_ssh_dir, filename)
        if not os.path.isfile(path):
            continue
        with open(path, "r", encoding="utf-8") as key_file:
            for line in key_file:
                key = line.strip()
                if not key or key.startswith("#") or not is_open_ssh_public_key(key):
                    continue
                if key not in seen:
                    public_keys.append(key)
                    seen.add(key)
    return public_keys


def write_authorized_keys(ssh_dir, public_keys):
    authorized_keys_path = os.path.join(ssh_dir, "authorized_keys")
    with open(authorized_keys_path, "w", encoding="utf-8") as authorized_keys:
        if public_keys:
            authorized_keys.write("\n".join(public_keys) + "\n")
    os.chmod(authorized_keys_path, 0o600)
    return authorized_keys_path


def remove_legacy_private_keys(ssh_dir):
    removed = []
    for filename in LEGACY_PRIVATE_KEY_NAMES:
        path = os.path.join(ssh_dir, filename)
        if os.path.isfile(path):
            os.remove(path)
            removed.append(path)
    return removed


def build_root_password():
    env_password = os.environ.get("DOCKER_SSH_ROOT_PASSWORD")
    if env_password:
        if "\n" in env_password or "\r" in env_password:
            raise ValueError("DOCKER_SSH_ROOT_PASSWORD must not contain newline characters")
        return env_password, False
    return secrets.token_urlsafe(24), True


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


def prompt_image_tag(repository):
    """Pick a local build of repository by tag; Enter takes the newest YYYYMM build."""
    tags = local_image_tags(repository)
    # docker-bake.hcl tags every build YYYYMM (and moves latest); newest first.
    versions = sorted((tag for tag in tags if IMAGE_VERSION_TAG.fullmatch(tag)), reverse=True)
    default = versions[0] if versions else "latest" if "latest" in tags else None
    if default is None:
        raise SystemExit(f"No local {repository} image found; build one first with `docker buildx bake`.")

    choices = ", ".join(versions or tags)
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


def prompt_memory_profile():
    memtotal_kib = memory.read_memtotal_kib()
    while True:
        name = input("Host RAM profile (auto/16/32/64, default auto): ").strip() or "auto"
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
    return f"{host_start}-{host_end}:{CONTAINER_DEV_PORT_START}-{container_end}/tcp"


def prompt_ssh_port(dev_port_start):
    start, end = dev_port_range(dev_port_start)
    while True:
        value = input("Enter the host port to map to container's port 22 (default 2222): ").strip() or "2222"
        if value.isascii() and value.isdecimal() and 1 <= int(value) <= 65535 and not start <= int(value) <= end:
            return str(int(value))
        print(f"Choose a port from 1 to 65535 outside the development range {start}-{end}.")


def prompt_key_only():
    return input("Disable password login and allow SSH public keys only? (y/N): ").strip().lower() == "y"


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
    if not (os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/dxg")):
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


def prompt_gpu():
    reason = nvidia_gpu_unavailable_reason()
    if reason:
        print(f"Warning: {reason}; creating the container without GPU passthrough.")
        return False
    return input("Enable NVIDIA GPU passthrough? (Y/n): ").strip().lower() != "n"


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
    agent_dir = ssh_agent.persistent_agent_dir()
    if not os.path.isdir(agent_dir):
        return False

    persistent_sock = os.path.join(agent_dir, ssh_agent.PERSISTENT_SSH_AGENT_SOCKET)
    if not os.path.exists(persistent_sock):
        print(
            f"Warning: persistent SSH agent socket is not available yet: {persistent_sock}. "
            "Container will still start; run the user service or ssh-add later."
        )

    command += [
        "-v", f"{agent_dir}:/ssh-agent",
        "-e", f"SSH_AUTH_SOCK=/ssh-agent/{ssh_agent.PERSISTENT_SSH_AGENT_SOCKET}",
    ]
    print(f"Using persistent SSH agent directory: {agent_dir}")
    return True


def add_proxied_ssh_agent_mount(command, container_name):
    ssh_auth_sock = ssh_agent.host_ssh_auth_sock()
    if not ssh_auth_sock:
        print(
            "Warning: SSH agent forwarding requested, but SSH_AUTH_SOCK is not available; continuing without "
            "agent forwarding. Run `host/devbox agent install` once to set up a persistent agent."
        )
        return False

    agent_dir = ssh_agent.container_agent_dir(container_name)
    try:
        proxy_sock = ssh_agent.start_proxy(agent_dir, ssh_auth_sock)
    except RuntimeError as exc:
        print(f"Warning: {exc}. Continuing without agent forwarding.")
        return False

    print(f"Started SSH agent proxy at {proxy_sock}")
    command += [
        "-v", f"{agent_dir}:/ssh-agent",
        "-e", f"SSH_AUTH_SOCK=/ssh-agent/{ssh_agent.SSH_AGENT_PROXY_SOCKET}",
    ]
    return True


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "create", help="Interactively create an SSH dev container",
        description=f"Interactively create an SSH dev container. Asks for the {IMAGE_REPOSITORY} tag "
                    "(Enter = newest YYYYMM build), container name, dev-port start, SSH port, login mode, "
                    "memory profile, GPU, and optional features.",
    )
    parser.set_defaults(func=run)


def run(args):
    image_name = prompt_image_tag(IMAGE_REPOSITORY)

    # Prompt for container name
    container_name = input("Enter the container name: ")

    # Reserve the complete development range and choose a separate SSH port.
    dev_port_start = prompt_dev_port_start()
    host_port = prompt_ssh_port(dev_port_start)
    key_only = prompt_key_only()
    memory_profile = prompt_memory_profile()
    enable_gpu = prompt_gpu()

    # Prompt for SSH agent forwarding
    forward_ssh_agent = input("Forward host SSH agent into the container? (y/N): ").strip().lower() == "y"

    # Prompt for Cursor/Claude Code terminal sandbox compatibility
    enable_agent_sandbox = input(
        "Enable Cursor/Claude terminal sandbox compatibility? (adds SYS_ADMIN/NET_ADMIN and unconfined seccomp/AppArmor) (y/N): "
    ).strip().lower() == "y"

    # Reuse the host daemon through its socket; do not start a nested daemon.
    enable_dind = input("Enable privileged mode and host Docker socket access? (Y/n): ").strip().lower() != "n"

    # Prompt for host kernel development passthrough. Defaults to enabled for this dev container.
    enable_kernel_dev = input(
        "Enable host kernel development passthrough for bpftrace/HugeTLB/CMA/NUMA? (Y/n): "
    ).strip().lower() != "n"

    if not key_only:
        root_password, generated_password = build_root_password()

    # Create the mount directory
    mount_dir = os.path.expanduser(f"~/docker_mount/{container_name}")
    data_dir = os.path.expanduser(f"~/docker_mount/{container_name}/data")
    vscode_server_dir = os.path.expanduser(f"~/docker_mount/{container_name}/.vscode-server")
    gitconfig_path = os.path.expanduser("~/.gitconfig")
    ssh_dir = os.path.expanduser(f"~/docker_mount/{container_name}/.ssh")

    dir_list = [mount_dir, data_dir, vscode_server_dir, ssh_dir]

    for directory in dir_list:
        if not os.path.exists(directory):
            os.makedirs(directory, exist_ok=True)

    removed_private_keys = remove_legacy_private_keys(ssh_dir)
    if removed_private_keys:
        print(f"Removed {len(removed_private_keys)} legacy private key file(s) from {ssh_dir}")

    host_ssh_dir = os.path.expanduser("~/.ssh")
    public_keys = collect_public_keys(host_ssh_dir)
    # Preserve keys already used to reach this container when recreating it.
    existing_keys_path = os.path.join(ssh_dir, "authorized_keys")
    if os.path.isfile(existing_keys_path):
        with open(existing_keys_path, "r", encoding="utf-8") as existing_keys:
            for line in existing_keys:
                key = line.strip()
                if key and not key.startswith("#") and key not in public_keys:
                    public_keys.append(key)
    if key_only and not public_keys:
        raise SystemExit("SSH key-only mode requires at least one public key in ~/.ssh/*.pub, ~/.ssh/authorized_keys, or the container authorized_keys.")
    authorized_keys_path = write_authorized_keys(ssh_dir, public_keys)
    if public_keys:
        print(f"Injected {len(public_keys)} public key(s) into {authorized_keys_path}")
    else:
        print("Warning: no public keys found in ~/.ssh/*.pub or ~/.ssh/authorized_keys; SSH key login will not be available.")

    os.chmod(ssh_dir, 0o700)

    # Create first so initialization failures can remove only this new container.
    command = [
        "docker", "create",
        "--name", container_name,
        "--hostname", container_name,
        "-p", f"{host_port}:22",
        "-p", dev_port_binding(dev_port_start),
    ]
    command += memory_profile.docker_args()

    command += [
        "-v", f"{data_dir}:/root/data",
        "-v", f"{vscode_server_dir}:/root/.vscode-server",
        "-v", f"{ssh_dir}:/root/.ssh",
    ]
    if os.path.isfile(gitconfig_path):
        command += ["-v", f"{gitconfig_path}:/root/.gitconfig:ro"]
    if enable_gpu:
        command += ["--gpus", "all"]

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

    if forward_ssh_agent and not add_persistent_ssh_agent_mount(command):
        add_proxied_ssh_agent_mount(command, container_name)

    if enable_dind:
        command += [
            "--privileged",
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
        ]
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

    command += [
        "--entrypoint", "/usr/sbin/sshd",
        "--restart", "unless-stopped",
        image_name,
        "-D",
        "-e",
    ]
    if key_only:
        command += ["-o", "PasswordAuthentication=no", "-o", "KbdInteractiveAuthentication=no",
                    "-o", "PermitRootLogin=prohibit-password"]

    create_result = subprocess.run(command, check=True, stdout=subprocess.PIPE, text=True)
    container_id = create_result.stdout.strip()
    if not container_id:
        raise RuntimeError("Docker create did not return a container ID")
    try:
        subprocess.run(["docker", "start", container_id], check=True, stdout=subprocess.PIPE, text=True)
        if not key_only:
            subprocess.run(
                ["docker", "exec", "-i", container_id, "chpasswd"],
                input=f"root:{root_password}\n",
                check=True,
                text=True,
            )
        subprocess.run(["docker", "exec", container_id, "/usr/sbin/sshd", "-t"], check=True)
    except (OSError, subprocess.CalledProcessError):
        cleanup = subprocess.run(["docker", "rm", "-f", container_id], check=False)
        if cleanup.returncode:
            print(f"Warning: initialization failed and Docker could not remove the new container {container_id}.", file=sys.stderr)
        raise
    print(f"Started container: {container_id}")

    if key_only:
        print("SSH public-key login enabled; password login is disabled.")
    elif generated_password:
        print("Generated root SSH password. It is shown once; store it now if you need password login.")
        print(f"Root SSH password: {root_password}")
    else:
        print("Root SSH password was set from DOCKER_SSH_ROOT_PASSWORD and was not displayed.")
    return 0
