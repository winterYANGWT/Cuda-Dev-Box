import io
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
from devbox_lib import container, image  # noqa: E402

REPOSITORY_ROOT = str(HOST_DIR.parent)


class DevboxBuildTests(unittest.TestCase):
    def run_build(self, version, *argv, image_tags="latest\n202609\n<none>\n"):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if command[:3] == ["docker", "image", "ls"]:
                return subprocess.CompletedProcess(command, 0, stdout=image_tags)
            return subprocess.CompletedProcess(command, 0)

        with tempfile.TemporaryDirectory() as directory:
            version_file = Path(directory) / "VERSION"
            if version is not None:
                version_file.write_text(version)
            with (
                patch.object(image, "VERSION_FILE", str(version_file)),
                patch("subprocess.run", side_effect=run),
                patch("builtins.print"),
                patch("sys.stderr", new_callable=io.StringIO),
            ):
                self.assertEqual(DEVBOX_MAIN(["build", *argv]), 0)
        return [command for command in commands if command[:2] == ["docker", "build"]]

    def test_version_file_sets_label_and_both_tags(self):
        self.assertEqual(self.run_build("202610-2\n"), [[
            "docker", "build", "--build-arg", "IMAGE_VERSION=202610-2",
            "-t", "cuda-devbox:202610-2", "-t", "cuda-devbox:latest", REPOSITORY_ROOT,
        ]])

    def test_pull_no_cache_and_build_args_are_passed_to_docker(self):
        builds = self.run_build("202611", "--pull", "--no-cache",
                                "--build-arg", "CODEX_VERSION=0.165.0", "--build-arg", "GO_TAG=1.26-trixie")
        self.assertEqual(builds, [[
            "docker", "build", "--build-arg", "IMAGE_VERSION=202611",
            "-t", "cuda-devbox:202611", "-t", "cuda-devbox:latest", "--pull", "--no-cache",
            "--build-arg", "CODEX_VERSION=0.165.0", "--build-arg", "GO_TAG=1.26-trixie", REPOSITORY_ROOT,
        ]])

    def test_existing_version_stops_before_building_and_suggests_the_next(self):
        # Building again would move the tag off the older image.
        for version, suggestion in (("202609", "202609-2"), ("202609-2", "202609-3")):
            with self.subTest(version=version):
                with self.assertRaisesRegex(SystemExit, f"cuda-devbox:{version} already exists.*{suggestion}"):
                    self.run_build(version, image_tags=f"latest\n{version}\n")

    def test_image_version_build_arg_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "IMAGE_VERSION comes from"):
            self.run_build("202611", "--build-arg", "IMAGE_VERSION=other")

    def test_missing_or_malformed_version_file_is_refused(self):
        for content in (None, "", "2026-10", "202610-", "202610 202611", "latest"):
            with self.subTest(content=content):
                with self.assertRaises(SystemExit):
                    self.run_build(content)

    def test_repository_version_file_is_valid(self):
        self.assertRegex(image.read_version(), container.IMAGE_VERSION_TAG)


if __name__ == "__main__":
    unittest.main()
