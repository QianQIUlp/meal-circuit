from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:18-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
PYTHON_IMAGE = "python:3.13-slim@sha256:9662417aace5ae7b8e2609cce472b72a8958e134ba372808abe9cc1a0c0125e6"
CADDY_IMAGE = "caddy:2.11.4-alpine@sha256:5f5c8640aae01df9654968d946d8f1a56c497f1dd5c5cda4cf95ab7c14d58648"
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
    coordinates = re.findall(r'(?:implementation|ksp|testImplementation|androidTestImplementation)\("([^"]+)"\)', build)
    if any("+" in coordinate or "SNAPSHOT" in coordinate for coordinate in coordinates):
        raise SystemExit("Android dependencies must not use dynamic or snapshot versions")
    for coordinate in coordinates:
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


def check_version_sources() -> None:
    version_tool = ROOT / "tools" / "version.py"
    if not version_tool.is_file():
        raise SystemExit("tools/version.py must be tracked as the version contract")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = project.get("project", {}).get("version")
    if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise SystemExit("pyproject.toml must contain the canonical SemVer project version")

    gradle = (ROOT / "android" / "app" / "build.gradle.kts").read_text(encoding="utf-8")
    if 'providers.gradleProperty("mealcircuitVersion").orElse' in gradle:
        raise SystemExit("Android version must not have an independent default")
    if "versionCode = 30000" in gradle or 'versionName = "0.3.0"' in gradle:
        raise SystemExit("Android version must be derived from the contract output")

    inno = (ROOT / "packaging" / "windows" / "MealCircuit.iss").read_text(encoding="utf-8")
    if '#define MyAppVersion "0.3.0"' in inno or "#error MyAppVersion" not in inno:
        raise SystemExit("Inno Setup must require the workflow-provided version")

    setup_uv = re.findall(
        r"astral-sh/setup-uv@[0-9a-f]{40}.*?\n\s+with:\n\s+version: \"([^\"]+)\"",
        "\n".join(
            path.read_text(encoding="utf-8")
            for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        ),
        re.DOTALL,
    )
    if not setup_uv or any(item != "0.8.22" for item in setup_uv):
        raise SystemExit(f"all setup-uv jobs must use 0.8.22, found: {setup_uv}")

    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        if re.search(r"^\s*VERSION:\s*0\.3\.0\s*$", workflow.read_text(encoding="utf-8"), re.MULTILINE):
            raise SystemExit(f"{workflow}: workflow version must come from the contract job")

    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        if "actionlint" in text and (
            "ACTIONLINT_VERSION: 1.7.12" not in text
            or "ACTIONLINT_SHA256: 8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8" not in text
        ):
            raise SystemExit(f"{workflow}: actionlint version and SHA must be immutable")


def check_container_pins() -> None:
    workflow = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    if f"image: {POSTGRES_IMAGE}" not in workflow:
        raise SystemExit("CI PostgreSQL service must use the approved immutable image digest")

    compose = (ROOT / "sync_server" / "compose.yaml").read_text(encoding="utf-8")
    for image in (POSTGRES_IMAGE, CADDY_IMAGE):
        if f"image: {image}" not in compose:
            raise SystemExit(f"sync_server/compose.yaml is missing immutable image pin: {image}")
    dockerfile = (ROOT / "sync_server" / "Dockerfile").read_text(encoding="utf-8")
    if f"FROM {PYTHON_IMAGE}" not in dockerfile:
        raise SystemExit("sync_server/Dockerfile must use the approved immutable Python image digest")

    for line in compose.splitlines():
        if line.strip().startswith("image:") and "@sha256:" not in line:
            raise SystemExit(f"compose image is not immutable: {line.strip()}")
    for line in dockerfile.splitlines():
        if line.strip().startswith("FROM ") and "@sha256:" not in line:
            raise SystemExit(f"Dockerfile base image is not immutable: {line.strip()}")


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

    windows_match = re.search(
        r"(?ms)^  windows:\n.*?(?=^  [A-Za-z0-9_-]+:\n)",
        workflow,
    )
    if not windows_match:
        raise SystemExit("Release workflow is missing the Windows job")
    windows_workflow = windows_match.group(0)
    for action, ref in re.findall(r"uses: ([^@\s]+)@([^\s#]+)", windows_workflow):
        if not re.fullmatch(r"[0-9a-f]{40}", ref):
            raise SystemExit(f"Windows actions use immutable pins: {action}")
    required_windows_snippets = {
        "Windows actions use immutable pins": (
            "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5",
            "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e # v6.8.0",
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02 # v4",
        ),
        "Windows packaged smoke test waits for the GUI process": (
            '$process = Start-Process -FilePath ".\\dist\\MealCircuit\\MealCircuit.exe" '
            '-ArgumentList "--smoke-test" -Wait -PassThru\n'
            '          if ($process.ExitCode -ne 0) { throw "Packaged smoke test failed',
        ),
        "Windows uv security pin": (
            'version: "0.8.22"',
        ),
        "Windows Inno Setup pin": (
            'https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe',
            'if ($innoHash -ne "9c73c3bae7ed48d44112a0f48e66742c00090bdb5bef71d9d3c056c66e97b732")',
            "Get-AuthenticodeSignature $innoInstaller",
            '$innoDir = Join-Path $env:RUNNER_TEMP "inno-6.7.3"',
        ),
        "Windows final artifacts are smoke tested": (
            "- name: Smoke test portable ZIP",
            "Expand-Archive",
            "- name: Smoke test installed application and uninstaller",
            "unins000.exe",
            "if (Test-Path $installDir) { throw",
        ),
        "Windows artifact upload fails closed": (
            "name: desktop-windows",
            "if-no-files-found: error",
        ),
    }
    for policy, snippets in required_windows_snippets.items():
        if any(snippet not in windows_workflow for snippet in snippets):
            raise SystemExit(f"Release workflow is missing required policy: {policy}")

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
        "Android strict AAB signer verification": (
            "- name: Verify Android signatures when configured",
            "ANDROID_KEYSTORE_PASSWORD: ${{ secrets.ANDROID_KEYSTORE_PASSWORD }}",
            "ANDROID_KEY_ALIAS: ${{ secrets.ANDROID_KEY_ALIAS }}",
            "jarsigner -verify -strict \\",
            "-keystore \"$RUNNER_TEMP/mealcircuit.jks\" \\",
            "-storepass \"$ANDROID_KEYSTORE_PASSWORD\" \\",
            "android/app/build/outputs/bundle/release/app-release.aab \"$ANDROID_KEY_ALIAS\"",
        ),
        "Android release assets are flattened": (
            "- name: Stage Android release assets",
            "mkdir -p \"$RUNNER_TEMP/android-release\"",
            "install -m 644 android/app/build/outputs/apk/release/app-release.apk \"$RUNNER_TEMP/android-release/app-release.apk\"",
            "install -m 644 android/app/build/outputs/bundle/release/app-release.aab \"$RUNNER_TEMP/android-release/app-release.aab\"",
            "path: ${{ runner.temp }}/android-release/*",
        ),
        "release build dependencies": (
            "needs: [contract, windows, macos-universal, linux, android]",
        ),
    }
    for policy, snippets in required_snippets.items():
        if any(snippet not in workflow for snippet in snippets):
            raise SystemExit(f"Release workflow is missing required policy: {policy}")


def check_workflow_action_pins() -> None:
    pattern = re.compile(r"^\s*(?:-\s*)?uses:\s*([^@\s]+)@([^\s#]+)", re.MULTILINE)
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.y*ml")):
        text = workflow.read_text(encoding="utf-8")
        for match in pattern.finditer(text):
            if not re.fullmatch(r"[0-9a-f]{40}", match.group(2)):
                raise SystemExit(
                    f"{workflow}: action {match.group(1)} must use an immutable commit SHA"
                )


def main() -> None:
    check_workflow_action_pins()
    check_version_sources()
    check_container_pins()
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
