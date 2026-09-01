from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path
from urllib.parse import quote


ROOT = Path(__file__).resolve().parents[1]


def _pypi_ref(name: str, version: str) -> str:
    normalized = name.lower().replace("_", "-")
    return f"pkg:pypi/{quote(normalized, safe='.-')}@{quote(version, safe='.+-')}"


def _maven_ref(group: str, name: str, version: str) -> str:
    return (
        f"pkg:maven/{quote(group, safe='.')}/{quote(name, safe='.-')}"
        f"@{quote(version, safe='.+-')}"
    )


def _dependency_names(package: dict[str, object]) -> set[str]:
    names: set[str] = set()
    groups = [package.get("dependencies", [])]
    for key in ("optional-dependencies", "dev-dependencies"):
        value = package.get(key, {})
        if isinstance(value, dict):
            groups.extend(value.values())
    for group in groups:
        if not isinstance(group, list):
            continue
        for dependency in group:
            if isinstance(dependency, dict) and isinstance(dependency.get("name"), str):
                names.add(dependency["name"].lower().replace("_", "-"))
    return names


def _python_components(lock_path: Path) -> tuple[list[dict[str, object]], dict[str, set[str]]]:
    lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = lock.get("package")
    if not isinstance(packages, list):
        raise ValueError(f"invalid uv lock: {lock_path}")

    components: list[dict[str, object]] = []
    dependencies: dict[str, set[str]] = {}
    refs_by_name: dict[str, str] = {}
    package_by_name: dict[str, dict[str, object]] = {}
    for package in packages:
        if not isinstance(package, dict):
            continue
        name = package.get("name")
        version = package.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            continue
        normalized = name.lower().replace("_", "-")
        ref = _pypi_ref(normalized, version)
        refs_by_name[normalized] = ref
        package_by_name[normalized] = package
        components.append(
            {
                "type": "application" if normalized == "mealcircuit" else "library",
                "bom-ref": ref,
                "name": normalized,
                "version": version,
                "purl": ref,
                "properties": [{"name": "mealcircuit:lock", "value": "uv.lock"}],
            }
        )

    for name, package in package_by_name.items():
        ref = refs_by_name[name]
        dependencies[ref] = {
            refs_by_name[item]
            for item in _dependency_names(package)
            if item in refs_by_name and refs_by_name[item] != ref
        }
    return components, dependencies


def _android_components(lock_path: Path) -> list[dict[str, object]]:
    components: dict[str, dict[str, object]] = {}
    for raw_line in lock_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        coordinate, configurations = line.split("=", 1)
        if "releaseRuntimeClasspath" not in configurations.split(","):
            continue
        parts = coordinate.split(":")
        if len(parts) != 3:
            raise ValueError(f"unsupported Gradle lock coordinate: {coordinate}")
        group, name, version = parts
        ref = _maven_ref(group, name, version)
        components[ref] = {
            "type": "library",
            "bom-ref": ref,
            "group": group,
            "name": name,
            "version": version,
            "purl": ref,
            "properties": [
                {"name": "mealcircuit:lock", "value": "android/app/gradle.lockfile"}
            ],
        }
    return list(components.values())


def build_sbom(
    uv_lock: Path = ROOT / "uv.lock",
    gradle_lock: Path = ROOT / "android" / "app" / "gradle.lockfile",
) -> dict[str, object]:
    python_components, dependency_graph = _python_components(uv_lock)
    android_components = _android_components(gradle_lock)
    components = sorted(
        [*python_components, *android_components],
        key=lambda item: str(item["bom-ref"]),
    )
    project = next(
        (item for item in python_components if item["name"] == "mealcircuit"),
        None,
    )
    if project is None:
        raise ValueError("uv.lock does not contain the MealCircuit project")

    project_ref = str(project["bom-ref"])
    dependency_graph.setdefault(project_ref, set()).update(
        str(item["bom-ref"]) for item in android_components
    )
    dependencies = [
        {"ref": ref, "dependsOn": sorted(depends_on)}
        for ref, depends_on in sorted(dependency_graph.items())
    ]
    known_dependency_refs = {str(item["ref"]) for item in dependencies}
    dependencies.extend(
        {"ref": str(item["bom-ref"]), "dependsOn": []}
        for item in android_components
        if str(item["bom-ref"]) not in known_dependency_refs
    )

    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "metadata": {
            "component": {
                "type": "application",
                "bom-ref": project_ref,
                "name": "mealcircuit",
                "version": project["version"],
                "purl": project_ref,
            },
            "properties": [
                {
                    "name": "mealcircuit:scope",
                    "value": "resolved Python and Android release lockfiles",
                }
            ],
        },
        "components": components,
        "dependencies": sorted(dependencies, key=lambda item: str(item["ref"])),
    }


def validate_sbom(sbom: dict[str, object]) -> None:
    serialized = json.dumps(sbom, ensure_ascii=False)
    if str(ROOT.resolve()) in serialized or str(ROOT.resolve()).replace("\\", "/") in serialized:
        raise ValueError("SBOM contains an absolute checkout path")
    components = sbom.get("components")
    dependencies = sbom.get("dependencies")
    if not isinstance(components, list) or not components:
        raise ValueError("SBOM has no components")
    if not isinstance(dependencies, list) or not any(item.get("dependsOn") for item in dependencies):
        raise ValueError("SBOM has no dependency relationships")
    required = {
        "mealcircuit",
        "cryptography",
        "keyring",
        "pywebview",
        "okhttp",
        "zxing-android-embedded",
    }
    names = {str(item.get("name")) for item in components if isinstance(item, dict)}
    missing = required - names
    if missing:
        raise ValueError(f"SBOM is missing release dependencies: {sorted(missing)}")
    versions = {
        str(item.get("name")): str(item.get("version"))
        for item in components
        if isinstance(item, dict)
    }
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    if versions.get("mealcircuit") != project_version:
        raise ValueError("SBOM project version does not match pyproject.toml")
    if versions.get("cryptography") != "50.0.0":
        raise ValueError("SBOM does not contain the audited cryptography 50.0.0 lock")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the lock-derived release CycloneDX SBOM")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    sbom = build_sbom()
    validate_sbom(sbom)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(sbom, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(sbom['components'])} lock-derived components to {args.output}")


if __name__ == "__main__":
    main()
