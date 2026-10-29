"""Build the Cuda-Dev-Box image and tag it with the version in the VERSION file."""

import os
import subprocess

from . import container

# host/devbox_lib/image.py -> the repository root, which holds the Dockerfile and VERSION.
REPOSITORY_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
VERSION_FILE = os.path.join(REPOSITORY_ROOT, "VERSION")


def read_version():
    try:
        with open(VERSION_FILE, "r", encoding="utf-8") as version_file:
            version = version_file.read().strip()
    except FileNotFoundError:
        raise SystemExit(f"{VERSION_FILE} is missing; put the image version in it, e.g. 202610.")
    if not container.IMAGE_VERSION_TAG.fullmatch(version):
        raise SystemExit(f"{VERSION_FILE} must hold one image version, YYYYMM or YYYYMM-N "
                         f"(e.g. 202610 or 202610-2), not {version!r}.")
    return version


def next_build_of(version):
    month, _, build = version.partition("-")
    return f"{month}-{int(build or 1) + 1}"


def run(args):
    version = read_version()
    repository = container.IMAGE_REPOSITORY
    if any(build_arg.split("=", 1)[0] == "IMAGE_VERSION" for build_arg in args.build_args):
        raise SystemExit(f"IMAGE_VERSION comes from {VERSION_FILE}; edit that file instead.")
    # Building again under an existing tag would move it off the older image.
    if version in container.local_image_tags(repository):
        raise SystemExit(f"{repository}:{version} already exists. Set {VERSION_FILE} to a new version "
                         f"(e.g. {next_build_of(version)}, or a new month), or remove the old image with "
                         f"`docker rmi {repository}:{version}`.")

    command = [
        "docker", "build",
        "--build-arg", f"IMAGE_VERSION={version}",
        "-t", f"{repository}:{version}",
        "-t", f"{repository}:latest",
    ]
    if args.pull:
        command.append("--pull")
    if args.no_cache:
        command.append("--no-cache")
    for build_arg in args.build_args:
        command += ["--build-arg", build_arg]
    command.append(REPOSITORY_ROOT)
    print(f"Building {repository}:{version} (also tagged latest)")
    subprocess.run(command, check=True)
    print(f"Built {repository}:{version} and {repository}:latest.")
    return 0


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "build", help="Build the image, tagged with the VERSION file's version and latest",
        description=f"Build {container.IMAGE_REPOSITORY} from the repository's Dockerfile. The version comes "
                    "from the VERSION file (YYYYMM, or YYYYMM-N for another build in the same month); the image "
                    "is tagged with it and with latest, and carries it as its version label.",
    )
    parser.add_argument("--pull", action="store_true",
                        help="re-pull the base images that floating tags (e.g. lts-trixie) point at")
    parser.add_argument("--no-cache", action="store_true", help="rebuild every layer")
    parser.add_argument("--build-arg", dest="build_args", action="append", default=[], metavar="NAME=VALUE",
                        help="override a Dockerfile build argument for this build; repeatable")
    parser.set_defaults(func=run)
