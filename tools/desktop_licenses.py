from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import re
import shutil
import sys
import sysconfig
import tomllib
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_LEGAL_FILES = (
    "LICENSE",
    "THIRD_PARTY_LICENSES.md",
    "PRIVACY.md",
    "SECURITY.md",
    "DISCLAIMER.md",
)
LICENSE_PREFIXES = ("license", "licence", "copying", "notice")
FALLBACK_LICENSES = {
    ("proxy-tools", "0.1.0"): Path("legal/third-party/proxy_tools-0.1.0/LICENSE.txt")
}
FALLBACK_DISTRIBUTIONS = {
    ("pyobjc-core", "12.2.1"): ("pyobjc-framework-cocoa", "12.2.1"),
    ("pyobjc-framework-security", "12.2.1"): ("pyobjc-framework-cocoa", "12.2.1"),
    ("pyobjc-framework-uniformtypeidentifiers", "12.2.1"): (
        "pyobjc-framework-cocoa",
        "12.2.1",
    ),
}


class LicenseBundleError(RuntimeError):
    pass


def canonical_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_package_path(value: object) -> PurePosixPath:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise LicenseBundleError(f"unsafe distribution file path: {value}")
    return path


def distribution_license_files(distribution: importlib.metadata.Distribution) -> list[tuple[PurePosixPath, Path]]:
    selected: list[tuple[PurePosixPath, Path]] = []
    for entry in distribution.files or ():
        raw_relative = PurePosixPath(str(entry).replace("\\", "/"))
        if not raw_relative.name.lower().startswith(LICENSE_PREFIXES):
            continue
        relative = _safe_package_path(entry)
        if not any(part.lower().endswith(".dist-info") for part in relative.parts):
            continue
        source = Path(distribution.locate_file(entry)).resolve()
        if source.is_file() and source.stat().st_size:
            selected.append((relative, source))
    return sorted(selected, key=lambda item: str(item[0]).lower())


def _python_license_candidates() -> Iterable[Path]:
    roots = {
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(sys.executable).resolve().parent,
    }
    for key in ("base", "installed_base", "prefix", "srcdir"):
        value = sysconfig.get_config_var(key)
        if value:
            roots.add(Path(value))
    relatives = (
        Path("LICENSE.txt"),
        Path("LICENSE"),
        Path("Doc/license.rst"),
        Path("Resources/English.lproj/License.rtf"),
        Path("Resources/English.lproj/License.txt"),
    )
    seen: set[Path] = set()
    for root in sorted((item.resolve() for item in roots if item.exists()), key=str):
        for relative in relatives:
            candidate = (root / relative).resolve()
            if candidate not in seen:
                seen.add(candidate)
                yield candidate
        for pattern in ("LICENSE*", "License*", "license*"):
            for candidate in root.glob(f"**/{pattern}"):
                candidate = candidate.resolve()
                lowered = {part.lower() for part in candidate.parts}
                if "site-packages" not in lowered and candidate not in seen:
                    seen.add(candidate)
                    yield candidate


def find_python_license() -> Path:
    for candidate in _python_license_candidates():
        if not candidate.is_file() or candidate.stat().st_size < 1000:
            continue
        content = candidate.read_text(encoding="utf-8", errors="ignore").lower()
        if "python software foundation" in content or "psf license" in content:
            return candidate
    raise LicenseBundleError(f"Python runtime license was not found below {sys.base_prefix}")


def _locked_versions(lock_path: Path) -> dict[str, set[str]]:
    payload = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    result: dict[str, set[str]] = {}
    for package in payload.get("package", []):
        name = canonical_name(str(package["name"]))
        result.setdefault(name, set()).add(str(package["version"]))
    return result


def _direct_desktop_locks(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        requirement, _, marker = line.partition(";")
        marker = marker.strip()
        if marker:
            match = re.fullmatch(r"sys_platform\s*(==|!=)\s*['\"]([^'\"]+)['\"]", marker)
            if not match:
                raise LicenseBundleError(f"unsupported desktop lock marker: {marker}")
            operator, expected = match.groups()
            applies = sys.platform == expected
            if (operator == "==" and not applies) or (operator == "!=" and applies):
                continue
        match = re.fullmatch(r"\s*([A-Za-z0-9_.-]+)==([^\s]+)\s*", requirement)
        if not match:
            raise LicenseBundleError(f"desktop dependency is not exactly locked: {raw_line}")
        name, version = match.groups()
        result[canonical_name(name)] = version
    return result


def installed_distributions() -> list[importlib.metadata.Distribution]:
    distributions: dict[str, importlib.metadata.Distribution] = {}
    for distribution in importlib.metadata.distributions():
        raw_name = distribution.metadata.get("Name")
        if not raw_name:
            raise LicenseBundleError(f"installed distribution has no Name metadata: {distribution}")
        name = canonical_name(raw_name)
        if name == "mealcircuit":
            continue
        previous = distributions.get(name)
        if previous is not None and previous.version != distribution.version:
            raise LicenseBundleError(
                f"multiple installed versions for {name}: {previous.version}, {distribution.version}"
            )
        distributions[name] = distribution
    return [distributions[name] for name in sorted(distributions)]


def validate_locked_environment(
    project_root: Path, distributions: Iterable[importlib.metadata.Distribution]
) -> dict[str, str]:
    actual = {
        canonical_name(str(distribution.metadata["Name"])): distribution.version
        for distribution in distributions
    }
    locked = _locked_versions(project_root / "uv.lock")
    for name, version in actual.items():
        if version not in locked.get(name, set()):
            raise LicenseBundleError(f"installed distribution is not locked by uv.lock: {name}=={version}")
    for name, version in _direct_desktop_locks(project_root / "requirements/desktop.lock").items():
        if actual.get(name) != version:
            raise LicenseBundleError(
                f"locked desktop dependency is missing or has the wrong version: {name}=={version}"
            )
    return actual


def _copy_record(source: Path, destination: Path, relative: str) -> dict[str, str]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    if destination.stat().st_size == 0:
        raise LicenseBundleError(f"empty legal file: {source}")
    return {"path": relative, "sha256": sha256(destination)}


def collect_bundle(
    project_root: Path,
    output: Path,
    *,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    python_license: Path | None = None,
) -> Path:
    project_root = project_root.resolve()
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise LicenseBundleError(f"legal bundle output must be empty: {output}")
    output.mkdir(parents=True, exist_ok=True)
    selected_distributions = list(distributions if distributions is not None else installed_distributions())
    actual = validate_locked_environment(project_root, selected_distributions)
    distribution_map = {
        (canonical_name(str(distribution.metadata["Name"])), distribution.version): distribution
        for distribution in selected_distributions
    }
    records: dict[str, Any] = {
        "schema_version": 1,
        "project": [],
        "python": None,
        "distributions": [],
    }
    for filename in PROJECT_LEGAL_FILES:
        source = project_root / filename
        if not source.is_file():
            raise LicenseBundleError(f"required project legal file is missing: {source}")
        records["project"].append(
            _copy_record(source, output / filename, filename)
        )

    python_source = (python_license or find_python_license()).resolve()
    suffix = python_source.suffix if python_source.suffix else ".txt"
    python_relative = f"python/PYTHON_LICENSE{suffix}"
    records["python"] = _copy_record(
        python_source, output / Path(*PurePosixPath(python_relative).parts), python_relative
    )

    for distribution in sorted(
        selected_distributions,
        key=lambda item: canonical_name(str(item.metadata["Name"])),
    ):
        name = canonical_name(str(distribution.metadata["Name"]))
        version = distribution.version
        licenses = distribution_license_files(distribution)
        if not licenses:
            fallback = FALLBACK_LICENSES.get((name, version))
            fallback_distribution = FALLBACK_DISTRIBUTIONS.get((name, version))
            if fallback is not None:
                source = (project_root / fallback).resolve()
                if not source.is_file():
                    raise LicenseBundleError(f"fallback license is missing for {name}=={version}: {source}")
                licenses = [(PurePosixPath("upstream/LICENSE.txt"), source)]
            elif fallback_distribution is not None:
                source_distribution = distribution_map.get(fallback_distribution)
                if source_distribution is None:
                    raise LicenseBundleError(
                        f"fallback distribution is missing for {name}=={version}: "
                        f"{fallback_distribution[0]}=={fallback_distribution[1]}"
                    )
                licenses = [
                    (PurePosixPath("upstream") / relative.name, source)
                    for relative, source in distribution_license_files(source_distribution)
                ]
                if not licenses:
                    raise LicenseBundleError(
                        f"fallback distribution has no bundled license text for {name}=={version}"
                    )
            else:
                raise LicenseBundleError(f"installed distribution has no bundled license text: {name}=={version}")
        distribution_record = {"name": name, "version": version, "licenses": []}
        for source_relative, source in licenses:
            relative = PurePosixPath("distributions") / f"{name}-{version}" / source_relative
            destination = output / Path(*relative.parts)
            distribution_record["licenses"].append(
                _copy_record(source, destination, relative.as_posix())
            )
        records["distributions"].append(distribution_record)

    expected = {item["name"]: item["version"] for item in records["distributions"]}
    if expected != actual:
        raise LicenseBundleError("legal manifest does not match the locked installed environment")
    manifest = output / "manifest.json"
    manifest.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    verify_bundle(output, project_root=project_root, distributions=selected_distributions)
    return output


def verify_bundle(
    bundle: Path,
    *,
    project_root: Path | None = None,
    distributions: Iterable[importlib.metadata.Distribution] | None = None,
    check_environment: bool = True,
) -> dict[str, Any]:
    bundle = bundle.resolve()
    manifest_path = bundle / "manifest.json"
    if not manifest_path.is_file():
        raise LicenseBundleError(f"legal manifest is missing: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise LicenseBundleError("unsupported legal manifest schema")
    project_records = payload.get("project")
    if {item.get("path") for item in project_records or []} != set(PROJECT_LEGAL_FILES):
        raise LicenseBundleError("legal bundle does not contain the five required project documents")
    records = list(project_records)
    python_record = payload.get("python")
    if not isinstance(python_record, dict) or not str(python_record.get("path", "")).startswith("python/"):
        raise LicenseBundleError("legal bundle does not contain a Python runtime license")
    records.append(python_record)
    distribution_records = payload.get("distributions")
    if not isinstance(distribution_records, list) or not distribution_records:
        raise LicenseBundleError("legal bundle has no third-party distribution licenses")
    manifest_environment: dict[str, str] = {}
    for distribution in distribution_records:
        name = canonical_name(str(distribution.get("name", "")))
        version = str(distribution.get("version", ""))
        licenses = distribution.get("licenses")
        if not name or not version or not isinstance(licenses, list) or not licenses:
            raise LicenseBundleError(f"invalid distribution legal record: {distribution}")
        if name in manifest_environment:
            raise LicenseBundleError(f"duplicate distribution legal record: {name}")
        manifest_environment[name] = version
        records.extend(licenses)
    for record in records:
        relative = _safe_package_path(record.get("path", ""))
        path = bundle / Path(*relative.parts)
        if not path.is_file() or path.stat().st_size == 0:
            raise LicenseBundleError(f"legal bundle file is missing or empty: {relative}")
        if sha256(path) != record.get("sha256"):
            raise LicenseBundleError(f"legal bundle checksum mismatch: {relative}")
    if check_environment:
        if project_root is None:
            raise LicenseBundleError("project root is required for environment verification")
        selected = list(distributions if distributions is not None else installed_distributions())
        actual = validate_locked_environment(project_root.resolve(), selected)
        if manifest_environment != actual:
            raise LicenseBundleError("legal bundle distributions do not match the locked installed environment")
    return payload


def find_bundle(artifact_root: Path) -> Path:
    artifact_root = artifact_root.resolve()
    matches = sorted(
        manifest.parent
        for manifest in artifact_root.rglob("manifest.json")
        if manifest.parent.name == "legal"
    )
    if len(matches) != 1:
        raise LicenseBundleError(
            f"expected exactly one embedded legal bundle below {artifact_root}, found {len(matches)}"
        )
    return matches[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect and verify desktop binary license texts")
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser("collect")
    collect.add_argument("--project-root", type=Path, default=Path.cwd())
    collect.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--project-root", type=Path, default=Path.cwd())
    location = verify.add_mutually_exclusive_group(required=True)
    location.add_argument("--bundle", type=Path)
    location.add_argument("--artifact-root", type=Path)
    verify.add_argument("--no-environment", action="store_true")
    args = parser.parse_args()
    if args.command == "collect":
        output = collect_bundle(args.project_root, args.output)
        print(output)
    else:
        bundle = args.bundle if args.bundle is not None else find_bundle(args.artifact_root)
        verify_bundle(
            bundle,
            project_root=args.project_root,
            check_environment=not args.no_environment,
        )
        print(bundle.resolve())


if __name__ == "__main__":
    main()
