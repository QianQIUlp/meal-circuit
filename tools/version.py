from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEMVER = re.compile(r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
sys.path.insert(0, str(ROOT))

from mealcircuit import __version__


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def android_version_code(version: str) -> int:
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ValueError(f"invalid project version: {version}")
    code = (
        int(match.group("major")) * 1_000_000
        + int(match.group("minor")) * 10_000
        + int(match.group("patch"))
    )
    if code > 2_147_483_647:
        raise ValueError(f"Android versionCode exceeds Int.MAX_VALUE: {code}")
    return code


def check(tag: str | None = None, output: Path | None = None) -> str:
    version = project_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"invalid project version: {version}")
    android_version_code(version)
    if __version__ != version:
        raise SystemExit("mealcircuit.__version__ does not match pyproject.toml")
    if tag and tag.startswith("v") and tag[1:] != version:
        raise SystemExit(f"Git tag {tag} does not match project version {version}")
    release_doc = ROOT / "docs" / "releases" / f"v{version}.md"
    if not release_doc.is_file():
        raise SystemExit(f"release document is missing: {release_doc}")
    if output is not None:
        with output.open("a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
    print(f"version {version} is consistent")
    return version


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not args.check:
        parser.error("--check is required")
    check(args.tag, args.output)


if __name__ == "__main__":
    main()
