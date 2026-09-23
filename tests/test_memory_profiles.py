import importlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


HOST_DIR = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST_DIR))


class MemoryConfigurationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Profiles and host settings live in the same module.
        cls.profiles = cls.host = importlib.import_module("devbox_lib.memory")

    def test_auto_takes_the_largest_profile_that_fits_usable_ram(self):
        # Needed: 8 -> 6.625, 16 -> 13.25, 32 -> 26.5, 64 -> 55 GiB, so a 16 GiB
        # host with a large integrated-GPU carve-out (13.2) gets 8.
        for usable_gib, expected in ((6.7, "8"), (13.2, "8"), (13.3, "16"), (14.3, "16"), (27, "32"), (62, "64")):
            with self.subTest(usable_gib=usable_gib):
                profile = self.profiles.select_profile("auto", int(usable_gib * 1024**2))
                self.assertEqual(profile.name, expected)

    def test_future_ram_profile_is_rejected_on_current_hardware(self):
        with self.assertRaisesRegex(ValueError, "RAM"):
            self.profiles.select_profile("64", 14991536)

    def test_low_memory_host_is_rejected_instead_of_overcommitted(self):
        with self.assertRaisesRegex(ValueError, r"smallest \(8 GiB\) profile needs at least 6\.625 GiB"):
            self.profiles.select_profile("auto", 6 * 1024**2)

    def test_docker_swap_flag_is_ram_plus_swap_not_swap_alone(self):
        for name, ram, expected in (
            ("8", 8, ["--memory", "4g", "--memory-swap", "6g"]),
            ("16", 16, ["--memory", "10g", "--memory-swap", "12g"]),
            ("32", 32, ["--memory", "22g", "--memory-swap", "26g"]),
            ("64", 64, ["--memory", "48g", "--memory-swap", "52g"]),
        ):
            with self.subTest(name=name):
                profile = self.profiles.select_profile(name, ram * 1024**2)
                self.assertEqual(profile.docker_args(), expected)

    def test_grub_replaces_old_reservations_and_preserves_other_options(self):
        profile = self.profiles.select_profile("16", 14991536)
        content = self.host.render_configuration(profile, numa_nodes=1)
        grub = content["etc/default/grub.d/99-ubuntubase-memory.cfg"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "memory.cfg"
            path.write_text(grub)
            script = '''
GRUB_CMDLINE_LINUX='root=UUID=abcd cma=1G hugepagesz=1G hugepages=4 default_hugepagesz=1G'
GRUB_CMDLINE_LINUX_DEFAULT='quiet splash transparent_hugepage=always cma=512M crashkernel=512M'
. "$1"
. "$1"
printf '%s\n%s\n' "$GRUB_CMDLINE_LINUX" "$GRUB_CMDLINE_LINUX_DEFAULT"
'''
            result = subprocess.run(["sh", "-c", script, "sh", str(path)], text=True, capture_output=True, check=True)
        linux, default = result.stdout.splitlines()
        self.assertEqual(linux.split(), ["root=UUID=abcd", "cma=256M", "default_hugepagesz=2M", "hugepagesz=2M", "hugepages=512", "transparent_hugepage=madvise"])
        self.assertEqual(default.split(), ["quiet", "splash", "crashkernel=512M"])

    def test_numa_policy_tracks_real_node_count(self):
        profile = self.profiles.select_profile("16", 14991536)
        for nodes, expected in ((1, "0"), (2, "1")):
            content = self.host.render_configuration(profile, numa_nodes=nodes)
            sysctl = content["etc/sysctl.d/90-ubuntubase-memory.conf"]
            values = dict(line.split("=", 1) for line in sysctl.splitlines() if line and not line.startswith("#"))
            self.assertEqual(values["kernel.numa_balancing"].strip(), expected)
            self.assertEqual(values["vm.nr_hugepages"].strip(), "512")
            self.assertEqual(values["vm.swappiness"].strip(), "10")

    def test_node_ranges_are_counted_including_sparse_nodes(self):
        self.assertEqual(self.host.count_nodes("0-1,4,6-7"), 5)

    def test_export_writes_only_configuration_without_running_system_commands(self):
        profile = self.profiles.select_profile("16", 14991536)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.host.write_configuration(root, self.host.render_configuration(profile, 1))
            files = sorted(str(path.relative_to(root)) for path in root.rglob("*") if path.is_file())
            self.assertEqual(files, ["etc/default/grub.d/99-ubuntubase-memory.cfg", "etc/sysctl.d/90-ubuntubase-memory.conf"])
            for filename in files:
                self.assertEqual((root / filename).stat().st_mode & 0o777, 0o644)

    def test_failed_grub_update_restores_boot_file_settings_and_modes(self):
        profile = self.profiles.select_profile("16", 14991536)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grub = root / "boot/grub/grub.cfg"
            grub.parent.mkdir(parents=True)
            grub.write_text("original boot configuration\n")
            grub.chmod(0o600)
            existing = root / "etc/sysctl.d/90-ubuntubase-memory.conf"
            existing.parent.mkdir(parents=True)
            existing.write_text("vm.swappiness=60\n")
            existing.chmod(0o640)
            updater = root / "update-grub"
            updater.write_text(f"#!/bin/sh\nprintf 'broken boot config' > '{grub}'\nexit 1\n")
            updater.chmod(0o755)
            with self.assertRaises(subprocess.CalledProcessError):
                self.host.install_configuration(root, self.host.render_configuration(profile, 1), str(updater))
            self.assertEqual(grub.read_text(), "original boot configuration\n")
            self.assertEqual(grub.stat().st_mode & 0o777, 0o600)
            self.assertEqual(existing.read_text(), "vm.swappiness=60\n")
            self.assertEqual(existing.stat().st_mode & 0o777, 0o640)
            self.assertFalse((root / "etc/default/grub.d/99-ubuntubase-memory.cfg").exists())

    def test_successful_grub_update_preserves_previous_boot_file(self):
        profile = self.profiles.select_profile("16", 14991536)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            grub = root / "boot/grub/grub.cfg"
            grub.parent.mkdir(parents=True)
            grub.write_text("original\n")
            updater = root / "update-grub"
            updater.write_text(f"#!/bin/sh\nprintf 'new boot config' > '{grub}'\n")
            updater.chmod(0o755)
            self.host.install_configuration(root, self.host.render_configuration(profile, 1), str(updater))
            self.assertEqual(grub.read_text(), "new boot config")
            self.assertEqual((grub.parent / "grub.cfg.ubuntubase-previous").read_text(), "original\n")


if __name__ == "__main__":
    unittest.main()
