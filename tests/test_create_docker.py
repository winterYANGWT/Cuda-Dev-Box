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

# Replies per prompt, keyed by a phrase from the prompt text and consumed in
# order; a rejected reply re-asks the same prompt and takes the next one.
DEFAULT_ANSWERS = {
    "Image tag": [""],
    "container name": ["test-dev"],
    "Host start port": [""],
    # The first two fall inside the default dev range 8000-8099 and are re-asked.
    "port 22": ["8000", "8099", "2222"],
    "password login": ["n"],
    # 64 does not fit the stubbed 14.3 GiB host and is re-asked.
    "RAM profile": ["64", "16"],
    "GPU passthrough": ["y"],
    "SSH agent": ["n"],
    "sandbox": ["n"],
    "privileged": ["n"],
    "kernel development": ["y"],
}


class CreateDockerTests(unittest.TestCase):
    def run_creation(self, fail_command=None, *, answers=None, with_keys=False, gitconfig=True,
                     gpu_toolkit=True):
        commands = []
        self.commands = commands
        self.prompts = prompts = []

        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == fail_command:
                raise subprocess.CalledProcessError(1, command)
            if command[:3] == ["docker", "image", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout="latest\n202608\n202609\n<none>\n")
            return subprocess.CompletedProcess(command, 0, stdout="test-container\n")

        replies = {key: list(values) for key, values in {**DEFAULT_ANSWERS, **(answers or {})}.items()}

        def reply(prompt):
            prompts.append(prompt)
            key = next(key for key in replies if key in prompt)
            return replies[key].pop(0)

        # The Docker boundary is stubbed: this exercises the actual interactive
        # script and filesystem setup without creating a real container.
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            if gitconfig:
                (home / ".gitconfig").touch()
            if with_keys:
                (home / ".ssh").mkdir()
                (home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAAhost host\n")
                # Keys that log in to the host (e.g. from a laptop); the restricted one is skipped.
                (home / ".ssh/authorized_keys").write_text(
                    "ssh-ed25519 AAAAlaptop laptop\nfrom=\"10.0.0.1\" ssh-ed25519 AAAArestricted restricted\n"
                )
                existing_ssh = home / "docker_mount/test-dev/.ssh"
                existing_ssh.mkdir(parents=True)
                (existing_ssh / "authorized_keys").write_text("ssh-ed25519 AAAAremote remote\n")
            expanduser = os.path.expanduser
            path_exists = os.path.exists

            def expand(path):
                return str(home / path[2:]) if path.startswith("~/") else expanduser(path)

            with (
                patch("builtins.input", side_effect=reply),
                patch("builtins.print"),
                patch("sys.stderr", new_callable=io.StringIO),
                patch("os.path.expanduser", side_effect=expand),
                patch("os.path.exists", side_effect=lambda path: True if path == "/dev/nvidiactl" else path_exists(path)),
                patch("subprocess.run", side_effect=run),
                patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if gpu_toolkit else None),
                patch("devbox_lib.container.CDI_SPEC_DIRS", ()),
                patch("devbox_lib.memory.read_memtotal_kib", return_value=14991536),
            ):
                if fail_command:
                    with self.assertRaises(subprocess.CalledProcessError):
                        DEVBOX_MAIN(["create"])
                else:
                    self.assertEqual(DEVBOX_MAIN(["create"]), 0)
            self.written_keys = (home / "docker_mount/test-dev/.ssh/authorized_keys").read_text()
        return commands

    def created(self, commands):
        return next(command for command in commands if command[:2] == ["docker", "create"])

    def port_bindings(self, create):
        return [create[i + 1] for i, value in enumerate(create) if value == "-p"]

    def test_creation_uses_profile_and_all_ports_without_ssh_collision(self):
        create = self.created(self.run_creation())
        self.assertEqual(self.port_bindings(create), ["2222:22", "8000-8099:8000-8099/tcp"])
        self.assertEqual(create[create.index("--memory") + 1], "10g")
        self.assertEqual(create[create.index("--memory-swap") + 1], "12g")
        self.assertIn("memlock=-1:-1", create)
        self.assertIn("--ipc=host", create)
        self.assertNotIn("--memory-swappiness", create)  # unsupported on cgroup v2

    def test_create_takes_no_command_line_options(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            DEVBOX_MAIN(["create", "--no-gpu"])

    def test_image_defaults_to_newest_monthly_tag(self):
        self.assertIn("cuda-devbox:202609", self.created(self.run_creation()))

    def test_image_tag_choice_rejects_missing_tags(self):
        create = self.created(self.run_creation(answers={"Image tag": ["201901", "202608"]}))
        self.assertIn("cuda-devbox:202608", create)

    def test_image_prompt_accepts_a_full_image_name(self):
        create = self.created(self.run_creation(answers={"Image tag": ["dev:ubuntu:26.04", "ubuntu-dev:26.04"]}))
        self.assertIn("ubuntu-dev:26.04", create)

    def test_start_port_prompt_maps_100_ports_onto_container_8000(self):
        commands = self.run_creation(answers={
            # Re-asked: a range past 65535, one covering port 22, and a range instead of a start.
            "Host start port": ["65500", "1", "62000-62099", "62000"],
            # Re-asked: inside the dev range 62000-62099.
            "port 22": ["62050", "61000"],
        })
        self.assertEqual(self.port_bindings(self.created(commands)), ["61000:22", "62000-62099:8000-8099/tcp"])

    def test_gpu_prompt_controls_passthrough(self):
        self.assertIn("--gpus", self.created(self.run_creation()))
        self.assertNotIn("--gpus", self.created(self.run_creation(answers={"GPU passthrough": ["n"]})))

    def test_gpu_is_not_offered_when_docker_cannot_use_it(self):
        # Driver present but no NVIDIA Container Toolkit: `docker start` would fail on --gpus.
        create = self.created(self.run_creation(gpu_toolkit=False))
        self.assertNotIn("--gpus", create)
        self.assertFalse(any("GPU passthrough" in prompt for prompt in self.prompts))

    def test_port_binding_or_password_failure_removes_only_new_container(self):
        for failing in (["docker", "start"], ["docker", "exec"]):
            with self.subTest(failing=failing):
                commands = self.run_creation(failing)
                self.assertIn(["docker", "rm", "-f", "test-container"], commands)

    def test_creation_failure_does_not_remove_existing_container(self):
        commands = self.run_creation(["docker", "create"])
        self.assertFalse(any(command[:2] == ["docker", "rm"] for command in commands))

    def test_key_only_preserves_existing_login_and_skips_missing_gitconfig(self):
        commands = self.run_creation(answers={"password login": ["y"]}, with_keys=True, gitconfig=False)
        create = self.created(commands)
        self.assertIn("PasswordAuthentication=no", create)
        self.assertIn("KbdInteractiveAuthentication=no", create)
        self.assertIn("PermitRootLogin=prohibit-password", create)
        self.assertFalse(any(command[-1] == "chpasswd" for command in commands))
        self.assertFalse(any(":/root/.gitconfig" in argument for argument in create))
        self.assertEqual(set(self.written_keys.splitlines()), {"ssh-ed25519 AAAAhost host", "ssh-ed25519 AAAAlaptop laptop", "ssh-ed25519 AAAAremote remote"})

    def test_key_only_without_public_keys_refuses_to_create_container(self):
        with self.assertRaisesRegex(SystemExit, "public key"):
            self.run_creation(answers={"password login": ["y"]})
        self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))


if __name__ == "__main__":
    unittest.main()
