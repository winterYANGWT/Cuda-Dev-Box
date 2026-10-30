import io
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


HOST_DIR = Path(__file__).resolve().parents[1] / "host"
sys.path.insert(0, str(HOST_DIR))
DEVBOX_MAIN = runpy.run_path(str(HOST_DIR / "devbox"))["main"]

HOST_PUB = "ssh-ed25519 AAAAhost winter@host\n"
LAPTOP = "ssh-ed25519 AAAAlaptop me@laptop"


class DevboxKeyTests(unittest.TestCase):
    def run_key(self, *argv, paste=None, existing="dev\n", added=None, authorized_keys=None, legacy_added=None):
        """Run `devbox key ...` against a fake home; returns (exit code or SystemExit, installed key files)."""
        installed = []

        def run(command, **kwargs):
            if command[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(command, 0, stdout=existing)
            if command[:2] == ["docker", "cp"]:
                installed.append((command[-1], Path(command[-2]).read_text()))
                return subprocess.CompletedProcess(command, 0)
            if command[0] == "ssh-keygen":
                # Stands in for the real check: test keys are valid when their data starts with AAAA.
                data = kwargs["input"].split()[1]
                if not data.startswith("AAAA"):
                    return subprocess.CompletedProcess(command, 255, stdout="")
                return subprocess.CompletedProcess(command, 0, stdout=f"256 SHA256:{data} comment (ED25519)\n")
            raise AssertionError(f"unexpected command {command}")

        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".ssh").mkdir()
            (home / ".ssh/id_ed25519.pub").write_text(HOST_PUB)
            if authorized_keys is not None:
                (home / ".ssh/authorized_keys").write_text(authorized_keys)
            added_path = home / "docker_mount/.devbox/dev/ssh-authorized-keys"
            for path, content in ((added_path, added), (home / "docker_mount/dev/ssh-authorized-keys", legacy_added)):
                if content is not None:
                    path.parent.mkdir(parents=True)
                    path.write_text(content)
            expanduser = os.path.expanduser
            with (
                patch("os.path.expanduser",
                      side_effect=lambda path: str(home / path[2:]) if path.startswith("~/") else expanduser(path)),
                patch("subprocess.run", side_effect=run),
                patch("builtins.input", return_value=paste or ""),
                patch("builtins.print") as printed,
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                try:
                    result = DEVBOX_MAIN(["key", *argv])
                except SystemExit as error:
                    result = error
            self.output = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
            self.added = added_path.read_text() if added_path.exists() else None
            self.added_mode = added_path.stat().st_mode & 0o777 if added_path.exists() else None
        return result, installed

    def test_add_stores_the_key_and_applies_host_plus_added_keys_to_the_box(self):
        result, installed = self.run_key("add", "dev", paste=f"  {LAPTOP}  ")
        self.assertEqual(result, 0)
        self.assertEqual(self.added, LAPTOP + "\n")
        self.assertEqual(self.added_mode, 0o600)
        self.assertEqual(installed, [("dev:/root/.ssh/authorized_keys", HOST_PUB + LAPTOP + "\n")])

    def test_add_keeps_earlier_added_keys_and_skips_an_already_authorized_key(self):
        result, installed = self.run_key("add", "dev", paste=LAPTOP, added="ssh-ed25519 AAAAphone phone\n")
        self.assertEqual(result, 0)
        self.assertEqual(self.added, f"ssh-ed25519 AAAAphone phone\n{LAPTOP}\n")
        # Same key data under another comment: already authorized from the host's own .pub.
        result, installed = self.run_key("add", "dev", paste="ssh-ed25519 AAAAhost other comment")
        self.assertEqual(result, 0)
        self.assertIsNone(self.added)
        self.assertIn("already authorized", self.output)
        self.assertEqual(installed, [("dev:/root/.ssh/authorized_keys", HOST_PUB)])

    def test_keys_added_by_an_older_devbox_are_kept(self):
        # They were kept in the box's home, which is now mounted as /root.
        result, installed = self.run_key("add", "dev", paste=LAPTOP, legacy_added="ssh-ed25519 AAAAphone phone\n")
        self.assertEqual(result, 0)
        self.assertEqual(self.added, f"ssh-ed25519 AAAAphone phone\n{LAPTOP}\n")
        self.assertEqual(installed, [("dev:/root/.ssh/authorized_keys", f"{HOST_PUB}ssh-ed25519 AAAAphone phone\n{LAPTOP}\n")])

    def test_add_before_create_stores_the_key_for_create(self):
        result, installed = self.run_key("add", "new-box", paste=LAPTOP, existing="dev\n")
        self.assertEqual(result, 0)
        self.assertEqual(installed, [])
        self.assertIn("create", self.output)

    def test_add_refuses_private_keys_and_anything_but_a_plain_public_key(self):
        for paste in ("-----BEGIN OPENSSH PRIVATE KEY-----", "", "hello",
                      f'from="10.0.0.1" {LAPTOP}', "ssh-ed25519 BROKEN truncated"):
            with self.subTest(paste=paste):
                result, installed = self.run_key("add", "dev", paste=paste)
                self.assertIsInstance(result, SystemExit)
                self.assertIsNone(self.added)
                self.assertEqual(installed, [])

    def test_container_name_cannot_leave_docker_mount(self):
        for name in ("..", "../x", "a"):
            with self.subTest(name=name):
                result, installed = self.run_key("add", name, paste=LAPTOP)
                self.assertRegex(str(result), "Invalid container name")
                self.assertEqual(installed, [])

    def test_list_shows_each_key_with_its_source(self):
        result, _ = self.run_key("list", "dev", added=LAPTOP + "\n", authorized_keys="ssh-rsa AAAAwork work\n")
        self.assertEqual(result, 0)
        lines = self.output.splitlines()
        self.assertRegex(lines[0], r"^SHA256:AAAAwork  work  \[.*/\.ssh/authorized_keys\]$")
        self.assertRegex(lines[1], r"^SHA256:AAAAhost  winter@host  \[.*/\.ssh/id_ed25519\.pub\]$")
        self.assertEqual(lines[2], "SHA256:AAAAlaptop  me@laptop  [added]")

    def test_remove_revokes_an_added_key(self):
        result, installed = self.run_key("remove", "dev", "SHA256:AAAAlaptop", added=f"{LAPTOP}\nssh-ed25519 AAAAphone phone\n")
        self.assertEqual(result, 0)
        self.assertEqual(self.added, "ssh-ed25519 AAAAphone phone\n")
        self.assertEqual(installed, [("dev:/root/.ssh/authorized_keys", HOST_PUB + "ssh-ed25519 AAAAphone phone\n")])

    def test_remove_points_host_keys_to_their_file_and_resyncs_once_deleted_there(self):
        result, installed = self.run_key("remove", "dev", "SHA256:AAAAhost")
        self.assertRegex(str(result), r"\.ssh/id_ed25519\.pub on the host")
        self.assertEqual(installed, [])
        # After it was deleted from the host, the same command refreshes the box.
        result, installed = self.run_key("remove", "dev", "SHA256:AAAAgone", added=LAPTOP + "\n")
        self.assertEqual(result, 0)
        self.assertEqual(installed, [("dev:/root/.ssh/authorized_keys", HOST_PUB + LAPTOP + "\n")])


if __name__ == "__main__":
    unittest.main()
