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
    # 64 does not fit the stubbed 14.3 GiB host and is re-asked.
    "RAM profile": ["64", "16"],
    "GPU": [""],
    "SSH agent": ["n"],
    "sandbox": ["n"],
    "Docker socket": ["n"],
    "kernel development": ["y"],
}
HOST_KEY = {".ssh/id_ed25519": "private key\n", ".ssh/id_ed25519.pub": "ssh-ed25519 AAAAhost host\n"}


class CreateDockerTests(unittest.TestCase):
    def run_creation(self, fail_command=None, *, interrupt=False, answers=None, with_keys=False, gitconfig=True,
                     gpu_toolkit=True, agent_installed=False, agent_states=(1,),
                     image_tags="latest\n202608\n202609\n<none>\n", persistent_files=None,
                     existing_containers="", home_files=None, memtotal_kib=14991536,
                     osrelease="7.0.0-31-generic\n", missing_programs=(), host_pub=True,
                     box_ssh_owned_by_root=False, state_files=None, home_label="true"):
        commands = []
        self.commands = commands
        self.prompts = prompts = []
        self.agent_environments = []
        self.installed_keys = installed_keys = []
        agent_states = list(agent_states)

        def run(command, **kwargs):
            commands.append(command)
            # A "~/" path in fail_command refers to the fake home; interrupt presses Ctrl-C there instead.
            if fail_command and command[:2] == [expand(part) for part in fail_command]:
                raise KeyboardInterrupt if interrupt else subprocess.CalledProcessError(1, command)
            if command[0] in missing_programs:
                raise FileNotFoundError(command[0])
            if command[:3] == ["docker", "image", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout=image_tags)
            if command[:3] == ["docker", "image", "inspect"]:
                return subprocess.CompletedProcess(command, 0, stdout=f"{home_label}\n")
            if command[:2] == ["docker", "ps"]:
                return subprocess.CompletedProcess(command, 0, stdout=existing_containers)
            if command[:2] == ["docker", "cp"]:
                installed_keys.append((command[-1], Path(command[-2]).read_text()))
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
            state_root = home / "docker_mount/.devbox/test-dev"
            self.home = home
            self.mount_root = str(mount_root)
            self.state_root = str(state_root)
            for root, files in ((mount_root, persistent_files), (state_root, state_files)):
                for relative, content in (files or {}).items():
                    path = root / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(content)
            unit_path = home / ".config/systemd/user/docker-ssh-agent.service"
            if agent_installed:
                unit_path.parent.mkdir(parents=True)
                unit_path.write_text("existing unit\n")
                (home / ".ssh-agent").mkdir()
            if gitconfig:
                (home / ".gitconfig").touch()
            if host_pub:
                (home / ".ssh").mkdir()
                (home / ".ssh/id_ed25519.pub").write_text("ssh-ed25519 AAAAhost host\n")
            if with_keys:
                (home / ".ssh").mkdir(exist_ok=True)
                # Keys that log in to the host (e.g. from a laptop); the restricted one is skipped.
                (home / ".ssh/authorized_keys").write_text(
                    "ssh-ed25519 AAAAlaptop laptop\nfrom=\"10.0.0.1\" ssh-ed25519 AAAArestricted restricted\n"
                )
                existing_ssh = home / "docker_mount/test-dev/.ssh"
                existing_ssh.mkdir(parents=True)
                # Left by an earlier box; recreation replaces it with the host's keys.
                (existing_ssh / "authorized_keys").write_text("ssh-ed25519 AAAAremote remote\n")
            for relative, content in (home_files or {}).items():
                path = home / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            osrelease_path = home / "osrelease"
            osrelease_path.write_text(osrelease)
            expanduser = os.path.expanduser
            path_exists = os.path.exists

            def expand(path):
                return str(home / path[2:]) if path.startswith("~/") else expanduser(path)

            box_ssh = mount_root / ".ssh"
            real_chmod = os.chmod

            def chmod(path, mode, *args, **kwargs):
                # A box can leave its .ssh owned by root; the host user then gets EPERM.
                if box_ssh_owned_by_root and str(path) == str(box_ssh):
                    raise PermissionError(1, "Operation not permitted", str(path))
                return real_chmod(path, mode, *args, **kwargs)

            if box_ssh_owned_by_root:
                box_ssh.mkdir(parents=True, exist_ok=True)
                real_chmod(box_ssh, 0o500)

            with (
                patch("builtins.input", side_effect=reply),
                patch("builtins.print") as printed,
                patch("sys.stderr", new_callable=io.StringIO),
                patch("os.path.expanduser", side_effect=expand),
                patch("os.path.exists", side_effect=lambda path: True if path == "/dev/nvidiactl" else path_exists(path)),
                patch("subprocess.run", side_effect=run),
                patch("os.geteuid", return_value=1000),
                patch("os.chmod", side_effect=chmod),
                patch("time.sleep"),
                patch("shutil.which", side_effect=lambda name: f"/usr/bin/{name}" if gpu_toolkit else None),
                patch("devbox_lib.container.CDI_SPEC_DIRS", ()),
                patch("devbox_lib.container.OSRELEASE_PATH", str(osrelease_path)),
                patch("devbox_lib.memory.read_memtotal_kib", return_value=memtotal_kib),
            ):
                if fail_command:
                    with self.assertRaises(KeyboardInterrupt if interrupt else subprocess.CalledProcessError):
                        DEVBOX_MAIN(["create"])
                else:
                    self.assertEqual(DEVBOX_MAIN(["create"]), 0)
            self.output = "\n".join(" ".join(map(str, call.args)) for call in printed.call_args_list)
            if box_ssh_owned_by_root:
                real_chmod(box_ssh, 0o700)
            self.written_keys = installed_keys[-1][1] if installed_keys else None
            self.agent_unit = unit_path.read_text() if unit_path.exists() else None
            self.agent_socket = str(home / ".ssh-agent/docker-agent.sock")
            self.home_modes = {
                name: (mount_root / name).stat().st_mode & 0o777 for name in (".", ".ssh") if (mount_root / name).is_dir()
            }
            self.home_listing = sorted(os.listdir(mount_root)) if mount_root.is_dir() else []
            self.persistent_files = {
                relative: (mount_root / relative).read_text() if (mount_root / relative).exists() else None
                for relative in (persistent_files or {})
            }
            self.state_files = {
                relative: (state_root / relative).read_text() if (state_root / relative).exists() else None
                for relative in {**(persistent_files or {}), **(state_files or {})}
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

    def key_loads(self, commands):
        return [command for command in commands if command[0] == "ssh-add" and command[1:] != ["-l"]]

    def host_key_generations(self, commands):
        return [command for command in commands if command[0] == "ssh-keygen"]

    def test_creation_uses_profile_and_all_ports_without_ssh_collision(self):
        create = self.created(self.run_creation())
        self.assertEqual(self.port_bindings(create), ["127.0.0.1:2222:22", "127.0.0.1:8000-8099:8000-8099/tcp"])
        self.assertEqual(create[create.index("--memory") + 1], "10g")
        self.assertEqual(create[create.index("--memory-swap") + 1], "12g")
        self.assertIn("memlock=-1:-1", create)
        self.assertIn("--ipc=host", create)
        self.assertNotIn("--shm-size", create)  # the host's /dev/shm is used instead
        self.assertNotIn("--memory-swappiness", create)  # unsupported on cgroup v2

    def test_without_kernel_dev_shm_is_half_the_container_ram(self):
        # Docker's private /dev/shm would otherwise be 64 MiB.
        for memtotal_gib, profile, shm in ((12, "8", "2048m"), (14.3, "16", "5120m")):
            with self.subTest(profile=profile):
                create = self.created(self.run_creation(memtotal_kib=int(memtotal_gib * 1024**2), answers={
                    "RAM profile": [profile], "kernel development": ["n"],
                }))
                self.assertNotIn("--ipc=host", create)
                self.assertEqual(create[create.index("--shm-size") + 1], shm)

    def test_small_host_defaults_to_the_8_gib_profile(self):
        # A 16 GiB laptop with a large integrated-GPU carve-out: 16 no longer fits and is re-asked.
        create = self.created(self.run_creation(memtotal_kib=12 * 1024**2, answers={"RAM profile": ["16", ""]}))
        self.assertEqual(create[create.index("--memory") + 1], "4g")
        self.assertEqual(create[create.index("--memory-swap") + 1], "6g")
        profile_prompts = [prompt for prompt in self.prompts if "RAM profile" in prompt]
        self.assertEqual(len(profile_prompts), 2)
        self.assertIn("auto/8/16/32/64", profile_prompts[0])

    def test_host_too_small_for_any_profile_exits_before_any_question(self):
        with self.assertRaisesRegex(SystemExit, "6.625 GiB"):
            self.run_creation(memtotal_kib=6 * 1024**2)
        self.assertEqual((self.prompts, self.commands), ([], []))

    def test_wsl_host_is_refused_before_any_question(self):
        with self.assertRaisesRegex(SystemExit, "WSL"):
            self.run_creation(osrelease="6.6.87.2-microsoft-standard-WSL2\n")
        self.assertEqual((self.prompts, self.commands), ([], []))

    def test_create_takes_no_command_line_options(self):
        with patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit):
            DEVBOX_MAIN(["create", "--no-gpu"])

    def test_the_whole_home_is_mounted_and_kept_private(self):
        create = self.created(self.run_creation())
        mounts = [create[i + 1] for i, value in enumerate(create) if value == "-v"]
        self.assertIn(f"{self.mount_root}:/root", mounts)
        # Only read-only files from the host go inside it, so nothing hides part of the home.
        self.assertEqual([mount for mount in mounts if ":/root/" in mount], [f"{self.home}/.gitconfig:/root/.gitconfig:ro"])
        self.assertEqual(self.home_modes, {".": 0o700, ".ssh": 0o700})
        # Host keys and added keys stay out of the box's view.
        self.assertEqual(self.home_listing, [".ssh", "data"])

    def test_recreation_and_failed_start_preserve_everything_in_the_home(self):
        files = {
            ".codex/auth.json": '{"test": "dummy-token"}\n',
            ".claude/projects/example/session.jsonl": '{"test": "session"}\n',
            ".dsh/.credentials.yaml": "test: dummy-key\n",
            ".npm-global/bin/example": "#!/bin/sh\necho example\n",
            ".cargo/bin/example": "cargo-installed\n",
            ".local/bin/example": "uv-tool-installed\n",
            ".cache/huggingface/model": "weights\n",
            ".config/gh/hosts.yml": "github.com: {}\n",
            ".bashrc": "# edited by hand\n",
            ".bash_history": "nvcc --version\n",
            "data/project/main.cu": "int main() {}\n",
        }
        for failing in (None, ["docker", "start"]):
            with self.subTest(failing=failing):
                create = self.created(self.run_creation(failing, persistent_files=files))
                self.assertIn(f"{self.mount_root}:/root", create)
                self.assertEqual(self.persistent_files, files)

    def test_a_new_home_gets_the_image_startup_files_once_the_box_runs(self):
        commands = self.run_creation()
        seed = next(command for command in commands if container.SEED_HOME_SCRIPT in command)
        self.assertEqual(seed[:3], ["docker", "exec", "test-container"])
        self.assertLess(commands.index(["docker", "start", "test-container"]), commands.index(seed))
        self.assertIn(f"Its /root is {self.mount_root} on the host", self.output)

    def test_an_image_without_the_persistent_home_label_asks_first(self):
        with self.assertRaisesRegex(SystemExit, "Nothing was created"):
            self.run_creation(home_label="", answers={"Continue anyway": [""]})
        self.assertFalse(any("container name" in prompt for prompt in self.prompts))
        self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))
        self.assertIn("cuda-devbox:latest", self.created(self.run_creation(home_label="", answers={"Continue anyway": ["y"]})))
        self.assertIn("devbox build", self.output)
        self.run_creation()
        self.assertFalse(any("Continue anyway" in prompt for prompt in self.prompts))

    def test_recreation_keeps_private_keys_generated_in_the_box(self):
        # The host never copies private keys in, so these were made inside the box.
        box_keys = {".ssh/id_ed25519": "deploy key\n", ".ssh/id_rsa": "rsa key\n", ".ssh/id_ecdsa": "ecdsa key\n"}
        self.run_creation(persistent_files=box_keys)
        self.assertEqual(self.persistent_files, box_keys)

    def test_first_creation_generates_box_host_keys_and_points_sshd_at_them(self):
        commands = self.run_creation(with_keys=True)
        key_dir = f"{self.state_root}/ssh-host-keys"
        generations = self.host_key_generations(commands)
        self.assertEqual(
            [(command[command.index("-t") + 1], command[command.index("-f") + 1], command[command.index("-C") + 1])
             for command in generations],
            [(key_type, f"{key_dir}/ssh_host_{key_type}_key", "root@test-dev") for key_type in ("ed25519", "ecdsa", "rsa")],
        )
        create = self.created(commands)
        self.assertLess(commands.index(generations[-1]), commands.index(create))
        self.assertIn(f"{key_dir}:/etc/ssh/host-keys:ro", create)
        # Only the mounted keys, so an older image's built-in keys are not used either.
        daemon_options = create[create.index("cuda-devbox:latest") + 3:]
        self.assertEqual(
            [option for option in daemon_options if option.startswith("HostKey=")],
            [f"HostKey=/etc/ssh/host-keys/ssh_host_{key_type}_key" for key_type in ("ed25519", "ecdsa", "rsa")],
        )
        # An image without built-in keys fails a plain `sshd -t`.
        check = next(command for command in commands if "-t" in command and command[:2] == ["docker", "exec"])
        self.assertEqual(check[check.index("-t") + 1:], daemon_options)
        self.assertIn("PasswordAuthentication=no", daemon_options)

    def test_recreation_reuses_the_box_host_keys(self):
        all_keys = {f"ssh-host-keys/ssh_host_{key_type}_key": f"{key_type} host key\n"
                    for key_type in ("ed25519", "ecdsa", "rsa")}
        ed25519_only = {"ssh-host-keys/ssh_host_ed25519_key": "ed25519 host key\n"}
        for existing, generated in ((all_keys, []), (ed25519_only, ["ecdsa", "rsa"])):
            with self.subTest(existing=sorted(existing)):
                commands = self.run_creation(state_files=existing)
                self.assertEqual(
                    [command[command.index("-t") + 1] for command in self.host_key_generations(commands)], generated,
                )
                self.assertEqual(self.state_files, existing)

    def test_devbox_files_in_an_older_home_move_out_of_the_box(self):
        legacy = {
            **{f"ssh-host-keys/ssh_host_{key_type}_key": f"{key_type} host key\n" for key_type in ("ed25519", "ecdsa", "rsa")},
            "ssh-authorized-keys": "ssh-ed25519 AAAAphone phone\n",
        }
        commands = self.run_creation(persistent_files=legacy)
        self.assertEqual(self.host_key_generations(commands), [])
        self.assertEqual(self.state_files, legacy)
        self.assertEqual(self.persistent_files, dict.fromkeys(legacy))
        self.assertIn(f"{self.state_root}/ssh-host-keys:/etc/ssh/host-keys:ro", self.created(commands))
        self.assertEqual(self.written_keys.splitlines(), ["ssh-ed25519 AAAAhost host", "ssh-ed25519 AAAAphone phone"])

    def test_missing_ssh_keygen_stops_before_container_creation(self):
        with self.assertRaisesRegex(SystemExit, "ssh-keygen is not installed"):
            self.run_creation(missing_programs=("ssh-keygen",))
        self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))

    def test_image_defaults_to_latest_even_with_older_monthly_builds(self):
        self.assertIn("cuda-devbox:latest", self.created(self.run_creation()))

    def test_image_defaults_to_newest_monthly_tag_without_latest(self):
        create = self.created(self.run_creation(image_tags="202608\n202609\n<none>\n"))
        self.assertIn("cuda-devbox:202609", create)

    def test_same_month_rebuilds_are_versions_ordered_by_number(self):
        # String order would put 202609-10 before 202609-9.
        for tags, newest in (("202609\n202609-2\n202608-3\n", "202609-2"),
                             ("202609-9\n202609-10\n202609\n", "202609-10")):
            with self.subTest(tags=tags):
                create = self.created(self.run_creation(image_tags=tags))
                self.assertIn(f"cuda-devbox:{newest}", create)

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
        self.assertEqual(self.port_bindings(self.created(commands)),
                         ["127.0.0.1:61000:22", "127.0.0.1:62000-62099:8000-8099/tcp"])

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
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_states=(2, 1), home_files=HOST_KEY)
        self.assertIn(["systemctl", "--user", "enable", "--now", "docker-ssh-agent.service"], commands)
        self.assertIn("ExecStart=/usr/bin/ssh-agent -D -a", self.agent_unit)
        load = ["ssh-add", str(self.home / ".ssh/id_ed25519")]
        self.assertEqual(self.key_loads(commands), [load])
        self.assertLess(commands.index(load), commands.index(self.created(commands)))
        self.assertTrue(all(env["SSH_AUTH_SOCK"] == self.agent_socket for env in self.agent_environments))
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock", self.created(commands))

    def test_forwarding_loads_every_host_key_ed25519_first(self):
        files = {}
        for name, key_type in (("id_rsa", "ssh-rsa"), ("id_ecdsa", "ecdsa-sha2-nistp256"),
                               ("id_ed25519_sk", "sk-ssh-ed25519@openssh.com"), ("id_ed25519", "ssh-ed25519"),
                               ("github", "ssh-ed25519"), ("old_dsa", "ssh-dss")):
            files[f".ssh/{name}"] = "private key\n"
            files[f".ssh/{name}.pub"] = f"{key_type} AAAA{name} {name}\n"
        # Neither a public key without its private key nor a certificate is a key to load.
        files[".ssh/laptop.pub"] = "ssh-ed25519 AAAAlaptop laptop\n"
        files[".ssh/id_ed25519-cert.pub"] = "ssh-ed25519-cert-v01@openssh.com AAAAcert id_ed25519\n"
        commands = self.run_creation(answers={"SSH agent": ["y"]}, home_files=files)
        # Ed25519 (by name), then ECDSA, then RSA; OpenSSH 10 cannot load DSA.
        expected = ("github", "id_ed25519", "id_ed25519_sk", "id_ecdsa", "id_rsa")
        self.assertEqual(self.key_loads(commands), [["ssh-add", *(str(self.home / ".ssh" / name) for name in expected)]])

    def test_forwarding_without_host_private_key_warns_and_still_creates(self):
        # Only a public key on the host, e.g. the private key stays on a laptop.
        commands = self.run_creation(answers={"SSH agent": ["y"]},
                                     home_files={".ssh/laptop.pub": "ssh-ed25519 AAAAlaptop laptop\n"})
        self.assertEqual(self.key_loads(commands), [])
        self.assertIn(f"SSH_AUTH_SOCK={self.agent_socket} ssh-add <key>", self.output)
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock", self.created(commands))

    def test_forwarding_reuses_loaded_agent_without_reinstalling_or_readding_keys(self):
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_installed=True, agent_states=(0,),
                                     home_files=HOST_KEY)
        self.assertEqual(self.agent_unit, "existing unit\n")
        self.assertFalse(any(command[0] == "systemctl" for command in commands))
        self.assertEqual(self.key_loads(commands), [])
        self.assertIn("SSH_AUTH_SOCK=/ssh-agent/docker-agent.sock", self.created(commands))

    def test_forwarding_starts_stopped_agent_and_waits_until_it_can_load_keys(self):
        commands = self.run_creation(answers={"SSH agent": ["y"]}, agent_installed=True,
                                     agent_states=(2, 2, 1), home_files=HOST_KEY)
        self.assertIn(["systemctl", "--user", "start", "docker-ssh-agent.service"], commands)
        self.assertEqual(self.key_loads(commands), [["ssh-add", str(self.home / ".ssh/id_ed25519")]])
        self.assertEqual(self.agent_unit, "existing unit\n")

    def test_forwarding_setup_failure_prevents_container_creation(self):
        for failing in (["systemctl", "--user"], ["ssh-add", "~/.ssh/id_ed25519"]):
            with self.subTest(failing=failing):
                commands = self.run_creation(failing, answers={"SSH agent": ["y"]}, home_files=HOST_KEY)
                self.assertFalse(any(command[:2] == ["docker", "create"] for command in commands))

    def test_unreachable_agent_times_out_before_container_creation(self):
        with self.assertRaisesRegex(SystemExit, "SSH agent.*ready"):
            self.run_creation(answers={"SSH agent": ["y"]}, agent_states=(2,))
        self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))

    def test_declining_forwarding_does_not_install_agent_or_load_keys(self):
        commands = self.run_creation()
        self.assertIsNone(self.agent_unit)
        self.assertFalse(any(command[0] in ("ssh-add", "systemctl") for command in commands))

    def test_key_install_start_or_check_failure_or_ctrl_c_removes_only_new_container(self):
        for failing in (["docker", "cp"], ["docker", "start"], ["docker", "exec"]):
            for interrupt in (False, True):
                with self.subTest(failing=failing, interrupt=interrupt):
                    commands = self.run_creation(failing, interrupt=interrupt)
                    self.assertIn(["docker", "rm", "-f", "test-container"], commands)

    def test_creation_failure_does_not_remove_existing_container(self):
        commands = self.run_creation(["docker", "create"])
        self.assertFalse(any(command[:2] == ["docker", "rm"] for command in commands))

    def test_only_public_keys_log_in_and_missing_gitconfig_is_skipped(self):
        commands = self.run_creation(with_keys=True, gitconfig=False)
        create = self.created(commands)
        self.assertFalse(any("password" in prompt.lower() for prompt in self.prompts))
        self.assertIn("PasswordAuthentication=no", create)
        self.assertIn("KbdInteractiveAuthentication=no", create)
        self.assertIn("PermitRootLogin=prohibit-password", create)
        self.assertFalse(any("chpasswd" in command for command in commands))
        self.assertFalse(any(":/root/.gitconfig" in argument for argument in create))
        # Host keys only: the earlier box's key and the restricted from= line are left out.
        self.assertEqual(set(self.written_keys.splitlines()), {"ssh-ed25519 AAAAhost host", "ssh-ed25519 AAAAlaptop laptop"})

    def test_keys_go_in_through_docker_before_start_even_when_box_ssh_is_root_owned(self):
        leftover = {".ssh/authorized_keys": "ssh-ed25519 AAAAremote remote\n"}
        commands = self.run_creation(persistent_files=leftover, box_ssh_owned_by_root=True)
        self.assertEqual(self.installed_keys, [("test-container:/root/.ssh/authorized_keys", "ssh-ed25519 AAAAhost host\n")])
        install = next(command for command in commands if command[:2] == ["docker", "cp"])
        self.assertLess(commands.index(self.created(commands)), commands.index(install))
        self.assertLess(commands.index(install), commands.index(["docker", "start", "test-container"]))
        # The host-side path is never written; docker cp replaces the file the box sees.
        self.assertEqual(self.persistent_files, leftover)

    def test_keys_added_with_devbox_key_add_are_authorized_with_the_host_keys(self):
        added = {"ssh-authorized-keys": "ssh-ed25519 AAAAphone phone\nssh-ed25519 AAAAhost same key, other comment\n"}
        self.run_creation(state_files=added)
        self.assertEqual(self.written_keys.splitlines(), ["ssh-ed25519 AAAAhost host", "ssh-ed25519 AAAAphone phone"])

    def test_without_any_public_key_create_refuses_before_other_questions(self):
        # Keys left in the box's authorized_keys do not count; only the host's and added ones do.
        for box_files in ({}, {".ssh/authorized_keys": "ssh-ed25519 AAAAremote remote\n"}):
            with self.subTest(box_files=box_files):
                with self.assertRaisesRegex(SystemExit, "devbox key add test-dev"):
                    self.run_creation(host_pub=False, persistent_files=box_files)
                self.assertFalse(any("port 22" in prompt for prompt in self.prompts))
                self.assertFalse(any(command[:2] == ["docker", "create"] for command in self.commands))


if __name__ == "__main__":
    unittest.main()
