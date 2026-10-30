"""Optional offline image check: DEVBOX_TEST_IMAGE=cuda-devbox:latest python3 -m unittest discover -s tests."""

import json
import os
import re
import subprocess
import unittest

# CUDA base-image metadata: used to build that image and read by the NVIDIA
# runtime when a container is created, never by programs in a session.
NOT_FOR_SESSIONS = re.compile(r"NV_.*|NVIDIA_.*|NVARCH|CUDA_VERSION|NCCL_VERSION")


@unittest.skipUnless(os.environ.get("DEVBOX_TEST_IMAGE"), "set DEVBOX_TEST_IMAGE to test a built image")
class SshEnvironmentDockerTests(unittest.TestCase):
    def setUp(self):
        self.image = os.environ["DEVBOX_TEST_IMAGE"]
        inspect = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{json .Config.Env}}", self.image],
            capture_output=True, text=True, check=True, timeout=30,
        )
        self.image_env = dict(item.split("=", 1) for item in json.loads(inspect.stdout))

    def run_in_image(self, script):
        result = subprocess.run(
            ["docker", "run", "--rm", "--network=none", "--entrypoint", "/bin/bash", self.image, "-euc", script],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def test_ssh_sessions_get_the_image_env_and_not_the_client_locale(self):
        # sshd -T prints the effective config; it needs a host key, which the image no longer ships.
        output = self.run_in_image('''
ssh-keygen -q -t ed25519 -N "" -f /tmp/host_key
/usr/sbin/sshd -T -o HostKey=/tmp/host_key | grep -E "^(acceptenv|setenv) "
echo ---
locale -a
''')
        sshd, locales = output.split("---\n")
        lines = sshd.splitlines()
        self.assertEqual([line for line in lines if line.startswith("acceptenv ")],
                         ["acceptenv COLORTERM", "acceptenv NO_COLOR"])
        setenv = dict(line.removeprefix("setenv ").split("=", 1) for line in lines if line.startswith("setenv "))
        # Every ENV a session can use, so a new Dockerfile ENV left out of setup_env.sh fails here.
        self.assertEqual(setenv, {name: value for name, value in self.image_env.items()
                                  if not NOT_FOR_SESSIONS.fullmatch(name)})
        self.assertEqual((setenv["LANG"], setenv["LC_ALL"]), ("C.UTF-8", "C.UTF-8"))
        self.assertEqual(set(locales.split()), {"C", "C.utf8", "POSIX", "en_US.utf8"})

    def test_sshd_accepts_public_keys_only(self):
        output = self.run_in_image('''
ssh-keygen -q -t ed25519 -N "" -f /tmp/host_key
/usr/sbin/sshd -T -o HostKey=/tmp/host_key | grep -E "^(permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication) "
''')
        self.assertEqual(sorted(output.splitlines()), [
            "kbdinteractiveauthentication no", "passwordauthentication no",
            "permitrootlogin prohibit-password", "pubkeyauthentication yes",
        ])

    def test_shell_startup_fills_in_missing_values_without_overriding_them(self):
        output = self.run_in_image('''
PATH=/parent/bin:$PATH CC=parent-cc bash -i -c 'echo "$PATH"; echo "$CC"' 2>/dev/null
env -i HOME=/root PATH=/usr/bin:/bin bash -lc 'echo "$PATH"; echo "$LIBRARY_PATH"'
''')
        interactive_path, cc, login_path, library_path = output.splitlines()
        # Like VS Code's `code` directory or an activated venv: ~/.bashrc must keep it.
        self.assertEqual(interactive_path, "/parent/bin:" + self.image_env["PATH"])
        self.assertEqual(cc, "parent-cc")
        # Missing image directories go in front, in the image's order.
        missing = [path for path in self.image_env["PATH"].split(":") if path not in ("/usr/bin", "/bin")]
        self.assertEqual(login_path, ":".join(missing + ["/usr/bin", "/bin"]))
        self.assertEqual(library_path, self.image_env["LIBRARY_PATH"])


if __name__ == "__main__":
    unittest.main()
