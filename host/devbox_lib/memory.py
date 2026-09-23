"""Host memory profiles and next-boot host memory settings.

PROFILES is the single source for both the container limits `devbox create`
applies and the HugeTLB/CMA reservations `devbox memory` installs on the host.
"""

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class MemoryProfile:
    name: str
    memory_gib: int
    swap_gib: int
    hugepages: int  # 2 MiB pages
    cma_mib: int

    def docker_args(self):
        return [
            "--memory", f"{self.memory_gib}g",
            "--memory-swap", f"{self.memory_gib + self.swap_gib}g",
        ]

    def minimum_mib(self):
        """Usable RAM the host needs: the container limit, both reservations, and 2 GiB for the host."""
        return self.memory_gib * 1024 + self.hugepages * 2 + self.cma_mib + 2 * 1024


PROFILES = {
    "8": MemoryProfile("8", 4, 2, 256, 128),
    "16": MemoryProfile("16", 10, 2, 512, 256),
    "32": MemoryProfile("32", 22, 4, 1024, 512),
    "64": MemoryProfile("64", 48, 4, 2048, 1024),
}
PROFILE_CHOICES = ("auto", *PROFILES)


def read_memtotal_kib():
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemTotal:"):
            return int(line.split()[1])
    raise ValueError("Cannot determine host RAM from /proc/meminfo")


def select_profile(name, memtotal_kib):
    usable_gib = memtotal_kib / 1024**2
    if name == "auto":
        # MemTotal already excludes firmware, integrated-GPU and kernel reservations,
        # so take the largest profile that fits instead of guessing the RAM size.
        fitting = [profile for profile in PROFILES.values() if memtotal_kib >= profile.minimum_mib() * 1024]
        if not fitting:
            smallest = min(PROFILES.values(), key=MemoryProfile.minimum_mib)
            raise ValueError(f"This host has {usable_gib:.2f} GiB usable RAM; even the smallest ({smallest.name} GiB) "
                             f"profile needs at least {smallest.minimum_mib() / 1024:g} GiB")
        return max(fitting, key=MemoryProfile.minimum_mib)
    if name not in PROFILES:
        raise ValueError(f"Choose a RAM profile: {', '.join(PROFILE_CHOICES)}")
    profile = PROFILES[name]
    if memtotal_kib < profile.minimum_mib() * 1024:
        raise ValueError(f"The {name} GiB profile needs at least {profile.minimum_mib() / 1024:g} GiB usable RAM; "
                         f"this host has {usable_gib:.2f} GiB")
    return profile


def count_nodes(node_list):
    nodes = set()
    for group in node_list.strip().split(","):
        bounds = [int(value) for value in group.split("-")]
        if len(bounds) == 1:
            nodes.add(bounds[0])
        elif len(bounds) == 2 and bounds[0] <= bounds[1]:
            nodes.update(range(bounds[0], bounds[1] + 1))
        else:
            raise ValueError(f"Invalid NUMA node list: {node_list}")
    return len(nodes)


def render_configuration(profile, numa_nodes):
    sysctl = (
        "# Managed by Cuda-Dev-Box `host/devbox memory`; applied at boot.\n"
        "vm.swappiness=10\n"
        f"vm.nr_hugepages={profile.hugepages}\n"
        f"kernel.numa_balancing={int(numa_nodes > 1)}\n"
    )
    # Clean both GRUB variables so earlier settings cannot override this profile.
    # Preserve unrelated boot arguments, including root, encryption and kdump.
    grub = '''# Managed by Cuda-Dev-Box `host/devbox memory`.
_ubuntubase_strip_memory_args() {
    printf '%s\\n' "$1" | sed -E 's/(^|[[:space:]])(cma|default_hugepagesz|hugepagesz|hugepages|transparent_hugepage)=[^[:space:]]+//g'
}
GRUB_CMDLINE_LINUX_DEFAULT="$(_ubuntubase_strip_memory_args "${GRUB_CMDLINE_LINUX_DEFAULT:-}")"
GRUB_CMDLINE_LINUX="$(_ubuntubase_strip_memory_args "${GRUB_CMDLINE_LINUX:-}") ARGUMENTS"
unset -f _ubuntubase_strip_memory_args
'''
    arguments = (
        f"cma={profile.cma_mib}M default_hugepagesz=2M hugepagesz=2M "
        f"hugepages={profile.hugepages} transparent_hugepage=madvise"
    )
    return {
        "etc/default/grub.d/99-ubuntubase-memory.cfg": grub.replace("ARGUMENTS", arguments),
        "etc/sysctl.d/90-ubuntubase-memory.conf": sysctl,
    }


def write_configuration(root, configuration):
    for relative_path, content in configuration.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o644)


def apply_configuration(configuration):
    if os.geteuid() != 0:
        raise ValueError("Use sudo with --apply on the Ubuntu host")
    if Path("/proc/1/comm").read_text().strip() != "systemd":
        raise ValueError("Run --apply on the Ubuntu systemd host, not inside a container or sandbox")
    updater = shutil.which("update-grub")
    if not updater or not Path("/etc/default/grub").is_file():
        raise ValueError("A GRUB-based Ubuntu host with update-grub is required")
    kernel_config = Path(f"/boot/config-{platform.release()}").read_text()
    for option in ("CONFIG_CMA=y", "CONFIG_DMA_CMA=y", "CONFIG_HUGETLBFS=y", "CONFIG_NUMA_BALANCING=y"):
        if option not in kernel_config.splitlines():
            raise ValueError(f"Current kernel is missing {option}")

    install_configuration(Path("/"), configuration, updater)
    print("Installed settings for the next boot. Reboot when convenient; no reboot or live memory change was performed.")


def install_configuration(root, configuration, updater):
    boot_config = root / "boot/grub/grub.cfg"
    boot_backup = boot_config.with_name("grub.cfg.ubuntubase-previous")
    if not boot_config.is_file() or boot_config.is_symlink() or boot_backup.is_symlink():
        raise ValueError("Expected a regular /boot/grub/grub.cfg and a non-symlink backup path")
    shutil.copy2(boot_config, boot_backup)
    previous = {}
    for relative_path in configuration:
        path = root / relative_path
        backup = path.with_suffix(path.suffix + ".previous")
        if path.is_symlink() or backup.is_symlink():
            raise ValueError(f"Refusing to replace symlink at {path} or {backup}")
        previous[relative_path] = backup if path.exists() else None
        if path.exists():
            shutil.copy2(path, backup)
    try:
        write_configuration(root, configuration)
        subprocess.run([updater], check=True)
    except (OSError, subprocess.CalledProcessError):
        failures = []
        for relative_path, backup in previous.items():
            try:
                path = root / relative_path
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    shutil.copy2(backup, path)
            except OSError as error:
                failures.append(str(error))
        try:
            shutil.copy2(boot_backup, boot_config)
        except OSError as error:
            failures.append(str(error))
        if failures:
            raise OSError("Rollback incomplete; restore the .previous files and grub.cfg.ubuntubase-previous before rebooting: " + "; ".join(failures))
        print("Failed to install settings; restored previous settings and generated GRUB file, including file permissions.")
        raise


def run(args):
    try:
        profile = select_profile(args.profile, read_memtotal_kib())
        numa_nodes = count_nodes(Path("/sys/devices/system/node/online").read_text())
        configuration = render_configuration(profile, numa_nodes)
        print(f"Profile {profile.name} GiB: container RAM {profile.memory_gib} GiB + swap {profile.swap_gib} GiB; HugeTLB {profile.hugepages * 2} MiB; CMA {profile.cma_mib} MiB; NUMA nodes {numa_nodes}")
        if args.apply:
            apply_configuration(configuration)
        elif args.output_dir:
            write_configuration(args.output_dir, configuration)
            print(f"Exported configuration to {args.output_dir}. Host settings were not changed.")
        else:
            for relative_path, content in configuration.items():
                print(f"\n# /{relative_path}\n{content}")
            print("Preview only. Use --apply with sudo to install these settings for the next boot.")
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Error: {error}")
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "memory", help="Preview or install host HugeTLB/CMA/NUMA settings (next boot)",
        description="Preview, export, or install Ubuntu host memory settings for the next boot. "
                    "Preview only unless --apply or --output-dir is given.",
    )
    parser.add_argument("--profile", choices=PROFILE_CHOICES, default="auto")
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--apply", action="store_true", help="install host settings and run update-grub; does not reboot")
    action.add_argument("--output-dir", type=Path, help="export configuration under this directory without applying it")
    parser.set_defaults(func=run)
