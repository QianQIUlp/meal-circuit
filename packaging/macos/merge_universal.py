from __future__ import annotations

import argparse
import os
import shutil
import subprocess
from pathlib import Path


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    result = subprocess.run(
        ["file", "-b", str(path)], capture_output=True, check=True, text=True
    )
    return "Mach-O" in result.stdout


def macho_paths(root: Path) -> set[Path]:
    return {path.relative_to(root) for path in root.rglob("*") if is_macho(path)}


def merge(arm_app: Path, intel_app: Path, output_app: Path) -> None:
    arm_paths = macho_paths(arm_app)
    intel_paths = macho_paths(intel_app)
    if arm_paths != intel_paths:
        missing_intel = sorted(str(path) for path in arm_paths - intel_paths)
        missing_arm = sorted(str(path) for path in intel_paths - arm_paths)
        raise RuntimeError(
            f"Mach-O layout mismatch; missing Intel={missing_intel}, missing ARM={missing_arm}"
        )
    if output_app.exists():
        shutil.rmtree(output_app)
    shutil.copytree(arm_app, output_app, symlinks=True)
    for relative in sorted(arm_paths, key=str):
        arm_binary = arm_app / relative
        intel_binary = intel_app / relative
        output_binary = output_app / relative
        temporary = output_binary.with_name(f".{output_binary.name}.universal")
        subprocess.run(
            ["lipo", "-create", str(intel_binary), str(arm_binary), "-output", str(temporary)],
            check=True,
        )
        os.chmod(temporary, output_binary.stat().st_mode)
        temporary.replace(output_binary)
        archs = subprocess.run(
            ["lipo", "-archs", str(output_binary)],
            capture_output=True,
            check=True,
            text=True,
        ).stdout.split()
        if not {"x86_64", "arm64"}.issubset(archs):
            raise RuntimeError(f"universal merge failed for {relative}: {archs}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Merge two PyInstaller app bundles into universal2")
    parser.add_argument("arm_app", type=Path)
    parser.add_argument("intel_app", type=Path)
    parser.add_argument("output_app", type=Path)
    args = parser.parse_args()
    merge(args.arm_app.resolve(), args.intel_app.resolve(), args.output_app.resolve())


if __name__ == "__main__":
    main()
