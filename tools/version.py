from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def check(tag: str | None = None, output: Path | None = None) -> str:
    version = project_version()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit(f"invalid project version: {version}")
    package_version = (ROOT / "mealcircuit" / "__init__.py").read_text(encoding="utf-8")
    if f'__version__ = "{version}"' not in package_version:
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
