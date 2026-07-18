from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_LINE = re.compile(r"^[A-Za-z0-9_.-]+==[^;\s]+(?:; .+)?$")
EXPECTED = {
    "desktop.lock": {"cryptography", "keyring", "pyinstaller", "pywebview"},
    "sync-server.lock": {
        "alembic", "argon2-cffi", "fastapi", "httpx", "psycopg", "pydantic",
        "sqlalchemy", "uvicorn",
    },
}


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_locks() -> set[str]:
    direct: set[str] = set()
    for filename, expected in EXPECTED.items():
        path = ROOT / "requirements" / filename
        names: set[str] = set()
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if not LOCK_LINE.fullmatch(line):
                raise SystemExit(f"{path}:{number}: dependency is not exactly pinned")
            name = normalized(line.split("==", 1)[0])
            if name in names:
                raise SystemExit(f"{path}:{number}: duplicate dependency {name}")
            names.add(name)
        missing = {normalized(item) for item in expected} - names
        if missing:
            raise SystemExit(f"{path}: missing direct dependencies: {sorted(missing)}")
        direct.update(normalized(item) for item in expected)
    return direct


def check_android() -> None:
    wrapper = (ROOT / "android" / "gradle" / "wrapper" / "gradle-wrapper.properties").read_text()
    match = re.search(r"^distributionSha256Sum=([0-9a-f]{64})$", wrapper, re.MULTILINE)
    if not match:
        raise SystemExit("Gradle distribution SHA-256 is missing")
    build = (ROOT / "android" / "app" / "build.gradle.kts").read_text()
    if re.search(r'"[^"\n]*\+[^"\n]*"', build) or "SNAPSHOT" in build:
        raise SystemExit("Android dependencies must not use dynamic or snapshot versions")
    for coordinate in re.findall(r'(?:implementation|ksp|testImplementation|androidTestImplementation)\("([^"]+)"\)', build):
        if coordinate.startswith("androidx.compose."):
            continue
        if coordinate.count(":") < 2:
            raise SystemExit(f"Android dependency lacks an exact version: {coordinate}")
    lock = ROOT / "android" / "app" / "gradle.lockfile"
    if not lock.is_file() or "androidx.compose" not in lock.read_text(encoding="utf-8"):
        raise SystemExit("Android resolved dependency lock is missing or incomplete")


def check_uv_lock() -> set[str]:
    path = ROOT / "uv.lock"
    if not path.is_file():
        raise SystemExit("uv.lock is missing")
    value = tomllib.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value.get("revision"), int) or not isinstance(value.get("package"), list):
        raise SystemExit("uv.lock is invalid")
    names = {normalized(item.get("name", "")) for item in value["package"] if isinstance(item, dict)}
    expected = {
        "mealcircuit", "cryptography", "keyring", "pyinstaller", "pywebview",
        "fastapi", "sqlalchemy", "alembic", "psycopg", "argon2-cffi", "uvicorn",
        "jsonschema", "pip-audit", "tzdata",
    }
    missing = {normalized(item) for item in expected} - names
    if missing:
        raise SystemExit(f"uv.lock is missing resolved packages: {sorted(missing)}")
    return {"jsonschema", "pip-audit", "setuptools", "uv"}


def check_release_tools() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    if not re.search(r"APPIMAGETOOL_X86_64_SHA256: [0-9a-f]{64}", workflow):
        raise SystemExit("AppImage build tool SHA-256 is not pinned")
    if "sha256sum --check --strict" not in workflow:
        raise SystemExit("AppImage build tool checksum is not enforced")
    check_release_workflow(workflow)


def check_release_workflow(workflow: str) -> None:
    forbidden_desktop_gates = (
        "Require Authenticode secrets for tagged releases",
        "Require Apple signing secrets for tagged releases",
    )
    for name in forbidden_desktop_gates:
        if name in workflow:
            raise SystemExit(f"Desktop tagged releases must not hard-fail on missing signing credentials: {name}")

    availability = {
        "WINDOWS_SIGNING_AVAILABLE": (
            "WINDOWS_SIGNING_AVAILABLE: ${{ secrets.WINDOWS_CERTIFICATE_BASE64 != '' "
            "&& secrets.WINDOWS_CERTIFICATE_PASSWORD != '' }}"
        ),
        "APPLE_SIGNING_AVAILABLE": (
            "APPLE_SIGNING_AVAILABLE: ${{ secrets.APPLE_CERTIFICATE_BASE64 != '' "
            "&& secrets.APPLE_CERTIFICATE_PASSWORD != '' && secrets.APPLE_SIGNING_IDENTITY != '' "
            "&& secrets.APPLE_ID != '' && secrets.APPLE_APP_PASSWORD != '' "
            "&& secrets.APPLE_TEAM_ID != '' }}"
        ),
        "ANDROID_SIGNING_AVAILABLE": (
            "ANDROID_SIGNING_AVAILABLE: ${{ secrets.ANDROID_KEYSTORE_BASE64 != '' "
            "&& secrets.ANDROID_KEYSTORE_PASSWORD != '' && secrets.ANDROID_KEY_ALIAS != '' "
            "&& secrets.ANDROID_KEY_PASSWORD != '' }}"
        ),
    }
    for variable, expression in availability.items():
        if workflow.count(f"{variable}:") != 1 or expression not in workflow:
            raise SystemExit(f"{variable} must require its complete credential set")

    required_snippets = {
        "Windows unsigned tagged-release warning": (
            "- name: Warn when Windows tagged release is unsigned",
            "if: startsWith(github.ref, 'refs/tags/v') && env.WINDOWS_SIGNING_AVAILABLE != 'true'",
        ),
        "Apple unsigned tagged-release warning": (
            "- name: Warn when macOS tagged release lacks Developer ID signing",
            "if: startsWith(github.ref, 'refs/tags/v') && env.APPLE_SIGNING_AVAILABLE != 'true'",
        ),
        "Android tagged-release omission policy": (
            "- name: Warn when Android assets are omitted from unsigned tagged release",
            "if: startsWith(github.ref, 'refs/tags/v') && env.ANDROID_SIGNING_AVAILABLE != 'true'",
            "Android APK and AAB are omitted from this tagged release",
            "if: ${{ !startsWith(github.ref, 'refs/tags/v') || env.ANDROID_SIGNING_AVAILABLE == 'true' }}",
            "if-no-files-found: error",
        ),
        "release build dependencies": (
            "needs: [windows, macos-universal, linux, android]",
        ),
    }
    for policy, snippets in required_snippets.items():
        if any(snippet not in workflow for snippet in snippets):
            raise SystemExit(f"Release workflow is missing required policy: {policy}")


def main() -> None:
    direct = check_locks()
    direct.update(check_uv_lock())
    check_android()
    check_release_tools()
    licenses = (ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8").lower()
    missing = sorted(item for item in direct if item not in licenses)
    if missing:
        raise SystemExit(f"THIRD_PARTY_LICENSES.md is missing direct packages: {missing}")
    print("Universal Python lock, dependency pins, Gradle checksum, and direct license inventory are complete")


if __name__ == "__main__":
    main()
