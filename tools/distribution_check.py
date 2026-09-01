from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
LEGAL_FILES = (
    "LICENSE",
    "THIRD_PARTY_LICENSES.md",
    "PRIVACY.md",
    "SECURITY.md",
    "DISCLAIMER.md",
)
RESOURCE_FILES = (
    "rules/core.md",
    "templates/profile.md",
    "templates/settings.json",
    "protocol/checkin-modules-v1.json",
    "protocol/domain-v1.schema.json",
    "protocol/state-machines-v1.json",
    "protocol/sync-v1.openapi.json",
)


class DistributionCheckError(RuntimeError):
    pass


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if result.returncode:
        rendered = subprocess.list2cmdline(command)
        raise DistributionCheckError(
            f"command failed ({result.returncode}): {rendered}\n{result.stdout}"
        )
    return result.stdout


def _copy_source_snapshot(destination: Path) -> None:
    output = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if output.returncode:
        raise DistributionCheckError(
            "cannot enumerate release source files: "
            + output.stderr.decode("utf-8", errors="replace")
        )
    for raw_name in output.stdout.split(b"\0"):
        if not raw_name:
            continue
        relative = Path(os.fsdecode(raw_name))
        source = ROOT / relative
        target = destination / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _assert_clean_members(members: list[str], archive: Path) -> None:
    forbidden = [
        member
        for member in members
        if "__pycache__" in PurePosixPath(member).parts or member.endswith((".pyc", ".pyo"))
    ]
    if forbidden:
        raise DistributionCheckError(
            f"{archive.name} contains compiled cache files: {forbidden[:5]}"
        )


def _missing_suffixes(members: list[str], suffixes: tuple[str, ...]) -> list[str]:
    normalized = [member.replace("\\", "/") for member in members]
    return [suffix for suffix in suffixes if not any(item.endswith(suffix) for item in normalized)]


def inspect_archives(wheel: Path, sdist: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        wheel_members = archive.namelist()
    with tarfile.open(sdist, "r:gz") as archive:
        sdist_members = archive.getnames()

    _assert_clean_members(wheel_members, wheel)
    _assert_clean_members(sdist_members, sdist)

    wheel_suffixes = tuple(
        f".data/data/share/mealcircuit/{item}" for item in RESOURCE_FILES
    ) + tuple(f".data/data/share/mealcircuit/legal/{item}" for item in LEGAL_FILES)
    missing_wheel = _missing_suffixes(wheel_members, wheel_suffixes)
    missing_sdist = _missing_suffixes(sdist_members, RESOURCE_FILES + LEGAL_FILES)
    if missing_wheel:
        raise DistributionCheckError(
            f"{wheel.name} is missing runtime resources: {missing_wheel}"
        )
    if missing_sdist:
        raise DistributionCheckError(
            f"{sdist.name} is missing source resources: {missing_sdist}"
        )


def _run_installed_cli(target: Path, home: Path, command: str) -> dict:
    bootstrap = (
        "import runpy,sys;"
        f"sys.path.insert(0,{str(target)!r});"
        f"sys.argv=['mealcircuit',{command!r}];"
        "runpy.run_module('mealcircuit.agent_cli',run_name='__main__')"
    )
    environment = os.environ.copy()
    environment.update(
        {
            "MEALCIRCUIT_HOME": str(home),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    for name in ("MEALCIRCUIT_DB", "MEALCIRCUIT_DOCTRINE", "PYTHONPATH"):
        environment.pop(name, None)
    output = _run(
        [sys.executable, "-S", "-c", bootstrap],
        cwd=home.parent,
        env=environment,
    )
    try:
        start = output.index("{")
        value, _ = json.JSONDecoder().raw_decode(output[start:])
    except (ValueError, json.JSONDecodeError) as exc:
        raise DistributionCheckError(
            f"installed CLI {command!r} did not emit JSON:\n{output}"
        ) from exc
    if not isinstance(value, dict):
        raise DistributionCheckError(f"installed CLI {command!r} emitted non-object JSON")
    return value


def verify_isolated_install(uv: str, wheel: Path, workspace: Path) -> None:
    target = workspace / "installed"
    home = workspace / "private-home"
    home.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [uv, "pip", "install", "--target", str(target), "--no-deps", str(wheel)],
        cwd=workspace,
    )
    initialized = _run_installed_cli(target, home, "init")
    if Path(initialized.get("home", "")) != home:
        raise DistributionCheckError("installed init used an unexpected private home")
    status = _run_installed_cli(target, home, "doctor")
    if not status.get("settings_valid"):
        raise DistributionCheckError(
            f"installed doctor rejected the fresh settings template: {status.get('settings_error')}"
        )
    if status.get("doctrine_mode") != "composed":
        raise DistributionCheckError("installed doctor could not load the bundled core rules")


def main() -> None:
    uv = shutil.which("uv")
    if not uv:
        raise SystemExit("distribution check requires uv on PATH")
    with tempfile.TemporaryDirectory(prefix="mealcircuit-distribution-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source"
        source.mkdir()
        _copy_source_snapshot(source)
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        _run(
            [uv, "build", "--wheel", "--sdist", "--out-dir", str(workspace / "dist"), "."],
            cwd=source,
            env=environment,
        )
        wheels = list((workspace / "dist").glob("*.whl"))
        sdists = list((workspace / "dist").glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise DistributionCheckError(
                f"expected one wheel and one sdist, found {len(wheels)} and {len(sdists)}"
            )
        inspect_archives(wheels[0], sdists[0])
        verify_isolated_install(uv, wheels[0], workspace)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "wheel": wheels[0].name,
                    "sdist": sdists[0].name,
                    "isolated_init": True,
                    "isolated_doctor": True,
                },
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except DistributionCheckError as exc:
        raise SystemExit(str(exc)) from exc
