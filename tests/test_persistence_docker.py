"""Optional offline smoke test: DEVBOX_TEST_IMAGE=cuda-devbox:latest python3 -m unittest discover -s tests."""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "host"))
from devbox_lib import container  # noqa: E402


@unittest.skipUnless(os.environ.get("DEVBOX_TEST_IMAGE"), "set DEVBOX_TEST_IMAGE to test a built image")
class PersistenceDockerTests(unittest.TestCase):
    def test_the_home_and_commands_installed_into_it_survive_container_recreation(self):
        image = os.environ["DEVBOX_TEST_IMAGE"]
        with tempfile.TemporaryDirectory(prefix="devbox-state-test-") as directory:
            root = Path(directory)
            package = root / "package"
            package.mkdir()
            (package / "package.json").write_text(json.dumps({
                "name": "devbox-persistence-smoke", "version": "1.0.0",
                "bin": {"devbox-persistence-smoke": "cli.js"},
            }))
            (package / "cli.js").write_text('#!/usr/bin/env node\nconsole.log("PERSIST_OK");\n')
            with tarfile.open(root / "package.tgz", "w:gz") as archive:
                archive.add(package, arcname="package")

            # An empty home mounted as /root, as devbox create does for a new box.
            home = root / "home"
            home.mkdir(mode=0o700)
            mounts = ["-v", f"{home}:/root"]

            def run_container(script):
                result = subprocess.run(
                    ["docker", "run", "--rm", "--network=none", *mounts,
                     "-v", f"{root / 'package.tgz'}:/tmp/package.tgz:ro",
                     "--entrypoint", "/bin/bash", image, "-euc", script],
                    capture_output=True, text=True, timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            try:
                run_container(container.SEED_HOME_SCRIPT + '''
cmp /root/.bashrc /usr/share/base-files/dot.bashrc
cmp /root/.profile /usr/share/base-files/dot.profile
echo '# edited in the box' >> /root/.bashrc
# The image's tools and Python must not depend on anything in the home.
test "$(command -v python3)" = /usr/local/bin/python3
test "$(readlink -f "$(uv python find)")" = "$(readlink -f /usr/local/bin/python3)"
node --version
tsc --version
codex --version
dsh --version
claude --version
nvcc --version
test "$CLAUDE_CONFIG_DIR" = /root/.claude
test "$(npm prefix -g)" = /root/.npm-global
test "$(npm config get userconfig)" = /root/.npm/npmrc
npm install -g --ignore-scripts --no-audit --no-fund /tmp/package.tgz
npm config set fund false
test "$(devbox-persistence-smoke)" = PERSIST_OK
claude mcp add --scope user persistence-smoke -- /bin/echo smoke
mkdir -p /root/.codex /root/.dsh
printf 'test-marker\n' > /root/.codex/persistence-marker
printf 'test-dsh-marker\n' > /root/.dsh/persistence-marker
mkdir -p /root/.agents/skills/smoke
printf 'test-skill\n' > /root/.agents/skills/smoke/SKILL.md
# Stand-ins for `cargo install` and `uv tool install`, which need the network.
test "$CARGO_INSTALL_ROOT" = /root/.cargo
mkdir -p /root/.cargo/bin /root/.local/bin /root/.cache
printf '#!/bin/sh\necho cargo-installed\n' > /root/.cargo/bin/devbox-smoke-cargo
printf '#!/bin/sh\necho uv-tool-installed\n' > /root/.local/bin/devbox-smoke-uv-tool
chmod +x /root/.cargo/bin/devbox-smoke-cargo /root/.local/bin/devbox-smoke-uv-tool
printf 'cached\n' > /root/.cache/marker
''')
                # The first container has been deleted by --rm. Reuse only its
                # home, just as devbox create does for the same name.
                run_container(container.SEED_HOME_SCRIPT + '''
tail -n 1 /root/.bashrc | grep -qx '# edited in the box'
test "$(devbox-smoke-cargo)" = cargo-installed
test "$(devbox-smoke-uv-tool)" = uv-tool-installed
test "$(cat /root/.cache/marker)" = cached
test "$(devbox-persistence-smoke)" = PERSIST_OK
test "$(npm config get fund)" = false
test "$(cat /root/.codex/persistence-marker)" = test-marker
test "$(cat /root/.dsh/persistence-marker)" = test-dsh-marker
test "$(cat /root/.agents/skills/smoke/SKILL.md)" = test-skill
node -e 'const c = require("/root/.claude/.claude.json"); if (!c.mcpServers["persistence-smoke"]) process.exit(1)'
# Model a fresh SSH login that did not inherit Docker ENV.
env -i HOME=/root PATH=/usr/bin:/bin /bin/bash -elc '
    test "$CLAUDE_CONFIG_DIR" = /root/.claude
    test "$(npm prefix -g)" = /root/.npm-global
    test "$(npm config get userconfig)" = /root/.npm/npmrc
    test "$(devbox-persistence-smoke)" = PERSIST_OK
    test "$(devbox-smoke-cargo)" = cargo-installed
'
''')
            finally:
                # Container root creates files in the fixture. Return ownership
                # only within this temporary directory so it can be removed.
                subprocess.run(
                    ["docker", "run", "--rm", "--network=none", "-v", f"{root}:/fixture",
                     "--entrypoint", "/bin/chown", image, "-R",
                     f"{os.getuid()}:{os.getgid()}", "/fixture"],
                    check=True, capture_output=True, timeout=30,
                )


if __name__ == "__main__":
    unittest.main()
