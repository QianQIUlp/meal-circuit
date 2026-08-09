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
