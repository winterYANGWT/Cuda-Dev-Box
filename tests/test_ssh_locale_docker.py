"""Optional offline image check: DEVBOX_TEST_IMAGE=cuda-devbox:latest python3 -m unittest discover -s tests."""

import os
import subprocess
import unittest


@unittest.skipUnless(os.environ.get("DEVBOX_TEST_IMAGE"), "set DEVBOX_TEST_IMAGE to test a built image")
class SshLocaleDockerTests(unittest.TestCase):
    def test_ssh_sessions_use_c_utf8_instead_of_the_client_locale(self):
        # sshd -T prints the effective config; it needs a host key, which the image no longer ships.
        script = '''
ssh-keygen -q -t ed25519 -N "" -f /tmp/host_key
/usr/sbin/sshd -T -o HostKey=/tmp/host_key | grep -E "^(acceptenv|setenv) "
echo ---
locale -a
'''
        result = subprocess.run(
            ["docker", "run", "--rm", "--network=none", "--entrypoint", "/bin/bash",
             os.environ["DEVBOX_TEST_IMAGE"], "-euc", script],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sshd, locales = result.stdout.split("---\n")
        self.assertEqual(sshd.splitlines(), [
            "acceptenv COLORTERM", "acceptenv NO_COLOR", "setenv LANG=C.UTF-8", "setenv LC_ALL=C.UTF-8",
        ])
        self.assertEqual(set(locales.split()), {"C", "C.utf8", "POSIX", "en_US.utf8"})


if __name__ == "__main__":
    unittest.main()
