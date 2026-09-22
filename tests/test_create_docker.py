import csv
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
from devbox_lib import container  # noqa: E402

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
    "GPU": [""],
    "SSH agent": ["n"],
    "sandbox": ["n"],
    "Docker socket": ["n"],
    "kernel development": ["y"],
}


class CreateDockerTests(unittest.TestCase):
    def run_creation(self, fail_command=None, *, answers=None, with_keys=False, gitconfig=True,
                     gpu_toolkit=True, agent_installed=False, agent_states=(1,),
                     image_tags="latest\n202608\n202609\n<none>\n", persistent_files=None,
                     existing_containers="", home_files=None):
        commands = []
        self.commands = commands
        self.prompts = prompts = []
        self.agent_environments = []
        agent_states = list(agent_states)

        def run(command, **kwargs):
            commands.append(command)
            if command[:2] == fail_command:
                raise subprocess.CalledProcessError(1, command)
            if command[:3] == ["docker", "image", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout=image_tags)
            if command[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(command, 0, stdout=existing_containers)
            if command[0] == "ssh-add":
                self.agent_environments.append(kwargs["env"])
                if command == ["ssh-add", "-l"]:
                    state = agent_states.pop(0) if len(agent_states) > 1 else agent_states[0]
                    return subprocess.CompletedProcess(command, state)
                # ssh-add must inherit the terminal for passphrase input.
                self.assertNotIn("stdin", kwargs)
                self.assertNotIn("input", kwargs)
                self.assertTrue(kwargs["check"])
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
            mount_root = home / "docker_mount/test-dev"
            self.mount_root = str(mount_root)
            for relative, content in (persistent_files or {}).items():
                path = mount_root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            unit_path = home / ".config/systemd/user/docker-ssh-agent.service"
            if agent_installed:
                unit_path.parent.mkdir(parents=True)
                unit_path.write_text("existing unit\n")
                (home / ".ssh-agent").mkdir()
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
            for relative, content in (home_files or {}).items():
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
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
                patch("os.geteuid", return_value=1000),
                patch("time.sleep"),
                patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if gpu_toolkit else None),
                patch("devbox_lib.container.CDI_SPEC_DIRS", ()),
                patch("devbox_lib.memory.read_memtotal_kib", return_value=14991536),
            ):
                if fail_command:
                    with self.assertRaises(subprocess.CalledProcessError):
                        DEVBOX_MAIN(["create"])
                else:
                    self.assertEqual(DEVBOX_MAIN(["create"]), 0)
            keys_path = mount_root / ".ssh/authorized_keys"
            self.written_keys = keys_path.read_text() if keys_path.exists() else None
            self.agent_unit = unit_path.read_text() if unit_path.exists() else None
            self.agent_socket = str(home / ".ssh-agent/docker-agent.sock")
            self.persistent_dirs = {
                name: (mount_root / name).stat().st_mode & 0o777
                for name in (".codex", ".claude", ".agents", ".npm", ".npm-global")
                if (mount_root / name).is_dir()
            }
            self.persistent_files = {
                relative: (mount_root / relative).read_text() if (mount_root / relative).exists() else None
                for relative in (persistent_files or {})
            }
            self.home_files = {
                relative: (home / relative).read_text() if (home / relative).exists() else None
                for relative in (home_files or {})
            }
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

    def test_creation_mounts_private_tool_state_and_runtime_npm_directories(self):
        create = self.created(self.run_creation())
        for name in (".codex", ".claude", ".agents", ".npm", ".npm-global"):
            with self.subTest(directory=name):
                self.assertIn(f"{self.mount_root}/{name}:/root/{name}", create)
                self.assertEqual(self.persistent_dirs[name], 0o700)
        # Empty bind mounts must not hide the toolchains installed in the image.
        self.assertFalse(any(arg.endswith((":/root", ":/root/.local", ":/usr/local")) for arg in create))

    def test_recreation_and_failed_start_preserve_tool_state_and_installed_packages(self):
        files = {
            ".codex/config.toml": 'model = "example"\n',
            ".codex/auth.json": '{"test": "dummy-token"}\n',
            ".claude/.claude.json": '{"test": "account-state"}\n',
            ".claude/projects/example/session.jsonl": '{"test": "session"}\n',
            ".agents/skills/example/SKILL.md": "example skill\n",
            ".npm/npmrc": "registry=https://registry.npmjs.org/\n",
            ".npm-global/lib/node_modules/example/package.json": '{"name": "example"}\n',
            ".npm-global/bin/example": "#!/bin/sh\necho example\n",
        }
        for failing in (None, ["docker", "start"]):
            with self.subTest(failing=failing):
                create = self.created(self.run_creation(failing, persistent_files=files))
                self.assertIn(f"{self.mount_root}/.codex:/root/.codex", create)
                self.assertEqual(self.persistent_files, files)

    def test_image_defaults_to_latest_even_with_older_monthly_builds(self):
        self.assertIn("cuda-devbox:latest", self.created(self.run_creation()))

    def test_image_defaults_to_newest_monthly_tag_without_latest(self):
        create = self.created(self.run_creation(image_tags="202608\n202609\n<none>\n"))
        self.assertIn("cuda-devbox:202609", create)

    def test_image_tag_choice_rejects_missing_tags(self):
        create = self.created(self.run_creation(answers={"Image tag": ["201901", "202608"]}))
        self.assertIn("cuda-devbox:202608", create)

    def test_image_prompt_accepts_a_full_image_name(self):
        create = self.created(self.run_creation(answers={"Image tag": ["dev:ubuntu:26.04", "ubuntu-dev:26.04"]}))
        self.assertIn("ubuntu-dev:26.04", create)

    def test_dotdot_container_name_cannot_reach_host_ssh_keys(self):
        # ~/docker_mount/../.ssh is the host's own ~/.ssh.
        host_ssh = {".ssh/id_ed25519": "host private key\n", ".ssh/authorized_keys": "# laptop\n"}
        create = self.created(self.run_creation(answers={"container name": ["..", "test-dev"]}, home_files=host_ssh))
        self.assertEqual(create[create.index("--name") + 1], "test-dev")
        self.assertEqual(self.home_files, host_ssh)

    def test_invalid_container_names_are_reasked(self):
        for invalid in ("", "   ", ".", "..", "../x", "a", "-dev", ".dev", "my dev", "a/b", "dév", "x" * 64):
            with self.subTest(invalid=invalid):
                create = self.created(self.run_creation(answers={"container name": [invalid, " test-dev "]}))
                self.assertEqual(create[create.index("--name") + 1], "test-dev")
                self.assertEqual(sum("container name" in prompt for prompt in self.prompts), 2)

    def test_container_names_follow_docker_rule_up_to_hostname_length(self):
        for name in ("ab", "a1", "My_Box.2-x", "a..b", "x" * 63):
            with self.subTest(name=name):
                self.assertIsNone(container.container_name_error(name, set()))

    def test_existing_container_name_is_reasked_before_touching_its_state(self):
        box_key = {".ssh/id_ed25519": "key generated inside the box\n"}
        create = self.created(self.run_creation(
            answers={"container name": ["test-dev", "test-dev-2"]},
            existing_containers="old-box\ntest-dev,web/test-dev\n",
            persistent_files=box_key,
        ))
        self.assertEqual(create[create.index("--name") + 1], "test-dev-2")
        self.assertEqual(self.persistent_files, box_key)

    def test_start_port_prompt_maps_100_ports_onto_container_8000(self):
        commands = self.run_creation(answers={
            # Re-asked: a range past 65535, one covering port 22, and a range instead of a start.
            "Host start port": ["65500", "1", "62000-62099", "62000"],
            # Re-asked: inside the dev range 62000-62099.
            "port 22": ["62050", "61000"],
        })
        self.assertEqual(self.port_bindings(self.created(commands)), ["61000:22", "62000-62099:8000-8099/tcp"])

    def test_gpu_defaults_to_all_without_an_enable_question(self):
        create = self.created(self.run_creation())
        self.assertEqual(create[create.index("--gpus") + 1], "all")
        self.assertFalse(any("Enable NVIDIA" in prompt for prompt in self.prompts))

    def test_gpu_selection_is_passed_as_one_docker_csv_field(self):
        for answer, expected in (
            ("all", "all"),
            ("0", "device=0"),
            ("0,2", "device=0,2"),
            ("1-3", "device=1,2,3"),
            (" 0, 2-4,3,06 ", "device=0,2,3,4,6"),
        ):
            with self.subTest(answer=answer):
                create = self.created(self.run_creation(answers={"GPU": [answer]}))
                value = create[create.index("--gpus") + 1]
                self.assertEqual(next(csv.reader([value])), [expected])

    def test_gpu_invalid_input_is_reasked(self):
        for invalid in ("n", "-1", "3-1", "0,,2", "1-", "all,0", "a", "1.5", "１"):
            with self.subTest(invalid=invalid):
                create = self.created(self.run_creation(answers={"GPU": [invalid, "2"]}))
                self.assertEqual(next(csv.reader([create[create.index("--gpus") + 1]])), ["device=2"])
                self.assertEqual(sum("GPU" in prompt for prompt in self.prompts), 2)

    def test_gpu_subset_keeps_host_docker_access_without_exposing_all_devices(self):
        create = self.created(self.run_creation(answers={"GPU": ["0,2"], "Docker socket": [""]}))
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", create)
        self.assertNotIn("--privileged", create)
        self.assertIn("--cap-add", create)
        self.assertFalse(any("Enable privileged" in prompt for prompt in self.prompts))

    def test_all_gpus_keeps_optional_privileged_mode(self):
        create = self.created(self.run_creation(answers={"Docker socket": [""]}))
        self.assertIn("--privileged", create)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", create)
        self.assertNotIn("--cap-add", create)

    def test_gpu_is_not_offered_when_docker_cannot_use_it(self):
        # Driver present but no NVIDIA Container Toolkit: `docker start` would fail on --gpus.
        create = self.created(self.run_creation(gpu_toolkit=False))
        self.assertNotIn("--gpus", create)
        self.assertFalse(any("GPU" in prompt for prompt in self.prompts))

    def test_forwarding_installs_agent_and_loads_keys_into_its_socket(self):
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_states=(2, 1))
        self.assertIn(["systemctl", "--user", "enable", "--now", "docker-ssh-agent.service"], commands)
        self.assertIn("ExecStart=/usr/bin/ssh-agent -D -a", self.agent_unit)
        self.assertIn(["ssh-add"], commands)
        self.assertLess(commands.index(["ssh-add"]), commands.index(self.created(commands)))
        self.assertTrue(all(env["SSH_AUTH_SOCK"] == self.agent_socket for env in self.agent_environments))
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock", self.created(commands))

    def test_forwarding_reuses_loaded_agent_without_reinstalling_or_readding_keys(self):
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_installed=True, agent_states=(0,))
        self.assertEqual(self.agent_unit, "existing unit\n")
        self.assertFalse(any(command[0] == "systemctl" for command in commands))
        self.assertNotIn(["ssh-add"], commands)
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock", self.created(commands))

    def test_forwarding_starts_stopped_agent_and_waits_until_it_can_load_keys(self):
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_installed=True,
                                     agent_states=(2, 2, 1))
        self.assertIn(["systemctl", "--user", "start", "docker-ssh-agent.service"], commands)
        self.assertIn(["ssh-add"], commands)
        self.assertEqual(self.agent_unit, "existing unit\n")

    def test_forwarding_setup_failure_prevents_container_creation(self):
        for failing in (["systemctl", "--user"], ["ssh-add"]):
            with self.subTest(failing=failing):
                commands = self.run_creation(failing, answers={"SSH agent": ["y"]})
                self.assertFalse(any(command[:2] == ["docker", "create"] for command in commands))

    def test_unreachable_agent_times_out_before_container_creation(self):
        with self.assertRaisesRegex(SystemExit, "SSH agent.*ready"):
            self.run_creation(answers={"SSH agent": ["y"]}, agent_states=(2,))
        self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))

    def test_declining_forwarding_does_not_install_agent_or_load_keys(self):
        commands = self.run_creation()
        self.assertIsNone(self.agent_unit)
        self.assertFalse(any(command[0] in ("ssh-add", "systemctl") for command in commands))

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
