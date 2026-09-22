#!/usr/bin/env python3
"""SSH agent forwarding for dev containers.

Two ways to reach a host SSH agent from a container:

- Persistent agent: `devbox create` installs it automatically when forwarding
  is enabled; `devbox agent install` can also install it in advance. It runs as a user
  systemd service on the fixed socket ~/.ssh-agent/docker-agent.sock, which
  survives host reboots and logins.
- Per-container proxy (legacy containers): a small relay on
  ~/docker_mount/<container>/ssh-agent/agent.sock that forwards to whatever
  SSH_AUTH_SOCK was when it started; `devbox agent refresh` restarts it.

Run directly, this file is the proxy process itself:
    ssh_agent.py <listen-socket> <target-ssh-auth-sock>
It must stay free of package-relative imports for that reason.
"""

import os
import signal
import socket
import subprocess
import sys
import threading
import time


SSH_AGENT_PROXY_SOCKET = "agent.sock"
SSH_AGENT_PROXY_PID = "agent-proxy.pid"
SSH_AGENT_PROXY_LOG = "agent-proxy.log"
PERSISTENT_SSH_AGENT_SOCKET = "docker-agent.sock"
PERSISTENT_SSH_AGENT_SERVICE = "docker-ssh-agent.service"
# Load order for the persistent agent. Containers offer agent keys in this order,
# so a service such as GitHub sees the Ed25519 key before an older key of the
# same user. Types not listed (DSA, which OpenSSH 10 cannot load) are skipped.
AGENT_KEY_TYPES = (
    ("ssh-ed25519", "sk-ssh-ed25519@openssh.com"),
    ("ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "sk-ecdsa-sha2-nistp256@openssh.com"),
    ("ssh-rsa",),
)


# Paths are resolved on each call, not at import, so they follow $HOME.
def persistent_agent_dir():
    return os.path.expanduser("~/.ssh-agent")


def container_agent_dir(container_name):
    return os.path.expanduser(f"~/docker_mount/{container_name}/ssh-agent")


# --- Persistent agent ------------------------------------------------------

def install_persistent_agent(_args=None):
    if os.geteuid() == 0:
        raise SystemExit("Run this as your normal user, not with sudo.")

    agent_dir = persistent_agent_dir()
    agent_sock = os.path.join(agent_dir, PERSISTENT_SSH_AGENT_SOCKET)
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    unit_path = os.path.join(unit_dir, PERSISTENT_SSH_AGENT_SERVICE)

    os.makedirs(agent_dir, exist_ok=True)
    os.makedirs(unit_dir, exist_ok=True)
    os.chmod(agent_dir, 0o700)

    with open(unit_path, "w", encoding="utf-8") as unit_file:
        unit_file.write(f"""[Unit]
Description=Persistent SSH agent for Docker dev containers
Documentation=man:ssh-agent(1)

[Service]
Type=simple
Environment=SSH_AUTH_SOCK={agent_sock}
ExecStartPre=/usr/bin/rm -f {agent_sock}
ExecStart=/usr/bin/ssh-agent -D -a {agent_sock}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
""")

    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", PERSISTENT_SSH_AGENT_SERVICE], check=True)

    print(f"Persistent Docker SSH agent socket: {agent_sock}")
    print("Add keys to it with:")
    print(f"  SSH_AUTH_SOCK={agent_sock} ssh-add ~/.ssh/id_ed25519")
    print()
    print("Optional: start this user service at boot before login:")
    print(f"  sudo loginctl enable-linger {os.environ.get('USER', '$USER')}")
    return 0


def host_private_keys():
    """Private keys in ~/.ssh that have a matching .pub, in AGENT_KEY_TYPES order, then by name."""
    ssh_dir = os.path.expanduser("~/.ssh")
    ranked = []
    for filename in sorted(os.listdir(ssh_dir)) if os.path.isdir(ssh_dir) else ():
        private_key = os.path.join(ssh_dir, filename.removesuffix(".pub"))
        if not filename.endswith(".pub") or not os.path.isfile(private_key):
            continue
        with open(os.path.join(ssh_dir, filename), "r", encoding="utf-8") as public_key:
            key_type = next(iter(public_key.read().split()), "")
        for rank, key_types in enumerate(AGENT_KEY_TYPES):
            if key_type in key_types:
                ranked.append((rank, private_key))
    return [private_key for _, private_key in sorted(ranked)]


def ensure_persistent_agent():
    """Install/start the container agent and interactively load keys when empty."""
    agent_sock = os.path.join(persistent_agent_dir(), PERSISTENT_SSH_AGENT_SOCKET)
    agent_env = dict(os.environ, SSH_AUTH_SOCK=agent_sock)
    unit_path = os.path.expanduser(f"~/.config/systemd/user/{PERSISTENT_SSH_AGENT_SERVICE}")

    def key_status():
        try:
            return subprocess.run(
                ["ssh-add", "-l"], env=agent_env, check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ).returncode
        except FileNotFoundError:
            raise SystemExit("ssh-add is not installed; install the host OpenSSH client first.")

    if not os.path.isfile(unit_path):
        print("Installing the persistent host SSH agent...")
        install_persistent_agent()
    elif key_status() not in (0, 1):
        os.makedirs(persistent_agent_dir(), mode=0o700, exist_ok=True)
        subprocess.run(["systemctl", "--user", "start", PERSISTENT_SSH_AGENT_SERVICE], check=True)

    # systemctl may return before ssh-agent has opened its socket. ssh-add -l
    # returns 0 for loaded keys, 1 for no keys, and 2 for an unreachable agent.
    for _ in range(50):
        status = key_status()
        if status == 0:
            return
        if status == 1:
            # Plain `ssh-add` would load only the default names, RSA first, and
            # exits 1 without a word when none exist.
            private_keys = host_private_keys()
            if not private_keys:
                print(f"Warning: no private key with a matching .pub in ~/.ssh; the persistent agent stays empty. "
                      f"Load one later with `SSH_AUTH_SOCK={agent_sock} ssh-add <key>`, or connect to the "
                      f"container with `ssh -A` if the key is only on another machine.")
                return
            names = ", ".join(os.path.basename(private_key) for private_key in private_keys)
            print(f"Loading {names} into the persistent agent; enter a key passphrase if prompted.")
            subprocess.run(["ssh-add", *private_keys], env=agent_env, check=True)
            return
        time.sleep(0.1)
    raise SystemExit(
        f"Persistent SSH agent is not ready at {agent_sock}; "
        f"check `systemctl --user status {PERSISTENT_SSH_AGENT_SERVICE}`."
    )


# --- Per-container proxy ---------------------------------------------------

def stop_proxy(agent_dir):
    pid_path = os.path.join(agent_dir, SSH_AGENT_PROXY_PID)
    try:
        with open(pid_path, "r", encoding="utf-8") as pid_file:
            pid = int(pid_file.read().strip())
    except (OSError, ValueError):
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        print(f"Warning: cannot stop previous SSH agent proxy process {pid}; replacing socket path anyway.")

    try:
        os.remove(pid_path)
    except FileNotFoundError:
        pass


def start_proxy(agent_dir, target_sock):
    """Start a background proxy in agent_dir; return its socket or raise RuntimeError."""
    os.makedirs(agent_dir, exist_ok=True)
    os.chmod(agent_dir, 0o700)

    listen_sock = os.path.join(agent_dir, SSH_AGENT_PROXY_SOCKET)
    log_path = os.path.join(agent_dir, SSH_AGENT_PROXY_LOG)

    stop_proxy(agent_dir)
    if os.path.exists(listen_sock):
        if os.path.isdir(listen_sock):
            raise RuntimeError(f"{listen_sock} is a directory")
        os.remove(listen_sock)

    with open(log_path, "ab") as log_file:
        process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), listen_sock, target_sock],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )

    for _ in range(20):
        if os.path.exists(listen_sock):
            with open(os.path.join(agent_dir, SSH_AGENT_PROXY_PID), "w", encoding="utf-8") as pid_file:
                pid_file.write(f"{process.pid}\n")
            return listen_sock
        if process.poll() is not None:
            break
        time.sleep(0.05)

    raise RuntimeError(f"SSH agent proxy did not start; see {log_path}")


def host_ssh_auth_sock():
    """Return the resolved host SSH_AUTH_SOCK, or None if it is unset or missing."""
    ssh_auth_sock = os.environ.get("SSH_AUTH_SOCK")
    if not ssh_auth_sock:
        return None
    ssh_auth_sock = os.path.realpath(ssh_auth_sock)
    return ssh_auth_sock if os.path.exists(ssh_auth_sock) else None


def refresh_proxy(args):
    ssh_auth_sock = host_ssh_auth_sock()
    if not ssh_auth_sock:
        print("SSH_AUTH_SOCK is not set in this shell or does not exist.", file=sys.stderr)
        return 1

    listen_sock = start_proxy(container_agent_dir(args.container), ssh_auth_sock)
    print(f"Refreshed SSH agent proxy: {listen_sock}")
    print(f"Containers created by `devbox create` use SSH_AUTH_SOCK=/ssh-agent/{SSH_AGENT_PROXY_SOCKET}.")
    return 0


# --- Proxy process -----------------------------------------------------------

def close_socket(sock):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def relay(source, destination):
    try:
        while True:
            data = source.recv(65536)
            if not data:
                break
            destination.sendall(data)
    except OSError:
        pass
    finally:
        close_socket(source)
        close_socket(destination)


def handle_client(client, target_path):
    upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        upstream.connect(target_path)
    except OSError as exc:
        print(f"failed to connect to upstream SSH agent {target_path}: {exc}", file=sys.stderr)
        close_socket(client)
        close_socket(upstream)
        return

    threading.Thread(target=relay, args=(client, upstream), daemon=True).start()
    threading.Thread(target=relay, args=(upstream, client), daemon=True).start()


def serve(listen_path, target_path):
    listen_path = os.path.abspath(listen_path)
    target_path = os.path.abspath(target_path)
    os.makedirs(os.path.dirname(listen_path), exist_ok=True)

    if os.path.exists(listen_path):
        if os.path.isdir(listen_path):
            print(f"listen path is a directory: {listen_path}", file=sys.stderr)
            return 1
        os.remove(listen_path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def cleanup(_signum=None, _frame=None):
        close_socket(server)
        try:
            os.remove(listen_path)
        except FileNotFoundError:
            pass
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    server.bind(listen_path)
    os.chmod(listen_path, 0o600)
    server.listen(128)

    while True:
        try:
            client, _ = server.accept()
        except OSError:
            cleanup()
        threading.Thread(target=handle_client, args=(client, target_path), daemon=True).start()


# --- CLI -------------------------------------------------------------------

def add_parser(subparsers):
    parser = subparsers.add_parser(
        "agent", help="Forward the host SSH agent into containers",
        description="Manage the host SSH agent that containers use for git over SSH.",
    )
    actions = parser.add_subparsers(dest="agent_command", required=True, metavar="ACTION")

    install = actions.add_parser(
        "install", help="Install the persistent host agent (user systemd service; one-time)",
    )
    install.set_defaults(func=install_persistent_agent)

    refresh = actions.add_parser(
        "refresh", help="Restart a container's fallback proxy after the host SSH_AUTH_SOCK changed",
    )
    refresh.add_argument("container", help="container name")
    refresh.set_defaults(func=refresh_proxy)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <listen-socket> <target-ssh-auth-sock>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(serve(sys.argv[1], sys.argv[2]))
