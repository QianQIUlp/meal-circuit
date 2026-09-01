from __future__ import annotations

import argparse
import json
import stat
import sys
import zipfile
from collections.abc import Iterable
from pathlib import Path


REQUIRED_LEGAL_FILES = (
    "MEALCIRCUIT_LICENSE.txt",
    "THIRD_PARTY_NOTICES.md",
    "APACHE-2.0.txt",
    "PRIVACY.md",
    "SECURITY.md",
    "DISCLAIMER.md",
)

MAX_LEGAL_FILE_SIZE = 1024 * 1024

TEXT_REQUIREMENTS: dict[str, tuple[int, tuple[str, ...]]] = {
    "APACHE-2.0.txt": (
        10_000,
        (
            "Apache License",
            "Version 2.0, January 2004",
            "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION",
            "END OF TERMS AND CONDITIONS",
            "APPENDIX: How to apply the Apache License to your work.",
        ),
    ),
    "THIRD_PARTY_NOTICES.md": (
        500,
        (
            "MealCircuit's Android application",
            "Kotlin",
            "AndroidX",
            "OkHttp",
            "Okio",
            "ZXing",
            "JSpecify",
            "android/app/gradle.lockfile",
            "APACHE-2.0.txt",
        ),
    ),
    "PRIVACY.md": (
        500,
        (
            "has no telemetry",
            "does not require registration",
            "private Room database",
            "does not call an external AI provider",
            "API keys are not synchronized or exported",
        ),
    ),
    "SECURITY.md": (
        500,
        (
            "local-first",
            "end-to-end encrypted",
            "private security advisory",
            "private reporting channel",
            "has not received an independent third-party audit",
        ),
    ),
    "DISCLAIMER.md": (
        200,
        (
            "does not diagnose, treat or prevent disease",
            "not a medical device",
            "registered nutrition professional",
        ),
    ),
}


class ArtifactLegalError(ValueError):
    """Raised when a release archive does not contain trustworthy legal assets."""


def _archive_legal_prefix(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".apk":
        return "assets/legal"
    if suffix == ".aab":
        return "base/assets/legal"
    raise ArtifactLegalError(f"unsupported Android artifact type: {path.name}")


def _safe_member_key(name: str) -> str:
    if not name or "\x00" in name or "\\" in name or name.startswith("/"):
        raise ArtifactLegalError(f"unsafe ZIP member path: {name!r}")
    trimmed = name[:-1] if name.endswith("/") else name
    parts = trimmed.split("/")
    if not trimmed or any(part in {"", ".", ".."} for part in parts):
        raise ArtifactLegalError(f"unsafe ZIP member path: {name!r}")
    if len(parts[0]) >= 2 and parts[0][1] == ":":
        raise ArtifactLegalError(f"unsafe ZIP member path: {name!r}")
    return trimmed


def _canonical_text(data: bytes, *, label: str) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactLegalError(f"{label} is not UTF-8: {exc}") from exc
    if "\r" in text.replace("\r\n", ""):
        raise ArtifactLegalError(f"{label} contains unsupported bare carriage returns")
    return text.replace("\r\n", "\n")


def _read_required_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    if info.is_dir():
        raise ArtifactLegalError(f"required legal asset is a directory: {info.filename}")
    if info.flag_bits & 0x1:
        raise ArtifactLegalError(f"required legal asset is encrypted: {info.filename}")
    if stat.S_ISLNK((info.external_attr >> 16) & 0xFFFF):
        raise ArtifactLegalError(f"required legal asset is a symbolic link: {info.filename}")
    if info.file_size > MAX_LEGAL_FILE_SIZE:
        raise ArtifactLegalError(
            f"required legal asset is unexpectedly large: {info.filename} ({info.file_size} bytes)"
        )
    try:
        data = archive.read(info)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise ArtifactLegalError(f"cannot read {info.filename}: {exc}") from exc
    if len(data) != info.file_size:
        raise ArtifactLegalError(f"truncated legal asset: {info.filename}")
    return data


def verify_android_artifact(path: str | Path, project_license: str | Path) -> dict[str, object]:
    artifact = Path(path).resolve()
    license_path = Path(project_license).resolve()
    prefix = _archive_legal_prefix(artifact)
    if not artifact.is_file():
        raise ArtifactLegalError(f"Android artifact does not exist: {artifact}")
    if not license_path.is_file():
        raise ArtifactLegalError(f"project LICENSE does not exist: {license_path}")

    try:
        project_license_text = _canonical_text(license_path.read_bytes(), label=str(license_path))
    except OSError as exc:
        raise ArtifactLegalError(f"cannot read project LICENSE: {exc}") from exc

    try:
        with zipfile.ZipFile(artifact) as archive:
            members: dict[str, zipfile.ZipInfo] = {}
            legal_casefold_names: dict[str, str] = {}
            for info in archive.infolist():
                member_key = _safe_member_key(info.filename)
                if member_key in members:
                    raise ArtifactLegalError(f"duplicate ZIP member path: {member_key}")
                members[member_key] = info

                if member_key.startswith(f"{prefix}/"):
                    folded = member_key.casefold()
                    previous = legal_casefold_names.get(folded)
                    if previous is not None and previous != member_key:
                        raise ArtifactLegalError(
                            f"case-colliding legal asset paths: {previous!r} and {member_key!r}"
                        )
                    legal_casefold_names[folded] = member_key

            expected_paths = {name: f"{prefix}/{name}" for name in REQUIRED_LEGAL_FILES}
            missing = [name for name, member in expected_paths.items() if member not in members]
            if missing:
                raise ArtifactLegalError(f"missing legal assets: {', '.join(missing)}")

            contents = {
                name: _read_required_member(archive, members[member])
                for name, member in expected_paths.items()
            }
    except zipfile.BadZipFile as exc:
        raise ArtifactLegalError(f"invalid Android ZIP archive {artifact.name}: {exc}") from exc
    except OSError as exc:
        raise ArtifactLegalError(f"cannot open Android artifact {artifact}: {exc}") from exc

    bundled_license = _canonical_text(
        contents["MEALCIRCUIT_LICENSE.txt"], label="MEALCIRCUIT_LICENSE.txt"
    )
    if bundled_license != project_license_text:
        raise ArtifactLegalError("bundled MealCircuit license does not exactly match project LICENSE")

    for name, (minimum_length, markers) in TEXT_REQUIREMENTS.items():
        text = _canonical_text(contents[name], label=name)
        if len(text) < minimum_length:
            raise ArtifactLegalError(
                f"{name} is truncated: {len(text)} characters, expected at least {minimum_length}"
            )
        whitespace_normalized = " ".join(text.split())
        missing_markers = [
            marker for marker in markers if " ".join(marker.split()) not in whitespace_normalized
        ]
        if missing_markers:
            raise ArtifactLegalError(
                f"{name} is missing required markers: {', '.join(missing_markers)}"
            )

    return {
        "artifact": str(artifact),
        "kind": artifact.suffix.lower().lstrip("."),
        "legal_prefix": prefix,
        "legal_files": list(REQUIRED_LEGAL_FILES),
        "ok": True,
    }


def check_artifacts(
    artifacts: Iterable[str | Path], project_license: str | Path
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    verified: list[dict[str, object]] = []
    failures: list[dict[str, str]] = []
    for artifact in artifacts:
        try:
            verified.append(verify_android_artifact(artifact, project_license))
        except ArtifactLegalError as exc:
            failures.append({"artifact": str(Path(artifact)), "error": str(exc)})
    return verified, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify legal assets inside final MealCircuit APK and AAB release archives."
    )
    parser.add_argument("artifacts", nargs="+", help="Final .apk and/or .aab artifacts")
    parser.add_argument(
        "--license",
        default=Path(__file__).resolve().parents[1] / "LICENSE",
        help="Project LICENSE that the bundled license must match",
    )
    args = parser.parse_args(argv)

    verified, failures = check_artifacts(args.artifacts, args.license)
    print(
        json.dumps(
            {"ok": not failures, "verified": verified, "failures": failures},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
