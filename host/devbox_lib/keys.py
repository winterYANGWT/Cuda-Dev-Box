"""Manage the SSH public keys a box authorizes: `devbox key add/list/remove`."""

import os
import subprocess

from . import container


def key_fingerprint(key):
    """SHA256 fingerprint, or None when ssh-keygen does not accept the key."""
    try:
        result = subprocess.run(
            ["ssh-keygen", "-l", "-E", "sha256", "-f", "-"],
            input=f"{key}\n", capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        raise SystemExit("ssh-keygen is not installed; install the host OpenSSH client first.")
    return result.stdout.split()[1] if result.returncode == 0 else None


def checked_container_name(name):
    # The name becomes a path under ~/docker_mount, so it gets create's rule.
    if not container.CONTAINER_NAME.fullmatch(name):
        raise SystemExit(f"Invalid container name: {name!r}.")
    return name


def apply_to_box(container_name):
    """Rewrite the box's authorized_keys; a box not created yet gets them at create."""
    if container_name not in container.existing_container_names():
        print(f"Container {container_name} does not exist yet; `host/devbox create` authorizes these keys.")
        return
    public_keys = [key for key, _ in container.box_key_sources(container_name)]
    container.install_authorized_keys(container_name, public_keys)
    print(f"{container_name} now authorizes {len(public_keys)} key(s).")


def write_added_keys(container_name, public_keys):
    path = container.added_keys_path(container_name)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as keys_file:
        keys_file.write("".join(f"{key}\n" for key in public_keys))


def read_added_keys(container_name):
    path = container.added_keys_path(container_name)
    return container.public_keys_in(path) if os.path.isfile(path) else []


def add_key(args):
    name = checked_container_name(args.container)
    print("Paste the public key of the machine that will log in, as one line "
          "(for example the contents of ~/.ssh/id_ed25519.pub on that machine):")
    key = input().strip()
    if "PRIVATE KEY" in key:
        raise SystemExit("That is a private key, which must never leave its machine. Paste the .pub file instead.")
    if not container.is_open_ssh_public_key(key):
        raise SystemExit("Not an OpenSSH public key line such as `ssh-ed25519 AAAA... comment` (options are not accepted).")
    fingerprint = key_fingerprint(key)
    if not fingerprint:
        raise SystemExit("ssh-keygen does not accept this public key; check that it was pasted completely.")

    for existing, path in container.box_key_sources(name):
        if container.key_identity(existing) == container.key_identity(key):
            print(f"{fingerprint} is already authorized (from {path}).")
            apply_to_box(name)
            return 0
    write_added_keys(name, [*read_added_keys(name), key])
    print(f"Added {fingerprint}.")
    apply_to_box(name)
    return 0


def list_keys(args):
    name = checked_container_name(args.container)
    added_path = container.added_keys_path(name)
    sources = container.box_key_sources(name)
    if not sources:
        print(f"No keys; add one with `host/devbox key add {name}`.")
    for key, path in sources:
        comment = " ".join(key.split()[2:]) or "-"
        source = "added" if path == added_path else path
        print(f"{key_fingerprint(key) or '(invalid key)'}  {comment}  [{source}]")
    return 0


def remove_key(args):
    name = checked_container_name(args.container)
    added = read_added_keys(name)
    remaining = [key for key in added if key_fingerprint(key) != args.fingerprint]
    if len(remaining) < len(added):
        write_added_keys(name, remaining)
        print(f"Removed {args.fingerprint}.")
    else:
        for key, path in container.box_key_sources(name):
            if key_fingerprint(key) == args.fingerprint:
                raise SystemExit(f"{args.fingerprint} comes from {path} on the host; "
                                 "delete it there, then run this command again.")
        print(f"{args.fingerprint} is not authorized; refreshing the box's keys from the host.")
    apply_to_box(name)
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "key", help="Manage the SSH public keys a container authorizes",
        description="A box authorizes the host user's ~/.ssh/*.pub, the keys in ~/.ssh/authorized_keys, and the keys "
                    "added here (kept in ~/docker_mount/.devbox/<container>/ssh-authorized-keys). Changes apply at once.",
    )
    actions = parser.add_subparsers(dest="key_command", required=True, metavar="ACTION")

    add = actions.add_parser("add", help="Authorize a public key pasted on standard input")
    add.add_argument("container", help="container name; may not be created yet")
    add.set_defaults(func=add_key)

    list_parser = actions.add_parser("list", help="Show the authorized keys and where each comes from")
    list_parser.add_argument("container", help="container name")
    list_parser.set_defaults(func=list_keys)

    remove = actions.add_parser("remove", help="Revoke a key added with `key add`, or re-sync after editing ~/.ssh")
    remove.add_argument("container", help="container name")
    remove.add_argument("fingerprint", help="SHA256:... as shown by `devbox key list`")
    remove.set_defaults(func=remove_key)
