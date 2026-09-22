"""Optional offline smoke test: DEVBOX_TEST_IMAGE=cuda-devbox:latest python3 -m unittest discover -s tests."""

import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("DEVBOX_TEST_IMAGE"), "set DEVBOX_TEST_IMAGE to test a built image")
class PersistenceDockerTests(unittest.TestCase):
    def test_state_and_global_npm_commands_survive_container_recreation(self):
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

            mounts = []
            for name in (".codex", ".claude", ".agents", ".npm", ".npm-global"):
                path = root / name
                path.mkdir(mode=0o700)
                mounts += ["-v", f"{path}:/root/{name}"]

            def run_container(script):
                result = subprocess.run(
                    ["docker", "run", "--rm", "--network=none", *mounts,
                     "-v", f"{root / 'package.tgz'}:/tmp/package.tgz:ro",
                     "--entrypoint", "/bin/bash", image, "-euc", script],
                    capture_output=True, text=True, timeout=90,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            try:
                run_container('''
test "$CLAUDE_CONFIG_DIR" = /root/.claude
test "$(npm prefix -g)" = /root/.npm-global
test "$(npm config get userconfig)" = /root/.npm/npmrc
npm install -g --ignore-scripts --no-audit --no-fund /tmp/package.tgz
npm config set fund false
test "$(devbox-persistence-smoke)" = PERSIST_OK
claude mcp add --scope user persistence-smoke -- /bin/echo smoke
printf 'test-marker\n' > /root/.codex/persistence-marker
mkdir -p /root/.agents/skills/smoke
printf 'test-skill\n' > /root/.agents/skills/smoke/SKILL.md
''')
                # The first container has been deleted by --rm. Reuse only its
                # bind mounts, just as devbox create does for the same name.
                run_container('''
test "$(devbox-persistence-smoke)" = PERSIST_OK
test "$(npm config get fund)" = false
test "$(cat /root/.codex/persistence-marker)" = test-marker
test "$(cat /root/.agents/skills/smoke/SKILL.md)" = test-skill
node -e 'const c = require("/root/.claude/.claude.json"); if (!c.mcpServers["persistence-smoke"]) process.exit(1)'
# Model a fresh SSH login that did not inherit Docker ENV.
env -i HOME=/root PATH=/usr/bin:/bin /bin/bash -elc '
    test "$CLAUDE_CONFIG_DIR" = /root/.claude
    test "$(npm prefix -g)" = /root/.npm-global
    test "$(npm config get userconfig)" = /root/.npm/npmrc
    test "$(devbox-persistence-smoke)" = PERSIST_OK
'
# New empty mounts must leave the image tools and Python runtime available.
node --version
tsc --version
codex --version
claude --version
python3 --version
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
